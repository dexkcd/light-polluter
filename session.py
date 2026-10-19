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

Only timing is random: how long each dwell lasts, how long each ramp takes and
when the pollution LED switches. The brightness levels and the pollution
condition are fixed, chosen in advance, and saved as labels in each run's meta
file so results can be grouped by condition afterwards.

Every session gets a new session seed (checked against the earlier sessions'
backups on the Pi), and each run's own seeds are derived from it and saved in
its meta file. Every LED command is logged with its exact time, including each
run's first command even when it leaves the LED where it was.

Examples:
    python session.py --plan small-steps --list
    python session.py --plan small-steps --broker 192.168.50.157 --time-scale 0.05   # ~4 min dry run
    python session.py --plan small-steps --broker 192.168.50.157                     # session 3
    python session.py --plan small-steps --broker 192.168.50.157 --block-order 95-first   # session 4
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
}
ALL_NAMES = sorted({row[1] for rows in PLANS.values() for row in rows})
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
        """Set the test LED. Publishes an event when the level changes, or always with force=True
        (used for each run's first command, so the log shows exactly when it was given)."""
        if force or self.leds.test_level is None or round(level, 4) != self.leds.test_level:
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


BEHAVIOURS = {"dark": dark, "constant": constant, "steps": steps, "alternate": alternate, "ramp": ramps}


def test_led_levels(a, row):
    """The fixed test LED levels of a run, as a list."""
    if row["change"] == "dark":
        return [0.0]
    if row["change"] == "constant":
        return [getattr(a, CONSTANT_LEVEL[row["name"]])]
    if row["change"] == "alternate":
        return list(row["levels"])
    if row["change"] == "steps":
        return [0.0] + a.step_levels
    return [a.high, a.low]


def condition(a, row):
    """The fixed, chosen conditions of a run, saved in its meta file for grouping later."""
    change = row["change"]
    if a.plan == "small-steps":
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
        "brightness": brightness,
        "timing": timing,
    }


def describe_levels(a, row):
    levels = test_led_levels(a, row)
    if row["change"] == "dark":
        return "off"
    if row["change"] == "ramp":
        return f"ramps {a.high}-{a.low}"
    return " / ".join(str(level) for level in levels)


def parse_args():
    p = argparse.ArgumentParser(description="Light sensor rig: experiment sessions")
    p.add_argument("--plan", choices=list(PLANS), required=True,
                   help="main: sessions 1-2 (runs 1-10); small-steps: sessions 3-4 (runs 11-18)")
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
    if a.time_scale <= 0:
        p.error("--time-scale must be positive")
    return a


def ordered_rows(a):
    rows = PLANS[a.plan]
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
        minutes = getattr(a, option)
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

    plan = build_plan(a)
    if not plan:
        sys.exit("Nothing to run: --only and --skip leave no runs.")
    period = max(a.period, a.integration / 1000 + 0.02)
    total = sum(row["minutes"] for row in plan)
    extras = [f"block order {a.block_order}"] if a.plan == "small-steps" else []
    if a.time_scale != 1:
        extras.append(f"time scale {a.time_scale:g}")
    print(f"Plan {a.plan}, seed {a.seed}" + "".join(f", {extra}" for extra in extras) + ":")
    for row in plan:
        print(f"  {row['number']:2d}  {row['name']:<23} {row['minutes']:5.1f} min   "
              f"test LED {describe_levels(a, row):<12} pollution {row['pollution_level']:<4}  {row['purpose']}")
    print(f"  Total {total:.0f} min ({int(total // 60)} h {total % 60:.0f} min)")
    print(f"Sensor: one reading every {period:g} s, gain {a.gain}, integration {a.integration} ms")
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

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    session = time.strftime("%Y%m%d-%H%M%S")
    leds = None
    finished = []
    stopped_in = None
    for position, row in enumerate(plan, 1):
        if stop.is_set():
            break
        name = row["name"]
        run_id = f"{session}_{row['number']:02d}_{name}"
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
        has_pollution = row["pollution_level"] != "off"
        pub.send("meta", {"event": "start", "test": name, "plan": a.plan, "session": session,
                          "run_index": row["number"], "position": position,
                          "run_order": a.block_order if a.plan == "small-steps" else None,
                          "purpose": row["purpose"], "minutes": row["minutes"],
                          **condition(a, row),
                          "session_seed": a.seed, "schedule_seed": schedule_seed,
                          "pollution_seed": pollution_seed if has_pollution else None,
                          "settings": {**vars(a), "data_dir": data_dir}, "period": period,
                          "pwm_frequency": rig.PWM_FREQUENCY, "test_led_pin": rig.TEST_LED_PIN,
                          "pollution_led_pin": rig.POLLUTION_LED_PIN})
        # Record where both LEDs are as this run begins, so every LED file is complete on its own.
        # The test LED isn't switched off between runs, so its temperature stays stable.
        leds.set_test(carried_level, "state at run start")
        leds.set_pollution(False, "state at run start")

        reader = rig.SensorReader(sensor, pub, leds, period, a.gain, a.integration)
        print(f"\n[{position}/{len(plan)}] {run_id}: {row['purpose']} "
              f"({row['minutes']:.1f} min, test LED {describe_levels(a, row)}, pollution {row['pollution_level']})")
        reader.start()

        run = Run(leds, a, schedule_seed, stop, time.monotonic() + row["minutes"] * 60)
        pollution = None
        if has_pollution:
            interval = getattr(a, f"pollution_{row['pollution_level']}")
            pollution = PollutionDriver(leds, interval, a.time_scale, pollution_seed)
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

    if leds is not None:  # make sure nothing is left on, even if stopped between runs
        leds.test.value = 0
        leds.pollution.off()

    if len(finished) == len(plan):
        print(f"\nSession {session} finished: all {len(plan)} runs done. Both LEDs are off.")
    else:
        remaining = [row["name"] for row in plan if row["name"] not in finished]
        print(f"\nSession {session} stopped early" + (f" during {stopped_in}." if stopped_in else ".")
              + f" Finished: {', '.join(finished) or 'none'}. Both LEDs are off.")
        options = f"--high {a.high:g} --low {a.low:g}" if a.plan == "main" else f"--block-order {a.block_order}"
        print("To run the rest with the same timings, use this plus any other options you changed")
        print("(and add 'warmup' to the list if the LED has been off a while):")
        print(f"  python session.py --plan {a.plan} --broker {a.broker} {options} "
              f"--seed {a.seed} --only {' '.join(remaining)}")
    print(f"Backups on the Pi are in {data_dir}")


if __name__ == "__main__":
    main()
