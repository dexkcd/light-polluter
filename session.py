#!/usr/bin/env python3
"""
Light sensor rig: experiment sessions (Raspberry Pi 5).

Runs a whole session in one sitting with the box sealed the entire time. Each
condition is its own run with its own files, and conditions change by script,
so nothing is opened or restarted in between. Pick the plan with --plan:

--plan main  (sessions 1 and 2, about 1 h 42 min)
     1  dark                     1 min   both LEDs off: the sensor's dark floor
     2  warmup                   2 min   test LED at --high
     3  baseline_high           12 min   test LED constant at --high
     4  baseline_low            12 min   test LED constant at --low
     5  steps_clean             15 min   test LED off / on, dwell 20-60 s, no pollution
     6  steps_pollution_low      9 min   same, pollution LED switching every 15-25 s
     7  steps_pollution_high     9 min   same, pollution LED switching every 3-7 s
     8  ramp_clean              20 min   test LED ramps between --high and --low, 60-180 s each
     9  ramp_pollution_low      11 min   same, pollution low
    10  ramp_pollution_high     11 min   same, pollution high

--plan small-steps  (sessions 3 and 4, about 72 min)
    11  dark                     1 min   both LEDs off
    12  warmup                   5 min   test LED at --high: the LED needs about 5 min to settle
    13  steps85_clean           15 min   test LED alternates 1.0 / 0.85, dwell 20-60 s, no pollution
    14  steps85_pollution_low    9 min   same, pollution low
    15  steps85_pollution_high   9 min   same, pollution high
    16  steps95_clean           15 min   test LED alternates 1.0 / 0.95, dwell 20-60 s, no pollution
    17  steps95_pollution_low    9 min   same, pollution low
    18  steps95_pollution_high   9 min   same, pollution high
  --block-order 95-first runs 16-18 before 13-15 (for session 4).

--plan pollution-levels  (the pollution-levels block, about 3 h 50 min)
    21  dark                     1 min   both LEDs off
    22  warmup                   5 min   test LED at --high
    23  steps95_clean           15 min   test LED alternates 1.0 / 0.95, no pollution
    24  steps85_clean           15 min   test LED alternates 1.0 / 0.85, no pollution
    25  ramp_clean              20 min   test LED ramps between 1.0 and 0.2, no pollution
    then one block per pollution duty (0.25, 0.5, 1.0), in an order drawn from the session seed:
    26-31 (duty 0.25), 32-37 (duty 0.5), 38-43 (duty 1.0):
        steps95_pollP_low  9 min, steps95_pollP_high  9 min,
        steps85_pollP_low  9 min, steps85_pollP_high  9 min,
        ramp_pollP_low    11 min, ramp_pollP_high    11 min
    where P is the duty in percent (25, 50 or 100), low switches every 15-25 s and high every 3-7 s.

--plan pollution-levels-test  (a check before the first pollution-levels session, about 9 min)
    21  dark, 22  warmup (5 min), then
     0  pollution_check          test LED at --high; pollution LED 10 s on / 10 s off,
                                 3 times at each duty 0.25, 0.5 and 1.0

--plan pollution-check  (a quick check, about 2 min, not an experiment run)
     0  pollution_check          test LED off (--check-test-level); pollution LED 10 s on / 10 s off,
                                 3 times at each brightness in --check-levels (default 1.0, then 0.5)

Only timing is random: how long each dwell lasts, how long each ramp takes and
when the pollution LED switches. The brightness levels and the pollution
condition are fixed, chosen in advance, and saved as labels in each run's meta
file so results can be grouped by condition afterwards. The pollution LED is on
hardware PWM (GPIO 12); when on, it's at full brightness, --pollution-brightness,
or in the pollution-levels plan the duty of the run's block.

Every session gets a new session seed (checked against the earlier sessions'
backups on the Pi), and each run's own seeds are derived from it and saved in
its meta file. Every LED command is logged with its exact time, including each
run's first command even when it leaves the LED where it was.

Examples:
    python session.py --plan small-steps --list
    python session.py --plan small-steps --broker 192.168.50.157 --time-scale 0.05   # ~4 min dry run
    python session.py --plan small-steps --broker 192.168.50.157                     # session 3
    python session.py --plan small-steps --broker 192.168.50.157 --block-order 95-first   # session 4
    python session.py --plan pollution-levels-test --broker 192.168.50.157                  # before the block
    python session.py --plan pollution-levels --broker 192.168.50.157                       # each session
"""

