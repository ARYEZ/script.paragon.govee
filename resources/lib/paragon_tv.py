# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Reading Paragon TV's Rerack schedule.

Paragon TV has its own Rerack -- a nine-phase preset macro system, one preset
per day of the week, each phase pinned to a time. This module reads that
schedule so a rerack here can be hung off one of those phases: the lights come
up when the channel does, the room goes dark when the box shuts down.

Nothing is written. Paragon TV's settings are read exactly as Paragon TV reads
them and never changed, so this cannot disturb a working television setup.
Paragon TV does not need to know this exists, and works unchanged whether or
not it does.

The one detail that matters: the day settings hold an *index*, not a name.
"3" means the third preset, and "0" means no preset today. Reading the value
as a name would silently find nothing.
"""

import xbmcaddon

ADDON_ID = 'script.paragontv'

# Order matters: the day settings store a 1-based index into this list, and
# it must match Paragon TV's own order exactly.
PRESET_NAMES = ('Alpha', 'Omega', 'Delta', 'Epsilon', 'Gamma', 'Sigma',
                'Omicron', 'Theta', 'Lambda')

DAY_SETTINGS = ('MondayPreset', 'TuesdayPreset', 'WednesdayPreset',
                'ThursdayPreset', 'FridayPreset', 'SaturdayPreset',
                'SundayPreset')

PHASE_COUNT = 9

# What each phase is for, in Paragon TV's own terms. Shown when picking one,
# because "phase 6" means nothing on its own.
PHASE_LABELS = (
    'maintenance',
    'wake and tune',
    'shut down',
    'push to satellites',
    'wake and tune',
    'shut down',
    'wake and tune',
    'shut down',
    'wake and tune',
)

# The satellite presets carry no maintenance phase at all, so a rerack hung
# off phase 1 will simply never come round on those days.
NO_PHASE_ONE = ('Sigma', 'Omicron', 'Theta', 'Lambda')


def _addon():
    """Paragon TV's add-on handle, or None when it is not installed."""
    try:
        return xbmcaddon.Addon(ADDON_ID)
    except Exception:
        # Kodi raises for an add-on that is not installed, and the exception
        # type has varied across versions.
        return None


def installed():
    return _addon() is not None


def _setting(key):
    addon = _addon()
    if addon is None:
        return ''
    try:
        return addon.getSetting(key) or ''
    except Exception:
        return ''


def enabled():
    """Whether Paragon TV's own preset system is switched on."""
    return _setting('EnablePresetSystem') == 'true'


def preset_for_day(weekday):
    """The preset name for a weekday (0 = Monday), or '' for none.

    The setting holds a 1-based index into PRESET_NAMES, with 0 meaning no
    preset that day -- which is how Paragon TV reads it, and reading it any
    other way finds nothing without saying so.
    """
    if not 0 <= weekday <= 6:
        return ''
    raw = _setting(DAY_SETTINGS[weekday]) or '0'
    try:
        index = int(raw)
    except (TypeError, ValueError):
        return ''
    if 1 <= index <= len(PRESET_NAMES):
        return PRESET_NAMES[index - 1]
    return ''


def todays_preset(now):
    return preset_for_day(now.weekday())


def week():
    """Paragon TV's whole weekly table, as seven names or blanks."""
    return [preset_for_day(day) for day in range(7)]


def phase_time(preset, phase):
    """When a phase of a preset runs, as 'HH:MM', or '' if it does not.

    A satellite preset has no phase 1, and any phase may simply have no time
    set -- both mean the same thing here: nothing to hang a rerack off.
    """
    if not preset or not 1 <= phase <= PHASE_COUNT:
        return ''
    if phase == 1 and preset in NO_PHASE_ONE:
        return ''
    return (_setting('%sPhase%dTime' % (preset, phase)) or '').strip()


def describe_phase(phase):
    """'phase 3 (shut down)', for a menu."""
    if not 1 <= phase <= PHASE_COUNT:
        return 'phase %s' % phase
    return 'phase %d (%s)' % (phase, PHASE_LABELS[phase - 1])


def status(now):
    """A short account of what Paragon TV says about today, for a dialog."""
    if not installed():
        return 'Paragon TV is not installed.'
    if not enabled():
        return 'Paragon TV is installed, but its Rerack system is switched off.'
    preset = todays_preset(now)
    if not preset:
        return 'Paragon TV has no preset scheduled for today.'

    times = []
    for phase in range(1, PHASE_COUNT + 1):
        when = phase_time(preset, phase)
        if when:
            times.append('%s  %s' % (when, describe_phase(phase)))
    if not times:
        return "Today's preset is %s, which has no phase times set." % preset
    return "Today's preset is %s:\n\n%s" % (preset, '\n'.join(times))
