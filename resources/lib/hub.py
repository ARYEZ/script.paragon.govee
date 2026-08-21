# -*- coding: utf-8 -*-
"""
Paragon Govee
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Routes every device operation to the driver that owns it.

One driver speaks one vendor's LAN protocol. The Hub presents the union of
them as a single set of devices, so the registry, the scene engine and the
menus never learn what a Govee bulb is -- they ask the Hub, and the Hub asks
whichever driver discovered the device.

A driver implements:

    DRIVER_ID, DRIVER_LABEL     identity
    discover(timeout)           -> ([Device], [warning strings])
    capabilities(device)        -> set of CAP_* it supports
    turn/set_brightness/set_color/set_color_temp
                                state verbs, raising ControlError on failure
    get_state(device)           -> dict or None
    get_states(devices, timeout)-> {device_id: state or None}
    commands(device)            -> [names] it can emit, [] for most
    send_command(device, name)  fire one, raising ControlError

Only what a device claims in capabilities() is ever called on it, so a driver
for something with no colour -- a plug, an IR blaster -- implements the verbs
it has and reports the rest as absent.
"""

from devices import CAP_COMMANDS, ControlError, DEFAULT_DRIVER


class Hub(object):
    """The set of drivers, addressed as one device layer."""

    def __init__(self, drivers=None, log_func=None):
        self.drivers = {}
        for driver in drivers or []:
            self.drivers[driver.DRIVER_ID] = driver
        self._log = log_func or (lambda message: None)

    # -- driver lookup -----------------------------------------------------

    def driver(self, driver_id):
        return self.drivers.get(driver_id or DEFAULT_DRIVER)

    def driver_for(self, device):
        """The driver that owns `device`.

        Falls back to the default rather than failing: a devices.json written
        before drivers existed records no driver, and every entry in it is a
        Govee light.
        """
        found = self.drivers.get(getattr(device, 'driver', None)
                                 or DEFAULT_DRIVER)
        if found is None:
            found = self.drivers.get(DEFAULT_DRIVER)
        return found

    def _require(self, device):
        driver = self.driver_for(device)
        if driver is None:
            raise ControlError('%s has no driver installed for "%s"'
                               % (device.name, getattr(device, 'driver', '?')))
        return driver

    # -- discovery ---------------------------------------------------------

    def discover(self, timeout=3.0):
        """Search with every driver. Returns (devices, warnings).

        One driver failing never stops the others: a Govee search that cannot
        bind its port should not hide the IR blasters.
        """
        found = []
        warnings = []
        for driver_id in sorted(self.drivers):
            driver = self.drivers[driver_id]
            try:
                devices, driver_warnings = driver.discover(timeout=timeout)
            except Exception as exc:
                self._log('%s discovery failed: %s' % (driver_id, exc))
                warnings.append('%s search failed: %s'
                                % (driver.DRIVER_LABEL, exc))
                continue
            for device in devices:
                device.driver = driver_id
            found.extend(devices)
            warnings.extend(driver_warnings or [])
        return found, warnings

    # -- capabilities ------------------------------------------------------

    def capabilities(self, device):
        driver = self.driver_for(device)
        if driver is None:
            return set()
        return driver.capabilities(device)

    def commands(self, device):
        driver = self.driver_for(device)
        if driver is None:
            return []
        return driver.commands(device)

    def send_command(self, device, name):
        driver = self._require(device)
        if CAP_COMMANDS not in driver.capabilities(device):
            raise ControlError('%s does not send commands' % device.name)
        return driver.send_command(device, name)

    # -- state verbs -------------------------------------------------------

    def turn(self, device, on):
        return self._require(device).turn(device, on)

    def set_brightness(self, device, percent):
        return self._require(device).set_brightness(device, percent)

    def set_color(self, device, red, green, blue):
        return self._require(device).set_color(device, red, green, blue)

    def set_color_temp(self, device, kelvin):
        return self._require(device).set_color_temp(device, kelvin)

    def get_state(self, device):
        driver = self.driver_for(device)
        if driver is None:
            return None
        return driver.get_state(device)

    def get_states(self, devices, timeout=3.0):
        """Read many devices at once, letting each driver batch its own.

        Grouping by driver is what keeps the Govee bulk sweep -- one socket,
        one timeout for all 25 bulbs -- rather than degrading to one round
        trip per device once a second vendor is present.
        """
        states = {}
        by_driver = {}
        for device in devices:
            by_driver.setdefault(getattr(device, 'driver', None)
                                 or DEFAULT_DRIVER, []).append(device)

        for driver_id, group in by_driver.items():
            driver = self.drivers.get(driver_id)
            if driver is None:
                for device in group:
                    states[device.device_id] = None
                continue
            try:
                states.update(driver.get_states(group, timeout=timeout))
            except Exception as exc:
                self._log('%s state read failed: %s' % (driver_id, exc))
                for device in group:
                    states.setdefault(device.device_id, None)
        return states

    # -- Govee-specific passthrough ----------------------------------------

    def pick_transport(self, device):
        """Which transport a device would use, when its driver has the idea.

        Only Govee has a LAN/cloud split; anything else answers None. Used by
        the cloud-quota warning, which has nothing to say about a driver with
        no cloud.
        """
        driver = self.driver_for(device)
        picker = getattr(driver, 'pick_transport', None)
        if picker is None:
            return None
        return picker(device)

    @property
    def mode(self):
        """The Govee transport mode, for the diagnostics report."""
        driver = self.drivers.get(DEFAULT_DRIVER)
        return getattr(driver, 'mode', None)

    @property
    def lan(self):
        """The Govee LAN transport, for the LAN diagnostics probe."""
        driver = self.drivers.get(DEFAULT_DRIVER)
        return getattr(driver, 'lan', None)

    @property
    def cloud(self):
        driver = self.drivers.get(DEFAULT_DRIVER)
        return getattr(driver, 'cloud', None)
