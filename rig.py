#!/usr/bin/env python3
"""
Light sensor rig: Pi side (Raspberry Pi 5).

Reads the TSL2591 once a second, runs one LED test, and publishes every
sensor reading and every LED change over MQTT. Each message carries a timestamp
taken on the Pi at the moment it happened:

    rig/sensor   one message per sensor reading
    rig/led      one message every time either LED changes (every ramp step too)
    rig/meta     one message when a run starts and one when it ends

Nothing is analysed here. The Pi only records what the sensor saw and what it
just told the LEDs to do. A copy of every message is also written to
~/rig-data/<run>.jsonl on the Pi, so a Wi-Fi drop can't lose any data.

Examples:
    python rig.py --broker 192.168.1.50 --test steps
    python rig.py --broker 192.168.1.50 --test ramp --ramp-seconds 300
    python rig.py --broker 192.168.1.50 --test pollution
    python rig.py --broker 192.168.1.50 --test hold --level 0.5 --hold-minutes 60
"""

import argparse
import atexit
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import adafruit_tsl2591
import board
import paho.mqtt.client as mqtt
from gpiozero import LED, PWMLED

# Wiring: BCM GPIO numbers, physical header pin in brackets
TEST_LED_PIN = 18        # [pin 12] -> FemtoBuck "Dim in". Dimmed by gpiozero, which on the Pi 5
                         # times the PWM in software (lgpio) and sets the duty in whole percent.
POLLUTION_LED_PIN = 12   # [pin 32] -> 150 ohm resistor -> pollution LED, on hardware PWM.
                         # Sessions before the pollution-levels block used GPIO 23 [pin 16], on/off only.
PWM_FREQUENCY = 1000     # Hz, for both LEDs

# Hardware PWM on the Raspberry Pi 5: the RP1 chip's PWM0 block, through Linux's sysfs interface
PWM_CHANNELS = {12: 0, 13: 1, 18: 2, 19: 3}          # GPIO -> PWM0 channel
PWM_OVERLAY = "dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4"
PWM_SYSFS = Path(os.environ.get("RIG_PWM_SYSFS", "/sys/class/pwm"))  # only overridden for testing

GAINS = {
    "low": adafruit_tsl2591.GAIN_LOW,     # 1x
    "med": adafruit_tsl2591.GAIN_MED,     # ~25x
    "high": adafruit_tsl2591.GAIN_HIGH,   # ~428x
    "max": adafruit_tsl2591.GAIN_MAX,     # ~9876x
}
INTEGRATIONS = {
    100: adafruit_tsl2591.INTEGRATIONTIME_100MS,
    200: adafruit_tsl2591.INTEGRATIONTIME_200MS,
    300: adafruit_tsl2591.INTEGRATIONTIME_300MS,
    400: adafruit_tsl2591.INTEGRATIONTIME_400MS,
    500: adafruit_tsl2591.INTEGRATIONTIME_500MS,
    600: adafruit_tsl2591.INTEGRATIONTIME_600MS,
}


def stamp():
    """Wall-clock time (readable) and monotonic time (never jumps; best for intervals)."""
    return time.time(), time.monotonic()


