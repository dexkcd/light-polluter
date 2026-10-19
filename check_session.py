#!/usr/bin/env python3
"""
Light sensor rig: check a session's data (runs on the laptop).

Reads the files logger.py wrote to data/ and prints the checks to do after a
session:

  - every run ended with status "finished"
  - the brightness in each run's meta file matches what the LED log shows
  - every session has its own seed (resumed sessions excepted)
  - the sensor settings (period, gain, integration) match the earlier sessions
  - how big each run's steps are in raw counts, and how much the pollution LED adds

Usage:
    python check_session.py                      # the newest session in data/
    python check_session.py 20261019-200512      # a specific session
    python check_session.py --plot               # also save a plot of each steps run (needs matplotlib)
"""

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

END_NOTES = {"state at run start", "session over", "stopped early", "end of run", "test finished"}


def read_meta(path):
    start = end = None
    with open(path) as f:
        for line in f:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("event") == "start" and start is None:
                start = message
            elif message.get("event") == "end":
                end = message
    return start, end


def load_sessions(folder):
    """All session.py runs in the folder, grouped by session, in the order they ran."""
    sessions = defaultdict(list)
    for path in Path(folder).glob("*_meta.jsonl"):
        start, end = read_meta(path)
        if start and start.get("session"):
            sessions[start["session"]].append({"run": path.name[:-len("_meta.jsonl")], "start": start, "end": end})
    for runs in sessions.values():
        runs.sort(key=lambda r: r["start"]["t"])
    return dict(sorted(sessions.items()))


def read_csv(path):
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except FileNotFoundError:
        return []


def seed_of(start):
    return start.get("session_seed", start.get("seed"))


def sensor_settings(start):
    settings = start.get("settings", {})
    return round(start.get("period") or 0, 3), settings.get("gain"), settings.get("integration")


def is_dry_run(start):
    return start.get("settings", {}).get("time_scale", 1) != 1


def is_partial(start):
    return bool(start.get("settings", {}).get("only"))


def readings_with_state(sensor, led):
    """Each good reading with the test LED level and pollution state in force when it
    was taken. Readings whose measurement overlapped an LED change are left out."""
    events = sorted((float(e["mono"]), e["led"], float(e["level"])) for e in led)
    state = {"test": None, "pollution": 0.0}
    last_change = None
    i = 0
    rows = []
    for r in sorted(sensor, key=lambda r: float(r["mono"])):
        mono = float(r["mono"])
        while i < len(events) and events[i][0] <= mono:
            when, which, level = events[i]
            if state[which] is not None and state[which] != level:
                last_change = when
            state[which] = level
            i += 1
        if r["error"] or not r["ch0_full"] or r["saturated"] == "True" or state["test"] is None:
            continue
        if last_change is not None and mono - last_change < int(r["integration_ms"]) / 1000:
            continue
        rows.append((state["test"], state["pollution"], int(r["ch0_full"])))
    return rows


def medians_by_level(rows, pollution):
    groups = defaultdict(list)
    for test, pol, count in rows:
        if pol == pollution:
            groups[test].append(count)
    return {level: statistics.median(counts) for level, counts in groups.items() if len(counts) >= 3}


def pollution_effect(rows):
    on, off = medians_by_level(rows, 1.0), medians_by_level(rows, 0.0)
    diffs = [on[level] - off[level] for level in on if level in off]
    return statistics.mean(diffs) if diffs else None


def brightness_matches(brightness, seen):
    """Do the test LED levels in the LED log match the brightness in the meta file?"""
    if not seen:
        return False
    if isinstance(brightness, list):
        return set(seen) == set(brightness)
    if isinstance(brightness, dict):
        kind = brightness.get("type")
        if kind == "constant":
            return set(seen) == {brightness["level"]}
        if kind == "steps":
            return set(seen) == {brightness["off"], *brightness["on"]}
        if kind == "ramp":
            return abs(min(seen) - brightness["low"]) < 1e-6 and abs(max(seen) - brightness["high"]) < 1e-6
    return False


def fmt_levels(levels):
    if len(levels) > 4:
        return f"{min(levels)} to {max(levels)} ({len(levels)} levels)"
    return ", ".join(str(level) for level in sorted(levels, reverse=True))


