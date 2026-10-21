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
import json
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
TEST_LED_PIN = 18        # [pin 12] -> FemtoBuck "Dim in"
POLLUTION_LED_PIN = 23   # [pin 16] -> 150 ohm resistor -> pollution LED
PWM_FREQUENCY = 1000     # Hz, dimming signal to the FemtoBuck

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


class Leds:
    """Every change to either LED is published immediately, stamped right after it happens."""

    def __init__(self, pub, pollution_brightness=1.0, pollution_pwm=None):
        self.pub = pub
        self.test = PWMLED(TEST_LED_PIN, frequency=PWM_FREQUENCY)
        self.pollution_brightness = pollution_brightness
        if pollution_pwm is None:
            pollution_pwm = pollution_brightness < 1.0
        if pollution_pwm:
            # Dimmed with PWM. At full brightness it stays a plain on/off output.
            self.pollution = PWMLED(POLLUTION_LED_PIN, frequency=PWM_FREQUENCY)
        else:
            self.pollution = LED(POLLUTION_LED_PIN)
        self.test_level = None      # unknown until the first command
        self.pollution_on = None

    def set_test(self, level, note=""):
        level = round(min(max(level, 0.0), 1.0), 4)
        self.test.value = level
        t, mono = stamp()
        self.test_level = level
        self.pub.send("led", {"led": "test", "level": level, "note": note}, t, mono)

    def set_pollution(self, on, note="", level=None):
        """Switch the pollution LED on (at `level`, or the usual brightness) or off."""
        level = (self.pollution_brightness if level is None else level) if on else 0.0
        if on and level < 1.0:
            if not isinstance(self.pollution, PWMLED):
                raise ValueError("dimming the pollution LED needs it set up with PWM")
            self.pollution.value = level
        elif on:
            self.pollution.on()
        else:
            self.pollution.off()
        t, mono = stamp()
        self.pollution_on = on
        self.pub.send("led", {"led": "pollution", "level": level, "note": note}, t, mono)

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
                   help="pollution LED brightness when on, 0-1 (default 1.0: plain on/off, no PWM)")
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

    pub = Publisher(a.broker, a.port, run_id, a.data_dir)
    if not pub.connected.wait(10):
        pub.close()
        sys.exit(f"Couldn't reach the broker at {a.broker}:{a.port}. Is Mosquitto running "
                 f"on the laptop with mosquitto.conf, and is that the laptop's address?")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    leds = Leds(pub, a.pollution_brightness)
    reader = SensorReader(sensor, pub, leds, period, a.gain, a.integration)

    pub.send("meta", {"event": "start", "test": a.test, "settings": vars(a), "period": period,
                      "pwm_frequency": PWM_FREQUENCY, "test_led_pin": TEST_LED_PIN,
                      "pollution_led_pin": POLLUTION_LED_PIN})
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
