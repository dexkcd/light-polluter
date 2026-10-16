# light-polluter

Software for the light-sealed sensor box: a TSL2591 light sensor facing a 3W test LED, plus a small off-axis "pollution" LED, all driven by a Raspberry Pi 5.

The Pi never analyses anything while it runs. It records two things, each with a timestamp taken on the Pi at the moment it happened:

- what the sensor saw
- what brightness it just told each LED to be

The laptop logs both streams to CSV, and all analysis happens afterwards on the saved files.

## Files

| File | Runs on | What it does |
| --- | --- | --- |
| `rig.py` | Pi | Reads the sensor about twice a second, runs one LED test, publishes everything over MQTT, and keeps a backup copy on the Pi |
| `logger.py` | Laptop | Subscribes to the broker and writes one set of CSV files per run |
| `mosquitto.conf` | Laptop | Lets the Pi connect to the Mosquitto broker over the local network |

## Wiring

| Pi pin | Connects to |
| --- | --- |
| 1 (3.3V) | TSL2591 VIN (Qwiic red) |
| 3 (SDA) | TSL2591 SDA (Qwiic blue) |
| 5 (SCL) | TSL2591 SCL (Qwiic yellow) |
| 6 (GND) | TSL2591 GND (Qwiic black) |
| 12 (GPIO18) | FemtoBuck dim input |
| 14 (GND) | FemtoBuck DGND |
| 16 (GPIO23) | 150Ω resistor, then the pollution LED's long leg |
| 20 (GND) | Pollution LED's short leg |

The 12V DC adapter goes to the FemtoBuck only (+ to VIN, − to PGND), never to a Pi pin. FemtoBuck LED+ and LED− go to the 3W LED's + and − pads.

## Setup

### Pi (once)

```
sudo raspi-config        # Interface Options -> I2C -> enable
sudo apt install -y python3-gpiozero python3-lgpio i2c-tools
python3 -m venv --system-site-packages ~/rig-venv
source ~/rig-venv/bin/activate
pip install adafruit-blinka adafruit-circuitpython-tsl2591 paho-mqtt
```

Check the sensor with `sudo i2cdetect -y 1`. You should see `29`.

Each time you open a new terminal on the Pi, run `source ~/rig-venv/bin/activate` first.

### Laptop (once)

1. Install Mosquitto: `brew install mosquitto` on a Mac, or the installer from mosquitto.org on Windows.
2. Install the MQTT library: `pip install paho-mqtt`.
3. Find the laptop's IP address: `ipconfig getifaddr en0` on a Mac, or `ipconfig` on Windows (look for "IPv4 Address").

## Running a session

1. On the laptop, in this folder, start the broker:
   ```
   mosquitto -c mosquitto.conf -v
   ```
   If Windows asks about the firewall, allow private networks. If Mosquitto is already running as a background service, stop it first.
2. In a second laptop terminal, start the logger:
   ```
   python logger.py
   ```
3. On the Pi, start a test:
   ```
   python rig.py --broker <laptop IP> --test steps
   ```

For a short check before real trials:

```
python rig.py --broker <laptop IP> --test steps --cycles 2 --on-seconds 5 --off-seconds 5 --dark-seconds 5
```

Then open the CSVs in `data/` and check that both sensor readings and LED events are appearing.

## Tests

| Test | What it does |
| --- | --- |
| `--test steps` | Switches the test LED abruptly between off and `--level` |
| `--test ramp` | Dims the test LED down to off and back up; every step is its own event |
| `--test pollution` | Holds the test LED steady while the pollution LED switches on and off |
| `--test hold` | Holds the test LED at one level for `--hold-minutes` (long drift baseline) |

Every run records `--dark-seconds` (default 30) with both LEDs off before and after the test. Run `python rig.py --help` for all options.

### Sensor settings

The script starts at the sensor's lowest gain (`--gain low`) with 100 ms integration. If it prints a saturation warning at these settings, the test LED is too bright for the sensor, so use a lower `--level`. If readings are tiny (under about 100), try `--gain med`.

## Output

The laptop writes one set of files per run to `data/`:

- `<run>_sensor.csv`: every sensor reading (raw counts `ch0_full` and `ch1_ir`)
- `<run>_led.csv`: every LED change
- `<run>_meta.jsonl`: start and end of the run, with the settings used

Use the `t` (wall clock) or `mono` (monotonic, best for intervals) columns for analysis. Both come from the Pi's clock, so the two streams line up. `received_t` is only when the laptop received the message.

The Pi also writes every message to `~/rig-data/<run>.jsonl`, so nothing is lost if Wi-Fi drops mid-run.
