# -*- coding: utf-8 -*-
"""
Paragon Home - robot vacuum probe (development tool, not shipped behaviour).

Run this on a PC with Python 3, on the same network as the vacuum:

    python3 tools/roborock_probe.py

It asks the network three questions and reports what comes back. Nothing is
sent anywhere but your own LAN, no account is needed and nothing is changed on
any device -- every packet here is a "who is there" and none is a command.

The answers decide which of two quite different jobs adding the vacuum is:

  miIO on UDP 54321      The Xiaomi-era protocol. Roborock S4, S5, S6 and
                         relatives, set up in Mi Home. Needs a token, which is
                         extracted once and then everything is local. This is
                         the buildable case.

  Roborock on TCP 58867  The newer protocol, used by models set up in the
                         Roborock app. Local, but the key that unlocks it
                         comes from a Roborock cloud login first.

  Roborock on UDP 58866  The newer protocol's own broadcast announcement.
"""

from __future__ import print_function

import socket
import struct
import sys
import threading
import time

MIIO_PORT = 54321
ROBOROCK_TCP_PORT = 58867
ROBOROCK_UDP_PORT = 58866

# "Hello" in miIO: magic, length 32, then everything else unknown. A device
# answers with its own id, and older firmware answers with its token too.
MIIO_HELLO = struct.pack('>HH', 0x2131, 0x0020) + (b'\xff' * 28)

BLANK_TOKENS = (b'\xff' * 16, b'\x00' * 16)


def local_addresses():
    """This host's IPv4 addresses, loopback excluded."""
    found = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(('203.0.113.1', 9))   # TEST-NET-3, never routed
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    try:
        for entry in socket.getaddrinfo(socket.gethostname(), None,
                                        socket.AF_INET):
            address = entry[4][0]
            if not address.startswith('127.') and address not in found:
                found.append(address)
    except OSError:
        pass
    return found


def subnet_of(address):
    parts = address.split('.')
    return '.'.join(parts[:3]) if len(parts) == 4 else ''


def hosts_in(prefix):
    return ['%s.%d' % (prefix, host) for host in range(1, 255)]


def parse_miio_reply(data, ip):
    """Read a miIO hello reply: device id, and the token if it was given."""
    if len(data) < 32:
        return None
    magic, length = struct.unpack('>HH', data[:4])
    if magic != 0x2131 or length < 32:
        return None

    device_id = struct.unpack('>I', data[8:12])[0]
    stamp = struct.unpack('>I', data[12:16])[0]
    token = data[16:32]
    return {
        'ip': ip,
        'device_id': device_id,
        'uptime': stamp,
        'token': None if token in BLANK_TOKENS else token.hex(),
    }


def sweep_miio(prefixes, seconds=6.0):
    """Ask every host on the subnet, and the broadcast address, on 54321."""
    found = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(('', 0))
    sock.setblocking(False)

    targets = ['255.255.255.255']
    for prefix in prefixes:
        targets.append('%s.255' % prefix)
        targets.extend(hosts_in(prefix))

    deadline = time.time() + seconds
    index = 0
    while time.time() < deadline:
        for target in targets[index:index + 24]:
            try:
                sock.sendto(MIIO_HELLO, (target, MIIO_PORT))
            except OSError:
                pass
        index = min(len(targets), index + 24)

        while True:
            try:
                data, sender = sock.recvfrom(1024)
            except OSError:
                break
            device = parse_miio_reply(data, sender[0])
            if device:
                found[sender[0]] = device
        time.sleep(0.01)

    sock.close()
    return list(found.values())


def scan_tcp(prefixes, port, seconds=8.0):
    """Which hosts have `port` open. A connect and an immediate close."""
    open_hosts = []
    lock = threading.Lock()
    targets = []
    for prefix in prefixes:
        targets.extend(hosts_in(prefix))

    def check(address):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1.5)
        try:
            if sock.connect_ex((address, port)) == 0:
                with lock:
                    open_hosts.append(address)
        except OSError:
            pass
        finally:
            sock.close()

    threads = []
    for address in targets:
        thread = threading.Thread(target=check, args=(address,))
        thread.daemon = True
        thread.start()
        threads.append(thread)
        if len(threads) >= 128:
            for done in threads:
                done.join(0.05)
            threads = [t for t in threads if t.is_alive()]

    deadline = time.time() + seconds
    for thread in threads:
        thread.join(max(0.0, deadline - time.time()))
    return sorted(open_hosts)


def listen_udp(port, seconds=8.0):
    """Anything that announces itself on `port` while we wait."""
    heard = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(('', port))
    except OSError as exc:
        sock.close()
        return None, str(exc)
    sock.setblocking(False)

    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            data, sender = sock.recvfrom(2048)
        except OSError:
            time.sleep(0.05)
            continue
        heard.setdefault(sender[0], data[:48].hex())
    sock.close()
    return heard, ''


def main():
    prefixes = []
    for address in local_addresses():
        prefix = subnet_of(address)
        if prefix and prefix not in prefixes:
            prefixes.append(prefix)

    if not prefixes:
        print('Could not work out which network this machine is on.')
        return 1

    print('Looking on %s' % ', '.join('%s.0/24' % p for p in prefixes))
    print('Nothing here sends a command; these are all "who is there".\n')

    print('1. miIO, UDP %d ...' % MIIO_PORT)
    miio = sweep_miio(prefixes)
    if miio:
        for device in miio:
            token = device['token'] or '(withheld - newer firmware)'
            print('   FOUND  %-15s  device id %d  token %s'
                  % (device['ip'], device['device_id'], token))
    else:
        print('   nothing answered')

    print('\n2. Roborock, TCP %d ...' % ROBOROCK_TCP_PORT)
    tcp = scan_tcp(prefixes, ROBOROCK_TCP_PORT)
    for address in tcp:
        print('   OPEN   %s' % address)
    if not tcp:
        print('   nothing listening')

    print('\n3. Roborock announcements, UDP %d ...' % ROBOROCK_UDP_PORT)
    heard, error = listen_udp(ROBOROCK_UDP_PORT)
    if error:
        print('   could not listen: %s' % error)
    elif heard:
        for address, sample in heard.items():
            print('   HEARD  %-15s  %s...' % (address, sample))
    else:
        print('   nothing heard')

    print('\n--- what this means ---')
    if miio and any(d['token'] for d in miio):
        print('miIO, and the token was handed over. This is the easy case:')
        print('everything can be local, with no account at all.')
    elif miio:
        print('miIO, but the token is withheld by the firmware. Still local,')
        print('but the token has to be extracted from the app once.')
    elif tcp or heard:
        print('The newer Roborock protocol. Local control is possible but the')
        print('key comes from a Roborock cloud login first.')
    else:
        print('Nothing answered. Either the vacuum is asleep on its dock, on')
        print('another subnet or SSID, or it speaks only to the cloud.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
