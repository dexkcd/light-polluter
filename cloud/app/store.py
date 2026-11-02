"""
The web app's data folder:

    runs/      every run's files, exactly as logger.py writes them on a laptop:
               <run>_sensor.csv, <run>_led.csv, <run>_meta.jsonl (+ <run>_plot.png from the checks)
    logs/      what session.py printed on the Pi, one file per session started from the app
    checks/    the latest check_session.py output for each session
"""

import csv
import io
import json
import re
import threading
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import check_session
import logger

RUN_FILE = re.compile(r"^(?P<run>\d{8}-\d{6}_[A-Za-z0-9_]+?)_(?P<kind>sensor\.csv|led\.csv|meta\.jsonl|plot\.png)$")
SESSION_ID = re.compile(r"^\d{8}-\d{6}$")
JOB_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
MAX_UPLOAD_FILE = 200 * 1024 * 1024


class RunFiles(logger.RunFiles):
    """logger.py's writer, made safe across app restarts: before a run's first message it reads
    the last seq already on disk, so messages the broker delivers again aren't written twice."""

    def write(self, msg, received_t):
        key = (msg["run"], msg["stream"])
        if key not in self.last_seq:
            last = last_seq_on_disk(self.folder, *key)
            if last is not None:
                self.last_seq[key] = last
        if msg["stream"] == "meta":
            prev = self.last_seq.get(key)
            if prev is not None and msg["seq"] <= prev:
                return
            self.last_seq[key] = msg["seq"]
        super().write(msg, received_t)


def last_seq_on_disk(folder, run, stream):
    if stream == "meta":
        path = Path(folder) / f"{run}_meta.jsonl"
        try:
            seqs = [json.loads(line).get("seq", 0) for line in path.read_text().splitlines() if line.strip()]
        except (OSError, ValueError):
            return None
        return max(seqs) if seqs else None
    path = Path(folder) / f"{run}_{stream}.csv"
    try:
        rows = read_csv(path)
    except OSError:
        return None
    seqs = [int(r["seq"]) for r in rows if (r.get("seq") or "").isdigit()]
    return max(seqs) if seqs else None


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def session_time(session_id):
    """'20261102-143000' -> '2 Nov 2026, 14:30' (the Pi's local time)."""
    try:
        t = time.strptime(session_id, "%Y%m%d-%H%M%S")
    except ValueError:
        return session_id
    return time.strftime(f"{t.tm_mday} %b %Y, %H:%M", t)


