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

import base64
import binascii
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

# A single-outlet plug switches on datapoint 1. Multi-outlet plugs use one
# datapoint per outlet; see tuya_driver for the allocation.
DP_SWITCH = '1'

# 3.1 through 3.3 differ only in how a payload is wrapped, which is a few
# lines either way. 3.4 and 3.5 negotiate a per-connection session key before
# anything else can be said, and 3.5 moves to AES-GCM -- a different job, not
# a bigger version number.
SUPPORTED_VERSIONS = ('3.1', '3.2', '3.3')


def version_note(version):
    """Why this protocol version cannot be driven, or None if it can."""
    version = str(version or '3.3')
    if version[:3] in SUPPORTED_VERSIONS:
        return None
    return ('This device speaks Tuya %s. Paragon Home drives 3.1 to 3.3; '
            '3.4 and later negotiate a session key first, which is not built '
            'yet.' % version)


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
        """Strip whatever header a reply carries and decrypt it to JSON.

        The header is not the same width in every version, which is easy to
        miss because both look like "version plus some bytes": 3.1 follows the
        version with a 16 character signature and base64, while 3.3 follows it
        with 12 bytes of padding and raw ciphertext. Reading a 3.1 reply with
        the 3.3 rule lands mid-signature and decrypts to noise.
        """
        if not payload:
            return {}

        payload = bytes(payload)
        marker = payload[:3]
        if marker == b'3.1':
            try:
                payload = base64.b64decode(payload[19:])
            except (TypeError, ValueError, binascii.Error):
                return {}
        elif marker in (b'3.2', b'3.3', b'3.4', b'3.5'):
            payload = payload[15:]

        plain = payload
        if self.keyed:
            try:
                plain = self._cipher().decrypt(payload)
            except (ValueError, TypeError):
                plain = payload

        parsed = _json_loads(plain)
        if parsed is None and plain != payload:
            # Some replies -- an echo of a control, mostly -- come back in
            # clear even on an encrypted connection.
            parsed = _json_loads(payload)
        return parsed if isinstance(parsed, dict) else {}

    # -- requests ----------------------------------------------------------

    def _body(self, command):
        """The JSON body a given command expects."""
        stamp = str(int(time.time()))
        if command == CMD_STATUS:
            return {'gwId': self.device_id, 'devId': self.device_id,
                    'uid': self.device_id, 't': stamp}
        return {'devId': self.device_id, 'uid': self.device_id, 't': stamp}

    def build_command_payload(self, command, body):
        """Encrypt and header a JSON body the way this version expects.

        Three rules vary by version and none of them is negotiated, so they
        are spelled out rather than inferred from the reply:

        3.1 sends status queries in clear, and signs control payloads with an
            md5 over the base64 ciphertext.
        3.3 encrypts both, and -- the rule that catches everyone -- puts the
            version header on a control but *not* on a status query. A status
            query sent with the header comes back as an error, which reads
            like a bad key and is not.
        """
        raw = json.dumps(body, separators=(',', ':')).encode('utf-8')

        if self.version.startswith('3.1'):
            if command == CMD_STATUS:
                return raw
            encoded = base64.b64encode(self._cipher().encrypt(raw))
            signature = hashlib.md5(
                b'data=' + encoded + b'||lpv=3.1||' + self.local_key
            ).hexdigest()[8:24].encode('utf-8')
            return b'3.1' + signature + encoded

        encrypted = self._cipher().encrypt(raw)
        if command == CMD_STATUS:
            return encrypted
        return self.version[:3].encode('utf-8') + (b'\x00' * 12) + encrypted

    @staticmethod
    def split_packets(buffer):
        """Split a receive buffer into whole packets and the leftover.

        A single read can hold more than the answer: a device often pushes an
        unsolicited status update alongside the reply, and TCP is free to cut
        the stream anywhere. Framing by the declared length rather than by
        read boundaries is what makes that harmless.
        """
        data = bytes(buffer)
        marker = struct.pack('>I', PREFIX)
        packets = []
        while True:
            start = data.find(marker)
            if start < 0 or len(data) - start < 24:
                break
            length = struct.unpack('>I', data[start + 12:start + 16])[0]
            end = start + 16 + length
            if length > 0xFFFF or len(data) < end:
                break
            packets.append(data[start:end])
            data = data[end:]
        return packets, data

    def _exchange(self, command, body=None):
        """Send one request and return the first reply that answers it.

        A fresh connection per exchange rather than a kept-open socket: Tuya
        devices drop an idle connection without saying so, and a plug is
        switched a few times an hour, not a few times a second. Reconnecting
        costs a few milliseconds; a stale socket costs a silent failure.
        """
        note = version_note(self.version)
        if note:
            raise TuyaError(note)

        if body is None:
            body = self._body(command)
        packet = self.build_packet(command,
                                   self.build_command_payload(command, body))

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            try:
                sock.connect((self.ip, CONTROL_PORT))
                sock.sendall(packet)
            except socket.error as exc:
                raise TuyaError('Could not reach %s on port %d: %s'
                                % (self.ip, CONTROL_PORT, exc))

            buffer = b''
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                except socket.error as exc:
                    raise TuyaError('%s closed the connection: %s'
                                    % (self.ip, exc))
                if not chunk:
                    break

                buffer += chunk
                packets, buffer = self.split_packets(buffer)
                for reply in packets:
                    answer = self._read_reply(command, reply)
                    if answer is not None:
                        return answer
        finally:
            try:
                sock.close()
            except socket.error:
                pass

        raise TuyaError('%s did not answer within %.0f seconds'
                        % (self.ip, self.timeout))

    def _read_reply(self, command, reply):
        """Interpret one reply packet, or None if it was not the answer."""
        return_code = struct.unpack('>I', reply[16:20])[0]
        raw = reply[20:-8]
        if return_code:
            detail = self._error_detail(raw)
            if detail:
                raise TuyaError('%s refused the request (error %d)%s'
                                % (self.ip, return_code, detail))
            # An error whose own reason is unreadable is the signature of a
            # key mismatch: the device could not decrypt what we sent, and
            # what it sent back we cannot decrypt either. Saying so beats
            # showing the user the decode failure, which names the wrong
            # thing entirely.
            raise TuyaError(
                '%s refused the request (error %d), and its reason came back '
                'unreadable -- which on this protocol means the local key '
                'does not match. A key changes every time the device is '
                're-paired in the app.' % (self.ip, return_code))

        parsed = self.decode_payload(raw)
        if command != CMD_STATUS:
            return parsed
        if 'dps' in parsed:
            return parsed
        if raw and not parsed:
            # The device answered, and the answer was unreadable. On this
            # protocol that means one thing: the payload was encrypted with a
            # key we do not have.
            raise TuyaError(
                '%s answered but the reply could not be read. The local key '
                'is almost certainly wrong -- it changes whenever the device '
                'is re-paired in the app.' % self.ip)
        return None

    @staticmethod
    def _error_detail(raw):
        """The device's own words for a failure, when it offers any.

        An error payload is sometimes a readable string and sometimes the
        ciphertext of one, so unprintable bytes are dropped rather than shown
        as mojibake next to a number that already says what went wrong.
        """
        if not raw:
            return ''
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            return ''
        readable = ''.join(c for c in text if ' ' <= c <= '~').strip()
        # Ciphertext that happens to decode is still not a message. A real
        # error string is ASCII throughout; anything half-unprintable is not
        # one, and passing it on as though it were would be worse than silence.
        if len(readable) * 2 < len(text):
            return ''
        return ': %s' % readable if readable else ''

    # -- verbs -------------------------------------------------------------

    def status(self):
        """Read the device's datapoints. Returns {datapoint: value}."""
        dps = self._exchange(CMD_STATUS).get('dps')
        return dps if isinstance(dps, dict) else {}

    def set_dps(self, values):
        """Set one or more datapoints. Returns True, or raises TuyaError."""
        body = self._body(CMD_CONTROL)
        body['dps'] = dict((str(key), value)
                           for key, value in values.items())
        self._exchange(CMD_CONTROL, body)
        return True

    def set_switch(self, on, dp=DP_SWITCH):
        return self.set_dps({dp: bool(on)})


