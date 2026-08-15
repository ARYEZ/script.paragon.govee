# -*- coding: utf-8 -*-
"""
Paragon Govee
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Named lighting presets.

A scene is a plain dict so it round-trips through JSON without a schema layer.
It describes what to do to a set of devices, not what state they must end in,
which is what lets a single scene ("Movie Night") drive a mixed set of lights
whose capabilities differ.

Fields:
    name        display name, and the key used by RunScript() and settings
    power       'on', 'off' or 'keep' (leave the current power state alone)
    brightness  1-100, or None to leave brightness untouched
    mode        'color', 'temp' or 'none'
    color       [r, g, b], used when mode == 'color'
    kelvin      colour temperature, used when mode == 'temp'
    targets     list of device ids; empty means every enabled device
"""

POWER_ON = 'on'
POWER_OFF = 'off'
POWER_KEEP = 'keep'

MODE_COLOR = 'color'
MODE_TEMP = 'temp'
MODE_NONE = 'none'

SCENE_FILE = 'scenes.json'


def make_scene(name, power=POWER_ON, brightness=None, mode=MODE_NONE,
               color=None, kelvin=None, targets=None):
    return {
        'name': name,
        'power': power,
        'brightness': brightness,
        'mode': mode,
        'color': list(color) if color else [255, 255, 255],
        'kelvin': kelvin or 2700,
        'targets': list(targets or []),
    }


def default_scenes():
    """The starter set, written on first run and editable afterwards."""
    return [
        make_scene('Movie Night', power=POWER_ON, brightness=8,
                   mode=MODE_TEMP, kelvin=2000),
        make_scene('Paused', power=POWER_ON, brightness=35,
                   mode=MODE_TEMP, kelvin=2400),
        make_scene('Lights Up', power=POWER_ON, brightness=100,
                   mode=MODE_TEMP, kelvin=4000),
        make_scene('Warm Evening', power=POWER_ON, brightness=55,
                   mode=MODE_TEMP, kelvin=2700),
        make_scene('Paragon Purple', power=POWER_ON, brightness=60,
                   mode=MODE_COLOR, color=[150, 60, 220]),
        make_scene('All Off', power=POWER_OFF),
    ]


def normalise(scene):
    """Coerce a loaded scene into the expected shape and value ranges.

    Scenes are user-editable JSON, so anything read back off disk is treated
    as untrusted: a hand-edited file should degrade to sane values rather than
    throw somewhere deep in a playback callback.
    """
    if not isinstance(scene, dict):
        return None
    name = scene.get('name')
    # `str` is not enough of a check on Python 2, where a name loaded from
    # JSON comes back as `unicode`; duck-typing on strip() covers both.
    if not name or not hasattr(name, 'strip'):
        return None
    name = name.strip()
    if not name:
        return None

    power = scene.get('power', POWER_ON)
    if power not in (POWER_ON, POWER_OFF, POWER_KEEP):
        power = POWER_ON

    brightness = scene.get('brightness')
    if brightness is not None:
        try:
            brightness = max(1, min(100, int(brightness)))
        except (TypeError, ValueError):
            brightness = None

    mode = scene.get('mode', MODE_NONE)
    if mode not in (MODE_COLOR, MODE_TEMP, MODE_NONE):
        mode = MODE_NONE

    color = scene.get('color') or [255, 255, 255]
    try:
        color = [max(0, min(255, int(c))) for c in color][:3]
    except (TypeError, ValueError):
        color = [255, 255, 255]
    while len(color) < 3:
        color.append(255)

    try:
        kelvin = int(scene.get('kelvin') or 2700)
    except (TypeError, ValueError):
        kelvin = 2700
    kelvin = max(1500, min(12000, kelvin))

    targets = scene.get('targets') or []
    if not isinstance(targets, list):
        targets = []
    targets = [str(t).upper() for t in targets if t]

    return {
        'name': name,
        'power': power,
        'brightness': brightness,
        'mode': mode,
        'color': color,
        'kelvin': kelvin,
        'targets': targets,
    }


def normalise_all(raw):
    """Normalise a loaded list, dropping entries that cannot be salvaged."""
    if not isinstance(raw, list):
        return []
    cleaned = []
    seen = set()
    for entry in raw:
        scene = normalise(entry)
        if scene is None:
            continue
        key = scene['name'].lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(scene)
    return cleaned


def find(scenes, name):
    """Look a scene up by name, case-insensitively. Returns None if absent."""
    if not name:
        return None
    wanted = name.strip().lower()
    for scene in scenes or []:
        if scene.get('name', '').strip().lower() == wanted:
            return scene
    return None


def describe(scene):
    """One-line summary for list rows, e.g. '35%, 2400K'."""
    bits = []
    if scene.get('power') == POWER_OFF:
        return 'Off'
    if scene.get('power') == POWER_KEEP:
        bits.append('keep power')
    if scene.get('brightness') is not None:
        bits.append('%d%%' % scene['brightness'])
    if scene.get('mode') == MODE_COLOR:
        color = scene.get('color') or [255, 255, 255]
        bits.append('RGB %d,%d,%d' % tuple(color[:3]))
    elif scene.get('mode') == MODE_TEMP:
        bits.append('%dK' % scene.get('kelvin', 2700))
    targets = scene.get('targets') or []
    bits.append('%d light(s)' % len(targets) if targets else 'all lights')
    return ', '.join(bits)


def scene_targets(scene, devices):
    """Resolve a scene's target list against the known devices."""
    enabled = [d for d in devices if d.enabled]
    targets = scene.get('targets') or []
    if not targets:
        return enabled
    wanted = set(targets)
    return [d for d in enabled if d.device_id in wanted]


def apply_scene(controller, scene, devices, log_func=None):
    """Apply `scene` and return (applied_count, [error strings]).

    Every device is attempted even if an earlier one failed, so one
    unreachable light does not leave the rest of the room untouched.
    """
    from devices import ControlError  # local import keeps this module standalone

    log = log_func or (lambda message: None)
    scene = normalise(scene)
    if scene is None:
        return 0, ['That scene is not valid']

    targets = scene_targets(scene, devices)
    if not targets:
        return 0, ['No lights matched that scene']

    applied = 0
    errors = []
    for device in targets:
        try:
            if scene['power'] == POWER_OFF:
                controller.turn(device, False)
                applied += 1
                continue

            if scene['power'] == POWER_ON:
                controller.turn(device, True)

            # Brightness before colour: on several Govee models a colour
            # command re-asserts the previous brightness, so setting colour
            # last keeps the two from fighting.
            if scene['brightness'] is not None \
                    and device.supports_cmd('brightness'):
                controller.set_brightness(device, scene['brightness'])

            if scene['mode'] == MODE_COLOR and device.supports_cmd('color'):
                controller.set_color(device, *scene['color'])
            elif scene['mode'] == MODE_TEMP and device.supports_cmd('colorTem'):
                controller.set_color_temp(device, scene['kelvin'])

            applied += 1
        except ControlError as exc:
            log('Scene "%s" failed on %s: %s' % (scene['name'], device.name, exc))
            errors.append(str(exc))

    return applied, errors
