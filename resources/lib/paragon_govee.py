# -*- coding: utf-8 -*-
"""
Paragon Govee
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The add-on session: reads Kodi settings, builds a controller, and owns the
cached device list and the scene list.

Both entry points (the interactive script and the background service) create
one of these. Nothing here draws UI, so the same object serves a dialog-driven
menu and a silent playback callback.
"""

import addon_utils as utils
import scenes as scene_lib
from devices import (DEVICE_CACHE, Device, TRANSPORT_AUTO, TRANSPORT_CLOUD,
                     TRANSPORT_LAN, build_controller)

# Order must match the `values` list on the transport_mode setting.
_TRANSPORT_MODES = [TRANSPORT_AUTO, TRANSPORT_LAN, TRANSPORT_CLOUD]


class ParagonGovee(object):
    """Everything the add-on needs, assembled from the user's settings."""

    def __init__(self):
        self.controller = build_controller(self.read_settings())
        self._devices = None
        self._scenes = None

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

        self._devices = found
        self.save_devices()
        utils.log('Device refresh stored %d device(s)' % len(found))
        return found, warnings

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

    def apply_scene(self, scene, announce=True):
        """Apply a scene dict and report the outcome. Returns True if any
        device accepted it."""
        applied, errors = scene_lib.apply_scene(
            self.controller, scene, self.devices, log_func=utils.log)

        if applied and announce:
            utils.notify('%s applied to %d light(s)'
                         % (scene.get('name', 'Scene'), applied))
        if errors and not applied:
            utils.force_notify(errors[0], heading='Paragon Govee')
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
        """Run `action(device)` over the targets, collecting failures."""
        from devices import ControlError

        done = 0
        errors = []
        for device in (targets if targets is not None else self.enabled_devices):
            try:
                action(device)
                done += 1
            except ControlError as exc:
                utils.log('Command failed on %s: %s' % (device.name, exc))
                errors.append(str(exc))
        return done, errors

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
        devices = targets if targets is not None else self.enabled_devices
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