import argparse
import itertools
import json
import random
import signal
import sys
import threading
import time
from pathlib import Path

# Each run: number, name, default minutes, option that sets the minutes, what the test LED
# does, pollution level, option holding the two step levels (alternate runs only), purpose
PLANS = {
    "main": [
        (1, "dark", 1, "dark_minutes", "dark", "off", None,
         "Both LEDs off: the sensor's dark floor"),
        (2, "warmup", 2, "warmup_minutes", "constant", "off", None,
         "Test LED at --high so it settles thermally before the baselines"),
        (3, "baseline_high", 12, "baseline_minutes", "constant", "off", None,
         "Noise floor at the bright end"),
        (4, "baseline_low", 12, "baseline_minutes", "constant", "off", None,
         "Noise floor at the dim end, where shot noise bites hardest"),
        (5, "steps_clean", 15, "steps_minutes", "steps", "off", None,
         "Abrupt on/off, no pollution: the easy case for BOCPD vs. CUSUM"),
        (6, "steps_pollution_low", 9, "steps_pollution_minutes", "steps", "low", None,
         "Abrupt on/off, low pollution: first point on the noise curve"),
        (7, "steps_pollution_high", 9, "steps_pollution_minutes", "steps", "high", None,
         "Abrupt on/off, high pollution: the stress case"),
        (8, "ramp_clean", 20, "ramp_minutes", "ramp", "off", None,
         "Gradual dimming, no pollution: GP vs. GAM vs. EWMA"),
        (9, "ramp_pollution_low", 11, "ramp_pollution_minutes", "ramp", "low", None,
         "Gradual dimming, low pollution: calibration under light noise"),
        (10, "ramp_pollution_high", 11, "ramp_pollution_minutes", "ramp", "high", None,
         "Gradual dimming, high pollution: do the intervals stay honest?"),
    ],
    "small-steps": [
        (11, "dark", 1, "dark_minutes", "dark", "off", None,
         "Both LEDs off: the sensor's dark floor"),
        (12, "warmup", 5, "warmup_minutes", "constant", "off", None,
         "Test LED at --high for the 5 min it needs to settle thermally"),
        (13, "steps85_clean", 15, "steps_minutes", "alternate", "off", "steps85_levels",
         "Medium steps (about 4x the pollution), no pollution"),
        (14, "steps85_pollution_low", 9, "steps_pollution_minutes", "alternate", "low", "steps85_levels",
         "Medium steps, low pollution"),
        (15, "steps85_pollution_high", 9, "steps_pollution_minutes", "alternate", "high", "steps85_levels",
         "Medium steps, high pollution"),
        (16, "steps95_clean", 15, "steps_minutes", "alternate", "off", "steps95_levels",
         "Small steps (about the pollution's size), no pollution"),
        (17, "steps95_pollution_low", 9, "steps_pollution_minutes", "alternate", "low", "steps95_levels",
         "Small steps, low pollution"),
        (18, "steps95_pollution_high", 9, "steps_pollution_minutes", "alternate", "high", "steps95_levels",
         "Small steps, high pollution"),
    ],
    # A quick check, not an experiment run: its length comes from the --check-* options
    "pollution-check": [
        (0, "pollution_check", None, None, "pollution_check", "check", None,
         "Does the pollution LED's light change with its brightness setting?"),
    ],
    # The check before the first pollution-levels session
    "pollution-levels-test": [
        (21, "dark", 1, "dark_minutes", "dark", "off", None,
         "Both LEDs off: is the dark floor unchanged?"),
        (22, "warmup", 5, "warmup_minutes", "constant", "off", None,
         "Test LED at --high: does it settle at the same level as before?"),
        (0, "pollution_check", None, None, "pollution_check", "check", None,
         "Pollution LED at each duty, test LED on: does its effect grow with duty, without flicker?"),
    ],
}

POLLUTION_DUTIES = (0.25, 0.5, 1.0)
RUN_DUTY = {}  # pollution-levels plan: run name -> pollution LED duty


