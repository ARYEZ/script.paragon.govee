# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The add-on session: reads Kodi settings, builds a controller, and owns the
cached device list and the scene list.

Both entry points (the interactive script and the background service) create
one of these. Nothing here draws UI, so the same object serves a dialog-driven
menu and a silent playback callback.
"""

import time

import addon_utils as utils
import palette as palette_lib
import scenes as scene_lib
from devices import (DEVICE_CACHE, Device, TRANSPORT_AUTO, TRANSPORT_CLOUD,
                     TRANSPORT_LAN, build_hub)

# Order must match the `values` list on the transport_mode setting.
_TRANSPORT_MODES = [TRANSPORT_AUTO, TRANSPORT_LAN, TRANSPORT_CLOUD]


class ParagonHome(object):
    """Everything the add-on needs, assembled from the user's settings."""

    CODE_FILE = 'broadlink_codes.json'
    KEY_FILE = 'tuya_keys.json'

    def __init__(self):
        # Learned IR/RF codes are loaded before the hub is built: the
        # Broadlink driver holds the same dict, so a code learned through the
        # driver is saved by this session without a round trip.
        self._codes = utils.read_json(self.CODE_FILE, default={}) or {}
        settings = self.read_settings()
        settings['broadlink_codes'] = self._codes
        settings['save_broadlink_codes'] = self.save_codes
        self._tuya_keys = utils.read_json(self.KEY_FILE, default={}) or {}
        settings['tuya_keys'] = self._tuya_keys
        settings['save_tuya_keys'] = self.save_tuya_keys
        settings['known_ips'] = self.known_ips
        settings['kasa_username'] = utils.get_setting('kasa_username')
        settings['kasa_password'] = utils.get_setting('kasa_password')
        self.controller = build_hub(settings)
        self._devices = None
        self._scenes = None
        self._palette = None
        # How many known lights failed to answer the last refresh, so the
        # control panel can say so rather than silently showing a short list.
        self.last_refresh_missing = 0

    # -- settings ----------------------------------------------------------

    @staticmethod
    def read_settings():
        mode_index = utils.get_int('transport_mode', 0)
        if mode_index < 0 or mode_index >= len(_TRANSPORT_MODES):
            mode_index = 0
        return {
            'mode': _TRANSPORT_MODES[mode_index],
            'api_key': utils.get_setting('api_key', ''),
            'bind_address': utils.get_setting('bind_address', ''),
            'verify_ssl': utils.get_bool('verify_ssl', True),
            'cloud_timeout': utils.get_int('cloud_timeout', 10) or 10,
            'command_retries': utils.get_int('command_retries', 2) or 2,
            'log_func': utils.debug,
        }

    def save_codes(self):
        utils.write_json(self.CODE_FILE, self._codes or {})

    def known_ips(self):
        """Addresses of every device already known, whatever its brand.

        Used to work out which subnet the house is actually on. A Govee bulb's
        address says that as well as anything, and better than this machine's
        own -- which on a box with a VPN or a container bridge can be a
        confident wrong answer.
        """
        return [d.ip for d in self.devices if d.ip]

    def save_tuya_keys(self):
        utils.write_json(self.KEY_FILE, self._tuya_keys or {})

    def set_local_key(self, device, key):
        """Store a Tuya local key. Returns False if it is not 16 characters."""
        from devices import ControlError

        driver = self.controller.driver_for(device)
        if driver is None or not hasattr(driver, 'set_local_key'):
            raise ControlError('%s does not use a local key' % device.name)
        return driver.set_local_key(device, key)

    def needs_local_key(self, device):
        driver = self.controller.driver_for(device)
        getter = getattr(driver, 'needs_key', None)
        return bool(getter and getter(device))

    # -- learned commands ---------------------------------------------------
    #
    # Routed through the owning driver rather than assuming Broadlink, so a
    # second kind of emitter needs no change here.

    def _emitter(self, device):
        from devices import ControlError

        driver = self.controller.driver_for(device)
        if driver is None or not hasattr(driver, 'start_learning'):
            raise ControlError('%s cannot learn commands' % device.name)
        return driver

    def test_device(self, device):
        """Prove a device answers, for drivers that can say so.

        Not routed through _emitter: an IR blaster is not the only thing worth
        testing, and a plug whose key has just been typed in by remote control
        is the clearest case of all.
        """
        from devices import ControlError

        driver = self.controller.driver_for(device)
        if driver is None or not hasattr(driver, 'test_connection'):
            raise ControlError('%s cannot be tested' % device.name)
        return driver.test_connection(device)

    def start_learning(self, device):
        return self._emitter(device).start_learning(device)

    def collect_learned(self, device):
        return self._emitter(device).collect_learned(device)

    def save_command(self, device, name, hex_code):
        return self._emitter(device).save_command(device, name, hex_code)

    def forget_command(self, device, name):
        return self._emitter(device).forget_command(device, name)

    @property
    def discovery_timeout(self):
        return max(1, utils.get_int('discovery_timeout', 3))

    # -- devices -----------------------------------------------------------

    @property
    def devices(self):
        """Cached device list, loaded from disk on first access."""
        if self._devices is None:
            raw = utils.read_json(DEVICE_CACHE, default=[]) or []
            self._devices = [Device.from_dict(entry) for entry in raw
                             if isinstance(entry, dict)]
        return self._devices

    @property
    def enabled_devices(self):
        return [d for d in self.devices if d.enabled]

    def save_devices(self):
        utils.write_json(DEVICE_CACHE, [d.to_dict() for d in self.devices])

    def forget_device(self, device):
        """Remove a light from the cache for good."""
        if device in self.devices:
            self._devices.remove(device)
            self.save_devices()
            utils.log('Forgot %s' % device.name)
            return True
        return False

    def device_by_id(self, device_id):
        wanted = (device_id or '').upper()
        for device in self.devices:
            if device.device_id == wanted:
                return device
        return None

    def refresh_devices(self):
        """Re-discover and merge into the cache. Returns (devices, warnings).

        User choices that live only on our side -- the friendly name and the
        enabled flag -- are carried across so a refresh never silently undoes
        them.
        """
        found, warnings = self.controller.discover(
            timeout=self.discovery_timeout)

        previous = {}
        for device in self.devices:
            previous[device.device_id] = device

        for device in found:
            old = previous.get(device.device_id)
            if old is None:
                continue
            device.enabled = old.enabled
            # Only keep a hand-set name; a placeholder should be replaced by
            # whatever this discovery turned up.
            if old.name and not old.name.startswith(old.model + ' ('):
                device.name = old.name

        # Keep lights that did not answer this time rather than dropping
        # them. A WiFi bulb asleep, powered off at the switch, or missed by a
        # single UDP sweep would otherwise be erased along with its name and
        # its enabled flag -- work that can represent 25 trips around the
        # house. Anything genuinely gone can be removed with "Forget this
        # light" in Manage devices.
        found_ids = set(d.device_id for d in found)
        missing = [d for d in self.devices if d.device_id not in found_ids]

        # A plug that has just been split into its outlets leaves its old
        # single entry behind. Keeping it would show a switch that no longer
        # controls anything, so an entry superseded by the outlets of the same
        # hardware is the one case where a device that did not answer is still
        # dropped.
        superseded = set()
        for device in found:
            native = (getattr(device, 'native_id', '') or '').upper()
            if native and native != device.device_id:
                superseded.add(native)
        missing = [d for d in missing if d.device_id not in superseded]
        for device in missing:
            utils.log('%s did not answer this search; keeping its entry'
                      % device.name)

        self._devices = sorted(found + missing, key=lambda d: d.name.lower())
        self.save_devices()
        utils.log('Device refresh: %d found, %d kept unseen, %d total'
                  % (len(found), len(missing), len(self._devices)))
        self.last_refresh_missing = len(missing)
        return self._devices, warnings

    # -- colour palette ----------------------------------------------------

    @property
    def palette(self):
        """Named colours for the menus, seeded on first read."""
        if self._palette is None:
            raw = utils.read_json(palette_lib.PALETTE_FILE, default=None)
            if raw is None:
                self._palette = palette_lib.default_palette()
                self.save_palette()
            else:
                self._palette = palette_lib.normalise_all(raw)
        return self._palette

    def save_palette(self):
        utils.write_json(palette_lib.PALETTE_FILE, self._palette or [])

    def color_by_name(self, name):
        return palette_lib.find(self.palette, name)

    def save_color(self, name, rgb, index=None):
        """Add or update a colour. Returns the stored entry, or None.

        A name that already exists is replaced wherever it sits, so the menu
        order is not shuffled by an edit.
        """
        entry = palette_lib.normalise({'name': name, 'color': list(rgb)})
        if entry is None:
            return None

        existing = palette_lib.find(self.palette, entry['name'])
        if existing is not None:
            self._palette[self._palette.index(existing)] = entry
        elif index is not None and 0 <= index < len(self._palette):
            self._palette[index] = entry
        else:
            self._palette.append(entry)
        self.save_palette()
        return entry

    def remove_color(self, entry):
        if entry in self.palette:
            self._palette.remove(entry)
            self.save_palette()
            return True
        return False

    def move_color(self, index, offset):
        new_index = palette_lib.move(self.palette, index, offset)
        if new_index != index:
            self.save_palette()
        return new_index

    def reset_palette(self):
        self._palette = palette_lib.default_palette()
        self.save_palette()

    # -- cycling -----------------------------------------------------------
    #
    # A cycling scene is stepped by the background service, but it is normally
    # started from the control panel -- a different Kodi process. The two share
    # a small state file rather than a window property: a file survives a Kodi
    # restart, so a cycle that was running is picked up again, and the service
    # can poll it for the price of a stat call.

    CYCLE_FILE = 'cycle.json'

    def read_cycle(self):
        """The running cycle, or None."""
        state = utils.read_json(self.CYCLE_FILE, default=None)
        if not isinstance(state, dict) or not state.get('scene'):
            return None
        return state

    def start_cycle(self, scene, assignment, now=None):
        """Record that `scene` is cycling, with the arrangement now showing."""
        interval = int(scene.get('cycle') or 0)
        if interval <= 0 or scene.get('mode') != scene_lib.MODE_MIX:
            return None

        now = time.time() if now is None else now
        state = {
            'scene': scene['name'],
            'interval': interval,
            'assignment': dict(assignment or {}),
            # Absolute rather than a delay, so a service restart does not
            # reset the clock and make the lights sit still for a full
            # interval again.
            'next_at': now + interval,
        }
        utils.write_json(self.CYCLE_FILE, state)
        utils.log('Cycling "%s" every %ds over %d light(s)'
                  % (scene['name'], interval, len(state['assignment'])))
        return state

    def stop_cycle(self):
        """Stop any running cycle. Returns the name that was running, or None."""
        state = self.read_cycle()
        if state is None:
            return None
        utils.write_json(self.CYCLE_FILE, {})
        utils.log('Stopped cycling "%s"' % state.get('scene'))
        return state.get('scene')

    def cycle_due(self, now=None):
        """The running cycle if it is time to step it, else None."""
        state = self.read_cycle()
        if state is None:
            return None
        now = time.time() if now is None else now
        try:
            next_at = float(state.get('next_at') or 0)
        except (TypeError, ValueError):
            return state
        # A next_at far in the future means the clock moved backwards, or the
        # file was written by a box with a different time. Step rather than
        # hang for hours.
        if now >= next_at or next_at - now > state.get('interval', 60) * 2:
            return state
        return None

    def cycle_step(self, now=None, shuffle_func=None):
        """Advance a cycling scene by one colour. Returns True if it stepped."""
        state = self.read_cycle()
        if state is None:
            return False

        scene = self.scene_by_name(state.get('scene'))
        if scene is None or scene.get('mode') != scene_lib.MODE_MIX \
                or not scene.get('colors'):
            utils.log('Cycling scene "%s" is gone or no longer a mix; stopping'
                      % state.get('scene'))
            self.stop_cycle()
            return False

        targets = scene_lib.scene_targets(scene, self.devices)
        assignment = state.get('assignment') or {}

        # Lights that joined since the cycle started, or a cycle resumed from
        # disk with nothing recorded, need dealing in rather than being left
        # on whatever they happen to be showing.
        missing = [d.device_id for d in targets
                   if d.device_id not in assignment]
        if missing:
            assignment = dict(assignment)
            assignment.update(scene_lib.deal_assignment(
                len(scene['colors']), missing, shuffle_func))

        assignment = scene_lib.rotate_assignment(assignment,
                                                 len(scene['colors']))
        # Colour only: power and brightness are already where the previous
        # step left them, so re-sending them is two thirds of the traffic for
        # no visible effect.
        applied, errors = scene_lib.apply_scene(
            self.controller, scene, self.devices, log_func=utils.debug,
            assignment=assignment, colors_only=True)

        now = time.time() if now is None else now
        state['assignment'] = assignment
        state['next_at'] = now + int(state.get('interval') or 60)
        utils.write_json(self.CYCLE_FILE, state)

        utils.debug('Cycle step: %d light(s), %d error(s)'
                    % (applied, len(errors)))
        return True

    # -- scenes ------------------------------------------------------------

    @property
    def scenes(self):
        """Scene list, seeded with the defaults the first time it is read."""
        if self._scenes is None:
            raw = utils.read_json(scene_lib.SCENE_FILE, default=None)
            if raw is None:
                self._scenes = scene_lib.default_scenes()
                self.save_scenes()
            else:
                self._scenes = scene_lib.normalise_all(raw)
                if not self._scenes:
                    self._scenes = scene_lib.default_scenes()
        return self._scenes

    def save_scenes(self):
        utils.write_json(scene_lib.SCENE_FILE, self._scenes or [])

    def scene_by_name(self, name):
        return scene_lib.find(self.scenes, name)

    def capture_scene(self, name, timeout=3.0):
        """Snapshot what the lights are doing now into a named scene.

        Returns (scene, captured_count, skipped_names). Nothing is saved here;
        the caller decides whether to keep it.
        """
        devices = self.enabled_devices
        states = self.controller.get_states(devices, timeout=timeout)
        scene, captured, skipped = scene_lib.capture_scene(
            name, devices, states)

        # Logged unconditionally rather than behind verbose logging: capture
        # is a deliberate one-off action, and when the result looks wrong the
        # raw reading per bulb is the only thing that says why.
        utils.log('--- capture "%s": %d of %d light(s) ---'
                  % (name, captured, len(devices)))
        for device in devices:
            utils.log('  %s [%s] raw=%s -> %s'
                      % (device.name, device.ip,
                         states.get(device.device_id),
                         (scene.get('devices') or {}).get(device.device_id)))
        utils.log('--- end capture ---')
        return scene, captured, skipped

    @staticmethod
    def summarise_capture(scene):
        """Counts by mode and the brightness range, for the capture dialog.

        Wrong-looking captures have a shape: every bulb reading as white when
        they are coloured, or every brightness pinned at 100. Showing the
        spread makes that obvious on screen instead of only in the log.
        """
        entries = list((scene.get('devices') or {}).values())
        counts = {'off': 0, scene_lib.MODE_COLOR: 0, scene_lib.MODE_TEMP: 0,
                  scene_lib.MODE_NONE: 0}
        levels = []
        for entry in entries:
            if entry.get('power') == scene_lib.POWER_OFF:
                counts['off'] += 1
                continue
            counts[entry.get('mode', scene_lib.MODE_NONE)] += 1
            if entry.get('brightness') is not None:
                levels.append(entry['brightness'])

        bits = []
        if counts[scene_lib.MODE_COLOR]:
            bits.append('%d colour' % counts[scene_lib.MODE_COLOR])
        if counts[scene_lib.MODE_TEMP]:
            bits.append('%d white' % counts[scene_lib.MODE_TEMP])
        if counts[scene_lib.MODE_NONE]:
            bits.append('%d with no colour reading'
                        % counts[scene_lib.MODE_NONE])
        if counts['off']:
            bits.append('%d off' % counts['off'])
        if levels:
            low, high = min(levels), max(levels)
            if low == high:
                bits.append('brightness %d%%' % low)
            else:
                bits.append('brightness %d-%d%%' % (low, high))
        return ', '.join(bits)

    def save_scene(self, scene):
        """Add or replace a scene by name. Returns the normalised scene."""
        cleaned = scene_lib.normalise(scene)
        if cleaned is None:
            return None
        existing = scene_lib.find(self.scenes, cleaned['name'])
        if existing is not None:
            self._scenes[self._scenes.index(existing)] = cleaned
        else:
            self._scenes.append(cleaned)
        self.save_scenes()
        return cleaned

    def apply_scene(self, scene, announce=True):
        """Apply a scene dict and report the outcome. Returns True if any
        device accepted it.

        Applying anything stops a running cycle first. Otherwise dimming for a
        film would be overwritten by the party still stepping in the
        background, which is a confusing way to discover a cycle is running.
        """
        self.stop_cycle()

        assignment = None
        if scene.get('mode') == scene_lib.MODE_MIX and scene.get('colors'):
            targets = scene_lib.scene_targets(scene, self.devices)
            assignment = scene_lib.deal_assignment(
                len(scene['colors']), [d.device_id for d in targets])

        applied, errors = scene_lib.apply_scene(
            self.controller, scene, self.devices, log_func=utils.log,
            assignment=assignment)

        if applied and assignment and int(scene.get('cycle') or 0) > 0:
            self.start_cycle(scene, assignment)

        if applied and announce:
            utils.notify('%s applied to %d light(s)'
                         % (scene.get('name', 'Scene'), applied))
        if errors and not applied:
            utils.force_notify(errors[0])
        elif errors:
            utils.log('Scene applied with %d error(s): %s'
                      % (len(errors), '; '.join(errors[:3])))
        return applied > 0

    def apply_scene_by_name(self, name, announce=True):
        scene = self.scene_by_name(name)
        if scene is None:
            utils.log('No scene named "%s"' % name)
            if announce:
                utils.force_notify('No scene named "%s"' % name)
            return False
        return self.apply_scene(scene, announce=announce)

    # -- bulk actions ------------------------------------------------------

    def _each(self, action, targets=None):
        """Run `action(device)` over the targets, collecting failures.

        Every target here gets the same instruction, so a driver is allowed to
        fold several of them into one command -- a multi-outlet plug switches
        the whole box in a single packet rather than once per outlet.
        """
        from devices import ControlError

        done = 0
        errors = []
        chosen = targets if targets is not None else self.enabled_devices
        for device in self._collapse(chosen):
            try:
                action(device)
                done += 1
            except ControlError as exc:
                utils.log('Command failed on %s: %s' % (device.name, exc))
                errors.append(str(exc))
        return done, errors

    def _collapse(self, devices):
        collapse = getattr(self.controller, 'collapse', None)
        if collapse is None:
            return list(devices)
        return collapse(devices)

    def power_all(self, on, targets=None):
        return self._each(lambda d: self.controller.turn(d, on), targets)

    def brightness_all(self, percent, targets=None):
        return self._each(
            lambda d: self.controller.set_brightness(d, percent), targets)

    def color_all(self, rgb, targets=None):
        return self._each(
            lambda d: self.controller.set_color(d, rgb[0], rgb[1], rgb[2]),
            targets)

    def color_temp_all(self, kelvin, targets=None):
        return self._each(
            lambda d: self.controller.set_color_temp(d, kelvin), targets)

    def toggle_all(self, targets=None):
        """Flip the lights as a group.

        The group is treated as one switch: if any target reports itself on,
        the whole group goes off. When no state can be read -- LAN status needs
        UDP 4002, which may be busy -- it falls back to turning on, because a
        toggle that does nothing visible reads as a broken button.
        """
        devices = self._collapse(
            targets if targets is not None else self.enabled_devices)
        any_on = None
        for device in devices:
            state = self.controller.get_state(device)
            if not state:
                continue
            power = state.get('power')
            if power in ('on', 'off'):
                any_on = bool(any_on) or power == 'on'

        target_state = False if any_on else True
        return self.power_all(target_state, devices)
