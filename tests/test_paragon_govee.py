# -*- coding: utf-8 -*-
"""
Paragon Govee - test suite.

Runs off-device: Kodi is replaced by the stubs in tests/kodistubs, the Govee
LAN device is replaced by a UDP socket on loopback, and the Govee cloud is
replaced by a local HTTP server. That covers the protocol encoding, the
transport-selection logic and the scene engine without needing hardware.

    python3 tests/test_paragon_govee.py
"""

import json
import os
import shutil
import socket
import sys
import threading
import time
import unittest

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
except ImportError:  # pragma: no cover - Python 2 runner
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, 'kodistubs'))
sys.path.insert(0, os.path.join(ROOT, 'resources', 'lib'))
sys.path.insert(0, ROOT)

import xbmcaddon  # noqa: E402
import xbmcgui  # noqa: E402

import devices as devices_mod  # noqa: E402
import govee_cloud  # noqa: E402
import govee_lan  # noqa: E402
import scenes as scene_lib  # noqa: E402
from devices import ControlError, Device, GoveeController  # noqa: E402

PROFILE = xbmcaddon._PROFILE

# High ports so the suite never needs the privileged Govee ports or a real
# multicast-capable network.
TEST_SCAN_PORT = 44001
TEST_LISTEN_PORT = 44002
TEST_COMMAND_PORT = 44003


def gui_highlight():
    import gui
    return tuple(gui.HIGHLIGHT_COLOR)


def clean_profile():
    if os.path.isdir(PROFILE):
        shutil.rmtree(PROFILE)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeGoveeDevice(object):
    """A UDP socket that answers scan and devStatus like a real Govee light."""

    def __init__(self, device_id, sku, port, host='127.0.0.1'):
        self.device_id = device_id
        self.sku = sku
        self.host = host
        self.received = []
        self.state = {'onOff': 1, 'brightness': 42,
                      'color': {'r': 10, 'g': 20, 'b': 30},
                      'colorTemInKelvin': 0}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.settimeout(0.2)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve)
        self.thread.daemon = True
        self.thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                payload, address = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                message = json.loads(payload.decode('utf-8'))['msg']
            except (ValueError, KeyError, UnicodeDecodeError):
                continue

            self.received.append(message)
            cmd = message.get('cmd')
            if cmd == 'scan':
                self._reply(address, {'msg': {'cmd': 'scan', 'data': {
                    'ip': self.host, 'device': self.device_id,
                    'sku': self.sku, 'wifiVersionSoft': '1.02.03'}}})
            elif cmd == 'devStatus':
                self._reply(address, {'msg': {'cmd': 'devStatus',
                                              'data': self.state}})

    def _reply(self, address, message):
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Bind to this device's own address so the reply carries it as the
            # source. A real light answers from its own IP, and the transport
            # relies on that to tell which device a status reply belongs to.
            out.bind((self.host, 0))
            out.sendto(json.dumps(message).encode('utf-8'), address)
        finally:
            out.close()

    def commands(self, name):
        return [m for m in self.received if m.get('cmd') == name]

    def close(self):
        self._stop.set()
        self.thread.join(timeout=2)
        self.sock.close()


class _CloudHandler(BaseHTTPRequestHandler):
    """Stands in for developer-api.govee.com."""

    responses = {}
    calls = []

    def log_message(self, fmt, *args):
        pass

    def _respond(self):
        key = (self.command, self.path.split('?')[0])
        status, body = self.responses.get(key, (404, {'code': 404,
                                                      'message': 'no route'}))
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length) if length else b''
        self.calls.append({
            'method': self.command,
            'path': self.path,
            'api_key': self.headers.get('Govee-API-Key'),
            'body': json.loads(raw.decode('utf-8')) if raw else None,
        })
        payload = json.dumps(body).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _respond
    do_PUT = _respond


class FakeCloud(object):
    def __init__(self):
        self.server = HTTPServer(('127.0.0.1', 0), _CloudHandler)
        self.port = self.server.server_address[1]
        _CloudHandler.responses = {}
        _CloudHandler.calls = []
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    @property
    def base(self):
        return 'http://127.0.0.1:%d' % self.port

    def route(self, method, path, status, body):
        _CloudHandler.responses[(method, path)] = (status, body)

    @property
    def calls(self):
        return _CloudHandler.calls

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class RecordingController(object):
    """Stands in for GoveeController so scene ordering can be asserted."""

    def __init__(self, fail_on=None):
        self.calls = []
        self.fail_on = fail_on or set()

    def _record(self, name, device, *args):
        if device.device_id in self.fail_on:
            raise ControlError('%s is unreachable' % device.name)
        self.calls.append((name, device.device_id) + args)

    def turn(self, device, on):
        self._record('turn', device, on)

    def set_brightness(self, device, percent):
        self._record('brightness', device, percent)

    def set_color(self, device, r, g, b):
        self._record('color', device, r, g, b)

    def set_color_temp(self, device, kelvin):
        self._record('temp', device, kelvin)


# ---------------------------------------------------------------------------
# Scenes
# ---------------------------------------------------------------------------

class TestScenes(unittest.TestCase):

    def test_normalise_clamps_out_of_range_values(self):
        scene = scene_lib.normalise({
            'name': '  Bright  ', 'power': 'nonsense', 'brightness': 5000,
            'mode': 'weird', 'color': [999, -5, 'x'], 'kelvin': 99999,
            'targets': ['ab:cd'],
        })
        self.assertEqual(scene['name'], 'Bright')
        self.assertEqual(scene['power'], scene_lib.POWER_ON)
        self.assertEqual(scene['brightness'], 100)
        self.assertEqual(scene['mode'], scene_lib.MODE_NONE)
        self.assertEqual(scene['color'], [255, 255, 255])
        self.assertEqual(scene['kelvin'], 12000)
        self.assertEqual(scene['targets'], ['AB:CD'])

    def test_normalise_rejects_unusable_entries(self):
        self.assertIsNone(scene_lib.normalise(None))
        self.assertIsNone(scene_lib.normalise({'name': '   '}))
        self.assertIsNone(scene_lib.normalise({'power': 'on'}))
        self.assertIsNone(scene_lib.normalise('not a dict'))

    def test_normalise_keeps_none_brightness(self):
        scene = scene_lib.normalise({'name': 'Keep', 'brightness': None})
        self.assertIsNone(scene['brightness'])

    def test_normalise_all_drops_duplicates_and_junk(self):
        cleaned = scene_lib.normalise_all([
            {'name': 'One'}, {'name': 'one'}, 'junk', {'name': 'Two'},
        ])
        self.assertEqual([s['name'] for s in cleaned], ['One', 'Two'])

    def test_find_is_case_insensitive(self):
        found = scene_lib.find(scene_lib.default_scenes(), '  movie NIGHT ')
        self.assertEqual(found['name'], 'Movie Night')
        self.assertIsNone(scene_lib.find(scene_lib.default_scenes(), 'nope'))

    def test_default_scenes_all_survive_normalisation(self):
        defaults = scene_lib.default_scenes()
        self.assertEqual(len(scene_lib.normalise_all(defaults)), len(defaults))

    def test_apply_scene_orders_brightness_before_colour(self):
        controller = RecordingController()
        device = Device('AA:BB', name='Lamp', lan=True, ip='127.0.0.1')
        scene = scene_lib.make_scene('Test', brightness=30,
                                     mode=scene_lib.MODE_COLOR,
                                     color=[10, 20, 30])
        applied, errors = scene_lib.apply_scene(controller, scene, [device])

        self.assertEqual(applied, 1)
        self.assertEqual(errors, [])
        self.assertEqual([c[0] for c in controller.calls],
                         ['turn', 'brightness', 'color'])
        self.assertEqual(controller.calls[2], ('color', 'AA:BB', 10, 20, 30))

    def test_apply_scene_off_skips_everything_else(self):
        controller = RecordingController()
        device = Device('AA:BB', name='Lamp', lan=True)
        scene = scene_lib.make_scene('Off', power=scene_lib.POWER_OFF,
                                     brightness=50)
        scene_lib.apply_scene(controller, scene, [device])
        self.assertEqual(controller.calls, [('turn', 'AA:BB', False)])

    def test_apply_scene_keep_power_does_not_switch(self):
        controller = RecordingController()
        device = Device('AA:BB', name='Lamp', lan=True)
        scene = scene_lib.make_scene('Dim', power=scene_lib.POWER_KEEP,
                                     brightness=20)
        scene_lib.apply_scene(controller, scene, [device])
        self.assertEqual([c[0] for c in controller.calls], ['brightness'])

    def test_apply_scene_continues_past_a_failing_device(self):
        controller = RecordingController(fail_on={'BAD'})
        good = Device('GOOD', name='Good', lan=True)
        bad = Device('BAD', name='Bad', lan=True)
        scene = scene_lib.make_scene('Test', brightness=50)

        applied, errors = scene_lib.apply_scene(controller, scene, [bad, good])
        self.assertEqual(applied, 1)
        self.assertEqual(len(errors), 1)
        self.assertIn('Bad is unreachable', errors[0])

    def test_apply_scene_respects_targets(self):
        controller = RecordingController()
        one = Device('ONE', name='One', lan=True)
        two = Device('TWO', name='Two', lan=True)
        scene = scene_lib.make_scene('Only one', targets=['TWO'])
        scene_lib.apply_scene(controller, scene, [one, two])
        self.assertEqual({c[1] for c in controller.calls}, {'TWO'})

    def test_apply_scene_skips_unsupported_commands(self):
        controller = RecordingController()
        device = Device('AA:BB', name='Plug', lan=False, cloud=True,
                        supports=['turn'])
        scene = scene_lib.make_scene('Test', brightness=30,
                                     mode=scene_lib.MODE_TEMP, kelvin=3000)
        scene_lib.apply_scene(controller, scene, [device])
        self.assertEqual([c[0] for c in controller.calls], ['turn'])

    def test_apply_scene_ignores_disabled_devices(self):
        controller = RecordingController()
        device = Device('AA:BB', name='Lamp', lan=True, enabled=False)
        applied, errors = scene_lib.apply_scene(
            controller, scene_lib.make_scene('Test'), [device])
        self.assertEqual(applied, 0)
        self.assertEqual(controller.calls, [])
        self.assertTrue(errors)


