# light-polluter

Software for the light-sealed sensor box: a TSL2591 light sensor facing a 3W test LED, plus a small off-axis "pollution" LED, all driven by a Raspberry Pi 5.

The Pi never analyses anything while it runs. It records two things, each with a timestamp taken on the Pi at the moment it happened:

- what the sensor saw
- what brightness it just told each LED to be

The laptop logs both streams to CSV, and all analysis happens afterwards on the saved files.

## Files

| File | Runs on | What it does |
| --- | --- | --- |
| `rig.py` | Pi | Reads the sensor once a second, runs one LED test, publishes everything over MQTT, and keeps a backup copy on the Pi |
| `session.py` | Pi | Runs a whole experiment session in one sitting: every condition back to back, each as its own run (see [Experiment sessions](#experiment-sessions)) |
| `setup_pwm.sh` | Pi | Gets the pollution LED's hardware PWM ready after a boot, if the scripts say they have no permission (see [Hardware PWM for the pollution LED](#hardware-pwm-for-the-pollution-led)) |
| `check_session.py` | Laptop | Checks a session's data: every run finished, brightness matches the LED log, seeds and sensor settings, step sizes in counts (see [Checking a session](#checking-a-session)) |
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
| 32 (GPIO12) | 150Ω resistor, then the pollution LED's long leg (hardware PWM) |
| 20 (GND) | Pollution LED's short leg |

The 12V DC adapter goes to the FemtoBuck only (+ to VIN, − to PGND), never to a Pi pin. FemtoBuck LED+ and LED− go to the 3W LED's + and − pads.

Until the pollution-levels block, the pollution LED's resistor was on pin 16 (GPIO23), switched on and off only. For the pollution-levels block it moved to pin 32 (GPIO12), a hardware PWM pin, so the scripts can dim it. Nothing else moved.

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

### Hardware PWM for the pollution LED

The pollution LED is dimmed by the Pi 5's hardware PWM on GPIO 12 (pin 32), channel 0 of the RP1 chip's PWM0 block. Set it up once:

1. Shut down the Pi, move the pollution LED's resistor wire from pin 16 to pin 32 (see [Wiring](#wiring)), and power up.
2. Add this line to `/boot/firmware/config.txt`, under `[all]` at the end (`sudo nano /boot/firmware/config.txt`), then reboot:
   ```
   dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4
   ```
   This also makes GPIO 13 (pin 33) a PWM output; leave it unconnected.
3. Check it worked:
   ```
   pinctrl get 12             # should mention PWM0_CHAN0
   ls -l /sys/class/pwm/      # one pwmchip should point to ...1f00098000.pwm
   ```
4. Run the 2-minute [pollution check](#quick-pollution-check). If the script says it has no permission to use hardware PWM, run `sudo sh setup_pwm.sh` after each boot, before the scripts.

The scripts find the right PWM controller on their own; its number differs between kernel versions. If they can't, they list the controllers they found, and you can give one with `--pollution-pwmchip`. Hardware PWM keeps running after a script stops, so the scripts switch it off when they exit.

The test LED is unchanged: still GPIO 18 (pin 12), dimmed by gpiozero. On the Pi 5, gpiozero times that PWM in software (through lgpio) and sets the duty in whole percent, whichever pin it's on, so the test LED isn't on hardware PWM. It doesn't conflict with the pollution LED: GPIO 12 and GPIO 18 are different PWM0 channels on the Pi 5 (0 and 2), and the test LED doesn't use the PWM0 block at all.

If hardware PWM won't work, `--pollution-pwm software` dims the pollution LED with gpiozero's software PWM instead, the same way as the test LED. `--pollution-pwm none --pollution-pin 23` drives it on and off from pin 16 as in the earlier sessions.

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

Every run records `--dark-seconds` (default 30) with both LEDs off before and after the test. `--pollution-brightness`, `--pollution-pwm` and `--pollution-pin` work as in `session.py`. Run `python rig.py --help` for all options.

### Sensor settings

The script starts at the sensor's lowest gain (`--gain low`) with 100 ms integration. If it prints a saturation warning at these settings, the test LED is too bright for the sensor, so use a lower `--level`. If readings are tiny (under about 100), try `--gain med`.

## Experiment sessions

`session.py` runs a whole session in one sitting, with the box sealed the entire time. Conditions change by script, not by opening the box. Each condition is its own run with its own set of files. Choose the plan with `--plan`:

| Plan | Sessions | Runs | Length |
| --- | --- | --- | --- |
| `main` | 1 and 2 | 1–10 | about 1 h 42 min |
| `small-steps` | 3 and 4 | 11–18 | about 1 h 12 min |
| `pollution-levels-test` | before the pollution-levels block | 21, 22, 0 | about 9 min: dark, warm-up and a pollution check at each duty |
| `pollution-levels` | the pollution-levels block, 3 sessions | 21–43 | about 3 h 50 min |
| `pollution-check` | any time | 0 | about 2 min: a quick check that the pollution LED's light changes with its brightness |

These rules hold in every plan:

- **Only timing is random, never the values.** The brightness levels and the pollution condition (off, low or high) are fixed and chosen in advance. Each run's meta file records them as labels (`change`, `pollution_level` and `brightness`), so results can be grouped by condition afterwards (see [Grouping runs by condition](#grouping-runs-by-condition)). What's random is when things happen: how long each dwell lasts, how long each ramp takes, and when the pollution LED switches. The pollution LED switches on and off at one fixed duty per run (full unless dimmed with `--pollution-brightness`, or the block's duty in the pollution-levels plan) and runs on its own schedule, independent of the test LED.
- **Every session gets a new seed.** The session seed is printed at the start. Each run's own seeds are derived from it and saved in its meta file: `schedule_seed` for the test LED and `pollution_seed` for the pollution LED. A new session never reuses an earlier session's seed (see [Seeds](#seeds)).
- **The test LED isn't switched off between runs** (except where a run starts from off), so its temperature stays stable from one run to the next.
- **Every LED command is logged with its time**, including each run's first command even when it leaves the LED where it was, so the LED log shows exactly when each run's first command was given. The first sensor reading of each run is usually taken just before that command, so it still reflects the previous run's brightness. Leave it out or handle it in the analysis.

### Main plan (sessions 1–2)

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

```
python session.py --plan main --broker <laptop IP> --low 0.2
```

If it's too long for one sitting, `--skip steps_pollution_high ramp_pollution_high` drops to a single pollution level (about 1 h 22 min), at the cost of only comparing clean against polluted rather than showing a trend across noise levels.

### Small-step plan (sessions 3–4)

The main sessions showed that a step from off to full brightness (about 580 counts) is about 30 times the pollution LED's effect (about 20 counts), so every method can tell real changes from pollution by size alone. The small-step plan adds steps closer to the pollution's size, and gives the LED the 5 minutes it needs to settle thermally:

| # | Run | Default | Test LED | Pollution LED |
| --- | --- | --- | --- | --- |
| 11 | `dark` | 1 min | Off | Off |
| 12 | `warmup` | 5 min | Constant at `--high` (1.0) | Off |
| 13 | `steps85_clean` | 15 min | Alternates 1.0 / 0.85 | Off |
| 14 | `steps85_pollution_low` | 9 min | Alternates 1.0 / 0.85 | Each state uniform(15 s, 25 s) |
| 15 | `steps85_pollution_high` | 9 min | Alternates 1.0 / 0.85 | Each state uniform(3 s, 7 s) |
| 16 | `steps95_clean` | 15 min | Alternates 1.0 / 0.95 | Off |
| 17 | `steps95_pollution_low` | 9 min | Alternates 1.0 / 0.95 | Each state uniform(15 s, 25 s) |
| 18 | `steps95_pollution_high` | 9 min | Alternates 1.0 / 0.95 | Each state uniform(3 s, 7 s) |

Each steps run starts at 1.0 and switches between its two levels, never off, each state lasting a random uniform(20 s, 60 s). The levels are set with `--steps85-levels` and `--steps95-levels`, and each run's meta file records them as `brightness`, such as `[1.0, 0.85]`.

Session 3 runs the 85 % block (13–15) first. Session 4 runs the 95 % block (16–18) first, with `--block-order 95-first`, so the methods can be tuned on one session and tested on the other. Runs keep their numbers and names whichever order they run in. Each run's meta file records the order as `run_order` and the run's place in the session as `position`.

```
python session.py --plan small-steps --list                                          # show the plan
python session.py --plan small-steps --broker <laptop IP> --time-scale 0.05          # dry run, about 4 min
python session.py --plan small-steps --broker <laptop IP>                            # session 3
python session.py --plan small-steps --broker <laptop IP> --block-order 95-first     # session 4
```

Between sessions 3 and 4, check session 3 on the laptop (see [Checking a session](#checking-a-session)).

**Dimming the pollution LED.** `--pollution-brightness 0.5` switches the pollution LED on at half brightness (dimmed with PWM), so it adds about 10 counts instead of about 20. That changes the comparisons above: the 85 % steps become about 8 times the pollution and the 95 % steps about 3 times, instead of 4 times and about the same size. Use the same setting in sessions 3 and 4 so the two are comparable. Each pollution run records it in its meta file as `pollution_brightness`, and the LED log shows the on level as 0.5.

### Pollution-levels plan (third block)

So far the pollution LED was only on or off, adding about 20 counts. To show how the methods behave as the disturbance gets weaker or stronger, this block records it at three duty cycles, 0.25, 0.5 and 1.0, set by the script through hardware PWM (see [Hardware PWM for the pollution LED](#hardware-pwm-for-the-pollution-led)). Each session starts with the clean runs, then has one block per duty:

| # | Run | Default | Test LED | Pollution LED |
| --- | --- | --- | --- | --- |
| 21 | `dark` | 1 min | Off | Off |
| 22 | `warmup` | 5 min | 1.0 | Off |
| 23 | `steps95_clean` | 15 min | Alternates 1.0 / 0.95 | Off |
| 24 | `steps85_clean` | 15 min | Alternates 1.0 / 0.85 | Off |
| 25 | `ramp_clean` | 20 min | Ramps 1.0 ↔ 0.2 | Off |
| 26–31 | `steps95_poll25_low`, `steps95_poll25_high`, `steps85_poll25_low`, `steps85_poll25_high`, `ramp_poll25_low`, `ramp_poll25_high` | 9, 9, 9, 9, 11, 11 min | As above | Duty 0.25: low switches every 15–25 s, high every 3–7 s |
| 32–37 | the same six with `poll50` | as above | As above | Duty 0.5 |
| 38–43 | the same six with `poll100` | as above | As above | Duty 1.0 |

The three duty blocks run in an order drawn from the session seed, so slow drift over a session doesn't always fall on the same duty. The order is printed at the start and saved in every run's meta file as `pollution_order`, and each run records its own duty as `pollution_duty` (0.0 for runs without pollution). Runs keep their numbers whichever order they run in; `position` records the order. Timing is random as in the other plans: steps dwell uniform(20 s, 60 s), ramps uniform(60 s, 180 s) with a 20 s plateau. The test LED is never switched off between runs.

Record 3 sessions (one for tuning, two for evaluation). Each takes about 3 h 50 min and can run overnight. Before the first one, run the 9-minute test plan and check it on the laptop:

```
python session.py --plan pollution-levels-test --broker <laptop IP>     # on the Pi
python check_session.py --plot                                         # on the laptop
python session.py --plan pollution-levels --list                         # shows this session's duty order
python session.py --plan pollution-levels --broker <laptop IP>           # each session
```

The test plan is dark, a 5-minute warm-up, then a [pollution check](#quick-pollution-check) at duty 0.25, 0.5 and 1.0 with the test LED at 1.0. `check_session.py` then shows whether the dark reading and the settled warm-up level match the earlier sessions (the rig is unchanged), whether the pollution effect grows with duty with full duty giving about the same 20 counts as before, and whether readings vary by about one count at every duty (no flicker).

PWM can only dim the pollution LED, so its strongest level is the same as before, about 20 counts. A brighter level would need a smaller resistor or a transistor driver.

### Quick pollution check

`--plan pollution-check` is a 2-minute check, not an experiment run: does the pollution LED's light change when its brightness changes? The test LED stays off while the pollution LED switches 10 s on and 10 s off, three times at full brightness and then three times at 0.5. The run is saved like any other, as `<session>_00_pollution_check`.

```
python session.py --plan pollution-check --broker <laptop IP>          # on the Pi
python check_session.py --plot                                         # on the laptop, afterwards
```

`check_session.py` prints how many counts the pollution LED adds at each brightness, what that is as a share of the effect at full brightness, and what it would be if light followed brightness exactly. It reports a problem if a lower brightness gives about the same light as a higher one (so the dimming isn't working), or if the pollution LED barely changes the reading at all. The plot shows the counts above the pollution LED's level.

| Option | Default | What it changes |
| --- | --- | --- |
| `--check-levels` | 1.0 0.5 (pollution-levels-test: 0.25 0.5 1.0) | Pollution LED brightness levels to check, in order. For example `--check-levels 1 0.5 0.25`. |
| `--check-test-level` | 0.0 (pollution-levels-test: 1.0) | Test LED level during the check. 0.0 is off; use 1.0 to see the pollution on top of the test LED, after a warm-up. |
| `--check-cycles` | 3 | On/off cycles at each brightness |
| `--check-on`, `--check-off` | 10, 10 | Seconds the pollution LED is on, and off between |

### Keep the sensor settings the same

Keep the sensor settings the same in every session, so all the data is comparable: one reading per second (`--period 1`, the default), and the same `--gain` and `--integration` as before (the defaults are `low` and 100 ms). `check_session.py` shows each session's settings and flags any session that differs.

### Seeds

Every new session draws a new seed and checks it against the sessions already in the Pi's backup folder (`~/rig-data`), so it never reuses one. Running with an earlier session's seed is refused, except together with `--only` to finish an interrupted session.

### If a session is interrupted

Ctrl+C turns both LEDs off, marks the current run as `stopped early`, and prints the command to run the remaining runs with the same seed. If the LED has been off for a while, add `warmup` to the start of the list, for example:

```
python session.py --plan small-steps --broker <laptop IP> --block-order 95-first --seed 123456 --only warmup steps85_clean steps85_pollution_low steps85_pollution_high
```

Include any other options you changed, so the conditions match the original session.

### Checking a session

On the laptop, `check_session.py` reads `data/` and prints the checks for a session (the newest, unless you name one):

```
python check_session.py                       # the newest session
python check_session.py 20261019-200512       # a specific session
python check_session.py --plot                # also save a plot of each steps run (needs: pip install matplotlib)
```

It shows:

- every session in `data/` with its plan, seed, sensor settings and how many runs finished, flagging a seed used by two sessions or sensor settings that differ from the others, and noting a dimmed pollution LED
- every run of the checked session with its status, the `brightness` in its meta file, the levels actually in its LED log, and how many times the pollution LED switched on and at what duty
- a rig check: the dark reading and the warm-up's last minute (for 5-minute warm-ups), against the sessions before this one
- how much the pollution LED adds: at each duty for a pollution-levels session or a [pollution check](#quick-pollution-check), otherwise from the session's pollution runs, and at full duty against the earlier sessions
- the step size of every steps run: the median counts at each test LED level with the pollution LED off, and how many times larger than the pollution that is (a polluted run against its own duty)
- noise: how much readings vary within steady stretches, with the pollution LED off and at each duty

It ends with `All checks passed`, or a list of problems: a run that didn't finish, brightness or `pollution_duty` that doesn't match the LED log, a reused seed, different sensor settings, failed or saturated readings, a dark reading more than 3 counts or a warm-up level more than 2 % from the earlier sessions, a pollution effect that doesn't grow with duty or differs at full duty from before, readings that vary more than twice as much with the pollution LED on (flicker). `--plot` saves `<run>_plot.png` next to each steps run's data, with the counts above the commanded test LED level and the pollution LED's on periods shaded (for a pollution check, the pollution LED's level instead).

After session 3, the 85 % steps should be about 85 counts and the 95 % steps about 28. If the 95 % steps are hard to see, check the brightness setting before running session 4.

### Useful options

| Option | Default | What it changes |
| --- | --- | --- |
| `--plan` | (required) | `main`, `small-steps`, `pollution-levels-test`, `pollution-levels` or `pollution-check` |
| `--block-order` | `85-first` | Small-step plan: which block of steps runs first. Use `95-first` for session 4. |
| `--steps85-levels A B`, `--steps95-levels A B` | 1.0 0.85, 1.0 0.95 | Small-step plan: the two levels each block alternates between, starting with the first |
| `--high`, `--low` | 1.0, 0.2 | Full brightness (also the warm-up level) and, in the main plan, the dimmest planned setting |
| `--step-levels` | `--high` | Main plan: "on" level for the steps runs. Give several to rotate through them in a fixed order. |
| `--pollution-low MIN MAX` | 15 25 | Seconds each pollution LED state lasts in the low-pollution runs |
| `--pollution-high MIN MAX` | 3 7 | Seconds each pollution LED state lasts in the high-pollution runs |
| `--pollution-brightness` | 1.0 | Pollution LED brightness when on, for example 0.5. Not used by the pollution-levels plan, which sets the duty per block. Also in `rig.py`. |
| `--pollution-pwm` | `hardware` | How the pollution LED is driven: `hardware` PWM, gpiozero's `software` PWM, or `none` (on/off only). Also in `rig.py`. |
| `--pollution-pin`, `--pollution-pwmchip` | 12, found automatically | The pollution LED's GPIO, and the sysfs pwmchip number for hardware PWM. Also in `rig.py`. |
| `--dwell MIN MAX` | 20 60 | Seconds each state lasts in the steps runs |
| `--ramp-duration MIN MAX`, `--ramp-hold`, `--ramp-step` | 60 180, 20, 1.0 | Main plan: how long each ramp takes, the plateau after it, and the seconds between ramp steps |
| `--dark-minutes`, `--warmup-minutes` | 1, 2 (main) or 5 (the other plans) | Run lengths |
| `--steps-minutes`, `--steps-pollution-minutes` | 15, 9 | Length of each clean steps run, and of each polluted steps run |
| `--baseline-minutes`, `--ramp-minutes`, `--ramp-pollution-minutes` | 12, 20, 11 | Run lengths of the baselines (main plan) and of the clean and polluted ramp runs |
| `--only RUN ...`, `--skip RUN ...` | all | Run just these runs, or leave these out. Plan order is kept. |
| `--seed` | new | Session seed for all random timings. Only reuse one to finish an interrupted session. |
| `--time-scale` | 1.0 | Multiplies every duration, for dry runs |
| `--period` | 1.0 | Seconds between sensor readings |

`--gain`, `--integration` and `--data-dir` work as in `rig.py`. Run `python session.py --help` for the full list.

## Output data

Each run gets an ID made from its start time and test name, for example `20261016-201500_ramp`. Runs from `session.py` share the session's start time and add their number and name, for example `20261017-140000_05_steps_clean` or `20261019-200512_16_steps95_clean`. The laptop writes three files per run to `data/`:

| File | One row per |
| --- | --- |
| `<run>_sensor.csv` | Sensor reading (one per second by default) |
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
| `level` | The PWM duty cycle the LED was set to, from 0 to 1 (0.5 means on half the time), as actually applied. For the test LED that's in whole percent, because gpiozero sets it that way. It's the setting, not measured light, and light output isn't guaranteed to be exactly proportional to it. For the pollution LED it's 0.0 when off, and its duty when on: 1.0, the `--pollution-brightness` level, or the block's duty in the pollution-levels plan (such as 0.25). |
| `note` | What the script was doing at the time (see below). |

An LED stays at its `level` until the next row for that same LED.

**Runs from before the pollution-levels block logged the commanded test LED level, not the applied one.** Their meta files have no `led_log_levels` field. gpiozero applied those levels truncated to whole percent, so a ramp step logged as 0.6671 really ran at 0.66. That's up to 0.01 off, about 5 counts at full brightness. It only affects ramp runs; the step levels (1.0, 0.95, 0.85, 0.2) were exact. To get the applied level for those runs:

```python
old = led["led"] == "test"
led.loc[old, "level"] = (led.loc[old, "level"] * 100).astype(int) / 100
```

Runs with `"led_log_levels": "applied"` in their meta file already log the applied level, and a ramp only gets a row when the applied level changes, so not every ramp step number appears.

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
| `dark`, `warmup at high`, `baseline at high`, `baseline at low` | A constant run's first command. Always written, even if the LED was already at that level. |
| `toggles start off` | A main-plan steps run's first command: test LED off before the first toggle. Always written. |
| `toggle on 7`, `toggle off 7` | Each toggle in a main-plan steps run, numbered |
| `steps start at 1.0` | A small-step run's first command: the test LED set to the run's first level. Always written. |
| `step 7 to 0.85` | Each switch in a small-step run, numbered, with the level it switched to |
| `ramps start at high` | A ramp run's first command: test LED to `--high` before the first ramp. Always written. |
| `ramp 3 down step 45/120`, `ramp 4 up step 1/90` | A step of a ramp that changed the applied level, with the ramp number, direction and step number. Ramps alternate down and up. A gap with no rows after the last step of a ramp is the plateau. |
| `pollution on 4`, `pollution off 4` | Each switch of the pollution LED, numbered |
| `steady for pollution check` | The pollution check's first command: test LED to `--check-test-level`. Always written. |
| `pollution on at 0.5 (2/3)`, `pollution off (2/3)` | Each switch in the pollution check, with the brightness and the cycle number at that brightness |
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
| `plan` | Start | `session.py` only: `main`, `small-steps`, `pollution-levels`, `pollution-levels-test` or `pollution-check` |
| `session`, `run_index`, `position` | Start | `session.py` only: the session's start time, this run's number in the plan (such as 16), and its place in the order the session actually ran (1 for the first run) |
| `run_order` | Start | Small-step plan only: which block of steps ran first in the session, `85-first` or `95-first` |
| `pollution_order` | Start | Pollution-levels plan only: the order the duty blocks ran in this session, such as `[0.5, 1.0, 0.25]` |
| `pollution_duty` | Start | `session.py` only, from the pollution-levels block on: the pollution LED's duty when on in this run, such as 0.25, or 0.0 for a run without pollution. For a pollution check, the list of duties checked. |
| `purpose`, `minutes` | Start | `session.py` only: what the run is for, and how long it was planned to last |
| `change` | Start | `session.py` only: what the test LED does, `none` (constant or dark), `steps` or `ramp`, or `pollution_check` for the pollution check |
| `pollution_level` | Start | `session.py` only: the pollution condition, `off`, `low` or `high`, or `check` for the pollution check |
| `pollution_interval_s` | Start | `session.py` only: the range each pollution LED state lasts, such as `[15, 25]`. Empty when pollution is off. |
| `pollution_brightness` | Start | `session.py` only: the pollution LED's brightness when on, 1.0 (full) or the `--pollution-brightness` level such as 0.5. Empty when pollution is off. Runs from before this option existed don't have it; they were all at 1.0. For the pollution check it's the list of brightness levels checked, such as `[1.0, 0.5]`. |
| `brightness` | Start | `session.py` only: the fixed test LED levels. Small-step and pollution-levels runs give a plain list: the two step levels such as `[1.0, 0.85]`, the ramp's ends such as `[1.0, 0.2]`, or one level such as `[1.0]` for the warm-up. Main-plan runs give a description such as `{"type": "steps", "off": 0.0, "on": [1.0]}`, `{"type": "ramp", "high": 1.0, "low": 0.2}` or `{"type": "constant", "level": 0.2}`. |
| `timing` | Start | `session.py` only: the ranges the random timings were drawn from, such as `{"dwell_s": [20, 60]}` or `{"ramp_duration_s": [60, 180], "ramp_hold_s": 20, "ramp_step_s": 1.0}`, or for the pollution check `{"on_s": 10, "off_s": 10, "cycles_per_level": 3}` |
| `session_seed` | Start | `session.py` only: the session's seed, the one you pass to `--seed` |
| `schedule_seed`, `pollution_seed` | Start | `session.py` only: this run's own seeds for the test LED's and the pollution LED's timings. `pollution_seed` is empty when pollution is off. |
| `period` | Start | Seconds between sensor readings actually used |
| `pwm_frequency` | Start | Frequency of the LEDs' dimming signal, in Hz |
| `test_led_pin`, `pollution_led_pin` | Start | GPIO numbers the LEDs were driven from (the pollution LED: 23 before the pollution-levels block, 12 from then on) |
| `pollution_pwm` | Start | From the pollution-levels block on: how the pollution LED was driven, such as `hardware PWM, /sys/class/pwm/pwmchip0/pwm0` |
| `led_log_levels` | Start | From the pollution-levels block on: `applied`, meaning the LED log holds the levels actually applied (see [`<run>_led.csv`](#run_ledcsv)) |
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

# Or every small-step run with the 95 % steps, from both sessions
small95 = runs[runs["brightness"].astype(str) == "[1.0, 0.95]"]
```
