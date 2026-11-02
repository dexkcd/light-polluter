"""
Light sensor rig: how every script reaches the MQTT broker.

The broker is given as an address, with or without a scheme:

    192.168.50.157              plain MQTT on port 1883 (the laptop broker)
    mqtt://host:1883            the same
    mqtts://host                MQTT over TLS, port 8883
    ws://host:8080/             MQTT over WebSockets
    wss://broker.example.com    MQTT over secure WebSockets, port 443 (the cloud broker)

The username and password come from the environment, never the command line, so
they don't end up in the run's meta file or in `ps`:

    RIG_BROKER          the broker address, used when --broker isn't given
    RIG_MQTT_USERNAME   broker username (optional for the laptop broker)
    RIG_MQTT_PASSWORD   broker password
    RIG_MQTT_CA         a CA certificate file for TLS (only needed for self-signed certificates)

The scripts also read these from ~/.config/light-polluter/rig.env (KEY=value lines),
or from the file named by RIG_ENV_FILE. Values already in the environment win.
"""

import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

ENV_FILE = Path.home() / ".config" / "light-polluter" / "rig.env"
DEFAULT_PORTS = {"mqtt": 1883, "mqtts": 8883, "ws": 80, "wss": 443}


def topic(name):
    """The full topic for a stream, e.g. rig/sensor. RIG_TOPIC_PREFIX=mock moves everything to
    mock/..., which is what the web app's simulated rig uses, so it never mixes with the real one."""
    return f"{os.environ.get('RIG_TOPIC_PREFIX') or 'rig'}/{name}"


def run_labels():
    """Extra fields for a run's start line: marks runs made by the simulated rig."""
    return {"mock": True} if os.environ.get("RIG_MOCK") else {}


def load_env_file(path=None):
    """Fill in missing environment variables from the rig's env file, if there is one."""
    path = Path(path or os.environ.get("RIG_ENV_FILE") or ENV_FILE)
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key.startswith("export "):
            key = key[len("export "):].strip()
        os.environ.setdefault(key, value)


def parse_broker(broker, port=None):
    """Split a broker address into host, port, transport (tcp or websockets), TLS and path."""
    if not broker:
        raise ValueError("no broker address")
    url = urlsplit(broker if "://" in broker else f"mqtt://{broker}")
    scheme = url.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise ValueError(f"unknown broker scheme '{scheme}' (use mqtt, mqtts, ws or wss)")
    return SimpleNamespace(
        host=url.hostname,
        port=port or url.port or DEFAULT_PORTS[scheme],
        transport="websockets" if scheme in ("ws", "wss") else "tcp",
        tls=scheme in ("mqtts", "wss"),
        path=url.path or "/",
    )


def make_client(client_id, broker, port=None, username=None, password=None, **client_options):
    """A paho MQTT client set up for the broker, with credentials from the environment
    (or given, for the web app). Extra options, such as clean_session, go to paho's Client.
    Returns (client, settings); connect with client.connect_async(settings.host, settings.port)."""
    import paho.mqtt.client as mqtt

    settings = parse_broker(broker, port)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, transport=settings.transport,
                         **client_options)
    if settings.transport == "websockets":
        client.ws_set_options(path=settings.path)
    if settings.tls:
        client.tls_set(ca_certs=os.environ.get("RIG_MQTT_CA") or None)
    username = username or os.environ.get("RIG_MQTT_USERNAME")
    if username:
        client.username_pw_set(username, password if password is not None else os.environ.get("RIG_MQTT_PASSWORD"))
    return client, settings


def describe(broker, port=None):
    """The broker address as a short readable string, without credentials."""
    s = parse_broker(broker, port)
    scheme = {("tcp", False): "mqtt", ("tcp", True): "mqtts",
              ("websockets", False): "ws", ("websockets", True): "wss"}[(s.transport, s.tls)]
    return f"{scheme}://{s.host}:{s.port}" + (s.path if s.transport == "websockets" else "")