def probe(timeout=8.0, log_func=None):
    """Diagnostic listen. Returns a report; never raises.

    discover() reports what it found, which is no help when the answer is
    nothing. This records whether each port could be opened, every datagram
    that arrived including ones that made no sense, and what was parsed out of
    them -- enough to tell a blocked port apart from a busy one, from a
    network that never carried the broadcast, from a device speaking something
    other than Tuya.
    """
    log = log_func or (lambda message: None)
    report = {
        'ports': {},
        'raw': [],
        'devices': [],
        'listened': timeout,
        'other_traffic': 0,
    }

    sockets = []
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
            report['ports'][port] = 'listening'
        except socket.error as exc:
            sock.close()
            report['ports'][port] = 'could not bind: %s' % exc

    if not sockets:
        return report

    seen = {}
    deadline = time.time() + max(1.0, float(timeout))
    try:
        while time.time() < deadline:
            for port, sock in sockets:
                try:
                    data, sender = sock.recvfrom(2048)
                except socket.error:
                    continue

                device = parse_broadcast(data)
                if device and device.get('device_id'):
                    device.setdefault('ip', sender[0])
                    if not device['ip']:
                        device['ip'] = sender[0]
                    seen[device['device_id']] = device
                else:
                    report['other_traffic'] += 1

                if len(report['raw']) < 6:
                    body = bytearray(data)[:64]
                    report['raw'].append({
                        'port': port,
                        'from': sender[0],
                        'bytes': len(data),
                        'hex': ''.join('%02x' % b for b in body),
                        'parsed': bool(device),
                    })
            time.sleep(0.05)
    finally:
        for _port, sock in sockets:
            sock.close()

    report['devices'] = list(seen.values())
    log('Tuya probe: %d device(s), %d unrecognised datagram(s)'
        % (len(report['devices']), report['other_traffic']))
    return report
