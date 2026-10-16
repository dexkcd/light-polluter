#!/usr/bin/env python3
"""
Light sensor rig: full experiment session (Raspberry Pi 5).

Runs the whole experimental procedure in one sitting with the box sealed the
entire time. Each condition is its own run with its own files, and conditions
change by script, so nothing is opened or restarted in between. With the
default durations it takes about 1 hour 40 minutes:

    dark              1 min   both LEDs off: the sensor's dark floor
    warmup            2 min   test LED at --high so it settles thermally before the baselines
    baseline_high    12 min   test LED constant at --high (full brightness)
    baseline_low     12 min   test LED constant at --low (the dimmest planned setting)
    steps_clean      15 min   test LED switched off and on, each state lasting 20-60 s
    steps_pollution  15 min   same, with the pollution LED switching on its own schedule
    ramp_clean       20 min   test LED ramps between --high and --low, 1-3 min per ramp
    ramp_pollution   20 min   same, with the pollution LED switching on its own schedule

Timings inside those ranges are random, so no method can latch onto a fixed
rhythm, and the pollution LED's schedule is independent of the test LED's.
Everything random comes from --seed, which is printed and saved in every run's
meta file, so a session can be repeated exactly.

Every LED change is published as its own event, exactly as with rig.py, so the
LED files are the ground truth for when each change happened.

Examples:
    python session.py --list
    python session.py --broker 192.168.50.157 --low 0.2
    python session.py --broker 192.168.50.157 --low 0.2 --time-scale 0.05     # ~5 min dry run
    python session.py --broker 192.168.50.157 --low 0.2 --only warmup ramp_clean ramp_pollution
"""

import argparse
import itertools
import random
import signal
import sys
import threading
import time

# (name, default minutes, option that overrides the minutes, purpose)
PLAN = [
    ("dark", 1, "dark_minutes",
     "Both LEDs off: the sensor's dark floor"),
    ("warmup", 2, "warmup_minutes",
     "Test LED at --high so it settles thermally before the baselines"),
    ("baseline_high", 12, "baseline_minutes",
     "Constant light at --high: noise floor at full brightness"),
    ("baseline_low", 12, "baseline_minutes",
     "Constant light at --low: noise floor at the dimmest planned setting"),
    ("steps_clean", 15, "steps_minutes",
     "Abrupt on/off, clean: detection delay and false-alarm rate"),
    ("steps_pollution", 15, "steps_minutes",
     "Abrupt on/off with the pollution LED switching independently"),
    ("ramp_clean", 20, "ramp_minutes",
     "Gradual dimming between --high and --low, clean"),
    ("ramp_pollution", 20, "ramp_minutes",
     "Gradual dimming with the pollution LED switching independently"),
]


class Run:
    """Timing helpers for one run. Every duration is multiplied by --time-scale."""

    def __init__(self, leds, a, name, stop, end):
        self.leds, self.a, self.stop, self.end = leds, a, stop, end
        self.rng = random.Random(f"{a.seed}-{name}")

    def random_seconds(self, low, high):
        return self.rng.uniform(low, high) * self.a.time_scale

    def wait(self, seconds):
        """Wait up to `seconds`. Returns True when the run is over (time's up or Ctrl+C)."""
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            return True
        if self.stop.wait(min(seconds, remaining)):
            return True
        return seconds >= remaining

    def set_test(self, level, note):
        """Set the test LED, publishing an event only if the level actually changes."""
        if self.leds.test_level is None or round(level, 4) != self.leds.test_level:
            self.leds.set_test(level, note)


class PollutionDriver(threading.Thread):
    """Switches the pollution LED on and off on its own random schedule."""

    def __init__(self, leds, a, name):
        super().__init__(daemon=True)
        self.leds, self.a = leds, a
        self.rng = random.Random(f"{a.seed}-{name}-pollution")
        self.halt = threading.Event()

    def _wait(self):
        seconds = self.rng.uniform(self.a.pollution_min, self.a.pollution_max) * self.a.time_scale
        return self.halt.wait(seconds)

    def run(self):
        n = 0
        while not self._wait():
            n += 1
            self.leds.set_pollution(True, f"pollution on {n}")
            if self._wait():
                break
            self.leds.set_pollution(False, f"pollution off {n}")
        if self.leds.pollution_on:
            self.leds.set_pollution(False, "pollution off at end of run")


# ---- What the test LED does in each run ----

def constant(level_option, note):
    def scenario(run):
        level = 0.0 if level_option is None else getattr(run.a, level_option)
        run.set_test(level, note)
        run.wait(float("inf"))
    return scenario


