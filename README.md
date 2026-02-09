# auto-z

Automatic Z-offset plugin for Klipper that interactively calibrates your Z offset using your probe or CNC Tap.  Calibrate once with a paper test, then every print gets a precise, automatic Z offset.

## Features

- **Probe type awareness** -- built-in presets for CNC Tap, Microprobe, BLTouch, inductive/capacitive probes, and a generic fallback.  Set `probe_type: tap` and get tuned defaults automatically.
- **One-time calibration** -- interactive paper test using Klipper's `TESTZ` / `ACCEPT` flow.  Only needs to be done once; calibration survives restarts.
- **Automatic per-print offset** -- call `AUTO_Z_TAP` in your `START_PRINT` macro.  The plugin probes the bed, detects drift from calibration, applies temperature compensation and profile adjustments, and sets `SET_GCODE_OFFSET`.
- **Thermal soak** -- waits for bed/chamber temperature to stabilize before probing.  Critical for CNC Tap and inductive probes where thermal expansion shifts the trigger point.
- **Warm-up taps** -- throwaway probe taps before the real measurement to settle thermal expansion, ooze, and mechanical play.
- **Probe health monitoring** -- tracks spread, drift, and retry rates across runs.  Detects degradation trends and warns you before problems affect prints.
- **Adaptive sample count** -- optionally lets the health tracker reduce samples when confidence is high or increase them when readings are erratic.
- **Adjustment profiles** -- per-material, per-surface, per-nozzle, and per-probe-type offset profiles with temperature coefficients.
- **Safety guards** -- rejects computed offsets outside a safe range to prevent nozzle crashes or air printing.
- **Polynomial temperature compensation** -- for probes with non-linear temperature response (inductive/capacitive).
- **Moonraker integration** -- update manager support and `get_status()` for Mainsail/Fluidd.

## Quick Start

### CNC Tap

```ini
[save_variables]
filename: ~/printer_data/config/variables.cfg

[auto_z_tap]
probe_type: tap
reference_xy_position: 150, 150
```

This gives you: 5 samples, 0.015mm spread limit, 3 retries, 2 warm-up taps, thermal soak enabled (0.3C/min threshold, 5 min timeout), temperature compensation tuned for tap thermal behavior.

### Microprobe / Klicky

```ini
[auto_z_tap]
probe_type: microprobe
reference_xy_position: 150, 150
```

### BLTouch

```ini
[auto_z_tap]
probe_type: bltouch
reference_xy_position: 150, 150
```

### Inductive / Capacitive

```ini
[auto_z_tap]
probe_type: inductive
reference_xy_position: 150, 150
```

Includes aggressive bed temperature compensation (0.0008 mm/C) and thermal soak (0.2C/min threshold, 10 min timeout) for probes whose trigger distance is highly temperature-dependent.

## Installation

```bash
cd ~/auto-z
./install.sh
```

The script will:
- symlink `klippy/extras/auto_z_tap.py` into Klipper's `klippy/extras/` directory,
- install sample config as `~/printer_data/config/auto_z_tap.cfg` (if not already present),
- write Moonraker update manager config,
- add the include to `moonraker.conf`.

After install, restart Klipper and Moonraker.

## First-Time Calibration

Run once:

```gcode
AUTO_Z_TAP CALIBRATE=1
```

The plugin will:
1. Home axes (if needed).
2. Wait for thermal stability (if thermal soak is enabled).
3. Run warm-up taps (if configured).
4. Probe the reference point.
5. Start Klipper's manual probe helper.

Then use the paper test:
- `TESTZ Z=-0.1` -- move nozzle down 0.1mm
- `TESTZ Z=+0.1` -- move nozzle up 0.1mm
- `TESTZ Z=-` -- bisect down
- `TESTZ Z=+` -- bisect up
- `ACCEPT` -- save when paper drag feels right

The calibration is stored in `[save_variables]` and persists across restarts.  You only need to recalibrate after major hardware changes (nozzle swap, toolhead change, bed stack change).

## Per-Print Usage

Add one line to your `START_PRINT` macro:

```gcode
AUTO_Z_TAP MATERIAL={params.MATERIAL|default("pla")|lower} BUILD_SURFACE={params.SURFACE|default("pei")|lower} BED_TEMP={params.BED|float} HOTEND_TEMP={params.HOTEND|float} FIRST_LAYER_HEIGHT={params.FIRST_LAYER_HEIGHT|default(0.2)|float}
```

