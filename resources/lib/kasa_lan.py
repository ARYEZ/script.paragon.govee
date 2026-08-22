# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Client for the TP-Link Kasa LAN protocol (HS100, HS103, HS110, KP series).

The pleasant surprise after Tuya: there is nothing to arrange. A Kasa device
answers any request on its own network with no key, no account, no pairing
step and no cloud round trip to fetch a credential first. Discovery is one
broadcast, and a plug found is a plug that can be switched.

The obfuscation is a running XOR rather than a cipher. Each byte is XORed
with the previous *output* byte, starting from 0xAB. It keeps the JSON off
the wire in plain sight and nothing more, which is worth being clear about:
anything on your network can switch these plugs, and that was true before
this add-on existed.

Two framings, one protocol, and the difference is the classic way to get this
wrong:

    UDP 9999   discovery -- payload alone, no length
    TCP 9999   commands  -- payload behind a 4-byte big-endian length

Send a TCP command without the prefix and the device simply never answers.
"""

import json
import socket
import struct
import time

from govee_lan import local_addresses

PORT = 9999
BROADCAST_ADDRESS = '255.255.255.255'

# The XOR chain starts here. Not a secret, and not treated as one.
INITIAL_KEY = 0xAB

# What a plug is asked. Kasa exposes a great deal more -- schedules, LED
# state, energy on the metered models -- none of which is needed to switch
# something on.
INFO_COMMAND = {'system': {'get_sysinfo': {}}}


class KasaError(Exception):
    """The device could not be reached, or refused the request."""


def encrypt(text):
    """Obfuscate a request. Each byte is XORed with the one sent before it."""
    if not isinstance(text, bytes):
        text = text.encode('utf-8')
    key = INITIAL_KEY
    out = bytearray()
    for byte in bytearray(text):
        key = byte ^ key
        out.append(key)
    return bytes(out)


def decrypt(data):
    """Undo encrypt(). The chain runs off the ciphertext, so it reverses."""
    key = INITIAL_KEY
    out = bytearray()
    for byte in bytearray(data):
        out.append(byte ^ key)
        key = byte
    return bytes(out)


def framed(payload):
    """Add the 4-byte length TCP wants and UDP must not have."""
    body = encrypt(payload)
    return struct.pack('>I', len(body)) + body


def _loads(raw):
    try:
        return json.loads(raw.decode('utf-8', 'replace'))
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None


def _dumps(body):
    return json.dumps(body, separators=(',', ':'))


def parse_sysinfo(payload, ip=''):
    """Turn a get_sysinfo reply into a device dict, or None.

    Kasa moved the identifiers around between generations -- `deviceId` on
    some, `mic_mac` and `mac` spelled differently on others -- so each is
    tried rather than assumed. A device with no id at all is not usable as a
    stable entry and is dropped.
    """
    info = payload
    if isinstance(payload, dict):
        info = (payload.get('system') or {}).get('get_sysinfo') or payload
    if not isinstance(info, dict):
        return None

    device_id = (info.get('deviceId') or info.get('mic_mac')
                 or info.get('mac') or '')
    if not device_id:
        return None

    children = []
    for child in info.get('children') or []:
        if not isinstance(child, dict):
            continue
        child_id = child.get('id') or ''
        if not child_id:
            continue
        # Some firmware reports a child id relative to the parent and some
        # reports it whole. The device wants the whole one.
        if not child_id.startswith(device_id):
            child_id = device_id + child_id
        children.append({
            'id': child_id,
            'alias': child.get('alias') or '',
            'state': child.get('state'),
        })

    return {
        'device_id': device_id,
        'ip': ip,
        'alias': info.get('alias') or '',
        'model': (info.get('model') or '').split('(')[0].strip(),
        'relay_state': info.get('relay_state'),
        'children': children,
        'raw': info,
    }


def discover(timeout=4.0, log_func=None):
    """Broadcast get_sysinfo and collect the answers.

    Sent from every local address as well as the default route, for the same
    reason the Govee search is: a machine with a VPN, a container bridge or a
    second NIC will otherwise broadcast out of the wrong one and hear nothing.
    """
    log = log_func or (lambda message: None)
    payload = encrypt(_dumps(INFO_COMMAND))
    found = {}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind(('', 0))
        except socket.error as exc:
            raise KasaError('Could not open a socket to search for Kasa '
                            'devices: %s' % exc)
        sock.setblocking(False)

        sent = 0
        for address in list(local_addresses()) + ['']:
            try:
                if address:
                    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    probe.bind((address, 0))
                    probe.sendto(payload, (BROADCAST_ADDRESS, PORT))
                    probe.close()
                else:
                    sock.sendto(payload, (BROADCAST_ADDRESS, PORT))
                sent += 1
            except socket.error as exc:
                log('Kasa broadcast via %s failed: %s'
                    % (address or 'default route', exc))

        if not sent:
            raise KasaError('Could not broadcast on any interface.')

        deadline = time.time() + max(1.0, float(timeout))
        while time.time() < deadline:
            try:
                data, sender = sock.recvfrom(4096)
            except socket.error:
                time.sleep(0.05)
                continue
            device = parse_sysinfo(_loads(decrypt(data)), ip=sender[0])
            if device:
                found[device['device_id']] = device
    finally:
        sock.close()

    log('Kasa discovery heard %d device(s)' % len(found))
    return list(found.values())


class Session(object):
    """One conversation with one Kasa device, over TCP."""

    def __init__(self, ip, timeout=5.0, log_func=None):
        self.ip = ip
        self.timeout = timeout
        self._log = log_func or (lambda message: None)

    def _exchange(self, body):
        """Send one request and return the decoded reply.

        A connection per request: these devices close an idle one on their own
        schedule, and a plug is switched a few times an hour.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            try:
                sock.connect((self.ip, PORT))
                sock.sendall(framed(_dumps(body)))
            except socket.error as exc:
                raise KasaError('Could not reach %s on port %d: %s'
                                % (self.ip, PORT, exc))

            buffer = b''
            expected = None
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                except socket.error as exc:
                    raise KasaError('%s closed the connection: %s'
                                    % (self.ip, exc))
                if not chunk:
                    break
                buffer += chunk
                if expected is None and len(buffer) >= 4:
                    expected = struct.unpack('>I', buffer[:4])[0]
                # sysinfo runs to a couple of kilobytes on some models, so the
                # reply is read to its declared length rather than assuming one
                # recv covered it.
                if expected is not None and len(buffer) >= expected + 4:
                    break
        finally:
            try:
                sock.close()
            except socket.error:
                pass

        if expected is None:
            raise KasaError(
                '%s did not answer. A Kasa device that answers discovery but '
                'not a command is usually running newer firmware that has '
                'closed the local protocol.' % self.ip)

        reply = _loads(decrypt(buffer[4:4 + expected]))
        if not isinstance(reply, dict):
            raise KasaError('%s sent a reply that could not be read' % self.ip)
        return reply

    def info(self):
        """Read the device's system info."""
        reply = self._exchange(INFO_COMMAND)
        info = (reply.get('system') or {}).get('get_sysinfo')
        if not isinstance(info, dict):
            raise KasaError('%s did not report its system info' % self.ip)
        return info

    def set_relay(self, on, child_id=None):
        """Switch the plug, or one outlet of a multi-outlet one."""
        body = {'system': {'set_relay_state': {'state': 1 if on else 0}}}
        if child_id:
            body = dict(body)
            body['context'] = {'child_ids': [child_id]}

        reply = self._exchange(body)
        result = (reply.get('system') or {}).get('set_relay_state') or {}
        code = result.get('err_code')
        if code:
            raise KasaError('%s refused the request (error %s)%s'
                            % (self.ip, code,
                               ': %s' % result['err_msg']
                               if result.get('err_msg') else ''))
        return True
