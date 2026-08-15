# -*- coding: utf-8 -*-
"""
Paragon Govee
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Client for the Govee LAN API.

The LAN API is a small UDP protocol that Govee devices speak once "LAN Control"
has been switched on for them in the Govee Home app:

    * discovery  -- a `scan` message sent to the multicast group
                    239.255.255.250 on port 4001; devices answer to port 4002
                    on the requesting host.
    * control    -- a JSON message sent straight to the device's own IP on
                    port 4003. Control messages are fire-and-forget: the
                    device does not acknowledge them.
    * status     -- a `devStatus` request to port 4003, answered on port 4002.

Preferring this over the cloud API matters here: there is no internet
round-trip, no API key and no rate limit, so lights can be driven on every
play/pause without burning through a daily quota.
"""

import json
import socket
import struct
import time

MULTICAST_GROUP = '239.255.255.250'
BROADCAST_ADDRESS = '255.255.255.255'
SCAN_PORT = 4001      # devices listen here for multicast discovery
LISTEN_PORT = 4002    # devices answer here
COMMAND_PORT = 4003   # devices listen here for unicast control

# A single datagram from a Govee device is well under 1 KB; 2 KB is headroom.
_BUFFER_SIZE = 2048


class LANError(Exception):
    """Raised when the LAN transport cannot be used at all."""


def local_addresses():
    """Best-effort list of this host's IPv4 addresses, loopback excluded.

    Kodi boxes are routinely multi-homed -- a VPN, a Hyper-V or docker bridge,
    wired plus wireless -- and a multicast sent from a socket bound to
    0.0.0.0 leaves via whichever interface the routing table prefers, which is
    frequently not the one the lights are on. Enumerating lets discovery probe
    every interface instead of betting on the default route.

    Uses only the standard library, since Kodi 17.6 has no netifaces.
    """
    found = []

    def remember(address):
        if (address and address not in found
                and not address.startswith('127.')
                and not address.startswith('169.254.')):
            found.append(address)

    try:
        hostname = socket.gethostname()
    except socket.error:
        hostname = ''

    if hostname:
        try:
            for entry in socket.getaddrinfo(hostname, None, socket.AF_INET):
                remember(entry[4][0])
        except socket.error:
            pass
        try:
            remember(socket.gethostbyname(hostname))
            for address in socket.gethostbyname_ex(hostname)[2]:
                remember(address)
        except socket.error:
            pass

    # A connect() on a UDP socket assigns a source address without sending
    # anything, which reveals the address the default route would use.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(('203.0.113.1', 9))  # TEST-NET-3, never routed anywhere
        remember(probe.getsockname()[0])
    except socket.error:
        pass
    finally:
        probe.close()

    return found


def _encode(message):
    return json.dumps(message).encode('utf-8')


def _decode(payload):
    """Parse a device datagram, returning the inner `msg` dict or None."""
    try:
        data = json.loads(payload.decode('utf-8', 'replace'))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    message = data.get('msg')
    if not isinstance(message, dict):
        return None
    return message


def scan_message():
    return {'msg': {'cmd': 'scan', 'data': {'account_topic': 'reserve'}}}


def turn_message(on):
    return {'msg': {'cmd': 'turn', 'data': {'value': 1 if on else 0}}}


def brightness_message(percent):
    return {'msg': {'cmd': 'brightness', 'data': {'value': int(percent)}}}


def color_message(red, green, blue):
    """RGB colour. `colorTemInKelvin` must be 0 or the device ignores the RGB."""
    return {'msg': {'cmd': 'colorwc', 'data': {
        'color': {'r': int(red), 'g': int(green), 'b': int(blue)},
        'colorTemInKelvin': 0,
    }}}


def color_temp_message(kelvin):
    """White colour temperature. RGB is sent as 0/0/0 alongside by convention."""
    return {'msg': {'cmd': 'colorwc', 'data': {
        'color': {'r': 0, 'g': 0, 'b': 0},
        'colorTemInKelvin': int(kelvin),
    }}}


def status_message():
    return {'msg': {'cmd': 'devStatus', 'data': {}}}


