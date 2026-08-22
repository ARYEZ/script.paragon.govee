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

The other thing a plug is not, sometimes, is one plug. A multi-outlet strip
is several independent switches in one box, and listing it as a single device
would mean a scene could only turn all of it on or all of it off. So a keyed
plug is asked what it has and listed as one device per outlet.
"""

import tuya_lan
from devices import CAP_POWER, CAP_STATE, ControlError, Device

KEY_FILE = 'tuya_keys.json'

# Tuya's socket instruction set is fixed across the OEM brands: mains outlets
# occupy datapoints 1-6 and USB banks 7-8, whatever the plug is sold as.
# Everything from 9 up is countdowns, child lock, relay memory and the rest --
# which is what keeps a child-lock toggle from being listed as an outlet.
OUTLET_DPS = ('1', '2', '3', '4', '5', '6')
USB_DPS = ('7', '8')
SWITCH_DPS = OUTLET_DPS + USB_DPS


def outlet_label(dp):
    """A human name for one switchable datapoint."""
    dp = str(dp)
    if dp in USB_DPS:
        position = USB_DPS.index(dp)
        return 'USB' if position == 0 else 'USB %d' % (position + 1)
    if dp in OUTLET_DPS:
        return 'Outlet %d' % (OUTLET_DPS.index(dp) + 1)
    return 'Switch %s' % dp


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
        unsupported = []
        for entry in heard:
            found = self._devices_for(entry)
            devices.extend(found)
            if self.needs_key(found[0]):
                unkeyed += 1
            else:
                note = tuya_lan.version_note(self.version_of(found[0]))
                if note:
                    unsupported.append(found[0].name)

        warnings = []
        if unkeyed:
            warnings.append(
                '%d Tuya device(s) need a local key before they can be '
                'switched. Manage devices -> the plug -> Set local key.'
                % unkeyed)
        if unsupported:
            warnings.append(
                '%d Tuya device(s) speak a protocol version this add-on '
                'cannot drive yet: %s.'
                % (len(unsupported), ', '.join(sorted(unsupported))))
        return devices, warnings

    def _build_device(self, entry, dp=None):
        """One listed device, for the whole plug or for one of its outlets."""
        device_id = entry['device_id']
        version = str(entry.get('version') or '3.3')
        name = ''
        if dp:
            name = 'Tuya %s %s' % (device_id[-4:].upper(), outlet_label(dp))
        device = Device(
            driver=self.DRIVER_ID,
            device_id='%s#%s' % (device_id, dp) if dp else device_id,
            native_id=device_id,
            name=name,
            model='Tuya %s' % version,
            ip=entry.get('ip', ''),
            lan=True,
            driver_data={'version': version, 'dp': dp} if dp
            else {'version': version},
        )
        return device

    def _devices_for(self, entry):
        """List one entry per outlet, or one for the plug as a whole.

        Which datapoints a plug actually has can only be learnt by asking it,
        and asking needs the key -- so an unkeyed plug is listed as one device
        and splits itself on the first search after a key is entered. The
        alternative, splitting on the product id, would mean carrying a table
        of every plug ever made and being wrong about the ones not in it.

        Anything that answers with a single switch stays a single device: a
        one-outlet plug should not be called "Outlet 1".
        """
        base = self._build_device(entry)
        if self.needs_key(base) or tuya_lan.version_note(self.version_of(base)):
            return [base]

        try:
            dps = self._session(base).status()
        except (tuya_lan.TuyaError, ControlError) as exc:
            self._log('Could not read %s to find its outlets: %s'
                      % (base.name, exc))
            return [base]

        switches = [dp for dp in SWITCH_DPS if isinstance(dps.get(dp), bool)]
        if len(switches) < 2:
            return [base]
        self._log('%s has %d switchable outlets' % (base.name, len(switches)))
        return [self._build_device(entry, dp) for dp in switches]

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

    def test_connection(self, device):
        """Read the device and report the result in words.

        Worth having for the moment straight after a 16 character key has been
        typed in on a remote control, which is the likeliest place for this to
        go wrong and the least obvious place to notice.
        """
        try:
            dps = self._session(device).status()
        except (tuya_lan.TuyaError, ControlError) as exc:
            return False, str(exc)

        state = self._state_from_dps(device, dps)
        if state is None:
            return False, ('%s answered, but datapoint %s is not a switch on '
                           'this device.\n\nIt reported: %s'
                           % (device.name, self.switch_dp(device),
                              ', '.join(sorted(str(k) for k in dps)) or 'nothing'))
        return True, ('%s answered and the key was accepted.\n\n'
                      'It is currently %s.' % (device.name, state['power']))

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

    @staticmethod
    def version_of(device):
        """The protocol version this device announced when it was found."""
        return str(getattr(device, 'driver_data', None)
                   and device.driver_data.get('version') or '3.3')

    @staticmethod
    def switch_dp(device):
        """Which datapoint this entry switches.

        A device listed before outlets were split -- or a plug with only one
        outlet -- has no datapoint recorded, and datapoint 1 is the switch on
        every single-outlet Tuya plug.
        """
        data = getattr(device, 'driver_data', None) or {}
        return str(data.get('dp') or tuya_lan.DP_SWITCH)

    def _session(self, device):
        if self.needs_key(device):
            raise ControlError(
                '%s has no local key yet.\n\nTuya devices never hand out '
                'their own key -- it has to be read from your Tuya account '
                'once. Use "Set local key" in Manage devices.' % device.name)
        return tuya_lan.Session(
            device.ip, self.native_id(device), self.local_key(device),
            version=self.version_of(device),
            timeout=self.timeout, log_func=self._log)

    # -- state verbs -------------------------------------------------------

    def turn(self, device, on):
        try:
            self._session(device).set_dps({self.switch_dp(device): bool(on)})
        except tuya_lan.TuyaError as exc:
            raise ControlError('%s: %s' % (device.name, exc))
        return True

    def set_brightness(self, device, percent):
        raise ControlError('%s has no brightness' % device.name)

    def set_color(self, device, red, green, blue):
        raise ControlError('%s has no colour' % device.name)

    def set_color_temp(self, device, kelvin):
        raise ControlError('%s has no colour' % device.name)

    def _state_from_dps(self, device, dps):
        """This entry's switch, as the state dict the rest of the add-on uses."""
        value = (dps or {}).get(self.switch_dp(device))
        if not isinstance(value, bool):
            return None
        # 'on'/'off' rather than a bool: that is the vocabulary the scene
        # engine reads, so a plug captured into a scene needs no special case.
        return {'power': 'on' if value else 'off', 'dps': dps}

    def get_state(self, device):
        try:
            dps = self._session(device).status()
        except (tuya_lan.TuyaError, ControlError) as exc:
            self._log('Could not read %s: %s' % (device.name, exc))
            return None
        return self._state_from_dps(device, dps)

    def get_states(self, devices, timeout=3.0):
        """Read every listed device, one round trip per physical plug.

        Outlets of the same plug share a status reply, so a three-outlet strip
        is one conversation rather than three -- which also keeps the three
        readings consistent with each other.
        """
        states = {}
        by_plug = {}
        order = []
        for device in devices:
            plug = self.native_id(device)
            if plug not in by_plug:
                by_plug[plug] = []
                order.append(plug)
            by_plug[plug].append(device)

        for plug in order:
            group = by_plug[plug]
            try:
                dps = self._session(group[0]).status()
            except (tuya_lan.TuyaError, ControlError) as exc:
                self._log('Could not read %s: %s' % (group[0].name, exc))
                dps = None
            for device in group:
                states[device.device_id] = self._state_from_dps(device, dps)
        return states