class TestHexColours(unittest.TestCase):
    """The Govee app hands out 8-digit codes, so pasting one must work."""

    def test_six_and_three_digit_forms(self):
        self.assertEqual(scene_lib.parse_hex_color('FF8800')[0], (255, 136, 0))
        self.assertEqual(scene_lib.parse_hex_color('#FF8800')[0], (255, 136, 0))
        self.assertEqual(scene_lib.parse_hex_color('f80')[0], (255, 136, 0))
        self.assertEqual(scene_lib.parse_hex_color(' ff 88 00 ')[0],
                         (255, 136, 0))

    def test_leading_ff_is_read_as_alpha_first(self):
        rgb, note = scene_lib.parse_hex_color('FFFF2896')
        self.assertEqual(rgb, (255, 40, 150))
        self.assertIn('AARRGGBB', note)

    def test_real_govee_codes(self):
        """Straight out of the Govee app.

        The RRGGBBAA reading of these would mean alphas of 7F and 3C -- bulbs
        at 50% and 23% transparency -- so AARRGGBB is the only sensible one.
        """
        rgb, note = scene_lib.parse_hex_color('FF3C447F')
        self.assertEqual(rgb, (60, 68, 127))     # deep slate blue
        self.assertIn('AARRGGBB', note)

        rgb, note = scene_lib.parse_hex_color('FF7F3C3C')
        self.assertEqual(rgb, (127, 60, 60))     # deep brick red
        self.assertIn('AARRGGBB', note)

    def test_a_govee_code_whose_last_byte_is_ff_still_reads_as_argb(self):
        """The ambiguous default has to match Govee, not just be a coin flip.

        A Govee colour with blue at full lands on FF at both ends, and the
        alpha-first default is what keeps that correct.
        """
        rgb, note = scene_lib.parse_hex_color('FF3C44FF')
        self.assertEqual(rgb, (60, 68, 255))
        self.assertIn('ambiguous', note)

    def test_trailing_ff_is_read_as_alpha_last(self):
        rgb, note = scene_lib.parse_hex_color('2896FFFF')
        self.assertEqual(rgb, (40, 150, 255))
        self.assertIn('RRGGBBAA', note)

    def test_ambiguous_codes_default_to_alpha_first_and_say_so(self):
        """FF at both ends, or neither, cannot be told apart."""
        rgb, note = scene_lib.parse_hex_color('FF00FFFF')
        self.assertEqual(rgb, (0, 255, 255))
        self.assertIn('ambiguous', note)

        rgb, note = scene_lib.parse_hex_color('8000FF80')
        self.assertEqual(rgb, (0, 255, 128))
        self.assertIn('ambiguous', note)

    def test_six_digit_codes_carry_no_note(self):
        """Nothing was inferred, so there is nothing to warn about."""
        self.assertEqual(scene_lib.parse_hex_color('FF8800')[1], '')

    def test_rubbish_is_rejected_with_a_reason(self):
        for bad in ('nope', 'FF88', '', None, 'GGGGGG', 'FF8800AABB'):
            rgb, reason = scene_lib.parse_hex_color(bad)
            self.assertIsNone(rgb, bad)
            self.assertTrue(reason)

    def test_the_error_mentions_both_accepted_lengths(self):
        _rgb, reason = scene_lib.parse_hex_color('FF88')
        self.assertIn('6 or 8', reason)


class TestSceneCapture(unittest.TestCase):
    """Snapshotting the lights -- how a Govee Tap-to-Run gets into Kodi."""

    def test_state_to_settings_reads_a_temperature_bulb(self):
        settings = scene_lib.state_to_settings(
            {'power': 'on', 'brightness': 30, 'colorTem': 2200,
             'color': {'r': 0, 'g': 0, 'b': 0}})
        self.assertEqual(settings['mode'], scene_lib.MODE_TEMP)
        self.assertEqual(settings['kelvin'], 2200)
        self.assertEqual(settings['brightness'], 30)

    def test_state_to_settings_reads_a_colour_bulb(self):
        settings = scene_lib.state_to_settings(
            {'power': 'on', 'brightness': 80, 'colorTem': 0,
             'color': {'r': 255, 'g': 40, 'b': 10}})
        self.assertEqual(settings['mode'], scene_lib.MODE_COLOR)
        self.assertEqual(settings['color'], [255, 40, 10])

    def test_a_tinted_bulb_wins_over_a_stale_temperature(self):
        """Taking kelvin first would capture a pink bulb as white."""
        settings = scene_lib.state_to_settings(
            {'power': 'on', 'brightness': 60, 'colorTem': 3800,
             'color': {'r': 255, 'g': 40, 'b': 150}})
        self.assertEqual(settings['mode'], scene_lib.MODE_COLOR)
        self.assertEqual(settings['color'], [255, 40, 150])

    def test_white_rgb_beside_a_temperature_is_read_as_white(self):
        """A real H610A reading: 255,255,255 next to colorTem 3800."""
        settings = scene_lib.state_to_settings(
            {'power': 'on', 'brightness': 35, 'colorTem': 3800,
             'color': {'r': 255, 'g': 255, 'b': 255}})
        self.assertEqual(settings['mode'], scene_lib.MODE_TEMP)
        self.assertEqual(settings['kelvin'], 3800)

    def test_zero_rgb_beside_a_temperature_is_read_as_white(self):
        """A real H6008 reading: 0,0,0 next to colorTem 3800."""
        settings = scene_lib.state_to_settings(
            {'power': 'on', 'brightness': 1, 'colorTem': 3800,
             'color': {'r': 0, 'g': 0, 'b': 0}})
        self.assertEqual(settings['mode'], scene_lib.MODE_TEMP)

    def test_colour_with_no_temperature_is_kept(self):
        """A real H6008 reading: 141,95,255 with colorTem 0."""
        settings = scene_lib.state_to_settings(
            {'power': 'on', 'brightness': 1, 'colorTem': 0,
             'color': {'r': 141, 'g': 95, 'b': 255}})
        self.assertEqual(settings['mode'], scene_lib.MODE_COLOR)
        self.assertEqual(settings['color'], [141, 95, 255])

    def test_near_grey_is_not_mistaken_for_a_colour(self):
        settings = scene_lib.state_to_settings(
            {'power': 'on', 'brightness': 50, 'colorTem': 4000,
             'color': {'r': 250, 'g': 255, 'b': 248}})
        self.assertEqual(settings['mode'], scene_lib.MODE_TEMP)

    def test_state_to_settings_treats_off_as_off(self):
        settings = scene_lib.state_to_settings(
            {'power': 'off', 'brightness': 80, 'colorTem': 2700})
        self.assertEqual(settings['power'], scene_lib.POWER_OFF)

    def test_state_to_settings_rejects_unreadable_state(self):
        self.assertIsNone(scene_lib.state_to_settings(None))
        self.assertIsNone(scene_lib.state_to_settings({}))
        self.assertIsNone(scene_lib.state_to_settings({'power': 'unknown'}))

    def test_brightness_scale_is_detected_across_the_whole_capture(self):
        """One reading over 100 proves the 0-254 scale for every bulb."""
        wide = {'a': {'power': 'on', 'brightness': 203},
                'b': {'power': 'on', 'brightness': 51}}
        self.assertEqual(scene_lib.detect_brightness_scale(wide), 254)

        narrow = {'a': {'power': 'on', 'brightness': 80},
                  'b': {'power': 'on', 'brightness': 20}}
        self.assertEqual(scene_lib.detect_brightness_scale(narrow), 100)

        # Unreadable and absent entries must not confuse the detection.
        self.assertEqual(scene_lib.detect_brightness_scale(
            {'a': None, 'b': {'power': 'on'}, 'c': {'brightness': 'x'}}), 100)
        self.assertEqual(scene_lib.detect_brightness_scale({}), 100)

    def test_capture_rescales_every_bulb_once_the_scale_is_known(self):
        """The clamp symptom: two different levels both captured at 100%."""
        low = Device('AA:BB', name='Low', lan=True)
        high = Device('CC:DD', name='High', lan=True)
        states = {
            'AA:BB': {'power': 'on', 'brightness': 51, 'colorTem': 2700},
            'CC:DD': {'power': 'on', 'brightness': 203, 'colorTem': 2700},
        }
        scene, _c, _s = scene_lib.capture_scene('Wide', [low, high], states)

        self.assertEqual(scene['devices']['AA:BB']['brightness'], 20)
        self.assertEqual(scene['devices']['CC:DD']['brightness'], 80)

    def test_capture_leaves_documented_scale_readings_alone(self):
        low = Device('AA:BB', name='Low', lan=True)
        high = Device('CC:DD', name='High', lan=True)
        states = {
            'AA:BB': {'power': 'on', 'brightness': 20, 'colorTem': 2700},
            'CC:DD': {'power': 'on', 'brightness': 80, 'colorTem': 2700},
        }
        scene, _c, _s = scene_lib.capture_scene('Narrow', [low, high], states)

        self.assertEqual(scene['devices']['AA:BB']['brightness'], 20)
        self.assertEqual(scene['devices']['CC:DD']['brightness'], 80)

    def test_capture_skips_devices_that_did_not_answer(self):
        devices = [Device('AA:BB', name='One', lan=True),
                   Device('CC:DD', name='Two', lan=True)]
        states = {'AA:BB': {'power': 'on', 'brightness': 50, 'colorTem': 3000},
                  'CC:DD': None}

        scene, captured, skipped = scene_lib.capture_scene('Snap', devices,
                                                           states)
        self.assertEqual(captured, 1)
        self.assertEqual(skipped, ['Two'])
        self.assertEqual(scene['targets'], ['AA:BB'])

    def test_captured_scene_replays_each_light_differently(self):
        """The point of capture: one scene, 2 lights, 2 different states."""
        warm = Device('AA:BB', name='Warm', lan=True)
        red = Device('CC:DD', name='Red', lan=True)
        states = {
            'AA:BB': {'power': 'on', 'brightness': 30, 'colorTem': 2200},
            'CC:DD': {'power': 'on', 'brightness': 80, 'colorTem': 0,
                      'color': {'r': 255, 'g': 40, 'b': 10}},
        }
        scene, _captured, _skipped = scene_lib.capture_scene(
            'Twilight', [warm, red], states)

        controller = RecordingController()
        applied, errors = scene_lib.apply_scene(controller, scene, [warm, red])

        self.assertEqual(applied, 2)
        self.assertEqual(errors, [])
        self.assertIn(('brightness', 'AA:BB', 30), controller.calls)
        self.assertIn(('temp', 'AA:BB', 2200), controller.calls)
        self.assertIn(('brightness', 'CC:DD', 80), controller.calls)
        self.assertIn(('color', 'CC:DD', 255, 40, 10), controller.calls)

    def test_captured_off_lights_stay_off_on_replay(self):
        on = Device('AA:BB', name='On', lan=True)
        off = Device('CC:DD', name='Off', lan=True)
        states = {'AA:BB': {'power': 'on', 'brightness': 50, 'colorTem': 3000},
                  'CC:DD': {'power': 'off'}}
        scene, _c, _s = scene_lib.capture_scene('Mixed', [on, off], states)

        controller = RecordingController()
        scene_lib.apply_scene(controller, scene, [on, off])

        self.assertIn(('turn', 'CC:DD', False), controller.calls)
        self.assertNotIn(('brightness', 'CC:DD', 50), controller.calls)
        self.assertIn(('turn', 'AA:BB', True), controller.calls)

    def test_captured_scene_survives_a_json_round_trip(self):
        device = Device('AA:BB', name='One', lan=True)
        states = {'AA:BB': {'power': 'on', 'brightness': 30, 'colorTem': 2200}}
        scene, _c, _s = scene_lib.capture_scene('Snap', [device], states)

        restored = scene_lib.normalise(json.loads(json.dumps(scene)))
        self.assertEqual(restored['devices']['AA:BB']['kelvin'], 2200)
        self.assertEqual(restored['devices']['AA:BB']['brightness'], 30)

    def test_normalise_clamps_per_device_entries_too(self):
        scene = scene_lib.normalise({
            'name': 'Hand edited',
            'devices': {'aa:bb': {'power': 'on', 'brightness': 9000,
                                  'mode': 'nonsense', 'kelvin': -5},
                        'cc:dd': 'not a dict'},
        })
        self.assertEqual(list(scene['devices'].keys()), ['AA:BB'])
        self.assertEqual(scene['devices']['AA:BB']['brightness'], 100)
        self.assertEqual(scene['devices']['AA:BB']['mode'],
                         scene_lib.MODE_NONE)
        self.assertEqual(scene['devices']['AA:BB']['kelvin'], 1500)

    def test_uniform_scenes_still_apply_to_devices_with_no_entry(self):
        """A device added after a capture falls back to the scene defaults."""
        known = Device('AA:BB', name='Known', lan=True)
        newcomer = Device('EE:FF', name='New', lan=True)
        scene = scene_lib.make_scene(
            'Mixed', brightness=55, mode=scene_lib.MODE_TEMP, kelvin=3000,
            devices={'AA:BB': {'power': 'on', 'brightness': 10,
                               'mode': scene_lib.MODE_TEMP, 'kelvin': 2000,
                               'color': [255, 255, 255]}})

        controller = RecordingController()
        scene_lib.apply_scene(controller, scene, [known, newcomer])

        self.assertIn(('brightness', 'AA:BB', 10), controller.calls)
        self.assertIn(('brightness', 'EE:FF', 55), controller.calls)
        self.assertIn(('temp', 'EE:FF', 3000), controller.calls)

    def test_describe_summarises_a_captured_scene(self):
        devices = [Device('AA:BB', name='One', lan=True),
                   Device('CC:DD', name='Two', lan=True)]
        states = {'AA:BB': {'power': 'on', 'brightness': 50, 'colorTem': 3000},
                  'CC:DD': {'power': 'off'}}
        scene, _c, _s = scene_lib.capture_scene('Snap', devices, states)
        text = scene_lib.describe(scene)
        self.assertIn('captured', text)
        self.assertIn('2 light(s)', text)
        self.assertIn('1 on', text)