class LANTransport(object):
    """Sends Govee LAN datagrams and collects the replies.

    Sockets are opened per operation rather than held open for the life of the
    add-on. Kodi runs the settings script and the playback service as separate
    Python instances, and both want port 4002; holding it open would mean
    whichever started first permanently locked the other out of discovery.
    """

    def __init__(self, bind_address='', retries=2, retry_gap=0.06,
                 log_func=None):
        self.bind_address = bind_address or ''
        self.retries = max(1, int(retries))
        self.retry_gap = retry_gap
        self._log = log_func or (lambda message: None)

    # -- socket plumbing ---------------------------------------------------

    def _make_socket(self, want_replies):
        """Create a UDP socket.

        When `want_replies` is set the socket is bound to the well-known reply
        port 4002, which is required for discovery and status. Control messages
        need no reply, so they go out from an ephemeral port and never contend
        for 4002.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            reuse_port = getattr(socket, 'SO_REUSEPORT', None)
            if reuse_port is not None:
                try:
                    sock.setsockopt(socket.SOL_SOCKET, reuse_port, 1)
                except socket.error:
                    # Not supported on every platform Kodi runs on; harmless.
                    pass

            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            if self.bind_address:
                try:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                    socket.inet_aton(self.bind_address))
                except socket.error as exc:
                    self._log('Could not pin multicast to %s: %s'
                              % (self.bind_address, exc))

            if want_replies:
                sock.bind((self.bind_address, LISTEN_PORT))
                self._join_multicast(sock)
            else:
                sock.bind((self.bind_address, 0))
            return sock
        except socket.error:
            sock.close()
            raise

    def _join_multicast(self, sock):
        """Best-effort multicast join.

        Devices normally answer by unicast, so a failed join is not fatal --
        it only costs us discovery on setups where the reply is multicast.
        """
        try:
            interface = (socket.inet_aton(self.bind_address)
                         if self.bind_address else struct.pack('!I',
                                                               socket.INADDR_ANY))
            membership = socket.inet_aton(MULTICAST_GROUP) + interface
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                            membership)
        except (socket.error, OSError) as exc:
            self._log('Multicast join failed (continuing unicast): %s' % exc)

    def _collect(self, sock, deadline, wanted_cmd, from_ip=None):
        """Read datagrams until `deadline`, yielding matching (ip, data) pairs."""
        results = []
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                payload, address = sock.recvfrom(_BUFFER_SIZE)
            except socket.timeout:
                break
            except socket.error as exc:
                self._log('Receive failed: %s' % exc)
                break

            if from_ip and address[0] != from_ip:
                continue
            message = _decode(payload)
            if message is None or message.get('cmd') != wanted_cmd:
                continue
            data = message.get('data')
            if isinstance(data, dict):
                results.append((address[0], data))
        return results

    # -- public API --------------------------------------------------------

    def _send_scan(self, sock):
        """Fire the scan out of every interface we can find.

        Returns a list of (description, error-or-None) so a caller that wants
        to report on the attempt can, and so a partial failure is visible
        rather than silently reducing coverage.
        """
        payload = _encode(scan_message())
        attempts = []

        targets = [self.bind_address] if self.bind_address \
            else local_addresses()
        # An empty entry means "leave the interface to the routing table",
        # which is the right answer on a single-homed box and a useful extra
        # shot when address enumeration came back short.
        if '' not in targets:
            targets.append('')

        for address in targets:
            label = address or 'default route'
            if address:
                try:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                    socket.inet_aton(address))
                except socket.error as exc:
                    attempts.append(('multicast via %s' % label, str(exc)))
                    continue
            try:
                sock.sendto(payload, (MULTICAST_GROUP, SCAN_PORT))
                attempts.append(('multicast via %s' % label, None))
            except socket.error as exc:
                attempts.append(('multicast via %s' % label, str(exc)))

        # Broadcast fallback: some switches and access points drop or fail to
        # flood 239.255.255.250 while passing a plain broadcast fine.
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(payload, (BROADCAST_ADDRESS, SCAN_PORT))
            attempts.append(('broadcast', None))
        except socket.error as exc:
            attempts.append(('broadcast', str(exc)))

        return attempts

    def _bind_error(self, exc):
        """Explain a failed bind on 4002 in terms the user can act on."""
        return ('Could not listen on UDP port %d: %s. Another Govee program '
                '(the Govee Desktop app, Home Assistant, a second Kodi) is '
                'probably holding it -- close it and search again.'
                % (LISTEN_PORT, exc))

    def discover(self, timeout=3.0):
        """Scan for devices and return a list of device dicts.

        Each dict carries at least `ip`, `device` (the Govee device id) and
        `sku` (the model, e.g. H6159). Duplicates are collapsed, because a
        device that hears the scan on more than one interface answers to each.
        """
        try:
            sock = self._make_socket(want_replies=True)
        except socket.error as exc:
            raise LANError(self._bind_error(exc))

        found = {}
        try:
            attempts = self._send_scan(sock)
            if not any(error is None for _label, error in attempts):
                raise LANError('Could not send the discovery scan: %s'
                               % '; '.join('%s: %s' % (label, error)
                                           for label, error in attempts[:3]))

            deadline = time.time() + max(0.5, float(timeout))
            for ip, data in self._collect(sock, deadline, 'scan'):
                device_id = data.get('device')
                if not device_id:
                    continue
                data = dict(data)
                data.setdefault('ip', ip)
                found[device_id.upper()] = data
        finally:
            sock.close()

        self._log('LAN discovery found %d device(s)' % len(found))
        return list(found.values())

    def probe(self, timeout=4.0):
        """Diagnostic sweep. Returns a report dict; never raises.

        discover() only reports what it found, which is no help when the
        answer is nothing. This records each step -- interfaces seen, whether
        the reply port could be opened, which probes went out, and every
        datagram that came back including ones we could not parse -- so a
        silent failure can be told apart from a blocked port, a busy port, and
        devices that simply do not speak the LAN protocol.
        """
        report = {
            'addresses': local_addresses(),
            'bind_address': self.bind_address,
            'listen_port': LISTEN_PORT,
            'bound': False,
            'bind_error': None,
            'attempts': [],
            'raw_replies': [],
            'devices': [],
        }

        try:
            sock = self._make_socket(want_replies=True)
            report['bound'] = True
        except socket.error as exc:
            report['bind_error'] = self._bind_error(exc)
            return report

        seen = {}
        try:
            report['attempts'] = self._send_scan(sock)

            deadline = time.time() + max(1.0, float(timeout))
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    payload, address = sock.recvfrom(_BUFFER_SIZE)
                except socket.timeout:
                    break
                except socket.error as exc:
                    report['raw_replies'].append(('-', 'receive failed: %s'
                                                  % exc))
                    break

                text = payload.decode('utf-8', 'replace')
                report['raw_replies'].append((address[0], text[:300]))

                message = _decode(payload)
                if message and message.get('cmd') == 'scan':
                    data = message.get('data')
                    if isinstance(data, dict) and data.get('device'):
                        data = dict(data)
                        data.setdefault('ip', address[0])
                        # The scan goes out on several paths, so a device
                        # answers more than once. Raw replies stay as they
                        # came -- that is the diagnostic value -- but the
                        # device list is deduplicated so the count shown to
                        # the user is the number of lights, not of datagrams.
                        seen[data['device'].upper()] = data
        finally:
            sock.close()

        report['devices'] = list(seen.values())
        return report

    def send(self, ip, message):
        """Fire a control message at a device. Returns True if it went out.

        Sent `retries` times: the LAN protocol has no acknowledgement, so a
        dropped datagram would otherwise be a silently ignored command.
        """
        try:
            sock = self._make_socket(want_replies=False)
        except socket.error as exc:
            self._log('Could not open control socket: %s' % exc)
            return False

        payload = _encode(message)
        sent = False
        try:
            for attempt in range(self.retries):
                try:
                    sock.sendto(payload, (ip, COMMAND_PORT))
                    sent = True
                except socket.error as exc:
                    self._log('Send to %s failed: %s' % (ip, exc))
                    break
                if attempt + 1 < self.retries:
                    time.sleep(self.retry_gap)
        finally:
            sock.close()
        return sent

    def status(self, ip, timeout=2.0):
        """Ask a device for its current state. Returns a dict or None.

        The reply lands on port 4002, so this needs the well-known port. If
        another process on the box already holds it the request is abandoned
        rather than retried -- status is informational, never on a control path.
        """
        try:
            sock = self._make_socket(want_replies=True)
        except socket.error as exc:
            self._log('Could not bind UDP port %d for status: %s'
                      % (LISTEN_PORT, exc))
            return None

        try:
            try:
                sock.sendto(_encode(status_message()), (ip, COMMAND_PORT))
            except socket.error as exc:
                self._log('Status request to %s failed: %s' % (ip, exc))
                return None

            deadline = time.time() + max(0.5, float(timeout))
            replies = self._collect(sock, deadline, 'devStatus', from_ip=ip)
        finally:
            sock.close()

        if not replies:
            return None
        return replies[0][1]