def plot_run(path, run, sensor, led, step):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    surface, ink, ink2, grid = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e0"
    counts_colour, pollution_colour = "#2a78d6", "#eb6834"

    good = [r for r in sensor if r["ch0_full"] and not r["error"]]
    if not good:
        return False
    t0 = min(float(r["mono"]) for r in sensor)
    t_end = max(float(r["mono"]) for r in sensor) - t0
    xs = [float(r["mono"]) - t0 for r in good]
    ys = [int(r["ch0_full"]) for r in good]
    test = sorted((float(e["mono"]) - t0, float(e["level"])) for e in led if e["led"] == "test")
    tx = [max(0.0, t) for t, _ in test] + [t_end]
    ty = [level for _, level in test] + [test[-1][1]]
    spans, on_at = [], None
    for e in sorted(led, key=lambda e: float(e["mono"])):
        if e["led"] != "pollution":
            continue
        t, level = float(e["mono"]) - t0, float(e["level"])
        if level == 1.0 and on_at is None:
            on_at = t
        elif level == 0.0 and on_at is not None:
            spans.append((on_at, t))
            on_at = None
    if on_at is not None:
        spans.append((on_at, t_end))

    fig, (top, bottom) = plt.subplots(2, 1, sharex=True, figsize=(11, 5.2),
                                      gridspec_kw={"height_ratios": [3, 1], "hspace": 0.08})
    fig.patch.set_facecolor(surface)
    for ax in (top, bottom):
        ax.set_facecolor(surface)
        for start, stop in spans:
            ax.axvspan(start, stop, color=pollution_colour, alpha=0.14, lw=0)
        ax.grid(axis="y", color=grid, lw=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(grid)
        ax.tick_params(colors=ink2, labelsize=9)
    top.plot(xs, ys, color=counts_colour, lw=1.5)
    top.set_ylabel("ch0_full (counts)", color=ink2, fontsize=10)
    title = run if step is None else f"{run}    step about {step:.0f} counts"
    top.set_title(title, loc="left", color=ink, fontsize=12)
    if spans:
        top.legend(handles=[Patch(color=pollution_colour, alpha=0.3, label="pollution LED on")],
                   loc="lower right", bbox_to_anchor=(1, 1), frameon=False, fontsize=9, labelcolor=ink2)
    bottom.step(tx, ty, where="post", color=ink2, lw=1.5)
    bottom.set_ylabel("test LED\nlevel", color=ink2, fontsize=10)
    bottom.set_xlabel("seconds since run start", color=ink2, fontsize=10)
    bottom.set_xlim(0, t_end)
    fig.savefig(path, dpi=120, bbox_inches="tight", facecolor=surface)
    plt.close(fig)
    return True


def main():
    p = argparse.ArgumentParser(description="Light sensor rig: check a session's data")
    p.add_argument("session", nargs="?", help="session to check, e.g. 20261019-200512 (default: the newest)")
    p.add_argument("--data-dir", default="data", help="folder logger.py writes to (default: data)")
    p.add_argument("--plot", action="store_true", help="save a plot of each steps run next to its data")
    a = p.parse_args()

    sessions = load_sessions(a.data_dir)
    if not sessions:
        sys.exit(f"No session.py runs found in {a.data_dir}/.")
    session = a.session or list(sessions)[-1]
    if session not in sessions:
        sys.exit(f"No session {session} in {a.data_dir}/. Sessions there: {', '.join(sessions)}")
    problems = []

    # ---- All sessions: seeds and sensor settings ----
    real = [s for s, runs in sessions.items() if not is_dry_run(runs[0]["start"])]
    usual = Counter(sensor_settings(sessions[s][0]["start"]) for s in real).most_common(1)
    usual = usual[0][0] if usual else None
    by_seed = defaultdict(list)
    for s, runs in sessions.items():
        by_seed[seed_of(runs[0]["start"])].append(s)

    print(f"Sessions in {a.data_dir}/:")
    for s, runs in sessions.items():
        start = runs[0]["start"]
        period, gain, integration = sensor_settings(start)
        finished = sum(1 for r in runs if r["end"] and r["end"].get("status") == "finished")
        notes = []
        if is_dry_run(start):
            notes.append("dry run")
        if is_partial(start):
            notes.append("partial or resumed")
        if not is_dry_run(start) and usual and sensor_settings(start) != usual:
            notes.append("SENSOR SETTINGS DIFFER")
        print(f"  {s}{'*' if s == session else ' '} {start.get('plan', 'main'):<11} seed {seed_of(start)!s:>7}   "
              f"every {period:g} s, gain {gain}, {integration} ms   {finished}/{len(runs)} runs finished"
              + (f"   ({', '.join(notes)})" if notes else ""))
    for seed, users in by_seed.items():
        fresh = [s for s in users if not is_partial(sessions[s][0]["start"])]
        if len(fresh) > 1:
            problems.append(f"Seed {seed} was used by more than one session: {', '.join(fresh)}.")
    start = sessions[session][0]["start"]
    if not is_dry_run(start) and usual and sensor_settings(start) != usual:
        problems.append(f"Session {session} used sensor settings {sensor_settings(start)} "
                        f"(period, gain, integration), but the other sessions used {usual}.")

    # ---- The chosen session, run by run ----
    plan = start.get("plan", "main")
    order = f", block order {start['run_order']}" if start.get("run_order") else ""
    print(f"\nSession {session}* ({plan}, seed {seed_of(start)}{order}):")
    print(f"  {'#':>2}  {'run':<23} {'status':<14} {'brightness (meta)':<22} {'levels in LED log':<22} "
          f"{'pollution':>9} {'readings':>8}")
    results, effects = [], []
    for r in sessions[session]:
        meta, end = r["start"], r["end"]
        name = meta.get("test")
        status = end.get("status") if end else "no end line"
        sensor = read_csv(Path(a.data_dir) / f"{r['run']}_sensor.csv")
        led = read_csv(Path(a.data_dir) / f"{r['run']}_led.csv")
        seen = sorted({float(e["level"]) for e in led if e["led"] == "test" and e["note"] not in END_NOTES})
        switches = sum(1 for e in led if e["led"] == "pollution" and e["level"] == "1.0")
        brightness = meta.get("brightness")
        shown = json.dumps(brightness) if isinstance(brightness, list) else (brightness or {}).get("type", "?")
        print(f"  {meta.get('run_index', ''):>2}  {name:<23} {status:<14} {shown:<22} {fmt_levels(seen):<22} "
              f"{switches:>9} {len(sensor):>8}")

        if status != "finished":
            problems.append(f"{name}: status is '{status}', not 'finished'.")
        if not brightness_matches(brightness, seen):
            problems.append(f"{name}: brightness in the meta file ({shown}) doesn't match the LED log ({fmt_levels(seen)}).")
        errors = sum(1 for x in sensor if x["error"])
        saturated = sum(1 for x in sensor if x["saturated"] == "True")
        if errors:
            problems.append(f"{name}: {errors} sensor read(s) failed.")
        if saturated:
            problems.append(f"{name}: {saturated} reading(s) saturated.")

        rows = readings_with_state(sensor, led)
        if meta.get("pollution_level") not in (None, "off"):
            effect = pollution_effect(rows)
            if effect is not None:
                effects.append(effect)
        if meta.get("change") == "steps":
            medians = medians_by_level(rows, 0.0)
            step = max(medians.values()) - min(medians.values()) if len(medians) >= 2 else None
            results.append((meta.get("run_index", ""), name, medians, step, r["run"], sensor, led))

    pollution = statistics.median(effects) if effects else None
    if results:
        print("\nStep sizes: median counts at each test LED level, pollution LED off, leaving out readings")
        print("taken while an LED was changing.")
        for number, name, medians, step, *_ in results:
            levels = "   ".join(f"{level}: {count:.1f}" for level, count in sorted(medians.items(), reverse=True))
            ratio = f"  ({step / pollution:.1f}x the pollution)" if step is not None and pollution else ""
            size = f"   step {step:.1f} counts{ratio}" if step is not None else "   not enough readings"
            print(f"  {number:>2}  {name:<23} {levels}{size}")
    if pollution is not None:
        print(f"\nThe pollution LED adds about {pollution:.1f} counts (median over this session's pollution runs).")

    if a.plot:
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            problems.append("--plot needs matplotlib: pip install matplotlib")
        else:
            saved = []
            for number, name, medians, step, run, sensor, led in results:
                path = Path(a.data_dir) / f"{run}_plot.png"
                if plot_run(path, run, sensor, led, step):
                    saved.append(str(path))
            if saved:
                print(f"\nSaved {len(saved)} plots:\n  " + "\n  ".join(saved))

    if problems:
        print(f"\n{len(problems)} problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