# ---------------------------------------------------------------------------
# Device model and transport selection
# ---------------------------------------------------------------------------

class TestDeviceModel(unittest.TestCase):

    def test_lan_device_gets_a_placeholder_name(self):
        device = Device('1F:80:C5:32:32:36:72:4E', model='H6159', lan=True)
        self.assertEqual(device.name, 'H6159 (724E)')

    def test_merge_prefers_the_cloud_name(self):
        lan = Device('AA:BB', model='H6159', ip='192.168.1.5', lan=True)
        cloud = Device('AA:BB', name='Living Room Strip', model='H6159',
                       cloud=True, supports=['turn', 'brightness'])
        lan.merge(cloud)

        self.assertEqual(lan.name, 'Living Room Strip')
        self.assertTrue(lan.lan and lan.cloud)
        self.assertEqual(lan.ip, '192.168.1.5')
        self.assertEqual(lan.supports, ['turn', 'brightness'])

    def test_merge_keeps_lan_placeholder_when_cloud_has_none(self):
        lan = Device('AA:BB', model='H6159', lan=True)
        other = Device('AA:BB', model='H6159', cloud=True)
        lan.merge(other)
        self.assertEqual(lan.name, 'H6159 (AABB)'.replace('AABB', 'AABB'))

    def test_supports_cmd_defaults_open_for_lan_devices(self):
        self.assertTrue(Device('AA:BB', lan=True).supports_cmd('colorTem'))
        limited = Device('AA:BB', cloud=True, supports=['turn'])
        self.assertTrue(limited.supports_cmd('turn'))
        self.assertFalse(limited.supports_cmd('colorTem'))

    def test_round_trips_through_json(self):
        device = Device('AA:BB', name='Lamp', model='H6159', ip='10.0.0.2',
                        lan=True, cloud=True, supports=['turn'],
                        temp_range=[2200, 6500], enabled=False)
        restored = Device.from_dict(json.loads(json.dumps(device.to_dict())))
        self.assertEqual(restored.to_dict(), device.to_dict())

    def test_auto_mode_prefers_lan(self):
        controller = GoveeController(lan=object(), cloud=None,
                                     mode=devices_mod.TRANSPORT_AUTO)
        both = Device('AA:BB', ip='10.0.0.2', lan=True, cloud=True)
        self.assertEqual(controller.pick_transport(both),
                         devices_mod.TRANSPORT_LAN)

    def test_lan_only_mode_ignores_cloud_devices(self):
        controller = GoveeController(lan=object(), cloud=object(),
                                     mode=devices_mod.TRANSPORT_LAN)
        cloud_only = Device('AA:BB', cloud=True)
        self.assertIsNone(controller.pick_transport(cloud_only))

    def test_cloud_only_mode_ignores_lan_devices(self):
        controller = GoveeController(lan=object(), cloud=object(),
                                     mode=devices_mod.TRANSPORT_CLOUD)
        lan_only = Device('AA:BB', ip='10.0.0.2', lan=True)
        self.assertIsNone(controller.pick_transport(lan_only))

    def test_unreachable_device_raises_a_readable_error(self):
        controller = GoveeController(lan=None, cloud=None)
        with self.assertRaises(ControlError) as caught:
            controller.turn(Device('AA:BB', name='Ghost'), True)
        self.assertIn('Ghost', str(caught.exception))


# ---------------------------------------------------------------------------
# LAN protocol
# ---------------------------------------------------------------------------

class TestLANMessages(unittest.TestCase):

    def test_turn_message(self):
        self.assertEqual(govee_lan.turn_message(True),
                         {'msg': {'cmd': 'turn', 'data': {'value': 1}}})
        self.assertEqual(govee_lan.turn_message(False)['msg']['data']['value'],
                         0)

    def test_colour_message_zeroes_the_temperature(self):
        data = govee_lan.color_message(1, 2, 3)['msg']['data']
        self.assertEqual(data['color'], {'r': 1, 'g': 2, 'b': 3})
        self.assertEqual(data['colorTemInKelvin'], 0)

    def test_temperature_message_zeroes_the_colour(self):
        data = govee_lan.color_temp_message(2700)['msg']['data']
        self.assertEqual(data['color'], {'r': 0, 'g': 0, 'b': 0})
        self.assertEqual(data['colorTemInKelvin'], 2700)

    def test_scan_message_shape(self):
        self.assertEqual(
            govee_lan.scan_message(),
            {'msg': {'cmd': 'scan', 'data': {'account_topic': 'reserve'}}})


class TestLANTransport(unittest.TestCase):
    """Drives the real transport against a fake light on loopback."""

    def setUp(self):
        self._saved = (govee_lan.MULTICAST_GROUP, govee_lan.SCAN_PORT,
                       govee_lan.LISTEN_PORT, govee_lan.COMMAND_PORT)
        govee_lan.MULTICAST_GROUP = '127.0.0.1'
        govee_lan.SCAN_PORT = TEST_SCAN_PORT
        govee_lan.LISTEN_PORT = TEST_LISTEN_PORT
        govee_lan.COMMAND_PORT = TEST_COMMAND_PORT

        self.scan_device = FakeGoveeDevice('AA:BB:CC:DD', 'H6159',
                                           TEST_SCAN_PORT)
        self.cmd_device = FakeGoveeDevice('AA:BB:CC:DD', 'H6159',
                                          TEST_COMMAND_PORT)
        self.transport = govee_lan.LANTransport(bind_address='127.0.0.1',
                                                retries=1)

    def tearDown(self):
        self.scan_device.close()
        self.cmd_device.close()
        (govee_lan.MULTICAST_GROUP, govee_lan.SCAN_PORT,
         govee_lan.LISTEN_PORT, govee_lan.COMMAND_PORT) = self._saved

    def test_discovery_parses_a_device_reply(self):
        found = self.transport.discover(timeout=1.5)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['device'], 'AA:BB:CC:DD')
        self.assertEqual(found[0]['sku'], 'H6159')
        self.assertEqual(found[0]['ip'], '127.0.0.1')

    def test_control_message_reaches_the_device(self):
        self.assertTrue(self.transport.send('127.0.0.1',
                                            govee_lan.brightness_message(77)))
        deadline = time.time() + 2
        while time.time() < deadline:
            if self.cmd_device.commands('brightness'):
                break
            time.sleep(0.05)
        sent = self.cmd_device.commands('brightness')
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]['data']['value'], 77)

    def test_status_round_trip(self):
        state = self.transport.status('127.0.0.1', timeout=1.5)
        self.assertIsNotNone(state)
        self.assertEqual(state['onOff'], 1)
        self.assertEqual(state['brightness'], 42)

    def test_retries_send_the_datagram_more_than_once(self):
        transport = govee_lan.LANTransport(bind_address='127.0.0.1', retries=3,
                                           retry_gap=0.01)
        transport.send('127.0.0.1', govee_lan.turn_message(True))
        deadline = time.time() + 2
        while time.time() < deadline:
            if len(self.cmd_device.commands('turn')) >= 3:
                break
            time.sleep(0.05)
        self.assertEqual(len(self.cmd_device.commands('turn')), 3)

    def test_probe_reports_a_successful_sweep(self):
        report = self.transport.probe(timeout=1.5)
        self.assertTrue(report['bound'])
        self.assertIsNone(report['bind_error'])
        self.assertTrue(any(err is None for _l, err in report['attempts']))
        self.assertTrue(report['raw_replies'])
        self.assertEqual(len(report['devices']), 1)
        self.assertEqual(report['devices'][0]['sku'], 'H6159')

    def test_probe_records_raw_replies_it_cannot_parse(self):
        """A non-Govee answer must show up, not be silently dropped."""
        noise = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(noise.close)

        original = self.scan_device._reply

        def reply_with_noise(address, message):
            original(address, message)
            noise.sendto(b'not json at all', address)

        self.scan_device._reply = reply_with_noise
        report = self.transport.probe(timeout=1.5)

        raw = ' '.join(text for _ip, text in report['raw_replies'])
        self.assertIn('not json at all', raw)
        self.assertEqual(len(report['devices']), 1)

    def test_scan_goes_out_on_more_than_one_path(self):
        """Multicast per interface plus a broadcast fallback."""
        sock = self.transport._make_socket(want_replies=True)
        try:
            attempts = self.transport._send_scan(sock)
        finally:
            sock.close()

        labels = [label for label, _error in attempts]
        self.assertIn('broadcast', labels)
        self.assertTrue(any(label.startswith('multicast') for label in labels))
        self.assertTrue(any(error is None for _label, error in attempts))

    def test_bind_failure_names_the_likely_culprit(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(blocker.close)
        message = self.transport._bind_error(socket.error('in use'))
        self.assertIn('4002'.replace('4002', str(govee_lan.LISTEN_PORT)),
                      message)
        self.assertIn('Govee Desktop', message)

    def test_local_addresses_excludes_loopback_and_link_local(self):
        for address in govee_lan.local_addresses():
            self.assertFalse(address.startswith('127.'), address)
            self.assertFalse(address.startswith('169.254.'), address)

    def test_bulk_status_queries_several_devices_in_one_pass(self):
        """25 bulbs must cost about one timeout, not 25 of them."""
        second = FakeGoveeDevice('EE:FF:00:11', 'H6008', TEST_COMMAND_PORT,
                                 host='127.0.0.2')
        self.addCleanup(second.close)
        second.state = {'onOff': 0, 'brightness': 10,
                        'color': {'r': 1, 'g': 2, 'b': 3},
                        'colorTemInKelvin': 2700}

        started = time.time()
        states = self.transport.status_many(['127.0.0.1', '127.0.0.2'],
                                            timeout=2.0)
        elapsed = time.time() - started

        self.assertEqual(sorted(states.keys()), ['127.0.0.1', '127.0.0.2'])
        self.assertEqual(states['127.0.0.1']['brightness'], 42)
        self.assertEqual(states['127.0.0.2']['colorTemInKelvin'], 2700)
        # Both answered, so it returns as soon as the set is complete rather
        # than sitting out the full window twice.
        self.assertLess(elapsed, 2.0)

    def test_bulk_status_returns_what_it_got_when_one_is_silent(self):
        states = self.transport.status_many(['127.0.0.1', '127.0.0.9'],
                                            timeout=1.0)
        self.assertIn('127.0.0.1', states)
        self.assertNotIn('127.0.0.9', states)

    def test_controller_get_states_maps_replies_onto_devices(self):
        controller = GoveeController(lan=self.transport, cloud=None,
                                     mode=devices_mod.TRANSPORT_LAN)
        device = Device('AA:BB:CC:DD', name='Lamp', lan=True, ip='127.0.0.1')
        ghost = Device('99:99', name='Ghost', lan=True, ip='127.0.0.9')

        states = controller.get_states([device, ghost], timeout=1.0)
        self.assertEqual(states['AA:BB:CC:DD']['power'], 'on')
        self.assertEqual(states['AA:BB:CC:DD']['brightness'], 42)
        self.assertIsNone(states['99:99'])

    def test_silent_lan_devices_do_not_trigger_a_serial_retry_storm(self):
        """Each fallback read re-binds port 4002 and sits out a timeout.

        With 25 bulbs, retrying every miss individually is half a minute of
        the UI apparently hung. status_many already retries internally.
        """
        calls = []
        real_status = self.transport.status

        def counting_status(ip, timeout=2.0):
            calls.append(ip)
            return real_status(ip, timeout=timeout)

        self.transport.status = counting_status
        controller = GoveeController(lan=self.transport, cloud=None,
                                     mode=devices_mod.TRANSPORT_LAN)
        ghosts = [Device('%02d' % i, name='Ghost %d' % i, lan=True,
                         ip='127.0.0.%d' % (20 + i)) for i in range(5)]

        started = time.time()
        states = controller.get_states(ghosts, timeout=0.6)
        elapsed = time.time() - started

        self.assertTrue(all(state is None for state in states.values()))
        self.assertEqual(calls, [], 'silent LAN devices were re-read serially')
        # Two bulk rounds at 0.6s, not five sequential 2s reads.
        self.assertLess(elapsed, 4.0)

    def test_controller_discovery_builds_devices(self):
        controller = GoveeController(lan=self.transport, cloud=None,
                                     mode=devices_mod.TRANSPORT_LAN)
        found, warnings = controller.discover(timeout=1.5)
        self.assertEqual(warnings, [])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].device_id, 'AA:BB:CC:DD')
        self.assertTrue(found[0].lan)
        self.assertEqual(found[0].name, 'H6159 (CCDD)')


