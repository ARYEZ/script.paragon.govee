# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

KLAP: the protocol TP-Link moved to on later Kasa hardware.

An HS103 hardware v2 answers the open protocol on port 9999 with no
credentials at all. An HS103 hardware v5 -- same model number, same box, same
app -- does not answer port 9999 at all. It speaks KLAP over HTTP on port 80,
and it will not say anything until both ends have proved they know the
password of the TP-Link account the plug is registered to.

So this is the opposite of the rest of the add-on's protocols in one
uncomfortable way: it needs your cloud account password, held locally, to do
something entirely local. That is TP-Link's design and there is no way round
it -- the plug checks a hash of those credentials before it will answer.
Nothing is sent to TP-Link; the password never leaves your network. It is
stored in Kodi's add-on settings in plain text, which is worth knowing.

The handshake, all of it over plain HTTP:

    POST /app/handshake1   16 random bytes from us
      <- 16 bytes of theirs, then a hash proving they hold the credentials
    POST /app/handshake2   the matching hash proving we hold them
    POST /app/request      AES-128-CBC under a key derived from both seeds

Two hash schemes exist and the device does not say which it wants. Both are
tried, and the hash the device returns in handshake1 says which was right --
a check rather than a guess.
"""

import hashlib
import hmac
import json
import os
import struct

from aes import AES
from compat import HTTPError, Request, URLError, urlopen

HTTP_PORT = 80
BLOCK = 16

SCHEME_V1 = 'v1'   # Kasa: md5 over md5s
SCHEME_V2 = 'v2'   # Tapo and later: sha256 over sha1s
SCHEMES = (SCHEME_V1, SCHEME_V2)

SESSION_COOKIE = 'TP_SESSIONID'


class KlapError(Exception):
    """The device could not be reached, or refused the handshake."""


class KlapAuthError(KlapError):
    """The device answered, and rejected the credentials.

    Kept distinct because it is the one failure the user can act on, and it
    means something quite specific: the plug is registered to a different
    TP-Link account than the one entered, or the password has changed.
    """


def _md5(data):
    return hashlib.md5(data).digest()


def _sha1(data):
    return hashlib.sha1(data).digest()


def _sha256(data):
    return hashlib.sha256(data).digest()


def auth_hash(username, password, scheme=SCHEME_V1):
    """The credential digest both ends derive their keys from."""
    username = username.encode('utf-8') if not isinstance(username, bytes) \
        else username
    password = password.encode('utf-8') if not isinstance(password, bytes) \
        else password
    if scheme == SCHEME_V2:
        return _sha256(_sha1(username) + _sha1(password))
    return _md5(_md5(username) + _md5(password))


def handshake1_hash(local_seed, remote_seed, digest, scheme=SCHEME_V1):
    """What the device must return to prove it holds the same credentials."""
    if scheme == SCHEME_V2:
        return _sha256(local_seed + remote_seed + digest)
    return _sha256(local_seed + digest)


def handshake2_hash(local_seed, remote_seed, digest, scheme=SCHEME_V1):
    """What we send back to prove the same of ourselves."""
    if scheme == SCHEME_V2:
        return _sha256(remote_seed + local_seed + digest)
    return _sha256(remote_seed + digest)


def _pad(data):
    """PKCS#7. The AES here does not pad, deliberately -- Broadlink must not."""
    padding = BLOCK - (len(data) % BLOCK)
    return data + (chr(padding) * padding).encode('latin-1')


def _unpad(data):
    if not data:
        return data
    padding = bytearray(data)[-1]
    if 0 < padding <= BLOCK and len(data) >= padding:
        return data[:-padding]
    return data


def _wrap_seq(value):
    """Keep a sequence number inside a signed 32-bit range, as the device does."""
    return ((int(value) + 0x80000000) % 0x100000000) - 0x80000000


class Encryption(object):
    """The per-connection cipher both ends derive from the two seeds."""

    def __init__(self, local_seed, remote_seed, digest):
        material = local_seed + remote_seed + digest
        self.key = _sha256(b'lsk' + material)[:16]
        full_iv = _sha256(b'iv' + material)
        self.iv = full_iv[:12]
        self.seq = struct.unpack('>i', full_iv[-4:])[0]
        self.signature = _sha256(b'ldk' + material)[:28]

    def encrypt(self, message):
        """Returns (body, sequence). The sequence goes in the request URL."""
        self.seq = _wrap_seq(self.seq + 1)
        counter = struct.pack('>i', self.seq)
        cipher = AES(self.key, self.iv + counter)
        ciphertext = cipher.encrypt(_pad(message))
        signed = _sha256(self.signature + counter + ciphertext)
        return signed + ciphertext, self.seq

    def decrypt(self, body):
        """Undo encrypt() on a reply, which is signed the same way."""
        if len(body) <= 32:
            raise KlapError('The device sent a reply too short to decrypt')
        counter = struct.pack('>i', self.seq)
        cipher = AES(self.key, self.iv + counter)
        return _unpad(cipher.decrypt(body[32:]))


