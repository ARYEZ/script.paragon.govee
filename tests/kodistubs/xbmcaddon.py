# -*- coding: utf-8 -*-
"""Minimal xbmcaddon stub backed by a plain dict of settings."""

import os
import tempfile

SETTINGS = {}
OPENED_SETTINGS = []

# Settings belonging to *other* add-ons, keyed by add-on id. Kodi keeps each
# add-on's settings to itself and raises for one that is not installed, so a
# stub that answered for any id could not tell "Paragon TV is not here" from
# "Paragon TV has nothing set".
FOREIGN = {}
PROFILES = {}

_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROFILE = os.path.join(tempfile.gettempdir(), 'paragon-home-test-profile')

_INFO = {
    'id': 'script.paragon.home',
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
    FOREIGN.clear()
    PROFILES.clear()
    del OPENED_SETTINGS[:]


def install(addon_id, settings=None, profile=None):
    """Pretend another add-on is installed, with the settings given.

    `profile` is its saved-data folder, which Kodi gives every add-on and
    which is how one add-on can find what another left behind.
    """
    FOREIGN[addon_id] = dict(settings or {})
    if profile is not None:
        PROFILES[addon_id] = profile
    return FOREIGN[addon_id]


class Addon(object):
    def __init__(self, addon_id=None):
        self._id = addon_id or _INFO['id']
        if self._id != _INFO['id'] and self._id not in FOREIGN:
            # What Kodi does for an add-on that is not installed.
            raise RuntimeError('Addon "%s" is not installed' % self._id)

    @property
    def _store(self):
        if self._id == _INFO['id']:
            return SETTINGS
        return FOREIGN[self._id]

    def getAddonInfo(self, key):
        if self._id != _INFO['id']:
            if key == 'id':
                return self._id
            if key == 'profile':
                return PROFILES.get(self._id, '')
            return ''
        return _INFO.get(key, '')

    def getSetting(self, setting_id):
        return self._store.get(setting_id, '')

    def setSetting(self, setting_id, value):
        self._store[setting_id] = value

    def openSettings(self):
        OPENED_SETTINGS.append(self._id)

    def getLocalizedString(self, string_id):
        return str(string_id)
