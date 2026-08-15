# -*- coding: utf-8 -*-
"""
Paragon Govee
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

One device abstraction over the two Govee transports.

Discovery runs against both the LAN and the cloud and merges the results on the
Govee device id, so a light that answers on both is a single entry the user can
act on. Which transport a command actually goes out on is decided per device at
send time: LAN when it is available (instant, unmetered), cloud otherwise.
"""

from govee_cloud import CloudError, CloudTransport, RateLimited
from govee_lan import (LANError, LANTransport, brightness_message,
                       color_message, color_temp_message, turn_message)

TRANSPORT_AUTO = 'auto'
TRANSPORT_LAN = 'lan'
TRANSPORT_CLOUD = 'cloud'

DEFAULT_TEMP_MIN = 2000
DEFAULT_TEMP_MAX = 9000

DEVICE_CACHE = 'devices.json'


class ControlError(Exception):
    """A command could not be delivered to a device."""


def _short_id(device_id):
    """Last two octets of a Govee id, used to label unnamed LAN devices."""
    parts = [p for p in (device_id or '').split(':') if p]
    return ''.join(parts[-2:]) if parts else '????'


class Device(object):
    """A single Govee light, reachable over LAN, cloud, or both."""

    def __init__(self, device_id, name='', model='', ip='', lan=False,
                 cloud=False, supports=None, temp_range=None, enabled=True):
        self.device_id = (device_id or '').upper()
        self.model = model or ''
        self.ip = ip or ''
        self.lan = bool(lan)
        self.cloud = bool(cloud)
        self.supports = list(supports or [])
        self.temp_range = temp_range or [DEFAULT_TEMP_MIN, DEFAULT_TEMP_MAX]
        self.enabled = bool(enabled)
        self.name = name or self._fallback_name()

    def _fallback_name(self):
        """LAN discovery has no friendly name, so build one from sku + id."""
        model = self.model or 'Govee'
        return '%s (%s)' % (model, _short_id(self.device_id))

    # -- capability checks -------------------------------------------------

    def supports_cmd(self, name):
        """Whether `name` is usable on this device.

        Only the cloud API reports a capability list. LAN devices are assumed
        capable: the LAN protocol has no discovery for this, and an unsupported
        command is simply ignored by the device rather than being an error.
        """
        if not self.supports:
            return True
        return name in self.supports

    def transports(self):
        available = []
        if self.lan:
            available.append(TRANSPORT_LAN)
        if self.cloud:
            available.append(TRANSPORT_CLOUD)
        return available

    # -- serialisation -----------------------------------------------------

    def to_dict(self):
        return {
            'device_id': self.device_id,
            'name': self.name,
            'model': self.model,
            'ip': self.ip,
            'lan': self.lan,
            'cloud': self.cloud,
            'supports': self.supports,
            'temp_range': self.temp_range,
            'enabled': self.enabled,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            device_id=data.get('device_id', ''),
            name=data.get('name', ''),
            model=data.get('model', ''),
            ip=data.get('ip', ''),
            lan=data.get('lan', False),
            cloud=data.get('cloud', False),
            supports=data.get('supports'),
            temp_range=data.get('temp_range'),
            enabled=data.get('enabled', True),
        )

    def merge(self, other):
        """Fold another discovery result for the same device into this one."""
        self.lan = self.lan or other.lan
        self.cloud = self.cloud or other.cloud
        self.ip = other.ip or self.ip
        self.model = self.model or other.model
        self.supports = other.supports or self.supports
        if other.temp_range:
            self.temp_range = other.temp_range
        # A cloud name is the one the user chose in the Govee app, so it wins
        # over the placeholder LAN discovery had to invent.
        if other.cloud and other.name and not other.name.startswith(
                other.model + ' ('):
            self.name = other.name
        return self

    def __repr__(self):
        return '<Device %s %s via %s>' % (
            self.device_id, self.name, '+'.join(self.transports()) or 'none')


