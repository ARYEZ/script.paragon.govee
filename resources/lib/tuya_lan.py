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
import hmac
import os
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

# 3.4 renumbered both verbs and added the three-step key negotiation that has
# to precede them on every connection.
CMD_SESS_KEY_NEG_START = 0x03
CMD_SESS_KEY_NEG_RESP = 0x04
CMD_SESS_KEY_NEG_FINISH = 0x05
CMD_CONTROL_NEW = 0x0D
CMD_STATUS_NEW = 0x10

# On 3.4 the version header goes inside the encryption, and only on the verbs
# that want it. A status query and every negotiation step go without.
V34_HEADERLESS = (CMD_STATUS_NEW, CMD_SESS_KEY_NEG_START,
                  CMD_SESS_KEY_NEG_RESP, CMD_SESS_KEY_NEG_FINISH)

NONCE_LENGTH = 16
HMAC_LENGTH = 32

# A single-outlet plug switches on datapoint 1. Multi-outlet plugs use one
# datapoint per outlet; see tuya_driver for the allocation.
DP_SWITCH = '1'

# 3.1 through 3.3 differ only in how a payload is wrapped. 3.4 adds a session
# key negotiated per connection, HMAC-SHA256 in place of the CRC, and its own
# numbering for the verbs -- handled here. 3.5 moves to AES-GCM, which is a
# different cipher rather than a bigger version number, and is not built.
SUPPORTED_VERSIONS = ('3.1', '3.2', '3.3', '3.4')


