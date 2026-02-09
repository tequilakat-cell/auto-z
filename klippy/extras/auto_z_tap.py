# Auto Z-offset manager for probe-based printers (e.g. Chaotic Lab CNC Tap V2)
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from . import manual_probe
from . import probe as probe_module


def _normalize_token(value):
    if value is None:
        return ""
    return str(value).strip().lower()


def _split_csv(value):
    if not value:
        return []
    return [_normalize_token(v) for v in str(value).split(',') if v.strip()]


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

        # Match filters (all specified filters must match)
        self.material = _normalize_token(config.get('material', ''))
        self.build_surface = _normalize_token(config.get('build_surface', ''))
        self.nozzle = _normalize_token(config.get('nozzle', ''))

        # Static and dynamic modifiers
        self.offset = config.getfloat('offset', 0.)
        self.bed_temp_coeff = config.getfloat('bed_temp_coeff', 0.)
        self.hotend_temp_coeff = config.getfloat('hotend_temp_coeff', 0.)
        self.chamber_temp_coeff = config.getfloat('chamber_temp_coeff', 0.)
        self.first_layer_coeff = config.getfloat('first_layer_coeff', 0.)

        # Optional references. If unset, plugin falls back to calibration refs.
        self.bed_temp_reference = config.getfloat('bed_temp_reference', None)
        self.hotend_temp_reference = config.getfloat(
            'hotend_temp_reference', None)
        self.chamber_temp_reference = config.getfloat(
            'chamber_temp_reference', None)
        self.first_layer_reference = config.getfloat(
            'first_layer_reference', None)

    def matches(self, material, build_surface, nozzle):
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

        if (self.bed_temp_coeff and bed_temp is not None
                and bed_ref is not None):
            val = (bed_temp - bed_ref) * self.bed_temp_coeff
            total += val
            details.append((
                "bed_temp", val,
                "(bed %.2f - ref %.2f) * %.6f" % (
                    bed_temp, bed_ref, self.bed_temp_coeff)))

        if (self.hotend_temp_coeff and hotend_temp is not None
                and hotend_ref is not None):
            val = (hotend_temp - hotend_ref) * self.hotend_temp_coeff
            total += val
            details.append((
                "hotend_temp", val,
                "(hotend %.2f - ref %.2f) * %.6f" % (
                    hotend_temp, hotend_ref, self.hotend_temp_coeff)))

        if (self.chamber_temp_coeff and chamber_temp is not None
                and chamber_ref is not None):
            val = (chamber_temp - chamber_ref) * self.chamber_temp_coeff
            total += val
            details.append((
                "chamber_temp", val,
                "(chamber %.2f - ref %.2f) * %.6f" % (
                    chamber_temp, chamber_ref, self.chamber_temp_coeff)))

        if (self.first_layer_coeff and first_layer_height is not None
                and first_layer_ref is not None):
            val = (first_layer_height - first_layer_ref) * self.first_layer_coeff
            total += val
            details.append((
                "first_layer", val,
                "(layer %.3f - ref %.3f) * %.6f" % (
                    first_layer_height, first_layer_ref,
                    self.first_layer_coeff)))

        return total, details


