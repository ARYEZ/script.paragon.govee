# -*- coding: utf-8 -*-
"""
Paragon Govee
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The interactive control panel.

Everything is built from the stock Kodi dialogs rather than a custom window.
On Krypton a custom skin file has to be maintained per resolution and per skin,
and none of that buys anything for what is essentially a list of lights and a
handful of values -- dialogs work identically under Estuary, skin.paragon and
whatever else the user has installed.
"""

import xbmcgui

import addon_utils as utils
import scenes as scene_lib
from devices import ControlError

# Presets offered before the user has to type anything.
BRIGHTNESS_STEPS = [5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]

COLOR_PRESETS = [
    ('Warm White', (255, 180, 107)),
    ('Cool White', (255, 255, 255)),
    ('Paragon Purple', (150, 60, 220)),
    ('Deep Red', (255, 0, 0)),
    ('Amber', (255, 120, 0)),
    ('Lime', (120, 255, 60)),
    ('Teal', (0, 200, 180)),
    ('Ocean Blue', (0, 80, 255)),
    ('Magenta', (255, 0, 150)),
]

TEMP_PRESETS = [
    ('Candle - 2000K', 2000),
    ('Warm - 2700K', 2700),
    ('Soft - 3000K', 3000),
    ('Neutral - 4000K', 4000),
    ('Cool - 5000K', 5000),
    ('Daylight - 6500K', 6500),
]

BACK = -1

# What a light is set to while the naming walkthrough is asking about it.
# Full brightness magenta reads clearly against normal room lighting and
# against the warm whites these bulbs usually sit at.
HIGHLIGHT_COLOR = (255, 0, 255)
HIGHLIGHT_BRIGHTNESS = 100


def _dialog():
    return xbmcgui.Dialog()


def _select(heading, options):
    """Wrapper around Dialog().select that returns BACK when cancelled."""
    if not options:
        return BACK
    return _dialog().select(heading, options)


def _report(result, success_message):
    """Turn a (applied, errors) result pair into one user-facing message."""
    done, errors = result
    if done and not errors:
        utils.notify(success_message)
    elif done and errors:
        utils.notify('%s (%d light(s) failed)' % (success_message, len(errors)))
    elif errors:
        utils.force_notify(errors[0])
    else:
        utils.force_notify('No lights to control. Run a device refresh.')


