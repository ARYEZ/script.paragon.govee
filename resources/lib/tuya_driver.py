# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The Tuya driver: smart plugs and switches from the Tuya OEM family, which
covers GHome, Gosund, Smart Life and most no-name plugs.

A plug is the simplest device shape so far: power, and nothing else. What is
not simple is permission. Every Tuya device needs its own *local key*, which
lives in Tuya's cloud and is never offered by the device, so a plug can be
discovered and identified long before it can be switched.

That shapes the driver: a device with no key is still a real, listed,
nameable device. It simply reports that it needs one, rather than being
hidden or looking broken.
"""

import tuya_lan
from devices import CAP_POWER, CAP_STATE, ControlError, Device

KEY_FILE = 'tuya_keys.json'


class TuyaDriver(object):
    """Discovers Tuya devices and switches the ones that have a key."""

    DRIVER_ID = 'tuya'
    DRIVER_LABEL = 'Tuya'

    def __init__(self, keys=None, log_func=None, save_keys=None,
                 timeout=5.0):
        # {device_id: local key}
        self.keys = keys if keys is not None else {}
        self.timeout = timeout
        self._log = log_func or (lambda message: None)
        self._save_keys = save_keys or (lambda: None)

    # -- discovery ---------------------------------------------------------

    def discover(self, timeout=3.0):
        """Listen for Tuya announcements.

        Tuya devices broadcast rather than answer, so the wait is at least one
        announce interval. A short discovery timeout tuned for Govee's
        request/response sweep would simply hear nothing.
        """
        listen = max(6.0, float(timeout) * 2)
        try:
            heard = tuya_lan.discover(timeout=listen, log_func=self._log)
        except tuya_lan.TuyaError as exc:
            return [], [str(exc)]

        devices = []
        unkeyed = 0
        for entry in heard:
            device = Device(
                driver=self.DRIVER_ID,
                device_id=entry['device_id'],
                native_id=entry['device_id'],
                model='Tuya %s' % entry.get('version', ''),
                ip=entry.get('ip', ''),
                lan=True,
            )
            device.tuya_version = entry.get('version', '3.3')
            devices.append(device)
            if not self.local_key(device):
                unkeyed += 1

        warnings = []
        if unkeyed:
            warnings.append(
                '%d Tuya device(s) were found but have no local key yet, so '
                'they cannot be switched. Add a key from Manage devices.'
                % unkeyed)
        return devices, warnings

    # -- capabilities ------------------------------------------------------

    @staticmethod
    def capabilities(device):
        """A plug switches, and reports whether it is on. That is all."""
        return set([CAP_POWER, CAP_STATE])

    @staticmethod
    def commands(device):
        return []

    def send_command(self, device, name):
        raise ControlError('%s does not send commands' % device.name)

    # -- keys --------------------------------------------------------------

    @staticmethod
    def native_id(device):
        """The id Tuya itself uses -- lower case, exactly as broadcast."""
        return getattr(device, 'native_id', None) or device.device_id

    def local_key(self, device):
        return self.keys.get(self.native_id(device), '')

    def set_local_key(self, device, key):
        key = (key or '').strip()
        if not key:
            self.keys.pop(self.native_id(device), None)
            self._save_keys()
            return True
        if len(key) != 16:
            return False
        self.keys[self.native_id(device)] = key
        self._save_keys()
        self._log('Stored a local key for %s' % device.name)
        return True

    def needs_key(self, device):
        return len(self.local_key(device)) != 16

    def _session(self, device):
        if self.needs_key(device):
            raise ControlError(
                '%s has no local key yet.\n\nTuya devices never hand out '
                'their own key -- it has to be read from your Tuya account '
                'once. Use "Set local key" in Manage devices.' % device.name)
        return tuya_lan.Session(
            device.ip, self.native_id(device), self.local_key(device),
            version=getattr(device, 'tuya_version', '3.3'),
            timeout=self.timeout, log_func=self._log)

    # -- state verbs -------------------------------------------------------

    def turn(self, device, on):
        raise ControlError(
            '%s cannot be switched yet: control needs the local key, and the '
            'protocol version it reported has not been wired up.'
            % device.name)

    def set_brightness(self, device, percent):
        raise ControlError('%s has no brightness' % device.name)

    def set_color(self, device, red, green, blue):
        raise ControlError('%s has no colour' % device.name)

    def set_color_temp(self, device, kelvin):
        raise ControlError('%s has no colour' % device.name)

    def get_state(self, device):
        return None

    def get_states(self, devices, timeout=3.0):
        return dict((d.device_id, None) for d in devices)