class AutoZTap:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')

        # Core behavior
        self.probe_object_name = config.get('probe_object', 'probe')
        self.auto_home = config.getboolean('auto_home', True)
        self.home_command = config.get('home_command', 'G28')
        self.clear_bed_mesh_before_probe = config.getboolean(
            'clear_bed_mesh_before_probe', True)
        self.require_save_variables = config.getboolean(
            'require_save_variables', True)

        # Motion / probing behavior
        self.safe_z = config.getfloat('safe_z', 10., above=0.)
        self.probe_start_z = config.getfloat('probe_start_z', 8., above=0.)
        self.travel_speed = config.getfloat('travel_speed', 150., above=0.)
        self.probe_speed = config.getfloat('probe_speed', None, above=0.)
        self.lift_speed = config.getfloat('lift_speed', None, above=0.)
        self.sample_retract_dist = config.getfloat(
            'sample_retract_dist', 1.5, above=0.)

        self.probe_samples = config.getint('probe_samples', 5, minval=1)
        self.probe_samples_result = config.getchoice(
            'probe_samples_result', ['average', 'median'], 'median')
        self.max_probe_spread = config.getfloat('max_probe_spread', 0.020,
                                                minval=0.)
        self.probe_retries = config.getint('probe_retries', 2, minval=0)
        self.max_drift = config.getfloat('max_drift', 1.0, minval=0.)
        self.calibration_z_hop = config.getfloat(
            'calibration_z_hop', 5.0, above=0.)

        # Base references and adjustments
        self.global_offset = config.getfloat('global_offset', 0.)
        self.bed_temp_coeff = config.getfloat('bed_temp_coeff', 0.)
        self.hotend_temp_coeff = config.getfloat('hotend_temp_coeff', 0.)
        self.chamber_temp_coeff = config.getfloat('chamber_temp_coeff', 0.)
        self.first_layer_coeff = config.getfloat('first_layer_coeff', 0.)

        self.bed_temp_reference = config.getfloat('bed_temp_reference', None)
        self.hotend_temp_reference = config.getfloat(
            'hotend_temp_reference', None)
        self.chamber_temp_reference = config.getfloat(
            'chamber_temp_reference', None)
        self.first_layer_reference = config.getfloat(
            'first_layer_reference', 0.20)

        self.default_profile = _normalize_token(config.get('default_profile', ''))
        self.chamber_sensor = _normalize_token(config.get('chamber_sensor', ''))
        self.max_total_adjustment = config.getfloat(
            'max_total_adjustment', 0.600, minval=0.)
        self.apply_move = config.getboolean('apply_move', False)
        self.persist_last_run = config.getboolean('persist_last_run', True)
        self.report_breakdown = config.getboolean('report_breakdown', True)

        # Reference probing position
        configured_ref_xy = config.getfloatlist(
            'reference_xy_position', None, count=2)
        if configured_ref_xy is not None:
            self.reference_xy = tuple(configured_ref_xy)
        else:
            self.reference_xy = self._derive_reference_xy_from_kinematics(config)

        # Variable naming
        raw_prefix = _normalize_token(config.get('variable_prefix', 'auto_z_tap'))
        clean_prefix = []
        for ch in raw_prefix:
            if ch.isalnum() or ch == '_':
                clean_prefix.append(ch)
            else:
                clean_prefix.append('_')
        self.variable_prefix = ''.join(clean_prefix).strip('_') or 'auto_z_tap'

        # Optional adjustment profiles
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
        self.pending_calibration = None
        self.status = {
            'calibrated': False,
            'reference_probe_z': 0.0,
            'paper_delta': 0.0,
            'reference_x': self.reference_xy[0] if self.reference_xy else 0.0,
            'reference_y': self.reference_xy[1] if self.reference_xy else 0.0,
            'calibration_bed_temp': None,
            'calibration_hotend_temp': None,
            'calibration_chamber_temp': None,
            'last_probe_z': None,
            'last_probe_spread': None,
            'last_offset': None,
            'last_drift': None,
            'last_profiles': [],
            'calibration_in_progress': False,
        }

        self.printer.register_event_handler('klippy:connect',
                                            self._handle_connect)

        self.gcode.register_command('AUTO_Z_TAP', self.cmd_AUTO_Z_TAP,
                                    desc=self.cmd_AUTO_Z_TAP_help)
        self.gcode.register_command('AUTO_Z_TAP_CALIBRATE',
                                    self.cmd_AUTO_Z_TAP_CALIBRATE,
                                    desc=self.cmd_AUTO_Z_TAP_CALIBRATE_help)
        self.gcode.register_command('AUTO_Z_TAP_STATUS',
                                    self.cmd_AUTO_Z_TAP_STATUS,
                                    desc=self.cmd_AUTO_Z_TAP_STATUS_help)
        self.gcode.register_command('AUTO_Z_TAP_CLEAR',
                                    self.cmd_AUTO_Z_TAP_CLEAR,
                                    desc=self.cmd_AUTO_Z_TAP_CLEAR_help)

    def _var_key(self, suffix):
        return '%s_%s' % (self.variable_prefix, suffix)

    def _derive_reference_xy_from_kinematics(self, config):
        if not config.has_section('stepper_x') or not config.has_section('stepper_y'):
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
        self.probe = self.printer.lookup_object(self.probe_object_name, None)
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
            return save_obj.get_status(self._eventtime()).get('variables', {})
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

        self.status['last_probe_z'] = values.get(self._var_key('last_probe_z'),
                                                 None)
        self.status['last_probe_spread'] = values.get(
            self._var_key('last_probe_spread'), None)
        self.status['last_offset'] = values.get(self._var_key('last_offset'),
                                                None)
        self.status['last_drift'] = values.get(self._var_key('last_drift'),
                                               None)

    def _require_probe(self, gcmd):
        if self.probe is None:
            raise gcmd.error(
                "AUTO_Z_TAP could not find probe object '%s'. "
                "Set probe_object in [auto_z_tap] if needed."
                % (self.probe_object_name,))

    def _require_save_variables(self, gcmd):
        if not self.require_save_variables:
            return
        save_obj = self.printer.lookup_object('save_variables', None)
        if save_obj is None:
            raise gcmd.error(
                "[save_variables] is required by AUTO_Z_TAP in this config. "
                "Add [save_variables] with a filename and restart Klipper.")

    def _axis_is_homed(self, axis):
        homed = self.toolhead.get_status(self._eventtime()).get('homed_axes', '')
        if isinstance(homed, str):
            return axis in homed.lower()
        return axis in homed

    def _ensure_homed(self, gcmd):
        need_axes = ('x', 'y', 'z')
        if all(self._axis_is_homed(axis) for axis in need_axes):
            return
        if not self.auto_home:
            raise gcmd.error("AUTO_Z_TAP requires XYZ to be homed")
        self.gcode.respond_info(
            "AUTO_Z_TAP: axes not homed, running '%s'" % (
                self.home_command,))
        self.gcode.run_script_from_command(self.home_command)

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
            # If bed_mesh command is unavailable, continue.
            pass

    def _manual_move(self, x=None, y=None, z=None, speed=None):
        move = [x, y, z]
        self.toolhead.manual_move(move, speed if speed is not None
                                  else self.travel_speed)

    def _raise_for_travel(self):
        cur = self.toolhead.get_position()
        if cur[2] < self.safe_z:
            self._manual_move(z=self.safe_z, speed=self._effective_lift_speed(None))

    def _move_to_reference(self, x, y):
        self._raise_for_travel()
        self._manual_move(x=x, y=y, speed=self.travel_speed)
        cur = self.toolhead.get_position()
        if cur[2] < self.probe_start_z:
            self._manual_move(z=self.probe_start_z,
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

    def _probe_once(self, gcmd):
        params = {'SAMPLES': '1'}
        probe_speed = self._effective_probe_speed(gcmd)
        if probe_speed is not None:
            params['PROBE_SPEED'] = '%.6f' % (probe_speed,)
        lift_speed = self._effective_lift_speed(gcmd)
        if lift_speed is not None:
            params['LIFT_SPEED'] = '%.6f' % (lift_speed,)
        params['SAMPLE_RETRACT_DIST'] = '%.6f' % (self._effective_retract(gcmd),)
        probe_gcmd = self.gcode.create_gcode_command(
            'AUTO_Z_TAP_INTERNAL_PROBE', 'AUTO_Z_TAP_INTERNAL_PROBE', params)
        return probe_module.run_single_probe(self.probe, probe_gcmd)

    def _run_guarded_probe(self, gcmd, x, y):
        samples = gcmd.get_int('SAMPLES', self.probe_samples, minval=1)
        retries = gcmd.get_int('RETRIES', self.probe_retries, minval=0)
        spread_limit = gcmd.get_float('MAX_SPREAD', self.max_probe_spread,
                                      minval=0.)
        method = _normalize_token(gcmd.get('SAMPLES_RESULT',
                                           self.probe_samples_result))
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
                "AUTO_Z_TAP: probe spread %.6f > %.6f; retry %d/%d" % (
                    spread, spread_limit, attempt + 1, retries))
            self._raise_for_travel()

        raise gcmd.error(
            "AUTO_Z_TAP probe repeatability failed: spread %.6f exceeds %.6f"
            % (last_spread or 0.0, spread_limit))

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

        material = _normalize_token(gcmd.get('MATERIAL', ''))
        build_surface = _normalize_token(gcmd.get('BUILD_SURFACE', ''))
        nozzle = _normalize_token(gcmd.get('NOZZLE', ''))

        return {
            'bed_temp': bed_temp,
            'hotend_temp': hotend_temp,
            'chamber_temp': chamber_temp,
            'first_layer_height': first_layer_height,
            'material': material,
            'build_surface': build_surface,
            'nozzle': nozzle,
        }

    def _calibration_refs(self):
        return {
            'bed_temp_reference': self.status.get('calibration_bed_temp'),
            'hotend_temp_reference': self.status.get('calibration_hotend_temp'),
            'chamber_temp_reference': self.status.get('calibration_chamber_temp'),
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
                    env.get('nozzle', ''))]
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
                raise gcmd.error("Unknown AUTO_Z_TAP profile: %s" % (name,))
            if not profile.enabled:
                continue
            resolved.append(profile)
            seen.add(name)
        return resolved

    def _compute_adjustment(self, gcmd, env):
        total = self.global_offset
        details = []
        if self.global_offset:
            details.append(("global_offset", self.global_offset, "config"))

        refs = self._calibration_refs()

        if (self.bed_temp_coeff and env['bed_temp'] is not None):
            bed_ref = self.bed_temp_reference
            if bed_ref is None:
                bed_ref = refs.get('bed_temp_reference')
            if bed_ref is not None:
                val = (env['bed_temp'] - bed_ref) * self.bed_temp_coeff
                total += val
                details.append((
                    "global_bed_temp", val,
                    "(bed %.2f - ref %.2f) * %.6f" % (
                        env['bed_temp'], bed_ref, self.bed_temp_coeff)))

        if (self.hotend_temp_coeff and env['hotend_temp'] is not None):
            hotend_ref = self.hotend_temp_reference
            if hotend_ref is None:
                hotend_ref = refs.get('hotend_temp_reference')
            if hotend_ref is not None:
                val = (env['hotend_temp'] - hotend_ref) * self.hotend_temp_coeff
                total += val
                details.append((
                    "global_hotend_temp", val,
                    "(hotend %.2f - ref %.2f) * %.6f" % (
                        env['hotend_temp'], hotend_ref,
                        self.hotend_temp_coeff)))

        if (self.chamber_temp_coeff and env['chamber_temp'] is not None):
            chamber_ref = self.chamber_temp_reference
            if chamber_ref is None:
                chamber_ref = refs.get('chamber_temp_reference')
            if chamber_ref is not None:
                val = (env['chamber_temp'] - chamber_ref) * self.chamber_temp_coeff
                total += val
                details.append((
                    "global_chamber_temp", val,
                    "(chamber %.2f - ref %.2f) * %.6f" % (
                        env['chamber_temp'], chamber_ref,
                        self.chamber_temp_coeff)))

        if (self.first_layer_coeff and env['first_layer_height'] is not None
                and self.first_layer_reference is not None):
            val = ((env['first_layer_height'] - self.first_layer_reference)
                   * self.first_layer_coeff)
            total += val
            details.append((
                "global_first_layer", val,
                "(layer %.3f - ref %.3f) * %.6f" % (
                    env['first_layer_height'], self.first_layer_reference,
                    self.first_layer_coeff)))

        profiles = self._resolve_profiles(gcmd, env)
        global_refs = self._global_refs()
        for profile in profiles:
            pval, pdetails = profile.calculate(env, refs, global_refs)
            total += pval
            details.append(("profile:%s" % (profile.name,), pval, "profile total"))
            for d in pdetails:
                if len(d) == 3:
                    details.append((
                        "profile:%s:%s" % (profile.name, d[0]), d[1], d[2]))
                else:
                    details.append((
                        "profile:%s:%s" % (profile.name, d[0]), d[1], ""))

        extra = gcmd.get_float('EXTRA', 0.)
        if extra:
            total += extra
            details.append(("extra", extra, "gcode EXTRA"))

        max_adjust = gcmd.get_float('MAX_ADJUST', self.max_total_adjustment,
                                    minval=0.)
        if max_adjust > 0. and abs(total) > max_adjust:
            raise gcmd.error(
                "AUTO_Z_TAP adjustment %.6f exceeds MAX_ADJUST %.6f"
                % (total, max_adjust))

        return total, details, profiles

    def _apply_offset(self, gcmd, offset):
        move = gcmd.get_int('MOVE', 1 if self.apply_move else 0, minval=0,
                            maxval=1)
        cmd = 'SET_GCODE_OFFSET Z=%.6f MOVE=%d' % (offset, move)
        if move:
            move_speed = gcmd.get_float('MOVE_SPEED', self.travel_speed, above=0.)
            cmd += ' MOVE_SPEED=%.6f' % (move_speed,)
        self.gcode.run_script_from_command(cmd)

    def _summarize(self, result):
        lines = [
            "AUTO_Z_TAP applied:",
            "  reference_xy=%.3f,%.3f" % (
                result['reference_x'], result['reference_y']),
            "  probe_z=%.6f spread=%.6f drift=%.6f" % (
                result['probe_z'], result['probe_spread'], result['drift']),
            "  paper_delta=%.6f estimated_paper_z=%.6f" % (
                result['paper_delta'], result['estimated_paper_z']),
            "  adjustment_total=%.6f final_offset=%.6f" % (
                result['adjustment_total'], result['final_offset']),
        ]
        if result['profiles']:
            lines.append("  profiles=%s" % (
                ','.join(result['profiles']),))

        if self.report_breakdown and result.get('details'):
            lines.append("  breakdown:")
            for name, value, note in result['details']:
                if note:
                    lines.append("    - %s: %.6f (%s)" % (name, value, note))
                else:
                    lines.append("    - %s: %.6f" % (name, value))
        return '\n'.join(lines)

    def _persist_calibration(self):
        self._save_variable(self._var_key('calibrated'),
                            bool(self.status['calibrated']))
        self._save_variable(self._var_key('reference_probe_z'),
                            float(self.status['reference_probe_z']))
        self._save_variable(self._var_key('paper_delta'),
                            float(self.status['paper_delta']))
        self._save_variable(self._var_key('reference_x'),
                            float(self.status['reference_x']))
        self._save_variable(self._var_key('reference_y'),
                            float(self.status['reference_y']))

        for src, key in (
                ('calibration_bed_temp', 'cal_bed_temp'),
                ('calibration_hotend_temp', 'cal_hotend_temp'),
                ('calibration_chamber_temp', 'cal_chamber_temp')):
            val = self.status.get(src)
            if val is not None:
                self._save_variable(self._var_key(key), float(val))

    def _persist_last_run(self):
        if self.status['last_probe_z'] is not None:
            self._save_variable(self._var_key('last_probe_z'),
                                float(self.status['last_probe_z']))
        if self.status['last_probe_spread'] is not None:
            self._save_variable(self._var_key('last_probe_spread'),
                                float(self.status['last_probe_spread']))
        if self.status['last_offset'] is not None:
            self._save_variable(self._var_key('last_offset'),
                                float(self.status['last_offset']))
        if self.status['last_drift'] is not None:
            self._save_variable(self._var_key('last_drift'),
                                float(self.status['last_drift']))

    def _run_auto_apply(self, gcmd, probe_result=None, probe_spread=None,
                        samples=None, retries_used=None):
        self._require_probe(gcmd)
        self._require_save_variables(gcmd)

        if not self.status['calibrated']:
            raise gcmd.error(
                "AUTO_Z_TAP is not calibrated. "
                "Run AUTO_Z_TAP CALIBRATE=1 for the initial interactive paper test.")

        x, y = self._resolve_reference_xy(gcmd)
        if probe_result is None:
            self._ensure_homed(gcmd)
            self._maybe_clear_bed_mesh()
            probe_result, probe_spread, retries_used, samples = self._run_guarded_probe(
                gcmd, x, y)

        probe_z = probe_result.bed_z
        drift = probe_z - self.status['reference_probe_z']
        max_drift = gcmd.get_float('MAX_DRIFT', self.max_drift, minval=0.)
        if max_drift > 0. and abs(drift) > max_drift:
            raise gcmd.error(
                "AUTO_Z_TAP drift %.6f exceeds MAX_DRIFT %.6f. "
                "Check mechanics or re-run calibration." % (drift, max_drift))

        estimated_paper_z = probe_z + self.status['paper_delta']
        env = self._resolve_environment(gcmd)
        adjustment, details, profiles = self._compute_adjustment(gcmd, env)
        final_offset = estimated_paper_z + adjustment

        self._apply_offset(gcmd, final_offset)

        self.status['last_probe_z'] = float(probe_z)
        self.status['last_probe_spread'] = float(probe_spread)
        self.status['last_offset'] = float(final_offset)
        self.status['last_drift'] = float(drift)
        self.status['last_profiles'] = [p.name for p in profiles]

        save = gcmd.get_int('SAVE', 1 if self.persist_last_run else 0,
                            minval=0, maxval=1)
        if save:
            self._persist_last_run()

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
        }
        return result

    def _start_calibration(self, gcmd):
        self._require_probe(gcmd)
        self._require_save_variables(gcmd)

        if self.pending_calibration is not None:
            raise gcmd.error("AUTO_Z_TAP calibration already in progress")

        manual_probe.verify_no_manual_probe(self.printer)

        self._ensure_homed(gcmd)
        self._maybe_clear_bed_mesh()
        x, y = self._resolve_reference_xy(gcmd)

        probe_result, probe_spread, retries_used, samples = self._run_guarded_probe(
            gcmd, x, y)

        cur = self.toolhead.get_position()
        hop_target = max(cur[2], probe_result.bed_z + self.calibration_z_hop,
                         self.safe_z)
        self._manual_move(z=hop_target, speed=self._effective_lift_speed(gcmd))
        self._manual_move(x=probe_result.bed_x, y=probe_result.bed_y,
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
            "AUTO_Z_TAP calibration started at X%.3f Y%.3f.\n"
            "Place paper under nozzle and use TESTZ / ACCEPT.\n"
            "- TESTZ Z=-0.1 (move down)\n"
            "- TESTZ Z=+0.1 (move up)\n"
            "- TESTZ Z=- (bisect down)\n"
            "- TESTZ Z=+ (bisect up)\n"
            "When paper drag is correct, run ACCEPT." % (x, y))

        manual_probe.ManualProbeHelper(self.printer, gcmd,
                                       self._finalize_calibration)

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
        self.status['calibration_bed_temp'] = pending['calibration_bed_temp']
        self.status['calibration_hotend_temp'] = pending['calibration_hotend_temp']
        self.status['calibration_chamber_temp'] = pending['calibration_chamber_temp']

        self._persist_calibration()

        apply_gcmd = self.gcode.create_gcode_command(
            'AUTO_Z_TAP', 'AUTO_Z_TAP', pending['command_params'])
        result = self._run_auto_apply(
            apply_gcmd,
            probe_result=pending['probe_result'],
            probe_spread=pending['probe_spread'],
            samples=pending['samples'],
            retries_used=pending['retries_used'])

        self.gcode.respond_info(
            "AUTO_Z_TAP calibration complete:\n"
            "  reference_probe_z=%.6f\n"
            "  paper_z=%.6f\n"
            "  stored_paper_delta=%.6f\n%s" % (
                self.status['reference_probe_z'],
                paper_z,
                self.status['paper_delta'],
                self._summarize(result)))

    cmd_AUTO_Z_TAP_help = (
        "Automatic Z offset run. "
        "Use CALIBRATE=1 once for interactive paper calibration, then call "
        "AUTO_Z_TAP in START_PRINT.")
    def cmd_AUTO_Z_TAP(self, gcmd):
        self._load_persistent_state()
        if gcmd.get_int('CLEAR', 0, minval=0, maxval=1):
            self.cmd_AUTO_Z_TAP_CLEAR(gcmd)
            return

        if gcmd.get_int('CALIBRATE', 0, minval=0, maxval=1):
            self._start_calibration(gcmd)
            return

        if (not self.status['calibrated']
                and gcmd.get_int('CALIBRATE_IF_NEEDED', 0, minval=0, maxval=1)):
            self._start_calibration(gcmd)
            return

        result = self._run_auto_apply(gcmd)
        gcmd.respond_info(self._summarize(result))

    cmd_AUTO_Z_TAP_CALIBRATE_help = (
        "Start interactive first-time paper calibration for AUTO_Z_TAP")
    def cmd_AUTO_Z_TAP_CALIBRATE(self, gcmd):
        self._load_persistent_state()
        self._start_calibration(gcmd)

    cmd_AUTO_Z_TAP_STATUS_help = "Show AUTO_Z_TAP calibration and last-run state"
    def cmd_AUTO_Z_TAP_STATUS(self, gcmd):
        self._load_persistent_state()
        lines = [
            "AUTO_Z_TAP status:",
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
            "  last_probe_z=%s spread=%s drift=%s" % (
                self.status['last_probe_z'],
                self.status['last_probe_spread'],
                self.status['last_drift']),
            "  last_offset=%s" % (self.status['last_offset'],),
        ]
        if self.adjustments:
            lines.append("  available_profiles=%s" % (
                ','.join(sorted(self.adjustments.keys())),))
        gcmd.respond_info('\n'.join(lines))

    cmd_AUTO_Z_TAP_CLEAR_help = "Clear AUTO_Z_TAP calibration and saved state"
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

        gcmd.respond_info("AUTO_Z_TAP calibration state cleared")

    def get_status(self, eventtime):
        return dict(self.status)


def load_config(config):
    return AutoZTap(config)
