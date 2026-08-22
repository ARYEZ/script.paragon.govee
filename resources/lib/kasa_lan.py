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

# How many times the broadcast goes out, and how many datagrams are sent
# between reads. UDP has no retransmission, so one broadcast is one chance.
BROADCAST_ROUNDS = 3
SEND_BATCH = 24

# How long everything must stay quiet, after the last datagram has gone out,
# before a pass is called finished. It keeps a search that found everything
# in half a second from sitting out the rest of its window, without cutting
# off a device that is slow to answer.
QUIET_PERIOD = 1.0


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


def directed_broadcast(address):
    """The all-hosts address of `address`'s subnet, assuming a /24.

    The mask is not knowable without platform-specific calls that Kodi's
    Python does not carry, and /24 is right on essentially every home
    network. It is an extra target rather than a replacement for
    255.255.255.255, so being wrong about it costs one wasted datagram.
    """
    parts = (address or '').split('.')
    if len(parts) != 4 or parts[0] == '127':
        return ''
    return '.'.join(parts[:3] + ['255'])


def sweep_addresses(address):
    """Every host on `address`'s assumed /24, for when broadcast is not enough.

    Mesh access points and "AP isolation" settings routinely drop or fail to
    flood broadcast traffic, which shows up as some devices answering and
    others never being heard from. A directly addressed datagram is not
    broadcast and is not subject to any of that.
    """
    parts = (address or '').split('.')
    if len(parts) != 4 or parts[0] == '127':
        return []
    prefix = '.'.join(parts[:3])
    return ['%s.%d' % (prefix, host) for host in range(1, 255)
            if '%s.%d' % (prefix, host) != address]


def _open_sockets(log):
    """One socket per local address, plus one on the default route.

    They stay open for the whole search. An earlier version sent from a
    socket it closed immediately afterwards, which threw away every reply to
    that send -- a device answers to the port the request came from, and by
    then there was nothing listening on it.
    """
    sockets = []
    for address in list(local_addresses()) + ['']:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((address, 0))
            sock.setblocking(False)
            sockets.append((address, sock))
        except socket.error as exc:
            sock.close()
            log('Kasa: could not open a socket on %s: %s'
                % (address or 'default route', exc))
    return sockets


def _collect(sockets, found):
    """Read every reply waiting on every socket. Returns how many arrived."""
    arrived = 0
    for _address, sock in sockets:
        while True:
            try:
                data, sender = sock.recvfrom(8192)
            except socket.error:
                break
            device = parse_sysinfo(_loads(decrypt(data)), ip=sender[0])
            if device:
                found[device['device_id']] = device
                arrived += 1
    return arrived


def _run(sockets, plan, payload, window, found):
    """Work through a send plan while reading throughout.

    Sending and listening are interleaved rather than sequential: a sweep is
    a few hundred datagrams, and a device that answers the first one should
    not have its reply sitting in a buffer until the last one has gone out.
    """
    before = len(found)
    index = 0
    quiet_since = None
    deadline = time.time() + window
    while time.time() < deadline:
        if index < len(plan):
            for sock, target in plan[index:index + SEND_BATCH]:
                try:
                    sock.sendto(payload, (target, PORT))
                except socket.error:
                    pass
            index = min(len(plan), index + SEND_BATCH)
            if index >= len(plan):
                quiet_since = time.time()

        if _collect(sockets, found):
            quiet_since = time.time()
        elif quiet_since is not None \
                and time.time() - quiet_since >= QUIET_PERIOD:
            break

        time.sleep(0.005 if index < len(plan) else 0.02)
    return len(found) - before


def search(timeout=5.0, log_func=None, sweep=True):
    """Find Kasa devices. Returns (devices, {'broadcast': n, 'sweep': n}).

    Two passes, because they fail differently. A broadcast is one datagram
    and finds everything on a well-behaved network. When an access point
    suppresses broadcast -- which mesh systems and guest networks routinely
    do -- it finds some devices and not others, which looks like the missing
    ones are broken. The sweep addresses each host on the subnet directly and
    does not depend on broadcast working at all.
    """
    log = log_func or (lambda message: None)
    payload = encrypt(_dumps(INFO_COMMAND))
    found = {}
    counts = {'broadcast': 0, 'sweep': 0}

    sockets = _open_sockets(log)
    if not sockets:
        raise KasaError('Could not open a socket to search for Kasa devices.')

    try:
        plan = []
        for address, sock in sockets:
            for target in [BROADCAST_ADDRESS, directed_broadcast(address)]:
                if target:
                    plan.append((sock, target))
        # Repeated because a broadcast is a single datagram with no
        # retransmission, and several devices answering at once is exactly
        # when one goes missing.
        counts['broadcast'] = _run(sockets, plan * BROADCAST_ROUNDS, payload,
                                   max(1.5, float(timeout) * 0.4), found)
        log('Kasa broadcast found %d device(s)' % counts['broadcast'])

        if sweep:
            plan = []
            for address, sock in sockets:
                for target in sweep_addresses(address):
                    plan.append((sock, target))
            if plan:
                counts['sweep'] = _run(sockets, plan, payload,
                                       max(2.0, float(timeout) * 0.6), found)
                log('Kasa subnet sweep found %d more' % counts['sweep'])
    finally:
        for _address, sock in sockets:
            sock.close()

    log('Kasa discovery found %d device(s) in total' % len(found))
    return list(found.values()), counts


def discover(timeout=5.0, log_func=None, sweep=True):
    """Find Kasa devices. Returns a list of device dicts."""
    return search(timeout=timeout, log_func=log_func, sweep=sweep)[0]


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
