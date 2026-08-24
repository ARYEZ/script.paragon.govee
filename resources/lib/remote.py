# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The web remote: one page and a JSON API, served on the LAN by the background
service, so a phone can run the scenes and sequences this box already knows.

Shaped after Kore and Yatse, pointed at the lights rather than at playback.
The verbs are the ones default.py already answers to -- scene, sequence, on,
off, toggle, brightness, colour, temperature, refresh, sync -- because a
second vocabulary for the same actions is a second thing to keep in step.

Three things are worth knowing before changing anything here.

**No handler ever touches the session.** A request arrives on one of the
server's own threads; all it does is put a job on a queue and wait. The
service loop takes the job off and performs it on the thread that owns
ParagonHome. That is the same discipline the player callbacks already follow
-- record what happened, let the loop do the talking -- and it is what stops
a phone tapping "All off" while the loop is halfway through a sequence step
from being two threads in the scene engine at once. It costs up to half a
second of queue latency, which is the tick interval, and buys a session that
is still single-threaded.

**The queue is drained inside sequence pauses too.** service.py's `_pause`
calls `_tick` every half second while a sequence waits, so a remote stays
answerable during an hour-long pause rather than going dark until it ends.

**Enabled is never accidentally open.** The server refuses to start without a
PIN rather than serving to anyone who can reach the port. See `Gate` for what
the PIN is worth and what backs it up.
"""

import collections
import json
import os
import socket
import threading
import time

import xbmc

import addon_utils as utils
import scenes as scene_lib
import sequences as sequence_lib
from compat import (BaseHTTPRequestHandler, HTTPServer, ThreadingMixIn,
                    same_secret, to_bytes, to_text)
from devices import (CAP_BRIGHTNESS, CAP_COLOR, CAP_COLOR_TEMP, CAP_POWER,
                     CAP_STATE)

# Where the API token is kept. Not in settings.xml: it is not something anyone
# types, and Kodi rewrites settings.xml on exit -- which is exactly the race
# the README warns about when copying a profile between boxes.
REMOTE_FILE = 'remote.json'

DEFAULT_PORT = 8778

PIN_LENGTH = 6
TOKEN_BYTES = 24

# How long a phone stays signed in. Long, because the failure mode of a short
# session is being asked for a PIN while standing in a dark room.
SESSION_SECONDS = 30 * 24 * 60 * 60

# What backs the PIN up. Six digits is a million guesses, which a script on the
# LAN would walk in minutes unopposed -- so wrong guesses cost time, doubling
# each round. Five is high enough that fat fingers on a phone keypad never
# reach it.
MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 60
LOCKOUT_CAP = 900

# A request body should be a few hundred bytes. Anything past this is either a
# mistake or someone seeing how much memory the service will hold for them.
MAX_BODY = 64 * 1024

# How many jobs may be waiting at once. The loop drains the whole queue every
# tick, so this only ever fills if the loop is deep in a sequence step -- and
# then a bounded queue is the point: it refuses new work rather than piling up
# a hundred "all off" jobs to run when the step finishes.
QUEUE_LIMIT = 32

# How long a handler waits for the loop to finish a job before answering
# without one. Generous: a cloud round trip to a Govee light can take a
# second, and a discovery sweep several.
RESULT_TIMEOUT = 20.0

# How stale the snapshot may be before the loop rebuilds it. It is only names
# and capabilities out of memory, but there is no reason to build it twice a
# second when nothing has changed.
SNAPSHOT_SECONDS = 2.0

# Set by the page's own fetch calls on every API request. A browser will not
# put a custom header on a cross-origin request without asking permission
# first, and nothing here ever grants it -- so a page on another origin cannot
# reach this API even from a phone that is signed in. That is the whole CSRF
# defence, and it is why it is required on login as well.
GUARD_HEADER = 'X-Paragon-Remote'
TOKEN_HEADER = 'X-Paragon-Token'
COOKIE_NAME = 'paragon_remote'

# Actions the remote accepts, and whether the phone waits for the answer.
# Sequences and discovery are not waited on: a sequence can hold an hour of
# pauses, and the phone wants to know it started, not sit there until it ends.
IMMEDIATE = ('on', 'off', 'toggle', 'brightness', 'color', 'temp', 'scene',
             'states')
# A satellite copying from its master reads five files over SSH, each with its
# own timeout, so a master that is off can take longer than a handler is
# willing to wait. Discovery is the same shape.
BACKGROUND = ('sequence', 'refresh', 'sync')
ACTIONS = IMMEDIATE + BACKGROUND

# What has to be true of a device before the page draws a row for it. Anything
# else -- an infrared blaster -- would get a row with nothing on it.
CONTROLLABLE = frozenset([CAP_POWER, CAP_BRIGHTNESS, CAP_COLOR,
                          CAP_COLOR_TEMP])


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def generate_pin(length=PIN_LENGTH):
    """A PIN nobody chose, so there is no default to forget to change.

    Drawn from os.urandom rather than `random`, and bytes at or above 250 are
    thrown away instead of folded in: 256 does not divide by 10, so taking
    every byte modulo 10 would make 0 to 5 slightly likelier than 6 to 9. It
    matters very little at six digits and costs one comparison to do properly.
    """
    digits = []
    while len(digits) < length:
        for byte in bytearray(os.urandom(length * 2)):
            if byte < 250:
                digits.append(str(byte % 10))
                if len(digits) == length:
                    break
    return ''.join(digits)


def generate_token(size=TOKEN_BYTES):
    """A long random token, hex encoded.

    Built a byte at a time because `str.encode('hex')` and `bytes.hex()` are
    each available on exactly one of the two Python versions this runs on.
    """
    return ''.join('%02x' % byte for byte in bytearray(os.urandom(size)))


def ensure_pin():
    """The configured PIN, inventing and saving one the first time."""
    pin = (utils.get_setting('remote_pin', '') or '').strip()
    if pin:
        return pin
    pin = generate_pin()
    utils.set_setting('remote_pin', pin)
    utils.log('Web remote: generated a new PIN')
    return pin


def ensure_token():
    """The API token, inventing and saving one the first time."""
    stored = utils.read_json(REMOTE_FILE, default={}) or {}
    token = stored.get('token')
    if token:
        return token
    token = generate_token()
    stored['token'] = token
    utils.write_json(REMOTE_FILE, stored)
    utils.log('Web remote: generated a new API token')
    return token


# ---------------------------------------------------------------------------
# Telling somebody where to point their phone
# ---------------------------------------------------------------------------

def local_address():
    """This box's address on the LAN, best effort, or an empty string.

    Kodi is asked first because it already knows, and it answers with the
    address of the interface actually carrying the network rather than
    whatever the hostname happens to resolve to. The fallback is for builds
    where it answers with the loopback address, which is true and useless.
    """
    try:
        address = to_text(xbmc.getIPAddress() or '')
    except Exception:
        address = ''
    if address and not address.startswith('127.'):
        return address
    try:
        found = socket.gethostbyname(socket.gethostname())
    except Exception:
        return ''
    return '' if found.startswith('127.') else found


def describe(port=None, pin=None, address=None):
    """What to tell somebody who wants to use the remote from a phone.

    Typing an address into a phone is the whole friction of this feature, so
    this is a dialog rather than a line in the log.
    """
    port = port or utils.get_int('remote_port', DEFAULT_PORT)
    if pin is None:
        pin = (utils.get_setting('remote_pin', '') or '').strip()
    if address is None:
        address = local_address()

    lines = []
    if not utils.get_bool('remote_enabled', False):
        lines.append('The web remote is switched off. Turn it on above, '
                     'then restart Kodi or wait a moment for the service '
                     'to pick it up.')
        lines.append('')

    if address:
        lines.append('http://%s:%d' % (address, port))
    else:
        lines.append('Port %d on this box, on whatever address it has.'
                     % port)
    lines.append('')
    lines.append('PIN: %s' % (pin or 'made when the remote first starts'))
    lines.append('')
    lines.append('Anything on your network that knows the PIN can control '
                 'the lights, so treat it as you would the wi-fi password. '
                 'The API token for scripts is in %s.' % REMOTE_FILE)
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Who gets in
# ---------------------------------------------------------------------------

class Gate(object):
    """The PIN, the token, and what happens to whoever keeps guessing.

    A six-digit PIN is only as good as what stands behind it, and what stands
    behind this one is the lockout: five wrong answers from an address and it
    stops answering that address for a minute, then two, then four, up to a
    quarter of an hour. Unopposed, a million guesses is a few minutes of
    scripting; at this rate it is months.

    Two ways in, one credential. A browser posts the PIN and gets a session
    token back in a cookie, so it signs in once. Anything that is not a
    browser -- curl, a keymap, another add-on -- sends the API token straight
    up in a header and skips the login. The session token expires; the API
    token does not, and changing the PIN clears the sessions but leaves it
    alone.
    """

    def __init__(self, pin, token, session_seconds=SESSION_SECONDS):
        self.pin = pin or ''
        self.token = token or ''
        self.session_seconds = session_seconds
        self._sessions = {}
        self._failures = {}
        self._lock = threading.Lock()

    # -- lockout -----------------------------------------------------------

    def locked_for(self, address, now=None):
        """Seconds this address must wait before guessing again."""
        moment = now or time.time()
        with self._lock:
            record = self._failures.get(address)
            if not record:
                return 0
            remaining = record['until'] - moment
        return int(remaining) + 1 if remaining > 0 else 0

    def _record_failure(self, address, now):
        record = self._failures.setdefault(
            address, {'count': 0, 'until': 0.0, 'rounds': 0})
        record['count'] += 1
        if record['count'] >= MAX_ATTEMPTS:
            record['rounds'] += 1
            record['count'] = 0
            wait = min(LOCKOUT_SECONDS * (2 ** (record['rounds'] - 1)),
                       LOCKOUT_CAP)
            record['until'] = now + wait
            utils.log('Web remote: %s locked out for %ds after %d wrong PINs'
                      % (address, wait, MAX_ATTEMPTS))

    # -- signing in --------------------------------------------------------

    def login(self, pin, address, now=None):
        """Trade a PIN for a session token. Returns the token, or None."""
        moment = now or time.time()
        if self.locked_for(address, moment):
            return None

        if not self.pin or not same_secret(pin, self.pin):
            with self._lock:
                self._record_failure(address, moment)
            utils.log('Web remote: wrong PIN from %s' % address)
            return None

        token = generate_token()
        with self._lock:
            self._failures.pop(address, None)
            self._sessions[token] = moment + self.session_seconds
        utils.log('Web remote: %s signed in' % address)
        return token

    def accepts(self, token, now=None):
        """Whether `token` is a live session or the API token."""
        if not token:
            return False
        # The API token first: it is the one a caller that never logs in uses,
        # and it does not expire.
        if self.token and same_secret(token, self.token):
            return True
        moment = now or time.time()
        with self._lock:
            self._prune(moment)
            expiry = self._sessions.get(token)
        return bool(expiry and expiry > moment)

    def forget(self, token):
        """Sign one session out. The API token cannot be signed out."""
        with self._lock:
            return self._sessions.pop(token, None) is not None

    def _prune(self, now):
        for token in list(self._sessions):
            if self._sessions[token] <= now:
                del self._sessions[token]


# ---------------------------------------------------------------------------
# Work waiting for the service loop
# ---------------------------------------------------------------------------

class Job(object):
    """One action, on its way to the loop and back."""

    def __init__(self, action, params):
        self.action = action
        self.params = params
        self.done = threading.Event()
        self.result = None

    def finish(self, result):
        self.result = result
        self.done.set()

    def wait(self, timeout):
        """Wait for the loop to run this. True if it did."""
        return self.done.wait(timeout)


class Commands(object):
    """The queue between the server's threads and the service loop.

    A deque rather than the standard queue module, which moved between the two
    Python versions and would need a shim of its own for one method.
    """

    def __init__(self, limit=QUEUE_LIMIT):
        self.limit = limit
        self.closed = False
        self._jobs = collections.deque()
        self._lock = threading.Lock()

    def submit(self, action, params):
        """Queue an action. Returns the Job, or None if it will not be run."""
        job = Job(action, params)
        with self._lock:
            if self.closed or len(self._jobs) >= self.limit:
                return None
            self._jobs.append(job)
        return job

    def take(self):
        with self._lock:
            if not self._jobs:
                return None
            return self._jobs.popleft()

    def pending(self):
        with self._lock:
            return len(self._jobs)

    def close(self):
        """Take no more work. Anything already queued still gets an answer."""
        with self._lock:
            self.closed = True

    def abandon(self, message='The service stopped before this ran'):
        """Answer everything still waiting, so no handler blocks on it."""
        while True:
            job = self.take()
            if job is None:
                return
            job.finish({'ok': False, 'message': message})


# ---------------------------------------------------------------------------
# Doing the work
# ---------------------------------------------------------------------------

def perform(app, action, params, sleep_func=None, on_step=None):
    """Run one action against the live session. Returns a result dict.

    Only ever called on the service loop's thread. Every branch answers with
    the same shape -- ok, and something a person could read -- because the
    page shows the message either way and a silent failure on a phone is
    indistinguishable from a light that is simply out of reach.
    """
    params = params or {}

    def outcome(result, message):
        done, errors = result
        if done:
            return {'ok': True, 'message': message}
        if errors:
            return {'ok': False, 'message': errors[0]}
        return {'ok': False,
                'message': 'Nothing to control. Run a device refresh.'}

    if action not in ACTIONS:
        return {'ok': False, 'message': 'Unknown action "%s"' % action}

    targets = None
    if action in ('on', 'off', 'toggle', 'brightness', 'color', 'temp'):
        targets = app.resolve_targets(params.get('target'))
        if targets == []:
            return {'ok': False,
                    'message': 'Nothing called "%s"' % params.get('target')}

    if action == 'on':
        return outcome(app.power_all(True, targets), 'On')
    if action == 'off':
        return outcome(app.power_all(False, targets), 'Off')
    if action == 'toggle':
        return outcome(app.toggle_all(targets), 'Toggled')

    if action == 'brightness':
        value = utils.clamp_int(params.get('value'), 1, 100)
        if value is None:
            return {'ok': False, 'message': 'Brightness needs a number 1-100'}
        return outcome(app.brightness_all(value, targets),
                       'Brightness %d%%' % value)

    if action == 'color':
        rgb = app.resolve_color(params.get('value'))
        if rgb is None:
            return {'ok': False,
                    'message': 'Colour needs a hex code or a saved name'}
        return outcome(app.color_all(rgb, targets), 'Colour set')

    if action == 'temp':
        value = utils.clamp_int(params.get('value'), 1500, 12000)
        if value is None:
            return {'ok': False, 'message': 'Temperature needs a number'}
        return outcome(app.color_temp_all(value, targets), '%dK' % value)

    if action == 'scene':
        name = params.get('name') or params.get('value') or ''
        if app.scene_by_name(name) is None:
            return {'ok': False, 'message': 'No scene called "%s"' % name}
        applied = app.apply_scene_by_name(name, announce=False)
        return {'ok': bool(applied),
                'message': name if applied else '%s reached nothing' % name}

    if action == 'sequence':
        name = params.get('name') or params.get('value') or ''
        sequence = app.sequence_by_name(name)
        if sequence is None:
            return {'ok': False, 'message': 'No sequence called "%s"' % name}
        # Called rather than run_sequence_by_name so the service's own pause
        # helper can be handed down: a step that waits then keeps the loop
        # ticking, which is what leaves the remote answerable while it runs.
        # Announce off -- a notification belongs on the television in front of
        # whoever pressed the button, and this one was pressed elsewhere.
        ran = app.run_sequence(sequence, announce=False,
                               sleep_func=sleep_func, on_step=on_step)
        return {'ok': bool(ran),
                'message': name if ran else '%s has no steps yet' % name}

    if action == 'refresh':
        found, warnings = app.refresh_devices()
        if warnings and not found:
            return {'ok': False, 'message': warnings[0]}
        return {'ok': True, 'message': 'Found %d device(s)' % len(found)}

    if action == 'sync':
        if not app.satellite_mode:
            return {'ok': False, 'message': 'This box is not a satellite'}
        copied, problems = app.sync_from_master()
        if not copied:
            return {'ok': False,
                    'message': problems[0] if problems
                    else 'Nothing copied from the master'}
        return {'ok': True, 'message': 'Copied %d file(s)' % len(copied)}

    if action == 'states':
        # The expensive one: a round trip per device, or per driver where a
        # driver can sweep. Only ever run because somebody pulled to refresh.
        readable = [device for device in app.enabled_devices
                    if CAP_STATE in app.controller.capabilities(device)]
        states = app.controller.get_states(readable) if readable else {}
        return {'ok': True, 'message': 'Read %d device(s)' % len(states),
                'states': states}

    return {'ok': False, 'message': 'Unknown action "%s"' % action}


# ---------------------------------------------------------------------------
# What the page is shown
# ---------------------------------------------------------------------------

def _hex(rgb):
    values = list(rgb or [255, 255, 255])[:3]
    while len(values) < 3:
        values.append(0)
    return '%02X%02X%02X' % tuple(max(0, min(255, int(v))) for v in values)


def _device_entry(app, device, state):
    caps = app.controller.capabilities(device)
    entry = {
        'id': device.device_id,
        'name': device.name,
        'driver': device.driver,
        'model': device.model,
        'light': scene_lib.is_a_light(device, app.controller),
        'caps': sorted(caps),
        'power': None,
        'brightness': None,
    }
    if state:
        entry['power'] = state.get('power')
        entry['brightness'] = state.get('brightness')
    return entry


def _driver_label(app, driver_id):
    driver = app.controller.driver(driver_id)
    return getattr(driver, 'DRIVER_LABEL', driver_id.title())


def snapshot(app, states=None, allow_sequences=True):
    """Everything the page draws itself from, built on the loop's thread.

    Names, capabilities and saved lists only -- all of it already in memory.
    The one thing that costs a network round trip, what each light is doing
    right now, is not here: it arrives through the `states` action when
    somebody asks for it, and is passed back in.
    """
    states = states or {}
    devices = []
    counts = collections.OrderedDict()
    for device in app.enabled_devices:
        if not CONTROLLABLE & app.controller.capabilities(device):
            # A blaster has no power, no brightness and no colour, so a row
            # for one could offer nothing. It is reached through a sequence
            # step instead -- the same reason it is not a scene target.
            continue
        devices.append(_device_entry(app, device,
                                     states.get(device.device_id)))
        counts[device.driver] = counts.get(device.driver, 0) + 1

    return {
        'ready': True,
        'name': utils.ADDON_NAME,
        'version': utils.ADDON_VERSION,
        'updated': time.time(),
        'satellite': {
            'mode': app.satellite_mode,
            'master': app.master_ip if app.satellite_mode else '',
        },
        'allow_sequences': bool(allow_sequences),
        'drivers': [{'id': driver, 'label': _driver_label(app, driver),
                     'count': counts[driver]} for driver in counts],
        'devices': devices,
        'scenes': [{'name': scene.get('name', '')} for scene in app.scenes],
        'sequences': [{'name': sequence.get('name', ''),
                       'schedule': sequence_lib.describe_schedule(sequence),
                       'steps': len(sequence_lib.filled_steps(sequence))}
                      for sequence in app.sequences],
        'palette': [{'name': entry.get('name', ''),
                     'hex': _hex(entry.get('color'))}
                    for entry in app.palette],
    }


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """One request. Reads headers, writes JSON, and touches nothing else.

    Deliberately has no reference to the session. Everything it wants either
    came off the last snapshot or goes on the queue -- see the module
    docstring for why that matters.

    No properties on this class on purpose: Python 2's request handlers are
    old-style classes, where descriptors do not behave, and a property that
    works on the test runner and not on the device is the worst of both.
    """

    protocol_version = 'HTTP/1.1'
    server_version = 'ParagonHome'
    sys_version = ''
    # Drop a connection a phone left open when it locked its screen, rather
    # than holding a thread for it. Keep-alive is still worth having: the page
    # makes two requests per tap.
    timeout = 15

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt, *args):
        """Kodi's log, not stderr -- there is no console behind a service."""
        try:
            utils.debug('Web remote: %s' % (fmt % args))
        except Exception:
            pass

    def address_string(self):
        """The caller's address, without asking DNS who they are.

        Python 2's handler resolves the client address for its log line, which
        on a home LAN with no reverse records means every request waits on a
        lookup that was never going to answer.
        """
        return self.client_address[0]

    def _header(self, name, default=''):
        return self.headers.get(name, default) or default

    def _token(self):
        """The credential this request carries, header first then cookie."""
        header = self._header(TOKEN_HEADER).strip()
        if header:
            return header
        for chunk in self._header('Cookie').split(';'):
            name, _sep, value = chunk.strip().partition('=')
            if name == COOKIE_NAME:
                return value.strip()
        return ''

    def _body(self):
        """The request body as a dict. None if it is not one."""
        try:
            length = int(self._header('Content-Length', '0') or 0)
        except ValueError:
            return None
        if length <= 0:
            return {}
        if length > MAX_BODY:
            return None
        try:
            payload = json.loads(to_text(self.rfile.read(length)))
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    # -- answering ---------------------------------------------------------

    def _send(self, status, body, content_type, extra=None):
        body = to_bytes(body)
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        # Nothing here is worth caching, and a cached /api/state is a page
        # showing lights that are no longer on.
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        # The page loads nothing from anywhere else, so nothing may leak the
        # address of this box to anywhere else either.
        self.send_header('Referrer-Policy', 'no-referrer')
        for name, value in (extra or []):
            self.send_header(name, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _send_json(self, status, payload, extra=None):
        self._send(status, json.dumps(payload),
                   'application/json; charset=utf-8', extra)

    def _send_page(self):
        self._send(200, PAGE, 'text/html; charset=utf-8', [
            # The page is one file that references nothing outside itself.
            # Saying so means a browser refuses anything that later tries to.
            ('Content-Security-Policy',
             "default-src 'none'; style-src 'unsafe-inline'; "
             "script-src 'unsafe-inline'; connect-src 'self'; "
             "img-src data:; form-action 'none'; frame-ancestors 'none'"),
        ])

    # -- the two checks a cross-origin page cannot pass --------------------

    def _custom_header(self):
        """Whether this request carries a header only our own code sets.

        A browser will not put a custom header on a cross-origin request
        without first asking permission, and nothing here ever grants it. So
        one custom header -- either the page's guard or an API token -- is
        enough to say this did not come from a page on another origin. That is
        the CSRF defence, and it is why login is behind it too.
        """
        return bool(self._header(GUARD_HEADER).strip()
                    or self._header(TOKEN_HEADER).strip())

    def _same_origin(self):
        origin = self._header('Origin').strip()
        if not origin:
            return True
        return origin.split('//')[-1] == self._header('Host').strip()

    def _allowed(self, needs_session=True):
        """Gatekeeping for every /api route. Answers the caller if refused."""
        if not self._custom_header():
            self._send_json(403, {'ok': False,
                                  'message': 'Missing %s' % GUARD_HEADER})
            return False
        if not self._same_origin():
            self._send_json(403, {'ok': False, 'message': 'Origin mismatch'})
            return False
        if needs_session and not self.server.remote.gate.accepts(self._token()):
            self._send_json(401, {'ok': False, 'message': 'Sign in first'})
            return False
        return True

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        self._route(self._get)

    def do_POST(self):
        self._route(self._post)

    def _route(self, handler):
        """Run a route, and answer with a 500 rather than dropping the socket.

        A handler that raises would otherwise close the connection with no
        status at all, which a phone shows as "could not connect" -- pointing
        at the network rather than at the traceback that is actually in the
        Kodi log.
        """
        try:
            handler(self.path.split('?', 1)[0])
        except Exception as exc:
            utils.log('Web remote: request failed: %s' % exc, xbmc.LOGERROR)
            try:
                self._send_json(500, {'ok': False, 'message': str(exc)})
            except Exception:
                pass

    def _get(self, path):
        if path in ('/', '/index.html'):
            return self._send_page()
        if path == '/favicon.ico':
            # An empty answer rather than a 404 on every single page load.
            return self._send(204, '', 'image/x-icon')
        if path == '/api/state':
            if not self._allowed():
                return None
            return self._send_json(200, self.server.remote.current_snapshot())
        return self._send_json(404, {'ok': False, 'message': 'No such route'})

    def _post(self, path):
        if path == '/api/login':
            return self._login()
        if path == '/api/logout':
            return self._logout()
        if path == '/api/action':
            if not self._allowed():
                return None
            return self._action()
        return self._send_json(404, {'ok': False, 'message': 'No such route'})

    def _login(self):
        if not self._allowed(needs_session=False):
            return None
        payload = self._body()
        if payload is None:
            return self._send_json(400, {'ok': False,
                                         'message': 'Malformed request'})

        gate = self.server.remote.gate
        address = self.client_address[0]
        waiting = gate.locked_for(address)
        if waiting:
            return self._send_json(429, {
                'ok': False, 'locked': waiting,
                'message': 'Too many wrong PINs. Try again in %d seconds.'
                           % waiting})

        token = gate.login(to_text(payload.get('pin') or ''), address)
        if not token:
            waiting = gate.locked_for(address)
            message = ('Too many wrong PINs. Try again in %d seconds.'
                       % waiting) if waiting else 'That is not the PIN'
            return self._send_json(401, {'ok': False, 'locked': waiting,
                                         'message': message})

        cookie = ('%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d'
                  % (COOKIE_NAME, token, gate.session_seconds))
        return self._send_json(200, {'ok': True, 'message': 'Signed in'},
                               [('Set-Cookie', cookie)])

    def _logout(self):
        if not self._allowed(needs_session=False):
            return None
        self.server.remote.gate.forget(self._token())
        expired = ('%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0'
                   % COOKIE_NAME)
        return self._send_json(200, {'ok': True, 'message': 'Signed out'},
                               [('Set-Cookie', expired)])

    def _action(self):
        payload = self._body()
        if payload is None:
            return self._send_json(400, {'ok': False,
                                         'message': 'Malformed request'})

        remote = self.server.remote
        action = to_text(payload.get('action') or '').strip().lower()
        if action not in ACTIONS:
            return self._send_json(400, {'ok': False,
                                         'message': 'Unknown action "%s"'
                                                    % action})
        if action == 'sequence' and not remote.allow_sequences:
            return self._send_json(403, {
                'ok': False,
                'message': 'Sequences are switched off for the remote'})

        job = remote.commands.submit(action, payload)
        if job is None:
            return self._send_json(503, {
                'ok': False, 'message': 'The service is busy. Try again.'})

        if action in BACKGROUND:
            # A sequence can hold an hour of pauses and a discovery sweep
            # takes seconds. Say it started; the snapshot will show what came
            # of it.
            return self._send_json(202, {'ok': True, 'queued': True,
                                         'message': 'Started'})

        if not job.wait(RESULT_TIMEOUT):
            return self._send_json(504, {
                'ok': False,
                'message': 'The service did not answer in time'})

        # An unreachable light is not an HTTP failure: the request was fine
        # and the answer is "no". 200 with ok=false, so the page shows the
        # reason rather than a status code.
        return self._send_json(200, job.result or
                               {'ok': False, 'message': 'No answer'})


class _Server(ThreadingMixIn, HTTPServer):
    """A thread per request, none of which outlive Kodi."""

    daemon_threads = True
    # A Kodi restart otherwise lands on the port still in TIME_WAIT from the
    # last run and the remote silently fails to come back.
    allow_reuse_address = True
    remote = None


class RemoteServer(object):
    """The web remote, from the service's point of view.

    Three calls: `start`, `pump` on every tick, `stop` on the way out.
    """

    def __init__(self, port=DEFAULT_PORT, gate=None, allow_sequences=True,
                 address=''):
        self.port = int(port or DEFAULT_PORT)
        # Empty means every interface, which is the point of the thing: the
        # phone is not on this box.
        self.address = address or ''
        self.gate = gate
        self.allow_sequences = bool(allow_sequences)
        self.commands = Commands()
        self._server = None
        self._thread = None
        self._snapshot = {'ready': False, 'message': 'Starting up'}
        self._snapshot_at = 0.0
        self._states = {}
        self._lock = threading.Lock()
        # Whether a sequence is running right now, set by whoever started it.
        # Only ever touched on the service loop's thread -- both the scheduler
        # and `pump` run there -- so it needs no lock. See `pump`.
        self.sequence_running = False

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        """Begin listening. Returns True if it did.

        Refuses without a PIN rather than serving to anyone who can reach the
        port: switching the remote on must never be the same thing as opening
        the house to the network by accident.
        """
        if self._server is not None:
            return True
        if self.gate is None or not self.gate.pin:
            utils.log('Web remote: refusing to start without a PIN',
                      xbmc.LOGERROR)
            return False

        try:
            server = _Server((self.address, self.port), _Handler)
        except Exception as exc:
            # Almost always the port already being in use -- a second Kodi, or
            # the last one still shutting down. Logged and left alone: the
            # lights keep working without a remote.
            utils.log('Web remote: cannot listen on port %d: %s'
                      % (self.port, exc), xbmc.LOGERROR)
            return False

        server.remote = self
        self._server = server
        self._thread = threading.Thread(target=self._serve)
        self._thread.daemon = True
        self._thread.start()
        utils.log('Web remote: listening on port %d' % self.port)
        return True

    def _serve(self):
        try:
            self._server.serve_forever(poll_interval=0.5)
        except Exception as exc:  # pragma: no cover - shutdown races
            utils.debug('Web remote: serve loop ended: %s' % exc)

    def stop(self):
        server, self._server = self._server, None
        if server is None:
            return
        # Refuse new work first, then answer whatever is already waiting.
        # Otherwise a handler blocked on a job the loop will never run sits
        # there until its timeout, holding the connection open.
        self.commands.close()
        self.commands.abandon()
        try:
            server.shutdown()
            server.server_close()
        except Exception as exc:  # pragma: no cover - shutdown races
            utils.debug('Web remote: shutdown said %s' % exc)
        self._thread = None
        utils.log('Web remote: stopped')

    def running(self):
        return self._server is not None

    # -- what the page reads -----------------------------------------------

    def current_snapshot(self):
        with self._lock:
            return self._snapshot

    def refresh(self, app):
        """Rebuild the snapshot. Only ever called on the loop's thread."""
        with self._lock:
            states = dict(self._states)
        try:
            fresh = snapshot(app, states=states,
                             allow_sequences=self.allow_sequences)
        except Exception as exc:
            utils.log('Web remote: could not build the snapshot: %s' % exc,
                      xbmc.LOGERROR)
            return False
        with self._lock:
            self._snapshot = fresh
            self._snapshot_at = time.time()
        return True

    # -- the loop's half of the queue --------------------------------------

    def pump(self, app, sleep_func=None, on_step=None, now=None):
        """Run everything the phone asked for. Returns how many jobs ran.

        Called from the service tick, which means it is also called from
        inside a sequence pause -- so the remote answers during an hour-long
        wait rather than going quiet until it ends. That re-entrancy is the
        point and is safe, because it is the same thread either way; the one
        thing it must not allow is a second sequence starting inside the
        first one's pause, which is what `sequence_running` is for.
        """
        moment = now or time.time()
        ran = 0
        while True:
            job = self.commands.take()
            if job is None:
                break

            if job.action == 'sequence' and self.sequence_running:
                # The rule the scheduler already follows: one sequence at a
                # time. This runs inside the first one's pause, so without
                # this the phone could start a second sequence that
                # interleaved its steps with the one already going.
                job.finish({'ok': False,
                            'message': 'A sequence is already running'})
                ran += 1
                continue

            claimed = job.action == 'sequence'
            if claimed:
                self.sequence_running = True
            try:
                result = perform(app, job.action, job.params,
                                 sleep_func=sleep_func, on_step=on_step)
            except Exception as exc:
                # A failing light must never take the service down, and must
                # never leave the phone waiting for an answer that is not
                # coming.
                utils.log('Web remote: %s failed: %s' % (job.action, exc),
                          xbmc.LOGERROR)
                result = {'ok': False, 'message': str(exc)}
            finally:
                if claimed:
                    self.sequence_running = False

            states = result.pop('states', None)
            if states is not None:
                with self._lock:
                    self._states = states
            job.finish(result)
            ran += 1

        if ran or moment - self._snapshot_at >= SNAPSHOT_SECONDS:
            self.refresh(app)
        return ran


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------
#
# One file, start to finish: no stylesheet to fetch, no framework, no font,
# no icon. Partly because there is nothing to serve them from -- this is a
# service thread in a media player, not a web host -- and partly because a
# page that loads nothing from anywhere else cannot leak the address of this
# box to anywhere else either, which is the whole reason for the strict policy
# header that goes out with it.
#
# Everything the page draws comes from /api/state, so nothing about the house
# is baked in here and a new device needs no change to this string. Names go
# in through textContent rather than innerHTML: a light is called whatever the
# Govee app says it is called, and one called `<img onerror=...>` should be a
# silly name rather than a way into the page.

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark">
<meta name="referrer" content="no-referrer">
<title>Paragon Home</title>
<style>
:root {
  --bg: #131118; --card: #1d1a25; --line: #2e2938; --text: #ece9f1;
  --muted: #9a94a8; --accent: #8b5cf6; --good: #34d399; --bad: #fb7185;
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
/* An explicit display beats the hidden attribute, and .badge has one -- so a
   box that is not a satellite showed an empty chip where the badge would be. */
[hidden] { display: none !important; }
body {
  margin: 0; padding: 16px 16px 40px; background: var(--bg); color: var(--text);
  font: 16px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  padding-bottom: calc(40px + env(safe-area-inset-bottom));
}
h1 { font-size: 20px; margin: 0; letter-spacing: .2px; }
h2 { font-size: 13px; margin: 26px 0 10px; color: var(--muted);
     text-transform: uppercase; letter-spacing: 1.2px; font-weight: 600; }
p { margin: 4px 0; }
.muted { color: var(--muted); font-size: 13px; }
header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
button {
  font: inherit; color: var(--text); background: var(--card);
  border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px;
  cursor: pointer; text-align: left; min-height: 50px;
}
button:active { border-color: var(--accent); transform: scale(.985); }
button.primary { background: var(--accent); border-color: var(--accent);
                 color: #fff; text-align: center; font-weight: 600; }
button.ghost { background: none; padding: 10px 12px; min-height: 0;
               font-size: 13px; color: var(--muted); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 10px; }
.row { display: flex; gap: 10px; }
.row > button { flex: 1; text-align: center; }
.list { display: flex; flex-direction: column; gap: 10px; }
.list button { display: block; width: 100%; }
.list .sub, .card .sub { display: block; color: var(--muted); font-size: 12px; margin-top: 3px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 14px;
        padding: 14px; margin-bottom: 10px; }
.card .name { font-weight: 600; }
.card .controls { display: flex; align-items: center; gap: 10px; margin-top: 12px; }
.card .controls button { padding: 10px 14px; min-height: 0; text-align: center; }
input[type=password], input[type=text] {
  width: 100%; font: inherit; padding: 16px; border-radius: 12px; color: var(--text);
  background: var(--card); border: 1px solid var(--line); letter-spacing: 6px;
  text-align: center; margin: 14px 0;
}
input[type=range] { width: 100%; accent-color: var(--accent); height: 42px; }
input[type=color] { width: 48px; height: 40px; padding: 0; background: none;
                    border: 1px solid var(--line); border-radius: 10px; }
.swatches { display: flex; flex-wrap: wrap; gap: 10px; }
.swatch { width: 46px; height: 46px; border-radius: 50%; border: 2px solid var(--line); padding: 0; }
.status { min-height: 22px; font-size: 13px; color: var(--muted); margin: 12px 0 0; }
.status.bad { color: var(--bad); }
.status.good { color: var(--good); }
.badge { display: inline-block; font-size: 11px; color: var(--accent);
         border: 1px solid var(--accent); border-radius: 999px; padding: 1px 8px; margin-top: 6px; }
.screen { max-width: 620px; margin: 0 auto; }
#login { padding-top: 12vh; text-align: center; }
footer { display: flex; gap: 10px; margin-top: 26px; border-top: 1px solid var(--line); padding-top: 12px; }
</style>
</head>
<body>

<div id="login" class="screen">
  <h1>Paragon Home</h1>
  <p class="muted">Settings &rarr; Remote on the Kodi box has the PIN.</p>
  <input id="pin" type="password" inputmode="numeric" autocomplete="one-time-code" maxlength="12" placeholder="PIN">
  <button id="signin" class="primary" style="width:100%">Sign in</button>
  <p id="loginerror" class="status"></p>
</div>

<div id="remote" class="screen" hidden>
  <header>
    <div>
      <h1 id="title">Paragon Home</h1>
      <p class="muted" id="subtitle"></p>
      <span class="badge" id="badge" hidden></span>
    </div>
    <button class="ghost" id="signout">Sign out</button>
  </header>
  <p class="status" id="status"></p>

  <section id="scenesBlock" hidden>
    <h2>Scenes</h2>
    <div class="grid" id="scenes"></div>
  </section>

  <section id="sequencesBlock" hidden>
    <h2>Sequences</h2>
    <div class="list" id="sequences"></div>
  </section>

  <section id="allBlock" hidden>
    <h2>All lights</h2>
    <div class="row">
      <button data-act="on">On</button>
      <button data-act="off">Off</button>
      <button data-act="toggle">Toggle</button>
    </div>
    <input type="range" id="allBright" min="1" max="100" value="60" aria-label="Brightness">
    <div class="swatches" id="palette"></div>
    <div class="row" style="margin-top:10px">
      <button data-temp="2700">Warm</button>
      <button data-temp="4000">Neutral</button>
      <button data-temp="5600">Cool</button>
    </div>
  </section>

  <div id="devices"></div>

  <footer>
    <button class="ghost" id="reread">Read the lights</button>
    <button class="ghost" id="rediscover">Search for devices</button>
  </footer>
</div>

<script>
var state = null;
var busy = false;

function el(tag, className, text) {
  var node = document.createElement(tag);
  if (className) { node.className = className; }
  if (text !== undefined && text !== null) { node.textContent = text; }
  return node;
}

function api(path, method, body) {
  var init = {
    method: method || 'GET',
    credentials: 'same-origin',
    headers: {'X-Paragon-Remote': '1'}
  };
  if (body) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  return fetch(path, init).then(function (response) {
    return response.json().then(function (data) {
      data.status = response.status;
      return data;
    }, function () {
      return {status: response.status, ok: false, message: 'Unreadable answer'};
    });
  }, function () {
    return {status: 0, ok: false, message: 'The Kodi box did not answer'};
  });
}

function say(text, kind) {
  var node = document.getElementById('status');
  node.textContent = text || '';
  node.className = 'status' + (kind ? ' ' + kind : '');
}

function showLogin() {
  document.getElementById('remote').hidden = true;
  document.getElementById('login').hidden = false;
  document.getElementById('pin').focus();
}

function signIn() {
  var field = document.getElementById('pin');
  var error = document.getElementById('loginerror');
  error.textContent = '';
  api('/api/login', 'POST', {pin: field.value}).then(function (data) {
    if (data.ok) {
      field.value = '';
      load();
    } else {
      error.textContent = data.message || 'That is not the PIN';
      error.className = 'status bad';
      field.value = '';
    }
  });
}

function act(action, extra) {
  if (busy) { return; }
  busy = true;
  var body = {action: action};
  for (var key in (extra || {})) { body[key] = extra[key]; }
  say('Working...');
  api('/api/action', 'POST', body).then(function (data) {
    busy = false;
    if (data.status === 401) { showLogin(); return; }
    say(data.message || '', data.ok ? 'good' : 'bad');
    // The action changed something; ask what it looks like now. Short delay
    // so the loop has had its tick before we ask.
    setTimeout(load, 600);
  });
}

function load() {
  return api('/api/state').then(function (data) {
    if (data.status === 401) { showLogin(); return; }
    if (data.status !== 200) {
      say(data.message || 'No answer from the Kodi box', 'bad');
      return;
    }
    if (!data.ready) { setTimeout(load, 800); return; }
    state = data;
    document.getElementById('login').hidden = true;
    document.getElementById('remote').hidden = false;
    render();
  });
}

function tile(label, sub, handler) {
  var node = el('button', null);
  node.appendChild(el('span', null, label));
  if (sub) { node.appendChild(el('span', 'sub', sub)); }
  node.addEventListener('click', handler);
  return node;
}

function renderScenes() {
  var box = document.getElementById('scenes');
  box.textContent = '';
  (state.scenes || []).forEach(function (scene) {
    box.appendChild(tile(scene.name, null, function () {
      act('scene', {name: scene.name});
    }));
  });
  document.getElementById('scenesBlock').hidden = !(state.scenes || []).length;
}

function renderSequences() {
  var box = document.getElementById('sequences');
  box.textContent = '';
  var list = state.allow_sequences ? (state.sequences || []) : [];
  list.forEach(function (sequence) {
    var sub = sequence.steps + ' step(s) - ' + sequence.schedule;
    box.appendChild(tile(sequence.name, sub, function () {
      act('sequence', {name: sequence.name});
    }));
  });
  document.getElementById('sequencesBlock').hidden = !list.length;
}

function renderPalette() {
  var box = document.getElementById('palette');
  box.textContent = '';
  (state.palette || []).forEach(function (colour) {
    var node = el('button', 'swatch');
    node.style.background = '#' + colour.hex;
    node.title = colour.name;
    node.setAttribute('aria-label', colour.name);
    node.addEventListener('click', function () {
      act('color', {value: colour.hex});
    });
    box.appendChild(node);
  });
}

function describe(device) {
  if (!device.power) { return device.model || ''; }
  var text = device.power === 'on' ? 'On' : 'Off';
  if (device.power === 'on' && device.brightness) {
    text += ' - ' + device.brightness + '%';
  }
  return text;
}

function deviceCard(device) {
  var card = el('div', 'card');
  card.appendChild(el('div', 'name', device.name));
  card.appendChild(el('span', 'sub', describe(device)));

  var caps = device.caps || [];
  var controls = el('div', 'controls');

  if (caps.indexOf('power') >= 0) {
    ['on', 'off', 'toggle'].forEach(function (verb) {
      var node = el('button', null, verb === 'toggle' ? 'Toggle'
                    : verb.charAt(0).toUpperCase() + verb.slice(1));
      node.addEventListener('click', function () {
        act(verb, {target: device.id});
      });
      controls.appendChild(node);
    });
  }

  if (caps.indexOf('color') >= 0) {
    var picker = el('input');
    picker.type = 'color';
    picker.value = '#ffffff';
    picker.setAttribute('aria-label', device.name + ' colour');
    picker.addEventListener('change', function () {
      act('color', {target: device.id, value: picker.value.replace('#', '')});
    });
    controls.appendChild(picker);
  }

  card.appendChild(controls);

  if (caps.indexOf('brightness') >= 0) {
    var slider = el('input');
    slider.type = 'range';
    slider.min = 1;
    slider.max = 100;
    slider.value = device.brightness || 60;
    slider.setAttribute('aria-label', device.name + ' brightness');
    // On change rather than input: a dragged slider fires input continuously,
    // and every one of those would be a packet at a bulb.
    slider.addEventListener('change', function () {
      act('brightness', {target: device.id, value: slider.value});
    });
    card.appendChild(slider);
  }

  return card;
}

function renderDevices() {
  var box = document.getElementById('devices');
  box.textContent = '';
  (state.drivers || []).forEach(function (driver) {
    var mine = (state.devices || []).filter(function (device) {
      return device.driver === driver.id;
    });
    if (!mine.length) { return; }
    var section = el('section');
    section.appendChild(el('h2', null, driver.label + ' (' + driver.count + ')'));
    mine.forEach(function (device) { section.appendChild(deviceCard(device)); });
    box.appendChild(section);
  });
  document.getElementById('allBlock').hidden = !(state.devices || []).length;
}

function render() {
  document.getElementById('title').textContent = state.name || 'Paragon Home';
  document.getElementById('subtitle').textContent = 'v' + (state.version || '');
  var badge = document.getElementById('badge');
  if (state.satellite && state.satellite.mode) {
    badge.textContent = 'Satellite, following ' + (state.satellite.master || 'the master');
    badge.hidden = false;
  } else {
    badge.hidden = true;
  }
  renderScenes();
  renderSequences();
  renderPalette();
  renderDevices();
}

document.getElementById('signin').addEventListener('click', signIn);
document.getElementById('pin').addEventListener('keydown', function (event) {
  if (event.key === 'Enter') { signIn(); }
});
document.getElementById('signout').addEventListener('click', function () {
  api('/api/logout', 'POST', {}).then(showLogin);
});
document.getElementById('reread').addEventListener('click', function () {
  act('states', {});
});
document.getElementById('rediscover').addEventListener('click', function () {
  act('refresh', {});
});
document.getElementById('allBright').addEventListener('change', function () {
  act('brightness', {value: this.value});
});
Array.prototype.forEach.call(document.querySelectorAll('[data-act]'), function (node) {
  node.addEventListener('click', function () { act(node.dataset.act, {}); });
});
Array.prototype.forEach.call(document.querySelectorAll('[data-temp]'), function (node) {
  node.addEventListener('click', function () {
    act('temp', {value: node.dataset.temp});
  });
});

// A scene fired from the television, or a satellite copying from its master,
// changes what this page should be showing. Only while it is actually on
// screen: a page left open in a background tab has no business waking the
// service twice a minute.
setInterval(function () {
  if (!document.hidden && state && !busy) { load(); }
}, 20000);

load();
</script>
</body>
</html>
"""
