#!/bin/sh
# Light sensor rig: cloud MQTT broker.
# Writes Mosquitto's config, users and access rules from the service's variables, then starts
# Mosquitto with MQTT over WebSockets on $PORT. Variables:
#   MQTT_PI_PASSWORD      password for user "pi"     (the Pi: sends data and agent status, takes commands)
#   MQTT_APP_PASSWORD     password for user "app"    (the web app: reads everything, sends commands)
#   MQTT_LAPTOP_PASSWORD  optional, user "laptop"    (logger.py on a laptop: reads the data only)
#   MQTT_MOCK_PASSWORD    optional, user "mock"      (the web app's simulated rig, on mock/... topics only)
#   PORT                  WebSockets port (default 8080)
set -e
: "${MQTT_PI_PASSWORD:?Set MQTT_PI_PASSWORD}"
: "${MQTT_APP_PASSWORD:?Set MQTT_APP_PASSWORD}"
CONF=${MOSQUITTO_CONF_DIR:-/mosquitto/config}
PORT=${PORT:-8080}
mkdir -p "$CONF"

cat > "$CONF/mosquitto.conf" <<CONFIG
persistence false
log_dest stdout
log_type error
log_type warning
log_type notice
connection_messages true
allow_anonymous false
# The web app keeps a persistent session, so the broker holds its messages while it restarts
# (a deploy, say). 100000 messages is more than a day of readings.
max_queued_messages 100000
persistent_client_expiration 7d
password_file $CONF/passwd
acl_file $CONF/acl
listener $PORT
protocol websockets
CONFIG

mosquitto_passwd -c -b "$CONF/passwd" pi "$MQTT_PI_PASSWORD"
mosquitto_passwd -b "$CONF/passwd" app "$MQTT_APP_PASSWORD"
if [ -n "$MQTT_LAPTOP_PASSWORD" ]; then
    mosquitto_passwd -b "$CONF/passwd" laptop "$MQTT_LAPTOP_PASSWORD"
fi
if [ -n "$MQTT_MOCK_PASSWORD" ]; then
    mosquitto_passwd -b "$CONF/passwd" mock "$MQTT_MOCK_PASSWORD"
fi

cat > "$CONF/acl" <<RULES
user pi
topic write rig/sensor
topic write rig/led
topic write rig/meta
topic write rig/agent/#
topic read rig/cmd

user app
topic read rig/#
topic write rig/cmd
topic read mock/#
topic write mock/cmd

user laptop
topic read rig/sensor
topic read rig/led
topic read rig/meta

user mock
topic write mock/sensor
topic write mock/led
topic write mock/meta
topic write mock/agent/#
topic read mock/cmd
RULES

if id mosquitto >/dev/null 2>&1; then
    chown mosquitto:mosquitto "$CONF/passwd" "$CONF/acl" "$CONF/mosquitto.conf"
fi
chmod 0600 "$CONF/passwd" "$CONF/acl"
echo "Broker: MQTT over WebSockets on port $PORT, users: pi, app$( [ -n "$MQTT_LAPTOP_PASSWORD" ] && echo ', laptop')$( [ -n "$MQTT_MOCK_PASSWORD" ] && echo ', mock')"
exec mosquitto -c "$CONF/mosquitto.conf"
