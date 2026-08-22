# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Client for the Tuya LAN protocol (GHome, Gosund, Smart Life and the rest of
the Tuya OEM family).

Discovery is passive. Tuya devices announce themselves every few seconds by
UDP broadcast, so finding them is a matter of listening rather than asking:

    port 6666 -- protocol 3.1, payload in clear JSON
    port 6667 -- protocol 3.3 and later, payload AES-128-ECB encrypted with a
                 key every Tuya device shares, published in every open-source
                 Tuya client

Control is the part that needs permission. Each device has its own *local
key*, which exists only in Tuya's cloud -- the device never gives it out. That
is the one real difference from Govee, which is open, and Broadlink, whose
handshake hands you a key. Without it a device can be found and identified but
not commanded, which is why discovery is useful on its own: it tells you the
device id you need in order to go and fetch the key.

Control, once keyed: TCP to port 6668, the same 0x000055AA framing, payload
AES-128-ECB under the local key, CRC32 over the header and payload.
"""

import hashlib
import json
import socket
import struct
import time
import zlib

from aes import AESECB

DISCOVERY_PORT_CLEAR = 6666     # protocol 3.1
DISCOVERY_PORT_CRYPTO = 6667    # protocol 3.3+
CONTROL_PORT = 6668

# The key every Tuya device uses for its discovery broadcast. Not a secret in
# any meaningful sense: it is identical across all devices and appears in
# every open-source Tuya implementation.
DISCOVERY_KEY = hashlib.md5(b'yGAdlopoPVldABfn').digest()

PREFIX = 0x000055AA
SUFFIX = 0x0000AA55

# Commands used here. Tuya defines many more.
CMD_CONTROL = 0x07
CMD_STATUS = 0x0A          # DP_QUERY: ask for the current datapoints

# A smart plug's on/off is datapoint 1 on every Tuya plug seen so far.
DP_SWITCH = '1'


class TuyaError(Exception):
    """The device could not be reached, or refused the request."""


class TuyaKeyMissing(TuyaError):
    """The device was found but there is no local key for it yet.

    Kept distinct so the difference between "cannot reach it" and "not set up
    yet" is not flattened into one message; only one of the two is something
    the user can fix in the add-on.
    """


def _json_loads(raw):
    try:
        return json.loads(raw.decode('utf-8', 'replace'))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None


def parse_broadcast(data):
    """Decode one discovery datagram into a device dict, or None.

    Two things vary and neither is announced: where the payload starts, and
    whether it is encrypted.

    The header is 16 bytes, but a broadcast carrying a return code puts the
    payload 4 bytes further in. Rather than assume, both offsets are tried and
    whichever yields JSON wins -- the payload is self-describing enough to
    make that safe, and guessing wrong would mean silently finding nothing.

    The same applies to encryption: 3.1 broadcasts are clear, 3.3+ are
    AES-ECB under the shared discovery key, and a listener holding a datagram
    has no way to know which port it arrived on by the time it gets here.
    """
    data = bytearray(data)
    if len(data) < 24:
        return None

    tail = -8 if len(data) > 28 else None
    for start in (20, 16):
        body = bytes(data[start:tail])
        if not body:
            continue

        payload = _json_loads(body)
        if payload is None:
            try:
                payload = _json_loads(AESECB(DISCOVERY_KEY).decrypt(body))
            except (ValueError, TypeError):
                payload = None
        if not isinstance(payload, dict):
            continue

        device_id = payload.get('gwId') or payload.get('devId')
        if not device_id:
            continue

        return {
            'device_id': device_id,
            'ip': payload.get('ip', ''),
            'version': str(payload.get('version') or '3.1'),
            'product_key': payload.get('productKey', ''),
            'encrypted': bool(payload.get('encrypt', True)),
            'active': payload.get('active'),
            'raw': payload,
        }
    return None


def discover(timeout=6.0, log_func=None):
    """Listen for Tuya broadcasts. Returns a list of device dicts.

    Passive, so the timeout has to cover a device's announce interval rather
    than a round trip -- a few seconds, not a few hundred milliseconds.
    """
    log = log_func or (lambda message: None)
    sockets = []
    found = {}

    for port in (DISCOVERY_PORT_CLEAR, DISCOVERY_PORT_CRYPTO):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            reuse_port = getattr(socket, 'SO_REUSEPORT', None)
            if reuse_port is not None:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, reuse_port, 1)
                except socket.error:
                    pass
            sock.bind(('', port))
            sock.setblocking(False)
            sockets.append((port, sock))
        except socket.error as exc:
            sock.close()
            log('Could not listen on UDP %d: %s' % (port, exc))

    if not sockets:
        raise TuyaError('Could not listen on UDP %d or %d. Another Tuya '
                        'program is probably holding them.'
                        % (DISCOVERY_PORT_CLEAR, DISCOVERY_PORT_CRYPTO))

    deadline = time.time() + max(1.0, float(timeout))
    try:
        while time.time() < deadline:
            for _port, sock in sockets:
                try:
                    data, sender = sock.recvfrom(2048)
                except socket.error:
                    continue
                device = parse_broadcast(data)
                if device and device['device_id']:
                    device.setdefault('ip', sender[0])
                    if not device['ip']:
                        device['ip'] = sender[0]
                    found[device['device_id']] = device
            time.sleep(0.05)
    finally:
        for _port, sock in sockets:
            sock.close()

    log('Tuya discovery heard %d device(s)' % len(found))
    return list(found.values())


class Session(object):
    """One conversation with one Tuya device, over TCP."""

    def __init__(self, ip, device_id, local_key, version='3.3', timeout=5.0,
                 log_func=None):
        self.ip = ip
        self.device_id = device_id
        self.local_key = (local_key or '').encode('utf-8') \
            if not isinstance(local_key, bytes) else local_key
        self.version = str(version or '3.3')
        self.timeout = timeout
        self._log = log_func or (lambda message: None)
        self.sequence = 0

    @property
    def keyed(self):
        return len(self.local_key or b'') == 16

    def _cipher(self):
        if not self.keyed:
            raise TuyaKeyMissing(
                'No local key for %s. A Tuya device will not hand out its own '
                'key; it has to be read from your Tuya account once.'
                % self.device_id)
        return AESECB(self.local_key)

    # -- framing -----------------------------------------------------------

    def build_packet(self, command, payload):
        """Wrap a payload in Tuya's 0x000055AA framing."""
        payload = bytearray(payload)
        self.sequence += 1

        header = struct.pack('>4I', PREFIX, self.sequence, command,
                             len(payload) + 8)
        body = bytearray(header) + payload
        crc = zlib.crc32(bytes(body)) & 0xFFFFFFFF
        body.extend(struct.pack('>I', crc))
        body.extend(struct.pack('>I', SUFFIX))
        return bytes(body)

    def parse_packet(self, data):
        """Return the payload of a reply, raising on a device-reported error."""
        data = bytearray(data)
        if len(data) < 24:
            raise TuyaError('Short reply from %s (%d bytes)'
                            % (self.ip, len(data)))

        prefix = struct.unpack('>I', bytes(data[0:4]))[0]
        if prefix != PREFIX:
            raise TuyaError('%s sent an unrecognised reply' % self.ip)

        return_code = struct.unpack('>I', bytes(data[16:20]))[0]
        payload = bytes(data[20:-8])
        if return_code:
            # The device puts an error string where the payload would be.
            raise TuyaError('%s returned error %d: %s'
                            % (self.ip, return_code,
                               payload.decode('utf-8', 'replace').strip()))
        return payload

    def decode_payload(self, payload):
        """Strip the version header if present and decrypt to JSON."""
        if not payload:
            return {}

        # 3.3+ prefixes some replies with the protocol version and 12 bytes of
        # padding; the rest is ciphertext.
        if payload[:3] in (b'3.3', b'3.4', b'3.5', b'3.1'):
            payload = payload[15:]

        plain = payload
        if self.keyed:
            try:
                plain = self._cipher().decrypt(payload)
            except (ValueError, TypeError):
                plain = payload

        parsed = _json_loads(plain)
        return parsed if isinstance(parsed, dict) else {}
