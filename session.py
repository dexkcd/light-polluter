#!/usr/bin/env python3
"""
Light sensor rig: full experiment session (Raspberry Pi 5).

Runs the whole experimental procedure in one sitting with the box sealed the
entire time. Each condition is its own run with its own files, and conditions
change by script, so nothing is opened or restarted in between. With the
default durations it takes about 1 hour 42 minutes:

    dark                   1 min   both LEDs off: the sensor's dark floor
    warmup                 2 min   test LED at --high so it settles thermally first
    baseline_high         12 min   test LED constant at --high
    baseline_low          12 min   test LED constant at --low (dimmest planned setting)
    steps_clean           15 min   test LED off/on, dwell 20-60 s, no pollution
    steps_pollution_low    9 min   same, pollution LED toggling every 15-25 s
    steps_pollution_high   9 min   same, pollution LED toggling every 3-7 s
    ramp_clean            20 min   test LED ramps between --high and --low, 60-180 s each
    ramp_pollution_low    11 min   same, pollution LED toggling every 15-25 s
    ramp_pollution_high   11 min   same, pollution LED toggling every 3-7 s

Only timing is random: how long each dwell lasts, how long each ramp takes and
when the pollution LED switches. The brightness values and the pollution
condition (off, low or high) are fixed, chosen in advance, and saved as labels
in each run's meta file so results can be grouped by condition afterwards.

Each run gets its own seeds, derived from --seed and saved in its meta file, so
any run's exact schedule can be reproduced. Every LED change is still published
as its own event, so the LED files are the ground truth for what happened when.

Examples:
    python session.py --list
    python session.py --broker 192.168.50.157 --low 0.2
    python session.py --broker 192.168.50.157 --low 0.2 --time-scale 0.05     # ~5 min dry run
    python session.py --broker 192.168.50.157 --low 0.2 --skip steps_pollution_high ramp_pollution_high
"""

import argparse
import itertools
import random
import signal
import sys
import threading
import time

# name, default minutes, option that sets the minutes, what the test LED does, pollution level, purpose
PLAN = [
    ("dark", 1, "dark_minutes", "dark", "off",
     "Both LEDs off: the sensor's dark floor"),
    ("warmup", 2, "warmup_minutes", "constant", "off",
     "Test LED at --high so it settles thermally before the baselines"),
    ("baseline_high", 12, "baseline_minutes", "constant", "off",
     "Noise floor at the bright end"),
    ("baseline_low", 12, "baseline_minutes", "constant", "off",
     "Noise floor at the dim end, where shot noise bites hardest"),
    ("steps_clean", 15, "steps_minutes", "steps", "off",
     "Abrupt on/off, no pollution: the easy case for BOCPD vs. CUSUM"),
    ("steps_pollution_low", 9, "steps_pollution_minutes", "steps", "low",
     "Abrupt on/off, low pollution: first point on the noise curve"),
    ("steps_pollution_high", 9, "steps_pollution_minutes", "steps", "high",
     "Abrupt on/off, high pollution: the stress case"),
    ("ramp_clean", 20, "ramp_minutes", "ramp", "off",
     "Gradual dimming, no pollution: GP vs. GAM vs. EWMA"),
    ("ramp_pollution_low", 11, "ramp_pollution_minutes", "ramp", "low",
     "Gradual dimming, low pollution: calibration under light noise"),
    ("ramp_pollution_high", 11, "ramp_pollution_minutes", "ramp", "high",
     "Gradual dimming, high pollution: do the intervals stay honest?"),
]
NAMES = [row[0] for row in PLAN]
CONSTANT_LEVEL = {"warmup": "high", "baseline_high": "high", "baseline_low": "low"}


def run_seeds(session_seed, name):
    """Two seeds per run, one for the test LED's schedule and one for the pollution LED's."""
    r = random.Random(f"{session_seed}-{name}")
    return r.randrange(2**31), r.randrange(2**31)


class Run:
    """Timing helpers for one run. Every duration is multiplied by --time-scale."""

    def __init__(self, leds, a, schedule_seed, stop, end):
        self.leds, self.a, self.stop, self.end = leds, a, stop, end
        self.rng = random.Random(schedule_seed)

    def random_seconds(self, bounds):
        return self.rng.uniform(*bounds) * self.a.time_scale

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
    """Switches the pollution LED on and off, each state lasting a random time
    within the condition's interval, on its own schedule."""

    def __init__(self, leds, interval, time_scale, seed):
        super().__init__(daemon=True)
        self.leds, self.interval, self.time_scale = leds, interval, time_scale
        self.rng = random.Random(seed)
        self.halt = threading.Event()

    def _wait(self):
        return self.halt.wait(self.rng.uniform(*self.interval) * self.time_scale)

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


