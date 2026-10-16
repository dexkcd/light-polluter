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
| `session.py` | Pi | Runs the full experimental procedure in one sitting: every condition back to back, each as its own run (see [Full experiment session](#full-experiment-session)) |
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

## Full experiment session

`session.py` runs the whole experimental procedure in one sitting, with the box sealed the entire time. Conditions change by script, not by opening the box. Each condition is its own run with its own set of files. With the default durations it takes about 1 hour 42 minutes:

| # | Run | Default | Pollution | What's random | Purpose |
| --- | --- | --- | --- | --- | --- |
| 1 | `dark` | 1 min | off | Nothing | Both LEDs off: the sensor's dark floor |
| 2 | `warmup` | 2 min | off | Nothing | Test LED at `--high`, so its warm-up drift doesn't land in the baseline |
| 3 | `baseline_high` | 12 min | off | Nothing | Noise floor at the bright end (raw counts, not just lux) |
| 4 | `baseline_low` | 12 min | off | Nothing | Noise floor at the dim end, where shot noise bites hardest |
| 5 | `steps_clean` | 15 min | off | Each dwell before a toggle, uniform(20 s, 60 s) | Core BOCPD vs. CUSUM test: the easy case |
| 6 | `steps_pollution_low` | 9 min | low | The dwell, and each pollution state, uniform(15 s, 25 s) | First point on the noise curve |
| 7 | `steps_pollution_high` | 9 min | high | The dwell, and each pollution state, uniform(3 s, 7 s) | Second point: the stress case for RQ1 |
| 8 | `ramp_clean` | 20 min | off | Each ramp's duration, uniform(60 s, 180 s) | Core GP vs. GAM vs. EWMA test |
| 9 | `ramp_pollution_low` | 11 min | low | Ramp duration, and each pollution state, uniform(15 s, 25 s) | Calibration under light noise |
| 10 | `ramp_pollution_high` | 11 min | high | Ramp duration, and each pollution state, uniform(3 s, 7 s) | Calibration under heavy noise: do GP's intervals stay honest? |

In the steps runs the test LED switches between off and `--high`. In the ramp runs it ramps between `--high` and `--low`, alternating down and up, with a 20 s plateau after each ramp.

**Only timing is random, never the values.** The brightness levels and the pollution condition (off, low or high) are fixed and chosen in advance. Each run's meta file records them as labels (`change`, `pollution_level` and `brightness`), so results can be grouped by condition afterwards (see [Grouping runs by condition](#grouping-runs-by-condition)). What's random is when things happen: how long each dwell lasts, how long each ramp takes, and when the pollution LED switches. The pollution LED runs on its own schedule, independent of the test LED.

**Every run has its own seeds.** `--seed` sets the session seed, which is printed at the start. Each run's seeds are derived from it and saved in its meta file: `schedule_seed` for the test LED and `pollution_seed` for the pollution LED. Running again with the same `--seed` reproduces every run's exact schedule. Every LED change is also logged with its timestamp, so the LED files are the ground truth whatever the seed.

For more trials at each condition, run more sessions. Each new session gets a new seed unless you set one, and the meta labels let you pool runs from several sessions.

The test LED isn't switched off between runs (except where a run starts from off), so its temperature carries over smoothly from one condition to the next.

### Running it

Start the broker and `logger.py` on the laptop as usual. Then on the Pi:

```
python session.py --list                                   # show the plan and total time
python session.py --broker <laptop IP> --low 0.2           # the real session
```

Set `--low` to your dimmest planned brightness (the default is 0.2). `--high` defaults to full brightness (1.0).

Before committing the full session, do a dry run of about 5 minutes. It runs every condition with all durations scaled down:

```
python session.py --broker <laptop IP> --low 0.2 --time-scale 0.05
```

If the full session is too long for one sitting, the easiest trim is to drop to a single pollution level. That takes it to about 1 hour 22 minutes, but you can then only compare clean against polluted, not show a trend across noise levels:

```
python session.py --broker <laptop IP> --low 0.2 --skip steps_pollution_high ramp_pollution_high
```

### If a session is interrupted

Ctrl+C turns both LEDs off, marks the current run as `stopped early`, and prints the command to run the remaining runs with the same seed. If the LED has been off for a while, add `warmup` to the start of the list, for example:

```
python session.py --broker <laptop IP> --high 1 --low 0.2 --seed 123456 --only warmup ramp_clean ramp_pollution_low ramp_pollution_high
```

Include any other options you changed, so the conditions match the original session.

### Useful options

| Option | Default | What it changes |
| --- | --- | --- |
| `--high`, `--low` | 1.0, 0.2 | Full brightness and the dimmest planned setting |
| `--step-levels` | `--high` | "On" level for the steps runs. Give several to rotate through them in a fixed order, such as `--step-levels 1.0 0.5`. Each toggle's level is logged. |
| `--pollution-low MIN MAX` | 15 25 | Seconds each pollution LED state lasts in the low-pollution runs |
| `--pollution-high MIN MAX` | 3 7 | Seconds each pollution LED state lasts in the high-pollution runs |
| `--dwell MIN MAX` | 20 60 | Seconds each off/on state lasts in the steps runs |
| `--ramp-duration MIN MAX` | 60 180 | Seconds each ramp takes |
| `--ramp-hold` | 20 | Seconds of plateau after each ramp |
| `--ramp-step` | 1.0 | Seconds between brightness steps within a ramp |
| `--dark-minutes`, `--warmup-minutes`, `--baseline-minutes` | 1, 2, 12 | Run lengths. `--baseline-minutes` applies to both baselines. |
| `--steps-minutes`, `--steps-pollution-minutes` | 15, 9 | Length of the clean steps run, and of each polluted steps run |
| `--ramp-minutes`, `--ramp-pollution-minutes` | 20, 11 | Length of the clean ramp run, and of each polluted ramp run |
| `--only RUN ...`, `--skip RUN ...` | all | Run just these runs, or leave these out. Plan order is kept. |
| `--seed` | random | Session seed for all random timings |
| `--time-scale` | 1.0 | Multiplies every duration, for dry runs |

`--gain`, `--integration`, `--period` and `--data-dir` work as in `rig.py`. Run `python session.py --help` for the full list.

## Output data

Each run gets an ID made from its start time and test name, for example `20261016-201500_ramp`. Runs from `session.py` share the session's start time and add their number and name, for example `20261017-140000_05_steps_clean`, so a session's files sort in order. The laptop writes three files per run to `data/`:

| File | One row per |
| --- | --- |
| `<run>_sensor.csv` | Sensor reading (about two per second) |
| `<run>_led.csv` | Change to either LED |
| `<run>_meta.jsonl` | Start or end of the run |

The Pi also keeps its own copy of everything (see [Pi backup file](#pi-backup-file)).

### Columns in every file

| Column | Meaning |
| --- | --- |
| `seq` | Message counter for this stream within the run, starting at 1. A jump (say 41 then 43) means a message didn't reach the laptop. It's still in the Pi's backup file. |
| `t` | When it happened, by the Pi's clock, in Unix time (seconds since 1970-01-01 UTC). Use it for readable dates and times. |
| `mono` | When it happened, by the Pi's monotonic clock, in seconds from an arbitrary starting point. It never jumps, even when the Pi corrects its clock over the network, so use it for measuring intervals and for lining the sensor and LED files up. Values are only comparable between runs if the Pi hasn't rebooted in between. |
| `received_t` | When the laptop received the message, by the laptop's clock. Only useful for checking network delays. Don't use it for analysis. |

### `<run>_sensor.csv`

| Column | Meaning |
| --- | --- |
| `ch0_full` | Raw count from the sensor's channel 0, which sees visible and infrared light. Unitless. Blank if the read failed. |
| `ch1_ir` | Raw count from channel 1, which sees infrared only. Visible light is roughly `ch0_full - ch1_ir`. Blank if the read failed. |
| `saturated` | `True` if either channel hit the sensor's maximum (36863 at 100 ms integration, 65535 otherwise). The reading is clipped and shouldn't be used. |
| `gain` | Sensor gain setting: `low`, `med`, `high` or `max`. |
| `integration_ms` | How long the sensor collected light for each reading, in milliseconds. |
| `error` | Empty normally. If reading the sensor failed, this holds the error message and both counts are blank. |

The counts are raw, not lux. They scale with gain and integration time, so only compare readings taken with the same settings. To compare across settings, divide by the gain multiplier (roughly 1, 25, 428 and 9876 for `low`, `med`, `high` and `max`) and by `integration_ms`.

Each reading is the light collected over the `integration_ms` before its timestamp. A reading taken less than `integration_ms` after an LED change can mix the old and new brightness.

### `<run>_led.csv`

| Column | Meaning |
| --- | --- |
| `led` | Which LED changed: `test` (the 3W LED) or `pollution` (the 5mm LED). |
| `level` | What the LED was set to, from 0 to 1. For the test LED this is the PWM duty cycle sent to the FemtoBuck (0.5 means on half the time). It's the commanded setting, not measured light, and light output isn't guaranteed to be exactly proportional to it. For the pollution LED it's 1.0 (on) or 0.0 (off). |
| `note` | What the script was doing at the time (see below). |

An LED stays at its `level` until the next row for that same LED.

Notes written by `rig.py`:

| `note` | Written when |
| --- | --- |
| `start` | Both LEDs are set to off as the run begins |
| `step on 2/5`, `step off 2/5` | The `steps` test switches the test LED on or off, with the cycle number |
| `ramp 1/5 step 37/101` | Each step of the `ramp` test, with the cycle and step number |
| `steady for pollution test` | The `pollution` test sets the test LED to its steady level |
| `pollution on 3/5`, `pollution off 3/5` | The `pollution` test switches the pollution LED, with the cycle number |
| `hold` | The `hold` test sets the test LED to its level |
| `test finished` | The test is over and the LEDs are switched off for the final dark period |
| `end of run` | An LED was switched off during shutdown. Only appears when a run was stopped early while an LED was on. |

Notes written by `session.py`:

| `note` | Written when |
| --- | --- |
| `state at run start` | First two rows of every run: where both LEDs are as the run begins, carried over from the previous run. Every run's LED file is complete on its own. |
| `dark`, `warmup at high`, `baseline at high`, `baseline at low` | The constant runs set the test LED. No row if it was already at that level. |
| `toggles start off` | A steps run switches the test LED off before its first toggle. No row if it was already off. |
| `toggle on 7`, `toggle off 7` | Each toggle in a steps run, numbered |
| `ramps start at high` | A ramp run sets the test LED to `--high` before the first ramp |
| `ramp 3 down step 45/120`, `ramp 4 up step 1/90` | Each step of each ramp, with the ramp number, direction and step number. Ramps alternate down and up. A gap with no rows after the last step of a ramp is the plateau. |
| `pollution on 4`, `pollution off 4` | Each switch of the pollution LED, numbered |
| `pollution off at end of run` | The pollution LED was on when a pollution run ended, so it's switched off |
| `session over` | Any LED still on is switched off at the end of the last run |
| `stopped early` | Any LED still on is switched off because the session was interrupted |

### `<run>_meta.jsonl`

One JSON object per line: one at the start of the run and one at the end. Besides `run`, `stream`, `seq`, `t`, `mono` and `received_t`:

| Field | In | Meaning |
| --- | --- | --- |
| `event` | Both | `start` or `end` |
| `test` | Start | Which test ran: `steps`, `ramp`, `pollution` or `hold` from `rig.py`, or the run name (such as `steps_clean`) from `session.py` |
| `settings` | Start | Every option the script was run with, such as `level`, `gain` and `integration` (or `high`, `low` and the timing ranges for `session.py`) |
| `session`, `run_index` | Start | `session.py` only: the session's start time, and this run's number in the plan |
| `purpose`, `minutes` | Start | `session.py` only: what the run is for, and how long it was planned to last |
| `change` | Start | `session.py` only: what the test LED does, `none` (constant or dark), `steps` or `ramp` |
| `pollution_level` | Start | `session.py` only: the pollution condition, `off`, `low` or `high` |
| `pollution_interval_s` | Start | `session.py` only: the range each pollution LED state lasts, such as `[15, 25]`. Empty when pollution is off. |
| `brightness` | Start | `session.py` only: the fixed test LED levels, such as `{"type": "steps", "off": 0.0, "on": [1.0]}`, `{"type": "ramp", "high": 1.0, "low": 0.2}` or `{"type": "constant", "level": 0.2}` |
| `timing` | Start | `session.py` only: the ranges the random timings were drawn from, such as `{"dwell_s": [20, 60]}` or `{"ramp_duration_s": [60, 180], "ramp_hold_s": 20, "ramp_step_s": 1.0}` |
| `session_seed` | Start | `session.py` only: the session's seed, the one you pass to `--seed` |
| `schedule_seed`, `pollution_seed` | Start | `session.py` only: this run's own seeds for the test LED's and the pollution LED's timings. `pollution_seed` is empty when pollution is off. |
| `period` | Start | Seconds between sensor readings actually used |
| `pwm_frequency` | Start | Frequency of the test LED's dimming signal, in Hz |
| `test_led_pin`, `pollution_led_pin` | Start | GPIO numbers the LEDs were driven from |
| `status` | End | `finished`, or `stopped early` if the run was interrupted (for example with Ctrl+C) |

If a meta file has no `end` line, either the Pi stopped without shutting down cleanly (power loss or a crash), or the message didn't reach the laptop. The Pi's backup file will tell you which.

### Pi backup file

`~/rig-data/<run>.jsonl` on the Pi holds every message from all three streams, one JSON object per line. It has the same fields as above, plus `stream` (`sensor`, `led` or `meta`) to say which kind each line is. It has no `received_t`. If the laptop missed anything, this file is the complete record.

### Lining up readings with LED levels

This adds the LED levels in force at each reading, and drops failed reads and readings that overlapped an LED change:

```python
import pandas as pd

run = "20261016-201500_ramp"
sensor = pd.read_csv(f"data/{run}_sensor.csv")
led = pd.read_csv(f"data/{run}_led.csv")

# Give every reading the LED levels that were in force when it was taken
for name in ["test", "pollution"]:
    events = led[led["led"] == name][["mono", "level"]].rename(columns={"level": f"{name}_level"})
    sensor = pd.merge_asof(sensor.sort_values("mono"), events, on="mono")

# Seconds since the most recent change to either LED
changes = led[["mono"]].rename(columns={"mono": "last_change"})
sensor = pd.merge_asof(sensor, changes, left_on="mono", right_on="last_change")
sensor["since_change"] = sensor["mono"] - sensor["last_change"]

# Drop failed reads, and readings whose measurement overlapped an LED change
clean = sensor[sensor["error"].isna() & (sensor["since_change"] > sensor["integration_ms"] / 1000)]
```

### Grouping runs by condition

Each `session.py` run's meta file starts with its condition labels. This builds a table of runs and picks out every run with a given condition, across all sessions:

```python
import glob, json
import pandas as pd

# One row per run, with its condition labels, from the first (start) line of each meta file
runs = pd.DataFrame([json.loads(open(f).readline()) for f in sorted(glob.glob("data/*_meta.jsonl"))])
runs = runs.dropna(subset=["change"])  # keep session.py runs
print(runs[["run", "change", "pollution_level", "schedule_seed"]])

# For example, every steps run at high pollution
for run in runs[(runs["change"] == "steps") & (runs["pollution_level"] == "high")]["run"]:
    sensor = pd.read_csv(f"data/{run}_sensor.csv")
```
