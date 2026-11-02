"""Simulated TSL2591 light sensor facing the test LED, with the pollution LED off to the side.
The pollution LED's level comes from the fake sysfs hardware PWM (or the mock gpiozero)."""
import math
import os
import random
import time
from pathlib import Path

import _mockstate

GAIN_LOW, GAIN_MED, GAIN_HIGH, GAIN_MAX = 0x00, 0x10, 0x20, 0x30
INTEGRATIONTIME_100MS, INTEGRATIONTIME_200MS, INTEGRATIONTIME_300MS = 0x00, 0x01, 0x02
INTEGRATIONTIME_400MS, INTEGRATIONTIME_500MS, INTEGRATIONTIME_600MS = 0x03, 0x04, 0x05
GAIN_FACTOR = {GAIN_LOW: 1, GAIN_MED: 25, GAIN_HIGH: 428, GAIN_MAX: 9876}

DARK = 6.0          # counts with both LEDs off
TEST_FULL = 560.0   # extra counts with the test LED at full brightness, once settled
POLLUTION = 20.0    # extra counts with the pollution LED at full duty
WARM_START = 0.03   # the test LED starts this much brighter ...
WARM_TIME = 90.0    # ... and settles with this time constant (seconds), about 5 minutes in all
NOISE = 0.5         # counts


def _hardware_pollution():
    pwm = Path(os.environ.get("RIG_PWM_SYSFS", "/nonexistent")) / "pwmchip0" / "pwm0"
    try:
        if (pwm / "enable").read_text().strip() == "1":
            period = int((pwm / "period").read_text())
            return int((pwm / "duty_cycle").read_text()) / period if period else 0.0
    except (OSError, ValueError):
        pass
    return 0.0


class TSL2591:
    def __init__(self, i2c):
        self.gain = GAIN_MED
        self.integration_time = INTEGRATIONTIME_100MS

    @property
    def raw_luminosity(self):
        test = _mockstate.levels["test"]
        pollution = max(_hardware_pollution(), _mockstate.levels["pollution"])
        warm = 1.0
        if _mockstate.test_on_since is not None:
            warm += WARM_START * math.exp(-(time.monotonic() - _mockstate.test_on_since) / WARM_TIME)
        scale = GAIN_FACTOR.get(self.gain, 1) * (self.integration_time + 1)
        light = (DARK + TEST_FULL * test * warm + POLLUTION * pollution) * scale
        full = min(65535, max(0, round(light + random.gauss(0, NOISE * math.sqrt(scale)))))
        return full, round(full * 0.1)

    @property
    def full_spectrum(self):
        return self.raw_luminosity[0]

    @property
    def infrared(self):
        return self.raw_luminosity[1]