Or simply:

```gcode
AUTO_Z_TAP
```

The plugin handles everything automatically: thermal soak, warm-up taps, multi-sample probing, drift detection, temperature compensation, profile matching, safety validation, and offset application.

## Configuration Reference

### [auto_z_tap]

#### Probe Type

| Parameter | Default | Description |
|-----------|---------|-------------|
| `probe_type` | `generic` | `tap`, `microprobe`, `bltouch`, `inductive`, `generic` |
| `probe_object` | `probe` | Klipper probe object name |

#### Probe Presets

Setting `probe_type` automatically configures sensible defaults:

| Parameter | tap | microprobe | bltouch | inductive | generic |
|-----------|-----|------------|---------|-----------|---------|
| `probe_samples` | 5 | 5 | 7 | 7 | 5 |
| `max_probe_spread` | 0.015 | 0.020 | 0.030 | 0.025 | 0.020 |
| `probe_retries` | 3 | 2 | 3 | 2 | 2 |
| `sample_retract_dist` | 1.0 | 1.5 | 2.0 | 2.0 | 1.5 |
| `warmup_taps` | 2 | 0 | 1 | 1 | 0 |
| `thermal_soak` | True | False | False | True | False |
| `bed_temp_coeff` | 0.00010 | 0.00005 | 0.00003 | 0.00080 | 0.0 |
| `max_drift` | 0.80 | 1.0 | 1.2 | 1.5 | 1.0 |

All preset values can be overridden by explicit config entries.

#### Motion & Probing

| Parameter | Default | Description |
|-----------|---------|-------------|
| `reference_xy_position` | bed center | X,Y probe point |
| `auto_home` | True | Home axes if needed |
| `home_command` | `G28` | Home command to run |
| `clear_bed_mesh_before_probe` | True | Clear mesh before probing |
| `safe_z` | 10 | Safe Z for XY travel (mm) |
| `probe_start_z` | 8 | Z height to start probing from (mm) |
| `travel_speed` | 150 | XY travel speed (mm/s) |
| `probe_speed` | (probe default) | Probe speed override (mm/s) |
| `lift_speed` | (travel_speed) | Z lift speed (mm/s) |
| `probe_samples` | (preset) | Number of probe samples |
| `probe_samples_result` | `median` | `median` or `average` |
| `max_probe_spread` | (preset) | Max sample spread (mm) |
| `probe_retries` | (preset) | Retries on high spread |
| `sample_retract_dist` | (preset) | Z retract between samples (mm) |
| `max_drift` | (preset) | Max drift from calibration (mm) |
| `calibration_z_hop` | 5.0 | Z hop during calibration (mm) |

#### Warm-up & Thermal Soak

| Parameter | Default | Description |
|-----------|---------|-------------|
| `warmup_taps` | (preset) | Throwaway taps before measurement |
| `thermal_soak` | (preset) | Wait for thermal stability |
| `thermal_soak_threshold` | (preset) | Max temp rate (C/min) |
| `thermal_soak_timeout` | (preset) | Max wait time (seconds) |
| `thermal_soak_sensors` | `heater_bed` | Sensors to monitor (comma-separated) |

#### Safety

| Parameter | Default | Description |
|-----------|---------|-------------|
| `safe_offset_min` | (preset) | Reject offset below this (mm) |
| `safe_offset_max` | (preset) | Reject offset above this (mm) |
| `max_total_adjustment` | (preset) | Cap on total adjustment (mm) |

#### Temperature Compensation

| Parameter | Default | Description |
|-----------|---------|-------------|
| `global_offset` | 0.0 | Fixed offset every run (mm) |
| `bed_temp_coeff` | (preset) | Linear bed temp coefficient |
| `hotend_temp_coeff` | (preset) | Linear hotend temp coefficient |
| `chamber_temp_coeff` | (preset) | Linear chamber temp coefficient |
| `first_layer_coeff` | 0.0 | First layer height coefficient |
| `bed_temp_poly` | (none) | Polynomial bed temp coefficients |
| `hotend_temp_poly` | (none) | Polynomial hotend temp coefficients |
| `chamber_temp_poly` | (none) | Polynomial chamber temp coefficients |
| `bed_temp_reference` | (calibration) | Bed temp reference (C) |
| `hotend_temp_reference` | (calibration) | Hotend temp reference (C) |
| `chamber_temp_reference` | (calibration) | Chamber temp reference (C) |
| `first_layer_reference` | 0.20 | First layer height reference (mm) |