# ---------------------------------------------------------------------------
# Cloud transport
# ---------------------------------------------------------------------------

class TestCloudTransport(unittest.TestCase):

    def setUp(self):
        self.cloud = FakeCloud()
        self._saved = (govee_cloud.DEVICES_ENDPOINT,
                       govee_cloud.CONTROL_ENDPOINT,
                       govee_cloud.STATE_ENDPOINT)
        govee_cloud.DEVICES_ENDPOINT = self.cloud.base + '/v1/devices'
        govee_cloud.CONTROL_ENDPOINT = self.cloud.base + '/v1/devices/control'
        govee_cloud.STATE_ENDPOINT = self.cloud.base + '/v1/devices/state'
        self.client = govee_cloud.CloudTransport('test-key', min_interval=0)

    def tearDown(self):
        (govee_cloud.DEVICES_ENDPOINT, govee_cloud.CONTROL_ENDPOINT,
         govee_cloud.STATE_ENDPOINT) = self._saved
        self.cloud.close()

    def test_list_devices_and_api_key_header(self):
        self.cloud.route('GET', '/v1/devices', 200, {
            'code': 200, 'message': 'Success',
            'data': {'devices': [{
                'device': 'AA:BB', 'model': 'H6159',
                'deviceName': 'Strip', 'controllable': True,
                'supportCmds': ['turn', 'brightness', 'color'],
                'properties': {'colorTem': {'range': {'min': 2000,
                                                      'max': 9000}}}}]}})
        found = self.client.list_devices()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['deviceName'], 'Strip')
        self.assertEqual(self.cloud.calls[0]['api_key'], 'test-key')

    def test_control_uses_put_with_the_documented_body(self):
        self.cloud.route('PUT', '/v1/devices/control', 200,
                         {'code': 200, 'message': 'Success'})
        self.client.control('AA:BB', 'H6159', 'brightness', 55)

        call = self.cloud.calls[0]
        self.assertEqual(call['method'], 'PUT')
        self.assertEqual(call['body'], {
            'device': 'AA:BB', 'model': 'H6159',
            'cmd': {'name': 'brightness', 'value': 55}})

    def test_state_is_flattened(self):
        self.cloud.route('GET', '/v1/devices/state', 200, {
            'code': 200, 'data': {'properties': [
                {'online': True}, {'powerState': 'on'}, {'brightness': 60}]}})
        state = self.client.state('AA:BB', 'H6159')
        self.assertEqual(state, {'online': True, 'powerState': 'on',
                                 'brightness': 60})

    def test_rate_limit_is_its_own_error(self):
        self.cloud.route('GET', '/v1/devices', 429, {'code': 429,
                                                     'message': 'slow down'})
        with self.assertRaises(govee_cloud.RateLimited):
            self.client.list_devices()

    def test_bad_key_is_reported_clearly(self):
        self.cloud.route('GET', '/v1/devices', 401, {'code': 401,
                                                     'message': 'bad key'})
        with self.assertRaises(govee_cloud.CloudError) as caught:
            self.client.list_devices()
        self.assertIn('API key', str(caught.exception))

    def test_missing_key_never_hits_the_network(self):
        client = govee_cloud.CloudTransport('', min_interval=0)
        self.assertFalse(client.configured)
        with self.assertRaises(govee_cloud.CloudError):
            client.list_devices()
        self.assertEqual(self.cloud.calls, [])

    def test_error_code_in_a_200_body_is_still_an_error(self):
        self.cloud.route('GET', '/v1/devices', 200, {'code': 400,
                                                     'message': 'nope'})
        with self.assertRaises(govee_cloud.CloudError):
            self.client.list_devices()

    def test_controller_merges_cloud_into_lan_results(self):
        self.cloud.route('GET', '/v1/devices', 200, {
            'code': 200, 'data': {'devices': [{
                'device': 'AA:BB:CC:DD', 'model': 'H6159',
                'deviceName': 'Living Room', 'controllable': True,
                'supportCmds': ['turn']}]}})

        class StubLAN(object):
            def discover(self, timeout=3.0):
                return [{'device': 'AA:BB:CC:DD', 'sku': 'H6159',
                         'ip': '10.0.0.9'}]

        controller = GoveeController(lan=StubLAN(), cloud=self.client,
                                     mode=devices_mod.TRANSPORT_AUTO)
        found, warnings = controller.discover()

        self.assertEqual(warnings, [])
        self.assertEqual(len(found), 1)
        device = found[0]
        self.assertEqual(device.name, 'Living Room')
        self.assertEqual(device.ip, '10.0.0.9')
        self.assertTrue(device.lan and device.cloud)

    def test_cloud_failure_does_not_hide_lan_devices(self):
        self.cloud.route('GET', '/v1/devices', 500, {'code': 500,
                                                     'message': 'boom'})

        class StubLAN(object):
            def discover(self, timeout=3.0):
                return [{'device': 'AA:BB', 'sku': 'H6159', 'ip': '10.0.0.9'}]

        controller = GoveeController(lan=StubLAN(), cloud=self.client)
        found, warnings = controller.discover()
        self.assertEqual(len(found), 1)
        self.assertEqual(len(warnings), 1)

    def test_lan_failure_does_not_hide_cloud_devices(self):
        self.cloud.route('GET', '/v1/devices', 200, {
            'code': 200, 'data': {'devices': [{'device': 'CC:DD',
                                               'model': 'H6104',
                                               'deviceName': 'Bar',
                                               'controllable': True}]}})

        class BrokenLAN(object):
            def discover(self, timeout=3.0):
                raise govee_lan.LANError('port 4002 is busy')

        controller = GoveeController(lan=BrokenLAN(), cloud=self.client)
        found, warnings = controller.discover()
        self.assertEqual(len(found), 1)
        self.assertIn('4002', warnings[0])


# ---------------------------------------------------------------------------
# Session: settings, caching, scene seeding
# ---------------------------------------------------------------------------

