# -*- coding: utf-8 -*-
"""Minimal xbmc stub, enough to import and drive the add-on off-device."""

import json
import os
import tempfile

LOGDEBUG = 0
LOGINFO = 1
LOGNOTICE = 2
LOGWARNING = 3
LOGERROR = 4

LOG_LINES = []

_PROFILE = os.path.join(tempfile.gettempdir(), 'paragon-home-test-profile')

COND_VISIBILITY = {}


def log(message, level=LOGDEBUG):
    LOG_LINES.append((level, message))


def translatePath(path):
    if path.startswith('special://profile'):
        return _PROFILE
    return path


# Every JSON-RPC request the code under test made, newest last, as dicts.
rpc_calls = []

# What executeJSONRPC answers with, by method name. A test sets what Kodi
# would say; anything not named here comes back as a bare success, which is
# what the Input.* methods really answer with.
rpc_results = {}

# Methods that should come back as an error, by name -- for testing what the
# remote does when Kodi refuses.
rpc_errors = {}


def executeJSONRPC(request):
    """Kodi's JSON-RPC, in process.

    The real one is synchronous and returns a JSON string, which is what makes
    it usable from the service loop without a socket in sight.
    """
    try:
        payload = json.loads(request)
    except (ValueError, TypeError):
        return json.dumps({'jsonrpc': '2.0', 'id': None,
                           'error': {'code': -32700, 'message': 'Parse error'}})

    rpc_calls.append(payload)
    method = payload.get('method')

    if method in rpc_errors:
        return json.dumps({'jsonrpc': '2.0', 'id': payload.get('id'),
                           'error': rpc_errors[method]})

    result = rpc_results.get(method, 'OK')
    return json.dumps({'jsonrpc': '2.0', 'id': payload.get('id'),
                       'result': result})


def reset_rpc():
    del rpc_calls[:]
    del BUILTINS[:]
    rpc_results.clear()
    rpc_errors.clear()


def getCondVisibility(condition):
    return bool(COND_VISIBILITY.get(condition, False))


# What Kodi reports as this box's address. A test that wants to see the web
# remote's address line can set it; the default is the answer a box with no
# network would give.
IP_ADDRESS = '192.168.1.50'


def getIPAddress():
    return IP_ADDRESS


def sleep(millis):
    import time
    time.sleep(millis / 1000.0)


class Monitor(object):
    def __new__(cls, *args, **kwargs):
        # Krypton's Monitor() takes no arguments, and like Player it parses
        # them in the base type. Guarded here for the same reason.
        if args or kwargs:
            raise TypeError('function takes exactly 0 arguments')
        return object.__new__(cls)

    def __init__(self):
        self._abort = False

    def abortRequested(self):
        return self._abort

    def waitForAbort(self, timeout=0):
        import time
        time.sleep(min(timeout, 0.01))
        return self._abort

    def onSettingsChanged(self):
        pass


class Player(object):
    playing_video = False
    playing_audio = False

    def __new__(cls, *args, **kwargs):
        # Krypton's binding declares Player(int playerCore) and parses the
        # constructor arguments in the base type's tp_new -- that is, before
        # any subclass __init__ runs. So `GoveePlayer(service)` raises
        # "an integer is required" with no subclass frame in the traceback.
        # Validating in __new__ rather than __init__ is what makes this stub
        # reproduce that, instead of letting Python's normal override
        # semantics hide it.
        for arg in args:
            if not isinstance(arg, int):
                raise TypeError('an integer is required')
        return object.__new__(cls)

    def isPlaying(self):
        return self.playing_video or self.playing_audio

    def isPlayingVideo(self):
        return self.playing_video

    def isPlayingAudio(self):
        return self.playing_audio

    playing_file = ''
    playing_title = ''
    elapsed = 0.0
    total = 0.0

    def getPlayingFile(self):
        if not self.isPlaying():
            raise RuntimeError('Kodi is not playing any file')
        return self.playing_file

    def getVideoInfoTag(self):
        if not self.isPlaying():
            raise RuntimeError('Kodi is not playing any file')
        return _InfoTag(self.playing_title)

    def getTime(self):
        if not self.isPlaying():
            raise RuntimeError('Kodi is not playing any file')
        return self.elapsed

    def getTotalTime(self):
        if not self.isPlaying():
            raise RuntimeError('Kodi is not playing any file')
        return self.total



# Every builtin the code under test asked Kodi to run, newest last.
BUILTINS = []


def executebuiltin(command):
    BUILTINS.append(command)
    LOG_LINES.append((LOGDEBUG, 'builtin: %s' % command))
