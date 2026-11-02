#!/usr/bin/env python3
"""
Light sensor rig: the Pi's always-on agent, so sessions can be started and stopped
from the web app.

It connects to the broker (RIG_BROKER and the pi user's password, from
~/.config/light-polluter/rig.env), waits for commands on rig/cmd and runs
session.py when asked, exactly as if it were typed in a terminal on the Pi.
The data still goes through session.py's own connection and backup files; the
agent only reports on what's happening:

    rig/agent/status   retained: idle, running, stopping, busy (a script started by
                       hand holds the LEDs) or offline, with the plan and progress
    rig/agent/log      each line session.py prints
    rig/agent/reply    the answer to each command: accepted, or why not
    rig/agent/backup   a session's backup files, when the app asks for them to fill gaps

Commands (JSON on rig/cmd; only the web app's broker user can send them):
    {"id": "...", "t": ..., "action": "start", "plan": "small-steps", "options": {"block_order": "95-first"}}
    {"id": "...", "t": ..., "action": "stop"}
    {"id": "...", "t": ..., "action": "status"}
    {"id": "...", "t": ..., "action": "backup", "session": "20261102-143000"}

Options are session.py's own, checked with session.py's own rules before anything
starts. The broker and data folder can't be set from the app. Commands more than
two minutes old are ignored, so nothing starts long after it was asked for.

session.py's output goes to ~/rig-data/agent/<job>.log, not through the agent, so
restarting the agent (to update the code, say) doesn't stop a running session: the
new agent picks it up again. Stop also works on a script started by hand.

Install it as a service with pi/install_agent.sh. To try it in a terminal:
    python pi_agent.py
"""

import json
import os
import random
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import mqtt_link
import session as plans

HERE = Path(__file__).resolve().parent
COMMAND_MAX_AGE = 120        # seconds
HEARTBEAT = 30               # seconds between status updates while nothing changes
STOP_ESCALATION = ((45, signal.SIGTERM), (90, signal.SIGKILL))  # if a stop is ignored
BACKUP_CHUNK = 2000          # backup lines per message

RUN_LINE = re.compile(r"^\[(\d+)/(\d+)\] ((\d{8}-\d{6})_\S+): ")
PLAN_LINE = re.compile(r"^Plan (\S+), seed (\d+)")
END_LINE = re.compile(r"^Session (\d{8}-\d{6}) (finished|stopped early)")
LOCK_LINE = re.compile(r"^pid (\d+): (.*)$")
SESSION_ID = re.compile(r"^\d{8}-\d{6}$")


