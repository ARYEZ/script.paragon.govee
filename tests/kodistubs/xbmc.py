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

    def isPlaying(self):
        return self.playing_video or self.playing_audio

    def isPlayingVideo(self):
        return self.playing_video

    def isPlayingAudio(self):
        return self.playing_audio


def executebuiltin(command):
    LOG_LINES.append((LOGDEBUG, 'builtin: %s' % command))
