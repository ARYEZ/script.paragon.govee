# -*- coding: utf-8 -*-
"""Minimal xbmcaddon stub backed by a plain dict of settings."""

import os
import tempfile

SETTINGS = {}
OPENED_SETTINGS = []

_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROFILE = os.path.join(tempfile.gettempdir(), 'paragon-home-test-profile')

_INFO = {
    'id': 'script.paragon.govee',
    'name': 'Paragon Home',
    'path': _PATH,
    'profile': _PROFILE,
    'version': '1.0.0',
}


def reset(defaults=None):
    """Clear settings between tests."""
    SETTINGS.clear()
    if defaults:
        SETTINGS.update(defaults)
    del OPENED_SETTINGS[:]


class Addon(object):
    def __init__(self, addon_id=None):
        self._id = addon_id or _INFO['id']

    def getAddonInfo(self, key):
        return _INFO.get(key, '')

    def getSetting(self, setting_id):
        return SETTINGS.get(setting_id, '')

    def setSetting(self, setting_id, value):
        SETTINGS[setting_id] = value

    def openSettings(self):
        OPENED_SETTINGS.append(self._id)

    def getLocalizedString(self, string_id):
        return str(string_id)
