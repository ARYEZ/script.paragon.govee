# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Client for the SwitchBot API (v1.1).

Unlike every other driver in this add-on, this one is not on the LAN. It is
not a choice: a Blind Tilt speaks Bluetooth Low Energy, the Hub 2 is what
bridges it to anything else, and SwitchBot publish no local API for that
bridge. Bluetooth from Kodi on Python 2.7 is not an option either -- it would
need a library that cannot be a standard-library import. So the route to the
blinds is out to SwitchBot and back, and it is the only route there is.

That is worth being plain about, because it means the blinds are the one
thing in Paragon Home that stops working when the internet does. Everything
else -- Govee, Tuya, Kasa, Broadlink -- keeps working on a dead uplink.

Credentials come from the SwitchBot phone app: Profile -> Preferences ->
About, then tap the app version ten times to reveal Developer Options. It
issues a token and a secret, and both are needed: every request is signed.

The signature is HMAC-SHA256 over the token, a millisecond timestamp and a
nonce, base64-encoded. hmac, hashlib, base64 and uuid are all standard
library on 2.7, so this adds no dependency.
"""

import base64
import hashlib
import hmac
import json
import ssl
import time
import uuid

from compat import (HTTPError, Request, URLError, to_bytes, to_native,
                    to_text, urlopen)

BASE_URL = 'https://api.switch-bot.com/v1.1'
DEVICES_ENDPOINT = BASE_URL + '/devices'


class CloudError(Exception):
    """A SwitchBot request could not be completed."""


class RateLimited(CloudError):
    """SwitchBot answered 429; back off before retrying."""


class SwitchBotTransport(object):
    """Minimal SwitchBot v1.1 client built on urllib, with no third-party deps.

    Kodi 17.6 does not guarantee `requests` is installed, and the whole of
    this API is three endpoints, so the standard library covers it.
    """

    def __init__(self, token, secret, timeout=10, min_interval=0.5,
                 verify_ssl=True, log_func=None):
        self.token = (token or '').strip()
        self.secret = (secret or '').strip()
        self.timeout = timeout
        self.min_interval = min_interval
        self.verify_ssl = verify_ssl
        self._log = log_func or (lambda message: None)
        self._last_call = 0.0

    @property
    def configured(self):
        """Both halves, or none. A token without its secret cannot sign."""
        return bool(self.token and self.secret)

    # -- signing -----------------------------------------------------------

    def _sign(self, now=None):
        """The four headers that authenticate one request.

        Split out from _request so a test can check the signature against a
        known token, secret, timestamp and nonce rather than against whatever
        the clock and uuid4 happened to produce.
        """
        stamp = str(int(round((now if now is not None else time.time())
                              * 1000)))
        nonce = str(uuid.uuid4())
        message = to_bytes(self.token + stamp + nonce)
        digest = hmac.new(to_bytes(self.secret), msg=message,
                          digestmod=hashlib.sha256).digest()
        return {
            'Authorization': self.token,
            't': stamp,
            'nonce': nonce,
            'sign': to_text(base64.b64encode(digest)),
        }

    # -- plumbing ----------------------------------------------------------

    def _ssl_context(self):
        if self.verify_ssl:
            return None  # urlopen's default context verifies certificates
        # Escape hatch for old Kodi builds with an unusable CA bundle. Off by
        # default, and noisy in the log when used, because it disables the
        # certificate check for credentials we send on every request.
        self._log('WARNING: TLS certificate verification is disabled for '
                  'SwitchBot requests')
        return ssl._create_unverified_context()

    def _throttle(self):
        elapsed = time.time() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def _request(self, url, method='GET', payload=None):
        if not self.configured:
            raise CloudError('No SwitchBot token and secret have been set')

        self._throttle()

        body = None
        headers = self._sign()
        headers['Accept'] = 'application/json'
        if payload is not None:
            body = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json; charset=utf8'

        # Native str for the URL and every header value. On Python 2 a single
        # unicode header makes the whole request unicode, and appending a
        # binary body to it then fails as an ascii decode error pointing at
        # the body rather than at the header that caused it.
        request = Request(to_native(url), data=body,
                          headers=dict((key, to_native(value))
                                       for key, value in headers.items()))
        # Python 2's urllib2 infers the verb from whether data is present.
        # Overriding get_method works on both 2 and 3.
        request.get_method = lambda: method

        context = self._ssl_context()
        try:
            if context is None:
                handle = urlopen(request, timeout=self.timeout)
            else:
                handle = urlopen(request, timeout=self.timeout,
                                 context=context)
        except HTTPError as exc:
            detail = ''
            try:
                detail = to_text(exc.read())[:200]
            except Exception:
                pass
            if exc.code == 429:
                raise RateLimited('SwitchBot rate limit reached. %s' % detail)
            if exc.code in (401, 403):
                raise CloudError('SwitchBot rejected the token and secret '
                                 '(HTTP %d). %s' % (exc.code, detail))
            raise CloudError('SwitchBot returned HTTP %d. %s'
                             % (exc.code, detail))
        except URLError as exc:
            raise CloudError('Could not reach SwitchBot: %s'
                             % getattr(exc, 'reason', exc))
        finally:
            self._last_call = time.time()

        try:
            raw = handle.read()
        finally:
            try:
                handle.close()
            except Exception:
                pass

        try:
            answer = json.loads(to_text(raw))
        except ValueError:
            raise CloudError('SwitchBot sent something that is not JSON')

        # The HTTP status is 200 even for a refusal; the verdict is in the
        # body. 100 is success, everything else is not.
        status = answer.get('statusCode')
        if status != 100:
            raise CloudError('SwitchBot refused the request (%s: %s)'
                             % (status, answer.get('message') or 'no reason'))
        return answer.get('body') or {}

    # -- endpoints ---------------------------------------------------------

    def devices(self):
        """Every device on the account, as SwitchBot describes it.

        Returns the infrared remotes too. Sorting out what this add-on can
        actually drive is the driver's job, not the transport's.
        """
        body = self._request(DEVICES_ENDPOINT)
        found = list(body.get('deviceList') or [])
        found.extend(body.get('infraredRemoteList') or [])
        return found

    def status(self, device_id):
        """What one device says it is currently doing."""
        return self._request('%s/%s/status' % (DEVICES_ENDPOINT,
                                               to_text(device_id)))

    def command(self, device_id, command, parameter='default',
                command_type='command'):
        """Send one command. Raises CloudError if SwitchBot will not take it."""
        payload = {
            'command': command,
            'parameter': parameter,
            'commandType': command_type,
        }
        return self._request('%s/%s/commands' % (DEVICES_ENDPOINT,
                                                 to_text(device_id)),
                             method='POST', payload=payload)