def steps(run):
    """Off and on, each state lasting a random --toggle-min to --toggle-max seconds."""
    a = run.a
    on_levels = itertools.cycle(a.step_levels)
    run.set_test(0.0, "toggles start off")
    n = 0
    while not run.wait(run.random_seconds(a.toggle_min, a.toggle_max)):
        n += 1
        run.set_test(next(on_levels), f"toggle on {n}")
        if run.wait(run.random_seconds(a.toggle_min, a.toggle_max)):
            return
        run.set_test(0.0, f"toggle off {n}")


def ramps(run):
    """High to low and back, each ramp lasting a random --ramp-min to --ramp-max
    seconds, with a short plateau of --ramp-hold seconds after each one."""
    a = run.a
    run.set_test(a.high, "ramps start at high")
    if run.wait(a.ramp_hold * a.time_scale):
        return
    n = 0
    while True:
        for direction, start, finish in (("down", a.high, a.low), ("up", a.low, a.high)):
            n += 1
            duration = run.random_seconds(a.ramp_min, a.ramp_max)
            count = max(1, round(duration / (a.ramp_step * a.time_scale)))
            for i in range(1, count + 1):
                run.set_test(start + (finish - start) * i / count, f"ramp {n} {direction} step {i}/{count}")
                if run.wait(duration / count):
                    return
            if run.wait(a.ramp_hold * a.time_scale):
                return


SCENARIOS = {
    "dark": constant(None, "dark"),
    "warmup": constant("high", "warmup at high"),
    "baseline_high": constant("high", "baseline at high"),
    "baseline_low": constant("low", "baseline at low"),
    "steps_clean": steps,
    "steps_pollution": steps,
    "ramp_clean": ramps,
    "ramp_pollution": ramps,
}


def parse_args():
    p = argparse.ArgumentParser(description="Light sensor rig: full experiment session")
    p.add_argument("--broker", help="the laptop's IP address, e.g. 192.168.50.157")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--list", action="store_true", help="print the plan and exit")
    p.add_argument("--only", nargs="+", choices=[name for name, *_ in PLAN],
                   help="run just these conditions, in plan order")
    p.add_argument("--seed", type=int, default=random.SystemRandom().randrange(1_000_000),
                   help="seed for all random timings (default: new random seed)")
    p.add_argument("--time-scale", type=float, default=1.0,
                   help="multiply every duration by this, e.g. 0.05 for a quick dry run")

    g = p.add_argument_group("brightness")
    g.add_argument("--high", type=float, default=1.0, help="full brightness, 0-1 (default 1.0)")
    g.add_argument("--low", type=float, default=0.2, help="dimmest planned setting, 0-1 (default 0.2)")
    g.add_argument("--step-levels", type=float, nargs="+",
                   help="'on' levels for the steps runs, used in rotation (default: --high)")

    g = p.add_argument_group("timings (seconds)")
    g.add_argument("--toggle-min", type=float, default=20)
    g.add_argument("--toggle-max", type=float, default=60)
    g.add_argument("--pollution-min", type=float, default=15)
    g.add_argument("--pollution-max", type=float, default=45)
    g.add_argument("--ramp-min", type=float, default=60)
    g.add_argument("--ramp-max", type=float, default=180)
    g.add_argument("--ramp-hold", type=float, default=20, help="plateau after each ramp (default 20)")
    g.add_argument("--ramp-step", type=float, default=1.0, help="seconds per ramp step (default 1.0)")

    g = p.add_argument_group("durations (minutes)")
    defaults = {option: minutes for _, minutes, option, _ in PLAN}
    for option, minutes in defaults.items():
        g.add_argument("--" + option.replace("_", "-"), dest=option, type=float, default=minutes)

    g = p.add_argument_group("sensor")
    g.add_argument("--gain", choices=["low", "med", "high", "max"], default="low")
    g.add_argument("--integration", type=int, choices=[100, 200, 300, 400, 500, 600], default=100)
    g.add_argument("--period", type=float, default=0.5, help="seconds between readings (default 0.5)")
    g.add_argument("--data-dir", default=None, help="where the Pi's backup copy goes (default ~/rig-data)")

    a = p.parse_args()
    if not 0 < a.low < a.high <= 1:
        p.error("need 0 < --low < --high <= 1")
    a.step_levels = a.step_levels or [a.high]
    if not all(0 < level <= 1 for level in a.step_levels):
        p.error("--step-levels must be between 0 and 1")
    for low, high in (("toggle_min", "toggle_max"), ("pollution_min", "pollution_max"), ("ramp_min", "ramp_max")):
        if not 0 < getattr(a, low) <= getattr(a, high):
            p.error(f"need 0 < --{low.replace('_', '-')} <= --{high.replace('_', '-')}")
    if a.time_scale <= 0:
        p.error("--time-scale must be positive")
    return a


def build_plan(a):
    plan = []
    for index, (name, _, option, purpose) in enumerate(PLAN, 1):
        if a.only and name not in a.only:
            continue
        plan.append((index, name, getattr(a, option) * a.time_scale, purpose))
    return plan


