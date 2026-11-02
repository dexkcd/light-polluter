"""Simulated gpiozero: behaves like the real one on a Pi 5, with PWM duty in whole percent."""
import _mockstate

TEST_LED_PIN = 18


def _name(pin):
    return "test" if pin == TEST_LED_PIN else "pollution"


class PWMLED:
    def __init__(self, pin, frequency=100, **kwargs):
        self.name, self._duty = _name(pin), 0

    @property
    def value(self):
        return self._duty / 100

    @value.setter
    def value(self, v):
        if not 0 <= v <= 1:
            raise ValueError("PWM value must be between 0 and 1")
        self._duty = int(v * 100)
        _mockstate.set_level(self.name, self._duty / 100)

    def on(self):
        self.value = 1.0

    def off(self):
        self.value = 0.0

    def close(self):
        self.off()


class LED:
    def __init__(self, pin, **kwargs):
        self.name = _name(pin)

    def on(self):
        _mockstate.set_level(self.name, 1.0)

    def off(self):
        _mockstate.set_level(self.name, 0.0)

    def close(self):
        self.off()
