#!/bin/sh
# Light sensor rig: get the pollution LED's hardware PWM ready (Raspberry Pi 5).
#
# Needed only if session.py or rig.py says it has no permission to use hardware PWM.
# Run it after each boot, before the scripts:
#     sudo sh setup_pwm.sh
#
# It switches on PWM channel 0 (GPIO 12, physical pin 32) and lets the gpio group,
# which your user is in, use it without sudo.
set -e
CHANNEL=0
CHIP=""
for c in /sys/class/pwm/pwmchip*; do
    case "$(readlink -f "$c")" in
        *98000.pwm*) CHIP="$c" ;;
    esac
done
if [ -z "$CHIP" ]; then
    echo "Hardware PWM for GPIO 12 isn't enabled. Add this line to /boot/firmware/config.txt and reboot:"
    echo "    dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4"
    echo "PWM controllers found:"
    ls -l /sys/class/pwm/ 2>/dev/null || echo "    none"
    exit 1
fi
[ -d "$CHIP/pwm$CHANNEL" ] || echo "$CHANNEL" > "$CHIP/export"
sleep 1
for f in period duty_cycle enable polarity; do
    chgrp gpio "$CHIP/pwm$CHANNEL/$f"
    chmod g+rw "$CHIP/pwm$CHANNEL/$f"
done
echo "Hardware PWM ready: $CHIP/pwm$CHANNEL (GPIO 12, physical pin 32)"
pinctrl get 12 2>/dev/null || true