def pollution_levels_rows():
    """Runs 21-25 (start and clean), then six runs per pollution duty, numbered 26-43."""
    rows = [
        (21, "dark", 1, "dark_minutes", "dark", "off", None,
         "Both LEDs off: the sensor's dark floor"),
        (22, "warmup", 5, "warmup_minutes", "constant", "off", None,
         "Test LED at --high for the 5 min it needs to settle thermally"),
        (23, "steps95_clean", 15, "steps_minutes", "alternate", "off", "steps95_levels",
         "Small steps, no pollution"),
        (24, "steps85_clean", 15, "steps_minutes", "alternate", "off", "steps85_levels",
         "Medium steps, no pollution"),
        (25, "ramp_clean", 20, "ramp_minutes", "ramp", "off", None,
         "Gradual dimming, no pollution"),
    ]
    number = 26
    for duty in POLLUTION_DUTIES:
        for kind, change, levels_option, minutes, option, what in (
                ("steps95", "alternate", "steps95_levels", 9, "steps_pollution_minutes", "Small steps"),
                ("steps85", "alternate", "steps85_levels", 9, "steps_pollution_minutes", "Medium steps"),
                ("ramp", "ramp", None, 11, "ramp_pollution_minutes", "Gradual dimming")):
            for speed in ("low", "high"):
                name = f"{kind}_poll{round(duty * 100)}_{speed}"
                RUN_DUTY[name] = duty
                rows.append((number, name, minutes, option, change, speed, levels_option,
                             f"{what}, pollution duty {duty}, {speed}"))
                number += 1
    return rows


PLANS["pollution-levels"] = pollution_levels_rows()
ALL_NAMES = sorted({row[1] for rows in PLANS.values() for row in rows})
CHECK_PLANS = ("pollution-check", "pollution-levels-test")
LIST_BRIGHTNESS_PLANS = ("small-steps", "pollution-levels", "pollution-levels-test")
CONSTANT_LEVEL = {"warmup": "high", "baseline_high": "high", "baseline_low": "low"}


def run_seeds(session_seed, name):
    """Two seeds per run, one for the test LED's schedule and one for the pollution LED's."""
    r = random.Random(f"{session_seed}-{name}")
    return r.randrange(2**31), r.randrange(2**31)


def used_seeds(data_dir):
    """Session seeds already used, from the first (meta start) line of each backup on the Pi."""
    seeds = {}
    for path in Path(data_dir).glob("*.jsonl"):
        try:
            with open(path) as f:
                first = json.loads(f.readline())
        except (OSError, ValueError):
            continue
        seed = first.get("session_seed", first.get("seed"))
        if first.get("session") and seed is not None:
            seeds.setdefault(seed, first["session"])
    return seeds


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

    def set_test(self, level, note, force=False):
        """Set the test LED. Publishes an event when the applied level changes, or always with
        force=True (used for each run's first command, so the log shows exactly when it was given)."""
        self.leds.set_test(level, note, only_if_changed=not force)


class PollutionDriver(threading.Thread):
    """Switches the pollution LED on and off, each state lasting a random time
    within the condition's interval, on its own schedule."""

    def __init__(self, leds, interval, time_scale, seed, level):
        super().__init__(daemon=True)
        self.leds, self.interval, self.time_scale, self.level = leds, interval, time_scale, level
        self.rng = random.Random(seed)
        self.halt = threading.Event()

    def _wait(self):
        return self.halt.wait(self.rng.uniform(*self.interval) * self.time_scale)

    def run(self):
        n = 0
        while not self._wait():
            n += 1
            self.leds.set_pollution(True, f"pollution on {n}", level=self.level)
            if self._wait():
                break
            self.leds.set_pollution(False, f"pollution off {n}")
        if self.leds.pollution_on:
            self.leds.set_pollution(False, "pollution off at end of run")


# ---- What the test LED does in each kind of run ----

def dark(run, row):
    run.set_test(0.0, "dark", force=True)
    run.wait(float("inf"))


def constant(run, row):
    level_option = CONSTANT_LEVEL[row["name"]]
    run.set_test(getattr(run.a, level_option), f"{row['name'].split('_')[0]} at {level_option}", force=True)
    run.wait(float("inf"))


def steps(run, row):
    """Main plan: off and on, each state lasting a random time within --dwell."""
    a = run.a
    on_levels = itertools.cycle(a.step_levels)
    run.set_test(0.0, "toggles start off", force=True)
    n = 0
    while not run.wait(run.random_seconds(a.dwell)):
        n += 1
        run.set_test(next(on_levels), f"toggle on {n}")
        if run.wait(run.random_seconds(a.dwell)):
            return
        run.set_test(0.0, f"toggle off {n}")


