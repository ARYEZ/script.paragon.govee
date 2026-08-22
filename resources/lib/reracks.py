# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Reracks: ten ordered steps, run as one.

Named after the Paragon TV preset macro system of the same name, and shaped
like it on purpose -- a fixed number of numbered slots rather than a list you
grow, so a rerack has the same shape every time you open it and slot 4 is
always slot 4. Empty slots are normal and cost nothing.

A step is three choices: what kind of thing, which one, and what to do to it.

    1. Scene       Warshade
    2. Tuya        Office Plug All outlets      On
    3. Broadlink   Bedroom Broadlink            TV power

Steps run in order, top to bottom. Each can hold a pause afterwards, which
matters more than it sounds: a television told to switch on and change channel
in the same breath will miss the second command, because it is still waking up.

A rerack is deliberately not a scene. A scene describes a state -- how the
lights should look -- and can be captured, mixed and cycled. A rerack
describes a sequence of things to do, including things with no state to
describe at all, like an infrared button press.
"""

import time as time_module

RERACK_FILE = 'reracks.json'

# Fixed, like the phases of the system this is named after. Ten is enough for
# anything anyone has wanted, and a fixed count is what lets the editor show
# every slot including the empty ones.
STEP_COUNT = 10

KIND_NONE = 'none'
KIND_SCENE = 'scene'
KIND_POWER = 'power'
KIND_COMMAND = 'command'

# Power actions a step can carry.
ACTION_ON = 'on'
ACTION_OFF = 'off'
ACTION_TOGGLE = 'toggle'
POWER_ACTIONS = (ACTION_ON, ACTION_OFF, ACTION_TOGGLE)

# A target of this stands for every enabled device of the step's driver,
# rather than one of them.
TARGET_ALL = '*'

MAX_PAUSE = 600


def empty_step():
    return {'kind': KIND_NONE, 'driver': '', 'target': '', 'action': '',
            'pause': 0}


def make_rerack(name, steps=None):
    """A rerack with its full complement of slots, however few are filled."""
    return normalise({'name': name, 'steps': list(steps or [])})


def _clean_int(value, low, high, default=0):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def normalise_step(raw):
    """Clamp one step, or return an empty slot if it is not usable.

    A step naming a kind it cannot carry out -- a power action with no target,
    a command with no name -- becomes empty rather than being kept and failing
    later. A half-filled slot that looks filled is worse than a blank one.
    """
    if not isinstance(raw, dict):
        return empty_step()

    kind = raw.get('kind')
    step = empty_step()
    step['pause'] = _clean_int(raw.get('pause'), 0, MAX_PAUSE)

    if kind == KIND_SCENE:
        name = (raw.get('target') or '').strip()
        if not name:
            return empty_step()
        step['kind'] = KIND_SCENE
        step['target'] = name
        return step

    if kind == KIND_POWER:
        action = (raw.get('action') or '').strip().lower()
        target = (raw.get('target') or '').strip()
        if action not in POWER_ACTIONS or not target:
            return empty_step()
        step['kind'] = KIND_POWER
        step['driver'] = (raw.get('driver') or '').strip()
        step['target'] = target
        step['action'] = action
        return step

    if kind == KIND_COMMAND:
        target = (raw.get('target') or '').strip()
        action = (raw.get('action') or '').strip()
        if not target or not action:
            return empty_step()
        step['kind'] = KIND_COMMAND
        step['driver'] = (raw.get('driver') or '').strip()
        step['target'] = target
        step['action'] = action
        return step

    return empty_step()


def normalise(raw):
    """Clamp one rerack, or None if it has no usable name.

    Always exactly STEP_COUNT slots: a file written by an older version, or
    edited by hand, is padded or trimmed rather than being rejected.
    """
    if not isinstance(raw, dict):
        return None
    name = (raw.get('name') or '').strip()
    if not name:
        return None

    steps = [normalise_step(entry) for entry in (raw.get('steps') or [])]
    steps = steps[:STEP_COUNT]
    while len(steps) < STEP_COUNT:
        steps.append(empty_step())

    return {'name': name,
            'description': (raw.get('description') or '').strip(),
            'steps': steps}


def normalise_all(raw):
    if not isinstance(raw, list):
        return []
    cleaned = []
    for entry in raw:
        rerack = normalise(entry)
        if rerack is not None:
            cleaned.append(rerack)
    return cleaned


def find(reracks, name):
    """Look a rerack up by name, ignoring case and surrounding space."""
    wanted = (name or '').strip().lower()
    for rerack in reracks or []:
        if rerack.get('name', '').lower() == wanted:
            return rerack
    return None


def filled_steps(rerack):
    """The steps that actually do something, in order."""
    return [step for step in rerack.get('steps') or []
            if step.get('kind') != KIND_NONE]


def describe(rerack):
    """One line for a menu: how many steps, and what the first one does."""
    steps = filled_steps(rerack)
    if not steps:
        return 'no steps yet'
    count = '%d step%s' % (len(steps), '' if len(steps) == 1 else 's')
    # Not lower-cased: the first step names a scene or a device, and those
    # are the user's own names to spell as they chose.
    return '%s, first: %s' % (count, describe_step(steps[0]))


def describe_step(step, device_name=None):
    """One line for a step, in the order it was chosen: kind, target, action."""
    kind = step.get('kind')
    if kind == KIND_NONE:
        return 'Empty'

    target = device_name or step.get('target') or ''
    if kind == KIND_SCENE:
        text = 'Scene: %s' % target
    elif kind == KIND_POWER:
        if step.get('target') == TARGET_ALL:
            target = 'all %s devices' % (step.get('driver') or 'listed')
        text = '%s: %s' % (target, step.get('action', '').title())
    elif kind == KIND_COMMAND:
        text = '%s: %s' % (target, step.get('action'))
    else:
        text = 'Empty'

    pause = step.get('pause') or 0
    if pause:
        text += '  (+%ds)' % pause
    return text


def resolve_targets(step, devices):
    """Which devices a step acts on. Empty means the step cannot run.

    Matched on device id first and name second, so renaming a device does not
    break a rerack that referred to it, and a rerack written by hand with a
    friendly name still works.
    """
    target = step.get('target') or ''
    driver = step.get('driver') or ''

    if target == TARGET_ALL:
        return [d for d in devices
                if d.enabled and (not driver or d.driver == driver)]

    wanted = target.strip().lower()
    matches = [d for d in devices if d.device_id.lower() == wanted]
    if not matches:
        matches = [d for d in devices if d.name.strip().lower() == wanted]
    return matches


def run(app, rerack, log_func=None, sleep_func=None, on_step=None):
    """Run one rerack top to bottom. Returns (steps done, [failures]).

    One step failing does not stop the rest. A rerack is a list of separate
    intentions -- lights, plugs, a television -- and a plug that has been
    unplugged is no reason to leave the rest of the room untouched. Every
    failure is collected and reported together at the end.
    """
    from devices import ControlError

    log = log_func or (lambda message: None)
    sleep = sleep_func or time_module.sleep
    done = 0
    errors = []

    steps = list(rerack.get('steps') or [])
    for index, step in enumerate(steps):
        if step.get('kind') == KIND_NONE:
            continue
        if on_step is not None and on_step(index, step) is False:
            log('Rerack "%s" cancelled at step %d'
                % (rerack.get('name'), index + 1))
            break

        try:
            _run_step(app, step)
            done += 1
        except ControlError as exc:
            errors.append('Step %d: %s' % (index + 1, exc))
            log('Rerack "%s" step %d failed: %s'
                % (rerack.get('name'), index + 1, exc))
        except Exception as exc:
            errors.append('Step %d: %s' % (index + 1, exc))
            log('Rerack "%s" step %d raised: %s'
                % (rerack.get('name'), index + 1, exc))

        pause = step.get('pause') or 0
        if pause:
            sleep(pause)

    log('Rerack "%s": %d step(s) done, %d failed'
        % (rerack.get('name'), done, len(errors)))
    return done, errors


def _run_step(app, step):
    """Carry out one step, raising ControlError if it cannot be done."""
    from devices import ControlError

    kind = step.get('kind')

    if kind == KIND_SCENE:
        if not app.apply_scene_by_name(step['target'], announce=False):
            raise ControlError('No scene called "%s"' % step['target'])
        return

    targets = resolve_targets(step, app.devices)
    if not targets:
        raise ControlError('Nothing matches "%s"' % step.get('target'))

    if kind == KIND_POWER:
        action = step.get('action')
        if action == ACTION_TOGGLE:
            _report(app.toggle_all(targets))
        else:
            _report(app.power_all(action == ACTION_ON, targets))
        return

    if kind == KIND_COMMAND:
        for device in targets:
            app.controller.send_command(device, step['action'])
        return

    raise ControlError('Unknown step type "%s"' % kind)


def _report(result):
    """Turn an (applied, errors) pair into a raise or a return."""
    from devices import ControlError

    applied, errors = result
    if not applied and errors:
        raise ControlError(errors[0])
    if not applied:
        raise ControlError('Nothing accepted the command')