Polynomial coefficients are comma-separated: `c1, c2, c3...` and applied as `c1*(T-Tref) + c2*(T-Tref)^2 + c3*(T-Tref)^3`.

#### Profiles & Behavior

| Parameter | Default | Description |
|-----------|---------|-------------|
| `default_profile` | (none) | Default profile name(s) |
| `chamber_sensor` | (none) | Chamber temperature sensor |
| `apply_move` | False | Move toolhead on offset change |
| `persist_last_run` | True | Save last run data |
| `report_breakdown` | True | Show detailed adjustment breakdown |

#### Calibration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `calibration_validate` | False | Probe after calibration to verify |
| `require_save_variables` | True | Require [save_variables] |
| `variable_prefix` | `auto_z_tap` | Prefix for saved variables |

#### Health Tracking

| Parameter | Default | Description |
|-----------|---------|-------------|
| `probe_health_tracking` | True | Enable probe health monitoring |
| `adaptive_samples` | False | Auto-adjust sample count |

### [auto_z_tap_adjustment \<name\>]

| Parameter | Default | Description |
|-----------|---------|-------------|
| `priority` | 100 | Lower = applied first |
| `enabled` | True | Enable/disable this profile |
| `material` | (any) | Match material tag |
| `build_surface` | (any) | Match surface tag |
| `nozzle` | (any) | Match nozzle tag |
| `probe_type` | (any) | Only apply for this probe type |
| `offset` | 0.0 | Static Z offset (mm) |
| `bed_temp_coeff` | 0.0 | Bed temp coefficient |
| `hotend_temp_coeff` | 0.0 | Hotend temp coefficient |
| `chamber_temp_coeff` | 0.0 | Chamber temp coefficient |
| `first_layer_coeff` | 0.0 | First layer coefficient |
| `bed_temp_poly` | (none) | Polynomial bed temp coefficients |
| `hotend_temp_poly` | (none) | Polynomial hotend temp coefficients |
| `chamber_temp_poly` | (none) | Polynomial chamber temp coefficients |
| `bed_temp_reference` | (global/cal) | Override bed temp reference |
| `hotend_temp_reference` | (global/cal) | Override hotend temp reference |
| `chamber_temp_reference` | (global/cal) | Override chamber temp reference |
| `first_layer_reference` | (global) | Override first layer reference |

## Commands

### AUTO_Z_TAP

Main command.  Call in `START_PRINT` for automatic Z offset.

**Parameters:**
- `CALIBRATE=1` -- start interactive paper calibration
- `CALIBRATE_IF_NEEDED=1` -- calibrate only if not already calibrated
- `CLEAR=1` -- clear calibration state
- `MATERIAL=<tag>` / `BUILD_SURFACE=<tag>` / `NOZZLE=<tag>` -- profile matching
- `PROFILE=<name>` / `PROFILES=<name1,name2>` -- explicit profiles
- `AUTO_MATCH=1|0` -- enable/disable auto profile matching
- `BED_TEMP=<float>` / `HOTEND_TEMP=<float>` / `CHAMBER_TEMP=<float>` -- temperature overrides
- `FIRST_LAYER_HEIGHT=<float>` -- first layer height
- `EXTRA=<float>` -- manual per-print additive trim
- `WARMUP_TAPS=<int>` -- override warm-up tap count
- `THERMAL_SOAK=1|0` -- override thermal soak
- `THERMAL_SOAK_TIMEOUT=<float>` -- override soak timeout
- `SAMPLES=<int>` / `RETRIES=<int>` / `MAX_SPREAD=<float>` -- probe parameters
- `SAMPLES_RESULT=median|average` -- aggregation method
- `MAX_DRIFT=<float>` / `MAX_ADJUST=<float>` -- safety limits
- `SAFE_OFFSET_MIN=<float>` / `SAFE_OFFSET_MAX=<float>` -- offset range
- `MOVE=1|0` / `MOVE_SPEED=<float>` -- SET_GCODE_OFFSET move behavior
- `SAVE=1|0` -- persist last run values
- `X=<float>` / `Y=<float>` -- override reference position