def alternate(run, row):
    """Small-step plan: switch between the run's two step levels, starting with the
    first, each state lasting a random time within --dwell."""
    first, second = row["levels"]
    run.set_test(first, f"steps start at {first}", force=True)
    current, other = first, second
    n = 0
    while not run.wait(run.random_seconds(run.a.dwell)):
        n += 1
        current, other = other, current
        run.set_test(current, f"step {n} to {current}")


def ramps(run, row):
    """Main plan: high to low and back, each ramp lasting a random time within
    --ramp-duration, with a plateau of --ramp-hold seconds after each one."""
    a = run.a
    run.set_test(a.high, "ramps start at high", force=True)
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


def pollution_check(run, row):
    """Pollution-check plan: test LED held at --check-test-level while the pollution LED
    switches on and off, --check-cycles times at each brightness in --check-levels."""
    a = run.a
    run.set_test(a.check_test_level, "steady for pollution check", force=True)
    if run.wait(a.check_off * a.time_scale):
        return
    for level in a.check_levels:
        for c in range(1, a.check_cycles + 1):
            run.leds.set_pollution(True, f"pollution on at {level} ({c}/{a.check_cycles})", level=level)
            if run.wait(a.check_on * a.time_scale):
                return
            run.leds.set_pollution(False, f"pollution off ({c}/{a.check_cycles})")
            if run.wait(a.check_off * a.time_scale):
                return


BEHAVIOURS = {"dark": dark, "constant": constant, "steps": steps, "alternate": alternate, "ramp": ramps,
              "pollution_check": pollution_check}


def check_minutes(a):
    """Length of the pollution check: a first off period, then on and off for every cycle and level."""
    return (a.check_off + len(a.check_levels) * a.check_cycles * (a.check_on + a.check_off)) / 60


def test_led_levels(a, row):
    """The fixed test LED levels of a run, as a list."""
    if row["change"] == "pollution_check":
        return [a.check_test_level]
    if row["change"] == "dark":
        return [0.0]
    if row["change"] == "constant":
        return [getattr(a, CONSTANT_LEVEL[row["name"]])]
    if row["change"] == "alternate":
        return list(row["levels"])
    if row["change"] == "steps":
        return [0.0] + a.step_levels
    return [a.high, a.low]


def run_duty(a, row):
    """The pollution LED's duty when it's on in this run, or 0.0 if it stays off."""
    if row["pollution_level"] not in ("low", "high"):
        return 0.0
    return RUN_DUTY[row["name"]] if a.plan == "pollution-levels" else a.pollution_brightness


def condition(a, row):
    """The fixed, chosen conditions of a run, saved in its meta file for grouping later."""
    change = row["change"]
    if change == "pollution_check":
        return {
            "change": "pollution_check",
            "pollution_level": "check",
            "pollution_interval_s": None,
            "pollution_brightness": a.check_levels,  # every brightness checked, in order
            "pollution_duty": a.check_levels,
            "brightness": test_led_levels(a, row),
            "timing": {"on_s": a.check_on, "off_s": a.check_off, "cycles_per_level": a.check_cycles},
        }
    duty = run_duty(a, row)
    if a.plan in LIST_BRIGHTNESS_PLANS:
        brightness = test_led_levels(a, row)  # e.g. [1.0, 0.85]
    elif change in ("dark", "constant"):
        brightness = {"type": "constant", "level": test_led_levels(a, row)[0]}
    elif change == "steps":
        brightness = {"type": "steps", "off": 0.0, "on": a.step_levels}
    else:
        brightness = {"type": "ramp", "high": a.high, "low": a.low}
    timing = {}
    if change in ("steps", "alternate"):
        timing["dwell_s"] = a.dwell
    if change == "ramp":
        timing.update(ramp_duration_s=a.ramp_duration, ramp_hold_s=a.ramp_hold, ramp_step_s=a.ramp_step)
    return {
        "change": {"dark": "none", "constant": "none", "alternate": "steps"}.get(change, change),
        "pollution_level": row["pollution_level"],
        "pollution_interval_s": getattr(a, f"pollution_{row['pollution_level']}", None),
        "pollution_brightness": None if row["pollution_level"] == "off" else duty,
        "pollution_duty": duty,
        "brightness": brightness,
        "timing": timing,
    }