class Publisher:
    """Sends each message to the broker and appends a backup copy to a local file."""

    def __init__(self, broker, port, run_id, backup_dir):
        self.run_id = run_id
        self.backup_path = Path(backup_dir) / f"{run_id}.jsonl"
        self.backup = None
        self.lock = threading.Lock()
        self.seq = {}
        self.last_info = None
        self.closing = False
        self.connected = threading.Event()

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"light-rig-pi-{run_id}")
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.reconnect_delay_set(min_delay=1, max_delay=10)
        self.client.connect_async(broker, port, keepalive=30)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            print(f"Broker refused the connection: {reason_code}")
            return
        print("Connected to the broker.")
        self.connected.set()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        if self.connected.is_set() and not self.closing:
            print(f"Lost the broker ({reason_code}); retrying. "
                  f"Everything is still being saved to {self.backup_path}")
        self.connected.clear()

    def send(self, stream, fields, t=None, mono=None):
        if t is None:
            t, mono = stamp()
        with self.lock:
            self.seq[stream] = self.seq.get(stream, 0) + 1
            message = {"run": self.run_id, "stream": stream, "seq": self.seq[stream],
                       "t": round(t, 6), "mono": round(mono, 6), **fields}
            line = json.dumps(message)
            if self.backup is None:
                self.backup_path.parent.mkdir(parents=True, exist_ok=True)
                self.backup = open(self.backup_path, "a", buffering=1)  # line-buffered
            self.backup.write(line + "\n")
            self.last_info = self.client.publish(f"rig/{stream}", line, qos=1)

    def close(self):
        if self.last_info is not None:
            try:
                self.last_info.wait_for_publish(timeout=5)
            except (RuntimeError, ValueError):
                pass  # not connected; the backup file has everything
        self.closing = True
        self.client.disconnect()
        self.client.loop_stop()
        if self.backup is not None:
            self.backup.close()


class PwmSetupError(RuntimeError):
    """Hardware PWM isn't usable yet; the message says how to set it up."""


def find_pwm_chip(chip=None):
    """The sysfs pwmchip of the Pi 5's RP1 PWM0 block. Its number differs between kernels,
    so it's found by its device (…1f00098000.pwm) unless given."""
    if chip is not None:
        path = PWM_SYSFS / f"pwmchip{chip}"
        if not path.exists():
            raise PwmSetupError(f"There's no {path}.")
        return path
    chips = sorted(PWM_SYSFS.glob("pwmchip*"))
    for path in chips:
        if "98000.pwm" in str(path.resolve()):
            return path
    found = "\n".join(f"  {path.name} -> {path.resolve()}" for path in chips) or "  none"
    raise PwmSetupError(
        "Hardware PWM for the pollution LED isn't enabled. Add this line to /boot/firmware/config.txt "
        f"and reboot:\n  {PWM_OVERLAY}\nPWM controllers found:\n{found}\n"
        "If one of them is the right one, give its number with --pollution-pwmchip.")


class HardwarePWM:
    """One hardware PWM output, used like gpiozero's PWMLED: .value (0-1), .on(), .off().
    Raspberry Pi 5 only. Setup: PWM_OVERLAY in /boot/firmware/config.txt and a reboot; and,
    if writing to the PWM files is refused, `sudo sh setup_pwm.sh` after each boot."""

    def __init__(self, gpio, frequency, chip=None):
        if gpio not in PWM_CHANNELS:
            raise PwmSetupError(f"GPIO {gpio} has no hardware PWM on the Pi 5. Use GPIO 12, 13, 18 or 19.")
        self.gpio, self.channel = gpio, PWM_CHANNELS[gpio]
        self.chip = find_pwm_chip(chip)
        self.path = self.chip / f"pwm{self.channel}"
        self.period_ns = round(1e9 / frequency)
        self._value = 0.0
        try:
            if not self.path.exists():
                self._write_to(self.chip / "export", self.channel)
            deadline = time.monotonic() + 3  # a new channel takes a moment to appear and become writable
            while not (self.path / "period").exists() or not os.access(self.path / "period", os.W_OK):
                if time.monotonic() > deadline:
                    break
                time.sleep(0.1)
            # Off first, so a channel left running (say, after a crash) takes the new settings
            self._write("enable", 0)
            if int(self._read("duty_cycle") or 0) > self.period_ns:
                self._write("duty_cycle", 0)
            self._write("period", self.period_ns)
            self._write("duty_cycle", 0)
            self._write("enable", 1)
        except PermissionError:
            raise PwmSetupError(f"No permission to use hardware PWM in {self.chip}. Run this once after "
                                f"each boot, then try again:\n  sudo sh setup_pwm.sh") from None
        except OSError as e:
            raise PwmSetupError(f"Couldn't set up hardware PWM channel {self.channel} in {self.chip}: {e}") from None
        atexit.register(self.close)

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        value = min(max(float(value), 0.0), 1.0)
        self._write("duty_cycle", round(self.period_ns * value))
        self._value = value

    def on(self):
        self.value = 1.0

    def off(self):
        self.value = 0.0

    def close(self):
        """Leave the LED off, since hardware PWM keeps running after the script stops."""
        try:
            self._write("duty_cycle", 0)
            self._write("enable", 0)
        except OSError:
            pass

    def _read(self, name):
        return (self.path / name).read_text().strip()

    def _write(self, name, value):
        self._write_to(self.path / name, value)

    @staticmethod
    def _write_to(path, value):
        with open(path, "w") as f:
            f.write(f"{int(value)}\n")


