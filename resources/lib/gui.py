# -*- coding: utf-8 -*-
"""
Paragon Home
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
import palette as palette_lib
import reracks as rerack_lib
import scenes as scene_lib
from devices import (CAP_BRIGHTNESS, CAP_COLOR, CAP_COLOR_TEMP,
                     CAP_COMMANDS, CAP_POWER, CAP_STATE,
                     ControlError, DEFAULT_DRIVER, TRANSPORT_CLOUD)

# Presets offered before the user has to type anything.
BRIGHTNESS_STEPS = [5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]

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
# The order the kinds of device are offered in, and what to call one
# when its driver is not loaded. A driver not listed here still
# appears, after these, under its own name.
DRIVER_ORDER = ('govee', 'broadlink', 'tuya', 'kasa')
DRIVER_LABELS = {'govee': 'Govee', 'broadlink': 'Broadlink',
                 'tuya': 'Tuya', 'kasa': 'Kasa'}

HIGHLIGHT_COLOR = (255, 0, 255)
HIGHLIGHT_BRIGHTNESS = 100

# How many times "Identify" blinks a light, and the gap between each change.
# Ten blinks is about eight seconds -- long enough to walk into the next room
# and still be looking when it is flashing.
IDENTIFY_FLASHES = 10
IDENTIFY_GAP = 0.4

# How long to wait for a remote button press when learning a code. Thirty
# seconds is long enough to find the right button on an unfamiliar remote.
LEARN_ATTEMPTS = 30
LEARN_POLL = 1.0

# Offered when setting how often a mix scene moves the colours along.
CYCLE_STEPS = [
    ('Off - hold the arrangement', 0),
    ('Every 15 seconds', 15),
    ('Every 30 seconds', 30),
    ('Every minute', 60),
    ('Every 2 minutes', 120),
    ('Every 5 minutes', 300),
    ('Every 15 minutes', 900),
]

# Cycling drives every light on every step. Over the cloud that is metered:
# Govee allows about 10,000 calls a day, so this is the point at which a
# cycle would eat the lot.
CLOUD_DAILY_CALLS = 10000


def _dialog():
    return xbmcgui.Dialog()


def _select(heading, options):
    """Wrapper around Dialog().select that returns BACK when cancelled."""
    if not options:
        return BACK
    return _dialog().select(heading, options)


def _duration(seconds):
    """'90 seconds' -> 'minute and a half'-ish, in plain words."""
    seconds = int(seconds)
    if seconds % 3600 == 0 and seconds >= 3600:
        hours = seconds // 3600
        return '%d hour%s' % (hours, '' if hours == 1 else 's')
    if seconds % 60 == 0 and seconds >= 60:
        minutes = seconds // 60
        return 'minute' if minutes == 1 else '%d minutes' % minutes
    return '%d seconds' % seconds


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
        prompt = ('No devices are known yet.\n\n'
                  'Search the network for them now?')
        if not _dialog().yesno(utils.ADDON_NAME, prompt):
            return True
        self.refresh_devices()
        return not self.app.devices

    # -- main menu ---------------------------------------------------------

    def main_menu(self):
        """Top-level menu, built as (label, handler) rows.

        One row per kind of device rather than one row per device. With three
        drivers and forty devices the flat list ran off the screen, and a
        Broadlink blaster sat among the bulbs offering a brightness it does
        not have. Sorting by driver first means every menu below this point
        can offer only what that kind of device actually does.

        The version is in the heading on purpose: the add-on is normally
        installed as a git clone and updated with `git pull`, and this is the
        quickest way to confirm which version is actually running.
        """
        devices = self.app.enabled_devices
        rows = []

        cycle = self.app.read_cycle()
        if cycle:
            rows.append(('Stop cycling (%s)' % cycle.get('scene'),
                         self.stop_cycling))

        for driver_id in self._driver_ids(devices):
            owned = self._devices_for(driver_id, devices)
            rows.append(('%s (%d)' % (self._driver_label(driver_id),
                                      len(owned)),
                         lambda d=driver_id: self.driver_menu(d)))

        rows.extend([
            ('Scenes...', self.scene_menu),
            ('Reracks...', self.rerack_menu),
            ('Refresh devices', self.refresh_devices),
            # Stays at the top level rather than inside a driver: the time you
            # need it is when a driver found nothing and so has no menu.
            ('Diagnose device search...', self.diagnose_menu),
            ('Settings', utils.open_settings),
        ])

        choice = _select('%s %s' % (utils.ADDON_NAME, utils.ADDON_VERSION),
                         [label for label, _handler in rows])
        if choice == BACK:
            return BACK
        rows[choice][1]()
        return None

    # -- drivers -----------------------------------------------------------

    @staticmethod
    def _driver_of(device):
        return getattr(device, 'driver', None) or DEFAULT_DRIVER

    @classmethod
    def _devices_for(cls, driver_id, devices):
        return [d for d in devices if cls._driver_of(d) == driver_id]

    @classmethod
    def _driver_ids(cls, devices):
        """Which drivers own something, in a stable, familiar order.

        Taken from the devices rather than from the installed drivers so a
        driver with nothing found does not sit on the menu as a dead row.
        """
        seen = []
        for device in devices:
            driver_id = cls._driver_of(device)
            if driver_id not in seen:
                seen.append(driver_id)
        known = [d for d in DRIVER_ORDER if d in seen]
        return known + sorted(d for d in seen if d not in DRIVER_ORDER)

    def _driver_label(self, driver_id):
        lookup = getattr(self.app.controller, 'driver', None)
        driver = lookup(driver_id) if lookup is not None else None
        return (getattr(driver, 'DRIVER_LABEL', None)
                or DRIVER_LABELS.get(driver_id)
                or driver_id.title())

    def driver_menu(self, driver_id):
        """Everything belonging to one kind of device, and nothing else."""
        while True:
            label = self._driver_label(driver_id)
            devices = self._devices_for(driver_id, self.app.enabled_devices)
            if not devices:
                utils.force_notify('No %s devices are enabled' % label)
                return

            rows = []
            if CAP_POWER in self._capabilities(devices):
                rows.append(('All %s (%d)' % (label, len(devices)),
                             lambda: self.control_menu(devices,
                                                       'All %s' % label)))

            for device in devices:
                # Bind the device to this row rather than closing over the
                # loop variable, which would leave every row pointing at the
                # last one.
                rows.append((self._device_label(device),
                             lambda d=device: self.device_menu(d)))

            rows.append(('Manage %s devices...' % label,
                         lambda: self.manage_devices(driver_id)))

            choice = _select(label, [row_label for row_label, _h in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def _device_label(self, device):
        """A device row: its name, and how it is reached when that varies.

        Only Govee can be reached more than one way. Tagging a Tuya plug
        [LAN] states the only possibility it has, which is noise on a menu
        whose whole point is that every row earns its place.
        """
        transports = device.transports()
        if not transports or not self._has_transport_choice(device):
            return device.name
        return '%s  [%s]' % (device.name,
                             '+'.join(t.upper() for t in transports))

    def _has_transport_choice(self, device):
        lookup = getattr(self.app.controller, 'driver_for', None)
        driver = lookup(device) if lookup is not None else None
        return bool(getattr(driver, 'HAS_TRANSPORTS', False))

    def device_menu(self, device):
        """Open the menu that fits what this device is.

        A blaster has codes, not brightness; a plug has power, not colour.
        Routing here is what stops one menu having to apologise for rows that
        do not apply.
        """
        if CAP_COMMANDS in self.app.controller.capabilities(device):
            return self.command_menu(device)
        return self.control_menu([device], device.name)

    def _capabilities(self, targets):
        """The union of what the targets can do."""
        devices = (targets if targets is not None
                   else self.app.enabled_devices)
        found = set()
        for device in devices:
            found |= set(self.app.controller.capabilities(device) or [])
        return found

    def stop_cycling(self):
        name = self.app.stop_cycle()
        if name:
            utils.notify('Stopped cycling %s' % name)

    def diagnose_menu(self):
        """Which search to look into. Each driver fails its own way."""
        rows = [('Govee lights', self.diagnose),
                ('Tuya plugs', self.diagnose_tuya),
                ('Kasa plugs', self.diagnose_kasa)]
        choice = _select('Diagnose device search',
                         [label for label, _handler in rows])
        if choice == BACK:
            return
        rows[choice][1]()

    def diagnose_kasa(self):
        """Broadcast for Kasa devices and explain what came back."""
        import diagnostics

        self._diagnose('Kasa', 'Searching for Kasa devices...',
                       lambda: diagnostics.run_kasa(self.app))

    def _diagnose(self, label, busy, run):
        progress = xbmcgui.DialogProgressBG()
        progress.create(utils.ADDON_NAME, busy)
        try:
            text, _report = run()
        except Exception as exc:
            utils.log('%s diagnostics failed: %s' % (label, exc))
            progress.close()
            _dialog().ok(utils.ADDON_NAME, 'Diagnostics failed:\n\n%s' % exc)
            return
        progress.close()
        _dialog().ok('%s - %s search' % (utils.ADDON_NAME, label), text)

    def diagnose_tuya(self):
        """Listen for Tuya announcements and explain what was heard."""
        import diagnostics

        progress = xbmcgui.DialogProgressBG()
        progress.create(utils.ADDON_NAME, 'Listening for Tuya devices...')
        try:
            text, _report = diagnostics.run_tuya(self.app)
        except Exception as exc:
            utils.log('Tuya diagnostics failed: %s' % exc)
            progress.close()
            _dialog().ok(utils.ADDON_NAME, 'Diagnostics failed:\n\n%s' % exc)
            return
        progress.close()
        _dialog().ok('%s - Tuya search' % utils.ADDON_NAME, text)

    def diagnose(self):
        """Probe the LAN and explain the result on screen and in the log."""
        import diagnostics

        progress = xbmcgui.DialogProgressBG()
        progress.create(utils.ADDON_NAME, 'Probing the network...')
        try:
            text, _report = diagnostics.run(self.app)
        except Exception as exc:
            utils.log('Diagnostics failed: %s' % exc)
            progress.close()
            _dialog().ok(utils.ADDON_NAME, 'Diagnostics failed:\n\n%s' % exc)
            return
        progress.close()
        _dialog().ok('%s - LAN diagnostics' % utils.ADDON_NAME, text)

    def verify_status(self, device):
        """Drive one bulb to a known colour and see if it reports it back."""
        import diagnostics

        if not _dialog().yesno(
                utils.ADDON_NAME,
                'This will briefly set %s to a test colour and then put it '
                'back, to check whether it reports its state honestly.\n\n'
                'Continue?' % device.name):
            return

        progress = xbmcgui.DialogProgressBG()
        progress.create(utils.ADDON_NAME, 'Testing %s...' % device.name)
        try:
            report = diagnostics.verify_status(self.app, device)
        except Exception as exc:
            utils.log('Status round-trip failed: %s' % exc)
            progress.close()
            _dialog().ok(utils.ADDON_NAME, 'Test failed:\n\n%s' % exc)
            return
        progress.close()
        _dialog().ok('%s - status check' % utils.ADDON_NAME,
                     diagnostics.verify_summary(report))

    # -- control ------------------------------------------------------------

    def control_menu(self, targets, heading):
        """Power/brightness/colour menu for one device or the whole group.

        Rows are (label, handler) pairs rather than a list indexed against a
        chain of elifs, so inserting a row cannot silently rewire the ones
        below it.
        """
        while True:
            capabilities = self._capabilities(targets)
            rows = []

            if CAP_POWER in capabilities:
                rows.extend([
                    ('Toggle',
                     lambda: _report(self.app.toggle_all(targets),
                                     'Toggled %s' % heading)),
                    ('On',
                     lambda: _report(self.app.power_all(True, targets),
                                     '%s on' % heading)),
                    ('Off',
                     lambda: _report(self.app.power_all(False, targets),
                                     '%s off' % heading)),
                ])
            if CAP_BRIGHTNESS in capabilities:
                rows.append(('Brightness...',
                             lambda: self.brightness_menu(targets, heading)))
            if CAP_COLOR in capabilities:
                rows.append(('Colour...',
                             lambda: self.color_menu(targets, heading)))
            if CAP_COLOR_TEMP in capabilities:
                rows.append(('Colour temperature...',
                             lambda: self.temp_menu(targets, heading)))

            if targets and len(targets) == 1:
                if CAP_STATE in capabilities:
                    rows.append(('Show status',
                                 lambda: self.show_status(targets[0])))
                if CAP_COLOR in capabilities:
                    # The round trip drives the device to a known colour and
                    # reads it back, so it has nothing to say about a plug.
                    rows.append(('Check status reporting...',
                                 lambda: self.verify_status(targets[0])))

            choice = _select(heading, [label for label, _handler in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def _explain_no_status(self, device):
        """Say why a state read failed, in the terms of the device that failed.

        A read returns None whatever went wrong, so this asks the driver
        again through its own connection test, which does report a reason.
        Without it every driver inherited Govee's explanation -- a Kasa plug
        that could not be reached was blamed on a UDP port Govee uses and
        Kasa has never heard of.
        """
        message = ''
        ok = False
        try:
            ok, message = self.app.test_device(device)
        except ControlError:
            message = ''

        if ok and message:
            # It answered on the second attempt, so the reading is the answer.
            _dialog().ok(utils.ADDON_NAME, message)
            return
        if message:
            _dialog().ok(utils.ADDON_NAME,
                         'Could not read the state of %s.\n\n%s'
                         % (device.name, message))
            return

        # Only Govee gets here: it is the one driver with no connection test,
        # and the one whose status read contends for a well-known port.
        _dialog().ok(utils.ADDON_NAME,
                     'Could not read the state of %s.\n\n'
                     'LAN status needs UDP port 4002, which another '
                     'program may be holding.' % device.name)

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

    def _palette_rows(self):
        """Menu labels for the saved colours, e.g. 'Deep Red  #FF0000'."""
        return ['%s  %s' % (entry['name'], palette_lib.to_hex(entry['color']))
                for entry in self.app.palette]

    def color_menu(self, targets, heading):
        while True:
            entries = self.app.palette
            options = self._palette_rows()
            options.append('Custom hex...')
            options.append('Manage colours...')

            choice = _select('%s - colour' % heading, options)
            if choice == BACK:
                return

            if choice == len(entries) + 1:
                self.manage_colors()
                continue

            if choice == len(entries):
                entered = self._ask_hex()
                if entered is None:
                    return
                rgb, label = entered
            else:
                entry = entries[choice]
                rgb, label = entry['color'], entry['name']

            _report(self.app.color_all(rgb, targets),
                    '%s set to %s' % (heading, label))
            return

    # -- colour palette -----------------------------------------------------

    def manage_colors(self):
        """Add, edit, reorder and delete the saved colours."""
        while True:
            entries = self.app.palette
            options = self._palette_rows()
            options.append('Add a colour...')
            options.append('Reset to the built-in colours')

            choice = _select('Manage colours', options)
            if choice == BACK:
                return
            if choice == len(entries):
                self._add_color()
            elif choice == len(entries) + 1:
                if _dialog().yesno(
                        utils.ADDON_NAME,
                        'Replace the colour list with the built-in set?\n\n'
                        'Any colours you added are removed.'):
                    self.app.reset_palette()
                    utils.notify('Colours reset')
            else:
                self._edit_color(choice)

    def _add_color(self):
        entered = self._ask_hex()
        if entered is None:
            return
        rgb, _label = entered

        name = _dialog().input('Name for this colour', '')
        if not name or not name.strip():
            return
        name = name.strip()

        if self.app.color_by_name(name) is not None and not _dialog().yesno(
                utils.ADDON_NAME,
                'A colour called "%s" already exists.\n\nReplace it?' % name):
            return

        if self.app.save_color(name, rgb) is None:
            utils.force_notify('That colour could not be saved')
            return
        utils.notify('Added %s  %s' % (name, palette_lib.to_hex(rgb)))

    def _edit_color(self, index):
        entries = self.app.palette
        if index < 0 or index >= len(entries):
            return
        entry = entries[index]

        rows = [
            ('Rename (currently "%s")' % entry['name'], 'rename'),
            ('Change colour (currently %s)'
             % palette_lib.to_hex(entry['color']), 'recolor'),
            ('Move up', 'up'),
            ('Move down', 'down'),
            ('Delete', 'delete'),
        ]
        choice = _select(entry['name'], [label for label, _key in rows])
        if choice == BACK:
            return
        action = rows[choice][1]

        if action == 'rename':
            name = _dialog().input('Colour name', entry['name'])
            if name and name.strip() and name.strip() != entry['name']:
                # Remove first so the rename does not collide with itself.
                self.app.remove_color(entry)
                self.app.save_color(name.strip(), entry['color'], index=index)
                utils.notify('Renamed to %s' % name.strip())
        elif action == 'recolor':
            entered = self._ask_hex()
            if entered is None:
                return
            self.app.save_color(entry['name'], entered[0])
            utils.notify('%s is now %s'
                         % (entry['name'], palette_lib.to_hex(entered[0])))
        elif action == 'up':
            self.app.move_color(index, -1)
        elif action == 'down':
            self.app.move_color(index, 1)
        elif action == 'delete':
            if _dialog().yesno(utils.ADDON_NAME,
                               'Delete the colour "%s"?' % entry['name']):
                self.app.remove_color(entry)
                utils.notify('Deleted %s' % entry['name'])

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
            self._explain_no_status(device)
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
                utils.ADDON_NAME,
                'A scene called "%s" already exists.\n\nReplace it with what '
                'the lights are doing now?' % name):
            return

        progress = xbmcgui.DialogProgressBG()
        progress.create(utils.ADDON_NAME, 'Reading the lights...')
        try:
            scene, captured, skipped = self.app.capture_scene(name)
        except Exception as exc:
            utils.log('Capture failed: %s' % exc)
            progress.close()
            _dialog().ok(utils.ADDON_NAME, 'Capture failed:\n\n%s' % exc)
            return
        progress.close()

        if not captured:
            _dialog().ok(utils.ADDON_NAME,
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
        _dialog().ok(utils.ADDON_NAME, message)

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
            elif scene['mode'] == scene_lib.MODE_MIX:
                names = [e['name'] for e in scene.get('colors') or []]
                appearance = 'mix: %s' % (', '.join(names[:3])
                                          + (', ...' if len(names) > 3 else ''))
            elif scene['mode'] == scene_lib.MODE_TEMP:
                appearance = '%dK' % scene['kelvin']
            else:
                appearance = 'leave alone'
            targets = scene['targets']
            # Says which "all" this is. A scene with no targets applies to
            # what it can describe, so a colour scene means the colour
            # devices and not every plug in the house.
            if targets:
                target_label = '%d selected' % len(targets)
            elif scene_lib.scene_expresses(scene):
                target_label = 'all colour devices'
            else:
                target_label = 'all devices'
            cycle = int(scene.get('cycle') or 0)
            cycle_label = ('off' if not cycle
                           else 'every %s' % _duration(cycle))

            commands = scene.get('actions') or []
            command_label = ('none' if not commands
                             else '%d' % len(commands))

            # (label, handler) rows rather than a list read back by index.
            # This editor has grown a row four times, and every time the
            # numbering under it had to be re-counted by hand.
            rows = [
                ('Name: %s' % scene['name'],
                 lambda: self._edit_scene_name(scene)),
                ('Power: %s' % scene['power'],
                 lambda: self._edit_power(scene)),
                ('Brightness: %s' % brightness,
                 lambda: self._edit_brightness(scene)),
                ('Appearance: %s' % appearance,
                 lambda: self._edit_appearance(scene)),
                ('Lights: %s' % target_label,
                 lambda: self._edit_targets(scene)),
                ('Commands to send: %s' % command_label,
                 lambda: self._edit_actions(scene)),
                ('Cycle colours: %s' % cycle_label,
                 lambda: self._edit_cycle(scene)),
                ('Test this scene', lambda: self.app.apply_scene(scene)),
                ('Save', lambda: self._save_and_close(scene, index)),
            ]
            if index is not None:
                rows.append(('Delete',
                             lambda: self._delete_scene(scene, index)))

            choice = _select('Edit scene', [label for label, _h in rows])
            if choice == BACK:
                return
            if rows[choice][1]() is False:
                return

    def _edit_scene_name(self, scene):
        name = _dialog().input('Scene name', scene['name'])
        if name and name.strip():
            scene['name'] = name.strip()

    def _edit_power(self, scene):
        pick = _select('Power', ['Turn on', 'Turn off', 'Leave as it is'])
        if pick != BACK:
            scene['power'] = [scene_lib.POWER_ON, scene_lib.POWER_OFF,
                              scene_lib.POWER_KEEP][pick]

    def _save_and_close(self, scene, index):
        self._save_scene(scene, index)
        return False

    def _delete_scene(self, scene, index):
        if _dialog().yesno(utils.ADDON_NAME,
                           'Delete the scene "%s"?' % scene['name']):
            del self.app.scenes[index]
            self.app.save_scenes()
            utils.notify('Scene deleted')
        return False

    def _edit_actions(self, scene):
        """The commands a scene fires alongside its lighting.

        A scene sets state; a command has none to set. An infrared blaster has
        no colour, it has "AVR Power" -- so one "Movie Night" can dim the
        lights and switch the amplifier on in the same breath.
        """
        from devices import CAP_COMMANDS

        while True:
            actions = scene.get('actions') or []
            rows = []
            for action in actions:
                device = self.app.device_by_id(action['device'])
                name = device.name if device else action['device']
                rows.append(('%s: %s' % (name, action['command']),
                             lambda a=action: self._remove_action(scene, a)))

            emitters = [d for d in self.app.enabled_devices
                        if CAP_COMMANDS in self.app.controller.capabilities(d)]
            if emitters:
                rows.append(('Add a command...',
                             lambda: self._add_action(scene, emitters)))
            elif not rows:
                utils.force_notify('No device here sends commands')
                return

            choice = _select('%s - commands' % scene['name'],
                             [label for label, _h in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def _add_action(self, scene, emitters):
        """Pick a blaster, then one of the codes it knows."""
        pick = _select('Which device', [d.name for d in emitters])
        if pick == BACK:
            return
        device = emitters[pick]

        names = self.app.controller.commands(device)
        if not names:
            _dialog().ok(utils.ADDON_NAME,
                         '%s has not learned any commands yet.\n\n'
                         'Teach it one under its own menu first.' % device.name)
            return

        chosen = _select('Which command', list(names))
        if chosen == BACK:
            return

        actions = list(scene.get('actions') or [])
        actions.append({'device': device.device_id,
                        'command': names[chosen]})
        scene['actions'] = actions

    def _remove_action(self, scene, action):
        if not _dialog().yesno(
                utils.ADDON_NAME,
                'Stop this scene sending "%s"?' % action['command']):
            return
        scene['actions'] = [a for a in scene.get('actions') or []
                            if a is not action]

    def _save_scene(self, scene, index):
        cleaned = scene_lib.normalise(scene)
        if cleaned is None:
            utils.force_notify('That scene could not be saved')
            return
        scenes = self.app.scenes
        clash = scene_lib.find(scenes, cleaned['name'])
        if clash is not None and (index is None or scenes[index] is not clash):
            if not _dialog().yesno(
                    utils.ADDON_NAME,
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
                         ['Colour temperature', 'Colour',
                          'Mix of colours (spread over the lights)...',
                          'Leave alone'])
        if choice == BACK:
            return
        if choice == 3:
            scene['mode'] = scene_lib.MODE_NONE
        elif choice == 2:
            self._edit_mix(scene)
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
            entries = self.app.palette
            options = self._palette_rows() + ['Custom hex...']
            pick = _select('Colour', options)
            if pick == BACK:
                return
            if pick == len(entries):
                entered = self._ask_hex()
                if entered is None:
                    return
                scene['color'] = list(entered[0])
            else:
                scene['color'] = list(entries[pick]['color'])
            scene['mode'] = scene_lib.MODE_COLOR

    def _edit_cycle(self, scene):
        """How often a mix moves the colours along, or off."""
        if scene.get('mode') != scene_lib.MODE_MIX:
            _dialog().ok(utils.ADDON_NAME,
                         'Cycling moves the lights through a mix of '
                         'colours.\n\nSet Appearance to "Mix of colours" '
                         'first.')
            return

        choice = _select('Cycle colours',
                         [label for label, _seconds in CYCLE_STEPS])
        if choice == BACK:
            return

        seconds = CYCLE_STEPS[choice][1]
        if seconds and not self._cycle_cost_ok(scene, seconds):
            return
        scene['cycle'] = seconds

    def _cycle_cost_ok(self, scene, seconds):
        """Warn before a cycle that would burn the Govee cloud quota.

        Every step drives every light. Over the LAN that is free; over the
        cloud it is metered, and 25 lights on a one-minute cycle is 36,000
        calls a day against a limit of about 10,000.
        """
        targets = scene_lib.scene_targets(scene, self.app.devices,
                                          self.app.controller)
        cloud = [d for d in targets
                 if self.app.controller.pick_transport(d) == TRANSPORT_CLOUD]
        if not cloud:
            return True

        per_day = len(cloud) * (86400.0 / seconds)
        if per_day <= CLOUD_DAILY_CALLS:
            return True

        return _dialog().yesno(
            utils.ADDON_NAME,
            '%d of these lights are driven over the Govee cloud, which is '
            'rate limited.\n\nCycling every %s would use about %d cloud '
            'calls a day against a limit near %d, so the lights would stop '
            'responding partway through.\n\nUse it anyway?'
            % (len(cloud), _duration(seconds), int(per_day),
               CLOUD_DAILY_CALLS))

    def _edit_mix(self, scene):
        """Tick which saved colours go into the mix.

        Colours are copied into the scene rather than referenced by name, so
        editing or deleting a palette entry later cannot silently change or
        empty a scene that was already built and tested.
        """
        entries = self.app.palette
        if not entries:
            utils.force_notify('No colours defined yet')
            return

        chosen = [dict(e) for e in (scene.get('colors') or [])]
        chosen_rgb = [tuple(e['color']) for e in chosen]

        while True:
            rows = []
            for entry in entries:
                mark = '[x]' if tuple(entry['color']) in chosen_rgb else '[ ]'
                rows.append('%s %s  %s'
                            % (mark, entry['name'],
                               palette_lib.to_hex(entry['color'])))
            rows.append('Done (%d in the mix)' % len(chosen))

            choice = _select('Colours to spread over the lights', rows)
            if choice == BACK:
                return
            if choice == len(entries):
                if not chosen:
                    utils.force_notify('Pick at least one colour')
                    continue
                scene['colors'] = chosen
                scene['mode'] = scene_lib.MODE_MIX
                return

            entry = entries[choice]
            rgb = tuple(entry['color'])
            if rgb in chosen_rgb:
                position = chosen_rgb.index(rgb)
                del chosen[position]
                del chosen_rgb[position]
            else:
                chosen.append(dict(entry))
                chosen_rgb.append(rgb)

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
        progress.create(utils.ADDON_NAME, 'Searching for lights...')
        try:
            devices, warnings = self.app.refresh_devices()
        except Exception as exc:  # a failed refresh must not kill the panel
            utils.log('Device refresh raised: %s' % exc)
            progress.close()
            _dialog().ok(utils.ADDON_NAME, 'Device search failed:\n\n%s' % exc)
            return
        progress.close()

        if not devices:
            message = 'No lights were found.\n\n' \
                      'Check that LAN Control is switched on for each light ' \
                      'in the Govee Home app, or set a Govee API key in ' \
                      'Settings to use the cloud.'
            if warnings:
                message += '\n\n' + '\n'.join(warnings[:2])
            _dialog().ok(utils.ADDON_NAME, message)
            return

        lan_count = len([d for d in devices if d.lan])
        summary = 'Found %d light(s), %d on the LAN.' % (len(devices),
                                                         lan_count)
        missing = getattr(self.app, 'last_refresh_missing', 0)
        if missing:
            summary += ('\n\n%d known light(s) did not answer and were kept '
                        'as they were. Their names are safe; they will pick '
                        'up again on the next search.' % missing)
        if warnings:
            _dialog().ok(utils.ADDON_NAME,
                         summary + '\n\n' + '\n'.join(warnings[:2]))
        else:
            utils.notify(summary)

    def manage_devices(self, driver_id=None):
        """Rename, enable, identify and forget. Scoped to one kind of device
        when it is reached from that kind's menu."""
        while True:
            devices = self.app.devices
            if driver_id is not None:
                devices = self._devices_for(driver_id, devices)
            if not devices:
                utils.force_notify('No devices known yet')
                return

            heading = 'Manage devices'
            rows = []
            if driver_id is not None:
                heading = 'Manage %s devices' % self._driver_label(driver_id)
            # The walkthrough lights each device in turn and asks what it is,
            # which needs something that can show a colour.
            if CAP_COLOR in self._capabilities(devices):
                rows.append(('Name lights one by one...',
                             lambda: self.name_lights(driver_id)))
            for device in devices:
                mark = '[x]' if device.enabled else '[ ]'
                label = '%s %s  (%s)' % (mark, device.name,
                                         '+'.join(device.transports())
                                         or 'offline')
                rows.append((label,
                             lambda d=device: self._edit_device(d)))

            choice = _select(heading, [l for l, _h in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def name_lights(self, driver_id=None):
        """Walk the lights one at a time, lighting each while asking its name.

        Renaming through the per-device menu means going and looking at the
        room, coming back, and navigating two menus again -- 25 times. Here
        the light stays lit while the keyboard is up, so the answer is on the
        wall in front of you as you type it.
        """
        devices = self.app.enabled_devices
        if driver_id is not None:
            devices = self._devices_for(driver_id, devices)
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
                utils.ADDON_NAME,
                'Each light will come on bright magenta in turn. Type the '
                'name of whichever one lights up.\n\n'
                'Cancel the keyboard to stop; names entered so far are '
                'kept.\n\nStart?'):
            return

        # One bulk read up front rather than per light: the lights get put
        # back from this snapshot as the walk moves on.
        progress = xbmcgui.DialogProgressBG()
        progress.create(utils.ADDON_NAME, 'Reading the lights...')
        try:
            states = self.app.controller.get_states(devices)
        except Exception as exc:
            utils.log('Could not read states before naming: %s' % exc)
            states = {}
        progress.close()

        named = self._walk_and_name(devices, states)

        self.app.save_devices()
        if named:
            _dialog().ok(utils.ADDON_NAME, 'Named %d light(s).' % named)
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
        """The menu for one device, built from what that device actually is.

        Rows are (label, handler) pairs rather than a list read back by
        index: which rows exist depends on the driver, so every new device
        type used to shift the numbering under the rows below it.
        """
        from devices import CAP_COMMANDS, CAP_POWER

        capabilities = self.app.controller.capabilities(device)
        driver = self.app.controller.driver_for(device)
        emitter = CAP_COMMANDS in capabilities
        keyed = hasattr(driver, 'set_local_key')
        testable = hasattr(driver, 'test_connection')

        rows = [
            ('Rename (currently "%s")' % device.name,
             lambda: self._rename_device(device)),
            ('Disable' if device.enabled else 'Enable',
             lambda: self._toggle_enabled(device)),
        ]

        if emitter:
            # An IR blaster has no light to flash and nothing to identify by,
            # so its menu is about the codes it knows instead.
            rows.append(('Commands (%d learned)...'
                         % len(self.app.controller.commands(device)),
                         lambda: self.command_menu(device)))
        else:
            if keyed:
                rows.append(('Set local key%s'
                             % (' (needed)' if self.app.needs_local_key(device)
                                else ''),
                             lambda: self.set_local_key(device)))
            if CAP_COLOR in capabilities:
                # Only something that can show a colour has anything to flash.
                rows.append(('Identify (flash this light)',
                             lambda: self._identify(device)))
            if testable and not (keyed
                                 and self.app.needs_local_key(device)):
                rows.append(('Test connection',
                             lambda: self.test_device(device)))
            if hasattr(driver, 'set_power_memory') and not (
                    keyed and self.app.needs_local_key(device)):
                rows.append(('After a power cut...',
                             lambda: self.power_memory_menu(device)))

        # Anything that switches, not only the drivers that need a key. This
        # was written when Tuya was the only plug driver, and quietly denied
        # a Kasa plug the two rows it most wants.
        if CAP_COMMANDS not in capabilities and CAP_POWER in capabilities:
            rows.append(('Switch on', lambda: self._switch(device, True)))
            rows.append(('Switch off', lambda: self._switch(device, False)))

        rows.append(('Forget this device',
                     lambda: self._forget_device(device)))

        choice = _select(device.name, [label for label, _handler in rows])
        if choice == BACK:
            return
        rows[choice][1]()

    def _rename_device(self, device):
        name = _dialog().input('Device name', device.name)
        if name and name.strip():
            device.name = name.strip()
            self.app.save_devices()
            utils.notify('Renamed to %s' % device.name)

    def _toggle_enabled(self, device):
        device.enabled = not device.enabled
        self.app.save_devices()
        utils.notify('%s %s' % (device.name,
                                'enabled' if device.enabled else 'disabled'))

    def _switch(self, device, on):
        """Power one device from its own menu.

        The place a plug most wants testing is right after its key is typed
        in, which is exactly where this sits.
        """
        try:
            self.app.controller.turn(device, on)
        except ControlError as exc:
            utils.force_notify(str(exc))
            return
        utils.notify('%s %s' % (device.name, 'on' if on else 'off'))

    def _forget_device(self, device):
        if _dialog().yesno(
                utils.ADDON_NAME,
                'Forget "%s"?\n\nIts name and settings are removed. A '
                'later search will find it again as an unnamed device.'
                % device.name):
            self.app.forget_device(device)
            utils.notify('Forgot %s' % device.name)

    def power_memory_menu(self, device):
        """What the plug should do when mains power comes back.

        Read from the plug rather than remembered here: it is the plug's own
        setting and survives the add-on entirely, so showing a value we merely
        last wrote would be a guess that looks like a fact.
        """
        utils.notify('Reading %s...' % device.name)
        try:
            current, options = self.app.power_memory(device)
        except ControlError as exc:
            utils.force_notify(str(exc))
            return

        if not options:
            _dialog().ok(utils.ADDON_NAME,
                         '%s did not report a power-cut setting.\n\n'
                         'Not every plug has one.' % device.name)
            return

        labels = []
        for label, value in options:
            labels.append('%s%s' % (label, '  (now)' if value == current
                                    else ''))
        choice = _select('%s after a power cut' % device.name, labels)
        if choice == BACK:
            return

        value = options[choice][1]
        if value == current:
            return
        try:
            self.app.set_power_memory(device, value)
        except ControlError as exc:
            utils.force_notify(str(exc))
            return
        # Said plainly because the entry may be one outlet of several: there
        # is one relay memory in the box, however many sockets it has.
        _dialog().ok(utils.ADDON_NAME,
                     'After a power cut, %s will %s.\n\nThis is a setting '
                     'on the plug itself, so it covers every outlet on it and '
                     'holds whether or not Kodi is running.'
                     % (device.name, options[choice][0].lower()))

    def set_local_key(self, device):
        """Paste in the local key a Tuya device needs before it can be used."""
        value = _dialog().input(
            'Local key for %s (16 characters)' % device.name,
            self.app.controller.driver_for(device).local_key(device))
        if value is None:
            return
        if self.app.set_local_key(device, value):
            utils.notify('Key %s for %s'
                         % ('cleared' if not value.strip() else 'saved',
                            device.name))
        else:
            _dialog().ok(utils.ADDON_NAME,
                         'A Tuya local key is exactly 16 characters.\n\n'
                         'You gave %d.' % len(value.strip()))

    # -- reracks -----------------------------------------------------------

    def rerack_menu(self):
        """The saved reracks. Picking one runs it."""
        while True:
            reracks = self.app.reracks
            rows = [('%s  -  %s' % (r['name'], rerack_lib.describe(r)),
                     lambda r=r: self.run_rerack(r)) for r in reracks]
            rows.append(('New rerack...', self.new_rerack))
            if reracks:
                rows.append(('Manage reracks...', self.manage_reracks))

            choice = _select('Reracks', [label for label, _h in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def run_rerack(self, rerack):
        """Run a rerack, showing progress and letting a long one be stopped.

        A rerack can hold pauses that add up to minutes, so it runs behind a
        cancellable progress dialog rather than freezing the menu.
        """
        steps = rerack_lib.filled_steps(rerack)
        if not steps:
            utils.force_notify('%s has no steps yet' % rerack['name'])
            return

        progress = xbmcgui.DialogProgress()
        progress.create(utils.ADDON_NAME, 'Running %s...' % rerack['name'])
        total = len(rerack.get('steps') or [])

        def announce(index, step):
            if progress.iscanceled():
                return False
            progress.update(int(100.0 * index / max(1, total)),
                            'Running %s...' % rerack['name'],
                            rerack_lib.describe_step(
                                step, self._target_name(step)))
            return True

        try:
            self.app.run_rerack(rerack, on_step=announce)
        finally:
            progress.close()

    def _target_name(self, step):
        """The friendly name of a step's target, for showing it back."""
        if step.get('kind') == rerack_lib.KIND_SCENE:
            return step.get('target')
        if step.get('target') == rerack_lib.TARGET_ALL:
            return None
        found = rerack_lib.resolve_targets(step, self.app.devices)
        return found[0].name if found else None

    def new_rerack(self):
        name = _dialog().input('Name for the new rerack', '')
        if not name or not name.strip():
            return
        if self.app.rerack_by_name(name):
            _dialog().ok(utils.ADDON_NAME,
                         'There is already a rerack called "%s".'
                         % name.strip())
            return
        rerack = self.app.save_rerack(rerack_lib.make_rerack(name.strip()))
        if rerack:
            self.edit_rerack(rerack)

    def manage_reracks(self):
        while True:
            reracks = self.app.reracks
            if not reracks:
                return
            rows = [(r['name'], lambda r=r: self.edit_rerack(r))
                    for r in reracks]
            choice = _select('Manage reracks', [label for label, _h in rows])
            if choice == BACK:
                return
            rows[choice][1]()

    def edit_rerack(self, rerack):
        """The ten slots, always all ten, numbered as they run."""
        while True:
            rows = []
            for index, step in enumerate(rerack['steps']):
                rows.append(('%2d. %s'
                             % (index + 1,
                                rerack_lib.describe_step(
                                    step, self._target_name(step))),
                             lambda i=index: self.edit_step(rerack, i)))
            rows.append(('Run it now', lambda: self.run_rerack(rerack)))
            rows.append(('Rename', lambda: self._rename_rerack(rerack)))
            rows.append(('Delete this rerack',
                         lambda: self._delete_rerack(rerack)))

            choice = _select(rerack['name'], [label for label, _h in rows])
            if choice == BACK:
                return
            if rows[choice][1]() is False:
                return

    def _rename_rerack(self, rerack):
        name = _dialog().input('Rerack name', rerack['name'])
        if not name or not name.strip() or name.strip() == rerack['name']:
            return
        if self.app.rerack_by_name(name):
            _dialog().ok(utils.ADDON_NAME,
                         'There is already a rerack called "%s".'
                         % name.strip())
            return
        self.app.delete_rerack(rerack)
        rerack['name'] = name.strip()
        self.app.save_rerack(rerack)

    def _delete_rerack(self, rerack):
        if not _dialog().yesno(utils.ADDON_NAME,
                               'Delete the rerack "%s"?' % rerack['name']):
            return
        self.app.delete_rerack(rerack)
        utils.notify('Deleted %s' % rerack['name'])
        return False

    # -- one step ----------------------------------------------------------

    def edit_step(self, rerack, index):
        """Pick a step in the order you would say it: kind, target, action."""
        kinds = [('Scene', self._step_scene)]
        for driver_id in self._driver_ids(self.app.enabled_devices):
            kinds.append((self._driver_label(driver_id),
                          lambda d=driver_id: self._step_device(d)))
        kinds.append(('Pause after this step', self._step_pause))
        kinds.append(('Clear this step', lambda: rerack_lib.empty_step()))

        choice = _select('Step %d' % (index + 1),
                         [label for label, _h in kinds])
        if choice == BACK:
            return

        step = kinds[choice][1]()
        if step is None:
            return
        if step == 'pause':
            self._ask_pause(rerack, index)
            return

        # A step's pause belongs to the slot rather than to what is in it, so
        # replacing the action does not silently drop the gap after it.
        step['pause'] = rerack['steps'][index].get('pause', 0)
        rerack['steps'][index] = rerack_lib.normalise_step(step)
        self.app.save_rerack(rerack)

    def _step_scene(self):
        scenes = self.app.scenes
        if not scenes:
            utils.force_notify('No scenes to choose from yet')
            return None
        choice = _select('Which scene', [s['name'] for s in scenes])
        if choice == BACK:
            return None
        return {'kind': rerack_lib.KIND_SCENE,
                'target': scenes[choice]['name']}

    def _step_device(self, driver_id):
        """Which device of this driver, then what to do to it."""
        from devices import CAP_COMMANDS, CAP_POWER

        label = self._driver_label(driver_id)
        devices = self._devices_for(driver_id, self.app.enabled_devices)
        if not devices:
            utils.force_notify('No %s devices' % label)
            return None

        rows = [('All %s' % label, None)]
        rows.extend((device.name, device) for device in devices)
        choice = _select('Which %s device' % label,
                         [row_label for row_label, _d in rows])
        if choice == BACK:
            return None

        device = rows[choice][1]
        target = device.device_id if device is not None \
            else rerack_lib.TARGET_ALL
        sample = device if device is not None else devices[0]
        capabilities = self.app.controller.capabilities(sample)

        actions = []
        if CAP_POWER in capabilities:
            actions.extend([('On', rerack_lib.ACTION_ON),
                            ('Off', rerack_lib.ACTION_OFF),
                            ('Toggle', rerack_lib.ACTION_TOGGLE)])
        commands = []
        if device is not None and CAP_COMMANDS in capabilities:
            commands = self.app.controller.commands(device)
            actions.extend((name, name) for name in commands)

        if not actions:
            utils.force_notify('%s has nothing to switch or send' % label)
            return None

        pick = _select('What should it do',
                       [action_label for action_label, _v in actions])
        if pick == BACK:
            return None

        chosen = actions[pick][1]
        kind = rerack_lib.KIND_COMMAND if chosen in commands \
            else rerack_lib.KIND_POWER
        return {'kind': kind, 'driver': driver_id, 'target': target,
                'action': chosen}

    def _step_pause(self):
        return 'pause'

    def _ask_pause(self, rerack, index):
        current = str(rerack['steps'][index].get('pause') or 0)
        value = self._ask_number('Seconds to wait after this step', current)
        if value is None:
            return
        rerack['steps'][index]['pause'] = max(0, min(rerack_lib.MAX_PAUSE,
                                                     value))
        self.app.save_rerack(rerack)

    # -- learned commands ---------------------------------------------------

    def command_menu(self, device):
        """The codes a blaster knows: fire one, learn another, delete one."""
        while True:
            names = self.app.controller.commands(device)
            rows = list(names)
            rows.append('Learn a new command...')
            rows.append('Test connection')

            choice = _select('%s - commands' % device.name, rows)
            if choice == BACK:
                return
            if choice == len(names):
                self.learn_command(device)
                continue
            if choice == len(names) + 1:
                self.test_device(device)
                continue

            name = names[choice]
            action = _select(name, ['Send it now', 'Delete'])
            if action == 0:
                try:
                    self.app.controller.send_command(device, name)
                    utils.notify('Sent %s' % name)
                except ControlError as exc:
                    utils.force_notify(str(exc))
            elif action == 1 and _dialog().yesno(
                    utils.ADDON_NAME, 'Delete the command "%s"?' % name):
                self.app.forget_command(device, name)
                utils.notify('Deleted %s' % name)

    def test_device(self, device):
        """Authenticate with an emitter and report the result plainly."""
        try:
            ok, message = self.app.test_device(device)
        except ControlError as exc:
            ok, message = False, str(exc)
        _dialog().ok('%s - %s' % (utils.ADDON_NAME,
                                  'connected' if ok else 'not connected'),
                     message)

    def learn_command(self, device, sleep_func=None):
        """Put the blaster into learning mode and wait for a remote press.

        The device has to be polled: it does not push the captured code, and
        it answers an error while it is still waiting, which is why a failed
        check means "keep waiting" rather than "give up".
        """
        import time

        sleep = sleep_func or time.sleep

        try:
            self.app.start_learning(device)
        except ControlError as exc:
            utils.force_notify(str(exc))
            return

        progress = xbmcgui.DialogProgress()
        progress.create(utils.ADDON_NAME,
                        'Point your remote at %s and press the button.'
                        % device.name,
                        'Cancel to give up.')
        code = None
        try:
            for step in range(LEARN_ATTEMPTS):
                progress.update(int(step * 100.0 / LEARN_ATTEMPTS))
                if progress.iscanceled():
                    break
                code = self.app.collect_learned(device)
                if code:
                    break
                sleep(LEARN_POLL)
        finally:
            progress.close()

        if not code:
            _dialog().ok(utils.ADDON_NAME,
                         'No code was captured.\n\nHold the remote close to '
                         '%s and press the button firmly while the dialog is '
                         'open.' % device.name)
            return

        name = _dialog().input('Name for this command', '')
        if not name or not name.strip():
            return
        if self.app.save_command(device, name.strip(), code):
            utils.notify('Learned %s' % name.strip())
        else:
            utils.force_notify('That command could not be saved')

    def _identify(self, device, sleep_func=None):
        """Blink a light so the user can tell which physical unit it is.

        Long enough to walk into the next room, and cancellable so it can be
        stopped the moment the light is spotted rather than standing there
        waiting for it to finish. The bulb is put back to the state it was in,
        which matters when it started out switched off -- otherwise
        identifying a light silently turns it on and leaves it on.
        """
        import time

        sleep = sleep_func or time.sleep
        before = self.app.controller.get_state(device)

        progress = xbmcgui.DialogProgress()
        progress.create(utils.ADDON_NAME, 'Flashing %s' % device.name,
                        'Cancel once you have spotted it.')
        try:
            try:
                for index in range(IDENTIFY_FLASHES):
                    progress.update(int(index * 100.0 / IDENTIFY_FLASHES))
                    if progress.iscanceled():
                        break
                    self.app.controller.turn(device, False)
                    sleep(IDENTIFY_GAP)
                    self.app.controller.turn(device, True)
                    sleep(IDENTIFY_GAP)
            finally:
                progress.close()
        except ControlError as exc:
            utils.force_notify(str(exc))
            return

        self._restore(device, before)
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
