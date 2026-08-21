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

# How far apart the RGB channels must be before a reading counts as a real
# colour rather than a shade of white. Govee bulbs report white either as
# 0,0,0 or as an equal-channel value alongside a colour temperature.
GREY_TOLERANCE = 12


def parse_hex_color(text):
    """Parse a hex colour string. Returns ((r, g, b), note) or (None, reason).

    Accepts 3, 6 and 8 digit forms. The Govee app shows 8-digit codes, which
    carry an alpha byte the LAN protocol has no use for.

    Govee emits AARRGGBB with the alpha always FF. That is confirmed from real
    codes out of the app -- FF3C447F and FF7F3C3C -- where the alternative
    RRGGBBAA reading would mean alphas of 7F and 3C, i.e. bulbs at 50% and 23%
    transparency, which is meaningless for a light. Read as AARRGGBB they are
    a deep slate blue and a deep brick red, which is what the app showed.

    Other sources are not consistent, though: Android writes AARRGGBB and CSS
    Color 4 writes RRGGBBAA, so the end is inferred rather than hardcoded. An
    alpha in a colour picker is almost always FF, so whichever end reads FF is
    taken as the alpha; when both ends are FF or neither is, the two readings
    are indistinguishable and alpha-first wins, which is also the right answer
    for Govee. `note` always says which reading was used, so a wrong inference
    shows up in the dialog rather than only on the wall.
    """
    if not text:
        return None, 'No colour entered'

    cleaned = text.strip().lstrip('#').replace(' ', '')
    for char in cleaned:
        if char not in '0123456789abcdefABCDEF':
            return None, 'That is not a hex colour'

    if len(cleaned) == 3:
        cleaned = ''.join(char * 2 for char in cleaned)
        note = ''
    elif len(cleaned) == 6:
        note = ''
    elif len(cleaned) == 8:
        head, tail = cleaned[0:2].upper(), cleaned[6:8].upper()
        if head == 'FF' and tail != 'FF':
            cleaned, note = cleaned[2:], 'read as AARRGGBB'
        elif tail == 'FF' and head != 'FF':
            cleaned, note = cleaned[:6], 'read as RRGGBBAA'
        else:
            cleaned, note = cleaned[2:], 'read as AARRGGBB (ambiguous)'
    else:
        return None, 'Enter 6 or 8 hex digits, e.g. FF8800 or FFFF8800'

    try:
        rgb = (int(cleaned[0:2], 16), int(cleaned[2:4], 16),
               int(cleaned[4:6], 16))
    except ValueError:
        return None, 'That is not a hex colour'
    return rgb, note


def make_scene(name, power=POWER_ON, brightness=None, mode=MODE_NONE,
               color=None, kelvin=None, targets=None, devices=None):
    return {
        'name': name,
        'power': power,
        'brightness': brightness,
        'mode': mode,
        'color': list(color) if color else [255, 255, 255],
        'kelvin': kelvin or 2700,
        'targets': list(targets or []),
        'devices': dict(devices or {}),
    }


def _normalise_settings(raw, fallback_power=POWER_ON):
    """Clamp one set of light settings. Shared by scenes and per-device entries."""
    if not isinstance(raw, dict):
        return None

    power = raw.get('power', fallback_power)
    if power not in (POWER_ON, POWER_OFF, POWER_KEEP):
        power = fallback_power

    brightness = raw.get('brightness')
    if brightness is not None:
        try:
            brightness = max(1, min(100, int(brightness)))
        except (TypeError, ValueError):
            brightness = None

    mode = raw.get('mode', MODE_NONE)
    if mode not in (MODE_COLOR, MODE_TEMP, MODE_NONE):
        mode = MODE_NONE

    color = raw.get('color') or [255, 255, 255]
    try:
        color = [max(0, min(255, int(c))) for c in color][:3]
    except (TypeError, ValueError):
        color = [255, 255, 255]
    while len(color) < 3:
        color.append(255)

    try:
        kelvin = int(raw.get('kelvin') or 2700)
    except (TypeError, ValueError):
        kelvin = 2700
    kelvin = max(1500, min(12000, kelvin))

    return {'power': power, 'brightness': brightness, 'mode': mode,
            'color': color, 'kelvin': kelvin}


def settings_for(scene, device_id):
    """The settings a scene applies to one device.

    A captured scene stores a per-device entry; everything else falls back to
    the scene's own uniform values. This is what lets one scene hold 25
    different bulb states without every other scene growing a device map.
    """
    per_device = (scene.get('devices') or {}).get((device_id or '').upper())
    if per_device:
        return per_device
    return {
        'power': scene.get('power', POWER_ON),
        'brightness': scene.get('brightness'),
        'mode': scene.get('mode', MODE_NONE),
        'color': scene.get('color') or [255, 255, 255],
        'kelvin': scene.get('kelvin', 2700),
    }


def detect_brightness_scale(states):
    """Work out which brightness scale a set of readings uses.

    Govee documents brightness as 0-100, but some models report the raw 0-254
    register. A single reading of 51 is ambiguous -- it could be 51% or 20% --
    so the scale cannot be inferred per bulb. Across a whole capture it can:
    one reading above 100 proves the wider scale, and every bulb in a capture
    is answering the same way.

    Returns 100 or 254. When every bulb happens to read at or below 100 on a
    254-scale model the two are indistinguishable and this returns 100; the
    captured values are then self-consistently low rather than wrong in a way
    that changes which bulb is brighter than which.
    """
    for state in (states or {}).values():
        if not state:
            continue
        value = state.get('brightness')
        try:
            if value is not None and int(value) > 100:
                return 254
        except (TypeError, ValueError):
            continue
    return 100


