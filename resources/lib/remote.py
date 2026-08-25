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
from devices import (CAP_BRIGHTNESS, CAP_COLOR, CAP_COLOR_TEMP, CAP_COMMANDS,
                     CAP_POWER, CAP_STATE)

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
             'command', 'states')
# A satellite copying from its master reads five files over SSH, each with its
# own timeout, so a master that is off can take longer than a handler is
# willing to wait. Discovery is the same shape.
BACKGROUND = ('sequence', 'refresh', 'sync')
ACTIONS = IMMEDIATE + BACKGROUND

# Everything the page loads from disk, as a table of whole routes rather than
# a directory to look in. The path is never built from what the URL asked for:
# joining a caller's string onto a path is how "/font/../../tuya_keys.json"
# becomes a way to read the Tuya keys, and everything above resources/ is this
# user's credentials.
#
# The typeface is Saira Condensed under the SIL Open Font License (see
# resources/fonts/OFL.txt), shipped rather than fetched from a font host: this
# is a media player on a LAN, and a remote that needs the internet to look
# right is a remote that looks wrong when the internet is down.
STATIC_FILES = {
    '/font/paragon-medium.woff2': (
        ('resources', 'fonts', 'paragon-medium.woff2'), 'font/woff2'),
    '/font/paragon-bold.woff2': (
        ('resources', 'fonts', 'paragon-bold.woff2'), 'font/woff2'),
    # The add-on's own icon, which is also what a tablet puts on its home
    # screen when the page is added to it.
    '/icon.png': (('icon.png',), 'image/png'),
}

# A year. These only change when the add-on is updated.
STATIC_CACHE = 'public, max-age=31536000, immutable'