### AUTO_Z_TAP_CALIBRATE

Shortcut for `AUTO_Z_TAP CALIBRATE=1`.

### AUTO_Z_TAP_STATUS

Shows calibration state, probe type, last run values, health statistics.

### AUTO_Z_TAP_CLEAR

Clears calibration and saved state.  Pass `CLEAR_HISTORY=1` to also clear probe health history.

### AUTO_Z_TAP_HEALTH

Detailed probe health report: confidence score, spread/drift statistics, trend analysis, suggested sample count, and warnings.

### AUTO_Z_TAP_PROBE_TEST

Diagnostic command.  Probes without applying any offset.  Reports individual values, median, average, spread, stdev, and a quality rating.

```gcode
AUTO_Z_TAP_PROBE_TEST SAMPLES=10
```

## Probe Type Guide

### CNC Tap

The nozzle is the probe.  No probe-to-nozzle offset.  Trigger point shifts with thermal expansion of the hotend and frame.

- **Warm-up taps** are important -- the first tap after homing is often less accurate due to thermal and mechanical settling.
- **Thermal soak** matters -- if the bed/frame temperature is changing, the trigger point drifts.  The preset enables soak by default.
- **Temperature coefficients** are pre-tuned for typical tap behavior.

### Microprobe / Klicky

Small, mechanically-triggered probes with minimal temperature sensitivity.

- Generally very repeatable, so no warm-up taps needed by default.
- Thermal soak is optional.
- Smaller temperature coefficients than tap.

### BLTouch / Servo Probes

Electro-mechanical probes with a deploy/retract cycle.

- Higher sample count (7) to account for deploy variation.
- Wider spread tolerance (0.030mm).
- One warm-up tap to settle the deploy mechanism.

### Inductive / Capacitive

Proximity probes with significant temperature sensitivity.

- **Very high bed temperature coefficient** (0.0008 mm/C) -- the trigger distance changes substantially with bed temperature.
- **Thermal soak is critical** -- enabled by default with a tight threshold (0.2C/min) and long timeout (10 min).
- **Polynomial compensation** available for non-linear response: set `bed_temp_poly` instead of `bed_temp_coeff`.

## Troubleshooting

### "probe repeatability failed: spread exceeds limit"

The probe is not returning consistent readings.  Check:
- Nozzle cleanliness (for CNC Tap)
- Probe mount tightness
- Wiring and electrical connections
- Thermal stability (`thermal_soak: True`)

Increase `max_probe_spread` or `probe_retries` if the spread is only slightly over the limit.

### "drift exceeds MAX_DRIFT"

The probe is triggering at a very different Z position than during calibration.  This usually means:
- Temperature has changed significantly since calibration
- Bed surface was changed or shifted
- Probe mount loosened

Re-run `AUTO_Z_TAP_CALIBRATE` at your typical printing temperatures.

### "offset below safe minimum" / "exceeds safe maximum"

The computed Z offset is outside the expected range.  This is a safety check to prevent nozzle crashes.  Check calibration accuracy, or adjust `safe_offset_min` / `safe_offset_max`.

### Probe health warnings

The health tracker will warn you about:
- **Degrading trend** -- probe spread is increasing over time.  Check mechanical components.
- **High retry rate** -- over 50% of sessions need retries.  Investigate probe repeatability.
- **High spread** -- recent spread exceeded 0.040mm.  Probe may need maintenance.

Run `AUTO_Z_TAP_HEALTH` for a full report.

## Moonraker / Mainsail Setup

The installer writes and includes an update manager section so this repo appears in Mainsail's update panel.  Template is in `examples/auto_z_tap_update_manager.conf` for manual setup.

All plugin status data is available via Moonraker's `printer.objects.auto_z_tap` query, including probe type, health statistics, confidence score, and calibration state.

## Notes

- This plugin requires a working probe setup in Klipper.
- Keep your reference XY position stable and representative of your print area.
- Re-run calibration after major hardware changes (nozzle, toolhead, probe mount, bed stack, gantry work).
- Calibration only needs to be done once.  After that, `AUTO_Z_TAP` handles everything automatically.