class Leds:
    """Every change to either LED is published immediately, stamped right after it happens.
    Levels are logged as actually applied, which for the test LED means whole percent."""

    def __init__(self, pub, pollution_brightness=1.0, pollution_pwm="hardware",
                 pollution_pin=POLLUTION_LED_PIN, pwmchip=None):
        self.pub = pub
        self.pollution_brightness = pollution_brightness
        self.pollution_pwm = pollution_pwm
        # The pollution LED first, so a PWM setup problem shows up before anything lights
        if pollution_pwm == "hardware":
            self.pollution = HardwarePWM(pollution_pin, PWM_FREQUENCY, pwmchip)
        elif pollution_pwm == "software":
            self.pollution = PWMLED(pollution_pin, frequency=PWM_FREQUENCY)
        else:
            self.pollution = LED(pollution_pin)
        self.test = PWMLED(TEST_LED_PIN, frequency=PWM_FREQUENCY)
        self.test_level = None      # unknown until the first command
        self.pollution_on = None

    def describe_pollution(self):
        if isinstance(self.pollution, HardwarePWM):
            return f"hardware PWM, {self.pollution.path}"
        return f"{self.pollution_pwm} PWM" if self.pollution_pwm == "software" else "on/off only"

    def set_test(self, level, note="", only_if_changed=False):
        self.test.value = round(min(max(level, 0.0), 1.0), 4)
        applied = round(float(self.test.value), 4)  # what gpiozero actually set
        if only_if_changed and applied == self.test_level:
            return
        t, mono = stamp()
        self.test_level = applied
        self.pub.send("led", {"led": "test", "level": applied, "note": note}, t, mono)

    def set_pollution(self, on, note="", level=None):
        """Switch the pollution LED on (at `level`, or the usual brightness) or off."""
        level = (self.pollution_brightness if level is None else level) if on else 0.0
        if self.pollution_pwm == "none":
            if 0 < level < 1:
                raise ValueError("dimming the pollution LED needs --pollution-pwm hardware or software")
            self.pollution.on() if level > 0 else self.pollution.off()
            applied = 1.0 if level > 0 else 0.0
        else:
            self.pollution.value = level
            applied = round(float(self.pollution.value), 4)
        t, mono = stamp()
        self.pollution_on = on
        self.pub.send("led", {"led": "pollution", "level": applied, "note": note}, t, mono)

    def all_off(self, note):
        if self.test_level != 0.0:
            self.set_test(0.0, note)
        if self.pollution_on is not False:
            self.set_pollution(False, note)


