"""
The simulated rig: stand-ins for the Pi's hardware libraries, so pi_agent.py, session.py
and rig.py run unchanged anywhere, for trying the web app without the Pi.

    mock_rig/hardware/adafruit_tsl2591.py   a light sensor that sees the two LEDs
    mock_rig/hardware/gpiozero.py           LEDs, with the Pi 5's whole-percent PWM duty
    mock_rig/hardware/board.py              I2C
    prepare(folder)                         a fake /sys/class/pwm, so the pollution LED's
                                            hardware PWM code runs too

The simulated sensor: about 6 counts dark, about 566 more with the test LED at full
brightness (starting about 3 % brighter and settling over 5 minutes, like the real LED),
up to 20 more from the pollution LED, and about half a count of noise.

pi_agent.py uses this when RIG_MOCK=1; the web app runs it that way for its mock rig.
"""

import os
from pathlib import Path

HARDWARE = Path(__file__).resolve().parent / "hardware"
DEVICE = "devices/platform/axi/1000120000.pcie/1f00098000.pwm/pwm/pwmchip0"


def prepare(folder):
    """Make a fake sysfs PWM controller in folder and return the environment for the scripts."""
    root = Path(folder)
    chip = root / DEVICE
    (chip / "pwm0").mkdir(parents=True, exist_ok=True)
    (chip / "export").write_text("")
    for name, value in (("period", "0"), ("duty_cycle", "0"), ("enable", "0"), ("polarity", "normal")):
        (chip / "pwm0" / name).write_text(value + "\n")
    link = root / "class" / "pwm" / "pwmchip0"
    link.parent.mkdir(parents=True, exist_ok=True)
    if not link.is_symlink():
        link.symlink_to(chip)
    path = os.pathsep.join(p for p in (str(HARDWARE), os.environ.get("PYTHONPATH", "")) if p)
    return {"PYTHONPATH": path, "RIG_PWM_SYSFS": str(root / "class" / "pwm")}
