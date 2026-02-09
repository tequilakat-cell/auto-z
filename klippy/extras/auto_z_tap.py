# Auto Z-offset manager for probe-based printers
#
# Supports CNC Tap, Microprobe, BLTouch, inductive probes, and generic setups.
# One-time interactive paper calibration, then automatic per-print Z offset.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import math
from . import manual_probe
from . import probe as probe_module


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _normalize_token(value):
    if value is None:
        return ""
    return str(value).strip().lower()


def _split_csv(value):
    if not value:
        return []
    return [_normalize_token(v) for v in str(value).split(',') if v.strip()]


def _parse_float_list(value):
    if not value:
        return []
    return [float(v.strip()) for v in str(value).split(',') if v.strip()]


# ---------------------------------------------------------------------------
# Probe-type preset system
# ---------------------------------------------------------------------------

class ProbePreset:
    """Sensible default parameters for a specific probe type.

    Users set probe_type in [auto_z_tap] and get tuned defaults without
    needing to configure every parameter individually.  Every preset value
    can still be overridden by an explicit config entry or gcode parameter.
    """

    PRESETS = {
        'tap': {
            'description': 'CNC Tap (nozzle-as-probe)',
            'probe_samples': 5,
            'max_probe_spread': 0.015,
            'probe_retries': 3,
            'sample_retract_dist': 1.0,
            'bed_temp_coeff': 0.00010,
            'hotend_temp_coeff': 0.00005,
            'chamber_temp_coeff': 0.00005,
            'warmup_taps': 2,
            'thermal_soak': True,
            'thermal_soak_threshold': 0.3,
            'thermal_soak_timeout': 300,
            'thermal_soak_sensors': 'heater_bed',
            'max_drift': 0.80,
            'max_total_adjustment': 0.500,
            'safe_offset_min': -0.500,
            'safe_offset_max': 0.500,
        },
        'microprobe': {
            'description': 'Microprobe / Klicky style',
            'probe_samples': 5,
            'max_probe_spread': 0.020,
            'probe_retries': 2,
            'sample_retract_dist': 1.5,
            'bed_temp_coeff': 0.00005,
            'hotend_temp_coeff': 0.00002,
            'chamber_temp_coeff': 0.00003,
            'warmup_taps': 0,
            'thermal_soak': False,
            'thermal_soak_threshold': 0.5,
            'thermal_soak_timeout': 180,
            'thermal_soak_sensors': 'heater_bed',
            'max_drift': 1.0,
            'max_total_adjustment': 0.600,
            'safe_offset_min': -0.600,
            'safe_offset_max': 0.600,
        },
        'bltouch': {
            'description': 'BLTouch / servo deploy probe',
            'probe_samples': 7,
            'max_probe_spread': 0.030,
            'probe_retries': 3,
            'sample_retract_dist': 2.0,
            'bed_temp_coeff': 0.00003,
            'hotend_temp_coeff': 0.00001,
            'chamber_temp_coeff': 0.00002,
            'warmup_taps': 1,
            'thermal_soak': False,
            'thermal_soak_threshold': 0.5,
            'thermal_soak_timeout': 120,
            'thermal_soak_sensors': 'heater_bed',
            'max_drift': 1.2,
            'max_total_adjustment': 0.700,
            'safe_offset_min': -0.700,
            'safe_offset_max': 0.700,
        },
        'inductive': {
            'description': 'Inductive / capacitive proximity probe',
            'probe_samples': 7,
            'max_probe_spread': 0.025,
            'probe_retries': 2,
            'sample_retract_dist': 2.0,
            'bed_temp_coeff': 0.00080,
            'hotend_temp_coeff': 0.00005,
            'chamber_temp_coeff': 0.00010,
            'warmup_taps': 1,
            'thermal_soak': True,
            'thermal_soak_threshold': 0.2,
            'thermal_soak_timeout': 600,
            'thermal_soak_sensors': 'heater_bed',
            'max_drift': 1.5,
            'max_total_adjustment': 0.800,
            'safe_offset_min': -0.800,
            'safe_offset_max': 0.800,
        },
        'generic': {
            'description': 'Generic / unknown probe type',
            'probe_samples': 5,
            'max_probe_spread': 0.020,
            'probe_retries': 2,
            'sample_retract_dist': 1.5,
            'bed_temp_coeff': 0.,
            'hotend_temp_coeff': 0.,
            'chamber_temp_coeff': 0.,
            'warmup_taps': 0,
            'thermal_soak': False,
            'thermal_soak_threshold': 0.3,
            'thermal_soak_timeout': 300,
            'thermal_soak_sensors': 'heater_bed',
            'max_drift': 1.0,
            'max_total_adjustment': 0.600,
            'safe_offset_min': -0.500,
            'safe_offset_max': 0.500,
        },
    }

    def __init__(self, probe_type):
        self.probe_type = probe_type
        self.values = dict(self.PRESETS.get(probe_type,
                                            self.PRESETS['generic']))

    def get(self, key, fallback=None):
        return self.values.get(key, fallback)

    @classmethod
    def known_types(cls):
        return sorted(cls.PRESETS.keys())


# ---------------------------------------------------------------------------
# Thermal soak helper
# ---------------------------------------------------------------------------

class ThermalStabilizer:
    """Waits for temperature rate-of-change to drop below a threshold.

    Critical for CNC Tap (thermal expansion shifts trigger point) and
    inductive probes (trigger distance is temperature-dependent).
    """

    def __init__(self, printer, reactor, gcode):
        self.printer = printer
        self.reactor = reactor
        self.gcode = gcode

    def wait_for_thermal_stability(self, sensor_names, threshold_per_min,
                                    timeout_sec, check_interval=2.0):
        start_time = self.reactor.monotonic()
        prev_temps = {}
        prev_time = start_time
        last_report = start_time

        for name in sensor_names:
            temp = self._read_sensor_temp(name)
            if temp is not None:
                prev_temps[name] = temp

        if not prev_temps:
            self.gcode.respond_info(
                "AUTO_Z_TAP: No valid sensors for thermal soak. Skipping.")
            return True

        self.gcode.respond_info(
            "AUTO_Z_TAP: Waiting for thermal stability "
            "(threshold %.2fC/min, timeout %.0fs)..."
            % (threshold_per_min, timeout_sec))

        while True:
            self.reactor.pause(self.reactor.monotonic() + check_interval)
            now = self.reactor.monotonic()
            elapsed = now - start_time

            if elapsed > timeout_sec:
                return False

            current_temps = {}
            all_stable = True
            for name in prev_temps:
                temp = self._read_sensor_temp(name)
                if temp is None:
                    continue
                current_temps[name] = temp
                minutes = (now - prev_time) / 60.0
                if minutes > 0.01:
                    rate = abs(temp - prev_temps[name]) / minutes
                    if rate > threshold_per_min:
                        all_stable = False

            if now - last_report >= 30.0:
                parts = []
                for name in sorted(current_temps):
                    minutes = (now - prev_time) / 60.0
                    rate = 0.
                    if minutes > 0.01 and name in prev_temps:
                        rate = abs(current_temps[name]
                                   - prev_temps[name]) / minutes
                    parts.append("%s: %.1fC (%.2fC/min)"
                                 % (name, current_temps[name], rate))
                self.gcode.respond_info(
                    "AUTO_Z_TAP: Thermal soak %.0fs/%.0fs - %s"
                    % (elapsed, timeout_sec, ', '.join(parts)))
                last_report = now

            if all_stable and elapsed >= check_interval * 2:
                self.gcode.respond_info(
                    "AUTO_Z_TAP: Thermal stability reached after %.0fs"
                    % (elapsed,))
                return True

            prev_temps = dict(current_temps)
            prev_time = now

    def _read_sensor_temp(self, sensor_name):
        obj = self.printer.lookup_object(sensor_name, None)
        if obj is None:
            return None
        try:
            status = obj.get_status(self.reactor.monotonic())
            return status.get('temperature')
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Probe health tracker
# ---------------------------------------------------------------------------

