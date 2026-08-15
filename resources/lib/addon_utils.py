# -*- coding: utf-8 -*-
"""
Paragon Govee
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Thin wrappers over the Kodi add-on APIs: logging, settings, notifications and
the profile directory used to persist the device cache and scene list.
"""

import json
import os

import xbmc
import xbmcaddon
import xbmcgui

from compat import PY2, to_bytes, to_text

ADDON = xbmcaddon.Addon()
ADDON_ID = ADDON.getAddonInfo('id')
ADDON_NAME = ADDON.getAddonInfo('name')
ADDON_PATH = ADDON.getAddonInfo('path')
ADDON_ICON = os.path.join(ADDON_PATH, 'icon.png')


def _translate(path):
    """xbmc.translatePath on Krypton, xbmcvfs.translatePath on Kodi 19+."""
    translate = getattr(xbmc, 'translatePath', None)
    if translate is None:  # pragma: no cover - Kodi 19+ only
        import xbmcvfs
        translate = xbmcvfs.translatePath
    return to_text(translate(path))


PROFILE_PATH = _translate(ADDON.getAddonInfo('profile'))


def profile_file(name):
    """Absolute path to `name` inside the add-on's profile directory."""
    return os.path.join(PROFILE_PATH, name)


def ensure_profile():
    """Create the profile directory if Kodi has not done so yet."""
    if not os.path.isdir(PROFILE_PATH):
        try:
            os.makedirs(PROFILE_PATH)
        except OSError as exc:
            log('Could not create profile dir %s: %s' % (PROFILE_PATH, exc),
                xbmc.LOGERROR)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(message, level=None):
    """Write to the Kodi log, always prefixed with the add-on id.

    Kodi 19 removed LOGNOTICE, so the default level is resolved at call time
    rather than baked into the signature.
    """
    if level is None:
        level = getattr(xbmc, 'LOGNOTICE', None)
        if level is None:  # pragma: no cover - Kodi 19+ only
            level = xbmc.LOGINFO
    try:
        line = '%s: %s' % (ADDON_ID, to_text(message))
        if PY2:
            # Krypton's xbmc.log wants a byte string; handing it a unicode
            # object with a non-ASCII light name in it raises. Device names
            # come from the user, so that is a live case, not a theoretical one.
            line = to_bytes(line)
        xbmc.log(line, level)
    except Exception:
        # Logging must never be the thing that breaks playback.
        pass


def debug(message):
    """Log only when the user has switched on verbose logging."""
    if get_bool('debug_logging'):
        log(message, xbmc.LOGDEBUG)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_setting(setting_id, default=''):
    try:
        value = ADDON.getSetting(setting_id)
    except Exception:
        return default
    if value is None or value == '':
        return default
    return to_text(value)


def set_setting(setting_id, value):
    try:
        ADDON.setSetting(setting_id, to_text(value))
    except Exception as exc:
        log('Could not write setting %s: %s' % (setting_id, exc), xbmc.LOGERROR)


def get_bool(setting_id, default=False):
    value = get_setting(setting_id, '')
    if value == '':
        return default
    return value.lower() == 'true'


def get_int(setting_id, default=0):
    value = get_setting(setting_id, '')
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def open_settings():
    ADDON.openSettings()


# ---------------------------------------------------------------------------
# User feedback
# ---------------------------------------------------------------------------

def notify(message, heading=None, millis=4000, icon=None):
    """Toast notification, suppressed when the user has turned them off."""
    if not get_bool('show_notifications', True):
        return
    force_notify(message, heading, millis, icon)


def force_notify(message, heading=None, millis=4000, icon=None):
    """Toast notification that ignores the 'show notifications' setting.

    Used for errors the user needs to see even with toasts turned off.
    """
    try:
        xbmcgui.Dialog().notification(
            to_text(heading or ADDON_NAME),
            to_text(message),
            to_text(icon or ADDON_ICON),
            millis,
        )
    except Exception as exc:
        log('Notification failed: %s' % exc, xbmc.LOGERROR)


# ---------------------------------------------------------------------------
# Small JSON store used for the device cache and the scene list
# ---------------------------------------------------------------------------

def read_json(name, default=None):
    """Load `name` from the profile dir, returning `default` on any problem."""
    path = profile_file(name)
    if not os.path.isfile(path):
        return default
    try:
        handle = open(path, 'r')
        try:
            data = handle.read()
        finally:
            handle.close()
        return json.loads(to_text(data))
    except (OSError, IOError, ValueError) as exc:
        log('Could not read %s: %s' % (path, exc), xbmc.LOGERROR)
        return default


def write_json(name, payload):
    """Persist `payload` as JSON in the profile dir. Returns True on success."""
    ensure_profile()
    path = profile_file(name)
    try:
        handle = open(path, 'w')
        try:
            handle.write(json.dumps(payload, indent=4, sort_keys=True))
        finally:
            handle.close()
        return True
    except (OSError, IOError, TypeError) as exc:
        log('Could not write %s: %s' % (path, exc), xbmc.LOGERROR)
        return False