# ---- What the test LED does in each kind of run ----

def dark(run, name):
    run.set_test(0.0, "dark")
    run.wait(float("inf"))


def constant(run, name):
    level_option = CONSTANT_LEVEL[name]
    run.set_test(getattr(run.a, level_option), f"{name.split('_')[0]} at {level_option}")
    run.wait(float("inf"))


def steps(run, name):
    """Off and on, each state lasting a random time within --dwell."""
    a = run.a
    on_levels = itertools.cycle(a.step_levels)
    run.set_test(0.0, "toggles start off")
    n = 0
    while not run.wait(run.random_seconds(a.dwell)):
        n += 1
        run.set_test(next(on_levels), f"toggle on {n}")
        if run.wait(run.random_seconds(a.dwell)):
            return
        run.set_test(0.0, f"toggle off {n}")


def ramps(run, name):
    """High to low and back, each ramp lasting a random time within --ramp-duration,
    with a plateau of --ramp-hold seconds after each one."""
    a = run.a
    run.set_test(a.high, "ramps start at high")
    if run.wait(a.ramp_hold * a.time_scale):
        return
    n = 0
    while True:
        for direction, start, finish in (("down", a.high, a.low), ("up", a.low, a.high)):
            n += 1
            duration = run.random_seconds(a.ramp_duration)
            count = max(1, round(duration / (a.ramp_step * a.time_scale)))
            for i in range(1, count + 1):
                run.set_test(start + (finish - start) * i / count, f"ramp {n} {direction} step {i}/{count}")
                if run.wait(duration / count):
                    return
            if run.wait(a.ramp_hold * a.time_scale):
                return


BEHAVIOURS = {"dark": dark, "constant": constant, "steps": steps, "ramp": ramps}


def condition(a, name, change, pollution_level):
    """The fixed, chosen conditions of a run, saved in its meta file for grouping later."""
    if change == "dark":
        brightness = {"type": "constant", "level": 0.0}
    elif change == "constant":
        brightness = {"type": "constant", "level": getattr(a, CONSTANT_LEVEL[name])}
    elif change == "steps":
        brightness = {"type": "steps", "off": 0.0, "on": a.step_levels}
    else:
        brightness = {"type": "ramp", "high": a.high, "low": a.low}
    timing = {}
    if change == "steps":
        timing["dwell_s"] = a.dwell
    if change == "ramp":
        timing.update(ramp_duration_s=a.ramp_duration, ramp_hold_s=a.ramp_hold, ramp_step_s=a.ramp_step)
    return {
        "change": "none" if change in ("dark", "constant") else change,
        "pollution_level": pollution_level,
        "pollution_interval_s": getattr(a, f"pollution_{pollution_level}", None),
        "brightness": brightness,
        "timing": timing,
    }


def parse_args():
    p = argparse.ArgumentParser(description="Light sensor rig: full experiment session")
    p.add_argument("--broker", help="the laptop's IP address, e.g. 192.168.50.157")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--list", action="store_true", help="print the plan and exit")
    p.add_argument("--only", nargs="+", choices=NAMES, metavar="RUN", help="run just these runs, in plan order")
    p.add_argument("--skip", nargs="+", choices=NAMES, metavar="RUN", default=[], help="leave these runs out")
    p.add_argument("--seed", type=int, default=random.SystemRandom().randrange(1_000_000),
                   help="session seed; each run's seeds are derived from it (default: new random seed)")
    p.add_argument("--time-scale", type=float, default=1.0,
                   help="multiply every duration by this, e.g. 0.05 for a quick dry run")

    g = p.add_argument_group("brightness (fixed, chosen in advance)")
    g.add_argument("--high", type=float, default=1.0, help="full brightness, 0-1 (default 1.0)")
    g.add_argument("--low", type=float, default=0.2, help="dimmest planned setting, 0-1 (default 0.2)")
    g.add_argument("--step-levels", type=float, nargs="+",
                   help="'on' level(s) for the steps runs, used in rotation (default: --high)")

    g = p.add_argument_group("pollution conditions (seconds each pollution LED state lasts)")
    g.add_argument("--pollution-low", type=float, nargs=2, default=[15, 25], metavar=("MIN", "MAX"))
    g.add_argument("--pollution-high", type=float, nargs=2, default=[3, 7], metavar=("MIN", "MAX"))

    g = p.add_argument_group("random timing ranges (seconds)")
    g.add_argument("--dwell", type=float, nargs=2, default=[20, 60], metavar=("MIN", "MAX"),
                   help="how long each off/on state lasts in the steps runs (default 20 60)")
    g.add_argument("--ramp-duration", type=float, nargs=2, default=[60, 180], metavar=("MIN", "MAX"),
                   help="how long each ramp takes (default 60 180)")
    g.add_argument("--ramp-hold", type=float, default=20, help="plateau after each ramp (default 20)")
    g.add_argument("--ramp-step", type=float, default=1.0, help="seconds between ramp steps (default 1.0)")

    g = p.add_argument_group("durations (minutes)")
    defaults = {option: minutes for _, minutes, option, *_ in PLAN}
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
    for option in ("pollution_low", "pollution_high", "dwell", "ramp_duration"):
        low, high = getattr(a, option)
        if not 0 < low <= high:
            p.error(f"--{option.replace('_', '-')} needs 0 < MIN <= MAX")
    if a.time_scale <= 0:
        p.error("--time-scale must be positive")
    return a


