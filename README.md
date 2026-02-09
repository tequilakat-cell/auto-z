# auto-z

`auto-z` is a Klipper extension that automates reproducible Z-offset setup for probe-based printers (including Chaotic Lab CNC Tap V2 style setups).

It adds one primary command, `AUTO_Z_TAP`, that you can run in `START_PRINT` to:
1. probe a reference point,
2. compute deterministic adjustments,
3. apply `SET_GCODE_OFFSET`,
4. persist last run values.

Initial setup is interactive and paper-based (`TESTZ` / `ACCEPT`) so you only need to do the paper test once.

## What This Plugin Provides

- Interactive one-time calibration with your paper test.
- Automatic repeatable runtime offset per print.
- Multi-sample guarded probing (spread check + retries).
- Configurable drift guard (`MAX_DRIFT`) to fail fast on bad probe behavior.
- Profile system (`[auto_z_tap_adjustment <name>]`) for material/surface/nozzle-specific compensation.
- Temperature and first-layer-aware adjustments.
- Save/restore state using Klipper `[save_variables]`.
- `install.sh` integration for Klipper module install and Moonraker update manager registration.

## Install

From this repo:

```bash
./install.sh
```

The script will:
- symlink `klippy/extras/auto_z_tap.py` into your Klipper `klippy/extras/` directory,
- install sample config as `~/printer_data/config/auto_z_tap.cfg` (if not already present),
- write `~/printer_data/config/auto_z_tap_update_manager.conf`,
- add `[include auto_z_tap_update_manager.conf]` to `moonraker.conf`.

After install, restart Klipper and Moonraker.

## printer.cfg Setup

Add (or include) these sections:

```ini
[save_variables]
filename: ~/printer_data/config/variables.cfg

[auto_z_tap]
probe_object: probe
reference_xy_position: 150, 150
auto_home: True
home_command: G28
clear_bed_mesh_before_probe: True
safe_z: 10
probe_start_z: 8
travel_speed: 200
probe_samples: 5
probe_samples_result: median
max_probe_spread: 0.020
probe_retries: 2
sample_retract_dist: 1.5
max_drift: 1.0
calibration_z_hop: 5.0
require_save_variables: True
variable_prefix: auto_z_tap

global_offset: 0.000
bed_temp_coeff: 0.00000
hotend_temp_coeff: 0.00000
chamber_temp_coeff: 0.00000
first_layer_coeff: 0.00000
first_layer_reference: 0.20

default_profile: pei
chamber_sensor: temperature_sensor chamber
max_total_adjustment: 0.600
apply_move: False
persist_last_run: True
report_breakdown: True
```

Example adjustment profiles:

```ini
[auto_z_tap_adjustment pei]
priority: 10
build_surface: pei
offset: 0.000
bed_temp_coeff: 0.00015
hotend_temp_coeff: 0.00005

[auto_z_tap_adjustment textured]
priority: 20
build_surface: textured
offset: -0.015

[auto_z_tap_adjustment pla]
priority: 30
material: pla
offset: 0.000
bed_temp_coeff: 0.00008
hotend_temp_coeff: 0.00003
first_layer_coeff: -0.25000
first_layer_reference: 0.20

[auto_z_tap_adjustment petg]
priority: 40
material: petg
offset: 0.010
bed_temp_coeff: 0.00010
hotend_temp_coeff: 0.00002
first_layer_coeff: -0.20000
first_layer_reference: 0.24
```

## First-Time Interactive Calibration (paper test)

Run:

```gcode
AUTO_Z_TAP CALIBRATE=1
```

Flow:
1. homes if needed,
2. probes reference point,
3. starts Klipper manual probe helper,
4. you use `TESTZ` and `ACCEPT` with paper drag,
5. plugin stores calibration and applies runtime offset immediately.

## Normal Per-Print Use (single command)

In `START_PRINT` call one line:

```gcode
AUTO_Z_TAP MATERIAL={params.MATERIAL|default("pla")|lower} BUILD_SURFACE={params.SURFACE|default("pei")|lower} BED_TEMP={params.BED|float} HOTEND_TEMP={params.HOTEND|float} FIRST_LAYER_HEIGHT={params.FIRST_LAYER_HEIGHT|default(0.2)|float}
```

You can also call simply:

```gcode
AUTO_Z_TAP
```

if all environment values are already represented by defaults/profile logic.

## Command Reference

### `AUTO_Z_TAP`

Main automatic command.

Common parameters:
- `CALIBRATE=1`: start interactive initial paper calibration.
- `CALIBRATE_IF_NEEDED=1`: automatically enter calibration flow if not calibrated.
- `PROFILE=<name>`: explicit profile(s), comma separated allowed.
- `PROFILES=<name1,name2>`: additional profile list.
- `AUTO_MATCH=1|0`: enable/disable automatic profile matching.
- `MATERIAL=<tag>` / `BUILD_SURFACE=<tag>` / `NOZZLE=<tag>`: matching keys.
- `BED_TEMP=<float>` / `HOTEND_TEMP=<float>` / `CHAMBER_TEMP=<float>`.
- `FIRST_LAYER_HEIGHT=<float>`.
- `EXTRA=<float>`: manual per-print additive trim.
- `MAX_ADJUST=<float>`: cap adjustment magnitude (non-probe terms).
- `MAX_DRIFT=<float>`: cap probe drift from reference.
- `SAMPLES=<int>` / `RETRIES=<int>` / `MAX_SPREAD=<float>`.
- `SAMPLES_RESULT=median|average`.
- `PROBE_SPEED=<float>` / `LIFT_SPEED=<float>` / `SAMPLE_RETRACT_DIST=<float>`.
- `MOVE=1|0`: pass-through for `SET_GCODE_OFFSET` move behavior.
- `MOVE_SPEED=<float>`: speed used if `MOVE=1`.
- `SAVE=1|0`: persist last run values.
- `CLEAR=1`: clear calibration.

### `AUTO_Z_TAP_CALIBRATE`

Shortcut for interactive calibration.

### `AUTO_Z_TAP_STATUS`

Shows loaded calibration and last run state.

### `AUTO_Z_TAP_CLEAR`

Clears calibration and persisted runtime state.

## Moonraker / Mainsail Update Manager

The installer writes and includes an update manager section so this repo appears in Mainsail's update panel.

Template is in `examples/auto_z_tap_update_manager.conf` if you prefer manual setup.

## Notes

- This plugin expects a working probe setup in Klipper (`probe_object`, default `probe`).
- Keep your reference XY stable and representative of your print area.
- Re-run calibration after major hardware changes (nozzle, toolhead, probe mount, bed stack, gantry work).