class GoveeController(object):
    """Discovers devices and routes commands to the right transport."""

    def __init__(self, lan=None, cloud=None, mode=TRANSPORT_AUTO,
                 log_func=None):
        self.lan = lan
        self.cloud = cloud
        self.mode = mode or TRANSPORT_AUTO
        self._log = log_func or (lambda message: None)

    # -- discovery ---------------------------------------------------------

    def discover(self, timeout=3.0):
        """Return merged devices plus a list of human-readable warnings.

        Errors from one transport never abort the other: a missing API key
        should not stop LAN lights being found, and a firewalled UDP port
        should not hide cloud lights.
        """
        merged = {}
        warnings = []

        if self.mode in (TRANSPORT_AUTO, TRANSPORT_LAN) and self.lan:
            try:
                for raw in self.lan.discover(timeout=timeout):
                    device = Device(
                        device_id=raw.get('device', ''),
                        model=raw.get('sku', ''),
                        ip=raw.get('ip', ''),
                        lan=True,
                    )
                    if device.device_id:
                        merged[device.device_id] = device
            except LANError as exc:
                warnings.append(str(exc))
                self._log('LAN discovery failed: %s' % exc)

        if self.mode in (TRANSPORT_AUTO, TRANSPORT_CLOUD) and self.cloud \
                and self.cloud.configured:
            try:
                for raw in self.cloud.list_devices():
                    properties = raw.get('properties') or {}
                    temp = (properties.get('colorTem') or {}).get('range') or {}
                    device = Device(
                        device_id=raw.get('device', ''),
                        name=raw.get('deviceName', ''),
                        model=raw.get('model', ''),
                        cloud=bool(raw.get('controllable', True)),
                        supports=raw.get('supportCmds'),
                        temp_range=[temp.get('min', DEFAULT_TEMP_MIN),
                                    temp.get('max', DEFAULT_TEMP_MAX)],
                    )
                    if not device.device_id:
                        continue
                    existing = merged.get(device.device_id)
                    if existing:
                        existing.merge(device)
                    else:
                        merged[device.device_id] = device
            except (CloudError, RateLimited) as exc:
                warnings.append(str(exc))
                self._log('Cloud discovery failed: %s' % exc)

        devices = sorted(merged.values(), key=lambda d: d.name.lower())
        return devices, warnings

    # -- transport selection ----------------------------------------------

    def pick_transport(self, device):
        """Decide how to reach `device`, honouring the user's mode setting."""
        if self.mode == TRANSPORT_LAN:
            return TRANSPORT_LAN if device.lan else None
        if self.mode == TRANSPORT_CLOUD:
            return TRANSPORT_CLOUD if device.cloud else None
        if device.lan and device.ip and self.lan:
            return TRANSPORT_LAN
        if device.cloud and self.cloud and self.cloud.configured:
            return TRANSPORT_CLOUD
        # A LAN device with no usable cloud fallback is still worth trying.
        if device.lan and self.lan:
            return TRANSPORT_LAN
        return None

    def _send(self, device, lan_message, cloud_name, cloud_value):
        transport = self.pick_transport(device)
        if transport is None:
            raise ControlError('%s is not reachable. Run a device refresh, or '
                               'set a Govee API key for cloud control.'
                               % device.name)

        if transport == TRANSPORT_LAN:
            if not device.ip:
                raise ControlError('%s has no known IP address; refresh '
                                   'devices.' % device.name)
            if self.lan.send(device.ip, lan_message):
                return TRANSPORT_LAN
            # LAN is best-effort. If the datagram could not even be handed to
            # the OS, fall through to the cloud rather than failing outright.
            if self.mode == TRANSPORT_AUTO and device.cloud and self.cloud \
                    and self.cloud.configured:
                self._log('LAN send to %s failed, retrying via cloud'
                          % device.name)
            else:
                raise ControlError('Could not reach %s over the LAN'
                                   % device.name)

        try:
            self.cloud.control(device.device_id, device.model, cloud_name,
                               cloud_value)
        except RateLimited as exc:
            raise ControlError(str(exc))
        except CloudError as exc:
            raise ControlError('%s: %s' % (device.name, exc))
        return TRANSPORT_CLOUD

    # -- commands ----------------------------------------------------------

    def turn(self, device, on):
        return self._send(device, turn_message(on), 'turn',
                          'on' if on else 'off')

    def set_brightness(self, device, percent):
        percent = max(1, min(100, int(percent)))
        return self._send(device, brightness_message(percent), 'brightness',
                          percent)

    def set_color(self, device, red, green, blue):
        rgb = [max(0, min(255, int(v))) for v in (red, green, blue)]
        return self._send(device, color_message(*rgb), 'color',
                          {'r': rgb[0], 'g': rgb[1], 'b': rgb[2]})

    def set_color_temp(self, device, kelvin):
        low, high = device.temp_range or [DEFAULT_TEMP_MIN, DEFAULT_TEMP_MAX]
        kelvin = max(int(low), min(int(high), int(kelvin)))
        return self._send(device, color_temp_message(kelvin), 'colorTem',
                          kelvin)

    def get_state(self, device):
        """Best-effort current state as a dict, or None if unavailable."""
        transport = self.pick_transport(device)
        if transport == TRANSPORT_LAN and device.ip:
            state = self.lan.status(device.ip)
            if state is not None:
                return {
                    'power': 'on' if state.get('onOff') else 'off',
                    'brightness': state.get('brightness'),
                    'color': state.get('color'),
                    'colorTem': state.get('colorTemInKelvin'),
                    'source': TRANSPORT_LAN,
                }
            if self.mode != TRANSPORT_AUTO or not device.cloud:
                return None

        if device.cloud and self.cloud and self.cloud.configured:
            try:
                state = self.cloud.state(device.device_id, device.model)
            except (CloudError, RateLimited) as exc:
                self._log('State lookup failed for %s: %s' % (device.name, exc))
                return None
            state['source'] = TRANSPORT_CLOUD
            return state
        return None


def build_controller(settings):
    """Assemble a controller from a plain settings dict.

    Kept free of Kodi imports so the whole control path can be exercised in
    tests without a running Kodi.
    """
    lan = LANTransport(
        bind_address=settings.get('bind_address', ''),
        retries=settings.get('command_retries', 2),
        log_func=settings.get('log_func'),
    )
    cloud = CloudTransport(
        api_key=settings.get('api_key', ''),
        timeout=settings.get('cloud_timeout', 10),
        verify_ssl=settings.get('verify_ssl', True),
        log_func=settings.get('log_func'),
    )
    return GoveeController(
        lan=lan,
        cloud=cloud,
        mode=settings.get('mode', TRANSPORT_AUTO),
        log_func=settings.get('log_func'),
    )
