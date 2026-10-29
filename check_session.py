#!/usr/bin/env python3
"""
Light sensor rig: check a session's data (runs on the laptop).

Reads the files logger.py wrote to data/ and prints the checks to do after a
session:

  - every run ended with status "finished"
  - the brightness and pollution duty in each run's meta file match what the LED log shows
  - every session has its own seed (resumed sessions excepted)
  - the sensor settings (period, gain, integration) match the earlier sessions
  - the rig is unchanged: the dark reading and the settled warm-up level match earlier sessions
  - how big each run's steps are in raw counts, and how much the pollution LED adds at each duty
  - no flicker: how much the readings vary within steady stretches, with the pollution LED off
    and at each duty

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
    """Each good reading with the test LED level and pollution level in force when it was
    taken, and the number of the steady stretch it belongs to (it goes up at every LED change).
    Readings whose measurement overlapped an LED change are left out."""
    events = sorted((float(e["mono"]), e["led"], float(e["level"])) for e in led)
    state = {"test": None, "pollution": 0.0}
    last_change = None
    stretch = 0
    i = 0
    rows = []
    for r in sorted(sensor, key=lambda r: float(r["mono"])):
        mono = float(r["mono"])
        while i < len(events) and events[i][0] <= mono:
            when, which, level = events[i]
            if state[which] is not None and state[which] != level:
                last_change = when
                stretch += 1
            state[which] = level
            i += 1
        if r["error"] or not r["ch0_full"] or r["saturated"] == "True" or state["test"] is None:
            continue
        if last_change is not None and mono - last_change < int(r["integration_ms"]) / 1000:
            continue
        rows.append((state["test"], state["pollution"], int(r["ch0_full"]), stretch))
    return rows


def medians_by_level(rows, pollution):
    """Median counts at each test LED level, with the pollution LED on (True) or off (False)."""
    groups = defaultdict(list)
    for test, pol, count, _ in rows:
        if (pol > 0) == pollution:
            groups[test].append(count)
    return {level: statistics.median(counts) for level, counts in groups.items() if len(counts) >= 3}


def effect_by_brightness(rows):
    """Pollution check: counts the pollution LED adds at each brightness, against its off periods."""
    off = [count for _, pol, count, _ in rows if pol == 0]
    on = defaultdict(list)
    for _, pol, count, _ in rows:
        if pol > 0:
            on[pol].append(count)
    if len(off) < 3:
        return {}
    base = statistics.median(off)
    return {level: statistics.median(counts) - base for level, counts in on.items() if len(counts) >= 3}


def pollution_effect(rows):
    on, off = medians_by_level(rows, True), medians_by_level(rows, False)
    diffs = [on[level] - off[level] for level in on if level in off]
    return statistics.mean(diffs) if diffs else None


def spread_by_pollution(rows):
    """How much readings vary within steady stretches (pooled standard deviation around each
    stretch's mean), with the pollution LED off (0.0) and at each duty. Stretches of fewer than
    4 readings, such as the steps of a ramp, are left out."""
    stretches = defaultdict(list)
    for _, pol, count, stretch in rows:
        stretches[(stretch, pol)].append(count)
    squares, dof = defaultdict(float), defaultdict(int)
    for (_, pol), counts in stretches.items():
        if len(counts) >= 4:
            mean = statistics.fmean(counts)
            squares[pol] += sum((c - mean) ** 2 for c in counts)
            dof[pol] += len(counts) - 1
    return {pol: (squares[pol] / dof[pol]) ** 0.5 for pol in squares if dof[pol] >= 6}


def duty_of(meta):
    """The pollution duty a run was meant to use when on: a number, a list (pollution check), or
    None for runs from before pollution_duty was recorded."""
    if "pollution_duty" in meta:
        return meta["pollution_duty"]
    if meta.get("pollution_level") in ("low", "high"):
        return meta.get("pollution_brightness") or 1.0
    return None


def brightness_matches(brightness, seen, change=None):
    """Do the test LED levels in the LED log match the brightness in the meta file?"""
    if not seen:
        return False
    if isinstance(brightness, list) and change == "ramp":
        return abs(min(seen) - min(brightness)) < 1e-6 and abs(max(seen) - max(brightness)) < 1e-6
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


def rig_level(name, meta, sensor):
    """The dark run's median reading, or the warm-up's median over its last minute (only for
    warm-ups of 5 minutes or more, so the LED has settled)."""
    good = [(float(r["mono"]), int(r["ch0_full"])) for r in sensor if r["ch0_full"] and not r["error"]]
    if len(good) < 3:
        return None
    if name == "dark":
        return statistics.median(count for _, count in good)
    if (meta.get("minutes") or 0) < 4.9:
        return None
    end = max(mono for mono, _ in good)
    last = [count for mono, count in good if mono >= end - 60]
    return statistics.median(last) if len(last) >= 3 else None


def earlier_reference(folder, sessions, session):
    """From the sessions before this one (dry runs left out): each session's dark reading, settled
    warm-up level and pollution effect at full duty, and the median of each."""
    found = {"dark": defaultdict(list), "warmup": defaultdict(list), "pollution": defaultdict(list)}
    for other, runs in sessions.items():
        if other >= session or is_dry_run(runs[0]["start"]):  # session names sort by time
            continue
        for r in runs:
            meta, name = r["start"], r["start"].get("test")
            wanted = name in ("dark", "warmup") or (meta.get("pollution_level") in ("low", "high")
                                                    and duty_of(meta) == 1.0)
            if not wanted:
                continue
            sensor = read_csv(Path(folder) / f"{r['run']}_sensor.csv")
            if name in ("dark", "warmup"):
                level = rig_level(name, meta, sensor)
                if level is not None:
                    found[name][other].append(level)
            else:
                effect = pollution_effect(readings_with_state(sensor, read_csv(Path(folder) / f"{r['run']}_led.csv")))
                if effect is not None:
                    found["pollution"][other].append(effect)
    reference = {}
    for key, by_session in found.items():
        per_session = [(other, statistics.median(values)) for other, values in sorted(by_session.items())]
        reference[key] = statistics.median(v for _, v in per_session) if per_session else None
        reference[key + "_by_session"] = per_session
    return reference


def fmt_earlier(earlier, key, last=6):
    """The earlier sessions' values, newest last, so a change shows up where it happened."""
    per_session = earlier[key + "_by_session"][-last:]
    return ", ".join(f"{value:.1f} ({other})" for other, value in per_session)


def compare_full_duty(added, earlier, label, problems):
    """Full duty should add about as much as the pollution LED did in earlier sessions."""
    before = earlier["pollution"]
    if before is None:
        return
    print(f"  at full duty it adds {added:.1f} counts; earlier sessions: median {before:.1f}")
    print(f"    by session: {fmt_earlier(earlier, 'pollution')}")
    if abs(added - before) > max(3, 0.15 * before):
        problems.append(f"{label}: at full duty the pollution LED adds {added:.1f} counts, against {before:.1f} "
                        f"in earlier sessions. Check its wiring and that nothing moved.")


def fmt_levels(levels):
    if len(levels) > 4:
        return f"{min(levels)} to {max(levels)} ({len(levels)} levels)"
    return ", ".join(str(level) for level in sorted(levels, reverse=True))


def plot_run(path, run, sensor, led, title, lower="test"):
    """Counts over time, pollution-on periods shaded, and the `lower` LED's commanded level below."""
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
    shown = sorted((float(e["mono"]) - t0, float(e["level"])) for e in led if e["led"] == lower)
    tx = [max(0.0, t) for t, _ in shown] + [t_end]
    ty = [level for _, level in shown] + [shown[-1][1]]
    spans, on_at = [], None
    for e in sorted(led, key=lambda e: float(e["mono"])):
        if e["led"] != "pollution":
            continue
        t, level = float(e["mono"]) - t0, float(e["level"])
        if level > 0 and on_at is None:
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
    top.set_title(title, loc="left", color=ink, fontsize=12)
    if spans:
        top.legend(handles=[Patch(color=pollution_colour, alpha=0.3, label="pollution LED on")],
                   loc="lower right", bbox_to_anchor=(1, 1), frameon=False, fontsize=9, labelcolor=ink2)
    bottom.step(tx, ty, where="post", color=ink2, lw=1.5)
    bottom.set_ylabel(f"{lower} LED\nlevel", color=ink2, fontsize=10)
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
        dimmed = start.get("settings", {}).get("pollution_brightness", 1.0)
        if dimmed != 1.0:
            notes.append(f"pollution LED at {dimmed:g}")
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
    dimmed = start.get("settings", {}).get("pollution_brightness", 1.0)
    order += f", pollution LED at {dimmed:g} brightness" if dimmed != 1.0 else ""
    print(f"\nSession {session}* ({plan}, seed {seed_of(start)}{order}):")
    if start.get("pollution_order"):
        print(f"Pollution duty order: {', '.join(str(duty) for duty in start['pollution_order'])}")
    print(f"  {'#':>2}  {'run':<23} {'status':<14} {'brightness (meta)':<22} {'levels in LED log':<22} "
          f"{'pollution on':>13} {'readings':>8}")
    results, checks = [], []
    effects, spreads = defaultdict(list), defaultdict(list)
    rig = {}
    for r in sessions[session]:
        meta, end = r["start"], r["end"]
        name = meta.get("test")
        status = end.get("status") if end else "no end line"
        sensor = read_csv(Path(a.data_dir) / f"{r['run']}_sensor.csv")
        led = read_csv(Path(a.data_dir) / f"{r['run']}_led.csv")
        seen = sorted({float(e["level"]) for e in led if e["led"] == "test" and e["note"] not in END_NOTES})
        pollution_on = [float(e["level"]) for e in led if e["led"] == "pollution" and float(e["level"]) > 0]
        brightness = meta.get("brightness")
        shown = json.dumps(brightness) if isinstance(brightness, list) else (brightness or {}).get("type", "?")
        on_shown = f"{len(pollution_on)} x {fmt_levels(sorted(set(pollution_on)))}" if pollution_on else "0"
        print(f"  {meta.get('run_index', ''):>2}  {name:<23} {status:<14} {shown:<22} {fmt_levels(seen):<22} "
              f"{on_shown:>13} {len(sensor):>8}")

        if status != "finished":
            problems.append(f"{name}: status is '{status}', not 'finished'.")
        if not brightness_matches(brightness, seen, meta.get("change")):
            problems.append(f"{name}: brightness in the meta file ({shown}) doesn't match the LED log ({fmt_levels(seen)}).")
        duty = duty_of(meta)
        if duty is not None:
            meant = set(duty) if isinstance(duty, list) else ({duty} if duty else set())
            if set(pollution_on) != meant:
                problems.append(f"{name}: pollution_duty in the meta file ({duty}) doesn't match the LED log "
                                f"(pollution LED on at {fmt_levels(sorted(set(pollution_on))) or 'nothing'}).")
        errors = sum(1 for x in sensor if x["error"])
        saturated = sum(1 for x in sensor if x["saturated"] == "True")
        if errors:
            problems.append(f"{name}: {errors} sensor read(s) failed.")
        if saturated:
            problems.append(f"{name}: {saturated} reading(s) saturated.")

        rows = readings_with_state(sensor, led)
        if meta.get("pollution_level") in ("low", "high", "check"):
            # Only runs where the pollution LED switches, so "off" means the same conditions as "on";
            # a warm-up's slow drift would otherwise look like noise
            for pol, spread in spread_by_pollution(rows).items():
                spreads[pol].append(spread)
        if name in ("dark", "warmup"):
            rig[name] = rig_level(name, meta, sensor)
        if meta.get("change") == "pollution_check":
            off_noise = spread_by_pollution(rows).get(0.0)
            checks.append((name, meta, effect_by_brightness(rows), r["run"], sensor, led, off_noise))
        elif meta.get("pollution_level") in ("low", "high"):
            effect = pollution_effect(rows)
            if effect is not None:
                effects[duty_of(meta)].append(effect)
        if meta.get("change") == "steps":
            medians = medians_by_level(rows, False)
            step = max(medians.values()) - min(medians.values()) if len(medians) >= 2 else None
            results.append((meta.get("run_index", ""), name, medians, step, r["run"], sensor, led, duty))

    # ---- Rig unchanged: dark and settled warm-up against the earlier sessions ----
    earlier = earlier_reference(a.data_dir, sessions, session)
    if rig:
        print("\nRig check, against the earlier sessions:")
        if "dark" in rig and rig["dark"] is not None:
            before = earlier["dark"]
            print(f"  dark: {rig['dark']:.1f} counts" + (f" (earlier sessions: median {before:.1f})" if before is not None else ""))
            if before is not None:
                print(f"    by session: {fmt_earlier(earlier, 'dark')}")
            if before is not None and abs(rig["dark"] - before) > 3:
                problems.append(f"The dark reading ({rig['dark']:.1f} counts) differs from the earlier sessions "
                                f"({before:.1f}). Is the box still sealed and nothing moved?")
        if "warmup" in rig and rig["warmup"] is not None:
            before = earlier["warmup"]
            print(f"  warm-up, last minute: {rig['warmup']:.1f} counts"
                  + (f" (earlier 5-minute warm-ups: median {before:.1f})" if before is not None else ""))
            if before is not None:
                print(f"    by session: {fmt_earlier(earlier, 'warmup')}")
            if before is not None and abs(rig["warmup"] - before) > 0.02 * before:
                problems.append(f"The settled warm-up level ({rig['warmup']:.1f} counts) is more than 2 % from the "
                                f"earlier sessions ({before:.1f}). Did the sensor or the test LED move?")

    for name, meta, by_level, _, _, _, off_noise in checks:
        # A change counts as clear if it's well above the readings' own noise (at least 1 count)
        clear = max(1.0, 2 * off_noise) if off_noise is not None else 2.0
        print(f"\nPollution check ({name}): counts the pollution LED adds at each brightness, against its")
        print(f"off periods, with the test LED at {meta['brightness'][0]}.")
        checked = meta.get("pollution_brightness") or []
        top = max(by_level) if by_level else None
        for level in checked:
            if level not in by_level:
                print(f"  brightness {level}: not enough readings")
                problems.append(f"{name}: not enough readings at pollution brightness {level}.")
                continue
            share = ""
            if level != top and by_level[top] > 0:
                expected = by_level[top] * level / top
                share = (f"  ({100 * by_level[level] / by_level[top]:.0f} % of the effect at {top}; "
                         f"about {expected:.1f} if light follows brightness)")
            print(f"  brightness {level}: adds {by_level[level]:.1f} counts{share}")
            if by_level[level] <= clear:
                problems.append(f"{name}: at brightness {level} the pollution LED hardly changes the "
                                f"reading ({by_level[level]:.1f} counts). Is it wired and pointing into the box?")
        levels = sorted(by_level)
        for low, high in zip(levels, levels[1:]):
            if by_level[high] - by_level[low] < clear:
                problems.append(f"{name}: the pollution LED gives about the same light at brightness {low} "
                                f"({by_level[low]:.1f} counts) as at {high} ({by_level[high]:.1f} counts). "
                                f"Its brightness setting isn't changing its light.")
        if 1.0 in by_level:
            compare_full_duty(by_level[1.0], earlier, name, problems)

    # ---- Pollution effect at each duty, from the session's steps runs ----
    pollution = {duty: statistics.median(found) for duty, found in effects.items() if not isinstance(duty, list)}
    if len(pollution) > 1:
        print("\nPollution effect at each duty (median over the session's pollution runs):")
        for duty in sorted(pollution):
            print(f"  duty {duty}: adds {pollution[duty]:.1f} counts")
        duties = sorted(pollution)
        for low, high in zip(duties, duties[1:]):
            if pollution[high] - pollution[low] < 2:
                problems.append(f"The pollution LED adds about as much at duty {low} ({pollution[low]:.1f} counts) "
                                f"as at duty {high} ({pollution[high]:.1f}). Its duty isn't changing its light.")
    elif pollution:
        duty, added = next(iter(pollution.items()))
        print(f"\nThe pollution LED adds about {added:.1f} counts (median over this session's pollution runs).")
    if 1.0 in pollution:
        compare_full_duty(pollution[1.0], earlier, "This session", problems)

    if results:
        print("\nStep sizes: median counts at each test LED level, pollution LED off, leaving out readings")
        print("taken while an LED was changing.")
        for number, name, medians, step, _, _, _, duty in results:
            levels = "   ".join(f"{level}: {count:.1f}" for level, count in sorted(medians.items(), reverse=True))
            # A polluted run against its own duty; a clean run against every duty in the session
            against = {duty: pollution[duty]} if duty in pollution else pollution
            ratios = ", ".join(f"{step / added:.1f}x the pollution" + (f" at duty {d}" if len(pollution) > 1 else "")
                               for d, added in sorted(against.items()) if added > 0) if step is not None else ""
            size = (f"   step {step:.1f} counts" + (f"  ({ratios})" if ratios else "")) if step is not None \
                else "   not enough readings"
            print(f"  {number:>2}  {name:<23} {levels}{size}")

    # ---- No flicker: spread within steady stretches, pollution off and at each duty ----
    spread = {pol: statistics.median(found) for pol, found in spreads.items()}
    if spread and any(pol > 0 for pol in spread):
        print("\nNoise: how much readings vary within steady stretches of the pollution runs")
        print("(standard deviation, counts):")
        print("  " + "   ".join(("pollution off" if pol == 0 else f"duty {pol}") + f": {value:.1f}"
                                for pol, value in sorted(spread.items())))
        off = spread.get(0.0)
        for pol, value in sorted(spread.items()):
            if pol > 0 and off is not None and value > 2 * off and value > 2:
                problems.append(f"With the pollution LED at duty {pol} the readings vary by {value:.1f} counts, "
                                f"against {off:.1f} with it off. It may be flickering.")

    if a.plot:
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            problems.append("--plot needs matplotlib: pip install matplotlib")
        else:
            saved = []
            for number, name, medians, step, run, sensor, led, _ in results:
                path = Path(a.data_dir) / f"{run}_plot.png"
                title = run if step is None else f"{run}    step about {step:.0f} counts"
                if plot_run(path, run, sensor, led, title):
                    saved.append(str(path))
            for name, meta, by_level, run, sensor, led, _ in checks:
                path = Path(a.data_dir) / f"{run}_plot.png"
                added = ", ".join(f"{level}: +{count:.0f}" for level, count in sorted(by_level.items(), reverse=True))
                if plot_run(path, run, sensor, led, f"{run}    {added} counts", lower="pollution"):
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
