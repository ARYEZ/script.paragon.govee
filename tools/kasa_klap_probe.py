# -*- coding: utf-8 -*-
"""
Paragon Home - KLAP credential probe (development tool, not shipped behaviour).

Run this on a PC with Python 3 when a Kasa plug refuses the handshake and
others on the same account accept it. It asks the plug directly and reports
which credentials and which hash scheme it actually wants.

    python3 tools/kasa_klap_probe.py 10.0.0.195 you@example.com

The password is asked for at the prompt and never printed, never written to a
file and never sent anywhere but the plug on your own network. Nothing here
talks to TP-Link.
"""

from __future__ import print_function

import getpass
import hashlib
import os
import sys
import urllib.error
import urllib.request

SETUP_CREDENTIALS = [
    ('kasa@tp-link.net', 'kasaSetup'),
    ('test@tp-link.net', 'test'),
]


def md5(data):
    return hashlib.md5(data).digest()


def sha1(data):
    return hashlib.sha1(data).digest()


def sha256(data):
    return hashlib.sha256(data).digest()


def schemes(username, password):
    """Every credential digest anyone has seen a TP-Link device ask for.

    More than the add-on implements on purpose: the point is to find out
    which one this device wants, and an answer here is worth a round trip
    that guessing is not.
    """
    user = username.encode('utf-8')
    word = password.encode('utf-8')
    return [
        ('v1  md5(md5(user)+md5(pass))', md5(md5(user) + md5(word))),
        ('v2  sha256(sha1(user)+sha1(pass))',
         sha256(sha1(user) + sha1(word))),
        ('v1h md5(md5hex(user)+md5hex(pass))',
         md5(hashlib.md5(user).hexdigest().encode()
             + hashlib.md5(word).hexdigest().encode())),
        ('v2h sha256(sha1hex(user)+sha1hex(pass))',
         sha256(hashlib.sha1(user).hexdigest().encode()
                + hashlib.sha1(word).hexdigest().encode())),
        ('v2b sha256(sha1(pass)+sha1(user))',
         sha256(sha1(word) + sha1(user))),
    ]


def handshake1(ip, port, local_seed):
    url = 'http://%s:%d/app/handshake1' % (ip, port)
    request = urllib.request.Request(url, data=local_seed)
    request.add_header('Content-Type', 'application/octet-stream')
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.read()


def candidates(email, password):
    """Credential pairs to try, most likely first."""
    pairs = []
    if email or password:
        pairs.append(('the account you gave', email, password))
        if email != email.lower():
            pairs.append(('that account, lower case', email.lower(), password))
    pairs.append(('no account at all', '', ''))
    for username, word in SETUP_CREDENTIALS:
        pairs.append(('TP-Link setup credentials', username, word))
    return pairs


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2

    ip = sys.argv[1]
    port = int(sys.argv[3]) if len(sys.argv) > 3 else 80
    email = sys.argv[2] if len(sys.argv) > 2 else ''
    password = getpass.getpass('TP-Link password (not shown, not stored): ') \
        if email else ''

    local_seed = os.urandom(16)
    try:
        reply = handshake1(ip, port, local_seed)
    except (urllib.error.URLError, OSError) as exc:
        print('Could not reach %s on port %d: %s' % (ip, port, exc))
        return 1

    if len(reply) < 48:
        print('%s answered handshake1 with %d bytes; expected at least 48.'
              % (ip, len(reply)))
        print('It may not speak KLAP. Raw reply: %s' % reply[:64].hex())
        return 1

    remote_seed, server_hash = reply[:16], reply[16:48]
    print('%s answered. Looking for what it wants...\n' % ip)

    for label, username, word in candidates(email, password):
        for scheme_name, digest in schemes(username, word):
            for style, computed in (
                    ('local_seed+auth',
                     sha256(local_seed + digest)),
                    ('local_seed+remote_seed+auth',
                     sha256(local_seed + remote_seed + digest))):
                if computed == server_hash:
                    print('MATCH')
                    print('  credentials : %s' % label)
                    print('  digest      : %s' % scheme_name)
                    print('  handshake1  : sha256(%s)' % style)
                    return 0

    print('No combination matched. Send these to Claude -- they contain no')
    print('password and cannot be used to derive one:')
    print('  local_seed  : %s' % local_seed.hex())
    print('  remote_seed : %s' % remote_seed.hex())
    print('  server_hash : %s' % server_hash.hex())
    return 1


if __name__ == '__main__':
    sys.exit(main())
