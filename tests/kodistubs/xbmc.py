# -*- coding: utf-8 -*-
"""Minimal xbmc stub, enough to import and drive the add-on off-device."""

import os
import tempfile

LOGDEBUG = 0
LOGINFO = 1
LOGNOTICE = 2
LOGWARNING = 3
LOGERROR = 4

LOG_LINES = []

_PROFILE = os.path.join(tempfile.gettempdir(), 'paragon-govee-test-profile')

COND_VISIBILITY = {}


def log(message, level=LOGDEBUG):
    LOG_LINES.append((level, message))


def translatePath(path):
    if path.startswith('special://profile'):
        return _PROFILE
    return path


def getCondVisibility(condition):
    return bool(COND_VISIBILITY.get(condition, False))


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


def executebuiltin(command):
    LOG_LINES.append((LOGDEBUG, 'builtin: %s' % command))