class ProbeHealthTracker:
    """Maintains rolling statistics on probe performance across runs.

    Tracks spread, drift, and retry counts.  Detects degradation trends,
    provides a confidence score, and can suggest adaptive sample counts.
    """

    MAX_HISTORY = 50

    def __init__(self, printer, gcode, variable_prefix):
        self.printer = printer
        self.gcode = gcode
        self.variable_prefix = variable_prefix
        self.history = []

    def load(self):
        save_obj = self.printer.lookup_object('save_variables', None)
        if save_obj is None:
            return
        try:
            variables = save_obj.get_status(0.).get('variables', {})
            key = '%s_probe_history' % (self.variable_prefix,)
            stored = variables.get(key)
            if stored and isinstance(stored, list):
                self.history = stored[-self.MAX_HISTORY:]
        except Exception:
            pass

    def record_session(self, probe_z, spread, drift, samples,
                       retries_used, bed_temp=None, hotend_temp=None):
        entry = {
            'z': round(float(probe_z), 6),
            's': round(float(spread), 6),
            'd': round(float(drift), 6),
            'n': int(samples),
            'r': int(retries_used),
        }
        if bed_temp is not None:
            entry['bt'] = round(float(bed_temp), 1)
        if hotend_temp is not None:
            entry['ht'] = round(float(hotend_temp), 1)
        self.history.append(entry)
        if len(self.history) > self.MAX_HISTORY:
            self.history = self.history[-self.MAX_HISTORY:]
        self._save()

    def get_statistics(self):
        if not self.history:
            return None
        spreads = [h['s'] for h in self.history]
        drifts = [h['d'] for h in self.history]
        retries = [h['r'] for h in self.history]
        return {
            'session_count': len(self.history),
            'avg_spread': sum(spreads) / len(spreads),
            'max_spread': max(spreads),
            'min_spread': min(spreads),
            'avg_drift': sum(drifts) / len(drifts),
            'max_abs_drift': max(abs(d) for d in drifts),
            'avg_retries': sum(retries) / len(retries),
            'retry_rate': sum(1 for r in retries if r > 0) / len(retries),
            'recent_trend': self._compute_trend(spreads),
        }

    def get_confidence(self):
        stats = self.get_statistics()
        if stats is None:
            return 'unknown', 0.0
        score = 1.0
        if stats['avg_spread'] > 0.015:
            score -= 0.2
        if stats['retry_rate'] > 0.3:
            score -= 0.2
        if stats['recent_trend'] == 'degrading':
            score -= 0.3
        if stats['max_abs_drift'] > 0.5:
            score -= 0.1
        score = max(0.0, min(1.0, score))
        if score >= 0.8:
            level = 'high'
        elif score >= 0.4:
            level = 'medium'
        else:
            level = 'low'
        return level, score

    def check_health(self):
        warnings = []
        stats = self.get_statistics()
        if stats is None:
            return warnings
        if stats['recent_trend'] == 'degrading':
            warnings.append(
                "Probe spread is trending upward over recent sessions. "
                "Check probe mount and wiring.")
        if stats['retry_rate'] > 0.5:
            warnings.append(
                "Over 50%% of recent probe sessions required retries. "
                "Check probe repeatability.")
        last_5 = self.history[-5:] if len(self.history) >= 5 else self.history
        if last_5 and max(h['s'] for h in last_5) > 0.040:
            warnings.append(
                "Recent probe spread exceeded 0.040mm. "
                "Probe may need maintenance.")
        return warnings

    def suggest_sample_count(self, default_samples):
        level, score = self.get_confidence()
        if score >= 0.9 and len(self.history) >= 20:
            return max(3, default_samples - 2)
        elif score < 0.4:
            return min(10, default_samples + 2)
        return default_samples

    def clear(self):
        self.history = []
        self._save()

    def _compute_trend(self, values):
        if len(values) < 10:
            return 'insufficient_data'
        half = len(values) // 2
        recent_avg = sum(values[half:]) / len(values[half:])
        older_avg = sum(values[:half]) / half
        if older_avg > 0 and recent_avg > older_avg * 1.3:
            return 'degrading'
        elif older_avg > 0 and recent_avg < older_avg * 0.7:
            return 'improving'
        return 'stable'

    def _save(self):
        save_obj = self.printer.lookup_object('save_variables', None)
        if save_obj is None:
            return
        key = '%s_probe_history' % (self.variable_prefix,)
        try:
            self.gcode.run_script_from_command(
                'SAVE_VARIABLE VARIABLE=%s VALUE="%s"'
                % (key, repr(self.history)))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Adjustment profiles
# ---------------------------------------------------------------------------

class AdjustmentProfile:
    def __init__(self, config):
        section_name = config.get_name().split(' ', 1)
        if len(section_name) != 2 or not section_name[1].strip():
            raise config.error(
                "auto_z_tap adjustment sections must be named like "
                "[auto_z_tap_adjustment my_profile]")
        self.name = _normalize_token(section_name[1])
        self.priority = config.getint('priority', 100)
        self.enabled = config.getboolean('enabled', True)

        # Match filters
        self.material = _normalize_token(config.get('material', ''))
        self.build_surface = _normalize_token(config.get('build_surface', ''))
        self.nozzle = _normalize_token(config.get('nozzle', ''))
        self.probe_type_filter = _normalize_token(
            config.get('probe_type', ''))

        # Static and dynamic modifiers
        self.offset = config.getfloat('offset', 0.)
        self.bed_temp_coeff = config.getfloat('bed_temp_coeff', 0.)
        self.hotend_temp_coeff = config.getfloat('hotend_temp_coeff', 0.)
        self.chamber_temp_coeff = config.getfloat('chamber_temp_coeff', 0.)
        self.first_layer_coeff = config.getfloat('first_layer_coeff', 0.)

        # Polynomial temperature coefficients (override linear if set)
        self.bed_temp_poly = _parse_float_list(
            config.get('bed_temp_poly', ''))
        self.hotend_temp_poly = _parse_float_list(
            config.get('hotend_temp_poly', ''))
        self.chamber_temp_poly = _parse_float_list(
            config.get('chamber_temp_poly', ''))

        # Optional references
        self.bed_temp_reference = config.getfloat('bed_temp_reference', None)
        self.hotend_temp_reference = config.getfloat(
            'hotend_temp_reference', None)
        self.chamber_temp_reference = config.getfloat(
            'chamber_temp_reference', None)
        self.first_layer_reference = config.getfloat(
            'first_layer_reference', None)

    def matches(self, material, build_surface, nozzle, probe_type=''):
        if self.probe_type_filter and self.probe_type_filter != probe_type:
            return False
        if self.material and self.material != material:
            return False
        if self.build_surface and self.build_surface != build_surface:
            return False
        if self.nozzle and self.nozzle != nozzle:
            return False
        return True

    def calculate(self, env, calibration_refs, global_refs):
        total = self.offset
        details = []
        if self.offset:
            details.append(("offset", self.offset))

        bed_temp = env.get('bed_temp')
        hotend_temp = env.get('hotend_temp')
        chamber_temp = env.get('chamber_temp')
        first_layer_height = env.get('first_layer_height')

        bed_ref = self.bed_temp_reference
        if bed_ref is None:
            bed_ref = global_refs.get('bed_temp_reference')
        if bed_ref is None:
            bed_ref = calibration_refs.get('bed_temp_reference')

        hotend_ref = self.hotend_temp_reference
        if hotend_ref is None:
            hotend_ref = global_refs.get('hotend_temp_reference')
        if hotend_ref is None:
            hotend_ref = calibration_refs.get('hotend_temp_reference')

        chamber_ref = self.chamber_temp_reference
        if chamber_ref is None:
            chamber_ref = global_refs.get('chamber_temp_reference')
        if chamber_ref is None:
            chamber_ref = calibration_refs.get('chamber_temp_reference')

        first_layer_ref = self.first_layer_reference
        if first_layer_ref is None:
            first_layer_ref = global_refs.get('first_layer_reference')

        # Bed temperature compensation
        if bed_temp is not None and bed_ref is not None:
            if self.bed_temp_poly:
                val = _compute_poly(self.bed_temp_poly, bed_temp, bed_ref)
                total += val
                details.append((
                    "bed_temp_poly", val,
                    "poly(%s) T=%.2f ref=%.2f" % (
                        ','.join('%.8f' % c for c in self.bed_temp_poly),
                        bed_temp, bed_ref)))
            elif self.bed_temp_coeff:
                val = (bed_temp - bed_ref) * self.bed_temp_coeff
                total += val
                details.append((
                    "bed_temp", val,
                    "(bed %.2f - ref %.2f) * %.6f" % (
                        bed_temp, bed_ref, self.bed_temp_coeff)))

        # Hotend temperature compensation
        if hotend_temp is not None and hotend_ref is not None:
            if self.hotend_temp_poly:
                val = _compute_poly(
                    self.hotend_temp_poly, hotend_temp, hotend_ref)
                total += val
                details.append((
                    "hotend_temp_poly", val,
                    "poly(%s) T=%.2f ref=%.2f" % (
                        ','.join('%.8f' % c for c in self.hotend_temp_poly),
                        hotend_temp, hotend_ref)))
            elif self.hotend_temp_coeff:
                val = (hotend_temp - hotend_ref) * self.hotend_temp_coeff
                total += val
                details.append((
                    "hotend_temp", val,
                    "(hotend %.2f - ref %.2f) * %.6f" % (
                        hotend_temp, hotend_ref, self.hotend_temp_coeff)))

        # Chamber temperature compensation
        if chamber_temp is not None and chamber_ref is not None:
            if self.chamber_temp_poly:
                val = _compute_poly(
                    self.chamber_temp_poly, chamber_temp, chamber_ref)
                total += val
                details.append((
                    "chamber_temp_poly", val,
                    "poly(%s) T=%.2f ref=%.2f" % (
                        ','.join('%.8f' % c for c in self.chamber_temp_poly),
                        chamber_temp, chamber_ref)))
            elif self.chamber_temp_coeff:
                val = (chamber_temp - chamber_ref) * self.chamber_temp_coeff
                total += val
                details.append((
                    "chamber_temp", val,
                    "(chamber %.2f - ref %.2f) * %.6f" % (
                        chamber_temp, chamber_ref, self.chamber_temp_coeff)))

        # First layer height compensation
        if (self.first_layer_coeff and first_layer_height is not None
                and first_layer_ref is not None):
            val = ((first_layer_height - first_layer_ref)
                   * self.first_layer_coeff)
            total += val
            details.append((
                "first_layer", val,
                "(layer %.3f - ref %.3f) * %.6f" % (
                    first_layer_height, first_layer_ref,
                    self.first_layer_coeff)))

        return total, details