def version_note(version):
    """Why this protocol version cannot be driven, or None if it can."""
    version = str(version or '3.3')
    if version[:3] in SUPPORTED_VERSIONS:
        return None
    return ('This device speaks Tuya %s. Paragon Home drives 3.1 to 3.4; '
            '3.5 encrypts with AES-GCM, which is not built yet.' % version)


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
        # Agreed per connection on 3.4 and discarded when it closes.
        self.session_key = None

    @property
    def keyed(self):
        return len(self.local_key or b'') == 16

    @property
    def is_v34(self):
        return self.version.startswith('3.4')

    def _cipher(self, key=None):
        if key is not None:
            return AESECB(key)
        if not self.keyed:
            raise TuyaKeyMissing(
                'No local key for %s. A Tuya device will not hand out its own '
                'key; it has to be read from your Tuya account once.'
                % self.device_id)
        return AESECB(self.local_key)

    def _payload_cipher(self):
        """What payloads are encrypted with: the session key once there is one."""
        if self.is_v34 and self.session_key:
            return self._cipher(self.session_key)
        return self._cipher()

    def _hmac_key(self):
        """The key a 3.4 packet is signed with, or None on earlier versions."""
        if not self.is_v34:
            return None
        return self.session_key or self.local_key

    @property
    def tail_length(self):
        """Bytes after the payload: HMAC and suffix on 3.4, CRC and suffix before."""
        return HMAC_LENGTH + 4 if self.is_v34 else 8

    @staticmethod
    def _nonce():
        return os.urandom(NONCE_LENGTH)

    # -- framing -----------------------------------------------------------

    def build_packet(self, command, payload):
        """Wrap a payload in Tuya's 0x000055AA framing.

        3.4 replaces the trailing CRC32 with an HMAC-SHA256 over the header
        and payload, which is both longer and keyed -- so the declared length
        changes with the version too.
        """
        payload = bytes(payload)
        self.sequence += 1
        hmac_key = self._hmac_key()

        header = struct.pack('>4I', PREFIX, self.sequence, command,
                             len(payload) + self.tail_length)
        body = header + payload
        if hmac_key:
            check = hmac.new(hmac_key, body, hashlib.sha256).digest()
        else:
            check = struct.pack('>I', zlib.crc32(body) & 0xFFFFFFFF)
        return body + check + struct.pack('>I', SUFFIX)

    def _reply_payload(self, reply):
        """Split a reply into (return code, payload).

        Not every 3.4 reply carries a return code -- the key negotiation step
        does not -- and nothing in the header says which. Every 3.4 payload is
        AES-ECB ciphertext, so the reading that leaves a whole number of
        blocks is the right one. That is a check against the format, not a
        guess between two equally likely options.
        """
        body = reply[16:-self.tail_length]
        if not self.is_v34:
            return struct.unpack('>I', body[:4])[0], body[4:]
        if len(body) >= 4 and len(body) % 16 and not len(body[4:]) % 16:
            return struct.unpack('>I', body[:4])[0], body[4:]
        return 0, body

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

        if self.is_v34:
            try:
                plain = self._payload_cipher().decrypt(payload)
            except (ValueError, TypeError):
                return {}
            if plain[:3] in (b'3.4', b'3.5'):
                plain = plain[15:]
            return self._unwrap(_json_loads(plain))

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
        return self._unwrap(parsed)

    @staticmethod
    def _unwrap(parsed):
        """Take the datapoints out of a 3.4 envelope, if that is what this is.

        3.4 wraps a reading as {"protocol":.., "t":.., "data":{"dps":..}}
        where 3.3 sends {"dps":..} flat. Unwrapping here means everything
        above this layer sees one shape.
        """
        if not isinstance(parsed, dict):
            return {}
        if 'dps' not in parsed:
            inner = parsed.get('data')
            if isinstance(inner, dict) and 'dps' in inner:
                return inner
        return parsed

    # -- requests ----------------------------------------------------------

    def _body(self, command, dps=None):
        """The JSON body a given command expects.

        3.4 restructured both: a status query carries nothing at all, and a
        control wraps its datapoints in an envelope with an integer timestamp
        where earlier versions used a string.
        """
        if self.is_v34:
            if command == CMD_STATUS:
                return {}
            return {'protocol': 5, 't': int(time.time()),
                    'data': {'dps': dps or {}}}

        stamp = str(int(time.time()))
        if command == CMD_STATUS:
            return {'gwId': self.device_id, 'devId': self.device_id,
                    'uid': self.device_id, 't': stamp}
        body = {'devId': self.device_id, 'uid': self.device_id, 't': stamp}
        if dps is not None:
            body['dps'] = dps
        return body

    def _wire_command(self, command):
        """The number this verb travels as. 3.4 renumbered both of them."""
        if not self.is_v34:
            return command
        if command == CMD_STATUS:
            return CMD_STATUS_NEW
        if command == CMD_CONTROL:
            return CMD_CONTROL_NEW
        return command

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
        3.4 keeps that split but moves the header inside the encryption, and
            encrypts with a session key rather than the local key.
        """
        raw = json.dumps(body, separators=(',', ':')).encode('utf-8')

        if self.is_v34:
            # The one that is genuinely counter-intuitive: on 3.3 the version
            # header sits in front of the ciphertext, on 3.4 it goes inside
            # it. Same three bytes, opposite side of the encryption.
            if self._wire_command(command) not in V34_HEADERLESS:
                raw = b'3.4' + (b'\x00' * 12) + raw
            return self._payload_cipher().encrypt(raw)

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

    def _open(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect((self.ip, CONTROL_PORT))
        except socket.error as exc:
            sock.close()
            raise TuyaError('Could not reach %s on port %d: %s'
                            % (self.ip, CONTROL_PORT, exc))
        return sock

    def _send(self, sock, command, payload):
        try:
            sock.sendall(self.build_packet(command, payload))
        except socket.error as exc:
            raise TuyaError('%s stopped listening: %s' % (self.ip, exc))

    def _read(self, sock, buffer):
        """Read until at least one whole packet is available.

        Framing is by the declared length rather than by read boundaries: a
        single read can hold more than the answer -- devices often push an
        unsolicited status alongside the reply -- and TCP is free to cut the
        stream anywhere.
        """
        deadline = time.time() + self.timeout
        while True:
            packets, buffer = self.split_packets(buffer)
            if packets:
                return packets, buffer
            if time.time() >= deadline:
                break
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

        raise TuyaError('%s did not answer within %.0f seconds'
                        % (self.ip, self.timeout))

    def negotiate(self, sock):
        """Agree a session key for this connection (protocol 3.4).

        3.4 accepts no command until both ends have shown they hold the same
        local key. We send a nonce; the device answers with its own plus an
        HMAC over ours; we return an HMAC over its one. The session key is
        derived from both nonces, so it is different on every connection and
        neither nonce is worth replaying.

        The failure that matters is a wrong key, and it is caught here at the
        HMAC rather than several steps later as an unreadable reply.
        """
        self.session_key = None
        local_nonce = self._nonce()

        self._send(sock, CMD_SESS_KEY_NEG_START,
                   self._cipher().encrypt(local_nonce))
        packets, _rest = self._read(sock, b'')
        _code, raw = self._reply_payload(packets[0])

        try:
            # Unpadded and sliced rather than unpadded-and-trusted: only the
            # first 48 bytes are defined, and devices differ on what they pad
            # the rest with.
            plain = self._cipher().decrypt(raw, unpad=False)
        except (ValueError, TypeError) as exc:
            raise TuyaError('%s answered the key negotiation with something '
                            'that will not decrypt (%s)' % (self.ip, exc))
        if len(plain) < NONCE_LENGTH + HMAC_LENGTH:
            raise TuyaError('%s cut the key negotiation short (%d bytes)'
                            % (self.ip, len(plain)))

        remote_nonce = plain[:NONCE_LENGTH]
        proof = hmac.new(self.local_key, local_nonce, hashlib.sha256).digest()
        if plain[NONCE_LENGTH:NONCE_LENGTH + HMAC_LENGTH] != proof:
            raise TuyaError(
                '%s could not prove it holds the same local key, so the key '
                'is wrong. A key changes every time the device is re-paired '
                'in the app.' % self.ip)

        self._send(sock, CMD_SESS_KEY_NEG_FINISH,
                   self._cipher().encrypt(
                       hmac.new(self.local_key, remote_nonce,
                                hashlib.sha256).digest()))

        mixed = bytearray(local_nonce)
        for index, byte in enumerate(bytearray(remote_nonce)):
            mixed[index] ^= byte
        self.session_key = self._cipher().encrypt(bytes(mixed), pad=False)
        return self.session_key

    def _exchange(self, command, body=None):
        """Send one request and return the first reply that answers it.

        A fresh connection per exchange rather than a kept-open socket: Tuya
        devices drop an idle connection without saying so, and a plug is
        switched a few times an hour, not a few times a second. On 3.4 that
        means re-negotiating each time, which is two more round trips on a
        LAN and still imperceptible.
        """
        note = version_note(self.version)
        if note:
            raise TuyaError(note)

        if body is None:
            body = self._body(command)

        sock = self._open()
        try:
            if self.is_v34:
                self.negotiate(sock)

            self._send(sock, self._wire_command(command),
                       self.build_command_payload(command, body))

            buffer = b''
            while True:
                packets, buffer = self._read(sock, buffer)
                for reply in packets:
                    answer = self._read_reply(command, reply)
                    if answer is not None:
                        return answer
        finally:
            self.session_key = None
            try:
                sock.close()
            except socket.error:
                pass

    def _read_reply(self, command, reply):
        """Interpret one reply packet, or None if it was not the answer."""
        return_code, raw = self._reply_payload(reply)
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
        dps = dict((str(key), value) for key, value in values.items())
        self._exchange(CMD_CONTROL, self._body(CMD_CONTROL, dps))
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