# What has to be true of a device before the page draws a row for it: there
# has to be something the row could offer. A blaster has no power, brightness
# or colour, but it does have the codes it has been taught, and those are as
# much a thing to press as an on switch is.
ACTIONABLE = frozenset([CAP_POWER, CAP_BRIGHTNESS, CAP_COLOR, CAP_COLOR_TEMP,
                        CAP_COMMANDS])


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
    if action in ('on', 'off', 'toggle', 'brightness', 'color', 'temp',
                  'command'):
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

    if action == 'command':
        name = params.get('name') or params.get('value') or ''
        if not name:
            return {'ok': False,
                    'message': 'That needs the name of a learned command'}
        if targets is None:
            # "All" is meaningless here. A learned code belongs to the blaster
            # that learned it, and firing every code in the house that happens
            # to be called "Power" is not something to do by accident.
            return {'ok': False,
                    'message': 'That needs the blaster to send it from'}

        done, errors = app.send_command_all(name, targets)
        if done:
            return {'ok': True, 'message': name}
        if errors:
            return {'ok': False, 'message': errors[0]}
        return {'ok': False, 'message': 'Nothing there sends commands'}

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
        # The codes this one has been taught, which for a blaster is the whole
        # of what it can be asked to do.
        'commands': (sorted(app.controller.commands(device))
                     if CAP_COMMANDS in caps else []),
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
        if not ACTIONABLE & app.controller.capabilities(device):
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

    def _send(self, status, body, content_type, extra=None, cache=None):
        body = to_bytes(body)
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        # Nothing here is worth caching by default, and a cached /api/state is
        # a page showing lights that are no longer on. The font is the
        # exception, and says so for itself.
        self.send_header('Cache-Control', cache or 'no-store')
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

    def _send_static(self, route):
        """Serve one of the files the page loads. Whole-route match only."""
        parts, content_type = STATIC_FILES[route]
        try:
            handle = open(os.path.join(utils.ADDON_PATH, *parts), 'rb')
            try:
                data = handle.read()
            finally:
                handle.close()
        except (OSError, IOError) as exc:
            # The page names a fallback for each of these, so a missing file
            # is a plainer remote rather than a broken one.
            utils.debug('Web remote: cannot read %s: %s' % (route, exc))
            return self._send_json(404, {'ok': False, 'message': 'Missing'})
        return self._send(200, data, content_type, cache=STATIC_CACHE)

    def _send_manifest(self):
        """The web app manifest, so a tablet can launch this without a browser
        wrapped around it.

        Served without a session: a browser fetches it before anyone has
        signed in, and it says nothing the login page does not already show.
        """
        body = json.dumps({
            'name': utils.ADDON_NAME,
            'short_name': 'Paragon',
            'description': 'Lights, scenes and sequences on your network',
            'start_url': '/',
            'scope': '/',
            # fullscreen first, standalone as the fallback for a browser that
            # will not give up its own chrome entirely.
            'display': 'fullscreen',
            'display_override': ['fullscreen', 'standalone'],
            'background_color': '#0a0a0b',
            'theme_color': '#0a0a0b',
            'icons': [{'src': '/icon.png', 'sizes': '512x512',
                       'type': 'image/png', 'purpose': 'any maskable'}],
        })
        return self._send(200, body, 'application/manifest+json')

    def _send_page(self):
        self._send(200, PAGE, 'text/html; charset=utf-8', [
            # The page is one file that references nothing outside itself.
            # Saying so means a browser refuses anything that later tries to.
            ('Content-Security-Policy',
             "default-src 'none'; style-src 'unsafe-inline'; "
             "script-src 'unsafe-inline'; connect-src 'self'; "
             "font-src 'self'; img-src 'self' data:; "
             "manifest-src 'self'; form-action 'none'; "
             "frame-ancestors 'none'"),
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
        if path in STATIC_FILES:
            return self._send_static(path)
        if path == '/manifest.webmanifest':
            return self._send_manifest()
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
<meta name="theme-color" content="#0a0a0b">
<!-- Added to a tablet's home screen, these launch it without a browser
     wrapped around it. iOS reads the apple- ones and honours them over plain
     HTTP; Android reads the manifest. -->
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Paragon Home">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.png">
<link rel="apple-touch-icon" href="/icon.png">
<title>Paragon Home</title>
<style>
/* The typeface ships with the add-on and is served from this box, so the page
   looks right on a LAN with no way out to the internet. */
@font-face {
  font-family: 'Paragon';
  font-weight: 500;
  font-style: normal;
  font-display: swap;
  src: url('/font/paragon-medium.woff2') format('woff2');
}
@font-face {
  font-family: 'Paragon';
  font-weight: 700;
  font-style: normal;
  font-display: swap;
  src: url('/font/paragon-bold.woff2') format('woff2');
}

:root {
  --bg: #0a0a0b;
  --card: #161619;
  --card-2: #1c1c21;
  --line: #2a2a30;
  --line-lit: #3a3a44;
  --orange: #ff5b1a;
  --orange-lit: #ff7f36;
  --red: #e01b24;
  --teal: #2dd8b8;
  --text: #f2f2f4;
  --muted: #8b8b93;
  --dim: #5f5f68;
  /* Secondary text sitting on the ember band. --dim is fine against the page
     but only reaches 2.5:1 against the warm part of a card, which is under
     the 3:1 floor for text you are meant to be able to read -- and the
     schedule under a sequence name is exactly what gets read from across a
     room. */
  --sub: #a2938f;
  --hot: linear-gradient(100deg, #ff6a1f 0%, #e0202a 100%);
  /* The wash every panel carries: dark in the top-left corner, warming
     through an ember band around two thirds across, cooling again at the far
     edge. A flat diagonal rather than a glow, because a radial bloom is sized
     against the element it sits in -- so the small device cards looked right
     and the tall ones turned into a brown smear.

     Opaque stops rather than a tint over a base colour: a translucent orange
     over near-black grey desaturates into mud, where naming the colours
     outright keeps the ember an ember. */
  --wash: linear-gradient(118deg,
      #16141a 0%, #1d1519 32%, #3f1b1a 66%, #2c1619 85%, #17131b 100%);
  /* The same light in the colour a lit device answers in. */
  --wash-lit: linear-gradient(118deg,
      #121a1c 0%, #12231f 32%, #133429 66%, #12261f 85%, #11191b 100%);
  --display: 'Paragon', 'Saira Condensed', 'Oswald', 'Roboto Condensed',
             'Arial Narrow', sans-serif;
  --body: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
}

* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
/* An explicit display beats the hidden attribute, and several rules below set
   one -- so this has to win, or a hidden block shows up empty. */
[hidden] { display: none !important; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font-family: var(--body);
  font-size: 15px;
  line-height: 1.45;
  min-height: 100vh;
}

/* The slashes. Bold diagonals in the gutters, faint behind the content, which
   is what the rest of the Paragon apps look like. Pure gradient: an image
   would be another thing to serve and another thing to get wrong. */
body::before {
  content: '';
  position: fixed;
  inset: -25% -15%;
  z-index: -2;
  background: repeating-linear-gradient(114deg,
      transparent 0 44px,
      rgba(224, 26, 36, .60) 44px 58px,
      transparent 58px 74px,
      rgba(255, 91, 26, .34) 74px 81px,
      transparent 81px 148px,
      rgba(150, 18, 24, .55) 148px 170px,
      transparent 170px 214px,
      rgba(255, 60, 30, .18) 214px 220px,
      transparent 220px 300px);
  -webkit-mask-image: linear-gradient(90deg, #000 0%, rgba(0,0,0,.14) 30%,
                                      rgba(0,0,0,.14) 70%, #000 100%);
  mask-image: linear-gradient(90deg, #000 0%, rgba(0,0,0,.14) 30%,
                              rgba(0,0,0,.14) 70%, #000 100%);
}
/* Darkens the slashes towards the bottom so a long page does not turn into
   wallpaper, and keeps text over them readable. */
body::after {
  content: '';
  position: fixed;
  inset: 0;
  z-index: -1;
  background: linear-gradient(180deg, rgba(10,10,11,.30) 0%,
                              rgba(10,10,11,.72) 55%, rgba(10,10,11,.88) 100%);
}

/* -- type ---------------------------------------------------------------- */

h1, h2, .display, button, .label, .tag {
  font-family: var(--display);
  text-transform: uppercase;
  font-weight: 700;
  letter-spacing: 1.4px;
}

h1 { font-size: 26px; margin: 0; letter-spacing: 1.6px; }
h2 { font-size: 14px; margin: 0; color: var(--text); letter-spacing: 2px; }

.label {
  font-size: 11px;
  color: var(--muted);
  letter-spacing: 1.6px;
  font-weight: 700;
}

.stat {
  font-family: var(--display);
  font-weight: 700;
  font-size: 34px;
  line-height: 1;
  color: var(--orange);
  letter-spacing: .5px;
}
.stat .unit { font-size: 16px; color: var(--muted); margin-left: 2px; }

/* -- chrome -------------------------------------------------------------- */

.bar {
  position: sticky;
  top: 0;
  z-index: 10;
  background: rgba(9, 9, 10, .93);
  border-bottom: 1px solid var(--line);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  padding: 12px 16px calc(12px + env(safe-area-inset-top)) 16px;
  padding-top: max(12px, env(safe-area-inset-top));
}
.bar::after {
  content: '';
  display: block;
  height: 2px;
  background: var(--hot);
  margin: 12px -16px -13px -16px;
}
.bar-in {
  max-width: 1680px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}
.bar-in .status { flex: 1 1 200px; margin: 0; }
/* On a phone the bar has no room for a status beside the wordmark, so it
   takes a line of its own -- and gives that line back when it has nothing to
   say, rather than leaving the header two rows tall over an empty strip. */
@media (max-width: 859px) {
  .bar-in .status { order: 3; flex: 1 1 100%; }
}
.status:empty { min-height: 0; margin: 0; padding: 0; border-left: 0; }
.barside { display: flex; align-items: center; gap: 10px; margin-left: auto; }

/* PARAGON HOME, with the slash mark the rest of the family carries. */
.mark { display: flex; align-items: center; gap: 9px; min-width: 0; }
.slashes {
  width: 20px; height: 22px; flex: none;
  background: repeating-linear-gradient(114deg,
      var(--orange) 0 3px, transparent 3px 7px);
}
.wordmark { white-space: nowrap; }
.wordmark .b { color: var(--orange); }

/* The deck. A phone gets one column and scrolls; a 16:9 tablet gets the
   width it actually has and each column scrolls on its own, so the panel on
   the wall never moves under the finger that is reaching for it. */
.wrap {
  max-width: 680px;
  margin: 0 auto;
  padding: 14px 16px calc(30px + env(safe-area-inset-bottom));
}
.deck { display: grid; grid-template-columns: 1fr; gap: 14px; }
.pane { min-width: 0; }
.pane > section:first-child { margin-top: 0; }

.meta {
  display: flex; align-items: center; gap: 8px;
  flex-wrap: wrap; margin: 14px 0 2px;
}
.tag {
  font-size: 10px; letter-spacing: 1.4px; color: var(--dim);
}
.badge {
  display: inline-block;
  font-family: var(--display);
  text-transform: uppercase;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 1.4px;
  color: var(--orange);
  border: 1px solid rgba(255, 91, 26, .45);
  background: rgba(255, 91, 26, .08);
  border-radius: 2px;
  padding: 3px 8px;
}

/* -- sections ------------------------------------------------------------ */

section { margin-top: 26px; }
.head { display: flex; align-items: center; gap: 10px; margin-bottom: 11px; }
.head .nick {
  width: 12px; height: 14px; flex: none;
  background: repeating-linear-gradient(114deg,
      var(--orange) 0 3px, transparent 3px 6px);
}
/* A driver heading is a button as well as a heading, so it needs every one
   of the button rules above taken back off it. */
button.head {
  width: 100%;
  background: none;
  border: 0;
  border-radius: 0;
  padding: 0;
  min-height: 0;
  text-align: left;
  cursor: pointer;
}
button.head:active { background: none; border-color: transparent; }
.head .caret {
  width: 8px; height: 8px; flex: none;
  margin: -3px 2px 0 2px;
  border-right: 2px solid var(--muted);
  border-bottom: 2px solid var(--muted);
  transform: rotate(45deg);
  transition: transform .15s ease;
}
button.head[aria-expanded="true"] .caret {
  transform: rotate(-135deg);
  margin-top: 3px;
}
.head .rule { flex: 1; height: 1px; background: var(--line); }
/* Beside the heading rather than out at the far end of the rule: at the end
   of a column that scrolls, the scrollbar was slicing it in half. */
.head .count {
  font-family: var(--display); font-size: 11px; font-weight: 700;
  letter-spacing: 1.4px; color: var(--dim); margin-left: -4px;
}

/* -- surfaces ------------------------------------------------------------ */

.card {
  position: relative;
  background-color: var(--card);
  background-image: var(--wash);
  border: 1px solid var(--line);
  border-radius: 3px;
  padding: 14px;
}
/* The bright top edge every panel in the Paragon apps wears. */
.card::before {
  content: '';
  position: absolute;
  left: -1px; right: -1px; top: -1px;
  height: 2px;
  background: var(--hot);
  border-radius: 3px 3px 0 0;
}
.card.plain::before { display: none; }
/* A device that reports itself on wears the teal edge rather than the orange
   one, so the colour carries the state instead of just being decoration --
   and the wash inside it turns with the edge. */
.card.lit { background-image: var(--wash-lit); }
.card.lit::before {
  background: linear-gradient(100deg, #2dd8b8 0%, #17a08c 100%);
}

.grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 9px; }
.stack { display: flex; flex-direction: column; gap: 9px; }
.row { display: flex; gap: 9px; }
.row > * { flex: 1; }

/* -- controls ------------------------------------------------------------ */

button {
  font-family: var(--display);
  text-transform: uppercase;
  font-weight: 700;
  font-size: 14px;
  letter-spacing: 1.5px;
  color: var(--text);
  background: var(--card-2);
  border: 1px solid var(--line);
  border-radius: 3px;
  padding: 13px 14px;
  min-height: 46px;
  cursor: pointer;
  text-align: center;
  transition: border-color .12s, background .12s;
}
button:active { border-color: var(--orange); background: #24242a; }
button.hot {
  background: var(--hot);
  border-color: transparent;
  color: #fff;
}
button.hot:active { filter: brightness(1.14); }
button.ghost {
  background: none;
  border-color: var(--line);
  color: var(--muted);
  font-size: 12px;
  min-height: 38px;
  padding: 9px 12px;
}
button.wide { width: 100%; }

/* A scene or sequence: a panel you press, with the same lit top edge. */
button.tile {
  position: relative;
  text-align: left;
  padding: 15px 14px 14px;
  background-color: var(--card);
  background-image: var(--wash);
  overflow: hidden;
}
button.tile:active { background-color: #1d1d22; }
button.tile::before {
  content: '';
  position: absolute;
  left: 0; top: 0; right: 0;
  height: 2px;
  background: var(--hot);
  opacity: .85;
}
button.tile .sub {
  display: block;
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 1.3px;
  color: var(--sub);
  margin-top: 5px;
}

input[type=password] {
  width: 100%;
  font-family: var(--display);
  font-size: 24px;
  font-weight: 700;
  line-height: 1.1;
  letter-spacing: 10px;
  text-align: center;
  text-indent: 10px;
  color: var(--text);
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 3px;
  padding: 12px;
  margin: 7px 0 14px;
}
input[type=password]:focus { outline: none; border-color: var(--orange); }

input[type=range] {
  width: 100%;
  accent-color: var(--orange);
  height: 34px;
  background: none;
}
input[type=color] {
  width: 44px; height: 40px; flex: none;
  padding: 0;
  background: var(--card-2);
  border: 1px solid var(--line);
  border-radius: 3px;
  cursor: pointer;
}
input[type=color]::-webkit-color-swatch-wrapper { padding: 4px; }
input[type=color]::-webkit-color-swatch { border: none; border-radius: 2px; }
input[type=color]::-moz-color-swatch { border: none; border-radius: 2px; }

.swatches { display: flex; flex-wrap: wrap; gap: 9px; }
.swatch {
  width: 40px; height: 40px; min-height: 0;
  padding: 0; flex: none;
  border-radius: 50%;
  border: 2px solid rgba(255, 255, 255, .14);
}
.swatch:active { border-color: var(--text); }

/* -- devices ------------------------------------------------------------- */

.dev { margin-bottom: 9px; }
.dev .name {
  font-family: var(--display);
  text-transform: uppercase;
  font-weight: 700;
  font-size: 16px;
  letter-spacing: 1.2px;
}
.dev .state { margin-top: 2px; }
.dev .state.on { color: var(--teal); }
.dev .controls { display: flex; gap: 8px; margin-top: 12px; }
.dev .controls button { flex: 1; font-size: 12px; padding: 10px 8px;
                        min-height: 40px; }
.dev .dim { display: flex; align-items: center; gap: 12px; margin-top: 4px; }
.dev .dim .stat { font-size: 22px; min-width: 54px; }
/* The codes a blaster has been taught. A grid rather than a row: there can be
   a handful or there can be thirty, and they are named whatever the remote
   they were learned from calls them. */
.dev .codes {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(104px, 1fr));
  gap: 8px;
  margin-top: 12px;
}
.dev .codes button {
  font-size: 12px; min-height: 42px; padding: 10px 8px;
  letter-spacing: 1.1px;
}
/* --sub, not --dim: this line sits on the warm part of a card like the
   schedule under a sequence does, and --dim does not clear 3:1 there. */
.dev .empty { margin-top: 10px; color: var(--sub); }

/* -- status -------------------------------------------------------------- */

.status {
  font-family: var(--display);
  text-transform: uppercase;
  font-weight: 700;
  font-size: 11px;
  letter-spacing: 1.6px;
  color: var(--muted);
  min-height: 18px;
  margin: 14px 0 0;
  padding-left: 10px;
  border-left: 2px solid var(--line);
}
.status.good { color: var(--teal); border-left-color: var(--teal); }
.status.bad { color: #ff5f5f; border-left-color: #ff5f5f; }
.status.busy { color: var(--orange); border-left-color: var(--orange); }

footer {
  display: flex; gap: 9px; flex-wrap: wrap;
  margin-top: 22px; padding-top: 16px;
  border-top: 1px solid var(--line);
}
footer button { flex: 1 1 auto; }

/* -- signing in ---------------------------------------------------------- */

#login { max-width: 380px; margin: 0 auto; padding: 16vh 20px 40px; }
#login .mark { justify-content: center; margin-bottom: 22px; }
#login .slashes { width: 26px; height: 30px; }
#login h1 { font-size: 30px; }
#login .lede {
  text-align: center; color: var(--muted); font-size: 13px; margin: 0;
}
#login .status { text-align: left; margin-top: 14px; }

/* Two columns once there is room for them, three on a 1080p panel. minmax(0)
   on every track: a grid column defaults to min-content, and one long device
   name would otherwise push the whole column wider than its share. */
@media (min-width: 860px) {
  .wrap { max-width: 1680px; padding-left: 22px; padding-right: 22px; }
  .deck { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 20px; }
}
/* Three panes do not divide into two columns: left to itself the devices
   pane drops to a second row and leaves half the screen empty. So the tall
   pane -- scenes and sequences -- takes the left column outright and the
   other two stack down the right. */
@media (min-width: 860px) and (max-width: 1179px) {
  .pane:nth-child(1) { grid-row: 1 / span 2; }
}
@media (min-width: 1180px) {
  .deck {
    grid-template-columns: minmax(0, 1.12fr) minmax(0, .92fr) minmax(0, 1.1fr);
  }
}

/* Wide and short is a wall panel, not a document: the header stays put, the
   columns take the rest of the height, and each one scrolls inside itself.
   `hidden` still wins over these display rules -- that is what the
   !important on [hidden] near the top is for. */
@media (min-width: 860px) and (max-width: 1179px) and (min-height: 460px) {
  .deck { grid-template-rows: minmax(0, 1fr) minmax(0, 1fr); }
}

@media (min-width: 860px) and (min-height: 460px) {
  html, body { height: 100vh; overflow: hidden; }
  html, body { height: 100dvh; }
  #remote { display: flex; flex-direction: column; height: 100%; }
  .wrap { flex: 1; min-height: 0; display: flex; flex-direction: column;
          padding-bottom: 18px; }
  .deck { flex: 1; min-height: 0; }
  .pane {
    overflow-y: auto;
    overscroll-behavior: contain;
    /* Clear of the scrollbar: the device count sits at the right end of a
       section heading and was being sliced in half by it. */
    padding-right: 14px;
    scrollbar-width: thin;
    scrollbar-color: var(--line-lit) transparent;
  }
  .pane::-webkit-scrollbar { width: 8px; }
  .pane::-webkit-scrollbar-thumb {
    background: var(--line-lit); border-radius: 4px;
  }
  .pane::-webkit-scrollbar-track { background: transparent; }
  .pane { display: flex; flex-direction: column; }
  /* Anchored to the bottom of its column, so the middle one reads as a
     finished panel rather than as content that ran out. */
  footer { margin-top: auto; }
}

/* Given real height, the scenes and sequences stretch to use it. A wall panel
   wants large targets, and six scenes clustered at the top of a column with
   four hundred pixels of nothing under them looks like a page that failed to
   load the rest of itself. Past a certain number they stop growing -- the
   tile keeps its minimum and the column scrolls instead. */
@media (min-width: 1180px) and (min-height: 620px) {
  #scenesBlock, #sequencesBlock {
    display: flex; flex-direction: column; min-height: 0;
  }
  /* The scenes take the height they need at a good size, and the sequences
     take whatever is left. Splitting the slack between both blocks instead
     opened a gap in the middle of the column, because a grid row will not
     grow past its cap however much room is going spare. */
  /* Both blocks take the height their contents want, and what is left over
     collects at the bottom of the column. Handing the slack to one of them
     instead only works while it is the lower of the two: a stretchy block on
     top pushes the other one down the screen and opens a gap in the middle.

     Both are sized against the height of the panel rather than fixed. At 1080
     they want to be generous; at 720 the same sizes would push the scenes off
     the bottom of the screen entirely. The plain value first is what a
     browser without clamp() falls back to. */
  #sequencesBlock, #scenesBlock { flex: 0 0 auto; }
  #sequences button { height: 150px; }
  #sequences button { height: clamp(96px, 15vh, 150px); }
  #scenes { grid-auto-rows: 150px; }
  #scenes { grid-auto-rows: clamp(86px, 13.5vh, 150px); }
}

/* Read from across the room, and tapped standing up. */
@media (min-width: 1180px) {
  body { font-size: 16px; }
  h1 { font-size: 30px; }
  h2 { font-size: 16px; }
  .slashes { width: 24px; height: 26px; }
  button { font-size: 16px; min-height: 54px; }
  button.tile { padding: 19px 17px; font-size: 19px; min-height: 86px; }
  button.tile .sub { font-size: 11px; margin-top: 7px; }
  /* Wider tiles so six scenes fill a column rather than huddling at the top
     of it, and so the label has room at arm's length. */
  .grid { grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); }
  .dev .name { font-size: 19px; }
  .dev .controls button { font-size: 14px; min-height: 46px; }
  .stat { font-size: 40px; }
  .swatch { width: 46px; height: 46px; }
  .label { font-size: 12px; }
}

@media (min-width: 560px) {
  .grid { grid-template-columns: repeat(auto-fill, minmax(148px, 1fr)); }
}
</style>
</head>
<body>

<div id="login" hidden>
  <div class="mark">
    <span class="slashes"></span>
    <h1 class="wordmark">Paragon <span class="b">Home</span></h1>
  </div>
  <p class="lede">Settings &rarr; Remote on the Kodi box has the PIN.</p>
  <p class="label" style="margin:26px 0 0">Enter PIN</p>
  <input id="pin" type="password" inputmode="numeric" autocomplete="one-time-code" maxlength="12">
  <button id="signin" class="hot wide">Sign in</button>
  <p class="status" id="loginerror"></p>
</div>

<div id="remote" hidden>
  <div class="bar">
    <div class="bar-in">
      <div class="mark">
        <span class="slashes"></span>
        <h1 class="wordmark">Paragon <span class="b">Home</span></h1>
      </div>
      <p class="status" id="status"></p>
      <div class="barside">
        <span class="tag" id="version"></span>
        <span class="badge" id="badge" hidden></span>
        <button class="ghost" id="fullscreen" hidden>Full screen</button>
        <button class="ghost" id="signout">Sign out</button>
      </div>
    </div>
  </div>

  <div class="wrap">
   <div class="deck">

    <div class="pane">
    <section id="sequencesBlock" hidden>
      <div class="head">
        <span class="nick"></span><h2>Sequences</h2><span class="rule"></span>
      </div>
      <div class="stack" id="sequences"></div>
    </section>

    <section id="scenesBlock" hidden>
      <div class="head">
        <span class="nick"></span><h2>Scenes</h2><span class="rule"></span>
      </div>
      <div class="grid" id="scenes"></div>
    </section>
    </div>

    <div class="pane">
    <section id="allBlock" hidden>
      <div class="head">
        <span class="nick"></span><h2>All lights</h2><span class="rule"></span>
      </div>
      <div class="card">
        <div class="row">
          <button data-act="on">On</button>
          <button data-act="off">Off</button>
          <button data-act="toggle">Toggle</button>
        </div>
        <p class="label" style="margin:16px 0 0">Brightness</p>
        <div class="dev dim">
          <span class="stat" id="allBrightValue">60<span class="unit">%</span></span>
          <input type="range" id="allBright" min="1" max="100" value="60" aria-label="Brightness">
        </div>
        <p class="label" style="margin:10px 0 9px">Colour</p>
        <div class="swatches" id="palette"></div>
        <p class="label" style="margin:16px 0 9px">White</p>
        <div class="row">
          <button data-temp="2700">Warm</button>
          <button data-temp="4000">Neutral</button>
          <button data-temp="5600">Cool</button>
        </div>
      </div>
    </section>

    <footer>
      <button class="ghost" id="reread">Read the lights</button>
      <button class="ghost" id="rediscover">Search for devices</button>
    </footer>
    </div>

    <div class="pane" id="devices"></div>

   </div>
  </div>
</div>

<script>
var state = null;
var busy = false;

/* -- full screen ---------------------------------------------------------
   A page cannot put itself into full screen when it loads. Every browser
   requires a gesture first, or any site could take over the display of
   anything that visited it -- so the closest to launching full screen is to
   remember that this panel wants it, and take the first touch as the gesture.

   Kept in this browser's own storage rather than in a Kodi setting: it is a
   property of the tablet on the wall, not of the house. The phone that
   occasionally opens the same address should not inherit it. */
var FULLSCREEN_KEY = 'paragon.fullscreen';

/* How many devices a driver may have before its section starts closed. Below
   this it costs nothing to show them; above it, one long section buries every
   other one under it. Whatever you then open or close is remembered, and like
   the full screen preference it lives in this browser rather than in a Kodi
   setting -- it is how this panel is arranged, not how the house is. */
var COLLAPSE_ABOVE = 6;
var SECTION_KEY = 'paragon.section.';

function sectionOpen(driver) {
  try {
    var saved = localStorage.getItem(SECTION_KEY + driver.id);
    if (saved !== null) { return saved === '1'; }
  } catch (error) { /* storage off; fall through to the default */ }
  return driver.count <= COLLAPSE_ABOVE;
}

function rememberSection(id, open) {
  try {
    localStorage.setItem(SECTION_KEY + id, open ? '1' : '0');
  } catch (error) { /* nothing to be done about it */ }
}

function fullscreenAvailable() {
  var el = document.documentElement;
  return !!(el.requestFullscreen || el.webkitRequestFullscreen);
}

function inFullscreen() {
  return !!(document.fullscreenElement || document.webkitFullscreenElement);
}

function wantsFullscreen() {
  try {
    return localStorage.getItem(FULLSCREEN_KEY) === '1';
  } catch (error) {
    // Private browsing, or storage switched off. The button still works for
    // this visit; it just will not be remembered for the next one.
    return false;
  }
}

function rememberFullscreen(on) {
  try {
    localStorage.setItem(FULLSCREEN_KEY, on ? '1' : '0');
  } catch (error) { /* nothing to be done about it */ }
}

function enterFullscreen() {
  var el = document.documentElement;
  var ask = el.requestFullscreen || el.webkitRequestFullscreen;
  if (!ask) { return; }
  try {
    var pending = ask.call(el);
    // Rejects when the browser did not count this as a gesture. That is a
    // refusal rather than a fault, and an uncaught rejection would land in
    // the console as though something had broken.
    if (pending && pending['catch']) { pending['catch'](function () {}); }
  } catch (error) { /* same */ }
}

function leaveFullscreen() {
  var drop = document.exitFullscreen || document.webkitExitFullscreen;
  if (!drop) { return; }
  try {
    var pending = drop.call(document);
    if (pending && pending['catch']) { pending['catch'](function () {}); }
  } catch (error) { /* same */ }
}

function paintFullscreenButton() {
  var node = document.getElementById('fullscreen');
  node.hidden = !fullscreenAvailable();
  node.textContent = inFullscreen() ? 'Exit full screen' : 'Full screen';
}

/* Takes the next touch anywhere as the gesture, once, so a panel that has
   asked for full screen returns to it the moment somebody reaches for it.
   Listening on the way down and never cancelling, so that same touch still
   lands on whatever button it was aimed at. */
function armFullscreen() {
  if (!wantsFullscreen() || !fullscreenAvailable() || inFullscreen()) {
    return;
  }
  var once = function () {
    document.removeEventListener('pointerdown', once, true);
    if (wantsFullscreen() && !inFullscreen()) { enterFullscreen(); }
  };
  document.addEventListener('pointerdown', once, true);
}

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
  say('Working', 'busy');
  api('/api/action', 'POST', body).then(function (data) {
    busy = false;
    if (data.status === 401) { showLogin(); return; }
    say(data.message || '', data.ok ? 'good' : 'bad');
    // Ask what it looks like now, once the loop has had its tick.
    setTimeout(load, 600);
  });
}

function load() {
  return api('/api/state').then(function (data) {
    if (data.status === 401) { showLogin(); return; }
    if (data.status !== 200) {
      // Both screens start hidden, so an error has to land somewhere the
      // phone can see it -- otherwise this is a black page and no reason.
      showLogin();
      if (data.status !== 401) {
        var problem = document.getElementById('loginerror');
        problem.textContent = data.message || 'No answer from the Kodi box';
        problem.className = 'status bad';
      }
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
  var node = el('button', 'tile');
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
  // A blaster has no power to report; what it has is however many codes it
  // has been taught.
  if ((device.caps || []).indexOf('power') < 0) {
    var learned = (device.commands || []).length;
    return learned ? learned + ' code(s)' : (device.model || 'Infrared');
  }
  if (!device.power) { return device.model || 'Ready'; }
  if (device.power !== 'on') { return 'Off'; }
  return device.brightness ? 'On at ' + device.brightness + '%' : 'On';
}

function deviceCard(device) {
  var card = el('div', 'card dev' + (device.power === 'on' ? ' lit' : ''));
  card.appendChild(el('div', 'name', device.name));
  var state_line = el('div', 'label state', describe(device));
  if (device.power === 'on') { state_line.className = 'label state on'; }
  card.appendChild(state_line);

  var caps = device.caps || [];
  var controls = el('div', 'controls');

  if (caps.indexOf('power') >= 0) {
    [['on', 'On'], ['off', 'Off'], ['toggle', 'Toggle']].forEach(function (pair) {
      var node = el('button', null, pair[1]);
      node.addEventListener('click', function () {
        act(pair[0], {target: device.id});
      });
      controls.appendChild(node);
    });
  }

  if (caps.indexOf('color') >= 0) {
    var picker = el('input');
    picker.type = 'color';
    // The first speed-dial colour rather than white: the well shows whatever
    // it is set to, and a white block reads as a gap in the card.
    picker.value = '#' + (((state.palette || [])[0] || {}).hex || 'FFB46B');
    picker.setAttribute('aria-label', device.name + ' colour');
    picker.addEventListener('change', function () {
      act('color', {target: device.id, value: picker.value.replace('#', '')});
    });
    controls.appendChild(picker);
  }

  card.appendChild(controls);

  if (caps.indexOf('brightness') >= 0) {
    var start = device.brightness || 60;
    var dim = el('div', 'dim');
    var readout = el('span', 'stat');
    readout.appendChild(document.createTextNode(String(start)));
    readout.appendChild(el('span', 'unit', '%'));

    var slider = el('input');
    slider.type = 'range';
    slider.min = 1;
    slider.max = 100;
    slider.value = start;
    slider.setAttribute('aria-label', device.name + ' brightness');
    slider.addEventListener('input', function () {
      readout.firstChild.nodeValue = slider.value;
    });
    // On change rather than input: a dragged slider fires input continuously,
    // and every one of those would be a packet at a bulb.
    slider.addEventListener('change', function () {
      act('brightness', {target: device.id, value: slider.value});
    });

    dim.appendChild(readout);
    dim.appendChild(slider);
    card.appendChild(dim);
  }

  if (caps.indexOf('commands') >= 0) {
    if ((device.commands || []).length) {
      var codes = el('div', 'codes');
      device.commands.forEach(function (name) {
        var node = el('button', null, name);
        node.addEventListener('click', function () {
          act('command', {target: device.id, name: name});
        });
        codes.appendChild(node);
      });
      card.appendChild(codes);
    } else {
      // Listed with nothing to press rather than left out: knowing the
      // blaster is found and reachable is most of what you wanted to know,
      // and learning a code is a job for the box it is plugged into.
      card.appendChild(el('p', 'label empty',
                          'No codes learned yet - teach it in Kodi'));
    }
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

    var open = sectionOpen(driver);
    var section = el('section');

    var head = el('button', 'head');
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
    head.appendChild(el('span', 'nick'));
    head.appendChild(el('h2', null, driver.label));
    head.appendChild(el('span', 'count', String(driver.count)));
    head.appendChild(el('span', 'rule'));
    head.appendChild(el('span', 'caret'));

    var body = el('div');
    body.hidden = !open;
    mine.forEach(function (device) { body.appendChild(deviceCard(device)); });

    head.addEventListener('click', function () {
      var opening = body.hidden;
      body.hidden = !opening;
      head.setAttribute('aria-expanded', opening ? 'true' : 'false');
      rememberSection(driver.id, opening);
    });

    section.appendChild(head);
    section.appendChild(body);
    box.appendChild(section);
  });
  document.getElementById('allBlock').hidden = !(state.devices || []).length;
}

function render() {
  document.getElementById('version').textContent = 'v' + (state.version || '');
  var badge = document.getElementById('badge');
  if (state.satellite && state.satellite.mode) {
    badge.textContent = 'Satellite - following ' + (state.satellite.master || 'the master');
    badge.hidden = false;
  } else {
    badge.hidden = true;
  }
  renderScenes();
  renderSequences();
  renderPalette();
  renderDevices();
}

document.getElementById('fullscreen').addEventListener('click', function () {
  if (inFullscreen()) {
    rememberFullscreen(false);
    leaveFullscreen();
  } else {
    rememberFullscreen(true);
    enterFullscreen();
  }
});
document.addEventListener('fullscreenchange', paintFullscreenButton);
document.addEventListener('webkitfullscreenchange', paintFullscreenButton);
paintFullscreenButton();
armFullscreen();

document.getElementById('signin').addEventListener('click', function () {
  // Signing in is a gesture, and on a panel that has asked for full screen it
  // is the one that gets there without needing a second tap.
  if (wantsFullscreen()) { enterFullscreen(); }
  signIn();
});
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

var allBright = document.getElementById('allBright');
var allBrightValue = document.getElementById('allBrightValue');
allBright.addEventListener('input', function () {
  allBrightValue.firstChild.nodeValue = allBright.value;
});
allBright.addEventListener('change', function () {
  act('brightness', {value: allBright.value});
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