def _compute_poly(coeffs, temp, ref):
    """Evaluate polynomial: c1*(T-Tref) + c2*(T-Tref)^2 + ..."""
    delta = temp - ref
    total = 0.
    for i, c in enumerate(coeffs):
        total += c * (delta ** (i + 1))
    return total


# ---------------------------------------------------------------------------
# Main plugin
# ---------------------------------------------------------------------------

class AutoZTap:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        # Probe type system
        self.probe_type = _normalize_token(
            config.get('probe_type', 'generic'))
        if self.probe_type not in ProbePreset.known_types():
            raise config.error(
                "Unknown probe_type '%s'. Valid types: %s"
                % (self.probe_type, ', '.join(ProbePreset.known_types())))
        self.preset = ProbePreset(self.probe_type)

        # Core behavior
        self.probe_object_name = config.get('probe_object', 'probe')
        self.auto_home = config.getboolean('auto_home', True)
        self.home_command = config.get('home_command', 'G28')
        self.clear_bed_mesh_before_probe = config.getboolean(
            'clear_bed_mesh_before_probe', True)
        self.require_save_variables = config.getboolean(
            'require_save_variables', True)

        # Motion / probing (preset-aware defaults)
        self.safe_z = config.getfloat('safe_z', 10., above=0.)
        self.probe_start_z = config.getfloat('probe_start_z', 8., above=0.)
        self.travel_speed = config.getfloat('travel_speed', 150., above=0.)
        self.probe_speed = config.getfloat('probe_speed', None, above=0.)
        self.lift_speed = config.getfloat('lift_speed', None, above=0.)
        self.sample_retract_dist = config.getfloat(
            'sample_retract_dist',
            self.preset.get('sample_retract_dist', 1.5), above=0.)

        self.probe_samples = config.getint(
            'probe_samples',
            self.preset.get('probe_samples', 5), minval=1)
        self.probe_samples_result = config.getchoice(
            'probe_samples_result', ['average', 'median'], 'median')
        self.max_probe_spread = config.getfloat(
            'max_probe_spread',
            self.preset.get('max_probe_spread', 0.020), minval=0.)
        self.probe_retries = config.getint(
            'probe_retries',
            self.preset.get('probe_retries', 2), minval=0)
        self.max_drift = config.getfloat(
            'max_drift',
            self.preset.get('max_drift', 1.0), minval=0.)
        self.calibration_z_hop = config.getfloat(
            'calibration_z_hop', 5.0, above=0.)

        # Warmup taps
        self.warmup_taps = config.getint(
            'warmup_taps',
            self.preset.get('warmup_taps', 0), minval=0)

        # Thermal soak
        self.thermal_soak_enabled = config.getboolean(
            'thermal_soak',
            self.preset.get('thermal_soak', False))
        self.thermal_soak_threshold = config.getfloat(
            'thermal_soak_threshold',
            self.preset.get('thermal_soak_threshold', 0.3), above=0.)
        self.thermal_soak_timeout = config.getfloat(
            'thermal_soak_timeout',
            self.preset.get('thermal_soak_timeout', 300), above=0.)
        self.thermal_soak_sensors_config = config.get(
            'thermal_soak_sensors',
            self.preset.get('thermal_soak_sensors', 'heater_bed'))

        # Safety limits
        self.safe_offset_min = config.getfloat(
            'safe_offset_min',
            self.preset.get('safe_offset_min', -0.500))
        self.safe_offset_max = config.getfloat(
            'safe_offset_max',
            self.preset.get('safe_offset_max', 0.500))

        # Base references and adjustments
        self.global_offset = config.getfloat('global_offset', 0.)
        self.bed_temp_coeff = config.getfloat(
            'bed_temp_coeff',
            self.preset.get('bed_temp_coeff', 0.))
        self.hotend_temp_coeff = config.getfloat(
            'hotend_temp_coeff',
            self.preset.get('hotend_temp_coeff', 0.))
        self.chamber_temp_coeff = config.getfloat(
            'chamber_temp_coeff',
            self.preset.get('chamber_temp_coeff', 0.))
        self.first_layer_coeff = config.getfloat('first_layer_coeff', 0.)

        # Polynomial temperature coefficients (override linear if set)
        self.bed_temp_poly = _parse_float_list(
            config.get('bed_temp_poly', ''))
        self.hotend_temp_poly = _parse_float_list(
            config.get('hotend_temp_poly', ''))
        self.chamber_temp_poly = _parse_float_list(
            config.get('chamber_temp_poly', ''))

        self.bed_temp_reference = config.getfloat('bed_temp_reference', None)
        self.hotend_temp_reference = config.getfloat(
            'hotend_temp_reference', None)
        self.chamber_temp_reference = config.getfloat(
            'chamber_temp_reference', None)
        self.first_layer_reference = config.getfloat(
            'first_layer_reference', 0.20)

        self.default_profile = _normalize_token(
            config.get('default_profile', ''))
        self.chamber_sensor = _normalize_token(
            config.get('chamber_sensor', ''))
        self.max_total_adjustment = config.getfloat(
            'max_total_adjustment',
            self.preset.get('max_total_adjustment', 0.600), minval=0.)
        self.apply_move = config.getboolean('apply_move', False)
        self.persist_last_run = config.getboolean('persist_last_run', True)
        self.report_breakdown = config.getboolean('report_breakdown', True)

        # Calibration enhancements
        self.calibration_validate = config.getboolean(
            'calibration_validate', False)

        # Health tracking
        self.probe_health_tracking = config.getboolean(
            'probe_health_tracking', True)
        self.adaptive_samples = config.getboolean('adaptive_samples', False)

        # Reference probing position
        configured_ref_xy = config.getfloatlist(
            'reference_xy_position', None, count=2)
        if configured_ref_xy is not None:
            self.reference_xy = tuple(configured_ref_xy)
        else:
            self.reference_xy = self._derive_reference_xy(config)

        # Variable naming
        raw_prefix = _normalize_token(
            config.get('variable_prefix', 'auto_z_tap'))
        clean = []
        for ch in raw_prefix:
            if ch.isalnum() or ch == '_':
                clean.append(ch)
            else:
                clean.append('_')
        self.variable_prefix = ''.join(clean).strip('_') or 'auto_z_tap'

        # Adjustment profiles
        self.adjustments = {}
        for aconfig in config.get_prefix_sections('auto_z_tap_adjustment '):
            profile = AdjustmentProfile(aconfig)
            if profile.name in self.adjustments:
                raise config.error(
                    "Duplicate auto_z_tap adjustment profile: %s"
                    % (profile.name,))
            self.adjustments[profile.name] = profile

        # Runtime state
        self.probe = None
        self.toolhead = None
        self.thermal_stabilizer = None
        self.health_tracker = None
        self.pending_calibration = None
        self.status = {
            'calibrated': False,
            'reference_probe_z': 0.0,
            'paper_delta': 0.0,
            'reference_x': (self.reference_xy[0]
                            if self.reference_xy else 0.0),
            'reference_y': (self.reference_xy[1]
                            if self.reference_xy else 0.0),
            'calibration_bed_temp': None,
            'calibration_hotend_temp': None,
            'calibration_chamber_temp': None,
            'calibration_probe_type': None,
            'last_probe_z': None,
            'last_probe_spread': None,
            'last_offset': None,
            'last_drift': None,
            'last_profiles': [],
            'calibration_in_progress': False,
        }

        self.printer.register_event_handler(
            'klippy:connect', self._handle_connect)

        self.gcode.register_command(
            'AUTO_Z_TAP', self.cmd_AUTO_Z_TAP,
            desc=self.cmd_AUTO_Z_TAP_help)
        self.gcode.register_command(
            'AUTO_Z_TAP_CALIBRATE', self.cmd_AUTO_Z_TAP_CALIBRATE,
            desc=self.cmd_AUTO_Z_TAP_CALIBRATE_help)
        self.gcode.register_command(
            'AUTO_Z_TAP_STATUS', self.cmd_AUTO_Z_TAP_STATUS,
            desc=self.cmd_AUTO_Z_TAP_STATUS_help)
        self.gcode.register_command(
            'AUTO_Z_TAP_CLEAR', self.cmd_AUTO_Z_TAP_CLEAR,
            desc=self.cmd_AUTO_Z_TAP_CLEAR_help)
        self.gcode.register_command(
            'AUTO_Z_TAP_HEALTH', self.cmd_AUTO_Z_TAP_HEALTH,
            desc=self.cmd_AUTO_Z_TAP_HEALTH_help)
        self.gcode.register_command(
            'AUTO_Z_TAP_PROBE_TEST', self.cmd_AUTO_Z_TAP_PROBE_TEST,
            desc=self.cmd_AUTO_Z_TAP_PROBE_TEST_help)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _var_key(self, suffix):
        return '%s_%s' % (self.variable_prefix, suffix)

    def _derive_reference_xy(self, config):
        if (not config.has_section('stepper_x')
                or not config.has_section('stepper_y')):
            return None
        xcfg = config.getsection('stepper_x')
        ycfg = config.getsection('stepper_y')
        xmin = xcfg.getfloat('position_min', 0., note_valid=False)
        xmax = xcfg.getfloat('position_max')
        ymin = ycfg.getfloat('position_min', 0., note_valid=False)
        ymax = ycfg.getfloat('position_max')
        return ((xmin + xmax) / 2., (ymin + ymax) / 2.)

    def _eventtime(self):
        return self.reactor.monotonic()

    def _handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        self.probe = self.printer.lookup_object(
            self.probe_object_name, None)
        self.thermal_stabilizer = ThermalStabilizer(
            self.printer, self.reactor, self.gcode)
        if self.probe_health_tracking:
            self.health_tracker = ProbeHealthTracker(
                self.printer, self.gcode, self.variable_prefix)
            self.health_tracker.load()
        self._load_persistent_state()

    def _parse_bool(self, value):
        if isinstance(value, bool):
            return value
        if isinstance(value, (float, int)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in ('1', 'true', 'yes', 'on')
        return False

    def _read_saved_variables(self):
        save_obj = self.printer.lookup_object('save_variables', None)
        if save_obj is None:
            return {}
        try:
            return save_obj.get_status(
                self._eventtime()).get('variables', {})
        except Exception:
            return {}

    def _save_variable(self, key, value):
        save_obj = self.printer.lookup_object('save_variables', None)
        if save_obj is None:
            return False
        self.gcode.run_script_from_command(
            'SAVE_VARIABLE VARIABLE=%s VALUE=%s' % (key, repr(value)))
        return True

    def _load_persistent_state(self):
        values = self._read_saved_variables()
        self.status['calibrated'] = self._parse_bool(
            values.get(self._var_key('calibrated'), False))
        self.status['reference_probe_z'] = float(
            values.get(self._var_key('reference_probe_z'), 0.0))
        self.status['paper_delta'] = float(
            values.get(self._var_key('paper_delta'), 0.0))
        self.status['reference_x'] = float(values.get(
            self._var_key('reference_x'),
            self.reference_xy[0] if self.reference_xy else 0.0))
        self.status['reference_y'] = float(values.get(
            self._var_key('reference_y'),
            self.reference_xy[1] if self.reference_xy else 0.0))
        self.status['calibration_bed_temp'] = values.get(
            self._var_key('cal_bed_temp'), None)
        self.status['calibration_hotend_temp'] = values.get(
            self._var_key('cal_hotend_temp'), None)
        self.status['calibration_chamber_temp'] = values.get(
            self._var_key('cal_chamber_temp'), None)
        self.status['calibration_probe_type'] = values.get(
            self._var_key('cal_probe_type'), None)
        self.status['last_probe_z'] = values.get(
            self._var_key('last_probe_z'), None)
        self.status['last_probe_spread'] = values.get(
            self._var_key('last_probe_spread'), None)
        self.status['last_offset'] = values.get(
            self._var_key('last_offset'), None)
        self.status['last_drift'] = values.get(
            self._var_key('last_drift'), None)

    # ------------------------------------------------------------------
    # Precondition checks
    # ------------------------------------------------------------------

    def _require_probe(self, gcmd):
        if self.probe is None:
            raise gcmd.error(
                "AUTO_Z_TAP could not find probe object '%s'.\n"
                "Ensure your probe is configured in printer.cfg and set "
                "probe_object in [auto_z_tap] if it uses a non-default name."
                % (self.probe_object_name,))

    def _require_save_variables(self, gcmd):
        if not self.require_save_variables:
            return
        save_obj = self.printer.lookup_object('save_variables', None)
        if save_obj is None:
            raise gcmd.error(
                "[save_variables] is required by AUTO_Z_TAP.\n"
                "Add the following to printer.cfg and restart Klipper:\n"
                "  [save_variables]\n"
                "  filename: ~/printer_data/config/variables.cfg")

    def _axis_is_homed(self, axis):
        homed = self.toolhead.get_status(
            self._eventtime()).get('homed_axes', '')
        if isinstance(homed, str):
            return axis in homed.lower()
        return axis in homed

    def _ensure_homed(self, gcmd):
        need = ('x', 'y', 'z')
        if all(self._axis_is_homed(a) for a in need):
            return
        if not self.auto_home:
            raise gcmd.error(
                "AUTO_Z_TAP requires XYZ to be homed.\n"
                "Home the printer first or set auto_home: True in "
                "[auto_z_tap].")
        self.gcode.respond_info(
            "AUTO_Z_TAP: Axes not homed, running '%s'"
            % (self.home_command,))
        self.gcode.run_script_from_command(self.home_command)

    # ------------------------------------------------------------------
    # Motion helpers
    # ------------------------------------------------------------------

    def _resolve_reference_xy(self, gcmd):
        x = gcmd.get_float('X', None)
        y = gcmd.get_float('Y', None)
        if x is not None and y is not None:
            return x, y
        if self.reference_xy is not None:
            return self.reference_xy
        return self.status['reference_x'], self.status['reference_y']

    def _maybe_clear_bed_mesh(self):
        if not self.clear_bed_mesh_before_probe:
            return
        if self.printer.lookup_object('bed_mesh', None) is None:
            return
        try:
            self.gcode.run_script_from_command('BED_MESH_CLEAR')
        except Exception:
            pass

    def _manual_move(self, x=None, y=None, z=None, speed=None):
        self.toolhead.manual_move(
            [x, y, z],
            speed if speed is not None else self.travel_speed)

    def _raise_for_travel(self):
        cur = self.toolhead.get_position()
        if cur[2] < self.safe_z:
            self._manual_move(
                z=self.safe_z, speed=self._effective_lift_speed(None))

    def _move_to_reference(self, x, y):
        self._raise_for_travel()
        self._manual_move(x=x, y=y, speed=self.travel_speed)
        cur = self.toolhead.get_position()
        if cur[2] < self.probe_start_z:
            self._manual_move(
                z=self.probe_start_z,
                speed=self._effective_lift_speed(None))

    def _effective_probe_speed(self, gcmd):
        if gcmd is not None:
            ps = gcmd.get_float('PROBE_SPEED', None, above=0.)
            if ps is not None:
                return ps
        if self.probe_speed is not None:
            return self.probe_speed
        return None

    def _effective_lift_speed(self, gcmd):
        if gcmd is not None:
            ls = gcmd.get_float('LIFT_SPEED', None, above=0.)
            if ls is not None:
                return ls
        if self.lift_speed is not None:
            return self.lift_speed
        return self.travel_speed

    def _effective_retract(self, gcmd):
        if gcmd is not None:
            rd = gcmd.get_float('SAMPLE_RETRACT_DIST', None, above=0.)
            if rd is not None:
                return rd
        return self.sample_retract_dist

    # ------------------------------------------------------------------
    # Probing
    # ------------------------------------------------------------------

    def _probe_once(self, gcmd):
        params = {'SAMPLES': '1'}
        probe_speed = self._effective_probe_speed(gcmd)
        if probe_speed is not None:
            params['PROBE_SPEED'] = '%.6f' % (probe_speed,)
        lift_speed = self._effective_lift_speed(gcmd)
        if lift_speed is not None:
            params['LIFT_SPEED'] = '%.6f' % (lift_speed,)
        params['SAMPLE_RETRACT_DIST'] = '%.6f' % (
            self._effective_retract(gcmd),)
        probe_gcmd = self.gcode.create_gcode_command(
            'AUTO_Z_TAP_INTERNAL_PROBE',
            'AUTO_Z_TAP_INTERNAL_PROBE', params)
        return probe_module.run_single_probe(self.probe, probe_gcmd)

    def _run_warmup_taps(self, gcmd, x, y, count):
        """Execute throwaway probe taps to settle the probe mechanism."""
        self.gcode.respond_info(
            "AUTO_Z_TAP: Performing %d warm-up tap(s)..." % (count,))
        self._move_to_reference(x, y)
        for i in range(count):
            self._probe_once(gcmd)
            cur = self.toolhead.get_position()
            self._manual_move(
                z=cur[2] + self._effective_retract(gcmd),
                speed=self._effective_lift_speed(gcmd))
        self.gcode.respond_info("AUTO_Z_TAP: Warm-up complete")
        self._raise_for_travel()

    def _run_guarded_probe(self, gcmd, x, y, effective_samples=None):
        samples = effective_samples
        if samples is None:
            samples = gcmd.get_int('SAMPLES', self.probe_samples, minval=1)
        retries = gcmd.get_int('RETRIES', self.probe_retries, minval=0)
        spread_limit = gcmd.get_float(
            'MAX_SPREAD', self.max_probe_spread, minval=0.)
        method = _normalize_token(
            gcmd.get('SAMPLES_RESULT', self.probe_samples_result))
        if method not in ('average', 'median'):
            raise gcmd.error("SAMPLES_RESULT must be average or median")

        best = None
        last_spread = None
        for attempt in range(retries + 1):
            self._move_to_reference(x, y)
            samples_raw = []
            for idx in range(samples):
                pres = self._probe_once(gcmd)
                samples_raw.append(pres)
                if idx + 1 < samples:
                    cur = self.toolhead.get_position()
                    self._manual_move(
                        z=cur[2] + self._effective_retract(gcmd),
                        speed=self._effective_lift_speed(gcmd))
            z_values = [p.bed_z for p in samples_raw]
            spread = max(z_values) - min(z_values)
            last_spread = spread
            best = probe_module.calc_probe_z_average(samples_raw, method)
            if spread_limit <= 0. or spread <= spread_limit:
                return best, spread, attempt, samples
            self.gcode.respond_info(
                "AUTO_Z_TAP: Probe spread %.4fmm > limit %.4fmm "
                "(retry %d/%d)" % (spread, spread_limit,
                                   attempt + 1, retries))
            self._raise_for_travel()

        raise gcmd.error(
            "AUTO_Z_TAP probe repeatability failed: spread %.4fmm "
            "exceeds limit %.4fmm after %d retries.\n"
            "Possible causes:\n"
            "  - Debris on nozzle tip or build plate\n"
            "  - Loose probe mount or gantry\n"
            "  - Electrical noise on probe signal\n"
            "  - Bed not thermally stable (try thermal_soak: True)\n"
            "Try: Clean nozzle, check wiring, or increase "
            "max_probe_spread / probe_retries in [auto_z_tap]."
            % (last_spread or 0.0, spread_limit, retries))

    # ------------------------------------------------------------------
    # Thermal soak integration
    # ------------------------------------------------------------------

    def _resolve_soak_sensors(self):
        return _split_csv(self.thermal_soak_sensors_config)

    def _maybe_thermal_soak(self, gcmd):
        do_soak = gcmd.get_int(
            'THERMAL_SOAK',
            1 if self.thermal_soak_enabled else 0,
            minval=0, maxval=1)
        if not do_soak or self.thermal_stabilizer is None:
            return
        sensors = self._resolve_soak_sensors()
        if not sensors:
            return
        timeout = gcmd.get_float(
            'THERMAL_SOAK_TIMEOUT',
            self.thermal_soak_timeout, above=0.)
        stable = self.thermal_stabilizer.wait_for_thermal_stability(
            sensors, self.thermal_soak_threshold, timeout)
        if not stable:
            self.gcode.respond_info(
                "AUTO_Z_TAP: Thermal soak timed out after %.0fs. "
                "Proceeding with current temperatures." % (timeout,))

    # ------------------------------------------------------------------
    # Temperature reading
    # ------------------------------------------------------------------

    def _heater_temperature(self, object_name):
        if not object_name:
            return None
        obj = self.printer.lookup_object(object_name, None)
        if obj is None:
            return None
        status = obj.get_status(self._eventtime())
        target = status.get('target', 0.)
        temperature = status.get('temperature')
        if target is not None and target > 0.:
            return float(target)
        if temperature is not None:
            return float(temperature)
        return None

    def _resolve_hotend_object_name(self):
        toolhead_status = self.toolhead.get_status(self._eventtime())
        return toolhead_status.get('extruder', 'extruder')

    def _resolve_environment(self, gcmd):
        bed_temp = gcmd.get_float('BED_TEMP', None)
        if bed_temp is None:
            bed_temp = self._heater_temperature('heater_bed')

        hotend_temp = gcmd.get_float('HOTEND_TEMP', None)
        if hotend_temp is None:
            hotend_temp = self._heater_temperature(
                self._resolve_hotend_object_name())

        chamber_temp = gcmd.get_float('CHAMBER_TEMP', None)
        if chamber_temp is None:
            chamber_temp = self._heater_temperature(self.chamber_sensor)

        first_layer_height = gcmd.get_float('FIRST_LAYER_HEIGHT', None)

        return {
            'bed_temp': bed_temp,
            'hotend_temp': hotend_temp,
            'chamber_temp': chamber_temp,
            'first_layer_height': first_layer_height,
            'material': _normalize_token(gcmd.get('MATERIAL', '')),
            'build_surface': _normalize_token(gcmd.get('BUILD_SURFACE', '')),
            'nozzle': _normalize_token(gcmd.get('NOZZLE', '')),
        }

    # ------------------------------------------------------------------
    # References and profiles
    # ------------------------------------------------------------------

    def _calibration_refs(self):
        return {
            'bed_temp_reference': self.status.get('calibration_bed_temp'),
            'hotend_temp_reference': self.status.get(
                'calibration_hotend_temp'),
            'chamber_temp_reference': self.status.get(
                'calibration_chamber_temp'),
        }

    def _global_refs(self):
        return {
            'bed_temp_reference': self.bed_temp_reference,
            'hotend_temp_reference': self.hotend_temp_reference,
            'chamber_temp_reference': self.chamber_temp_reference,
            'first_layer_reference': self.first_layer_reference,
        }

    def _resolve_profiles(self, gcmd, env):
        requested = []
        requested.extend(_split_csv(gcmd.get('PROFILE', '')))
        requested.extend(_split_csv(gcmd.get('PROFILES', '')))

        if not requested and self.default_profile:
            requested.extend(_split_csv(self.default_profile))

        auto_match = gcmd.get_int('AUTO_MATCH', 1, minval=0, maxval=1)
        if auto_match:
            matches = [
                p for p in self.adjustments.values()
                if p.enabled and p.matches(
                    env.get('material', ''),
                    env.get('build_surface', ''),
                    env.get('nozzle', ''),
                    self.probe_type)]
            matches.sort(key=lambda p: (p.priority, p.name))
            for profile in matches:
                if profile.name not in requested:
                    requested.append(profile.name)

        resolved = []
        seen = set()
        for name in requested:
            if not name or name in seen:
                continue
            profile = self.adjustments.get(name)
            if profile is None:
                raise gcmd.error(
                    "Unknown AUTO_Z_TAP profile: %s\n"
                    "Available profiles: %s"
                    % (name, ', '.join(sorted(self.adjustments.keys()))))
            if not profile.enabled:
                continue
            resolved.append(profile)
            seen.add(name)
        return resolved

    # ------------------------------------------------------------------
    # Adjustment computation
    # ------------------------------------------------------------------

    def _compute_adjustment(self, gcmd, env):
        total = self.global_offset
        details = []
        if self.global_offset:
            details.append(("global_offset", self.global_offset, "config"))

        refs = self._calibration_refs()

        # Global bed temp compensation
        if env['bed_temp'] is not None:
            bed_ref = self.bed_temp_reference
            if bed_ref is None:
                bed_ref = refs.get('bed_temp_reference')
            if bed_ref is not None:
                if self.bed_temp_poly:
                    val = _compute_poly(
                        self.bed_temp_poly, env['bed_temp'], bed_ref)
                    total += val
                    details.append((
                        "global_bed_temp_poly", val,
                        "poly T=%.2f ref=%.2f" % (env['bed_temp'], bed_ref)))
                elif self.bed_temp_coeff:
                    val = (env['bed_temp'] - bed_ref) * self.bed_temp_coeff
                    total += val
                    details.append((
                        "global_bed_temp", val,
                        "(bed %.2f - ref %.2f) * %.6f"
                        % (env['bed_temp'], bed_ref, self.bed_temp_coeff)))

        # Global hotend temp compensation
        if env['hotend_temp'] is not None:
            hotend_ref = self.hotend_temp_reference
            if hotend_ref is None:
                hotend_ref = refs.get('hotend_temp_reference')
            if hotend_ref is not None:
                if self.hotend_temp_poly:
                    val = _compute_poly(
                        self.hotend_temp_poly, env['hotend_temp'], hotend_ref)
                    total += val
                    details.append((
                        "global_hotend_temp_poly", val,
                        "poly T=%.2f ref=%.2f"
                        % (env['hotend_temp'], hotend_ref)))
                elif self.hotend_temp_coeff:
                    val = ((env['hotend_temp'] - hotend_ref)
                           * self.hotend_temp_coeff)
                    total += val
                    details.append((
                        "global_hotend_temp", val,
                        "(hotend %.2f - ref %.2f) * %.6f"
                        % (env['hotend_temp'], hotend_ref,
                           self.hotend_temp_coeff)))

        # Global chamber temp compensation
        if env['chamber_temp'] is not None:
            chamber_ref = self.chamber_temp_reference
            if chamber_ref is None:
                chamber_ref = refs.get('chamber_temp_reference')
            if chamber_ref is not None:
                if self.chamber_temp_poly:
                    val = _compute_poly(
                        self.chamber_temp_poly,
                        env['chamber_temp'], chamber_ref)
                    total += val
                    details.append((
                        "global_chamber_temp_poly", val,
                        "poly T=%.2f ref=%.2f"
                        % (env['chamber_temp'], chamber_ref)))
                elif self.chamber_temp_coeff:
                    val = ((env['chamber_temp'] - chamber_ref)
                           * self.chamber_temp_coeff)
                    total += val
                    details.append((
                        "global_chamber_temp", val,
                        "(chamber %.2f - ref %.2f) * %.6f"
                        % (env['chamber_temp'], chamber_ref,
                           self.chamber_temp_coeff)))

        # Global first layer compensation
        if (self.first_layer_coeff
                and env['first_layer_height'] is not None
                and self.first_layer_reference is not None):
            val = ((env['first_layer_height'] - self.first_layer_reference)
                   * self.first_layer_coeff)
            total += val
            details.append((
                "global_first_layer", val,
                "(layer %.3f - ref %.3f) * %.6f"
                % (env['first_layer_height'], self.first_layer_reference,
                   self.first_layer_coeff)))

        # Profile adjustments
        profiles = self._resolve_profiles(gcmd, env)
        global_refs = self._global_refs()
        for profile in profiles:
            pval, pdetails = profile.calculate(env, refs, global_refs)
            total += pval
            details.append(
                ("profile:%s" % (profile.name,), pval, "profile total"))
            for d in pdetails:
                note = d[2] if len(d) == 3 else ""
                details.append((
                    "profile:%s:%s" % (profile.name, d[0]),
                    d[1], note))

        # EXTRA manual trim
        extra = gcmd.get_float('EXTRA', 0.)
        if extra:
            total += extra
            details.append(("extra", extra, "gcode EXTRA"))

        # Max adjustment cap
        max_adjust = gcmd.get_float(
            'MAX_ADJUST', self.max_total_adjustment, minval=0.)
        if max_adjust > 0. and abs(total) > max_adjust:
            raise gcmd.error(
                "AUTO_Z_TAP adjustment %.4fmm exceeds MAX_ADJUST %.4fmm.\n"
                "If this is expected, increase max_total_adjustment in "
                "[auto_z_tap] or pass MAX_ADJUST=<value>."
                % (total, max_adjust))

        return total, details, profiles

    # ------------------------------------------------------------------
    # Safety
    # ------------------------------------------------------------------

    def _validate_offset_safety(self, gcmd, final_offset):
        min_safe = gcmd.get_float('SAFE_OFFSET_MIN', self.safe_offset_min)
        max_safe = gcmd.get_float('SAFE_OFFSET_MAX', self.safe_offset_max)

        if final_offset < min_safe:
            raise gcmd.error(
                "AUTO_Z_TAP SAFETY: Offset %.4fmm is below safe minimum "
                "%.4fmm.\n"
                "This could cause nozzle collision with the bed.\n"
                "Check probe calibration or adjust safe_offset_min in "
                "[auto_z_tap]." % (final_offset, min_safe))

        if final_offset > max_safe:
            raise gcmd.error(
                "AUTO_Z_TAP SAFETY: Offset %.4fmm exceeds safe maximum "
                "%.4fmm.\n"
                "This could cause poor adhesion or air printing.\n"
                "Check probe calibration or adjust safe_offset_max in "
                "[auto_z_tap]." % (final_offset, max_safe))

    # ------------------------------------------------------------------
    # Offset application
    # ------------------------------------------------------------------

    def _apply_offset(self, gcmd, offset):
        move = gcmd.get_int(
            'MOVE', 1 if self.apply_move else 0, minval=0, maxval=1)
        cmd = 'SET_GCODE_OFFSET Z=%.6f MOVE=%d' % (offset, move)
        if move:
            move_speed = gcmd.get_float(
                'MOVE_SPEED', self.travel_speed, above=0.)
            cmd += ' MOVE_SPEED=%.6f' % (move_speed,)
        self.gcode.run_script_from_command(cmd)

    # ------------------------------------------------------------------
    # Summary output
    # ------------------------------------------------------------------

    def _summarize(self, result):
        lines = [
            "AUTO_Z_TAP applied (probe_type=%s):" % (self.probe_type,),
            "  reference_xy=%.3f,%.3f" % (
                result['reference_x'], result['reference_y']),
            "  probe_z=%.6f spread=%.4f drift=%.4f" % (
                result['probe_z'], result['probe_spread'], result['drift']),
            "  paper_delta=%.6f estimated_paper_z=%.6f" % (
                result['paper_delta'], result['estimated_paper_z']),
            "  adjustment_total=%.4f final_offset=%.6f" % (
                result['adjustment_total'], result['final_offset']),
        ]
        if result.get('warmup_taps'):
            lines.append("  warmup_taps=%d" % (result['warmup_taps'],))
        if result.get('thermal_soak'):
            lines.append("  thermal_soak=yes")
        if result['profiles']:
            lines.append("  profiles=%s" % (
                ','.join(result['profiles']),))

        if self.report_breakdown and result.get('details'):
            lines.append("  breakdown:")
            for entry in result['details']:
                name, value = entry[0], entry[1]
                note = entry[2] if len(entry) > 2 else ""
                if note:
                    lines.append(
                        "    - %s: %.6f (%s)" % (name, value, note))
                else:
                    lines.append("    - %s: %.6f" % (name, value))

        if self.health_tracker:
            level, score = self.health_tracker.get_confidence()
            if level != 'unknown':
                lines.append("  probe_confidence=%s (%.2f)" % (level, score))

        return '\n'.join(lines)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_calibration(self):
        self._save_variable(
            self._var_key('calibrated'),
            bool(self.status['calibrated']))
        self._save_variable(
            self._var_key('reference_probe_z'),
            float(self.status['reference_probe_z']))
        self._save_variable(
            self._var_key('paper_delta'),
            float(self.status['paper_delta']))
        self._save_variable(
            self._var_key('reference_x'),
            float(self.status['reference_x']))
        self._save_variable(
            self._var_key('reference_y'),
            float(self.status['reference_y']))
        self._save_variable(
            self._var_key('cal_probe_type'),
            self.probe_type)

        for src, key in (
                ('calibration_bed_temp', 'cal_bed_temp'),
                ('calibration_hotend_temp', 'cal_hotend_temp'),
                ('calibration_chamber_temp', 'cal_chamber_temp')):
            val = self.status.get(src)
            if val is not None:
                self._save_variable(self._var_key(key), float(val))

    def _persist_last_run(self):
        for src, key in (
                ('last_probe_z', 'last_probe_z'),
                ('last_probe_spread', 'last_probe_spread'),
                ('last_offset', 'last_offset'),
                ('last_drift', 'last_drift')):
            val = self.status.get(src)
            if val is not None:
                self._save_variable(self._var_key(key), float(val))

    # ------------------------------------------------------------------
    # Core flows
    # ------------------------------------------------------------------

    def _run_auto_apply(self, gcmd, probe_result=None, probe_spread=None,
                        samples=None, retries_used=None,
                        did_warmup=False, did_soak=False):
        self._require_probe(gcmd)
        self._require_save_variables(gcmd)

        if not self.status['calibrated']:
            raise gcmd.error(
                "AUTO_Z_TAP is not calibrated.\n"
                "Run AUTO_Z_TAP CALIBRATE=1 for the initial interactive "
                "paper test.\nThis only needs to be done once.")

        x, y = self._resolve_reference_xy(gcmd)

        if probe_result is None:
            # Thermal soak
            self._maybe_thermal_soak(gcmd)
            did_soak = self.thermal_soak_enabled

            self._ensure_homed(gcmd)
            self._maybe_clear_bed_mesh()

            # Warmup taps
            warmup = gcmd.get_int(
                'WARMUP_TAPS', self.warmup_taps, minval=0)
            if warmup > 0:
                self._run_warmup_taps(gcmd, x, y, warmup)
                did_warmup = True

            # Adaptive sample count
            effective_samples = None
            if self.adaptive_samples and self.health_tracker:
                effective_samples = \
                    self.health_tracker.suggest_sample_count(
                        gcmd.get_int('SAMPLES', self.probe_samples,
                                     minval=1))

            probe_result, probe_spread, retries_used, samples = \
                self._run_guarded_probe(gcmd, x, y, effective_samples)

        probe_z = probe_result.bed_z
        drift = probe_z - self.status['reference_probe_z']
        max_drift = gcmd.get_float('MAX_DRIFT', self.max_drift, minval=0.)
        if max_drift > 0. and abs(drift) > max_drift:
            direction = ("closer to bed" if drift < 0
                         else "further from bed")
            raise gcmd.error(
                "AUTO_Z_TAP drift %.4fmm (%s) exceeds MAX_DRIFT %.4fmm.\n"
                "The probe triggered %.4fmm %s than during calibration.\n"
                "Possible causes:\n"
                "  - Thermal expansion (temperature changed since "
                "calibration)\n"
                "  - Bed surface changed or shifted\n"
                "  - Probe mount loosened\n"
                "Try: Re-run AUTO_Z_TAP_CALIBRATE at current temperatures, "
                "or increase max_drift in [auto_z_tap]."
                % (drift, direction, max_drift, abs(drift), direction))

        estimated_paper_z = probe_z + self.status['paper_delta']
        env = self._resolve_environment(gcmd)
        adjustment, details, profiles = self._compute_adjustment(gcmd, env)
        final_offset = estimated_paper_z + adjustment

        # Safety check
        self._validate_offset_safety(gcmd, final_offset)

        self._apply_offset(gcmd, final_offset)

        # Update status
        self.status['last_probe_z'] = float(probe_z)
        self.status['last_probe_spread'] = float(probe_spread)
        self.status['last_offset'] = float(final_offset)
        self.status['last_drift'] = float(drift)
        self.status['last_profiles'] = [p.name for p in profiles]

        # Persist
        save = gcmd.get_int(
            'SAVE', 1 if self.persist_last_run else 0,
            minval=0, maxval=1)
        if save:
            self._persist_last_run()

        # Health tracking
        if self.health_tracker:
            self.health_tracker.record_session(
                probe_z, probe_spread, drift, samples,
                retries_used or 0,
                bed_temp=env.get('bed_temp'),
                hotend_temp=env.get('hotend_temp'))
            warnings = self.health_tracker.check_health()
            for w in warnings:
                self.gcode.respond_info("AUTO_Z_TAP WARNING: %s" % (w,))

        result = {
            'reference_x': x,
            'reference_y': y,
            'samples': samples,
            'retries_used': retries_used,
            'probe_z': probe_z,
            'probe_spread': probe_spread,
            'drift': drift,
            'paper_delta': self.status['paper_delta'],
            'estimated_paper_z': estimated_paper_z,
            'adjustment_total': adjustment,
            'final_offset': final_offset,
            'profiles': [p.name for p in profiles],
            'details': details,
            'warmup_taps': self.warmup_taps if did_warmup else 0,
            'thermal_soak': did_soak,
        }
        return result

    def _start_calibration(self, gcmd):
        self._require_probe(gcmd)
        self._require_save_variables(gcmd)

        if self.pending_calibration is not None:
            raise gcmd.error(
                "AUTO_Z_TAP calibration already in progress.\n"
                "Use ABORT to cancel, or ACCEPT to finish the current "
                "calibration.")

        manual_probe.verify_no_manual_probe(self.printer)

        # Thermal soak before calibration
        self._maybe_thermal_soak(gcmd)

        self._ensure_homed(gcmd)
        self._maybe_clear_bed_mesh()
        x, y = self._resolve_reference_xy(gcmd)

        # Warmup taps
        warmup = gcmd.get_int('WARMUP_TAPS', self.warmup_taps, minval=0)
        if warmup > 0:
            self._run_warmup_taps(gcmd, x, y, warmup)

        # Probe reference point
        probe_result, probe_spread, retries_used, samples = \
            self._run_guarded_probe(gcmd, x, y)

        # Z-hop and position for paper test
        cur = self.toolhead.get_position()
        hop_target = max(cur[2],
                         probe_result.bed_z + self.calibration_z_hop,
                         self.safe_z)
        self._manual_move(
            z=hop_target, speed=self._effective_lift_speed(gcmd))
        self._manual_move(
            x=probe_result.bed_x, y=probe_result.bed_y,
            speed=self.travel_speed)

        command_params = dict(gcmd.get_command_parameters())
        command_params.pop('CALIBRATE', None)
        command_params.pop('CALIBRATE_IF_NEEDED', None)

        env = self._resolve_environment(gcmd)
        self.pending_calibration = {
            'reference_x': x,
            'reference_y': y,
            'probe_result': probe_result,
            'probe_spread': probe_spread,
            'retries_used': retries_used,
            'samples': samples,
            'command_params': command_params,
            'calibration_bed_temp': env.get('bed_temp'),
            'calibration_hotend_temp': env.get('hotend_temp'),
            'calibration_chamber_temp': env.get('chamber_temp'),
        }
        self.status['calibration_in_progress'] = True

        self.gcode.respond_info(
            "AUTO_Z_TAP calibration started at X%.3f Y%.3f "
            "(probe_type=%s).\n"
            "Place paper under nozzle and use TESTZ / ACCEPT.\n"
            "  TESTZ Z=-0.1  (move nozzle down 0.1mm)\n"
            "  TESTZ Z=+0.1  (move nozzle up 0.1mm)\n"
            "  TESTZ Z=-     (bisect down)\n"
            "  TESTZ Z=+     (bisect up)\n"
            "When paper drag feels right, run ACCEPT.\n"
            "This calibration only needs to be done once."
            % (x, y, self.probe_type))

        manual_probe.ManualProbeHelper(
            self.printer, gcmd, self._finalize_calibration)

    def _finalize_calibration(self, mpresult):
        pending = self.pending_calibration
        self.pending_calibration = None
        self.status['calibration_in_progress'] = False

        if pending is None:
            return
        if mpresult is None:
            self.gcode.respond_info("AUTO_Z_TAP calibration aborted")
            return

        probe_z = pending['probe_result'].bed_z
        paper_z = mpresult.bed_z
        self.status['calibrated'] = True
        self.status['reference_probe_z'] = float(probe_z)
        self.status['paper_delta'] = float(paper_z - probe_z)
        self.status['reference_x'] = float(pending['reference_x'])
        self.status['reference_y'] = float(pending['reference_y'])
        self.status['calibration_bed_temp'] = (
            pending['calibration_bed_temp'])
        self.status['calibration_hotend_temp'] = (
            pending['calibration_hotend_temp'])
        self.status['calibration_chamber_temp'] = (
            pending['calibration_chamber_temp'])
        self.status['calibration_probe_type'] = self.probe_type

        self._persist_calibration()

        # Apply offset using the same probe result
        apply_gcmd = self.gcode.create_gcode_command(
            'AUTO_Z_TAP', 'AUTO_Z_TAP', pending['command_params'])
        result = self._run_auto_apply(
            apply_gcmd,
            probe_result=pending['probe_result'],
            probe_spread=pending['probe_spread'],
            samples=pending['samples'],
            retries_used=pending['retries_used'])

        lines = [
            "AUTO_Z_TAP calibration complete:",
            "  probe_type=%s" % (self.probe_type,),
            "  reference_probe_z=%.6f" % (self.status['reference_probe_z'],),
            "  paper_z=%.6f" % (paper_z,),
            "  stored_paper_delta=%.6f" % (self.status['paper_delta'],),
        ]

        # Optional validation probe
        if self.calibration_validate:
            self.gcode.respond_info(
                "AUTO_Z_TAP: Validating calibration with a fresh probe...")
            try:
                x = pending['reference_x']
                y = pending['reference_y']
                val_result, val_spread, _, _ = self._run_guarded_probe(
                    apply_gcmd, x, y)
                val_drift = val_result.bed_z - probe_z
                lines.append(
                    "  validation: drift=%.4fmm spread=%.4fmm" % (
                        val_drift, val_spread))
                if abs(val_drift) > self.max_probe_spread * 3:
                    lines.append(
                        "  WARNING: Validation shows significant drift. "
                        "Consider re-running calibration.")
            except Exception as e:
                lines.append(
                    "  validation: FAILED (%s)" % (str(e),))

        lines.append(self._summarize(result))
        self.gcode.respond_info('\n'.join(lines))

    # ------------------------------------------------------------------
    # Gcode commands
    # ------------------------------------------------------------------

    cmd_AUTO_Z_TAP_help = (
        "Automatic Z offset. Use CALIBRATE=1 once for interactive "
        "paper calibration, then call AUTO_Z_TAP in START_PRINT.")

    def cmd_AUTO_Z_TAP(self, gcmd):
        self._load_persistent_state()

        if gcmd.get_int('CLEAR', 0, minval=0, maxval=1):
            self.cmd_AUTO_Z_TAP_CLEAR(gcmd)
            return

        if gcmd.get_int('CALIBRATE', 0, minval=0, maxval=1):
            self._start_calibration(gcmd)
            return

        if (not self.status['calibrated']
                and gcmd.get_int('CALIBRATE_IF_NEEDED', 0,
                                 minval=0, maxval=1)):
            self._start_calibration(gcmd)
            return

        result = self._run_auto_apply(gcmd)
        gcmd.respond_info(self._summarize(result))

    cmd_AUTO_Z_TAP_CALIBRATE_help = (
        "Start interactive paper calibration for AUTO_Z_TAP")

    def cmd_AUTO_Z_TAP_CALIBRATE(self, gcmd):
        self._load_persistent_state()
        self._start_calibration(gcmd)

    cmd_AUTO_Z_TAP_STATUS_help = (
        "Show AUTO_Z_TAP calibration, probe type, and health state")

    def cmd_AUTO_Z_TAP_STATUS(self, gcmd):
        self._load_persistent_state()
        lines = [
            "AUTO_Z_TAP status:",
            "  probe_type=%s (%s)" % (
                self.probe_type, self.preset.get('description', '')),
            "  calibrated=%s" % (self.status['calibrated'],),
            "  calibration_in_progress=%s" % (
                self.status['calibration_in_progress'],),
            "  reference_xy=%.3f,%.3f" % (
                self.status['reference_x'], self.status['reference_y']),
            "  reference_probe_z=%.6f" % (
                self.status['reference_probe_z'],),
            "  paper_delta=%.6f" % (self.status['paper_delta'],),
            "  calibration_temps bed=%s hotend=%s chamber=%s" % (
                self.status['calibration_bed_temp'],
                self.status['calibration_hotend_temp'],
                self.status['calibration_chamber_temp']),
            "  calibration_probe_type=%s" % (
                self.status.get('calibration_probe_type', 'unknown'),),
            "  last_probe_z=%s spread=%s drift=%s" % (
                self.status['last_probe_z'],
                self.status['last_probe_spread'],
                self.status['last_drift']),
            "  last_offset=%s" % (self.status['last_offset'],),
            "  warmup_taps=%d thermal_soak=%s" % (
                self.warmup_taps,
                'enabled' if self.thermal_soak_enabled else 'disabled'),
            "  safe_offset_range=[%.3f, %.3f]" % (
                self.safe_offset_min, self.safe_offset_max),
        ]
        if self.adjustments:
            lines.append("  available_profiles=%s" % (
                ','.join(sorted(self.adjustments.keys())),))

        if self.health_tracker:
            stats = self.health_tracker.get_statistics()
            if stats:
                level, score = self.health_tracker.get_confidence()
                lines.append(
                    "  probe_health: confidence=%s (%.2f) "
                    "sessions=%d avg_spread=%.4f trend=%s" % (
                        level, score, stats['session_count'],
                        stats['avg_spread'], stats['recent_trend']))

        gcmd.respond_info('\n'.join(lines))

    cmd_AUTO_Z_TAP_CLEAR_help = (
        "Clear AUTO_Z_TAP calibration and saved state")

    def cmd_AUTO_Z_TAP_CLEAR(self, gcmd):
        self.pending_calibration = None
        self.status['calibration_in_progress'] = False
        self.status['calibrated'] = False
        self.status['reference_probe_z'] = 0.0
        self.status['paper_delta'] = 0.0
        self.status['last_probe_z'] = None
        self.status['last_probe_spread'] = None
        self.status['last_offset'] = None
        self.status['last_drift'] = None

        self._save_variable(self._var_key('calibrated'), False)
        self._save_variable(self._var_key('reference_probe_z'), 0.0)
        self._save_variable(self._var_key('paper_delta'), 0.0)
        self._save_variable(self._var_key('last_probe_z'), 0.0)
        self._save_variable(self._var_key('last_probe_spread'), 0.0)
        self._save_variable(self._var_key('last_offset'), 0.0)
        self._save_variable(self._var_key('last_drift'), 0.0)

        clear_history = gcmd.get_int(
            'CLEAR_HISTORY', 0, minval=0, maxval=1)
        if clear_history and self.health_tracker:
            self.health_tracker.clear()
            gcmd.respond_info(
                "AUTO_Z_TAP calibration and probe history cleared")
        else:
            gcmd.respond_info("AUTO_Z_TAP calibration state cleared")

    cmd_AUTO_Z_TAP_HEALTH_help = "Show detailed probe health report"

    def cmd_AUTO_Z_TAP_HEALTH(self, gcmd):
        if not self.health_tracker:
            gcmd.respond_info(
                "AUTO_Z_TAP probe health tracking is disabled.\n"
                "Set probe_health_tracking: True in [auto_z_tap].")
            return

        stats = self.health_tracker.get_statistics()
        if stats is None:
            gcmd.respond_info(
                "AUTO_Z_TAP probe health: No data yet.\n"
                "Run AUTO_Z_TAP at least once to start collecting data.")
            return

        level, score = self.health_tracker.get_confidence()
        suggested = self.health_tracker.suggest_sample_count(
            self.probe_samples)

        lines = [
            "AUTO_Z_TAP probe health:",
            "  probe_type=%s" % (self.probe_type,),
            "  sessions_tracked=%d" % (stats['session_count'],),
            "  confidence=%s (%.2f)" % (level, score),
            "  avg_spread=%.4fmm  max_spread=%.4fmm  min=%.4fmm" % (
                stats['avg_spread'], stats['max_spread'],
                stats['min_spread']),
            "  avg_drift=%.4fmm  max_abs_drift=%.4fmm" % (
                stats['avg_drift'], stats['max_abs_drift']),
            "  avg_retries=%.1f  retry_rate=%.1f%%" % (
                stats['avg_retries'], stats['retry_rate'] * 100),
            "  trend=%s" % (stats['recent_trend'],),
            "  suggested_samples=%d (configured=%d)" % (
                suggested, self.probe_samples),
        ]

        warnings = self.health_tracker.check_health()
        if warnings:
            lines.append("  warnings:")
            for w in warnings:
                lines.append("    - %s" % (w,))

        gcmd.respond_info('\n'.join(lines))

    cmd_AUTO_Z_TAP_PROBE_TEST_help = (
        "Diagnostic probe test without applying offset")

    def cmd_AUTO_Z_TAP_PROBE_TEST(self, gcmd):
        self._require_probe(gcmd)
        self._ensure_homed(gcmd)
        self._maybe_clear_bed_mesh()

        x, y = self._resolve_reference_xy(gcmd)
        samples = gcmd.get_int('SAMPLES', self.probe_samples, minval=1)

        # Warmup taps
        warmup = gcmd.get_int('WARMUP_TAPS', self.warmup_taps, minval=0)
        if warmup > 0:
            self._run_warmup_taps(gcmd, x, y, warmup)

        self._move_to_reference(x, y)
        z_values = []
        for idx in range(samples):
            pres = self._probe_once(gcmd)
            z_values.append(pres.bed_z)
            if idx + 1 < samples:
                cur = self.toolhead.get_position()
                self._manual_move(
                    z=cur[2] + self._effective_retract(gcmd),
                    speed=self._effective_lift_speed(gcmd))

        self._raise_for_travel()

        sorted_vals = sorted(z_values)
        avg = sum(z_values) / len(z_values)
        median = sorted_vals[len(sorted_vals) // 2]
        spread = max(z_values) - min(z_values)
        variance = sum((v - avg) ** 2 for v in z_values) / len(z_values)
        stdev = math.sqrt(variance)

        if spread <= 0.005:
            rating = "EXCELLENT"
        elif spread <= 0.015:
            rating = "GOOD"
        elif spread <= 0.030:
            rating = "ACCEPTABLE"
        else:
            rating = "POOR"

        lines = [
            "AUTO_Z_TAP probe test (%d samples, probe_type=%s):" % (
                samples, self.probe_type),
            "  values: %s" % (
                ', '.join('%.4f' % v for v in z_values),),
            "  median=%.6f  average=%.6f" % (median, avg),
            "  spread=%.4fmm  stdev=%.4fmm" % (spread, stdev),
            "  rating: %s" % (rating,),
        ]

        if warmup > 0:
            lines.append("  warmup_taps=%d" % (warmup,))

        gcmd.respond_info('\n'.join(lines))

    # ------------------------------------------------------------------
    # Moonraker status
    # ------------------------------------------------------------------

    def get_status(self, eventtime):
        status = dict(self.status)
        status['probe_type'] = self.probe_type
        status['probe_type_description'] = self.preset.get(
            'description', '')
        status['thermal_soak_enabled'] = self.thermal_soak_enabled
        status['warmup_taps'] = self.warmup_taps
        status['safe_offset_min'] = self.safe_offset_min
        status['safe_offset_max'] = self.safe_offset_max

        if self.health_tracker:
            stats = self.health_tracker.get_statistics()
            if stats:
                level, score = self.health_tracker.get_confidence()
                status['health_session_count'] = stats['session_count']
                status['health_avg_spread'] = stats['avg_spread']
                status['health_confidence'] = level
                status['health_confidence_score'] = score
                status['health_trend'] = stats['recent_trend']

        return status


def load_config(config):
    return AutoZTap(config)
