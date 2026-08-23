# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Script entry point.

With no arguments this opens the control panel. With arguments it performs a
single action and exits, which is what makes the add-on usable from a remote
button, a keymap or a favourite:

    RunScript(script.paragon.govee,action=toggle)
    RunScript(script.paragon.govee,action=scene,name=Movie Night)
    RunScript(script.paragon.govee,action=sequence,name=Bedtime)
    RunScript(script.paragon.govee,action=brightness,value=20)
    RunScript(script.paragon.govee,action=color,value=FF8800)
    RunScript(script.paragon.govee,action=color,value=Paragon Purple)
    RunScript(script.paragon.govee,action=temp,value=2700)
    RunScript(script.paragon.govee,action=off,target=Living Room Strip)

`target` accepts a device name or a Govee device id; leave it out to act on
every enabled light.
"""

import os
import sys

import xbmc
import xbmcaddon
import xbmcgui

_ADDON_PATH = xbmcaddon.Addon().getAddonInfo('path')
_LIB_PATH = os.path.join(_ADDON_PATH, 'resources', 'lib')
if _LIB_PATH not in sys.path:
    sys.path.insert(0, _LIB_PATH)


def parse_args(argv):
    """Turn Kodi's RunScript arguments into a dict.

    Kodi splits RunScript() parameters on commas, but the `key=value&key=value`
    convention is just as common in the wild, so both separators are accepted
    and a value is allowed to contain spaces (scene names do).
    """
    params = {}
    joined = '&'.join(part for part in argv if part)
    for chunk in joined.replace(',', '&').split('&'):
        chunk = chunk.strip()
        if not chunk or '=' not in chunk:
            continue
        key, _sep, value = chunk.partition('=')
        params[key.strip().lower()] = value.strip()
    return params


def resolve_targets(app, target):
    """Resolve a `target` argument to a device list, or None for 'all'."""
    if not target or target.lower() == 'all':
        return None
    wanted = target.strip().lower()
    matches = [d for d in app.enabled_devices
               if d.name.lower() == wanted or d.device_id.lower() == wanted]
    return matches or []


def _parse_int(value, low, high):
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return max(low, min(high, number))


def _parse_hex(value):
    """Parse a hex colour for action=color. Returns (r, g, b) or None.

    Shares the add-on's colour parser so a RunScript() binding accepts the
    same 8-digit codes the Govee app produces as the control panel does.
    """
    import scenes as scene_lib

    rgb, _note = scene_lib.parse_hex_color(value)
    return rgb


def run_action(app, params, utils):
    """Execute a single non-interactive action. Returns True if handled."""
    action = params.get('action', '').lower()
    if not action or action == 'panel':
        return False

    targets = resolve_targets(app, params.get('target'))
    if targets == []:
        utils.force_notify('No light called "%s"' % params.get('target'))
        return True

    def report(result, message):
        done, errors = result
        if done:
            utils.notify(message)
        elif errors:
            utils.force_notify(errors[0])
        else:
            utils.force_notify('No lights to control. Run a device refresh.')

    if action == 'on':
        report(app.power_all(True, targets), 'Lights on')
    elif action == 'off':
        report(app.power_all(False, targets), 'Lights off')
    elif action == 'toggle':
        report(app.toggle_all(targets), 'Lights toggled')
    elif action == 'brightness':
        value = _parse_int(params.get('value'), 1, 100)
        if value is None:
            utils.force_notify('brightness needs value=1-100')
        else:
            report(app.brightness_all(value, targets), 'Brightness %d%%' % value)
    elif action == 'color':
        value = params.get('value')
        rgb = _parse_hex(value)
        if rgb is None:
            # Fall back to a saved colour name, so a colour added to the
            # speed dial can be bound to a remote button by name rather than
            # having its hex copied into the keymap.
            entry = app.color_by_name(value)
            if entry:
                rgb = tuple(entry['color'])
        if rgb is None:
            utils.force_notify('color needs value=RRGGBB, AARRGGBB, or the '
                               'name of a saved colour')
        else:
            report(app.color_all(rgb, targets), 'Colour set')
    elif action == 'temp':
        value = _parse_int(params.get('value'), 1500, 12000)
        if value is None:
            utils.force_notify('temp needs value in Kelvin')
        else:
            report(app.color_temp_all(value, targets), '%dK' % value)
    elif action == 'scene':
        name = params.get('name') or params.get('value')
        if not name:
            utils.force_notify('scene needs name=<scene name>')
        else:
            app.apply_scene_by_name(name)
    elif action in ('sequence', 'rerack'):
        # "rerack" still works: it is what this was called until v2.14, and a
        # keymap or favourite holding one should not stop working over a
        # rename.
        name = params.get('name') or params.get('value')
        if not name:
            utils.force_notify('sequence needs name=<sequence name>')
        else:
            app.run_sequence_by_name(name)
    elif action == 'refresh':
        devices, warnings = app.refresh_devices()
        if warnings and not devices:
            utils.force_notify(warnings[0])
        else:
            utils.notify('Found %d light(s)' % len(devices))
    elif action == 'diagnose':
        import diagnostics
        text, _report = diagnostics.run(app)
        xbmcgui.Dialog().ok('%s - LAN diagnostics' % utils.ADDON_NAME,
                            text)
    elif action == 'verifystatus':
        import diagnostics
        chosen = targets or app.enabled_devices
        if not chosen:
            utils.force_notify('No lights to test. Run a device refresh.')
        else:
            # Not named `report`: that is the local result-reporting helper
            # above, and shadowing it here would be a trap for the next edit.
            outcome = diagnostics.verify_status(app, chosen[0])
            xbmcgui.Dialog().ok('%s - status check' % utils.ADDON_NAME,
                                diagnostics.verify_summary(outcome))
    elif action == 'pick_scene':
        # Fired by the "choose scene" buttons in the add-on settings.
        import gui
        setting_id = params.get('setting')
        if setting_id:
            gui.pick_scene_for_setting(app, setting_id)
    elif action == 'settings':
        utils.open_settings()
    else:
        utils.force_notify('Unknown action "%s"' % action)
    return True


def main():
    import addon_utils as utils

    params = parse_args(sys.argv[1:])
    utils.debug('Started with %s' % (params or 'no arguments'))

    try:
        from paragon_home import ParagonHome
        app = ParagonHome()
    except Exception as exc:
        utils.log('Failed to start: %s' % exc, xbmc.LOGERROR)
        xbmcgui.Dialog().ok(utils.ADDON_NAME,
                            'Could not start the add-on:\n\n%s' % exc)
        return

    try:
        if run_action(app, params, utils):
            return
        import gui
        gui.ControlPanel(app).run()
    except Exception as exc:
        utils.log('Unhandled error: %s' % exc, xbmc.LOGERROR)
        import traceback
        utils.log(traceback.format_exc(), xbmc.LOGERROR)
        xbmcgui.Dialog().ok(utils.ADDON_NAME,
                            'Something went wrong:\n\n%s' % exc)


if __name__ == '__main__':
    main()