class SensorReader(threading.Thread):
    """Reads the sensor every `period` seconds until halted, publishing raw counts."""

    def __init__(self, sensor, pub, leds, period, gain, integration_ms):
        super().__init__(daemon=True)
        self.sensor, self.pub, self.leds = sensor, pub, leds
        self.period, self.gain, self.integration_ms = period, gain, integration_ms
        # The TSL2591 tops out at 36863 counts with 100 ms integration, 65535 otherwise
        self.max_count = 36863 if integration_ms == 100 else 65535
        self.halt = threading.Event()
        self.warned_saturation = False

    def run(self):
        start = next_time = time.monotonic()
        last_print = start - 10
        while not self.halt.is_set():
            try:
                full, ir = self.sensor.raw_luminosity
                t, mono = stamp()
                saturated = full >= self.max_count or ir >= self.max_count
                fields = {"ch0_full": full, "ch1_ir": ir, "saturated": saturated, "error": ""}
            except (OSError, RuntimeError) as e:
                t, mono = stamp()
                full = ir = None
                saturated = False
                fields = {"ch0_full": None, "ch1_ir": None, "saturated": False, "error": str(e)}
            fields["gain"] = self.gain
            fields["integration_ms"] = self.integration_ms
            self.pub.send("sensor", fields, t, mono)

            if saturated and not self.warned_saturation:
                print("WARNING: sensor is saturated (maxed out). Use a lower --gain or "
                      "shorter --integration, or those readings are useless.")
                self.warned_saturation = True
            if mono - last_print >= 10:
                pol = {True: "on", False: "off", None: "?"}[self.leds.pollution_on]
                print(f"{mono - start:7.0f}s  full={full}  ir={ir}  "
                      f"test LED={self.leds.test_level}  pollution={pol}")
                last_print = mono

            next_time += self.period
            self.halt.wait(max(0.0, next_time - time.monotonic()))


# ---- Test sequences. Each returns early if `stop` is set (Ctrl+C). ----

def test_steps(leds, a, stop):
    """Abrupt on/off: the test LED jumps between off and --level."""
    for c in range(1, a.cycles + 1):
        leds.set_test(a.level, f"step on {c}/{a.cycles}")
        if stop.wait(a.on_seconds):
            return
        leds.set_test(0.0, f"step off {c}/{a.cycles}")
        if stop.wait(a.off_seconds):
            return


def test_ramp(leds, a, stop):
    """Gradual dimming: --level down to off and back up, --ramp-steps steps each way.
    Every single step is published as its own LED event."""
    n = a.ramp_steps
    levels = [a.level * (1 - i / n) for i in range(n + 1)] + [a.level * i / n for i in range(1, n + 1)]
    step_time = a.ramp_seconds / len(levels)
    for c in range(1, a.cycles + 1):
        for i, level in enumerate(levels, 1):
            leds.set_test(level, f"ramp {c}/{a.cycles} step {i}/{len(levels)}")
            if stop.wait(step_time):
                return


def test_pollution(leds, a, stop):
    """Test LED held steady at --level while the pollution LED switches on and off."""
    leds.set_test(a.level, "steady for pollution test")
    if stop.wait(a.off_seconds):
        return
    for c in range(1, a.cycles + 1):
        leds.set_pollution(True, f"pollution on {c}/{a.cycles}")
        if stop.wait(a.on_seconds):
            return
        leds.set_pollution(False, f"pollution off {c}/{a.cycles}")
        if stop.wait(a.off_seconds):
            return


def test_hold(leds, a, stop):
    """Test LED held at --level for --hold-minutes (long, steady drift baseline)."""
    leds.set_test(a.level, "hold")
    stop.wait(a.hold_minutes * 60)


TESTS = {"steps": test_steps, "ramp": test_ramp, "pollution": test_pollution, "hold": test_hold}


