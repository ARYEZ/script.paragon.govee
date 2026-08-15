# -*- coding: utf-8 -*-
"""
Paragon Govee
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Client for the Govee Cloud (developer) API.

This is the fallback transport, used for devices that do not have LAN Control
available. It needs an API key, which Govee issues from the Govee Home app
under Profile -> About Us -> Apply for API Key.

Unlike the LAN API this one is rate limited (Govee documents a daily account
quota and a per-device per-minute cap), so calls are throttled here and HTTP
429 is surfaced as a specific error rather than a generic failure.
"""

import json
import ssl
import time

from compat import HTTPError, Request, URLError, urlencode, urlopen, to_text

BASE_URL = 'https://developer-api.govee.com'
DEVICES_ENDPOINT = BASE_URL + '/v1/devices'
CONTROL_ENDPOINT = BASE_URL + '/v1/devices/control'
STATE_ENDPOINT = BASE_URL + '/v1/devices/state'


class CloudError(Exception):
    """A cloud request could not be completed."""


class RateLimited(CloudError):
    """The Govee API answered 429; back off before retrying."""


class CloudTransport(object):
    """Minimal Govee cloud client built on urllib, with no third-party deps.

    Kodi 17.6 does not guarantee `requests` is installed, and pulling in
    script.module.requests just for four endpoints would add an install-time
    dependency for something the standard library already covers.
    """

    def __init__(self, api_key, timeout=10, min_interval=0.5,
                 verify_ssl=True, log_func=None):
        self.api_key = (api_key or '').strip()
        self.timeout = timeout
        self.min_interval = min_interval
        self.verify_ssl = verify_ssl
        self._log = log_func or (lambda message: None)
        self._last_call = 0.0

    @property
    def configured(self):
        return bool(self.api_key)

    # -- plumbing ----------------------------------------------------------

    def _ssl_context(self):
        if self.verify_ssl:
            return None  # urlopen's default context verifies certificates
        # Escape hatch for old Kodi builds with an unusable CA bundle. Off by
        # default, and noisy in the log when used, because it disables the
        # certificate check for the API key we send on every request.
        self._log('WARNING: TLS certificate verification is disabled for '
                  'Govee cloud requests')
        return ssl._create_unverified_context()

    def _throttle(self):
        elapsed = time.time() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def _request(self, url, method='GET', payload=None):
        if not self.configured:
            raise CloudError('No Govee API key has been set')

        self._throttle()

        body = None
        headers = {
            'Govee-API-Key': self.api_key,
            'Accept': 'application/json',
        }
        if payload is not None:
            body = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'

        request = Request(url, data=body, headers=headers)
        # Python 2's urllib2 infers the verb from whether data is present, so
        # PUT has to be forced. Overriding get_method works on both 2 and 3.
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
                raise RateLimited('Govee API rate limit reached. %s' % detail)
            if exc.code in (401, 403):
                raise CloudError('Govee rejected the API key (HTTP %d). %s'
                                 % (exc.code, detail))
            raise CloudError('Govee API error HTTP %d. %s' % (exc.code, detail))
        except URLError as exc:
            raise CloudError('Could not reach the Govee API: %s' % exc.reason)
        except ssl.SSLError as exc:
            raise CloudError('TLS error talking to the Govee API: %s' % exc)
        finally:
            self._last_call = time.time()

        try:
            raw = handle.read()
        finally:
            handle.close()

        try:
            data = json.loads(to_text(raw))
        except ValueError:
            raise CloudError('Govee API returned a non-JSON response')

        code = data.get('code', 200)
        if code not in (200, 0):
            raise CloudError(data.get('message') or
                             'Govee API returned code %s' % code)
        return data

    # -- endpoints ---------------------------------------------------------

    def list_devices(self):
        """Return the raw device list from GET /v1/devices."""
        data = self._request(DEVICES_ENDPOINT)
        devices = (data.get('data') or {}).get('devices')
        if not isinstance(devices, list):
            return []
        self._log('Cloud API listed %d device(s)' % len(devices))
        return devices

    def control(self, device, model, name, value):
        """Send one command via PUT /v1/devices/control.

        `name` is a Govee command name (turn, brightness, color, colorTem) and
        `value` its documented payload for that command.
        """
        payload = {
            'device': device,
            'model': model,
            'cmd': {'name': name, 'value': value},
        }
        self._request(CONTROL_ENDPOINT, method='PUT', payload=payload)
        return True

    def state(self, device, model):
        """Return the device's reported properties as a flat dict.

        The API answers with a list of single-key dicts; flattening it here
        keeps the shape consistent with what the LAN transport returns.
        """
        query = urlencode({'device': device, 'model': model})
        data = self._request('%s?%s' % (STATE_ENDPOINT, query))
        properties = (data.get('data') or {}).get('properties')
        flattened = {}
        if isinstance(properties, list):
            for entry in properties:
                if isinstance(entry, dict):
                    flattened.update(entry)
        return flattened