def main():
    a = parse_args()
    plan = build_plan(a)
    total = sum(minutes for _, _, minutes, _ in plan)
    print(f"Session plan (seed {a.seed}{'' if a.time_scale == 1 else f', time scale {a.time_scale:g}'}):")
    for index, name, minutes, purpose in plan:
        print(f"  {index:2d}  {name:<16} {minutes:6.1f} min   {purpose}")
    print(f"  Total {total:.0f} min ({int(total // 60)} h {total % 60:.0f} min)")
    if a.list:
        return
    if not a.broker:
        sys.exit("Give the laptop's address with --broker.")

    import rig  # imported here so --list works on a computer without the Pi's libraries
    from pathlib import Path
    data_dir = a.data_dir or str(Path.home() / "rig-data")

    try:
        sensor = rig.adafruit_tsl2591.TSL2591(rig.board.I2C())
    except (ValueError, OSError) as e:
        sys.exit(f"Can't find the TSL2591 on I2C ({e}). Check the wiring and that "
                 f"'sudo i2cdetect -y 1' shows 29.")
    sensor.gain = rig.GAINS[a.gain]
    sensor.integration_time = rig.INTEGRATIONS[a.integration]
    time.sleep(a.integration / 1000 + 0.05)
    period = max(a.period, a.integration / 1000 + 0.02)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    session = time.strftime("%Y%m%d-%H%M%S")
    leds = None
    finished = []
    stopped_in = None
    for position, (index, name, minutes, purpose) in enumerate(plan, 1):
        if stop.is_set():
            break
        run_id = f"{session}_{index:02d}_{name}"
        pub = rig.Publisher(a.broker, a.port, run_id, data_dir)
        if not pub.connected.wait(10):
            if leds is None:
                pub.close()
                sys.exit(f"Couldn't reach the broker at {a.broker}:{a.port}. Is Mosquitto "
                         f"running on the laptop, and is that the laptop's address?")
            print("WARNING: can't reach the broker right now. Carrying on; this run is "
                  f"saved on the Pi in {pub.backup_path}")

        if leds is None:
            leds = rig.Leds(pub)
        carried_level = leds.test_level if leds.test_level is not None else 0.0
        leds.pub = pub  # from here on, LED events go to this run's files

        pub.send("meta", {"event": "start", "test": name, "session": session, "run_index": index,
                          "purpose": purpose, "minutes": minutes, "seed": a.seed,
                          "settings": {**vars(a), "data_dir": data_dir}, "period": period,
                          "pwm_frequency": rig.PWM_FREQUENCY, "test_led_pin": rig.TEST_LED_PIN,
                          "pollution_led_pin": rig.POLLUTION_LED_PIN})
        # Record where both LEDs are as this run begins, so every LED file is complete on its own
        leds.set_test(carried_level, "state at run start")
        leds.set_pollution(False, "state at run start")

        reader = rig.SensorReader(sensor, pub, leds, period, a.gain, a.integration)
        print(f"\n[{position}/{len(plan)}] {run_id}: {purpose} ({minutes:.1f} min)")
        reader.start()

        run = Run(leds, a, name, stop, time.monotonic() + minutes * 60)
        pollution = PollutionDriver(leds, a, name) if name.endswith("_pollution") else None
        if pollution:
            pollution.start()

        failed = False
        try:
            SCENARIOS[name](run)
        except BaseException:
            failed = True
            raise
        finally:
            if pollution:
                pollution.halt.set()
                pollution.join()
            ended_early = stop.is_set() or failed
            if ended_early or position == len(plan):
                leds.all_off("session over" if not ended_early else "stopped early")
            reader.halt.set()
            reader.join(timeout=2)
            status = "stopped early" if ended_early else "finished"
            pub.send("meta", {"event": "end", "status": status})
            pub.close()
            print(f"{run_id} {status}.")
        if status == "finished":
            finished.append(name)
        else:
            stopped_in = name

    if leds is not None:  # make sure nothing is left on, even if stopped between runs
        leds.test.value = 0
        leds.pollution.off()

    if len(finished) == len(plan):
        print(f"\nSession {session} finished: all {len(plan)} runs done. Both LEDs are off.")
    else:
        remaining = [name for _, name, _, _ in plan if name not in finished]
        print(f"\nSession {session} stopped early" + (f" during {stopped_in}." if stopped_in else ".")
              + f" Finished: {', '.join(finished) or 'none'}. Both LEDs are off.")
        print("To run the rest with the same timings (add 'warmup' first if the LED has been off a while):")
        print(f"  python session.py --broker {a.broker} --seed {a.seed} --only {' '.join(remaining)}")
    print(f"Backups on the Pi are in {data_dir}")


if __name__ == "__main__":
    main()