def describe_pollution(a, row):
    if a.plan == "pollution-levels" and row["pollution_level"] in ("low", "high"):
        return f"{row['pollution_level']} @ {run_duty(a, row)}"
    return row["pollution_level"]


def describe_levels(a, row):
    levels = test_led_levels(a, row)
    if row["change"] == "dark" or levels == [0.0]:
        return "off"
    if row["change"] == "ramp":
        return f"ramps {a.high}-{a.low}"
    return " / ".join(str(level) for level in levels)


def parse_args():
    p = argparse.ArgumentParser(description="Light sensor rig: experiment sessions")
    p.add_argument("--plan", choices=list(PLANS), required=True,
                   help="main: sessions 1-2 (runs 1-10); small-steps: sessions 3-4 (runs 11-18); "
                        "pollution-levels: the pollution-levels block (runs 21-43); "
                        "pollution-levels-test: the check before that block; "
                        "pollution-check: a quick check of the pollution LED's brightness")
    p.add_argument("--broker", help="the laptop's IP address, e.g. 192.168.50.157")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--list", action="store_true", help="print the plan and exit")
    p.add_argument("--only", nargs="+", choices=ALL_NAMES, metavar="RUN", help="run just these runs, in plan order")
    p.add_argument("--skip", nargs="+", choices=ALL_NAMES, metavar="RUN", default=[], help="leave these runs out")
    p.add_argument("--seed", type=int, default=None,
                   help="session seed (default: a new one, never used by an earlier session). "
                        "Only reuse one to finish an interrupted session with --only.")
    p.add_argument("--time-scale", type=float, default=1.0,
                   help="multiply every duration by this, e.g. 0.05 for a quick dry run")

    g = p.add_argument_group("brightness (fixed, chosen in advance)")
    g.add_argument("--high", type=float, default=1.0, help="full brightness, 0-1 (default 1.0); also the warm-up level")
    g.add_argument("--low", type=float, default=0.2, help="main plan: dimmest planned setting (default 0.2)")
    g.add_argument("--step-levels", type=float, nargs="+",
                   help="main plan: 'on' level(s) for the steps runs, used in rotation (default: --high)")
    g.add_argument("--steps85-levels", type=float, nargs=2, default=[1.0, 0.85], metavar=("FIRST", "SECOND"),
                   help="small-steps plan: the two levels runs 13-15 alternate between (default 1.0 0.85)")
    g.add_argument("--steps95-levels", type=float, nargs=2, default=[1.0, 0.95], metavar=("FIRST", "SECOND"),
                   help="small-steps plan: the two levels runs 16-18 alternate between (default 1.0 0.95)")
    g.add_argument("--block-order", choices=["85-first", "95-first"], default="85-first",
                   help="small-steps plan: which block of steps runs first (default 85-first)")

    g = p.add_argument_group("pollution conditions (seconds each pollution LED state lasts)")
    g.add_argument("--pollution-low", type=float, nargs=2, default=[15, 25], metavar=("MIN", "MAX"))
    g.add_argument("--pollution-high", type=float, nargs=2, default=[3, 7], metavar=("MIN", "MAX"))
    g.add_argument("--pollution-brightness", type=float, default=1.0,
                   help="pollution LED brightness when on, 0-1 (default 1.0). Not used by the "
                        "pollution-levels plan, which sets the duty per block.")

    g = p.add_argument_group("pollution LED hardware")
    g.add_argument("--pollution-pwm", choices=["hardware", "software", "none"], default="hardware",
                   help="how the pollution LED is driven: hardware PWM (default), gpiozero's software "
                        "PWM, or plain on/off")
    g.add_argument("--pollution-pin", type=int, default=12, help="GPIO of the pollution LED (default 12)")
    g.add_argument("--pollution-pwmchip", type=int, default=None,
                   help="sysfs pwmchip number for hardware PWM (default: found automatically)")

    g = p.add_argument_group("pollution check (--plan pollution-check or pollution-levels-test)")
    g.add_argument("--check-levels", type=float, nargs="+", default=None, metavar="LEVEL",
                   help="pollution LED brightness levels to check, in order "
                        "(default 1.0 0.5; pollution-levels-test 0.25 0.5 1.0)")
    g.add_argument("--check-test-level", type=float, default=None,
                   help="test LED level during the check (default 0.0, off; pollution-levels-test --high)")
    g.add_argument("--check-cycles", type=int, default=3, help="on/off cycles at each level (default 3)")
    g.add_argument("--check-on", type=float, default=10, help="seconds the pollution LED is on (default 10)")
    g.add_argument("--check-off", type=float, default=10, help="seconds it's off between (default 10)")

    g = p.add_argument_group("random timing ranges (seconds)")
    g.add_argument("--dwell", type=float, nargs=2, default=[20, 60], metavar=("MIN", "MAX"),
                   help="how long each state lasts in the steps runs (default 20 60)")
    g.add_argument("--ramp-duration", type=float, nargs=2, default=[60, 180], metavar=("MIN", "MAX"),
                   help="main plan: how long each ramp takes (default 60 180)")
    g.add_argument("--ramp-hold", type=float, default=20, help="main plan: plateau after each ramp (default 20)")
    g.add_argument("--ramp-step", type=float, default=1.0, help="main plan: seconds between ramp steps (default 1.0)")

    g = p.add_argument_group("durations (minutes; defaults depend on the plan)")
    defaults = {}
    for plan, rows in PLANS.items():
        for _, _, minutes, option, *_ in rows:
            if option:
                defaults.setdefault(option, {})[plan] = minutes
    for option, by_plan in defaults.items():
        shown = ", ".join(f"{plan} {minutes:g}" for plan, minutes in by_plan.items())
        g.add_argument("--" + option.replace("_", "-"), dest=option, type=float, default=None,
                       help=f"default: {shown}")

    g = p.add_argument_group("sensor (keep the same as earlier sessions)")
    g.add_argument("--gain", choices=["low", "med", "high", "max"], default="low")
    g.add_argument("--integration", type=int, choices=[100, 200, 300, 400, 500, 600], default=100)
    g.add_argument("--period", type=float, default=1.0, help="seconds between readings (default 1.0)")
    g.add_argument("--data-dir", default=None, help="where the Pi's backup copy goes (default ~/rig-data)")

    a = p.parse_args()
    if a.check_levels is None:
        a.check_levels = list(POLLUTION_DUTIES) if a.plan == "pollution-levels-test" else [1.0, 0.5]
    if a.check_test_level is None:
        a.check_test_level = a.high if a.plan == "pollution-levels-test" else 0.0
    plan_names = {row[1] for row in PLANS[a.plan]}
    for option in ("only", "skip"):
        wrong = [name for name in (getattr(a, option) or []) if name not in plan_names]
        if wrong:
            p.error(f"--{option}: {', '.join(wrong)} not in the {a.plan} plan")
    if not 0 < a.low < a.high <= 1:
        p.error("need 0 < --low < --high <= 1")
    a.step_levels = a.step_levels or [a.high]
    if not all(0 < level <= 1 for level in a.step_levels):
        p.error("--step-levels must be between 0 and 1")
    for option in ("steps85_levels", "steps95_levels"):
        first, second = getattr(a, option)
        if not (0 <= first <= 1 and 0 <= second <= 1 and first != second):
            p.error(f"--{option.replace('_', '-')} needs two different levels between 0 and 1")
    for option in ("pollution_low", "pollution_high", "dwell", "ramp_duration"):
        low, high = getattr(a, option)
        if not 0 < low <= high:
            p.error(f"--{option.replace('_', '-')} needs 0 < MIN <= MAX")
    if not 0 < a.pollution_brightness <= 1:
        p.error("--pollution-brightness must be above 0 and at most 1")
    if not all(0 < level <= 1 for level in a.check_levels):
        p.error("--check-levels must each be above 0 and at most 1")
    if not 0 <= a.check_test_level <= 1:
        p.error("--check-test-level must be between 0 and 1")
    if a.check_cycles < 1 or a.check_on <= 0 or a.check_off <= 0:
        p.error("--check-cycles, --check-on and --check-off must be positive")
    dims = (a.pollution_brightness < 1 or a.plan in ("pollution-levels", "pollution-levels-test")
            or (a.plan == "pollution-check" and min(a.check_levels) < 1))
    if a.pollution_pwm == "none" and dims:
        p.error("dimming the pollution LED needs --pollution-pwm hardware or software")
    if a.time_scale <= 0:
        p.error("--time-scale must be positive")
    return a