def main():
    p = argparse.ArgumentParser(description="Light sensor rig: Pi side")
    p.add_argument("--broker", required=True, help="the laptop's IP address, e.g. 192.168.1.50")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--test", choices=list(TESTS), required=True)
    p.add_argument("--level", type=float, default=1.0, help="test LED brightness 0-1 (default 1.0)")
    p.add_argument("--pollution-brightness", type=float, default=1.0,
                   help="pollution LED brightness when on, 0-1 (default 1.0)")
    p.add_argument("--pollution-pwm", choices=["hardware", "software", "none"], default="hardware",
                   help="how the pollution LED is driven: hardware PWM (default), gpiozero's software "
                        "PWM, or plain on/off")
    p.add_argument("--pollution-pin", type=int, default=POLLUTION_LED_PIN,
                   help=f"GPIO of the pollution LED (default {POLLUTION_LED_PIN})")
    p.add_argument("--pollution-pwmchip", type=int, default=None,
                   help="sysfs pwmchip number for hardware PWM (default: found automatically)")
    p.add_argument("--cycles", type=int, default=5, help="repeats for steps/ramp/pollution (default 5)")
    p.add_argument("--on-seconds", type=float, default=30, help="on time per cycle (default 30)")
    p.add_argument("--off-seconds", type=float, default=30, help="off time per cycle (default 30)")
    p.add_argument("--ramp-seconds", type=float, default=120, help="one full down-and-up ramp (default 120)")
    p.add_argument("--ramp-steps", type=int, default=50, help="steps each way in a ramp (default 50)")
    p.add_argument("--hold-minutes", type=float, default=30, help="length of the hold test (default 30)")
    p.add_argument("--dark-seconds", type=float, default=30,
                   help="LEDs off, still recording, before and after the test (default 30)")
    p.add_argument("--gain", choices=list(GAINS), default="low", help="sensor gain (default low)")
    p.add_argument("--integration", type=int, choices=list(INTEGRATIONS), default=100,
                   help="sensor integration time in ms (default 100)")
    p.add_argument("--period", type=float, default=1.0, help="seconds between readings (default 1.0)")
    p.add_argument("--data-dir", default=str(Path.home() / "rig-data"), help="where the backup copy goes")
    a = p.parse_args()
    if not 0 < a.pollution_brightness <= 1:
        p.error("--pollution-brightness must be above 0 and at most 1")
    if a.pollution_pwm == "none" and a.pollution_brightness < 1:
        p.error("dimming the pollution LED needs --pollution-pwm hardware or software")

    # Never read faster than the sensor finishes a measurement
    period = max(a.period, a.integration / 1000 + 0.02)
    run_id = time.strftime("%Y%m%d-%H%M%S") + "_" + a.test

    # Hardware first, so wiring problems show up before anything else
    try:
        sensor = adafruit_tsl2591.TSL2591(board.I2C())
    except (ValueError, OSError) as e:
        sys.exit(f"Can't find the TSL2591 on I2C ({e}). Check the Qwiic wiring, "
                 f"that I2C is enabled, and that 'sudo i2cdetect -y 1' shows 29.")
    sensor.gain = GAINS[a.gain]
    sensor.integration_time = INTEGRATIONS[a.integration]
    time.sleep(a.integration / 1000 + 0.05)  # let the first measurement finish
    try:
        leds = Leds(None, a.pollution_brightness, a.pollution_pwm, a.pollution_pin, a.pollution_pwmchip)
    except PwmSetupError as e:
        sys.exit(str(e))

    pub = Publisher(a.broker, a.port, run_id, a.data_dir)
    if not pub.connected.wait(10):
        pub.close()
        sys.exit(f"Couldn't reach the broker at {a.broker}:{a.port}. Is Mosquitto running "
                 f"on the laptop with mosquitto.conf, and is that the laptop's address?")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    leds.pub = pub
    reader = SensorReader(sensor, pub, leds, period, a.gain, a.integration)

    pub.send("meta", {"event": "start", "test": a.test, "settings": vars(a), "period": period,
                      "pwm_frequency": PWM_FREQUENCY, "test_led_pin": TEST_LED_PIN,
                      "pollution_led_pin": a.pollution_pin, "pollution_pwm": leds.describe_pollution(),
                      "led_log_levels": "applied"})
    leds.all_off("start")
    print(f"Run {run_id}: {a.dark_seconds:.0f}s dark, then the '{a.test}' test. Ctrl+C stops it.")
    reader.start()

    completed = False
    try:
        if not stop.wait(a.dark_seconds):
            TESTS[a.test](leds, a, stop)
            if not stop.is_set():
                leds.all_off("test finished")
                print(f"Test finished. Recording {a.dark_seconds:.0f}s more in the dark...")
                completed = not stop.wait(a.dark_seconds)
    finally:
        leds.all_off("end of run")
        reader.halt.set()
        reader.join(timeout=2)
        status = "finished" if completed else "stopped early"
        pub.send("meta", {"event": "end", "status": status})
        pub.close()
        print(f"Run {run_id} {status}. Backup on the Pi: {pub.backup_path}")


if __name__ == "__main__":
    main()