class Session(object):
    """One KLAP conversation with one device."""

    def __init__(self, ip, username, password, port=HTTP_PORT, timeout=5.0,
                 log_func=None):
        self.ip = ip
        self.username = username or ''
        self.password = password or ''
        self.port = port or HTTP_PORT
        self.timeout = timeout
        self._log = log_func or (lambda message: None)
        self.cookie = ''
        self.scheme = None
        self.encryption = None

    # -- HTTP --------------------------------------------------------------

    def _post(self, path, body, query=''):
        url = 'http://%s:%d%s%s' % (self.ip, self.port, path, query)
        request = Request(url, data=body)
        request.add_header('Content-Type', 'application/octet-stream')
        if self.cookie:
            request.add_header('Cookie', self.cookie)
        try:
            response = urlopen(request, timeout=self.timeout)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise KlapAuthError(
                    '%s rejected the TP-Link account details (HTTP %d)'
                    % (self.ip, exc.code))
            raise KlapError('%s answered HTTP %d for %s'
                            % (self.ip, exc.code, path))
        except URLError as exc:
            raise KlapError('Could not reach %s on port %d: %s'
                            % (self.ip, self.port, getattr(exc, 'reason', exc)))

        self._remember_cookie(response)
        try:
            return response.read()
        finally:
            try:
                response.close()
            except Exception:
                pass

    def _remember_cookie(self, response):
        info = response.info()
        getter = getattr(info, 'get', None) or getattr(info, 'getheader')
        raw = getter('Set-Cookie') or ''
        for part in raw.split(';'):
            part = part.strip()
            if part.startswith(SESSION_COOKIE + '='):
                self.cookie = part
                return

    # -- handshake ---------------------------------------------------------

    def handshake(self):
        """Agree a session key, working out which hash scheme applies.

        The device never says which it wants. It does return a hash computed
        under whichever one it uses, and that hash is a definite answer -- so
        both are derived and compared rather than one being assumed.
        """
        local_seed = os.urandom(16)
        reply = self._post('/app/handshake1', local_seed)
        if len(reply) < 48:
            raise KlapError('%s cut the handshake short (%d bytes)'
                            % (self.ip, len(reply)))

        remote_seed, server_hash = reply[:16], reply[16:48]
        for scheme in SCHEMES:
            digest = auth_hash(self.username, self.password, scheme)
            expected = handshake1_hash(local_seed, remote_seed, digest, scheme)
            if _constant_equal(expected, server_hash):
                self.scheme = scheme
                break
        else:
            raise KlapAuthError(
                '%s did not accept the TP-Link account details.\\n\\nThe plug '
                'answers only to the account it is registered to. Check the '
                'e-mail address and password are the ones you use in the Kasa '
                'app.' % self.ip)

        self._post('/app/handshake2',
                   handshake2_hash(local_seed, remote_seed, digest,
                                   self.scheme))
        self.encryption = Encryption(local_seed, remote_seed, digest)
        self._log('KLAP handshake with %s succeeded (%s)'
                  % (self.ip, self.scheme))
        return self.scheme

    # -- requests ----------------------------------------------------------

    def request(self, body):
        """Send one JSON request and return the decoded reply."""
        if self.encryption is None:
            self.handshake()

        payload = json.dumps(body, separators=(',', ':')).encode('utf-8')
        encrypted, sequence = self.encryption.encrypt(payload)
        reply = self._post('/app/request', encrypted, '?seq=%d' % sequence)

        plain = self.encryption.decrypt(reply)
        try:
            parsed = json.loads(plain.decode('utf-8', 'replace'))
        except ValueError:
            raise KlapError('%s sent a reply that could not be read' % self.ip)
        if not isinstance(parsed, dict):
            raise KlapError('%s sent an unexpected reply' % self.ip)
        return parsed


def _constant_equal(left, right):
    """Compare two digests without leaking where they differ."""
    checker = getattr(hmac, 'compare_digest', None)
    if checker is not None:
        return checker(left, right)
    return left == right