class ControlPanel(object):
    """Drives the nested dialog menus."""

    def __init__(self, app):
        self.app = app

    # -- entry point -------------------------------------------------------

    def run(self):
        if not self.app.devices:
            if self._first_run():
                return
        while True:
            if self.main_menu() == BACK:
                return

    def _first_run(self):
        """Offer a discovery pass when the cache is empty. True = give up."""
        prompt = ('No Govee lights are known yet.\n\n'
                  'Search the network for them now?')
        if not _dialog().yesno('Paragon Govee', prompt):
            return True
        self.refresh_devices()
        return not self.app.devices

    # -- main menu ---------------------------------------------------------

    def main_menu(self):
        """Top-level menu, built as (label, handler) rows.

        The version is in the heading on purpose: the add-on is normally
        installed as a git clone and updated with `git pull`, and this is the
        quickest way to confirm which version is actually running.
        """
        devices = self.app.enabled_devices
        rows = []

        if devices:
            rows.append(('All Lights (%d)' % len(devices),
                         lambda: self.control_menu(None, 'All Lights')))

        for device in devices:
            label = device.name
            transports = device.transports()
            if transports:
                label = '%s  [%s]' % (device.name,
                                      '+'.join(t.upper() for t in transports))
            # Bind the device to this row rather than closing over the loop
            # variable, which would otherwise leave every row pointing at the
            # last light.
            rows.append((label,
                         lambda d=device: self.control_menu([d], d.name)))

        rows.extend([
            ('Scenes...', self.scene_menu),
            ('Capture lights as a scene...', self.capture_scene),
            ('Refresh devices', self.refresh_devices),
            ('Manage devices...', self.manage_devices),
            ('Diagnose LAN search...', self.diagnose),
            ('Settings', utils.open_settings),
        ])

        choice = _select('Paragon Govee %s' % utils.ADDON_VERSION,
                         [label for label, _handler in rows])
        if choice == BACK:
            return BACK
        rows[choice][1]()
        return None

    def diagnose(self):
        """Probe the LAN and explain the result on screen and in the log."""
        import diagnostics

        progress = xbmcgui.DialogProgressBG()
        progress.create('Paragon Govee', 'Probing the network...')
        try:
            text, _report = diagnostics.run(self.app)
        except Exception as exc:
            utils.log('Diagnostics failed: %s' % exc)
            progress.close()
            _dialog().ok('Paragon Govee', 'Diagnostics failed:\n\n%s' % exc)
            return
        progress.close()
        _dialog().ok('Paragon Govee - LAN diagnostics', text)

    def verify_status(self, device):
        """Drive one bulb to a known colour and see if it reports it back."""
        import diagnostics

        if not _dialog().yesno(
                'Paragon Govee',
                'This will briefly set %s to a test colour and then put it '
                'back, to check whether it reports its state honestly.\n\n'
                'Continue?' % device.name):
            return

        progress = xbmcgui.DialogProgressBG()
        progress.create('Paragon Govee', 'Testing %s...' % device.name)
        try:
            report = diagnostics.verify_status(self.app, device)
        except Exception as exc:
            utils.log('Status round-trip failed: %s' % exc)
            progress.close()
            _dialog().ok('Paragon Govee', 'Test failed:\n\n%s' % exc)
            return
        progress.close()
        _dialog().ok('Paragon Govee - status check',
                     diagnostics.verify_summary(report))

    # -- control ------------------------------------------------------------

    def control_menu(self, targets, heading):
        """Power/brightness/colour menu for one device or the whole group.

        Rows are (label, handler) pairs rather than a list indexed against a
        chain of elifs, so inserting a row cannot silently rewire the ones
        below it.
        """
        while True:
            rows = [
                ('Toggle',
                 lambda: _report(self.app.toggle_all(targets),
                                 'Toggled %s' % heading)),
                ('On',
                 lambda: _report(self.app.power_all(True, targets),
                                 '%s on' % heading)),
                ('Off',
                 lambda: _report(self.app.power_all(False, targets),
                                 '%s off' % heading)),
                ('Brightness...',
                 lambda: self.brightness_menu(targets, heading)),
                ('Colour...', lambda: self.color_menu(targets, heading)),
                ('Colour temperature...',
                 lambda: self.temp_menu(targets, heading)),
            ]
            if targets and len(targets) == 1:
                rows.append(('Show status',
                             lambda: self.show_status(targets[0])))
                rows.append(('Check status reporting...',
                             lambda: self.verify_status(targets[0])))

            choice = _select(heading, [label for label, _handler in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def brightness_menu(self, targets, heading):
        options = ['%d%%' % step for step in BRIGHTNESS_STEPS]
        options.append('Custom...')
        choice = _select('%s - brightness' % heading, options)
        if choice == BACK:
            return

        if choice == len(BRIGHTNESS_STEPS):
            value = self._ask_number('Brightness (1-100)', '50')
            if value is None:
                return
            percent = max(1, min(100, value))
        else:
            percent = BRIGHTNESS_STEPS[choice]

        _report(self.app.brightness_all(percent, targets),
                '%s at %d%%' % (heading, percent))

    def color_menu(self, targets, heading):
        options = [name for name, _rgb in COLOR_PRESETS]
        options.append('Custom hex...')
        choice = _select('%s - colour' % heading, options)
        if choice == BACK:
            return

        if choice == len(COLOR_PRESETS):
            entered = self._ask_hex()
            if entered is None:
                return
            rgb, label = entered
        else:
            label, rgb = COLOR_PRESETS[choice]

        _report(self.app.color_all(rgb, targets),
                '%s set to %s' % (heading, label))

    def temp_menu(self, targets, heading):
        options = [name for name, _k in TEMP_PRESETS]
        options.append('Custom...')
        choice = _select('%s - colour temperature' % heading, options)
        if choice == BACK:
            return

        if choice == len(TEMP_PRESETS):
            value = self._ask_number('Colour temperature in Kelvin', '2700')
            if value is None:
                return
            kelvin = max(1500, min(12000, value))
        else:
            kelvin = TEMP_PRESETS[choice][1]

        _report(self.app.color_temp_all(kelvin, targets),
                '%s at %dK' % (heading, kelvin))

    def show_status(self, device):
        state = self.app.controller.get_state(device)
        if not state:
            _dialog().ok('Paragon Govee',
                         'Could not read the state of %s.\n\n'
                         'LAN status needs UDP port 4002, which another '
                         'program may be holding.' % device.name)
            return

        lines = ['Power: %s' % state.get('power', 'unknown')]
        if state.get('brightness') is not None:
            lines.append('Brightness: %s%%' % state.get('brightness'))
        color = state.get('color')
        if isinstance(color, dict) and any(color.get(k) for k in 'rgb'):
            lines.append('Colour: RGB %s, %s, %s'
                         % (color.get('r', 0), color.get('g', 0),
                            color.get('b', 0)))
        if state.get('colorTem'):
            lines.append('Temperature: %sK' % state.get('colorTem'))
        lines.append('Read over: %s' % str(state.get('source', '?')).upper())
        if device.ip:
            lines.append('Address: %s' % device.ip)

        _dialog().ok(device.name, '\n'.join(lines))

    # -- scenes -------------------------------------------------------------

    def scene_menu(self):
        while True:
            scenes = self.app.scenes
            options = ['%s  -  %s' % (s['name'], scene_lib.describe(s))
                       for s in scenes]
            options.append('Capture lights as a new scene...')
            options.append('Manage scenes...')

            choice = _select('Scenes', options)
            if choice == BACK:
                return
            if choice == len(scenes):
                self.capture_scene()
                continue
            if choice == len(scenes) + 1:
                self.manage_scenes()
                continue
            self.app.apply_scene(scenes[choice])

    def capture_scene(self):
        """Snapshot the lights' current state into a scene.

        This is the route for Govee Tap-to-Run scenes: run one in the Govee
        app, then capture the result here. The saved copy replays over the
        LAN, so it needs no Govee account credentials and no cloud call.
        """
        if not self.app.enabled_devices:
            utils.force_notify('No lights to capture. Run a device refresh.')
            return

        name = _dialog().input('Name for the captured scene', '')
        if not name or not name.strip():
            return
        name = name.strip()

        existing = self.app.scene_by_name(name)
        if existing is not None and not _dialog().yesno(
                'Paragon Govee',
                'A scene called "%s" already exists.\n\nReplace it with what '
                'the lights are doing now?' % name):
            return

        progress = xbmcgui.DialogProgressBG()
        progress.create('Paragon Govee', 'Reading the lights...')
        try:
            scene, captured, skipped = self.app.capture_scene(name)
        except Exception as exc:
            utils.log('Capture failed: %s' % exc)
            progress.close()
            _dialog().ok('Paragon Govee', 'Capture failed:\n\n%s' % exc)
            return
        progress.close()

        if not captured:
            _dialog().ok('Paragon Govee',
                         'None of the lights reported their state.\n\n'
                         'Status replies need UDP port %d -- close the Govee '
                         'Desktop app and try again.' % 4002)
            return

        if self.app.save_scene(scene) is None:
            utils.force_notify('That scene could not be saved')
            return

        message = 'Captured "%s" from %d light(s).' % (name, captured)
        summary = self.app.summarise_capture(scene)
        if summary:
            message += '\n\n' + summary
        if skipped:
            message += ('\n\n%d did not answer and were left out:\n%s'
                        % (len(skipped), ', '.join(skipped[:6])))
        # The LAN protocol has no scene concept, so a bulb driven into a
        # Govee app scene keeps reporting whatever was last set locally.
        # Saying so here is the difference between the user trusting a wrong
        # capture and knowing to set the look from Kodi first.
        message += ('\n\nIf that does not match what you see, the bulbs '
                    'reported a stale state: Govee app scenes are invisible '
                    'over the LAN. Set the look from Kodi first, then '
                    'capture.')
        # Always shown rather than a toast: this is the moment to notice that
        # every bulb read back as white, or every brightness as 100%.
        _dialog().ok('Paragon Govee', message)

    def manage_scenes(self):
        while True:
            scenes = self.app.scenes
            options = [s['name'] for s in scenes]
            options.append('Add a new scene...')

            choice = _select('Manage scenes', options)
            if choice == BACK:
                return
            if choice == len(scenes):
                self.edit_scene(None)
            else:
                self.edit_scene(choice)

    def edit_scene(self, index):
        """Edit an existing scene by index, or create one when index is None."""
        scenes = self.app.scenes
        if index is None:
            scene = scene_lib.make_scene('New scene')
        else:
            scene = dict(scenes[index])

        while True:
            brightness = ('leave alone' if scene['brightness'] is None
                          else '%d%%' % scene['brightness'])
            if scene['mode'] == scene_lib.MODE_COLOR:
                appearance = 'RGB %d, %d, %d' % tuple(scene['color'][:3])
            elif scene['mode'] == scene_lib.MODE_TEMP:
                appearance = '%dK' % scene['kelvin']
            else:
                appearance = 'leave alone'
            targets = scene['targets']
            target_label = ('all lights' if not targets
                            else '%d selected' % len(targets))

            options = [
                'Name: %s' % scene['name'],
                'Power: %s' % scene['power'],
                'Brightness: %s' % brightness,
                'Appearance: %s' % appearance,
                'Lights: %s' % target_label,
                'Test this scene',
                'Save',
            ]
            if index is not None:
                options.append('Delete')

            choice = _select('Edit scene', options)
            if choice == BACK:
                return

            if choice == 0:
                name = _dialog().input('Scene name', scene['name'])
                if name and name.strip():
                    scene['name'] = name.strip()
            elif choice == 1:
                pick = _select('Power', ['Turn on', 'Turn off',
                                         'Leave as it is'])
                if pick != BACK:
                    scene['power'] = [scene_lib.POWER_ON, scene_lib.POWER_OFF,
                                      scene_lib.POWER_KEEP][pick]
            elif choice == 2:
                self._edit_brightness(scene)
            elif choice == 3:
                self._edit_appearance(scene)
            elif choice == 4:
                self._edit_targets(scene)
            elif choice == 5:
                self.app.apply_scene(scene)
            elif choice == 6:
                self._save_scene(scene, index)
                return
            elif choice == 7:
                if _dialog().yesno('Paragon Govee',
                                   'Delete the scene "%s"?' % scene['name']):
                    del scenes[index]
                    self.app.save_scenes()
                    utils.notify('Scene deleted')
                return

    def _save_scene(self, scene, index):
        cleaned = scene_lib.normalise(scene)
        if cleaned is None:
            utils.force_notify('That scene could not be saved')
            return
        scenes = self.app.scenes
        clash = scene_lib.find(scenes, cleaned['name'])
        if clash is not None and (index is None or scenes[index] is not clash):
            if not _dialog().yesno(
                    'Paragon Govee',
                    'A scene called "%s" already exists.\n\nReplace it?'
                    % cleaned['name']):
                return
            scenes.remove(clash)
            if index is not None and index >= len(scenes):
                index = None

        if index is None:
            scenes.append(cleaned)
        else:
            scenes[index] = cleaned
        self.app.save_scenes()
        utils.notify('Scene "%s" saved' % cleaned['name'])

    def _edit_brightness(self, scene):
        options = ['Leave brightness alone']
        options += ['%d%%' % step for step in BRIGHTNESS_STEPS]
        options.append('Custom...')
        choice = _select('Scene brightness', options)
        if choice == BACK:
            return
        if choice == 0:
            scene['brightness'] = None
        elif choice == len(options) - 1:
            value = self._ask_number('Brightness (1-100)', '50')
            if value is not None:
                scene['brightness'] = max(1, min(100, value))
        else:
            scene['brightness'] = BRIGHTNESS_STEPS[choice - 1]

    def _edit_appearance(self, scene):
        choice = _select('Scene appearance',
                         ['Colour temperature', 'Colour', 'Leave alone'])
        if choice == BACK:
            return
        if choice == 2:
            scene['mode'] = scene_lib.MODE_NONE
        elif choice == 0:
            options = [name for name, _k in TEMP_PRESETS] + ['Custom...']
            pick = _select('Colour temperature', options)
            if pick == BACK:
                return
            if pick == len(TEMP_PRESETS):
                value = self._ask_number('Kelvin', str(scene['kelvin']))
                if value is None:
                    return
                scene['kelvin'] = max(1500, min(12000, value))
            else:
                scene['kelvin'] = TEMP_PRESETS[pick][1]
            scene['mode'] = scene_lib.MODE_TEMP
        else:
            options = [name for name, _rgb in COLOR_PRESETS] + ['Custom hex...']
            pick = _select('Colour', options)
            if pick == BACK:
                return
            if pick == len(COLOR_PRESETS):
                entered = self._ask_hex()
                if entered is None:
                    return
                scene['color'] = list(entered[0])
            else:
                scene['color'] = list(COLOR_PRESETS[pick][1])
            scene['mode'] = scene_lib.MODE_COLOR

    def _edit_targets(self, scene):
        """Pick which lights a scene touches, one at a time.

        Krypton's Dialog().multiselect exists but silently differs across skins
        on some builds, so this uses a plain checklist the user toggles.
        """
        devices = self.app.devices
        if not devices:
            utils.force_notify('No devices known yet')
            return

        chosen = set(scene['targets'])
        while True:
            options = ['Apply to all lights' + (' [x]' if not chosen else '')]
            for device in devices:
                mark = '[x]' if device.device_id in chosen else '[ ]'
                options.append('%s %s' % (mark, device.name))
            options.append('Done')

            choice = _select('Lights in this scene', options)
            if choice == BACK or choice == len(options) - 1:
                scene['targets'] = sorted(chosen)
                return
            if choice == 0:
                chosen = set()
                continue
            device = devices[choice - 1]
            if device.device_id in chosen:
                chosen.discard(device.device_id)
            else:
                chosen.add(device.device_id)

    # -- devices ------------------------------------------------------------

    def refresh_devices(self):
        progress = xbmcgui.DialogProgressBG()
        progress.create('Paragon Govee', 'Searching for lights...')
        try:
            devices, warnings = self.app.refresh_devices()
        except Exception as exc:  # a failed refresh must not kill the panel
            utils.log('Device refresh raised: %s' % exc)
            progress.close()
            _dialog().ok('Paragon Govee', 'Device search failed:\n\n%s' % exc)
            return
        progress.close()

        if not devices:
            message = 'No lights were found.\n\n' \
                      'Check that LAN Control is switched on for each light ' \
                      'in the Govee Home app, or set a Govee API key in ' \
                      'Settings to use the cloud.'
            if warnings:
                message += '\n\n' + '\n'.join(warnings[:2])
            _dialog().ok('Paragon Govee', message)
            return

        lan_count = len([d for d in devices if d.lan])
        summary = 'Found %d light(s), %d on the LAN.' % (len(devices),
                                                         lan_count)
        if warnings:
            _dialog().ok('Paragon Govee',
                         summary + '\n\n' + '\n'.join(warnings[:2]))
        else:
            utils.notify(summary)

    def manage_devices(self):
        while True:
            devices = self.app.devices
            if not devices:
                utils.force_notify('No devices known yet')
                return

            rows = [('Name lights one by one...', self.name_lights)]
            for device in devices:
                mark = '[x]' if device.enabled else '[ ]'
                label = '%s %s  (%s)' % (mark, device.name,
                                         '+'.join(device.transports())
                                         or 'offline')
                rows.append((label,
                             lambda d=device: self._edit_device(d)))

            choice = _select('Manage devices', [l for l, _h in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def name_lights(self):
        """Walk the lights one at a time, lighting each while asking its name.

        Renaming through the per-device menu means going and looking at the
        room, coming back, and navigating two menus again -- 25 times. Here
        the light stays lit while the keyboard is up, so the answer is on the
        wall in front of you as you type it.
        """
        devices = self.app.enabled_devices
        if not devices:
            utils.force_notify('No lights to name. Run a device refresh.')
            return

        unnamed = [d for d in devices if self._is_placeholder_name(d)]
        if unnamed and len(unnamed) != len(devices):
            choice = _select(
                'Which lights?',
                ['Only the %d still unnamed' % len(unnamed),
                 'All %d lights' % len(devices)])
            if choice == BACK:
                return
            devices = unnamed if choice == 0 else devices

        if not _dialog().yesno(
                'Paragon Govee',
                'Each light will come on bright magenta in turn. Type the '
                'name of whichever one lights up.\n\n'
                'Cancel the keyboard to stop; names entered so far are '
                'kept.\n\nStart?'):
            return

        # One bulk read up front rather than per light: the lights get put
        # back from this snapshot as the walk moves on.
        progress = xbmcgui.DialogProgressBG()
        progress.create('Paragon Govee', 'Reading the lights...')
        try:
            states = self.app.controller.get_states(devices)
        except Exception as exc:
            utils.log('Could not read states before naming: %s' % exc)
            states = {}
        progress.close()

        named = self._walk_and_name(devices, states)

        self.app.save_devices()
        if named:
            _dialog().ok('Paragon Govee', 'Named %d light(s).' % named)
        else:
            utils.notify('No lights were renamed')

    @staticmethod
    def _is_placeholder_name(device):
        """True when the name is still the model + id one discovery invents."""
        return device.name.startswith(device.model + ' (')

    def _walk_and_name(self, devices, states):
        """Light each device in turn and prompt. Returns how many were named."""
        highlight = {'power': scene_lib.POWER_ON,
                     'brightness': HIGHLIGHT_BRIGHTNESS,
                     'mode': scene_lib.MODE_COLOR,
                     'color': list(HIGHLIGHT_COLOR),
                     'kelvin': 2700}
        named = 0

        for index, device in enumerate(devices):
            try:
                scene_lib.apply_settings(self.app.controller, device,
                                         highlight)
            except ControlError as exc:
                utils.log('Could not light %s for naming: %s'
                          % (device.name, exc))

            answer = _dialog().input(
                'Light %d of %d - which one is lit up?'
                % (index + 1, len(devices)), device.name)

            # Put it back before doing anything else, so a stop leaves the
            # room as it was rather than with one bulb stuck on magenta.
            self._restore(device, states.get(device.device_id))

            if not answer:
                # Kodi returns an empty string when the keyboard is cancelled,
                # which is the only signal available for "stop here".
                break
            answer = answer.strip()
            if answer and answer != device.name:
                device.name = answer
                named += 1

        return named

    def _restore(self, device, state):
        settings = scene_lib.state_to_settings(state)
        if not settings:
            return
        try:
            scene_lib.apply_settings(self.app.controller, device, settings)
        except ControlError as exc:
            utils.log('Could not restore %s after naming: %s'
                      % (device.name, exc))

    def _edit_device(self, device):
        options = [
            'Rename (currently "%s")' % device.name,
            'Disable' if device.enabled else 'Enable',
            'Identify (flash this light)',
        ]
        choice = _select(device.name, options)
        if choice == BACK:
            return

        if choice == 0:
            name = _dialog().input('Light name', device.name)
            if name and name.strip():
                device.name = name.strip()
                self.app.save_devices()
                utils.notify('Renamed to %s' % device.name)
        elif choice == 1:
            device.enabled = not device.enabled
            self.app.save_devices()
            utils.notify('%s %s' % (device.name,
                                    'enabled' if device.enabled else 'disabled'))
        elif choice == 2:
            self._identify(device)

    def _identify(self, device):
        """Blink a light so the user can tell which physical unit it is."""
        import time

        try:
            for _ in range(3):
                self.app.controller.turn(device, False)
                time.sleep(0.4)
                self.app.controller.turn(device, True)
                time.sleep(0.4)
        except ControlError as exc:
            utils.force_notify(str(exc))
            return
        utils.notify('Flashed %s' % device.name)

    # -- input helpers ------------------------------------------------------

    @staticmethod
    def _ask_number(heading, default):
        value = _dialog().input(heading, str(default),
                                type=xbmcgui.INPUT_NUMERIC)
        if value is None or value == '':
            return None
        try:
            return int(value)
        except ValueError:
            utils.force_notify('That is not a number')
            return None

    @staticmethod
    def _ask_hex():
        """Prompt for a hex colour, returning ((r, g, b), label) or None.

        The Govee app hands out 8-digit codes, so pasting one straight in has
        to work. The returned label carries how it was read, because dropping
        the alpha off the wrong end silently produces a different colour.
        """
        value = _dialog().input('Colour as hex - 6 or 8 digits, e.g. FF8800 '
                                'or FFFF8800', '')
        if not value:
            return None

        rgb, note = scene_lib.parse_hex_color(value)
        if rgb is None:
            utils.force_notify(note)
            return None

        label = '#%02X%02X%02X' % rgb
        if note:
            label = '%s (%s)' % (label, note)
        return rgb, label


def pick_scene_for_setting(app, setting_id):
    """Settings helper: choose a scene and write its name into `setting_id`."""
    scenes = app.scenes
    if not scenes:
        utils.force_notify('No scenes are defined yet')
        return
    options = ['(none)'] + ['%s  -  %s' % (s['name'], scene_lib.describe(s))
                            for s in scenes]
    choice = _select('Choose a scene', options)
    if choice == BACK:
        return
    name = '' if choice == 0 else scenes[choice - 1]['name']
    utils.set_setting(setting_id, name)
    utils.notify('Scene set to %s' % (name or 'none'))
