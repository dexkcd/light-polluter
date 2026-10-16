#!/usr/bin/env python3
"""
Light sensor rig: laptop side.

Listens to the Mosquitto broker on this laptop and writes every message from the
Pi to files, one set per run:

    data/<run>_sensor.csv   every sensor reading
    data/<run>_led.csv      every LED change ("brightness changed to X")
    data/<run>_meta.jsonl   start/end of the run and the settings used

Nothing is analysed here. The t and mono timestamps come from the Pi and are the
ones to analyse with. received_t is only when the laptop got the message.

Run it before starting rig.py on the Pi:
    python logger.py
"""

import argparse
import csv
import json
import time
from pathlib import Path

import paho.mqtt.client as mqtt

FIELDS = {
    "sensor": ["seq", "t", "mono", "ch0_full", "ch1_ir", "saturated", "gain",
               "integration_ms", "error", "received_t"],
    "led": ["seq", "t", "mono", "led", "level", "note", "received_t"],
}


class RunFiles:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.open = {}       # (run, stream) -> (file, csv writer)
        self.last_seq = {}   # (run, stream) -> last seq number written
        self.counts = {}     # run -> {"sensor": n, "led": n}

    def write(self, msg, received_t):
        run, stream, seq = msg["run"], msg["stream"], msg["seq"]

        if stream == "meta":
            with open(self.folder / f"{run}_meta.jsonl", "a") as f:
                f.write(json.dumps({**msg, "received_t": round(received_t, 6)}) + "\n")
            if msg.get("event") == "start":
                print(f"\nRun {run} started ({msg.get('test')} test)")
            elif msg.get("event") == "end":
                c = self.counts.get(run, {})
                print(f"Run {run} {msg.get('status')}: {c.get('sensor', 0)} readings, "
                      f"{c.get('led', 0)} LED events saved in {self.folder}/")
                self.close_run(run)
            return
        if stream not in FIELDS:
            return

        key = (run, stream)
        prev = self.last_seq.get(key)
        if prev is not None and seq <= prev:
            return  # duplicate redelivery, already written
        if prev is not None and seq != prev + 1:
            print(f"Warning: {stream} messages {prev + 1}-{seq - 1} of run {run} didn't arrive. "
                  f"They're still in the backup file on the Pi.")
        self.last_seq[key] = seq

        if key not in self.open:
            path = self.folder / f"{run}_{stream}.csv"
            is_new = not path.exists()
            f = open(path, "a", newline="")
            writer = csv.DictWriter(f, fieldnames=FIELDS[stream], extrasaction="ignore")
            if is_new:
                writer.writeheader()
            self.open[key] = (f, writer)
        f, writer = self.open[key]
        writer.writerow({**msg, "received_t": round(received_t, 6)})
        f.flush()  # on disk straight away, in case anything crashes

        c = self.counts.setdefault(run, {"sensor": 0, "led": 0})
        c[stream] += 1
        if stream == "sensor" and c["sensor"] % 20 == 0:
            print(f"  {c['sensor']} readings, {c['led']} LED events, last full={msg.get('ch0_full')}")

    def close_run(self, run):
        for key in [k for k in self.open if k[0] == run]:
            self.open.pop(key)[0].close()

    def close_all(self):
        for f, _ in self.open.values():
            f.close()
        self.open.clear()


def main():
    p = argparse.ArgumentParser(description="Light sensor rig: laptop logger")
    p.add_argument("--broker", default="localhost", help="broker address (default: this laptop)")
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--data-dir", default="data", help="folder for the CSV files (default: data)")
    a = p.parse_args()

    files = RunFiles(a.data_dir)

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            print(f"Broker refused the connection: {reason_code}")
            return
        client.subscribe("rig/#", qos=1)
        print(f"Listening on {a.broker}:{a.port}. Saving to {Path(a.data_dir).resolve()}")
        print("Waiting for the Pi... (Ctrl+C to quit)")

    def on_message(client, userdata, message):
        received_t = time.time()
        try:
            msg = json.loads(message.payload)
            files.write(msg, received_t)
        except (ValueError, KeyError) as e:
            print(f"Skipped a message on {message.topic} I couldn't read: {e}")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="light-rig-logger")
    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(a.broker, a.port, keepalive=30)
    except OSError as e:
        raise SystemExit(f"Can't reach the broker at {a.broker}:{a.port} ({e}). "
                         f"Start Mosquitto first: mosquitto -c mosquitto.conf -v")
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        pass
    finally:
        files.close_all()
        client.disconnect()
        print("\nStopped. Files are closed.")


if __name__ == "__main__":
    main()
