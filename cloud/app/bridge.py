"""
The web app's connection to the broker. It runs in its own thread:

  - stores every reading, LED event and meta line from the Pi (rig/sensor, rig/led, rig/meta)
  - keeps the latest Pi agent status, recent readings and console lines for the live page
  - sends commands to the Pi agent (rig/cmd) and hands back its replies
  - fills gaps from the Pi's backup files when they arrive (rig/agent/backup)

It connects with a persistent session, so while the app restarts the broker keeps the
Pi's messages for it and delivers them when it's back.
"""

import asyncio
import json
import time
import uuid
from collections import deque

import mqtt_link

DATA_STREAMS = ("sensor", "led", "meta")
LIVE_SECONDS = 15 * 60       # how much recent data the live page gets when it opens
STALE_STATUS = 90            # seconds without word from the agent before it counts as offline


class Bridge:
    def __init__(self, store, broker, username, password, client_id="light-rig-app", prefix="rig"):
        self.store = store
        self.prefix = prefix
        self.broker = broker
        self.loop = None
        self.subscribers = set()
        self.connected = False
        self.error = None
        self.status = {"state": "unknown"}
        self.status_received = None
        self.sensor = deque(maxlen=LIVE_SECONDS * 2)
        self.led = deque(maxlen=4000)
        self.levels = {"test": None, "pollution": None}
        self.run = None            # meta start of the run in progress
        self.last_end = None
        self.logs = deque(maxlen=400)
        self.waiters = {}
        self.backups = {}          # (command id, run) -> {part: lines}
        self.backup_results = {}   # session -> {"t", "runs": {run: added}}

        self.client, self.settings = mqtt_link.make_client(client_id, broker, username=username, password=password,
                                                           clean_session=False)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=20)

    def start(self, loop):
        self.loop = loop
        self.client.connect_async(self.settings.host, self.settings.port, keepalive=30)
        self.client.loop_start()

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()

    # ---- broker callbacks (MQTT thread) ----
    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            self.error = f"The broker refused the app's login ({reason_code}). Check MQTT_APP_PASSWORD."
            print(self.error)
            return
        self.connected, self.error = True, None
        client.subscribe([(f"{self.prefix}/{stream}", 1) for stream in DATA_STREAMS] + [(f"{self.prefix}/agent/#", 1)])
        print(f"Connected to the broker at {mqtt_link.describe(self.broker)} for {self.prefix}/...")
        self.emit({"type": "broker", "connected": True})

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        self.emit({"type": "broker", "connected": False})

    def on_message(self, client, userdata, message):
        received = time.time()
        try:
            payload = json.loads(message.payload)
        except ValueError:
            print(f"Skipped a message on {message.topic} I couldn't read")
            return
        name = message.topic[len(self.prefix) + 1:]
        try:
            if name in DATA_STREAMS:
                self.on_data(payload, received)
            elif name == "agent/status":
                self.status, self.status_received = payload, received
                self.emit({"type": "status", "status": self.public_status()})
            elif name == "agent/log":
                self.on_log(payload)
            elif name == "agent/reply":
                self.resolve(payload)
            elif name == "agent/backup":
                self.on_backup(payload)
        except Exception as e:  # keep the connection alive whatever arrives
            print(f"Couldn't handle a message on {message.topic}: {e!r}")

    def on_data(self, msg, received):
        self.store.write(msg, received)
        stream = msg.get("stream")
        if stream == "sensor" and msg.get("ch0_full") is not None:
            point = {"t": msg["t"], "full": msg["ch0_full"], "ir": msg.get("ch1_ir"), "run": msg["run"],
                     "saturated": msg.get("saturated")}
            self.sensor.append(point)
            self.emit({"type": "sensor", **point})
        elif stream == "led":
            self.levels[msg.get("led")] = msg.get("level")
            event = {"t": msg["t"], "led": msg.get("led"), "level": msg.get("level"), "note": msg.get("note"),
                     "run": msg["run"]}
            self.led.append(event)
            self.emit({"type": "led", **event})
        elif stream == "meta":
            if msg.get("event") == "start":
                self.run = {k: msg.get(k) for k in ("run", "test", "plan", "session", "run_index", "position",
                                                     "purpose", "minutes", "pollution_level", "pollution_duty", "t")}
            elif msg.get("event") == "end":
                self.last_end = {"run": msg["run"], "status": msg.get("status"), "t": msg.get("t")}
                if self.run and self.run["run"] == msg["run"]:
                    self.run = None
            self.emit({"type": "run", "run": self.run, "last_end": self.last_end})

    def on_log(self, msg):
        line = str(msg.get("line", ""))[:2000]
        entry = {"t": msg.get("t"), "job": msg.get("job"), "session": msg.get("session"), "line": line}
        self.logs.append(entry)
        self.store.add_log(msg.get("job"), msg.get("session"), line)
        self.emit({"type": "log", **entry})

    def on_backup(self, msg):
        key = (msg.get("id"), msg.get("run"))
        parts = self.backups.setdefault(key, {})
        parts[msg.get("part")] = msg.get("lines") or []
        if len(parts) < (msg.get("parts") or 1):
            return
        del self.backups[key]
        lines = [line for part in sorted(parts) for line in parts[part]]
        added = self.store.merge_backup(msg["run"], lines)
        result = self.backup_results.setdefault(msg.get("session"), {"t": time.time(), "runs": {}})
        result["t"] = time.time()
        result["runs"][msg["run"]] = added
        print(f"Backup of {msg['run']}: added {added or 'nothing'}")
        self.emit({"type": "backup", "session": msg.get("session"), "run": msg["run"], "added": added})

    # ---- state for the pages ----
    def public_status(self):
        status = dict(self.status)
        age = time.time() - (status.get("t") or 0)
        if status.get("state") not in ("offline", "unknown") and age > STALE_STATUS:
            status["state"] = "offline"
            status["stale"] = True
        status["broker_connected"] = self.connected
        status["broker_error"] = self.error
        return status

    def snapshot(self):
        since = time.time() - LIVE_SECONDS
        return {"type": "snapshot", "status": self.public_status(), "broker": self.connected,
                "sensor": [p for p in list(self.sensor) if p["t"] >= since],
                "led": [e for e in list(self.led) if e["t"] >= since], "levels": dict(self.levels),
                "run": self.run, "last_end": self.last_end, "logs": list(self.logs)[-200:], "now": time.time()}

    # ---- live updates to the browsers (event loop) ----
    def emit(self, event):
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._fanout, event)

    def _fanout(self, event):
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self.subscribers.discard(queue)  # a stuck browser; it reconnects and gets a snapshot

    def subscribe(self):
        queue = asyncio.Queue(maxsize=2000)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue):
        self.subscribers.discard(queue)

    # ---- commands to the Pi agent ----
    def resolve(self, reply):
        future = self.waiters.pop(reply.get("id"), None)
        if future is not None and self.loop is not None:
            self.loop.call_soon_threadsafe(lambda: future.done() or future.set_result(reply))

    async def command(self, action, timeout=10, **fields):
        if not self.connected:
            return {"ok": False, "error": self.error or "The app isn't connected to the broker right now. Try again in a moment."}
        if self.public_status().get("state") in ("offline", "unknown"):
            return {"ok": False, "error": "The Pi is offline. Check that it's on and connected, "
                                          "and that the agent is running (journalctl -u rig-agent)."}
        cmd_id = uuid.uuid4().hex[:12]
        future = asyncio.get_running_loop().create_future()
        self.waiters[cmd_id] = future
        self.client.publish(f"{self.prefix}/cmd", json.dumps({"id": cmd_id, "t": time.time(), "action": action, **fields}), qos=1)
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self.waiters.pop(cmd_id, None)
            return {"ok": False, "error": f"The Pi didn't answer within {timeout} s. It may have just gone offline."}