def state_to_settings(state, brightness_scale=100):
    """Turn a device state reading into scene settings, or None if unusable.

    Which appearance mode a bulb is in has to be inferred: Govee reports a
    colour temperature of 0 when the bulb is showing RGB, and reports RGB of
    0,0,0 when it is showing white, so whichever one is non-zero is the live
    one.

    `brightness_scale` comes from detect_brightness_scale() over the whole
    capture; see there for why it cannot be decided from one reading.
    """
    if not state:
        return None

    power = state.get('power')
    if power not in ('on', 'off'):
        return None
    if power == 'off':
        return {'power': POWER_OFF, 'brightness': None, 'mode': MODE_NONE,
                'color': [255, 255, 255], 'kelvin': 2700}

    settings = {'power': POWER_ON, 'brightness': None, 'mode': MODE_NONE,
                'color': [255, 255, 255], 'kelvin': 2700}

    brightness = state.get('brightness')
    if brightness is not None:
        try:
            brightness = int(brightness)
        except (TypeError, ValueError):
            brightness = None
    if brightness is not None:
        if brightness_scale and brightness_scale != 100:
            brightness = int(round(brightness * 100.0 / brightness_scale))
        settings['brightness'] = max(1, min(100, brightness))

    try:
        kelvin = int(state.get('colorTem') or 0)
    except (TypeError, ValueError):
        kelvin = 0

    rgb = None
    color = state.get('color')
    if isinstance(color, dict):
        try:
            rgb = [max(0, min(255, int(color.get(k) or 0)))
                   for k in ('r', 'g', 'b')]
        except (TypeError, ValueError):
            rgb = None

    # Which mode a bulb is in has to be inferred, and the two fields can
    # disagree. A bulb showing white reports either 0,0,0 or the white
    # equivalent next to its temperature; a bulb showing an actual colour
    # reports that colour. So a *tinted* RGB is the live value even when a
    # stale temperature sits beside it -- taking kelvin first would capture a
    # pink bulb as white. A grey or zero RGB carries no colour information,
    # so there the temperature wins.
    lit = bool(rgb) and any(rgb)
    tinted = lit and (max(rgb) - min(rgb)) > GREY_TOLERANCE

    if tinted:
        settings['mode'] = MODE_COLOR
        settings['color'] = rgb
    elif kelvin > 0:
        settings['mode'] = MODE_TEMP
        settings['kelvin'] = max(1500, min(12000, kelvin))
    elif lit:
        settings['mode'] = MODE_COLOR
        settings['color'] = rgb

    return settings


def capture_scene(name, devices, states):
    """Build a scene from what the lights are doing right now.

    Returns (scene, captured_count, skipped_names). This is how a Govee
    Tap-to-Run gets into Kodi: run it in the Govee app, then snapshot the
    result here. Replaying the snapshot is pure LAN, so it needs no account
    credentials and no cloud round-trip.
    """
    per_device = {}
    skipped = []
    scale = detect_brightness_scale(states)

    for device in devices:
        settings = state_to_settings(states.get(device.device_id),
                                     brightness_scale=scale)
        if settings is None:
            skipped.append(device.name)
            continue
        per_device[device.device_id] = settings

    scene = make_scene(name, targets=sorted(per_device.keys()),
                       devices=per_device)
    return scene, len(per_device), skipped


def apply_settings(controller, device, settings):
    """Drive one device to one settings dict. Raises ControlError on failure.

    Shared by scene application, the status round-trip's restore step, and the
    naming walkthrough's highlight-and-put-back, so the ordering rules live in
    one place rather than being re-derived at each call site.
    """
    if settings['power'] == POWER_OFF:
        controller.turn(device, False)
        return

    if settings['power'] == POWER_ON:
        controller.turn(device, True)

    # Brightness before colour: on several Govee models a colour command
    # re-asserts the previous brightness, so setting colour last keeps the
    # two from fighting.
    if settings['brightness'] is not None and device.supports_cmd('brightness'):
        controller.set_brightness(device, settings['brightness'])

    if settings['mode'] == MODE_COLOR and device.supports_cmd('color'):
        controller.set_color(device, *settings['color'])
    elif settings['mode'] == MODE_TEMP and device.supports_cmd('colorTem'):
        controller.set_color_temp(device, settings['kelvin'])


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

    settings = _normalise_settings(scene) or _normalise_settings({})

    targets = scene.get('targets') or []
    if not isinstance(targets, list):
        targets = []
    targets = [str(t).upper() for t in targets if t]

    per_device = {}
    raw_devices = scene.get('devices')
    if isinstance(raw_devices, dict):
        for device_id, entry in raw_devices.items():
            cleaned = _normalise_settings(entry)
            if cleaned is not None and device_id:
                per_device[str(device_id).upper()] = cleaned

    return {
        'name': name,
        'power': settings['power'],
        'brightness': settings['brightness'],
        'mode': settings['mode'],
        'color': settings['color'],
        'kelvin': settings['kelvin'],
        'targets': targets,
        'devices': per_device,
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
    per_device = scene.get('devices') or {}
    if per_device:
        # A captured scene has no single brightness or colour to report.
        lit = len([s for s in per_device.values()
                   if s.get('power') != POWER_OFF])
        return 'captured, %d light(s), %d on' % (len(per_device), lit)
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
        # A captured scene carries this device's own recorded settings; every
        # other scene falls back to its single uniform set.
        settings = settings_for(scene, device.device_id)
        try:
            apply_settings(controller, device, settings)
            applied += 1
        except ControlError as exc:
            log('Scene "%s" failed on %s: %s' % (scene['name'], device.name, exc))
            errors.append(str(exc))

    return applied, errors