def build_plan(a):
    plan = []
    for index, (name, _, option, change, pollution_level, purpose) in enumerate(PLAN, 1):
        if (a.only and name not in a.only) or name in a.skip:
            continue
        plan.append((index, name, getattr(a, option) * a.time_scale, change, pollution_level, purpose))
    return plan


def main():
    a = parse_args()
    plan = build_plan(a)
    if not plan:
        sys.exit("Nothing to run: --only and --skip leave no runs.")
    total = sum(row[2] for row in plan)
    print(f"Session plan (seed {a.seed}{'' if a.time_scale == 1 else f', time scale {a.time_scale:g}'}):")
    for index, name, minutes, change, pollution_level, purpose in plan:
        print(f"  {index:2d}  {name:<21} {minutes:6.1f} min   pollution {pollution_level:<4}  {purpose}")
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
    for position, (index, name, minutes, change, pollution_level, purpose) in enumerate(plan, 1):
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

        schedule_seed, pollution_seed = run_seeds(a.seed, name)
        has_pollution = pollution_level != "off"
        pub.send("meta", {"event": "start", "test": name, "session": session, "run_index": index,
                          "purpose": purpose, "minutes": minutes,
                          **condition(a, name, change, pollution_level),
                          "session_seed": a.seed, "schedule_seed": schedule_seed,
                          "pollution_seed": pollution_seed if has_pollution else None,
                          "settings": {**vars(a), "data_dir": data_dir}, "period": period,
                          "pwm_frequency": rig.PWM_FREQUENCY, "test_led_pin": rig.TEST_LED_PIN,
                          "pollution_led_pin": rig.POLLUTION_LED_PIN})
        # Record where both LEDs are as this run begins, so every LED file is complete on its own
        leds.set_test(carried_level, "state at run start")
        leds.set_pollution(False, "state at run start")

        reader = rig.SensorReader(sensor, pub, leds, period, a.gain, a.integration)
        print(f"\n[{position}/{len(plan)}] {run_id}: {purpose} ({minutes:.1f} min, pollution {pollution_level})")
        reader.start()

        run = Run(leds, a, schedule_seed, stop, time.monotonic() + minutes * 60)
        pollution = None
        if has_pollution:
            interval = getattr(a, f"pollution_{pollution_level}")
            pollution = PollutionDriver(leds, interval, a.time_scale, pollution_seed)
            pollution.start()

        failed = False
        try:
            BEHAVIOURS[change](run, name)
        except BaseException:
            failed = True
            raise
        finally:
            if pollution:
                pollution.halt.set()
                pollution.join()
            ended_early = stop.is_set() or failed
            if ended_early or position == len(plan):
                leds.all_off("stopped early" if ended_early else "session over")
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
        remaining = [row[1] for row in plan if row[1] not in finished]
        print(f"\nSession {session} stopped early" + (f" during {stopped_in}." if stopped_in else ".")
              + f" Finished: {', '.join(finished) or 'none'}. Both LEDs are off.")
        print("To run the rest with the same timings, use this plus any other options you changed")
        print("(and add 'warmup' to the list if the LED has been off a while):")
        print(f"  python session.py --broker {a.broker} --high {a.high:g} --low {a.low:g} "
              f"--seed {a.seed} --only {' '.join(remaining)}")
    print(f"Backups on the Pi are in {data_dir}")


if __name__ == "__main__":
    main()