class Store:
    def __init__(self, data_dir):
        self.root = Path(data_dir)
        self.runs = self.root / "runs"
        self.logs = self.root / "logs"
        self.checks = self.root / "checks"
        for folder in (self.runs, self.logs, self.checks):
            folder.mkdir(parents=True, exist_ok=True)
        self.files = RunFiles(self.runs)
        self.lock = threading.RLock()  # the MQTT thread writes; uploads and backups rewrite files

    # ---- live data ----
    def write(self, msg, received_t):
        with self.lock:
            self.files.write(msg, received_t)

    def add_log(self, job, session, line):
        if not JOB_ID.match(job or ""):
            return
        with self.lock:
            with open(self.logs / f"{job}.log", "a") as f:
                f.write(line + "\n")
            info = self.logs / f"{job}.json"
            if session and not info.exists():
                info.write_text(json.dumps({"job": job, "session": session}))

    def session_log(self, session):
        lines = []
        for info in sorted(self.logs.glob("*.json")):
            try:
                if json.loads(info.read_text()).get("session") == session:
                    lines += info.with_suffix(".log").read_text(errors="replace").splitlines()
            except (OSError, ValueError):
                continue
        return lines

    # ---- what's stored ----
    def sessions(self):
        """session.py sessions, newest first, with a summary of each."""
        found = check_session.load_sessions(self.runs)
        out = []
        for sid, runs in sorted(found.items(), reverse=True):
            start = runs[0]["start"]
            settings = start.get("settings", {})
            out.append({
                "id": sid, "when": session_time(sid), "plan": start.get("plan", "main"),
                "seed": check_session.seed_of(start), "runs": len(runs),
                "finished": sum(1 for r in runs if r["end"] and r["end"].get("status") == "finished"),
                "dry_run": settings.get("time_scale", 1) != 1, "partial": bool(settings.get("only")),
                "size": sum(p.stat().st_size for p in self.runs.glob(f"{sid}_*")),
            })
        return out

    def single_tests(self):
        """rig.py runs (one test each, not part of a session), newest first."""
        tests = []
        for path in sorted(self.runs.glob("*_meta.jsonl"), reverse=True):
            start, end = check_session.read_meta(path)
            if start and not start.get("session"):
                run = path.name[:-len("_meta.jsonl")]
                tests.append({"run": run, "when": session_time(run.split("_")[0]), "test": start.get("test"),
                              "status": (end or {}).get("status", "no end line"), "files": self.run_files(run)})
        return tests

    def run_files(self, run):
        return [p.name for p in sorted(self.runs.glob(f"{run}_*")) if RUN_FILE.match(p.name)]

    def session_detail(self, sid):
        runs = check_session.load_sessions(self.runs).get(sid)
        if not runs:
            return None
        rows = []
        for r in runs:
            start, end = r["start"], r["end"]
            counts = {}
            for stream in ("sensor", "led"):
                path = self.runs / f"{r['run']}_{stream}.csv"
                try:
                    seqs = [int(x["seq"]) for x in read_csv(path) if (x.get("seq") or "").isdigit()]
                except OSError:
                    seqs = []
                counts[stream] = len(seqs)
                counts[f"{stream}_missing"] = (max(seqs) - len(set(seqs))) if seqs else 0
            rows.append({"run": r["run"], "number": start.get("run_index"), "name": start.get("test"),
                         "purpose": start.get("purpose"), "minutes": start.get("minutes"),
                         "pollution_level": start.get("pollution_level"), "pollution_duty": start.get("pollution_duty"),
                         "status": (end or {}).get("status", "no end line yet"), **counts,
                         "files": self.run_files(r["run"])})
        start = runs[0]["start"]
        settings = start.get("settings", {})
        return {"id": sid, "when": session_time(sid), "plan": start.get("plan", "main"),
                "seed": check_session.seed_of(start), "settings": settings, "runs": rows,
                "pollution_order": start.get("pollution_order"), "block_order": start.get("run_order"),
                "dry_run": settings.get("time_scale", 1) != 1, "partial": bool(settings.get("only")),
                "missing": sum(r["sensor_missing"] + r["led_missing"] for r in rows)}

    def file_path(self, name):
        """The path of a stored run file, or None if the name isn't one."""
        if not RUN_FILE.match(name):
            return None
        path = self.runs / name
        return path if path.is_file() else None

    def zip_to(self, out, prefix=None):
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for path in sorted(self.runs.iterdir()):
                if RUN_FILE.match(path.name) and (prefix is None or path.name.startswith(prefix + "_")):
                    z.write(path, path.name)

    # ---- filling gaps from the Pi's backup ----
    def merge_backup(self, run, lines):
        """Add what the Pi's backup file has and the stored files don't. Returns rows added per stream."""
        if not RUN_FILE.match(f"{run}_meta.jsonl"):
            return {}
        backup = defaultdict(dict)
        for line in lines:
            try:
                msg = json.loads(line)
                backup[msg["stream"]][int(msg["seq"])] = msg
            except (ValueError, KeyError, TypeError):
                continue
        added = {}
        with self.lock:
            self.files.close_run(run)
            for stream in ("sensor", "led"):
                if not backup.get(stream):
                    continue
                path = self.runs / f"{run}_{stream}.csv"
                rows = {int(r["seq"]): r for r in (read_csv(path) if path.exists() else [])
                        if (r.get("seq") or "").isdigit()}
                new = {seq: {**msg, "received_t": ""} for seq, msg in backup[stream].items() if seq not in rows}
                if new:
                    rows.update(new)
                    write_csv_atomic(path, logger.FIELDS[stream], [rows[seq] for seq in sorted(rows)])
                    added[stream] = len(new)
                self.files.last_seq[(run, stream)] = max(rows)
            if backup.get("meta"):
                path = self.runs / f"{run}_meta.jsonl"
                have = {}
                if path.exists():
                    for line in path.read_text().splitlines():
                        try:
                            msg = json.loads(line)
                            have[int(msg["seq"])] = msg
                        except (ValueError, KeyError, TypeError):
                            continue
                new = {seq: {**msg, "received_t": None} for seq, msg in backup["meta"].items() if seq not in have}
                if new:
                    have.update(new)
                    tmp = path.with_suffix(".tmp")
                    tmp.write_text("".join(json.dumps(have[seq]) + "\n" for seq in sorted(have)))
                    tmp.replace(path)
                    added["meta"] = len(new)
                self.files.last_seq[(run, "meta")] = max(have)
        return added

    # ---- adding older data ----
    def add_upload(self, filename, data):
        """Store one uploaded file (a run file, or a .zip of them). Returns (added, skipped) names."""
        name = Path(filename or "").name
        if name.lower().endswith(".zip"):
            added, skipped = [], []
            try:
                z = zipfile.ZipFile(io.BytesIO(data))
            except zipfile.BadZipFile:
                return [], [f"{name}: not a zip file I can open"]
            for info in z.infolist():
                if info.is_dir():
                    continue
                if info.file_size > MAX_UPLOAD_FILE:
                    skipped.append(f"{info.filename}: too big")
                    continue
                a, s = self.add_upload(info.filename, z.read(info)) if not info.filename.lower().endswith(".zip") \
                    else ([], [f"{info.filename}: zips inside zips aren't read"])
                added += a
                skipped += s
            return added, skipped
        if not RUN_FILE.match(name) or name.endswith("_plot.png"):
            return [], [f"{name}: not a run file (<run>_sensor.csv, <run>_led.csv or <run>_meta.jsonl)"]
        path = self.runs / name
        with self.lock:
            if path.exists():
                same = path.read_bytes() == data
                return [], [f"{name}: {'already stored' if same else 'a different file with this name is already stored'}"]
            tmp = path.with_suffix(".upload")
            tmp.write_bytes(data)
            tmp.replace(path)
        return [name], []


def write_csv_atomic(path, fields, rows):
    tmp = Path(path).with_suffix(".tmp")
    with open(tmp, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)