class TestSession(unittest.TestCase):

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        # addon_utils caches module-level state, so reload it per test.
        for name in ('addon_utils', 'paragon_govee'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def test_transport_mode_setting_maps_to_constants(self):
        from paragon_govee import ParagonGovee

        xbmcaddon.SETTINGS['transport_mode'] = '1'
        self.assertEqual(ParagonGovee.read_settings()['mode'],
                         devices_mod.TRANSPORT_LAN)

        xbmcaddon.SETTINGS['transport_mode'] = '2'
        self.assertEqual(ParagonGovee.read_settings()['mode'],
                         devices_mod.TRANSPORT_CLOUD)

        # Out-of-range values fall back rather than raising.
        xbmcaddon.SETTINGS['transport_mode'] = '99'
        self.assertEqual(ParagonGovee.read_settings()['mode'],
                         devices_mod.TRANSPORT_AUTO)

    def test_scenes_are_seeded_and_persisted_on_first_read(self):
        from paragon_govee import ParagonGovee

        app = ParagonGovee()
        names = [s['name'] for s in app.scenes]
        self.assertIn('Movie Night', names)
        self.assertTrue(os.path.isfile(os.path.join(PROFILE, 'scenes.json')))

        # A second session reads them back off disk unchanged.
        app2 = ParagonGovee()
        self.assertEqual([s['name'] for s in app2.scenes], names)

    def test_corrupt_scene_file_falls_back_to_defaults(self):
        from paragon_govee import ParagonGovee

        os.makedirs(PROFILE)
        handle = open(os.path.join(PROFILE, 'scenes.json'), 'w')
        handle.write('{ this is not json')
        handle.close()

        app = ParagonGovee()
        self.assertIn('Movie Night', [s['name'] for s in app.scenes])

    def test_device_cache_round_trips(self):
        from paragon_govee import ParagonGovee

        app = ParagonGovee()
        app._devices = [Device('AA:BB', name='Lamp', model='H6159',
                               ip='10.0.0.2', lan=True)]
        app.save_devices()

        reloaded = ParagonGovee()
        self.assertEqual(len(reloaded.devices), 1)
        self.assertEqual(reloaded.devices[0].name, 'Lamp')
        self.assertEqual(reloaded.device_by_id('aa:bb').ip, '10.0.0.2')

    def test_refresh_preserves_custom_name_and_disabled_flag(self):
        from paragon_govee import ParagonGovee

        app = ParagonGovee()
        app._devices = [Device('AA:BB', name='Behind the TV', model='H6159',
                               ip='10.0.0.2', lan=True, enabled=False)]
        app.save_devices()

        class StubController(object):
            def discover(self, timeout=3.0):
                return [Device('AA:BB', model='H6159', ip='10.0.0.7',
                               lan=True)], []

        app.controller = StubController()
        found, _warnings = app.refresh_devices()

        self.assertEqual(found[0].name, 'Behind the TV')
        self.assertFalse(found[0].enabled)
        self.assertEqual(found[0].ip, '10.0.0.7')

    def test_cloud_names_survive_switching_to_lan_only(self):
        """The exact round trip: name via cloud, then go LAN-only."""
        from paragon_govee import ParagonGovee

        app = ParagonGovee()

        # First refresh with the API key set: the cloud supplies real names.
        class CloudAndLan(object):
            def discover(self, timeout=3.0):
                device = Device('AA:BB', model='H6008', ip='10.0.0.11',
                                lan=True)
                device.merge(Device('AA:BB', name='KITCHEN RIGHT LOW',
                                    model='H6008', cloud=True))
                return [device], []

        app.controller = CloudAndLan()
        app.refresh_devices()
        self.assertEqual(app.devices[0].name, 'KITCHEN RIGHT LOW')

        # Now LAN only: discovery has no names and offers the placeholder.
        class LanOnly(object):
            def discover(self, timeout=3.0):
                return [Device('AA:BB', model='H6008', ip='10.0.0.11',
                               lan=True)], []

        reopened = ParagonGovee()          # reloads devices.json from disk
        reopened.controller = LanOnly()
        reopened.refresh_devices()

        self.assertEqual(reopened.devices[0].name, 'KITCHEN RIGHT LOW')
        self.assertFalse(reopened.devices[0].cloud)
        self.assertTrue(reopened.devices[0].lan)

        saved = json.load(open(os.path.join(PROFILE, 'devices.json')))
        self.assertEqual(saved[0]['name'], 'KITCHEN RIGHT LOW')

    def test_a_light_that_misses_one_search_keeps_its_name(self):
        """A sleeping bulb used to be erased along with its name."""
        from paragon_govee import ParagonGovee

        app = ParagonGovee()
        app._devices = [
            Device('AA:BB', name='KITCHEN RIGHT LOW', model='H6008',
                   ip='10.0.0.11', lan=True),
            Device('CC:DD', name='BEDROOM FRONT TOP', model='H6008',
                   ip='10.0.0.12', lan=True, enabled=False),
        ]
        app.save_devices()

        class OnlyOneAnswers(object):
            def discover(self, timeout=3.0):
                return [Device('AA:BB', model='H6008', ip='10.0.0.11',
                               lan=True)], []

        app.controller = OnlyOneAnswers()
        devices, _warnings = app.refresh_devices()

        self.assertEqual(len(devices), 2)
        names = sorted(d.name for d in devices)
        self.assertEqual(names, ['BEDROOM FRONT TOP', 'KITCHEN RIGHT LOW'])
        self.assertEqual(app.last_refresh_missing, 1)

        # The absent one keeps everything, including being disabled.
        absent = app.device_by_id('CC:DD')
        self.assertFalse(absent.enabled)
        self.assertEqual(absent.ip, '10.0.0.12')

    def test_a_forgotten_light_is_gone_from_disk(self):
        from paragon_govee import ParagonGovee

        app = ParagonGovee()
        keep = Device('AA:BB', name='Keep', lan=True)
        drop = Device('CC:DD', name='Drop', lan=True)
        app._devices = [keep, drop]
        app.save_devices()

        self.assertTrue(app.forget_device(drop))
        self.assertEqual([d.name for d in app.devices], ['Keep'])
        saved = json.load(open(os.path.join(PROFILE, 'devices.json')))
        self.assertEqual([d['name'] for d in saved], ['Keep'])

        # Forgetting something already gone is a no-op, not an error.
        self.assertFalse(app.forget_device(drop))

    def test_placeholder_names_are_still_replaced_by_discovery(self):
        """Preserving names must not freeze a light on its placeholder."""
        from paragon_govee import ParagonGovee

        app = ParagonGovee()
        app._devices = [Device('AA:BB', model='H6008', lan=True)]
        self.assertEqual(app.devices[0].name, 'H6008 (AABB)')
        app.save_devices()

        class CloudNames(object):
            def discover(self, timeout=3.0):
                return [Device('AA:BB', name='GREATROOM BACK TOP',
                               model='H6008', cloud=True)], []

        app.controller = CloudNames()
        app.refresh_devices()
        self.assertEqual(app.devices[0].name, 'GREATROOM BACK TOP')

    def test_apply_scene_by_name_reports_a_missing_scene(self):
        from paragon_govee import ParagonGovee

        app = ParagonGovee()
        self.assertFalse(app.apply_scene_by_name('Does Not Exist'))
        self.assertTrue(any('Does Not Exist' in message
                            for _heading, message in xbmcgui.NOTIFICATIONS))

    def test_toggle_turns_the_group_off_when_any_light_is_on(self):
        from paragon_govee import ParagonGovee

        app = ParagonGovee()
        one = Device('ONE', name='One', lan=True, ip='10.0.0.1')
        two = Device('TWO', name='Two', lan=True, ip='10.0.0.2')
        app._devices = [one, two]

        recorder = RecordingController()
        recorder.get_state = lambda d: {'power': 'on'} if d is one else \
            {'power': 'off'}
        app.controller = recorder

        done, errors = app.toggle_all()
        self.assertEqual(done, 2)
        self.assertEqual(errors, [])
        self.assertTrue(all(call[2] is False for call in recorder.calls))

    def test_toggle_turns_on_when_no_state_can_be_read(self):
        from paragon_govee import ParagonGovee

        app = ParagonGovee()
        app._devices = [Device('ONE', name='One', lan=True, ip='10.0.0.1')]
        recorder = RecordingController()
        recorder.get_state = lambda d: None
        app.controller = recorder

        app.toggle_all()
        self.assertTrue(all(call[2] is True for call in recorder.calls))

    def test_notifications_can_be_switched_off(self):
        import addon_utils

        xbmcaddon.SETTINGS['show_notifications'] = 'false'
        addon_utils.notify('quiet please')
        self.assertEqual(xbmcgui.NOTIFICATIONS, [])

        # Errors still get through, because they need acting on.
        addon_utils.force_notify('this one matters')
        self.assertEqual(len(xbmcgui.NOTIFICATIONS), 1)

    def test_settings_helpers_coerce_types(self):
        import addon_utils

        xbmcaddon.SETTINGS.update({'a': 'true', 'b': 'False', 'c': '12',
                                   'd': 'not a number'})
        self.assertTrue(addon_utils.get_bool('a'))
        self.assertFalse(addon_utils.get_bool('b'))
        self.assertTrue(addon_utils.get_bool('missing', True))
        self.assertEqual(addon_utils.get_int('c'), 12)
        self.assertEqual(addon_utils.get_int('d', 7), 7)
        self.assertEqual(addon_utils.get_int('missing', 5), 5)


# ---------------------------------------------------------------------------
# RunScript argument handling
# ---------------------------------------------------------------------------

class TestScriptArguments(unittest.TestCase):

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_govee', 'default'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def test_parses_comma_and_ampersand_separators(self):
        import default

        self.assertEqual(default.parse_args(['action=toggle']),
                         {'action': 'toggle'})
        self.assertEqual(
            default.parse_args(['action=scene', 'name=Movie Night']),
            {'action': 'scene', 'name': 'Movie Night'})
        self.assertEqual(
            default.parse_args(['action=pick_scene&setting=scene_playing']),
            {'action': 'pick_scene', 'setting': 'scene_playing'})
        self.assertEqual(default.parse_args([]), {})
        self.assertEqual(default.parse_args(['garbage']), {})

    def test_hex_parsing_accepts_short_and_long_forms(self):
        import default

        self.assertEqual(default._parse_hex('#FF8800'), (255, 136, 0))
        self.assertEqual(default._parse_hex('f80'), (255, 136, 0))
        self.assertIsNone(default._parse_hex('nope'))
        self.assertIsNone(default._parse_hex(''))

    def test_target_resolves_by_name_and_by_id(self):
        import default
        from paragon_govee import ParagonGovee

        app = ParagonGovee()
        app._devices = [Device('AA:BB', name='Living Room', lan=True)]

        self.assertIsNone(default.resolve_targets(app, 'all'))
        self.assertIsNone(default.resolve_targets(app, ''))
        self.assertEqual(len(default.resolve_targets(app, 'living room')), 1)
        self.assertEqual(len(default.resolve_targets(app, 'aa:bb')), 1)
        self.assertEqual(default.resolve_targets(app, 'nowhere'), [])

    def test_unknown_target_is_reported_and_nothing_is_sent(self):
        import addon_utils
        import default
        from paragon_govee import ParagonGovee

        app = ParagonGovee()
        recorder = RecordingController()
        app._devices = [Device('AA:BB', name='Living Room', lan=True)]
        app.controller = recorder

        handled = default.run_action(app, {'action': 'on', 'target': 'nope'},
                                     addon_utils)
        self.assertTrue(handled)
        self.assertEqual(recorder.calls, [])
        self.assertTrue(any('nope' in message
                            for _h, message in xbmcgui.NOTIFICATIONS))

    def test_actions_drive_the_controller(self):
        import addon_utils
        import default
        from paragon_govee import ParagonGovee

        app = ParagonGovee()
        recorder = RecordingController()
        app._devices = [Device('AA:BB', name='Lamp', lan=True)]
        app.controller = recorder

        default.run_action(app, {'action': 'on'}, addon_utils)
        default.run_action(app, {'action': 'brightness', 'value': '25'},
                           addon_utils)
        default.run_action(app, {'action': 'color', 'value': 'FF8800'},
                           addon_utils)
        default.run_action(app, {'action': 'temp', 'value': '2700'},
                           addon_utils)

        self.assertEqual([c[0] for c in recorder.calls],
                         ['turn', 'brightness', 'color', 'temp'])
        self.assertEqual(recorder.calls[1][2], 25)
        self.assertEqual(recorder.calls[2][2:], (255, 136, 0))
        self.assertEqual(recorder.calls[3][2], 2700)

    def test_out_of_range_values_are_clamped(self):
        import addon_utils
        import default
        from paragon_govee import ParagonGovee

        app = ParagonGovee()
        recorder = RecordingController()
        app._devices = [Device('AA:BB', name='Lamp', lan=True)]
        app.controller = recorder

        default.run_action(app, {'action': 'brightness', 'value': '900'},
                           addon_utils)
        self.assertEqual(recorder.calls[0][2], 100)

    def test_bad_value_is_reported_not_sent(self):
        import addon_utils
        import default
        from paragon_govee import ParagonGovee

        app = ParagonGovee()
        recorder = RecordingController()
        app._devices = [Device('AA:BB', name='Lamp', lan=True)]
        app.controller = recorder

        default.run_action(app, {'action': 'brightness', 'value': 'loads'},
                           addon_utils)
        self.assertEqual(recorder.calls, [])
        self.assertTrue(xbmcgui.NOTIFICATIONS)

    def test_no_action_falls_through_to_the_panel(self):
        import addon_utils
        import default
        from paragon_govee import ParagonGovee

        app = ParagonGovee()
        self.assertFalse(default.run_action(app, {}, addon_utils))
        self.assertFalse(default.run_action(app, {'action': 'panel'},
                                            addon_utils))


# ---------------------------------------------------------------------------
# Playback service decisions
# ---------------------------------------------------------------------------

class TestPlaybackService(unittest.TestCase):

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        import xbmc
        xbmc.Player.playing_video = False
        xbmc.Player.playing_audio = False
        xbmc.COND_VISIBILITY.clear()
        for name in ('addon_utils', 'paragon_govee', 'service'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def test_content_filter_video_only(self):
        import xbmc
        import service

        xbmcaddon.SETTINGS['sync_content'] = '0'
        xbmc.Player.playing_audio = True
        self.assertFalse(service.GoveeService._content_allowed())

        xbmc.Player.playing_video = True
        self.assertTrue(service.GoveeService._content_allowed())

    def test_content_filter_video_and_music(self):
        import xbmc
        import service

        xbmcaddon.SETTINGS['sync_content'] = '1'
        xbmc.Player.playing_audio = True
        self.assertTrue(service.GoveeService._content_allowed())

    def test_content_filter_everything_needs_no_player(self):
        import service

        xbmcaddon.SETTINGS['sync_content'] = '2'
        self.assertTrue(service.GoveeService._content_allowed())

    def test_fullscreen_filter(self):
        import xbmc
        import service

        xbmcaddon.SETTINGS['sync_fullscreen_only'] = 'false'
        self.assertTrue(service.GoveeService._fullscreen_ok())

        xbmcaddon.SETTINGS['sync_fullscreen_only'] = 'true'
        self.assertFalse(service.GoveeService._fullscreen_ok())

        xbmc.COND_VISIBILITY['VideoPlayer.IsFullscreen'] = True
        self.assertTrue(service.GoveeService._fullscreen_ok())

    def test_stop_does_not_restore_when_we_never_dimmed(self):
        import service

        xbmcaddon.SETTINGS.update({'playback_sync': 'true',
                                   'scene_stopped': 'Lights Up'})
        svc = service.GoveeService()
        applied = []
        svc._app = type('App', (), {
            'apply_scene_by_name': lambda self, name, announce=True:
                applied.append(name) or True})()

        svc._we_dimmed = False
        svc.handle(service.EVENT_STOP)
        self.assertEqual(applied, [])

    def test_stop_restores_after_we_dimmed(self):
        import service

        xbmcaddon.SETTINGS.update({'playback_sync': 'true',
                                   'scene_stopped': 'Lights Up'})
        svc = service.GoveeService()
        applied = []
        svc._app = type('App', (), {
            'apply_scene_by_name': lambda self, name, announce=True:
                applied.append(name) or True})()

        svc._we_dimmed = True
        svc.handle(service.EVENT_STOP)
        self.assertEqual(applied, ['Lights Up'])
        self.assertFalse(svc._we_dimmed)

    def test_nothing_happens_while_sync_is_off(self):
        import service

        xbmcaddon.SETTINGS['playback_sync'] = 'false'
        svc = service.GoveeService()
        applied = []
        svc._app = type('App', (), {
            'apply_scene_by_name': lambda self, name, announce=True:
                applied.append(name) or True})()

        svc._we_dimmed = True
        svc.handle(service.EVENT_STOP)
        self.assertEqual(applied, [])

    def test_player_callbacks_only_queue(self):
        import service

        svc = service.GoveeService()
        svc.player.onPlayBackPaused()
        self.assertEqual(svc._pending, service.EVENT_PAUSE)
        svc.player.onPlayBackStopped()
        self.assertEqual(svc._pending, service.EVENT_STOP)

    def test_player_subclass_takes_no_constructor_arguments(self):
        """Kodi parses Player() arguments in the base type.

        Passing anything to the subclass constructor fails with
        "an integer is required" before the subclass __init__ runs, which is
        what crashed the service on Krypton. The service must construct the
        player bare and attach itself afterwards.
        """
        import service

        with self.assertRaises(TypeError):
            service.GoveePlayer(object())

        player = service.GoveePlayer()
        self.assertIsNone(player.service)

        svc = service.GoveeService()
        self.assertIs(svc.player.service, svc)

    def test_callbacks_are_inert_before_attach_and_after_detach(self):
        import service

        player = service.GoveePlayer()
        player.onPlayBackStarted()  # must not raise on a bare player

        svc = service.GoveeService()
        svc.player.onPlayBackStarted()
        self.assertEqual(svc._pending, service.EVENT_PLAY)

        svc._pending = None
        svc.player.service = None
        svc.player.onPlayBackStopped()
        self.assertIsNone(svc._pending)


# ---------------------------------------------------------------------------
# Diagnostics
#
# The whole point of this module is telling apart failures that look identical
# from the control panel, so each verdict gets its own case.
# ---------------------------------------------------------------------------

class TestDiagnostics(unittest.TestCase):

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_govee', 'diagnostics'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    @staticmethod
    def report(**overrides):
        base = {
            'addresses': ['192.168.1.50'], 'bind_address': '',
            'listen_port': 4002, 'bound': True, 'bind_error': None,
            'attempts': [('multicast via 192.168.1.50', None),
                         ('broadcast', None)],
            'raw_replies': [], 'devices': [],
        }
        base.update(overrides)
        return base

    def test_port_busy_is_named_as_such(self):
        import diagnostics

        report = self.report(bound=False, bind_error='port in use')
        self.assertEqual(diagnostics.classify(report),
                         diagnostics.CAUSE_PORT_BUSY)
        report['cause'] = diagnostics.CAUSE_PORT_BUSY
        text = diagnostics.summary(report)
        self.assertIn('Govee Desktop', text)

    def test_send_failure_is_distinguished_from_silence(self):
        import diagnostics

        report = self.report(attempts=[('multicast via eth0', 'no route'),
                                       ('broadcast', 'not permitted')])
        self.assertEqual(diagnostics.classify(report),
                         diagnostics.CAUSE_NO_SEND)
        report['cause'] = diagnostics.CAUSE_NO_SEND
        self.assertIn('no route', diagnostics.summary(report))

    def test_silence_suggests_model_support_firewall_and_lan_toggle(self):
        import diagnostics

        report = self.report()
        self.assertEqual(diagnostics.classify(report),
                         diagnostics.CAUSE_NO_REPLIES)
        report['cause'] = diagnostics.CAUSE_NO_REPLIES
        text = diagnostics.summary(report)
        self.assertIn('do not support the Govee LAN API', text)
        self.assertIn('firewall', text)
        self.assertIn('LAN Control', text)
        self.assertIn('192.168.1.50', text)

    def test_unparsed_replies_are_their_own_verdict(self):
        import diagnostics

        report = self.report(raw_replies=[('192.168.1.9', 'HTTP/1.1 200 OK')])
        self.assertEqual(diagnostics.classify(report),
                         diagnostics.CAUSE_UNPARSED)
        report['cause'] = diagnostics.CAUSE_UNPARSED
        self.assertIn('none was a Govee scan response',
                      diagnostics.summary(report))

    def test_success_lists_the_models_found(self):
        import diagnostics

        report = self.report(
            raw_replies=[('192.168.1.9', '{}')],
            devices=[{'device': 'AA:BB', 'sku': 'H6159', 'ip': '192.168.1.9'},
                     {'device': 'CC:DD', 'sku': 'H6104', 'ip': '192.168.1.10'}])
        self.assertEqual(diagnostics.classify(report), diagnostics.CAUSE_OK)
        report['cause'] = diagnostics.CAUSE_OK
        text = diagnostics.summary(report)
        self.assertIn('H6104', text)
        self.assertIn('H6159', text)

    def test_log_lines_carry_the_detail_needed_to_debug(self):
        import diagnostics

        report = self.report(
            raw_replies=[('192.168.1.9', 'garbage')],
            devices=[{'device': 'AA:BB', 'sku': 'H6159', 'ip': '192.168.1.9'}])
        report['mode'] = 'auto'
        report['api_key_set'] = False
        report['cause'] = diagnostics.CAUSE_OK

        text = '\n'.join(diagnostics.format_lines(report))
        self.assertIn('192.168.1.50', text)      # interfaces enumerated
        self.assertIn('Listening on UDP 4002', text)
        self.assertIn('garbage', text)           # raw reply preserved
        self.assertIn('H6159', text)
        self.assertIn('Verdict', text)

    def test_run_writes_to_the_log_and_returns_a_summary(self):
        import diagnostics
        import xbmc
        from paragon_govee import ParagonGovee

        app = ParagonGovee()

        class StubLAN(object):
            def probe(self, timeout=4.0):
                return TestDiagnostics.report()

        app.controller.lan = StubLAN()
        del xbmc.LOG_LINES[:]

        text, report = diagnostics.run(app)
        self.assertEqual(report['cause'], diagnostics.CAUSE_NO_REPLIES)
        self.assertIn('nothing answered', text)
        logged = '\n'.join(message for _level, message in xbmc.LOG_LINES)
        self.assertIn('Paragon Govee LAN diagnostics', logged)


class TestStatusRoundTrip(unittest.TestCase):
    """Does this model report back what it was set to?

    Capture, Toggle and Show status all trust devStatus. When a model reports
    a fixed or long-stale payload, all three are quietly wrong, and no single
    capture can tell that apart from a light something else had set.
    """

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_govee', 'diagnostics'):
            if name in sys.modules:
                del sys.modules[name]

        from paragon_govee import ParagonGovee
        self.app = ParagonGovee()
        self.device = Device('AA:BB', name='Lamp', model='H6008', lan=True,
                             ip='10.0.0.11')
        self.app._devices = [self.device]

    def tearDown(self):
        clean_profile()

    def _controller(self, readback):
        recorder = RecordingController()
        states = [{'power': 'on', 'brightness': 1, 'colorTem': 3800,
                   'color': {'r': 0, 'g': 0, 'b': 0}}, readback]

        def get_state(device):
            return states.pop(0) if states else readback

        recorder.get_state = get_state
        self.app.controller = recorder
        return recorder

    def test_a_bulb_that_reports_the_probe_back_is_trustworthy(self):
        import diagnostics

        self._controller({'power': 'on', 'brightness': 40, 'colorTem': 0,
                          'color': {'r': 255, 'g': 0, 'b': 255}})
        report = diagnostics.verify_status(self.app, self.device,
                                           sleep_func=lambda _s: None)

        self.assertEqual(report['verdict'], diagnostics.VERDICT_TRACKS)
        self.assertIn('reports back what it was set to',
                      diagnostics.verify_summary(report))

    def test_a_bulb_stuck_on_a_stale_payload_is_called_out(self):
        import diagnostics

        # The real H6008 behaviour under investigation: set to magenta, still
        # reporting 0,0,0 at 3800K.
        self._controller({'power': 'on', 'brightness': 1, 'colorTem': 3800,
                          'color': {'r': 0, 'g': 0, 'b': 0}})
        report = diagnostics.verify_status(self.app, self.device,
                                           sleep_func=lambda _s: None)

        self.assertEqual(report['verdict'], diagnostics.VERDICT_STALE)
        text = diagnostics.verify_summary(report)
        self.assertIn('did NOT report back', text)
        self.assertIn('Capture cannot work', text)

    def test_no_readback_is_its_own_verdict(self):
        import diagnostics

        self._controller(None)
        report = diagnostics.verify_status(self.app, self.device,
                                           sleep_func=lambda _s: None)
        self.assertEqual(report['verdict'], diagnostics.VERDICT_NO_READBACK)
        self.assertIn('4002', diagnostics.verify_summary(report))

    def test_a_bulb_that_cannot_be_driven_is_distinguished(self):
        import diagnostics

        recorder = RecordingController(fail_on={'AA:BB'})
        recorder.get_state = lambda device: None
        self.app.controller = recorder

        report = diagnostics.verify_status(self.app, self.device,
                                           sleep_func=lambda _s: None)
        self.assertEqual(report['verdict'],
                         diagnostics.VERDICT_CONTROL_FAILED)
        self.assertIn('Could not drive', diagnostics.verify_summary(report))

    def test_probe_avoids_the_colour_the_bulb_already_shows(self):
        """Probing with the current colour could not tell stale from correct."""
        import diagnostics

        already = {'power': 'on', 'brightness': 40, 'colorTem': 0,
                   'color': {'r': 255, 'g': 0, 'b': 255}}
        recorder = RecordingController()
        recorder.get_state = lambda device: already
        self.app.controller = recorder

        report = diagnostics.verify_status(self.app, self.device,
                                           sleep_func=lambda _s: None)
        self.assertEqual(report['probe'], diagnostics.PROBE_ALT)

    def test_the_bulb_is_put_back_after_the_probe(self):
        import diagnostics

        recorder = self._controller(
            {'power': 'on', 'brightness': 40, 'colorTem': 0,
             'color': {'r': 255, 'g': 0, 'b': 255}})
        diagnostics.verify_status(self.app, self.device,
                                  sleep_func=lambda _s: None)

        # Last colour-ish command should restore the original 3800K white,
        # not leave the bulb sitting on the probe colour.
        temps = [c for c in recorder.calls if c[0] == 'temp']
        self.assertTrue(temps, 'bulb was not restored')
        self.assertEqual(temps[-1][2], 3800)


# ---------------------------------------------------------------------------
# Control panel menu walks
#
# The menus are built by appending to a list and then indexing back into it,
# which is exactly the kind of arithmetic that silently drifts when an entry is
# added. These walk the real menus with scripted dialog answers.
# ---------------------------------------------------------------------------

class TestControlPanel(unittest.TestCase):

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_govee', 'gui'):
            if name in sys.modules:
                del sys.modules[name]

        from paragon_govee import ParagonGovee
        self.app = ParagonGovee()
        self.recorder = RecordingController()
        self.app._devices = [Device('AA:BB', name='Lamp', lan=True,
                                    ip='10.0.0.2')]
        self.app.controller = self.recorder

    def tearDown(self):
        clean_profile()

    def panel(self):
        import gui
        return gui.ControlPanel(self.app)

    def test_main_menu_group_then_on(self):
        # 0 = "All Lights", then 1 = "On" in the control menu.
        xbmcgui.SELECT_QUEUE.extend([0, 1])
        self.panel().run()
        self.assertEqual(self.recorder.calls, [('turn', 'AA:BB', True)])

    def test_main_menu_device_row_selects_that_device(self):
        self.app._devices.append(Device('CC:DD', name='Strip', lan=True,
                                        ip='10.0.0.3'))
        # 0 = All Lights, 1 = first device, 2 = second device.
        xbmcgui.SELECT_QUEUE.extend([2, 2])  # second device, then "Off"
        self.panel().run()
        self.assertEqual(self.recorder.calls, [('turn', 'CC:DD', False)])

    def test_every_control_row_does_what_its_label_says(self):
        """Walk each row by label and assert the command that came out."""
        expected = {
            'On': [('turn', 'AA:BB', True)],
            'Off': [('turn', 'AA:BB', False)],
            'Toggle': [('turn', 'AA:BB', True)],  # no state readable -> on
        }
        self.recorder.get_state = lambda d: None

        # Discover the row order the same way the user sees it.
        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().control_menu(None, 'All Lights')
        labels = xbmcgui.SELECT_CALLS[-1][1]

        for label, want in expected.items():
            del self.recorder.calls[:]
            xbmcgui.reset()
            xbmcgui.SELECT_QUEUE.extend([labels.index(label)])
            self.panel().control_menu(None, 'All Lights')
            self.assertEqual(self.recorder.calls, want,
                             'row "%s" did not do what it says' % label)

    def test_status_row_is_only_offered_for_a_single_device(self):
        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().control_menu(None, 'All Lights')
        self.assertNotIn('Show status', xbmcgui.SELECT_CALLS[-1][1])

        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().control_menu([self.app.devices[0]], 'Lamp')
        self.assertIn('Show status', xbmcgui.SELECT_CALLS[-1][1])

    def test_main_menu_offers_capture_and_shows_the_version(self):
        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().run()

        heading, labels = xbmcgui.SELECT_CALLS[-1]
        self.assertIn('Capture lights as a scene...', labels)
        self.assertIn(xbmcaddon._INFO['version'], heading)

    def test_main_menu_capture_row_reaches_capture(self):
        self.recorder.get_states = lambda devices, timeout=3.0: {
            'AA:BB': {'power': 'on', 'brightness': 30, 'colorTem': 2200}}

        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().run()
        labels = xbmcgui.SELECT_CALLS[-1][1]

        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend(
            [labels.index('Capture lights as a scene...')])
        xbmcgui.INPUT_QUEUE.append('From Main Menu')
        self.panel().main_menu()

        self.assertIsNotNone(self.app.scene_by_name('From Main Menu'))

    def test_every_device_row_targets_its_own_light(self):
        """Guards against a loop-variable closure pointing all rows at one light."""
        self.app._devices = [
            Device('AA:BB', name='One', lan=True, ip='10.0.0.1'),
            Device('CC:DD', name='Two', lan=True, ip='10.0.0.2'),
            Device('EE:FF', name='Three', lan=True, ip='10.0.0.3'),
        ]

        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().main_menu()
        labels = xbmcgui.SELECT_CALLS[-1][1]

        for device in self.app.devices:
            row = [i for i, label in enumerate(labels)
                   if label.startswith(device.name)][0]
            del self.recorder.calls[:]
            xbmcgui.reset()
            # Select the device row, then "Off" inside the control menu.
            xbmcgui.SELECT_QUEUE.extend([row, 2])
            self.panel().main_menu()
            self.assertEqual(self.recorder.calls,
                             [('turn', device.device_id, False)],
                             'row for %s drove the wrong light' % device.name)

    def test_brightness_preset_index_maps_to_the_shown_percentage(self):
        import gui

        # BRIGHTNESS_STEPS[3] is 30%, and the label at that index says "30%".
        self.assertEqual(gui.BRIGHTNESS_STEPS[3], 30)
        xbmcgui.SELECT_QUEUE.extend([3])
        self.panel().brightness_menu(None, 'All Lights')
        self.assertEqual(self.recorder.calls, [('brightness', 'AA:BB', 30)])

    def test_brightness_custom_entry_is_clamped(self):
        import gui

        xbmcgui.SELECT_QUEUE.extend([len(gui.BRIGHTNESS_STEPS)])
        xbmcgui.INPUT_QUEUE.append('250')
        self.panel().brightness_menu(None, 'All Lights')
        self.assertEqual(self.recorder.calls, [('brightness', 'AA:BB', 100)])

    def test_colour_preset_index_matches_its_label(self):
        import gui

        index = [n for n, _rgb in gui.COLOR_PRESETS].index('Paragon Purple')
        xbmcgui.SELECT_QUEUE.extend([index])
        self.panel().color_menu(None, 'All Lights')
        expected = gui.COLOR_PRESETS[index][1]
        self.assertEqual(self.recorder.calls,
                         [('color', 'AA:BB') + tuple(expected)])

    def test_colour_custom_hex(self):
        import gui

        xbmcgui.SELECT_QUEUE.extend([len(gui.COLOR_PRESETS)])
        xbmcgui.INPUT_QUEUE.append('#00FF00')
        self.panel().color_menu(None, 'All Lights')
        self.assertEqual(self.recorder.calls, [('color', 'AA:BB', 0, 255, 0)])

    def test_eight_digit_govee_code_reaches_the_lights(self):
        import gui

        xbmcgui.SELECT_QUEUE.extend([len(gui.COLOR_PRESETS)])
        xbmcgui.INPUT_QUEUE.append('FFFF2896')
        self.panel().color_menu(None, 'All Lights')

        self.assertEqual(self.recorder.calls,
                         [('color', 'AA:BB', 255, 40, 150)])
        shown = ' '.join(message for _h, message in xbmcgui.NOTIFICATIONS)
        self.assertIn('AARRGGBB', shown)

    def test_an_eight_digit_code_can_be_saved_into_a_scene(self):
        panel = self.panel()
        # Appearance -> Colour -> Custom hex, then Save.
        xbmcgui.SELECT_QUEUE.extend([3, 1, 9, 0, 6])
        xbmcgui.INPUT_QUEUE.extend(['FFFF2896', 'Pink'])
        panel.edit_scene(None)

        scene = self.app.scene_by_name('Pink')
        self.assertIsNotNone(scene)
        self.assertEqual(scene['color'], [255, 40, 150])

    def test_temperature_preset_index_matches_its_label(self):
        import gui

        xbmcgui.SELECT_QUEUE.extend([1])
        self.panel().temp_menu(None, 'All Lights')
        self.assertEqual(gui.TEMP_PRESETS[1][0], 'Warm - 2700K')
        self.assertEqual(self.recorder.calls, [('temp', 'AA:BB', 2700)])

    def test_temperature_custom_entry(self):
        import gui

        xbmcgui.SELECT_QUEUE.extend([len(gui.TEMP_PRESETS)])
        xbmcgui.INPUT_QUEUE.append('3300')
        self.panel().temp_menu(None, 'All Lights')
        self.assertEqual(self.recorder.calls, [('temp', 'AA:BB', 3300)])

    def test_scene_menu_applies_the_selected_scene(self):
        xbmcgui.SELECT_QUEUE.extend([0])  # "Movie Night" is first
        self.panel().scene_menu()
        self.assertEqual(self.app.scenes[0]['name'], 'Movie Night')
        self.assertEqual([c[0] for c in self.recorder.calls],
                         ['turn', 'brightness', 'temp'])

    def test_scene_menu_trailing_rows_are_capture_then_manage(self):
        """Pick the rows by label so adding another cannot silently rewire them."""
        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().scene_menu()
        labels = xbmcgui.SELECT_CALLS[-1][1]

        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend([labels.index('Manage scenes...')])
        self.panel().scene_menu()
        headings = [heading for heading, _options in xbmcgui.SELECT_CALLS]
        self.assertIn('Manage scenes', headings)

        xbmcgui.reset()
        # Capture prompts for a name; declining leaves everything alone.
        xbmcgui.SELECT_QUEUE.extend(
            [labels.index('Capture lights as a new scene...')])
        before = len(self.app.scenes)
        self.panel().scene_menu()
        self.assertEqual(len(self.app.scenes), before)

    def test_capture_saves_what_the_lights_are_doing(self):
        one = self.app.devices[0]
        two = Device('CC:DD', name='Strip', lan=True, ip='10.0.0.3')
        self.app._devices = [one, two]

        self.recorder.get_states = lambda devices, timeout=3.0: {
            'AA:BB': {'power': 'on', 'brightness': 30, 'colorTem': 2200,
                      'color': {'r': 0, 'g': 0, 'b': 0}},
            'CC:DD': {'power': 'on', 'brightness': 80, 'colorTem': 0,
                      'color': {'r': 255, 'g': 40, 'b': 10}},
        }

        xbmcgui.INPUT_QUEUE.append('Twilight')
        self.panel().capture_scene()

        scene = self.app.scene_by_name('Twilight')
        self.assertIsNotNone(scene)
        self.assertEqual(sorted(scene['devices'].keys()), ['AA:BB', 'CC:DD'])
        self.assertEqual(scene['devices']['AA:BB']['mode'],
                         scene_lib.MODE_TEMP)
        self.assertEqual(scene['devices']['AA:BB']['kelvin'], 2200)
        self.assertEqual(scene['devices']['CC:DD']['mode'],
                         scene_lib.MODE_COLOR)
        self.assertEqual(scene['devices']['CC:DD']['color'], [255, 40, 10])

        # And it survived to disk in a form that reloads.
        saved = json.load(open(os.path.join(PROFILE, 'scenes.json')))
        stored = [s for s in saved if s['name'] == 'Twilight'][0]
        self.assertEqual(stored['devices']['CC:DD']['brightness'], 80)

    def test_capture_reports_lights_that_did_not_answer(self):
        two = Device('CC:DD', name='Strip', lan=True, ip='10.0.0.3')
        self.app._devices = [self.app.devices[0], two]
        self.recorder.get_states = lambda devices, timeout=3.0: {
            'AA:BB': {'power': 'on', 'brightness': 30, 'colorTem': 2200},
            'CC:DD': None,
        }

        xbmcgui.INPUT_QUEUE.append('Partial')
        self.panel().capture_scene()

        scene = self.app.scene_by_name('Partial')
        self.assertEqual(list(scene['devices'].keys()), ['AA:BB'])
        shown = ' '.join(line for _h, line in xbmcgui.OK_DIALOGS)
        self.assertIn('Strip', shown)

    def test_capture_with_no_answers_saves_nothing(self):
        self.recorder.get_states = lambda devices, timeout=3.0: {'AA:BB': None}
        before = len(self.app.scenes)

        xbmcgui.INPUT_QUEUE.append('Nothing')
        self.panel().capture_scene()

        self.assertEqual(len(self.app.scenes), before)
        self.assertIsNone(self.app.scene_by_name('Nothing'))

    def test_capture_asks_before_replacing_an_existing_scene(self):
        self.recorder.get_states = lambda devices, timeout=3.0: {
            'AA:BB': {'power': 'on', 'brightness': 30, 'colorTem': 2200}}

        xbmcgui.INPUT_QUEUE.append('Movie Night')
        xbmcgui.YESNO_QUEUE.append(False)   # decline the replace
        self.panel().capture_scene()

        scene = self.app.scene_by_name('Movie Night')
        self.assertFalse(scene.get('devices'))  # original, uncaptured

        xbmcgui.INPUT_QUEUE.append('Movie Night')
        xbmcgui.YESNO_QUEUE.append(True)    # accept it this time
        self.panel().capture_scene()

        scene = self.app.scene_by_name('Movie Night')
        self.assertTrue(scene.get('devices'))
        # Replaced in place rather than duplicated.
        self.assertEqual(len([s for s in self.app.scenes
                              if s['name'] == 'Movie Night']), 1)

    def test_new_scene_can_be_named_and_saved(self):
        panel = self.panel()
        before = len(self.app.scenes)

        # 0 = Name, then 6 = Save.
        xbmcgui.SELECT_QUEUE.extend([0, 6])
        xbmcgui.INPUT_QUEUE.append('Reading')
        panel.edit_scene(None)

        self.assertEqual(len(self.app.scenes), before + 1)
        self.assertIsNotNone(self.app.scene_by_name('Reading'))
        # And it survived to disk.
        saved = json.load(open(os.path.join(PROFILE, 'scenes.json')))
        self.assertIn('Reading', [s['name'] for s in saved])

    def test_editing_an_existing_scene_keeps_its_slot(self):
        panel = self.panel()
        index = 0
        original = self.app.scenes[index]['name']

        xbmcgui.SELECT_QUEUE.extend([0, 6])  # Name, Save
        xbmcgui.INPUT_QUEUE.append('Cinema')
        panel.edit_scene(index)

        self.assertEqual(self.app.scenes[index]['name'], 'Cinema')
        self.assertIsNone(self.app.scene_by_name(original))

    def test_scene_delete_row_only_exists_when_editing(self):
        panel = self.panel()
        xbmcgui.SELECT_QUEUE.extend([-1])
        panel.edit_scene(None)
        new_options = xbmcgui.SELECT_CALLS[-1][1]
        self.assertNotIn('Delete', new_options)

        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend([-1])
        panel.edit_scene(0)
        edit_options = xbmcgui.SELECT_CALLS[-1][1]
        self.assertIn('Delete', edit_options)

    def test_scene_target_picker_toggles_devices(self):
        panel = self.panel()
        scene = scene_lib.make_scene('Test')

        # Row 0 is "all lights", rows 1..n are devices, last row is Done.
        xbmcgui.SELECT_QUEUE.extend([1, 2])  # toggle device, then Done
        panel._edit_targets(scene)
        self.assertEqual(scene['targets'], ['AA:BB'])

        xbmcgui.SELECT_QUEUE.extend([1, 2])  # toggle it back off, then Done
        panel._edit_targets(scene)
        self.assertEqual(scene['targets'], [])

    def _manage_row(self, matcher):
        """Index of the Manage devices row whose label matches."""
        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().manage_devices()
        labels = xbmcgui.SELECT_CALLS[-1][1]
        xbmcgui.reset()
        return [i for i, label in enumerate(labels) if matcher in label][0]

    def test_device_rename_persists(self):
        row = self._manage_row('Lamp')
        xbmcgui.SELECT_QUEUE.extend([row, 0])  # the device, then "Rename"
        xbmcgui.INPUT_QUEUE.append('Behind the TV')
        self.panel().manage_devices()

        self.assertEqual(self.app.devices[0].name, 'Behind the TV')
        saved = json.load(open(os.path.join(PROFILE, 'devices.json')))
        self.assertEqual(saved[0]['name'], 'Behind the TV')

    def test_device_can_be_disabled_and_drops_out_of_the_group(self):
        row = self._manage_row('Lamp')
        xbmcgui.SELECT_QUEUE.extend([row, 1])  # the device, then "Disable"
        self.panel().manage_devices()

        self.assertFalse(self.app.devices[0].enabled)
        self.assertEqual(self.app.enabled_devices, [])

    def test_naming_walkthrough_lights_each_bulb_and_saves_names(self):
        # Both still on placeholder names, so the walk covers all of them
        # without the "which lights?" prompt.
        self.app._devices = [
            Device('AA:BB', name='H6008 (AABB)', model='H6008', lan=True,
                   ip='10.0.0.2'),
            Device('CC:DD', name='H6008 (CCDD)', model='H6008', lan=True,
                   ip='10.0.0.3'),
        ]
        self.recorder.get_states = lambda devices, timeout=3.0: {
            'AA:BB': {'power': 'on', 'brightness': 40, 'colorTem': 2700},
            'CC:DD': {'power': 'on', 'brightness': 40, 'colorTem': 2700},
        }

        xbmcgui.YESNO_QUEUE.append(True)
        xbmcgui.INPUT_QUEUE.extend(['Kitchen Left', 'Kitchen Right'])
        self.panel().name_lights()

        self.assertEqual([d.name for d in self.app.devices],
                         ['Kitchen Left', 'Kitchen Right'])
        saved = json.load(open(os.path.join(PROFILE, 'devices.json')))
        self.assertEqual(sorted(d['name'] for d in saved),
                         ['Kitchen Left', 'Kitchen Right'])

        # Each bulb was driven to the highlight colour while being asked about.
        highlights = [c for c in self.recorder.calls
                      if c[0] == 'color' and c[2:] == gui_highlight()]
        self.assertEqual(len(highlights), 2)

    def test_naming_puts_each_light_back_before_moving_on(self):
        self.recorder.get_states = lambda devices, timeout=3.0: {
            'AA:BB': {'power': 'on', 'brightness': 40, 'colorTem': 2700}}

        xbmcgui.YESNO_QUEUE.append(True)
        xbmcgui.INPUT_QUEUE.append('Kitchen Left')
        self.panel().name_lights()

        # The last thing done to the bulb is the restore, not the highlight.
        self.assertEqual(self.recorder.calls[-1], ('temp', 'AA:BB', 2700))

    def test_cancelling_the_keyboard_stops_and_keeps_earlier_names(self):
        self.app._devices = [
            Device('AA:BB', name='H6008 (AABB)', model='H6008', lan=True,
                   ip='10.0.0.2'),
            Device('CC:DD', name='H6008 (CCDD)', model='H6008', lan=True,
                   ip='10.0.0.3'),
        ]
        self.recorder.get_states = lambda devices, timeout=3.0: {}

        xbmcgui.YESNO_QUEUE.append(True)
        xbmcgui.INPUT_QUEUE.append('Kitchen Left')  # then the queue runs dry
        self.panel().name_lights()

        self.assertEqual(self.app.devices[0].name, 'Kitchen Left')
        self.assertEqual(self.app.devices[1].name, 'H6008 (CCDD)')
        saved = json.load(open(os.path.join(PROFILE, 'devices.json')))
        self.assertIn('Kitchen Left', [d['name'] for d in saved])

    def test_naming_offers_to_do_only_the_unnamed_ones(self):
        named = self.app.devices[0]          # 'Lamp' -- already named
        unnamed = Device('CC:DD', name='H6008 (CCDD)', model='H6008', lan=True,
                         ip='10.0.0.3')
        self.app._devices = [named, unnamed]
        self.recorder.get_states = lambda devices, timeout=3.0: {}

        xbmcgui.SELECT_QUEUE.extend([0])     # "Only the 1 still unnamed"
        xbmcgui.YESNO_QUEUE.append(True)
        xbmcgui.INPUT_QUEUE.append('Kitchen Right')
        self.panel().name_lights()

        self.assertEqual(named.name, 'Lamp')
        self.assertEqual(unnamed.name, 'Kitchen Right')

    def test_declining_the_walkthrough_changes_nothing(self):
        xbmcgui.YESNO_QUEUE.append(False)
        self.panel().name_lights()
        self.assertEqual(self.recorder.calls, [])
        self.assertEqual(self.app.devices[0].name, 'Lamp')

    def test_identify_flashes_the_configured_number_of_times(self):
        import gui

        device = self.app.devices[0]
        self.recorder.get_state = lambda d: None
        self.panel()._identify(device, sleep_func=lambda _s: None)

        # One off and one on per flash.
        turns = [c for c in self.recorder.calls if c[0] == 'turn']
        self.assertEqual(len(turns), gui.IDENTIFY_FLASHES * 2)
        self.assertEqual(gui.IDENTIFY_FLASHES, 10)
        self.assertEqual(turns[0], ('turn', 'AA:BB', False))
        self.assertEqual(turns[1], ('turn', 'AA:BB', True))

    def test_identify_can_be_cancelled_early(self):
        device = self.app.devices[0]
        self.recorder.get_state = lambda d: None
        xbmcgui.CANCEL_AFTER.append(3)   # cancel on the third update

        self.panel()._identify(device, sleep_func=lambda _s: None)

        turns = [c for c in self.recorder.calls if c[0] == 'turn']
        self.assertEqual(len(turns), 4)  # two full flashes, then stopped

    def test_identify_puts_a_light_that_was_off_back_off(self):
        """Otherwise identifying a light silently switches it on for good."""
        device = self.app.devices[0]
        self.recorder.get_state = lambda d: {'power': 'off'}

        self.panel()._identify(device, sleep_func=lambda _s: None)

        self.assertEqual(self.recorder.calls[-1], ('turn', 'AA:BB', False))

    def test_identify_restores_the_previous_colour(self):
        device = self.app.devices[0]
        self.recorder.get_state = lambda d: {
            'power': 'on', 'brightness': 25, 'colorTem': 0,
            'color': {'r': 255, 'g': 40, 'b': 150}}

        self.panel()._identify(device, sleep_func=lambda _s: None)

        self.assertIn(('brightness', 'AA:BB', 25), self.recorder.calls)
        self.assertEqual(self.recorder.calls[-1],
                         ('color', 'AA:BB', 255, 40, 150))

    def test_identify_reports_a_light_it_cannot_reach(self):
        device = self.app.devices[0]
        recorder = RecordingController(fail_on={'AA:BB'})
        recorder.get_state = lambda d: None
        self.app.controller = recorder

        self.panel()._identify(device, sleep_func=lambda _s: None)
        self.assertTrue(xbmcgui.NOTIFICATIONS)

    def test_pick_scene_writes_the_setting(self):
        import gui

        xbmcgui.SELECT_QUEUE.extend([1])  # row 0 is "(none)"
        gui.pick_scene_for_setting(self.app, 'scene_playing')
        self.assertEqual(xbmcaddon.SETTINGS['scene_playing'], 'Movie Night')

    def test_pick_scene_can_clear_the_setting(self):
        import gui

        xbmcaddon.SETTINGS['scene_playing'] = 'Movie Night'
        xbmcgui.SELECT_QUEUE.extend([0])  # "(none)"
        gui.pick_scene_for_setting(self.app, 'scene_playing')
        self.assertEqual(xbmcaddon.SETTINGS['scene_playing'], '')

    def test_empty_cache_offers_discovery_and_backs_out_cleanly(self):
        self.app._devices = []
        xbmcgui.YESNO_QUEUE.append(False)  # decline the search
        self.panel().run()
        self.assertEqual(self.recorder.calls, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