def code_version():
    try:
        return subprocess.run(["git", "-C", str(HERE), "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def rig_process(pid):
    """The command line of pid if it's a running rig script (session.py or rig.py), else None."""
    try:
        os.kill(pid, 0)
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return None
    args = [arg.decode(errors="replace") for arg in args if arg]
    scripts = [i for i, arg in enumerate(args) if Path(arg).name in ("session.py", "rig.py")]
    if not scripts:
        return None
    i = scripts[0]
    return " ".join([Path(args[i]).name, *args[i + 1:]])


def leds_off():
    """After a script had to be killed: switch the pollution LED's hardware PWM off.
    (The test LED's FemtoBuck goes back to full brightness whenever nothing drives it.)"""
    for chip in Path("/sys/class/pwm").glob("pwmchip*"):
        if "98000.pwm" in str(chip.resolve()):
            for name, value in (("duty_cycle", "0"), ("enable", "0")):
                try:
                    (chip / "pwm0" / name).write_text(value)
                except OSError:
                    pass


class Agent:
    def __init__(self, broker, data_dir, data_dir_given):
        self.broker = broker
        self.data_dir = Path(data_dir)
        self.data_dir_given = data_dir_given
        self.agent_dir = self.data_dir / "agent"
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        self.job_file = self.agent_dir / "job.json"
        self.lock = threading.RLock()
        self.job = None
        self.last = None
        self.started = time.time()
        self.code = code_version()
        self.halt = threading.Event()
        self.wake = threading.Event()

        self.client, settings = mqtt_link.make_client(f"light-rig-agent-{mqtt_link.topic(socket.gethostname())}".replace("/", "-"), broker)
        self.client.will_set(mqtt_link.topic("agent/status"), json.dumps({"state": "offline", "t": None}), qos=1, retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.connect_async(settings.host, settings.port, keepalive=30)
        self.adopt()

    # ---- broker ----
    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            print(f"Broker refused the connection: {reason_code}. Check RIG_MQTT_USERNAME and RIG_MQTT_PASSWORD.")
            return
        client.subscribe(mqtt_link.topic("cmd"), qos=1)
        print(f"Connected to {mqtt_link.describe(self.broker)}; waiting for commands.")
        self.publish_status()

    def on_message(self, client, userdata, message):
        try:
            cmd = json.loads(message.payload)
            if not isinstance(cmd, dict):
                raise ValueError("not an object")
            age = time.time() - float(cmd.get("t") or 0)
        except (TypeError, ValueError):
            print(f"Ignored a command I couldn't read: {message.payload[:200]!r}")
            return
        action, cmd_id = cmd.get("action"), str(cmd.get("id") or "")[:64]
        if abs(age) > COMMAND_MAX_AGE:
            print(f"Ignored a '{action}' command from {age:.0f} s ago.")
            self.reply(cmd_id, False, "The command arrived too late (or the Pi's clock is off), so it was ignored.")
            return
        handlers = {"start": self.start, "stop": self.stop, "status": self.status_cmd, "backup": self.backup}
        if action not in handlers:
            self.reply(cmd_id, False, f"Unknown action '{action}'.")
            return
        try:
            handlers[action](cmd_id, cmd)
        except plans.OptionError as e:
            self.reply(cmd_id, False, str(e))
        except Exception as e:  # report anything unexpected rather than crash the agent
            print(f"'{action}' failed: {e!r}")
            self.reply(cmd_id, False, f"The Pi agent hit an error: {e}")

    def reply(self, cmd_id, ok, error=None, **extra):
        self.client.publish(mqtt_link.topic("agent/reply"), json.dumps({"id": cmd_id, "ok": ok, "error": error, **extra}), qos=1)

    def publish_status(self):
        with self.lock:
            manual = self.manual_script()
            job = self.job
            state = (job.get("state", "running") if job else "busy" if manual else "idle")
            status = {"state": state, "t": round(time.time(), 3), "host": socket.gethostname(),
                      "code": self.code, "agent_started": round(self.started, 3),
                      "job": {k: v for k, v in job.items() if k != "log"} if job else None,
                      "manual": manual, "last": self.last}
        self.client.publish(mqtt_link.topic("agent/status"), json.dumps(status), qos=1, retain=True)

    # ---- what's running ----
    def manual_script(self):
        """A rig script holding the hardware lock that this agent didn't start, if any."""
        try:
            holder = (self.data_dir / ".hardware.lock").read_text().strip()
        except OSError:
            return None
        m = LOCK_LINE.match(holder)
        if not m:
            return None
        pid = int(m.group(1))
        if self.job and self.job.get("pid") == pid:
            return None
        command = rig_process(pid)
        return {"pid": pid, "command": command} if command else None

    def adopt(self):
        """Pick up a session started by an earlier run of the agent, if it's still going."""
        try:
            job = json.loads(self.job_file.read_text())
        except (OSError, ValueError):
            return
        if rig_process(job.get("pid", 0)):
            job["adopted"] = True
            self.job = job
            print(f"Picked up the running session (pid {job['pid']}, plan {job['plan']}).")
            threading.Thread(target=self.follow, args=(job, None), daemon=True).start()
        else:
            self.job_file.unlink(missing_ok=True)
            self.last = {k: job.get(k) for k in ("id", "plan", "session", "started")}
            self.last.update(ended=None, result="ended while the agent was off", exit_code=None,
                             message=self.result_from_log(job))

    @staticmethod
    def result_from_log(job):
        try:
            lines = Path(job["log"]).read_text(errors="replace").splitlines()
        except (OSError, KeyError):
            return None
        return next((line for line in reversed(lines) if line.strip()), None)

    def save_job(self):
        tmp = self.job_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.job))
        tmp.replace(self.job_file)

    # ---- commands ----
    def status_cmd(self, cmd_id, cmd):
        self.publish_status()
        self.reply(cmd_id, True)

    def start(self, cmd_id, cmd):
        with self.lock:
            if self.job:
                raise plans.OptionError(f"A session is already running ({self.job['plan']}). Stop it first.")
            manual = self.manual_script()
            if manual:
                raise plans.OptionError(f"A script started on the Pi is running ({manual['command']}, "
                                        f"pid {manual['pid']}). Stop it first.")
            argv = plans.argv_from_options(cmd.get("plan"), cmd.get("options") or {})
            a = plans.parse_args(argv, raise_errors=True)
            used = plans.used_seeds(str(self.data_dir))
            if a.seed is None:
                rng = random.SystemRandom()
                a.seed = rng.randrange(1_000_000)
                while a.seed in used:
                    a.seed = rng.randrange(1_000_000)
                argv += ["--seed", str(a.seed)]
            elif a.seed in used and not a.only:
                raise plans.OptionError(f"Seed {a.seed} was already used by session {used[a.seed]}. Leave the "
                                        f"seed empty for a new one; reuse it only with 'only' to finish that session.")
            a.pollution_order = plans.pollution_order(a)
            rows = plans.build_plan(a)
            if not rows:
                raise plans.OptionError("Nothing to run: 'only' and 'skip' leave no runs.")
            if self.data_dir_given:
                argv += ["--data-dir", str(self.data_dir)]

            job_id = cmd_id or uuid.uuid4().hex[:12]
            log = self.agent_dir / f"{time.strftime('%Y%m%d-%H%M%S')}_{re.sub(r'[^A-Za-z0-9-]', '', job_id)[:24]}.log"
            with open(log, "w") as out:
                proc = subprocess.Popen([sys.executable, "-u", str(HERE / "session.py"), *argv], cwd=HERE,
                                        stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                        start_new_session=True)
            self.job = {"id": job_id, "state": "running", "plan": a.plan, "options": cmd.get("options") or {},
                        "argv": ["session.py", *argv], "seed": a.seed, "pollution_order": a.pollution_order,
                        "pid": proc.pid, "started": round(time.time(), 3), "log": str(log),
                        "rows": [{"number": r["number"], "name": r["name"], "minutes": round(r["minutes"], 3)}
                                 for r in rows],
                        "total_minutes": round(sum(r["minutes"] for r in rows), 3),
                        "session": None, "position": 0, "total": len(rows), "run": None, "run_started": None,
                        "result": None}
            self.save_job()
            job = self.job
        print(f"Started session.py {' '.join(argv)} (pid {proc.pid}); output in {log}")
        threading.Thread(target=self.follow, args=(job, proc), daemon=True).start()
        self.reply(cmd_id, True, job={"id": job_id, "seed": a.seed})
        self.publish_status()

    def stop(self, cmd_id, cmd):
        with self.lock:
            if self.job:
                pid, what = self.job["pid"], f"the {self.job['plan']} session"
                self.job["state"] = "stopping"
                self.save_job()
            else:
                manual = self.manual_script()
                if not manual:
                    raise plans.OptionError("Nothing is running on the Pi.")
                pid, what = manual["pid"], manual["command"]
        try:
            os.kill(pid, signal.SIGINT)  # what Ctrl+C does: the run ends cleanly, LEDs off
        except ProcessLookupError:
            pass
        print(f"Stopping {what} (pid {pid}).")
        threading.Thread(target=self.escalate, args=(pid,), daemon=True).start()
        self.reply(cmd_id, True)
        self.publish_status()

    def escalate(self, pid):
        began = time.monotonic()
        for delay, sig in STOP_ESCALATION:
            while time.monotonic() - began < delay:
                if not rig_process(pid):
                    return
                time.sleep(1)
            if rig_process(pid):
                print(f"pid {pid} is still running; sending {sig.name}.")
                os.kill(pid, sig)
                if sig == signal.SIGKILL:
                    time.sleep(1)
                    leds_off()

    def backup(self, cmd_id, cmd):
        session = str(cmd.get("session") or "")
        if not SESSION_ID.match(session):
            raise plans.OptionError(f"'{session}' isn't a session id like 20261102-143000.")
        files = sorted(self.data_dir.glob(f"{session}_*.jsonl"))
        if not files:
            raise plans.OptionError(f"There are no backup files for session {session} on the Pi.")
        for path in files:
            lines = [line for line in path.read_text(errors="replace").splitlines() if line.strip()]
            parts = max(1, -(-len(lines) // BACKUP_CHUNK))
            for part in range(parts):
                chunk = lines[part * BACKUP_CHUNK:(part + 1) * BACKUP_CHUNK]
                self.client.publish(mqtt_link.topic("agent/backup"), json.dumps(
                    {"id": cmd_id, "session": session, "run": path.stem, "part": part + 1, "parts": parts,
                     "lines": chunk}), qos=1)
        self.reply(cmd_id, True, runs=len(files))

    # ---- following session.py's output ----
    def follow(self, job, proc):
        """Read session.py's log as it's written: publish each line and keep track of progress."""
        replay = bool(job.get("adopted"))  # lines from before this agent started: parse, don't resend
        buffer = ""
        with open(job["log"], errors="replace") as f:
            while True:
                chunk = f.read()
                if chunk:
                    buffer += chunk
                    *lines, buffer = buffer.split("\n")
                    for line in lines:
                        self.on_line(job, line, publish=not replay)
                    continue
                replay = False
                running = proc.poll() is None if proc else bool(rig_process(job["pid"]))
                if not running:
                    rest = f.read()
                    for line in (buffer + rest).split("\n"):
                        if line:
                            self.on_line(job, line)
                    break
                self.halt.wait(0.25)
        exit_code = proc.wait() if proc else None
        self.finish(job, exit_code)

    def on_line(self, job, line, publish=True):
        changed = False
        with self.lock:
            if m := RUN_LINE.match(line):
                if publish or job.get("run") != m.group(3):  # replaying: keep the saved start time
                    job["run_started"] = round(time.time(), 3)
                job.update(position=int(m.group(1)), total=int(m.group(2)), run=m.group(3), session=m.group(4))
                changed = True
            elif m := END_LINE.match(line):
                job.update(session=m.group(1), result=m.group(2))
                changed = True
            if changed and self.job is job:
                self.save_job()
        if publish:
            self.client.publish(mqtt_link.topic("agent/log"), json.dumps(
                {"job": job["id"], "session": job.get("session"), "t": round(time.time(), 3), "line": line}), qos=1)
        if changed and publish:
            self.publish_status()

    def finish(self, job, exit_code):
        with self.lock:
            result = job.get("result")
            if not result:
                result = "failed" if exit_code not in (0, None) else "ended"
            message = None
            if result == "failed":
                message = self.result_from_log(job)
            self.last = {"id": job["id"], "plan": job["plan"], "session": job.get("session"),
                         "started": job["started"], "ended": round(time.time(), 3), "result": result,
                         "exit_code": exit_code, "message": message}
            if self.job is job:
                self.job = None
                self.job_file.unlink(missing_ok=True)
        print(f"session.py ended: {result}" + (f" (exit code {exit_code})" if exit_code else ""))
        self.publish_status()

    # ---- main loop ----
    def run(self):
        self.client.loop_start()
        try:
            while not self.halt.is_set():
                self.wake.wait(HEARTBEAT)
                self.wake.clear()
                if not self.halt.is_set():
                    self.publish_status()
        finally:
            info = self.client.publish(mqtt_link.topic("agent/status"), json.dumps(
                {"state": "offline", "t": round(time.time(), 3), "job": None, "last": self.last}), qos=1, retain=True)
            try:
                info.wait_for_publish(timeout=3)
            except (RuntimeError, ValueError):
                pass
            self.client.disconnect()
            self.client.loop_stop()


def main():
    mqtt_link.load_env_file()
    broker = os.environ.get("RIG_BROKER")
    if not broker:
        sys.exit("Set RIG_BROKER (and RIG_MQTT_USERNAME / RIG_MQTT_PASSWORD) in ~/.config/light-polluter/rig.env.")
    data_dir = os.environ.get("RIG_DATA_DIR")
    if os.environ.get("RIG_MOCK"):
        # The simulated rig (mock_rig/): session.py runs as usual, against simulated hardware
        import mock_rig
        os.environ.update(mock_rig.prepare(Path(data_dir or Path.home() / "rig-data") / "mock-sysfs"))
        print("Mock rig: simulated sensor and LEDs; topics under " + mqtt_link.topic(""))
    agent = Agent(broker, data_dir or Path.home() / "rig-data", data_dir_given=bool(data_dir))

    def quit_(*_):
        agent.halt.set()
        agent.wake.set()
    signal.signal(signal.SIGINT, quit_)
    signal.signal(signal.SIGTERM, quit_)
    print(f"Pi agent for {mqtt_link.describe(broker)}; code {agent.code or 'unknown'}.")
    if agent.job:
        print("A session it started earlier is still running; it keeps going.")
    agent.run()
    print("Agent stopped." + (" The running session keeps going." if agent.job else ""))


if __name__ == "__main__":
    main()