def ordered_rows(a):
    rows = PLANS[a.plan]
    if a.plan == "pollution-levels":
        # Start and clean runs first, then one block per duty in this session's order
        return ([row for row in rows if row[1] not in RUN_DUTY]
                + [row for duty in a.pollution_order for row in rows if RUN_DUTY.get(row[1]) == duty])
    if a.plan == "small-steps" and a.block_order == "95-first":
        rows = ([row for row in rows if not row[1].startswith("steps")]
                + [row for row in rows if row[1].startswith("steps95")]
                + [row for row in rows if row[1].startswith("steps85")])
    return rows


def build_plan(a):
    plan = []
    for number, name, default, option, change, pollution_level, levels_option, purpose in ordered_rows(a):
        if (a.only and name not in a.only) or name in a.skip:
            continue
        minutes = check_minutes(a) if change == "pollution_check" else getattr(a, option)
        plan.append({
            "number": number, "name": name, "change": change, "pollution_level": pollution_level,
            "minutes": (default if minutes is None else minutes) * a.time_scale,
            "levels": getattr(a, levels_option) if levels_option else None, "purpose": purpose,
        })
    return plan


def main():
    a = parse_args()
    data_dir = a.data_dir or str(Path.home() / "rig-data")
    used = used_seeds(data_dir)
    if a.seed is None:
        rng = random.SystemRandom()
        a.seed = rng.randrange(1_000_000)
        while a.seed in used:
            a.seed = rng.randrange(1_000_000)
    elif a.seed in used and not a.only and not a.list:
        sys.exit(f"Seed {a.seed} was already used by session {used[a.seed]}. Every new session needs "
                 f"a new seed, so leave out --seed to get one. Reusing a seed is only for finishing "
                 f"an interrupted session with --only.")

    # The pollution-levels plan runs its duty blocks in an order drawn from the session seed,
    # so slow drift over a session doesn't always land on the same duty
    a.pollution_order = (random.Random(f"{a.seed}-pollution-order").sample(POLLUTION_DUTIES, len(POLLUTION_DUTIES))
                         if a.plan == "pollution-levels" else None)
    plan = build_plan(a)
    if not plan:
        sys.exit("Nothing to run: --only and --skip leave no runs.")
    period = max(a.period, a.integration / 1000 + 0.02)
    total = sum(row["minutes"] for row in plan)
    extras = [f"block order {a.block_order}"] if a.plan == "small-steps" else []
    if a.pollution_order:
        extras.append(f"pollution duty order {', '.join(str(duty) for duty in a.pollution_order)}")
    if a.time_scale != 1:
        extras.append(f"time scale {a.time_scale:g}")
    print(f"Plan {a.plan}, seed {a.seed}" + "".join(f", {extra}" for extra in extras) + ":")
    for row in plan:
        print(f"  {row['number']:2d}  {row['name']:<23} {row['minutes']:5.1f} min   "
              f"test LED {describe_levels(a, row):<14} pollution {describe_pollution(a, row):<11} {row['purpose']}")
    print(f"  Total {total:.0f} min ({int(total // 60)} h {total % 60:.0f} min)")
    print(f"Sensor: one reading every {period:g} s, gain {a.gain}, integration {a.integration} ms")
    if a.plan in CHECK_PLANS:
        print(f"Pollution check: duty {', '.join(str(level) for level in a.check_levels)}, "
              f"{a.check_cycles} x ({a.check_on:g} s on, {a.check_off:g} s off) each, "
              f"test LED at {a.check_test_level}")
    elif a.plan == "pollution-levels":
        print(f"Pollution LED duty per block, in this session's order: "
              f"{', '.join(str(duty) for duty in a.pollution_order)}")
    else:
        print("Pollution LED when on: " + ("full brightness" if a.pollution_brightness == 1
                                           else f"{a.pollution_brightness:g} brightness"))
    if a.list:
        return
    if not a.broker:
        sys.exit("Give the laptop's address with --broker.")

    import rig  # imported here so --list works on a computer without the Pi's libraries

    try:
        sensor = rig.adafruit_tsl2591.TSL2591(rig.board.I2C())
    except (ValueError, OSError) as e:
        sys.exit(f"Can't find the TSL2591 on I2C ({e}). Check the wiring and that "
                 f"'sudo i2cdetect -y 1' shows 29.")
    sensor.gain = rig.GAINS[a.gain]
    sensor.integration_time = rig.INTEGRATIONS[a.integration]
    time.sleep(a.integration / 1000 + 0.05)
    try:
        leds = rig.Leds(None, a.pollution_brightness, a.pollution_pwm, a.pollution_pin, a.pollution_pwmchip)
    except rig.PwmSetupError as e:
        sys.exit(str(e))
    print(f"Pollution LED: GPIO {a.pollution_pin}, {leds.describe_pollution()}")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    session = time.strftime("%Y%m%d-%H%M%S")
    finished = []
    stopped_in = None
    for position, row in enumerate(plan, 1):
        if stop.is_set():
            break
        name = row["name"]
        run_id = f"{session}_{row['number']:02d}_{name}"
        pub = rig.Publisher(a.broker, a.port, run_id, data_dir)
        if not pub.connected.wait(10):
            if position == 1:
                pub.close()
                sys.exit(f"Couldn't reach the broker at {a.broker}:{a.port}. Is Mosquitto "
                         f"running on the laptop, and is that the laptop's address?")
            print("WARNING: can't reach the broker right now. Carrying on; this run is "
                  f"saved on the Pi in {pub.backup_path}")

        carried_level = leds.test_level if leds.test_level is not None else 0.0
        leds.pub = pub  # from here on, LED events go to this run's files

        schedule_seed, pollution_seed = run_seeds(a.seed, name)
        has_pollution = row["pollution_level"] in ("low", "high")  # runs with a random pollution schedule
        pub.send("meta", {"event": "start", "test": name, "plan": a.plan, "session": session,
                          "run_index": row["number"], "position": position,
                          "run_order": a.block_order if a.plan == "small-steps" else None,
                          "pollution_order": a.pollution_order,
                          "purpose": row["purpose"], "minutes": row["minutes"],
                          **condition(a, row),
                          "session_seed": a.seed, "schedule_seed": schedule_seed,
                          "pollution_seed": pollution_seed if has_pollution else None,
                          "settings": {**vars(a), "data_dir": data_dir}, "period": period,
                          "pwm_frequency": rig.PWM_FREQUENCY, "test_led_pin": rig.TEST_LED_PIN,
                          "pollution_led_pin": a.pollution_pin, "pollution_pwm": leds.describe_pollution(),
                          "led_log_levels": "applied"})
        # Record where both LEDs are as this run begins, so every LED file is complete on its own.
        # The test LED isn't switched off between runs, so its temperature stays stable.
        leds.set_test(carried_level, "state at run start")
        leds.set_pollution(False, "state at run start")

        reader = rig.SensorReader(sensor, pub, leds, period, a.gain, a.integration)
        print(f"\n[{position}/{len(plan)}] {run_id}: {row['purpose']} "
              f"({row['minutes']:.1f} min, test LED {describe_levels(a, row)}, pollution {describe_pollution(a, row)})")
        reader.start()

        # The pollution check ends when its sequence does; the slack only covers timing overheads
        slack = 5 if row["change"] == "pollution_check" else 0
        run = Run(leds, a, schedule_seed, stop, time.monotonic() + row["minutes"] * 60 + slack)
        pollution = None
        if has_pollution:
            interval = getattr(a, f"pollution_{row['pollution_level']}")
            pollution = PollutionDriver(leds, interval, a.time_scale, pollution_seed, run_duty(a, row))
            pollution.start()

        failed = False
        try:
            BEHAVIOURS[row["change"]](run, row)
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

    leds.test.value = 0  # make sure nothing is left on, even if stopped between runs
    leds.pollution.off()

    if len(finished) == len(plan):
        print(f"\nSession {session} finished: all {len(plan)} runs done. Both LEDs are off.")
    else:
        remaining = [row["name"] for row in plan if row["name"] not in finished]
        print(f"\nSession {session} stopped early" + (f" during {stopped_in}." if stopped_in else ".")
              + f" Finished: {', '.join(finished) or 'none'}. Both LEDs are off.")
        options = {"main": f"--high {a.high:g} --low {a.low:g}",
                   "small-steps": f"--block-order {a.block_order}"}.get(a.plan, "")
        if a.pollution_brightness != 1:
            options += f" --pollution-brightness {a.pollution_brightness:g}"
        print("To run the rest with the same timings, use this plus any other options you changed")
        print("(and add 'warmup' to the list if the LED has been off a while):")
        print(f"  python session.py --plan {a.plan} --broker {a.broker} {options} "
              f"--seed {a.seed} --only {' '.join(remaining)}".replace("  --", " --"))
    print(f"Backups on the Pi are in {data_dir}")


if __name__ == "__main__":
    main()
