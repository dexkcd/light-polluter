#!/bin/sh
# Light sensor rig: install the Pi agent, so the web app can start and stop sessions.
#
# Run it on the Pi, from the light-polluter folder, as your normal user (it asks for sudo):
#     sh pi/install_agent.sh
#
# It installs two services:
#   rig-pwm    runs setup_pwm.sh at boot, so the pollution LED's hardware PWM is ready
#   rig-agent  runs pi_agent.py, starts it at boot and restarts it if it stops
#
# The broker address and the pi user's password go in ~/.config/light-polluter/rig.env
# (the first run makes it from pi/rig.env.example and asks you to fill it in).
# After updating the code (git pull), run this again or: sudo systemctl restart rig-agent
# A running session keeps going while the agent restarts.
set -e
REPO=$(cd "$(dirname "$0")/.." && pwd)
RIG_USER=$(id -un)
PYTHON=${RIG_PYTHON:-$HOME/rig-venv/bin/python}
ENV_FILE=$HOME/.config/light-polluter/rig.env

if [ ! -x "$PYTHON" ]; then
    echo "No Python at $PYTHON. Make the venv first (README, 'Pi (once)'), or set RIG_PYTHON."
    exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
    mkdir -p "$(dirname "$ENV_FILE")"
    cp "$REPO/pi/rig.env.example" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "Made $ENV_FILE. Fill in RIG_BROKER and RIG_MQTT_PASSWORD (nano $ENV_FILE), then run this again."
    exit 1
fi
chmod 600 "$ENV_FILE"
if ! grep -q '^RIG_MQTT_PASSWORD=..*' "$ENV_FILE"; then
    echo "Set RIG_MQTT_PASSWORD in $ENV_FILE first (nano $ENV_FILE)."
    exit 1
fi
if ! "$PYTHON" -c "import paho.mqtt" 2>/dev/null; then
    echo "paho-mqtt is missing. Install it with: $PYTHON -m pip install paho-mqtt"
    exit 1
fi

sudo tee /etc/systemd/system/rig-pwm.service >/dev/null <<UNIT
[Unit]
Description=Light sensor rig: hardware PWM for the pollution LED

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh $REPO/setup_pwm.sh

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/rig-agent.service >/dev/null <<UNIT
[Unit]
Description=Light sensor rig: Pi agent for the web app
Wants=network-online.target rig-pwm.service
After=network-online.target rig-pwm.service

[Service]
User=$RIG_USER
WorkingDirectory=$REPO
ExecStart=$PYTHON -u $REPO/pi_agent.py
Restart=always
RestartSec=5
# Stopping or restarting the agent leaves a running session alone; the next agent picks it up
KillMode=process

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
if ! sudo systemctl enable --now rig-pwm.service; then
    echo "rig-pwm didn't start, so hardware PWM isn't set up yet (README, 'Hardware PWM for the pollution LED')."
    echo "The agent still runs; sessions with --pollution-pwm software or none work without it."
fi
sudo systemctl enable rig-agent.service
sudo systemctl restart rig-agent.service
sleep 3
systemctl --no-pager --lines=8 status rig-agent.service || true
echo
echo "Installed. The web app should show the Pi as online. Agent log: journalctl -u rig-agent -f"
