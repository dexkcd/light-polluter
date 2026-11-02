"""What the simulated LEDs are doing right now (shared by the mock gpiozero and sensor)."""
import time

levels = {"test": 0.0, "pollution": 0.0}
test_on_since = None  # when the test LED last came on, for its warm-up drift


def set_level(name, value):
    global test_on_since
    if name == "test":
        if value > 0 and levels["test"] == 0:
            test_on_since = time.monotonic()
        elif value == 0:
            test_on_since = None
    levels[name] = value
