# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Reracks: a day laid out in nine phases, each holding a sequence.

Shaped after the Paragon TV Rerack deliberately, down to the preset names, so
the two line up. There are nine presets and they always exist; an empty one
costs nothing and is simply never assigned to a day.

    Alpha
       Phase 1  maintenance          (empty)
       Phase 2  wake and tune        Curtain Up      07:00
       Phase 3  shut down            Wind Down       23:30
       Phase 5  wake and tune        Curtain Up      17:00

The point of the layer is the third and fourth lines: one sequence, written
once, used at several points in a day. A sequence knows how to do a thing; a
rerack decides when a day does it.

Where the times come from is per rerack. Its own, set here, and it works with
no Paragon TV at all. Or Paragon TV's, taken from the preset of the same name
-- which is why the names match, and why a rerack called Alpha lines up with
the television's Alpha without anything being said twice.

A weekly table says which rerack a day gets, exactly as Paragon TV's does.
"""

import sequences as sequence_lib

# Deliberately not reracks.json or rerack_state.json: those were what
# sequences were saved in before v2.14, and are still read once to carry
# older setups over. Reusing either name would have this file read those.
RERACK_FILE = 'rerack_presets.json'
RERACK_STATE_FILE = 'rerack_phase_state.json'

# The same nine names, in the same order, as Paragon TV. Matching by name is
# how a rerack takes its times from the television's preset, so the order and
# spelling are not ours to vary.
PRESET_NAMES = ('Alpha', 'Omega', 'Delta', 'Epsilon', 'Gamma', 'Sigma',
                'Omicron', 'Theta', 'Lambda')

PHASE_COUNT = 9

DAYS = sequence_lib.DAYS


def empty_phase():
    return {'time': '', 'sequence': ''}


def make_rerack(name, phases=None, follow_tv=False):
    return normalise({'name': name, 'phases': list(phases or []),
                      'follow_tv': follow_tv})


def normalise_phase(raw):
    """Clamp one phase. A phase with no sequence does nothing, whatever else
    it holds, so it is stored empty rather than half filled."""
    if not isinstance(raw, dict):
        return empty_phase()
    name = (raw.get('sequence') or '').strip()
    if not name:
        return empty_phase()
    return {'time': sequence_lib.parse_time(raw.get('time')),
            'sequence': name}


def normalise(raw):
    """Clamp one rerack, or None if it has no usable name."""
    if not isinstance(raw, dict):
        return None
    name = (raw.get('name') or '').strip()
    if not name:
        return None

    phases = [normalise_phase(entry) for entry in (raw.get('phases') or [])]
    phases = phases[:PHASE_COUNT]
    while len(phases) < PHASE_COUNT:
        phases.append(empty_phase())

    return {'name': name,
            'follow_tv': bool(raw.get('follow_tv')),
            'phases': phases}


def default_reracks():
    """All nine, empty. They always exist, as Paragon TV's presets do."""
    return [make_rerack(name) for name in PRESET_NAMES]


def normalise_all(raw):
    """The nine presets, in order, whatever the file happened to hold.

    Anything unrecognised is dropped and anything missing is added back
    empty, so a hand-edited or older file still opens on nine presets in the
    familiar order rather than on whatever survived.
    """
    found = {}
    for entry in raw or []:
        rerack = normalise(entry)
        if rerack is not None:
            found[rerack['name']] = rerack
    return [found.get(name) or make_rerack(name) for name in PRESET_NAMES]


def find(reracks, name):
    wanted = (name or '').strip().lower()
    for rerack in reracks or []:
        if rerack.get('name', '').lower() == wanted:
            return rerack
    return None


def clean_week(raw):
    """The weekly table: seven entries, each a rerack name or ''."""
    week = ['' for _ in range(7)]
    if isinstance(raw, dict):
        raw = [raw.get(day.lower(), '') for day in DAYS]
    if not isinstance(raw, list):
        return week
    for index, entry in enumerate(raw[:7]):
        name = (entry or '').strip()
        if name in PRESET_NAMES:
            week[index] = name
    return week


def todays_rerack(week, reracks, now):
    """The rerack a day gets, or None."""
    week = clean_week(week)
    return find(reracks, week[now.weekday()])


def filled_phases(rerack):
    """(number, phase) for the phases that hold a sequence."""
    return [(index + 1, phase)
            for index, phase in enumerate(rerack.get('phases') or [])
            if phase.get('sequence')]


def describe(rerack):
    """One line for a menu."""
    filled = filled_phases(rerack)
    if not filled:
        return 'empty'
    where = 'Paragon TV times' if rerack.get('follow_tv') else 'its own times'
    return '%d phase%s, %s' % (len(filled), '' if len(filled) == 1 else 's',
                               where)


def describe_phase_row(rerack, number, phase, tv_time=None):
    """A phase as it reads in the editor: what it does and when."""
    label = sequence_lib.describe_phase(number)
    if not phase.get('sequence'):
        return '%s  -  empty' % label.capitalize()

    if rerack.get('follow_tv'):
        when = tv_time or 'no Paragon TV time'
    else:
        when = phase.get('time') or 'no time set'
    return '%s  -  %s  (%s)' % (label.capitalize(), phase['sequence'], when)


def phase_time(rerack, number, tv_time=''):
    """When a phase runs, as 'HH:MM', or '' if it never does.

    A rerack following Paragon TV has no times of its own to fall back on: if
    the television has no time for that phase, the phase does not run. Falling
    back to a stale local time would fire it at an hour nobody set.
    """
    phases = rerack.get('phases') or []
    if not 1 <= number <= len(phases):
        return ''
    if rerack.get('follow_tv'):
        return (tv_time or '').strip()
    return phases[number - 1].get('time') or ''


def stamp(rerack, number, now, at_time):
    """The key that says this phase has already run today."""
    return '%s#%d %04d-%02d-%02d %s' % (rerack.get('name'), number,
                                        now.year, now.month, now.day, at_time)


def due_phases(rerack, now, state, tv_times=None):
    """The phases of this rerack whose time has come.

    Returns (number, sequence name, time, stamp) for each. The state is read
    rather than written here so the caller can decide when a phase counts as
    run -- which it does before running it, not after.
    """
    tv_times = tv_times or {}
    due = []
    for number, phase in filled_phases(rerack):
        at_time = phase_time(rerack, number, tv_times.get(number, ''))
        if not at_time:
            continue
        key = stamp(rerack, number, now, at_time)
        if key in state:
            continue
        if sequence_lib.due({'time': at_time, 'days': [now.weekday()]}, now,
                            schedule=(at_time, [now.weekday()])):
            due.append((number, phase['sequence'], at_time, key))
    return due


def used_by(reracks, sequence_name):
    """Where a sequence is used, so the reuse is visible from the sequence."""
    wanted = (sequence_name or '').strip().lower()
    places = []
    for rerack in reracks or []:
        for number, phase in filled_phases(rerack):
            if phase['sequence'].strip().lower() == wanted:
                places.append('%s phase %d' % (rerack['name'], number))
    return places
