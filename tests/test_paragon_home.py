# -*- coding: utf-8 -*-
"""
Paragon Home - test suite.

Runs off-device: Kodi is replaced by the stubs in tests/kodistubs, the Govee
LAN device is replaced by a UDP socket on loopback, and the Govee cloud is
replaced by a local HTTP server. That covers the protocol encoding, the
transport-selection logic and the scene engine without needing hardware.

    python3 tests/test_paragon_home.py
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import shutil
import socket
import struct
import sys
import threading
import time
import unittest
import zlib

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


def _free_port():
    """A port nothing is listening on, for testing an unreachable device."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def gui_highlight():
    import gui
    return tuple(gui.HIGHLIGHT_COLOR)


def palette_row(panel, name):
    """Index of the palette row for `name` in a colour menu."""
    return [e['name'] for e in panel.app.palette].index(name)


def menu_row(open_menu, prefix):
    """Index of a menu row by its label prefix, and the labels it sat among.

    Looked up rather than hardcoded, for the same reason scene_row is: every
    positional index into a menu has broken at least once when a row was
    added above it.
    """
    xbmcgui.SELECT_QUEUE.extend([-1])
    open_menu()
    labels = xbmcgui.SELECT_CALLS[-1][1]
    xbmcgui.reset()
    matches = [i for i, label in enumerate(labels)
               if label.startswith(prefix)]
    assert matches, 'no row starting "%s" among %r' % (prefix, labels)
    return matches[0], labels


def scene_row(panel, prefix, index=None):
    """Index of a scene-editor row by its label prefix.

    Looked up rather than hardcoded: the editor has grown rows several times,
    and every positional index in these tests broke each time it did.
    """
    xbmcgui.SELECT_QUEUE.extend([-1])
    panel.edit_scene(index)
    labels = xbmcgui.SELECT_CALLS[-1][1]
    xbmcgui.reset()
    return [i for i, label in enumerate(labels)
            if label.startswith(prefix)][0]


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
    """Stands in for the Hub so scene ordering can be asserted.

    Presents the same surface the Hub does, which is what the menus and the
    scene engine talk to.
    """

    def __init__(self, fail_on=None, caps=None):
        self.calls = []
        self.fail_on = fail_on or set()
        self.caps = caps

    def capabilities(self, device):
        """The real implementation unless a test asks for something else.

        Deliberately not a fixed set of everything: a stub that claims every
        capability for every device cannot catch a scene sending a plug a
        brightness, which is one of the things this stub exists to watch.
        """
        if self.caps is not None:
            return set(self.caps)
        return GoveeController.capabilities(device)

    @staticmethod
    def commands(device):
        return []

    class _StandIn(object):
        """What the Hub would hand back for this device's driver."""

        def __init__(self, has_transports):
            self.HAS_TRANSPORTS = has_transports

    def driver_for(self, device):
        # Answering None made every driver look alike, which cannot catch a
        # menu tagging a plug with a transport it has no choice about.
        return self._StandIn(
            (getattr(device, 'driver', None) or 'govee') == 'govee')

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

    def send_command(self, device, name):
        # The Hub has this; leaving it off meant a sequence step that fires an
        # infrared code failed against the stub for a reason the real code
        # would never have had.
        self._record('command', device, name)


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
        """A device that can express part of a scene gets that part only."""
        controller = RecordingController()
        device = Device('AA:BB', name='Warm bulb', lan=False, cloud=True,
                        supports=['turn', 'colorTem'])
        scene = scene_lib.make_scene('Test', brightness=30,
                                     mode=scene_lib.MODE_TEMP, kelvin=3000)

        scene_lib.apply_scene(controller, scene, [device])

        self.assertEqual([c[0] for c in controller.calls], ['turn', 'temp'])

    def test_an_untargeted_scene_leaves_out_what_it_cannot_describe(self):
        """The bug behind a sequence switching every plug in the house.

        A scene naming no targets meant "every enabled device", which was
        right when lights were all there was. Once plugs were listed too, a
        colour scene switched all of them as a side effect of the power
        setting that came with the colour.
        """
        controller = RecordingController()
        bulb = Device('AA:BB', name='Lamp', driver='govee', lan=True)
        plug = Device('8006ABCD', name='Christmas Tree', driver='kasa',
                      lan=True)
        controller.capabilities = lambda d: set(
            ['power', 'state'] if d.driver == 'kasa'
            else ['power', 'brightness', 'color', 'color_temp', 'state'])
        scene = scene_lib.make_scene('Warshade', mode=scene_lib.MODE_COLOR,
                                     color=[80, 0, 120])

        scene_lib.apply_scene(controller, scene, [bulb, plug])

        self.assertEqual(set(c[1] for c in controller.calls), set(['AA:BB']))

    def test_a_scene_that_only_says_off_still_reaches_the_plugs(self):
        """"All off" means all of it, because off is something a plug can be."""
        controller = RecordingController()
        bulb = Device('AA:BB', name='Lamp', driver='govee', lan=True)
        plug = Device('8006ABCD', name='Christmas Tree', driver='kasa',
                      lan=True)
        controller.capabilities = lambda d: set(['power', 'state'])
        scene = scene_lib.make_scene('All Off', power=scene_lib.POWER_OFF)

        scene_lib.apply_scene(controller, scene, [bulb, plug])

        self.assertEqual(set(c[1] for c in controller.calls),
                         set(['AA:BB', '8006ABCD']))

    def test_a_plug_named_in_a_scene_is_still_honoured(self):
        """Naming targets is how a plug joins a colour scene deliberately."""
        controller = RecordingController()
        bulb = Device('AA:BB', name='Lamp', driver='govee', lan=True)
        plug = Device('8006ABCD', name='Christmas Tree', driver='kasa',
                      lan=True)
        controller.capabilities = lambda d: set(
            ['power', 'state'] if d.driver == 'kasa'
            else ['power', 'brightness', 'color', 'color_temp', 'state'])
        scene = scene_lib.make_scene('Warshade', mode=scene_lib.MODE_COLOR,
                                     color=[80, 0, 120],
                                     targets=['AA:BB', '8006ABCD'])

        scene_lib.apply_scene(controller, scene, [bulb, plug])

        self.assertEqual(set(c[1] for c in controller.calls),
                         set(['AA:BB', '8006ABCD']))

    def test_apply_scene_ignores_disabled_devices(self):
        controller = RecordingController()
        device = Device('AA:BB', name='Lamp', lan=True, enabled=False)
        applied, errors = scene_lib.apply_scene(
            controller, scene_lib.make_scene('Test'), [device])
        self.assertEqual(applied, 0)
        self.assertEqual(controller.calls, [])
        self.assertTrue(errors)


class TestPlugsInScenes(unittest.TestCase):
    """A scene is written once and applied to whatever is enabled.

    Once that includes plugs, "what commands does this bulb list" is the wrong
    question -- a plug lists nothing and would be sent everything.
    """

    def plug(self):
        return Device('wp9abc#1', name='Office Plug', driver='tuya',
                      native_id='wp9abc', driver_data={'dp': '1'})

    def test_a_plug_in_a_scene_is_only_switched(self):
        controller = RecordingController(caps=['power', 'state'])
        settings = scene_lib.make_scene(
            'Evening', power=scene_lib.POWER_ON, brightness=40,
            mode=scene_lib.MODE_COLOR, color=[255, 0, 0])

        scene_lib.apply_settings(controller, self.plug(), settings)

        self.assertEqual([call[0] for call in controller.calls], ['turn'])

    def test_a_plug_still_turns_off_with_the_rest(self):
        controller = RecordingController(caps=['power', 'state'])
        settings = scene_lib.make_scene(
            'All off', power=scene_lib.POWER_OFF)

        scene_lib.apply_settings(controller, self.plug(), settings)

        self.assertEqual(controller.calls, [('turn', 'WP9ABC#1', False)])

    def test_a_cycle_step_passes_a_plug_by(self):
        """A colour-only step has nothing to say to something with no colour."""
        controller = RecordingController(caps=['power', 'state'])
        settings = scene_lib.make_scene(
            'Mix', power=scene_lib.POWER_ON,
            mode=scene_lib.MODE_COLOR, color=[0, 255, 0])

        scene_lib.apply_settings(controller, self.plug(), settings,
                                 colors_only=True)

        self.assertEqual(controller.calls, [])

    def test_a_bulb_is_unaffected_by_the_capability_gate(self):
        """The Govee path must behave exactly as it did before."""
        controller = RecordingController()
        bulb = Device('AA:BB', name='Lamp', supports=['turn', 'brightness'])
        settings = scene_lib.make_scene(
            'Evening', power=scene_lib.POWER_ON, brightness=40,
            mode=scene_lib.MODE_COLOR, color=[255, 0, 0])

        scene_lib.apply_settings(controller, bulb, settings)

        self.assertEqual([call[0] for call in controller.calls],
                         ['turn', 'brightness'])


class TestSequences(unittest.TestCase):
    """Ten ordered steps, run as one."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'sequences', 'gui'):
            if name in sys.modules:
                del sys.modules[name]
        import sequences

        self.sequences = sequences

    def tearDown(self):
        clean_profile()

    def app(self):
        from paragon_home import ParagonHome

        app = ParagonHome()
        self.recorder = RecordingController()
        # Per driver, as the real Hub reports it. A blaster has commands and
        # no power; giving every device the same set would have let a scene
        # send a blaster a power command and called it correct.
        by_driver = {'govee': ['power', 'brightness', 'color', 'color_temp',
                               'state'],
                     'tuya': ['power', 'state'],
                     'broadlink': ['commands']}
        self.recorder.capabilities = lambda d: set(by_driver[d.driver])
        self.recorder.commands = lambda device: ['TV power', 'Volume up']
        app.controller = self.recorder
        app._devices = [
            Device('AA:BB', name='Back Office Left Low', driver='govee',
                   lan=True),
            Device('WP9ABC#ALL', name='Office Plug All outlets',
                   driver='tuya', lan=True, native_id='wp9abc'),
            Device('EE:FF', name='Bedroom Broadlink', driver='broadlink',
                   lan=True),
        ]
        app._scenes = [scene_lib.make_scene('Warshade',
                                            power=scene_lib.POWER_ON,
                                            mode=scene_lib.MODE_COLOR,
                                            color=[80, 0, 120],
                                            targets=['AA:BB'])]
        return app

    def example(self):
        """The sequence from the request, written the way it was described."""
        return self.sequences.make_sequence('Wind Down', [
            {'kind': 'scene', 'target': 'Warshade'},
            {'kind': 'power', 'driver': 'tuya', 'target': 'WP9ABC#ALL',
             'action': 'on'},
            {'kind': 'command', 'driver': 'broadlink', 'target': 'EE:FF',
             'action': 'TV power'},
        ])

    # -- shape -------------------------------------------------------------

    def test_a_sequence_always_has_ten_slots(self):
        """Named after the preset system, and fixed like it: slot 4 is slot 4."""
        sequence = self.sequences.make_sequence('Wind Down')

        self.assertEqual(len(sequence['steps']), self.sequences.STEP_COUNT)
        self.assertEqual(self.sequences.STEP_COUNT, 10)
        self.assertTrue(all(s['kind'] == 'none' for s in sequence['steps']))

    def test_three_steps_leave_seven_empty_slots(self):
        sequence = self.example()

        self.assertEqual(len(self.sequences.filled_steps(sequence)), 3)
        self.assertEqual(len(sequence['steps']), 10)

    def test_more_than_ten_steps_are_trimmed(self):
        sequence = self.sequences.make_sequence(
            'Too many',
            [{'kind': 'scene', 'target': 'Warshade'}] * 14)

        self.assertEqual(len(sequence['steps']), 10)

    def test_a_half_filled_step_becomes_empty_rather_than_broken(self):
        """A slot that looks filled but cannot run is worse than a blank one."""
        for raw in ({'kind': 'power', 'target': ''},
                    {'kind': 'power', 'target': 'AA:BB', 'action': 'sideways'},
                    {'kind': 'command', 'target': 'EE:FF', 'action': ''},
                    {'kind': 'scene', 'target': '   '}):
            self.assertEqual(self.sequences.normalise_step(raw)['kind'], 'none',
                             'accepted %r' % (raw,))

    def test_a_sequence_with_no_name_is_not_a_sequence(self):
        self.assertIsNone(self.sequences.normalise({'name': '  '}))

    # -- running -----------------------------------------------------------

    def test_the_example_runs_top_to_bottom_in_order(self):
        app = self.app()

        done, errors = self.sequences.run(app, self.example())

        self.assertEqual((done, errors), (3, []))
        self.assertEqual(
            [call[:2] for call in self.recorder.calls],
            [('turn', 'AA:BB'),          # 1. Govee, scene, Warshade
             ('color', 'AA:BB'),         #    (the scene's colour)
             ('turn', 'WP9ABC#ALL'),     # 2. Tuya, all outlets, on
             ('command', 'EE:FF')])      # 3. Broadlink, bedroom, TV power

    def test_a_scene_step_applies_the_scene_to_whatever_the_scene_says(self):
        """A sequence step runs a scene; it does not redefine one.

        The scene here names one bulb, so the plug beside it is untouched --
        the sequence's own step 2 is what switches that.
        """
        app = self.app()
        sequence = self.sequences.make_sequence(
            'Just the scene', [{'kind': 'scene', 'target': 'Warshade'}])

        self.sequences.run(app, sequence)

        self.assertEqual(set(call[1] for call in self.recorder.calls),
                         set(['AA:BB']))

    def test_a_scene_step_beside_a_plug_step_leaves_the_other_plugs_alone(self):
        """The reported bug: one Kasa step, and every Kasa plug switched.

        The scene was doing it. A scene naming no targets meant every enabled
        device, so "Scene: Warshade" switched all four plugs as a side effect
        of the power setting that came with the colour -- and the plug step
        beside it only accounted for one of them.
        """
        app = self.app()
        for suffix in ('1', '2', '3', '4'):
            app._devices.append(
                Device('8006ABC%s' % suffix, name='Kasa %s' % suffix,
                       driver='kasa', lan=True))
        by_driver = {'govee': ['power', 'brightness', 'color', 'color_temp',
                               'state'],
                     'tuya': ['power', 'state'],
                     'broadlink': ['commands'],
                     'kasa': ['power', 'state']}
        self.recorder.capabilities = lambda d: set(by_driver[d.driver])
        # An ordinary colour scene, saved before any plug existed and so
        # naming no targets at all.
        app._scenes = [scene_lib.make_scene('Warshade',
                                            mode=scene_lib.MODE_COLOR,
                                            color=[80, 0, 120])]

        sequence = self.sequences.make_sequence('Wind Down', [
            {'kind': 'scene', 'target': 'Warshade'},
            {'kind': 'power', 'driver': 'kasa', 'target': '8006ABC2',
             'action': 'on'},
        ])
        self.sequences.run(app, sequence)

        switched = set(call[1] for call in self.recorder.calls)
        self.assertEqual(switched, set(['AA:BB', '8006ABC2']))
        for suffix in ('1', '3', '4'):
            self.assertNotIn('8006ABC%s' % suffix, switched)

    def test_an_all_off_scene_step_still_switches_everything(self):
        """The other half: a scene that only says off means all of it."""
        app = self.app()
        app._devices.append(Device('8006ABC1', name='Kasa 1', driver='kasa',
                                   lan=True))
        by_driver = {'govee': ['power', 'state'], 'tuya': ['power', 'state'],
                     'broadlink': ['commands'], 'kasa': ['power', 'state']}
        self.recorder.capabilities = lambda d: set(by_driver[d.driver])
        app._scenes = [scene_lib.make_scene('All Off',
                                            power=scene_lib.POWER_OFF)]

        self.sequences.run(app, self.sequences.make_sequence(
            'Goodnight', [{'kind': 'scene', 'target': 'All Off'}]))

        self.assertIn('8006ABC1',
                      set(call[1] for call in self.recorder.calls))

    def test_a_failing_step_does_not_stop_the_ones_after_it(self):
        """A plug that has been unplugged is no reason to leave the room dark."""
        app = self.app()
        self.recorder.fail_on = set(['WP9ABC#ALL'])

        done, errors = self.sequences.run(app, self.example())

        self.assertEqual(done, 2)
        self.assertEqual(len(errors), 1)
        self.assertIn('Step 2', errors[0])
        self.assertIn(('command', 'EE:FF'),
                      [call[:2] for call in self.recorder.calls])

    def test_a_step_pointing_at_nothing_says_so_and_carries_on(self):
        app = self.app()
        sequence = self.sequences.make_sequence('Gone', [
            {'kind': 'power', 'driver': 'tuya', 'target': 'NOT:HERE',
             'action': 'on'},
            {'kind': 'scene', 'target': 'Warshade'},
        ])

        done, errors = self.sequences.run(app, sequence)

        self.assertEqual(done, 1)
        self.assertIn('Nothing matches', errors[0])

    def test_a_missing_scene_is_reported_rather_than_silently_skipped(self):
        app = self.app()
        sequence = self.sequences.make_sequence(
            'Ghost', [{'kind': 'scene', 'target': 'No Such Scene'}])

        done, errors = self.sequences.run(app, sequence)

        self.assertEqual(done, 0)
        self.assertIn('No scene called', errors[0])

    def test_a_pause_waits_after_its_step_not_before(self):
        """A television told to wake and change channel at once misses one."""
        app = self.app()
        slept = []
        sequence = self.sequences.make_sequence('Slow', [
            {'kind': 'command', 'driver': 'broadlink', 'target': 'EE:FF',
             'action': 'TV power', 'pause': 4},
            {'kind': 'command', 'driver': 'broadlink', 'target': 'EE:FF',
             'action': 'Volume up'},
        ])

        self.sequences.run(app, sequence, sleep_func=slept.append)

        self.assertEqual(slept, [4])
        self.assertEqual(len(self.recorder.calls), 2)

    def test_empty_slots_cost_nothing_and_are_skipped(self):
        app = self.app()
        sequence = self.sequences.make_sequence('Sparse')
        sequence['steps'][7] = {'kind': 'scene', 'target': 'Warshade',
                              'pause': 0}
        slept = []

        done, errors = self.sequences.run(app, sequence, sleep_func=slept.append)

        self.assertEqual((done, errors), (1, []))
        self.assertEqual(slept, [])

    def test_all_of_a_driver_is_a_valid_target(self):
        app = self.app()
        app._devices.append(Device('WP9ABC#1', name='Outlet 1', driver='tuya',
                                   lan=True, native_id='wp9abc'))
        sequence = self.sequences.make_sequence('Plugs off', [
            {'kind': 'power', 'driver': 'tuya',
             'target': self.sequences.TARGET_ALL, 'action': 'off'}])

        self.sequences.run(app, sequence)

        self.assertEqual([call[:2] for call in self.recorder.calls],
                         [('turn', 'WP9ABC#ALL'), ('turn', 'WP9ABC#1')])

    def test_a_renamed_device_is_still_found_by_id(self):
        """Sequences refer to ids, so renaming a device does not break one."""
        app = self.app()
        sequence = self.example()
        app._devices[1].name = 'Something Else Entirely'

        done, errors = self.sequences.run(app, sequence)

        self.assertEqual((done, errors), (3, []))

    def test_a_step_written_by_hand_can_name_a_device(self):
        app = self.app()
        sequence = self.sequences.make_sequence('By name', [
            {'kind': 'power', 'target': 'Office Plug All outlets',
             'action': 'on'}])

        done, errors = self.sequences.run(app, sequence)

        self.assertEqual((done, errors), (1, []))

    def test_a_run_can_be_stopped_part_way(self):
        app = self.app()
        seen = []

        def stop_after_first(index, step):
            seen.append(index)
            return len(seen) <= 1

        done, _errors = self.sequences.run(app, self.example(),
                                         on_step=stop_after_first)

        self.assertEqual(done, 1)

    # -- when it runs ------------------------------------------------------

    SATURDAY_6PM = datetime.datetime(2026, 8, 22, 18, 0)   # a Saturday

    def ignition(self):
        """The sequence from the request: Saturday nights at six."""
        return self.sequences.make_sequence('Ignition', [
            {'kind': 'scene', 'target': 'Warshade'}], time='6pm', days=[5])

    def test_a_time_is_read_however_it_is_typed(self):
        """Entered on a remote, where "6pm" is much less work than "18:00"."""
        for text, expected in (('18:00', '18:00'), ('6pm', '18:00'),
                               ('6 PM', '18:00'), ('6:30pm', '18:30'),
                               ('1800', '18:00'), ('0:05', '00:05'),
                               ('12am', '00:00'), ('12pm', '12:00')):
            self.assertEqual(self.sequences.parse_time(text), expected,
                             'could not read %r' % text)

    def test_something_that_is_not_a_time_is_refused(self):
        for text in ('24:00', '18:60', 'banana', '', None, '99'):
            self.assertEqual(self.sequences.parse_time(text), '',
                             'accepted %r as a time' % (text,))

    def test_a_schedule_needs_both_a_time_and_days(self):
        """Either alone would be a schedule that can never come round."""
        self.assertFalse(self.sequences.scheduled(
            self.sequences.make_sequence('A', time='6pm')))
        self.assertFalse(self.sequences.scheduled(
            self.sequences.make_sequence('B', days=[5])))
        self.assertTrue(self.sequences.scheduled(self.ignition()))

    def test_it_is_due_at_its_time_on_its_day(self):
        self.assertTrue(self.sequences.due(self.ignition(), self.SATURDAY_6PM))

    def test_it_is_not_due_before_its_time(self):
        self.assertFalse(self.sequences.due(
            self.ignition(), self.SATURDAY_6PM - datetime.timedelta(minutes=1)))

    def test_it_is_not_due_on_another_day(self):
        for offset in (1, 2, 3, 4, 5, 6):
            self.assertFalse(
                self.sequences.due(self.ignition(),
                                 self.SATURDAY_6PM
                                 + datetime.timedelta(days=offset)),
                'fired %d day(s) late' % offset)

    def test_a_few_minutes_late_still_counts(self):
        """Kodi is not always awake at the exact minute."""
        self.assertTrue(self.sequences.due(
            self.ignition(), self.SATURDAY_6PM + datetime.timedelta(minutes=4)))

    def test_an_hour_late_does_not(self):
        """A sequence that lifts the lights at six should not do it at seven."""
        self.assertFalse(self.sequences.due(
            self.ignition(), self.SATURDAY_6PM + datetime.timedelta(hours=1)))

    def test_it_does_not_run_twice_in_the_same_day(self):
        sequence = self.ignition()
        already = self.sequences.stamp(sequence, self.SATURDAY_6PM)

        self.assertFalse(self.sequences.due(sequence, self.SATURDAY_6PM, already))

    def test_moving_the_time_later_the_same_day_lets_it_run_again(self):
        """The stamp holds the time as well as the date, on purpose."""
        sequence = self.ignition()
        already = self.sequences.stamp(sequence, self.SATURDAY_6PM)
        sequence['time'] = '20:00'

        self.assertTrue(self.sequences.due(
            sequence, self.SATURDAY_6PM.replace(hour=20), already))

    def test_next_week_it_is_due_again(self):
        sequence = self.ignition()
        already = self.sequences.stamp(sequence, self.SATURDAY_6PM)

        self.assertTrue(self.sequences.due(
            sequence, self.SATURDAY_6PM + datetime.timedelta(days=7), already))

    def test_the_schedule_reads_the_way_it_would_be_said(self):
        self.assertEqual(self.sequences.describe_schedule(self.ignition()),
                         'Sat at 18:00')
        self.assertEqual(self.sequences.describe_schedule(
            self.sequences.make_sequence('A', time='07:00',
                                     days=[0, 1, 2, 3, 4])),
            'weekdays at 07:00')
        self.assertEqual(self.sequences.describe_schedule(
            self.sequences.make_sequence('B', time='09:00', days=[5, 6])),
            'weekends at 09:00')
        self.assertEqual(self.sequences.describe_schedule(
            self.sequences.make_sequence('C', time='23:00', days=list(range(7)))),
            'every day at 23:00')
        self.assertEqual(
            self.sequences.describe_schedule(self.sequences.make_sequence('D')),
            'only when you run it')

    # -- firing ------------------------------------------------------------

    def test_a_due_sequence_runs_and_is_not_run_again(self):
        app = self.app()
        app._sequences = [self.ignition()]

        first = app.run_due_sequences(now=self.SATURDAY_6PM)
        second = app.run_due_sequences(
            now=self.SATURDAY_6PM + datetime.timedelta(minutes=2))

        self.assertEqual(first, ['Ignition'])
        self.assertEqual(second, [])

    def test_a_restart_does_not_re_run_what_already_ran(self):
        """The record is on disk, so it survives Kodi closing."""
        app = self.app()
        app._sequences = [self.ignition()]
        app.run_due_sequences(now=self.SATURDAY_6PM)

        from paragon_home import ParagonHome
        again = ParagonHome()
        again.controller = self.recorder
        again._sequences = [self.ignition()]

        self.assertEqual(
            again.run_due_sequences(
                now=self.SATURDAY_6PM + datetime.timedelta(minutes=1)),
            [])

    def test_it_is_marked_as_run_before_it_runs(self):
        """A sequence that fails half way must not retry on every tick."""
        app = self.app()
        sequence = self.ignition()
        sequence['steps'][1] = {'kind': 'scene', 'target': 'No Such Scene',
                              'pause': 0}
        app._sequences = [sequence]

        app.run_due_sequences(now=self.SATURDAY_6PM)

        self.assertEqual(
            app.run_due_sequences(
                now=self.SATURDAY_6PM + datetime.timedelta(minutes=1)),
            [])

    def test_an_unscheduled_sequence_never_fires_itself(self):
        app = self.app()
        app._sequences = [self.sequences.make_sequence('Manual only', [
            {'kind': 'scene', 'target': 'Warshade'}])]

        self.assertEqual(app.run_due_sequences(now=self.SATURDAY_6PM), [])

    def test_two_sequences_due_at_once_both_run(self):
        app = self.app()
        other = self.ignition()
        other['name'] = 'Also Ignition'
        app._sequences = [self.ignition(), other]

        self.assertEqual(sorted(app.run_due_sequences(now=self.SATURDAY_6PM)),
                         ['Also Ignition', 'Ignition'])

    def test_deleting_a_sequence_forgets_when_it_last_ran(self):
        """Otherwise a new one of the same name inherits a day it never had."""
        app = self.app()
        app.save_sequence(self.ignition())
        app.run_due_sequences(now=self.SATURDAY_6PM)
        app.delete_sequence({'name': 'Ignition'})

        app.save_sequence(self.ignition())
        self.assertEqual(app.run_due_sequences(now=self.SATURDAY_6PM),
                         ['Ignition'])

    def test_a_schedule_survives_a_restart(self):
        app = self.app()
        app.save_sequence(self.ignition())

        from paragon_home import ParagonHome
        saved = ParagonHome().sequence_by_name('Ignition')

        self.assertEqual(saved['time'], '18:00')
        self.assertEqual(saved['days'], [5])

    # -- persistence -------------------------------------------------------

    def test_sequences_saved_as_reracks_are_carried_over(self):
        """These were called reracks until v2.14, and were already in use."""
        import addon_utils as utils
        import sequences

        utils.write_json('reracks.json', [
            sequences.make_sequence('Ignition',
                                    [{'kind': 'scene', 'target': 'Warshade'}],
                                    time='6pm', days=[5])])

        app = self.app()
        carried = app.sequence_by_name('Ignition')

        self.assertIsNotNone(carried)
        self.assertEqual(carried['time'], '18:00')
        self.assertEqual(len(carried['steps']), 10)
        # Written out under the new name, so the old file is read only once.
        self.assertIsNotNone(utils.read_json('sequences.json', default=None))

    def test_when_it_last_ran_is_carried_over_too(self):
        """Otherwise the rename would re-run everything that ran today."""
        import addon_utils as utils
        import sequences

        utils.write_json('reracks.json', [
            sequences.make_sequence('Ignition',
                                    [{'kind': 'scene', 'target': 'Warshade'}],
                                    time='6pm', days=[5])])
        utils.write_json('rerack_state.json',
                         {'Ignition': '2026-08-22 18:00'})

        app = self.app()

        self.assertEqual(
            app.run_due_sequences(now=datetime.datetime(2026, 8, 22, 18, 0)),
            [])

    def test_a_new_install_does_not_look_for_the_old_file(self):
        app = self.app()

        self.assertEqual(app.sequences, [])

    def test_a_sequence_survives_a_restart(self):
        app = self.app()
        app.save_sequence(self.example())

        from paragon_home import ParagonHome
        again = ParagonHome()

        saved = again.sequence_by_name('Wind Down')
        self.assertIsNotNone(saved)
        self.assertEqual(len(saved['steps']), 10)
        self.assertEqual(saved['steps'][0]['target'], 'Warshade')

    def test_saving_the_same_name_replaces_rather_than_duplicates(self):
        app = self.app()
        app.save_sequence(self.example())
        app.save_sequence(self.sequences.make_sequence(
            'Wind Down', [{'kind': 'scene', 'target': 'Warshade'}]))

        self.assertEqual(len(app.sequences), 1)
        self.assertEqual(len(self.sequences.filled_steps(app.sequences[0])), 1)

    def test_a_sequence_is_found_however_its_name_is_typed(self):
        app = self.app()
        app.save_sequence(self.example())

        self.assertIsNotNone(app.sequence_by_name('  wind DOWN '))

    def test_deleting_one_leaves_the_others(self):
        app = self.app()
        app.save_sequence(self.example())
        app.save_sequence(self.sequences.make_sequence('Other'))

        self.assertTrue(app.delete_sequence({'name': 'Other'}))
        self.assertEqual([r['name'] for r in app.sequences], ['Wind Down'])

    def test_a_fresh_install_has_no_sequences_rather_than_invented_ones(self):
        """A starter sequence would be ten slots pointing at nobody's devices."""
        self.assertEqual(self.app().sequences, [])

    def test_run_by_name_reports_a_name_that_is_not_there(self):
        app = self.app()

        self.assertFalse(app.run_sequence_by_name('Nothing', announce=False))

    # -- how it reads ------------------------------------------------------

    def test_the_summary_keeps_the_names_as_they_were_typed(self):
        self.assertEqual(self.sequences.describe(self.example()),
                         '3 steps, first: Scene: Warshade')

    def test_a_step_reads_back_in_the_order_it_was_chosen(self):
        sequence = self.example()

        self.assertEqual(
            [self.sequences.describe_step(s) for s in sequence['steps'][:3]],
            ['Scene: Warshade', 'WP9ABC#ALL: On', 'EE:FF: TV power'])

    def test_a_pause_is_shown_on_the_step_it_follows(self):
        step = self.sequences.normalise_step(
            {'kind': 'scene', 'target': 'Warshade', 'pause': 30})

        self.assertEqual(self.sequences.describe_step(step),
                         'Scene: Warshade  (+30s)')

    def test_an_all_target_reads_as_all_of_them(self):
        step = self.sequences.normalise_step(
            {'kind': 'power', 'driver': 'tuya',
             'target': self.sequences.TARGET_ALL, 'action': 'off'})

        self.assertEqual(self.sequences.describe_step(step),
                         'all tuya devices: Off')


class TestParagonTV(unittest.TestCase):
    """Reading Paragon TV's own Sequence schedule, exactly as it reads it."""

    SATURDAY = datetime.datetime(2026, 8, 22, 18, 0)
    THURSDAY = datetime.datetime(2026, 8, 20, 18, 0)

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        for name in ('addon_utils', 'paragon_home', 'paragon_tv', 'sequences'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def install_tv(self, **settings):
        """Paragon TV as Kodi would present it, with its own settings."""
        base = {'EnablePresetSystem': 'true',
                # The index of the preset, which is what the setting holds.
                'SaturdayPreset': '1',        # Alpha
                'ThursdayPreset': '6',        # Sigma, a satellite preset
                'AlphaPhase1Time': '04:00',
                'AlphaPhase2Time': '07:00',
                'AlphaPhase3Time': '23:30',
                'SigmaPhase2Time': '08:00'}
        base.update(settings)
        xbmcaddon.install('script.paragontv', base)
        import paragon_tv
        return paragon_tv

    def test_it_notices_when_paragon_tv_is_not_installed(self):
        import paragon_tv

        self.assertFalse(paragon_tv.installed())
        self.assertFalse(paragon_tv.enabled())
        self.assertEqual(paragon_tv.todays_preset(self.SATURDAY), '')

    def test_a_day_setting_holds_an_index_and_not_a_name(self):
        """Reading it as a name would silently find nothing at all."""
        tv = self.install_tv()

        self.assertEqual(tv.todays_preset(self.SATURDAY), 'Alpha')
        self.assertEqual(tv.preset_for_day(3), 'Sigma')

    def test_a_day_set_to_none_has_no_preset(self):
        tv = self.install_tv(SaturdayPreset='0')

        self.assertEqual(tv.todays_preset(self.SATURDAY), '')

    def test_an_index_out_of_range_is_no_preset_rather_than_a_crash(self):
        for value in ('99', '-1', 'Alpha', ''):
            tv = self.install_tv(SaturdayPreset=value)
            self.assertEqual(tv.todays_preset(self.SATURDAY), '',
                             'accepted %r as a preset index' % value)

    def test_phase_times_come_back_for_the_named_preset(self):
        tv = self.install_tv()

        self.assertEqual(tv.phase_time('Alpha', 1), '04:00')
        self.assertEqual(tv.phase_time('Alpha', 3), '23:30')
        self.assertEqual(tv.phase_time('Alpha', 9), '')

    def test_a_satellite_preset_has_no_maintenance_phase(self):
        """Sigma, Omicron, Theta and Lambda skip phase 1 entirely."""
        tv = self.install_tv(SigmaPhase1Time='04:00')

        self.assertEqual(tv.phase_time('Sigma', 1), '')
        self.assertEqual(tv.phase_time('Sigma', 2), '08:00')

    def test_a_phase_number_out_of_range_has_no_time(self):
        tv = self.install_tv()

        self.assertEqual(tv.phase_time('Alpha', 0), '')
        self.assertEqual(tv.phase_time('Alpha', 10), '')

    def test_the_status_says_what_today_holds(self):
        tv = self.install_tv()

        text = tv.status(self.SATURDAY)

        self.assertIn('Alpha', text)
        self.assertIn('04:00', text)
        self.assertIn('maintenance', text)

    def test_the_status_says_when_there_is_nothing_to_say(self):
        import paragon_tv
        self.assertIn('not installed', paragon_tv.status(self.SATURDAY))

        tv = self.install_tv(EnablePresetSystem='false')
        self.assertIn('switched off', tv.status(self.SATURDAY))

        tv = self.install_tv(SaturdayPreset='0')
        self.assertIn('no preset scheduled', tv.status(self.SATURDAY))

    # -- a sequence following a phase ----------------------------------------

    def app(self, tv=None):
        from paragon_home import ParagonHome

        app = ParagonHome()
        self.recorder = RecordingController()
        self.recorder.capabilities = lambda d: set(['power', 'state'])
        app.controller = self.recorder
        app._devices = [Device('AA:BB', name='Lamp', driver='govee',
                               lan=True)]
        app._scenes = [scene_lib.make_scene('Warshade', targets=['AA:BB'])]
        return app

    def following(self, phase):
        import sequences

        return sequences.make_sequence(
            'Curtain Up', [{'kind': 'scene', 'target': 'Warshade'}],
            phase=phase)

    def test_a_sequence_can_hang_off_a_phase_instead_of_a_clock(self):
        self.install_tv()
        app = self.app()
        app._sequences = [self.following(2)]

        # Alpha's phase 2 is 07:00, and today is a Saturday, which is Alpha.
        at_seven = self.SATURDAY.replace(hour=7, minute=0)

        self.assertEqual(app.run_due_sequences(now=at_seven), ['Curtain Up'])

    def test_it_does_not_run_at_another_phases_time(self):
        self.install_tv()
        app = self.app()
        app._sequences = [self.following(2)]

        self.assertEqual(
            app.run_due_sequences(now=self.SATURDAY.replace(hour=23, minute=30)),
            [])

    def test_it_follows_today_preset_rather_than_a_fixed_time(self):
        """The same sequence runs at a different hour on a different day."""
        self.install_tv()
        app = self.app()
        app._sequences = [self.following(2)]

        # Thursday is Sigma, whose phase 2 is 08:00 rather than 07:00.
        self.assertEqual(
            app.run_due_sequences(now=self.THURSDAY.replace(hour=7)), [])
        self.assertEqual(
            app.run_due_sequences(now=self.THURSDAY.replace(hour=8)),
            ['Curtain Up'])

    def test_it_does_not_run_on_a_day_with_no_preset(self):
        self.install_tv(SaturdayPreset='0')
        app = self.app()
        app._sequences = [self.following(2)]

        self.assertEqual(
            app.run_due_sequences(now=self.SATURDAY.replace(hour=7)), [])

    def test_it_does_not_run_when_paragon_tv_is_switched_off(self):
        """Paragon TV's own master switch governs this too."""
        self.install_tv(EnablePresetSystem='false')
        app = self.app()
        app._sequences = [self.following(2)]

        self.assertEqual(
            app.run_due_sequences(now=self.SATURDAY.replace(hour=7)), [])

    def test_it_does_nothing_at_all_without_paragon_tv(self):
        app = self.app()
        app._sequences = [self.following(2)]

        self.assertEqual(
            app.run_due_sequences(now=self.SATURDAY.replace(hour=7)), [])

    def test_a_phase_a_satellite_preset_lacks_simply_never_comes_round(self):
        self.install_tv()
        app = self.app()
        app._sequences = [self.following(1)]      # maintenance

        # Thursday is Sigma, which has no phase 1.
        self.assertEqual(
            app.run_due_sequences(now=self.THURSDAY.replace(hour=4)), [])
        # Saturday is Alpha, which does.
        self.assertEqual(
            app.run_due_sequences(now=self.SATURDAY.replace(hour=4)),
            ['Curtain Up'])

    def test_it_still_runs_only_once_a_day(self):
        self.install_tv()
        app = self.app()
        app._sequences = [self.following(2)]
        at_seven = self.SATURDAY.replace(hour=7)

        app.run_due_sequences(now=at_seven)

        self.assertEqual(
            app.run_due_sequences(now=at_seven + datetime.timedelta(minutes=2)),
            [])

    def test_moving_a_phase_time_re_arms_the_sequence(self):
        """The record holds the time, so a phase that moved counts as new."""
        tv = self.install_tv()
        app = self.app()
        app._sequences = [self.following(2)]
        app.run_due_sequences(now=self.SATURDAY.replace(hour=7))

        xbmcaddon.FOREIGN['script.paragontv']['AlphaPhase2Time'] = '09:00'

        self.assertEqual(
            app.run_due_sequences(now=self.SATURDAY.replace(hour=9)),
            ['Curtain Up'])

    def test_following_a_phase_reads_as_such(self):
        import sequences

        self.assertEqual(sequences.describe_schedule(self.following(3)),
                         'Paragon TV phase 3 (shut down)')

    def test_paragon_tv_settings_are_never_written_to(self):
        """A working television setup must not be disturbed by any of this."""
        tv = self.install_tv()
        before = dict(xbmcaddon.FOREIGN['script.paragontv'])
        app = self.app()
        app._sequences = [self.following(2)]

        app.run_due_sequences(now=self.SATURDAY.replace(hour=7))

        self.assertEqual(xbmcaddon.FOREIGN['script.paragontv'], before)


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


class TestColourMix(unittest.TestCase):
    """Several colours spread evenly but randomly over the lights."""

    @staticmethod
    def devices(count):
        return [Device('%02X' % i, name='Light %d' % i, lan=True)
                for i in range(count)]

    def test_even_split_over_many_lights(self):
        """25 lights and 3 colours must be 9/8/8, not whatever chance gives."""
        dealt = scene_lib.deal_colors(['R', 'G', 'B'], 25)
        counts = sorted([dealt.count(c) for c in ('R', 'G', 'B')])
        self.assertEqual(counts, [8, 8, 9])
        self.assertEqual(len(dealt), 25)

    def test_even_split_when_it_divides_exactly(self):
        dealt = scene_lib.deal_colors(['R', 'G'], 10)
        self.assertEqual(dealt.count('R'), 5)
        self.assertEqual(dealt.count('G'), 5)

    def test_more_colours_than_lights_uses_a_subset_once_each(self):
        dealt = scene_lib.deal_colors(['R', 'G', 'B', 'Y'], 2)
        self.assertEqual(len(dealt), 2)
        self.assertEqual(len(set(dealt)), 2)

    def test_the_spare_light_does_not_always_go_to_the_same_colour(self):
        """Repeat-and-truncate alone would hand it to the first colour every
        time, which is a visible bias over repeated applications."""
        winners = set()
        for _ in range(60):
            dealt = scene_lib.deal_colors(['R', 'G', 'B'], 25)
            winners.add([c for c in ('R', 'G', 'B') if dealt.count(c) == 9][0])
        self.assertEqual(winners, {'R', 'G', 'B'})

    def test_the_arrangement_changes_between_applications(self):
        first = scene_lib.deal_colors(['R', 'G', 'B'], 25)
        self.assertTrue(any(scene_lib.deal_colors(['R', 'G', 'B'], 25) != first
                            for _ in range(40)))

    def test_empty_inputs_are_handled(self):
        self.assertEqual(scene_lib.deal_colors([], 5), [])
        self.assertEqual(scene_lib.deal_colors(['R'], 0), [])

    def test_applying_a_mix_gives_every_light_one_of_the_colours(self):
        devices = self.devices(7)
        scene = scene_lib.make_scene(
            'Party', brightness=60, mode=scene_lib.MODE_MIX,
            colors=[{'name': 'Red', 'color': [255, 0, 0]},
                    {'name': 'Blue', 'color': [0, 0, 255]}])

        controller = RecordingController()
        applied, errors = scene_lib.apply_scene(controller, scene, devices)

        self.assertEqual(applied, 7)
        self.assertEqual(errors, [])
        colors = [c[2:] for c in controller.calls if c[0] == 'color']
        self.assertEqual(len(colors), 7)
        self.assertEqual(set(colors), {(255, 0, 0), (0, 0, 255)})
        # Even: 4 and 3, in some order.
        self.assertEqual(sorted([colors.count((255, 0, 0)),
                                 colors.count((0, 0, 255))]), [3, 4])

        # Brightness still applies to all of them.
        self.assertEqual(len([c for c in controller.calls
                              if c[0] == 'brightness']), 7)

    def test_applying_a_mix_does_not_write_back_into_the_scene(self):
        """The dealt colour is per-application, not a scene edit."""
        devices = self.devices(4)
        scene = scene_lib.make_scene(
            'Party', mode=scene_lib.MODE_MIX,
            colors=[{'name': 'Red', 'color': [255, 0, 0]},
                    {'name': 'Blue', 'color': [0, 0, 255]}])
        before = json.dumps(scene, sort_keys=True)

        scene_lib.apply_scene(RecordingController(), scene, devices)
        self.assertEqual(json.dumps(scene, sort_keys=True), before)

    def test_a_captured_per_light_entry_wins_over_the_mix(self):
        one, two = self.devices(2)
        scene = scene_lib.make_scene(
            'Party', mode=scene_lib.MODE_MIX,
            colors=[{'name': 'Red', 'color': [255, 0, 0]}],
            devices={one.device_id: {'power': 'on', 'brightness': 10,
                                     'mode': scene_lib.MODE_TEMP,
                                     'kelvin': 2200,
                                     'color': [255, 255, 255]}})

        controller = RecordingController()
        scene_lib.apply_scene(controller, scene, [one, two])

        self.assertIn(('temp', one.device_id, 2200), controller.calls)
        self.assertIn(('color', two.device_id, 255, 0, 0), controller.calls)

    def test_a_mix_survives_a_json_round_trip(self):
        scene = scene_lib.make_scene(
            'Party', mode=scene_lib.MODE_MIX,
            colors=[{'name': 'Red', 'color': [255, 0, 0]},
                    {'name': 'Blue', 'color': [0, 0, 255]}])
        restored = scene_lib.normalise(json.loads(json.dumps(scene)))

        self.assertEqual(restored['mode'], scene_lib.MODE_MIX)
        self.assertEqual([e['name'] for e in restored['colors']],
                         ['Red', 'Blue'])

    def test_a_mix_with_no_colours_degrades_to_leaving_colour_alone(self):
        scene = scene_lib.normalise({'name': 'Empty',
                                     'mode': scene_lib.MODE_MIX,
                                     'colors': []})
        self.assertEqual(scene['mode'], scene_lib.MODE_NONE)

    def test_bare_rgb_entries_are_accepted_and_named_by_hex(self):
        scene = scene_lib.normalise({'name': 'Hand edited',
                                     'mode': scene_lib.MODE_MIX,
                                     'colors': [[255, 40, 150], 'junk',
                                                {'color': [0, 255, 0]}]})
        self.assertEqual([e['name'] for e in scene['colors']],
                         ['#FF2896', '#00FF00'])

    def test_describe_mentions_the_mix(self):
        scene = scene_lib.make_scene(
            'Party', brightness=60, mode=scene_lib.MODE_MIX,
            colors=[{'name': 'Red', 'color': [255, 0, 0]},
                    {'name': 'Blue', 'color': [0, 0, 255]}])
        text = scene_lib.describe(scene)
        self.assertIn('mix of 2 colours', text)
        self.assertIn('60%', text)


class TestCycling(unittest.TestCase):
    """Stepping a mix scene through its colours on a timer."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'palette', 'scenes'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def build(self, count=4, interval=60):
        import scenes as fresh_scenes
        from paragon_home import ParagonHome

        app = ParagonHome()
        app._devices = [Device('%02X' % i, name='Light %d' % i, lan=True,
                               ip='10.0.0.%d' % (i + 1))
                        for i in range(count)]
        recorder = RecordingController()
        app.controller = recorder

        scene = fresh_scenes.make_scene(
            'Party', mode=fresh_scenes.MODE_MIX, cycle=interval,
            colors=[{'name': 'Red', 'color': [255, 0, 0]},
                    {'name': 'Green', 'color': [0, 255, 0]},
                    {'name': 'Blue', 'color': [0, 0, 255]}])
        app.save_scene(scene)
        return app, recorder, fresh_scenes

    def test_rotation_keeps_the_even_spread(self):
        import scenes as fresh_scenes

        ids = ['%02X' % i for i in range(25)]
        assignment = fresh_scenes.deal_assignment(3, ids)
        for _step in range(6):
            counts = sorted([list(assignment.values()).count(i)
                             for i in range(3)])
            self.assertEqual(counts, [8, 8, 9])
            assignment = fresh_scenes.rotate_assignment(assignment, 3)

    def test_rotation_moves_every_light_to_a_new_colour(self):
        import scenes as fresh_scenes

        before = fresh_scenes.deal_assignment(3, ['A', 'B', 'C', 'D'])
        after = fresh_scenes.rotate_assignment(before, 3)
        for key in before:
            self.assertNotEqual(before[key], after[key])

    def test_applying_a_cycling_scene_starts_the_cycle(self):
        app, _recorder, _lib = self.build()

        app.apply_scene(app.scene_by_name('Party'))

        state = app.read_cycle()
        self.assertIsNotNone(state)
        self.assertEqual(state['scene'], 'Party')
        self.assertEqual(state['interval'], 60)
        self.assertEqual(len(state['assignment']), 4)
        self.assertTrue(os.path.isfile(os.path.join(PROFILE, 'cycle.json')))

    def test_applying_a_plain_scene_stops_a_running_cycle(self):
        """Otherwise dimming for a film gets overwritten by the party."""
        app, _recorder, lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))
        self.assertIsNotNone(app.read_cycle())

        app.apply_scene(lib.make_scene('Movie Night', brightness=8,
                                       mode=lib.MODE_TEMP, kelvin=2000))
        self.assertIsNone(app.read_cycle())

    def test_a_step_advances_every_light_and_reschedules(self):
        app, recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))
        before = dict(app.read_cycle()['assignment'])
        del recorder.calls[:]

        self.assertTrue(app.cycle_step(now=1000.0))

        after = app.read_cycle()
        for key in before:
            self.assertNotEqual(before[key], after['assignment'][key])
        self.assertEqual(after['next_at'], 1000.0 + 60)

        colors = [c for c in recorder.calls if c[0] == 'color']
        self.assertEqual(len(colors), 4)

    def test_a_step_sends_only_the_colour(self):
        """Power and brightness are already where the last step left them."""
        app, recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))
        del recorder.calls[:]

        app.cycle_step(now=1000.0)

        kinds = set(c[0] for c in recorder.calls)
        self.assertEqual(kinds, set(['color']))
        self.assertEqual(len(recorder.calls), 4)

    def test_the_first_apply_still_sets_power_and_brightness(self):
        app, recorder, lib = self.build()
        scene = app.scene_by_name('Party')
        scene['brightness'] = 60
        app.save_scene(scene)
        del recorder.calls[:]

        app.apply_scene(app.scene_by_name('Party'))

        kinds = set(c[0] for c in recorder.calls)
        self.assertEqual(kinds, set(['turn', 'brightness', 'color']))

    def test_sends_are_paced_across_the_lights(self):
        """A burst of datagrams to every bulb at once gets dropped."""
        import scenes as fresh_scenes

        devices = [Device('%02X' % i, name='L%d' % i, lan=True)
                   for i in range(6)]
        gaps = []
        fresh_scenes.apply_scene(
            RecordingController(),
            fresh_scenes.make_scene('Solid', mode=fresh_scenes.MODE_COLOR,
                                    color=[1, 2, 3]),
            devices, sleep_func=lambda s: gaps.append(s))

        # One pause between each pair of lights, none before the first.
        self.assertEqual(len(gaps), 5)
        self.assertTrue(all(g > 0 for g in gaps))

    def test_pacing_can_be_switched_off(self):
        import scenes as fresh_scenes

        gaps = []
        fresh_scenes.apply_scene(
            RecordingController(),
            fresh_scenes.make_scene('Solid', mode=fresh_scenes.MODE_COLOR),
            [Device('AA', name='L', lan=True), Device('BB', name='M', lan=True)],
            gap=0, sleep_func=lambda s: gaps.append(s))
        self.assertEqual(gaps, [])

    def test_colours_only_still_honours_temperature_entries(self):
        import scenes as fresh_scenes

        controller = RecordingController()
        device = Device('AA', name='L', lan=True)
        fresh_scenes.apply_settings(
            controller, device,
            {'power': 'on', 'brightness': 50, 'mode': fresh_scenes.MODE_TEMP,
             'kelvin': 2700, 'color': [255, 255, 255]},
            colors_only=True)
        self.assertEqual(controller.calls, [('temp', 'AA', 2700)])

    def test_not_due_yet_means_no_step(self):
        app, _recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))
        state = app.read_cycle()

        self.assertIsNone(app.cycle_due(now=state['next_at'] - 1))
        self.assertIsNotNone(app.cycle_due(now=state['next_at']))

    def test_a_clock_jump_backwards_does_not_stall_for_hours(self):
        app, _recorder, _lib = self.build(interval=60)
        app.apply_scene(app.scene_by_name('Party'))
        state = app.read_cycle()

        # next_at far in the future relative to "now" -- a wrong clock, not a
        # cycle that genuinely has hours to wait.
        self.assertIsNotNone(app.cycle_due(now=state['next_at'] - 10000))

    def test_lights_added_after_the_cycle_started_are_dealt_in(self):
        app, recorder, _lib = self.build(count=3)
        app.apply_scene(app.scene_by_name('Party'))

        app._devices.append(Device('FF', name='Newcomer', lan=True,
                                   ip='10.0.0.9'))
        del recorder.calls[:]
        app.cycle_step(now=2000.0)

        self.assertIn('FF', app.read_cycle()['assignment'])
        self.assertEqual(len([c for c in recorder.calls if c[0] == 'color']), 4)

    def test_a_deleted_scene_stops_the_cycle_rather_than_erroring(self):
        app, _recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))

        app._scenes = [s for s in app.scenes if s['name'] != 'Party']
        app.save_scenes()

        self.assertFalse(app.cycle_step(now=3000.0))
        self.assertIsNone(app.read_cycle())

    def test_stopping_leaves_the_lights_where_they_are(self):
        app, recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))
        del recorder.calls[:]

        self.assertEqual(app.stop_cycle(), 'Party')
        self.assertIsNone(app.read_cycle())
        self.assertEqual(recorder.calls, [])
        self.assertIsNone(app.stop_cycle())

    def test_a_cycle_survives_being_reloaded_from_disk(self):
        """The panel starts it; the service is a different process."""
        app, _recorder, _lib = self.build()
        app.apply_scene(app.scene_by_name('Party'))
        expected = dict(app.read_cycle()['assignment'])

        from paragon_home import ParagonHome
        service_side = ParagonHome()
        service_side._devices = app._devices
        service_side.controller = RecordingController()

        state = service_side.read_cycle()
        self.assertEqual(state['scene'], 'Party')
        self.assertEqual(state['assignment'], expected)

        service_side.cycle_step(now=4000.0)
        self.assertEqual(len([c for c in service_side.controller.calls
                              if c[0] == 'color']), 4)

    def test_cycle_is_only_kept_for_a_mix(self):
        import scenes as fresh_scenes

        scene = fresh_scenes.normalise({'name': 'Solid', 'cycle': 60,
                                        'mode': fresh_scenes.MODE_TEMP})
        self.assertEqual(scene['cycle'], 0)

        scene = fresh_scenes.normalise({
            'name': 'Party', 'cycle': 60, 'mode': fresh_scenes.MODE_MIX,
            'colors': [[255, 0, 0], [0, 0, 255]]})
        self.assertEqual(scene['cycle'], 60)

    def test_a_cycling_scene_survives_a_json_round_trip(self):
        app, _recorder, _lib = self.build()
        saved = json.load(open(os.path.join(PROFILE, 'scenes.json')))
        party = [s for s in saved if s['name'] == 'Party'][0]
        self.assertEqual(party['cycle'], 60)


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


class FakeRM(object):
    """A UDP socket that answers like a real Broadlink RM.

    Speaks the actual protocol -- checksums, AES, the auth key swap -- so the
    client is exercised against the wire format rather than against a mock of
    itself.
    """

    LEARNED = b'\x26\x00\x28\x00' + b'\x10' * 36

    def __init__(self, devtype=0x27c2, name='Lounge RM', new_framing=False):
        import broadlink_lan as bl

        self.bl = bl
        self.devtype = devtype
        # Which data payload layout this device accepts. Anything else is
        # refused with an error, exactly as the hardware does.
        self.new_framing = new_framing
        # Refuse every data command regardless of layout, to stand in for a
        # device that is genuinely failing rather than mismatched.
        self.refuse_all = False
        self.name = name
        self.mac = bytearray([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF])
        self.session_key = bytearray(range(0x10, 0x20))
        self.device_id = bytearray([1, 2, 3, 4])
        self.sent_codes = []
        self.learning = False

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(0.2)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve)
        self.thread.daemon = True
        self.thread.start()

    def close(self):
        self._stop.set()
        self.thread.join(timeout=2)
        self.sock.close()

    def _hello_reply(self):
        reply = bytearray(0x40 + len(self.name) + 1)
        reply[0x34] = self.devtype & 0xFF
        reply[0x35] = (self.devtype >> 8) & 0xFF
        reply[0x3A:0x40] = self.mac
        reply[0x40:0x40 + len(self.name)] = bytearray(self.name.encode())
        return bytes(reply)

    def _wrap(self, payload, key):
        from aes import AES

        payload = bytearray(payload)
        if len(payload) % 16:
            payload.extend(bytearray(16 - len(payload) % 16))
        packet = bytearray(0x38)
        packet.extend(bytearray(AES(key, self.bl.INITIAL_IV).encrypt(payload)))
        return bytes(packet)

    def _refuse(self, sender, code=0xFFFB):
        """Answer with a device error, the way a real one refuses."""
        error = bytearray(0x38)
        error[0x22] = code & 0xFF
        error[0x23] = (code >> 8) & 0xFF
        self.sock.sendto(bytes(error), sender)

    def _reply(self, data):
        """A data reply in whichever layout this device speaks."""
        import struct as _struct

        if self.new_framing:
            body = bytearray(_struct.pack('<I', 0)) + bytearray(data)
            payload = bytearray(_struct.pack('<H', len(body))) + body
        else:
            payload = bytearray(4) + bytearray(data)
        return self._wrap(payload, self.session_key)

    def _serve(self):
        from aes import AES

        while not self._stop.is_set():
            try:
                data, sender = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break

            raw = bytearray(data)
            if len(raw) == 0x30:                      # discovery hello
                self.sock.sendto(self._hello_reply(), sender)
                continue
            if len(raw) < 0x38:
                continue

            command = raw[0x26] | (raw[0x27] << 8)
            if command == self.bl.CMD_AUTH:
                key = self.bl.INITIAL_KEY
                payload = bytearray(0x40)
                payload[0x00:0x04] = self.device_id
                payload[0x04:0x14] = self.session_key
                self.sock.sendto(self._wrap(payload, key), sender)
                continue

            body = bytes(raw[0x38:])
            request = bytearray(
                AES(self.session_key, self.bl.INITIAL_IV).decrypt(body))

            if self.refuse_all:
                self._refuse(sender, code=0xFFF3)
                continue

            # Refuse the wrong payload layout, which is what a real device
            # does: it authenticates fine and then rejects every command.
            if self.new_framing:
                declared = request[0] | (request[1] << 8)
                if declared < 4 or declared > len(request):
                    self._refuse(sender)
                    continue
                verb, data = request[2], request[6:]
            else:
                if request[1] or request[2] or request[3]:
                    self._refuse(sender)
                    continue
                verb, data = request[0], request[4:]

            if verb == self.bl.DATA_SEND:
                self.sent_codes.append(bytes(data))
                self.sock.sendto(self._reply(bytearray(16)), sender)
            elif verb == self.bl.DATA_LEARN:
                self.learning = True
                self.sock.sendto(self._reply(bytearray(16)), sender)
            elif verb == self.bl.DATA_CHECK:
                if not self.learning:
                    self._refuse(sender, code=0xFFF9)
                else:
                    self.sock.sendto(self._reply(bytearray(self.LEARNED)),
                                     sender)


class TestBroadlinkProtocol(unittest.TestCase):
    """The RM client, against a device speaking the real wire format."""

    def setUp(self):
        self.device = FakeRM()
        self.addCleanup(self.device.close)
        import broadlink_lan as bl
        self.bl = bl
        # The fake listens on an ephemeral port; the real one is always 80.
        self._saved_port = bl.DEVICE_PORT
        bl.DEVICE_PORT = self.device.port

    def tearDown(self):
        self.bl.DEVICE_PORT = self._saved_port

    def session(self):
        session = self.bl.Session('127.0.0.1', self.device.mac,
                                  self.device.devtype, timeout=2.0)
        session.authenticate()
        return session

    def test_checksum_is_seeded_at_beaf(self):
        self.assertEqual(self.bl.checksum(b''), 0xBEAF)
        self.assertEqual(self.bl.checksum(b'\x01'), 0xBEB0)
        # Wraps at 16 bits rather than growing.
        self.assertEqual(self.bl.checksum(b'\xff' * 1000),
                         (0xBEAF + 255 * 1000) & 0xFFFF)

    def test_error_codes_are_decoded_as_signed(self):
        """0xffff is -1, which is what every reference calls it."""
        self.assertIn('Authentication failed', self.bl.error_text(0xFFFF))
        self.assertIn('-1', self.bl.error_text(0xFFFF))
        self.assertIn('You have been logged out', self.bl.error_text(0xFFFE))
        self.assertIn('Control key is expired', self.bl.error_text(0xFFF7))

    def test_an_unnamed_code_still_shows_its_signed_value(self):
        text = self.bl.error_text(0xFFF9)
        self.assertIn('-7', text)
        self.assertIn('0xfff9', text)

    def test_auth_errors_are_recognised_as_such(self):
        self.assertTrue(self.bl.is_auth_error(0xFFFF))
        self.assertTrue(self.bl.is_auth_error(0xFFF7))
        self.assertFalse(self.bl.is_auth_error(0xFFF9))
        self.assertFalse(self.bl.is_auth_error(0x000B))

    def test_an_auth_error_carries_the_unlock_advice(self):
        """The number alone tells the user nothing they can act on."""
        session = self.bl.Session('10.0.0.1', b'\x01\x02\x03\x04\x05\x06',
                                  0x27c2)
        reply = bytearray(0x38)
        reply[0x22] = 0xFF
        reply[0x23] = 0xFF

        with self.assertRaises(self.bl.BroadlinkError) as caught:
            session.parse_response(reply)

        message = str(caught.exception)
        self.assertIn('Authentication failed', message)
        self.assertIn('Lock device', message)

    def test_a_non_auth_error_does_not_blame_the_app_lock(self):
        session = self.bl.Session('10.0.0.1', b'\x01\x02\x03\x04\x05\x06',
                                  0x27c2)
        reply = bytearray(0x38)
        reply[0x22] = 0x0B

        with self.assertRaises(self.bl.BroadlinkError) as caught:
            session.parse_response(reply)
        self.assertNotIn('Lock device', str(caught.exception))

    def test_hello_is_48_bytes_with_a_valid_checksum(self):
        packet = bytearray(self.bl.build_hello('192.168.1.50', 4321))
        self.assertEqual(len(packet), 0x30)

        stored = packet[0x20] | (packet[0x21] << 8)
        packet[0x20] = packet[0x21] = 0
        self.assertEqual(self.bl.checksum(packet), stored)

    def test_hello_carries_the_local_address_and_port(self):
        packet = bytearray(self.bl.build_hello('192.168.1.50', 0x1234))
        self.assertEqual(list(packet[0x18:0x1c]), [192, 168, 1, 50])
        self.assertEqual(packet[0x1c], 0x34)
        self.assertEqual(packet[0x1d], 0x12)

    def test_a_packet_carries_the_magic_header_and_both_checksums(self):
        session = self.bl.Session('10.0.0.1', b'\x01\x02\x03\x04\x05\x06',
                                  0x27c2)
        packet = bytearray(session.build_packet(self.bl.CMD_DATA,
                                                bytearray([2, 0, 0, 0])))

        self.assertEqual(list(packet[0:4]), [0x5A, 0xA5, 0xAA, 0x55])
        self.assertEqual(packet[0x26], self.bl.CMD_DATA & 0xFF)
        stored = packet[0x20] | (packet[0x21] << 8)
        packet[0x20] = packet[0x21] = 0
        self.assertEqual(self.bl.checksum(packet), stored)

    def test_the_counter_advances_between_packets(self):
        session = self.bl.Session('10.0.0.1', b'\x01\x02\x03\x04\x05\x06',
                                  0x27c2)
        first = bytearray(session.build_packet(self.bl.CMD_DATA, b'\x02'))
        second = bytearray(session.build_packet(self.bl.CMD_DATA, b'\x02'))
        self.assertNotEqual(first[0x28:0x2a], second[0x28:0x2a])

    def test_discovery_finds_the_device(self):
        transport = self.bl.BroadlinkTransport(timeout=2.0)
        # The fake is bound to loopback, which a real 255.255.255.255
        # broadcast would never reach, so the hello is aimed there instead.
        saved = (self.bl.BROADCAST_ADDRESS, self.bl.BROADCAST_PORT)
        self.bl.BROADCAST_ADDRESS = '127.0.0.1'
        self.bl.BROADCAST_PORT = self.device.port
        try:
            found = [d for d in transport.discover(
                timeout=1.5, local_addresses=['127.0.0.1'])
                if d['mac'].startswith('FF:EE')]
        finally:
            (self.bl.BROADCAST_ADDRESS, self.bl.BROADCAST_PORT) = saved

        self.assertTrue(found, 'no Broadlink device discovered')
        self.assertEqual(found[0]['devtype'], 0x27c2)
        self.assertEqual(found[0]['label'], 'RM Mini 3')
        self.assertEqual(found[0]['name'], 'Lounge RM')

    def test_authentication_swaps_in_the_session_key(self):
        session = self.session()

        self.assertTrue(session.authenticated)
        self.assertEqual(bytearray(session.key), self.device.session_key)
        self.assertEqual(bytearray(session.device_id), self.device.device_id)
        self.assertNotEqual(bytearray(session.key),
                            bytearray(self.bl.INITIAL_KEY))

    def test_sending_a_code_reaches_the_device_intact(self):
        session = self.session()
        code = b'\x26\x00\x20\x00' + b'\x33' * 28

        session.send_code(code)
        self.assertEqual(len(self.device.sent_codes), 1)
        self.assertTrue(self.device.sent_codes[0].startswith(code))

    def test_learning_returns_the_captured_code(self):
        session = self.session()

        self.assertIsNone(session.check_learned())   # nothing pressed yet
        session.enter_learning()
        learned = session.check_learned()

        self.assertIsNotNone(learned)
        self.assertTrue(learned.startswith(b'\x26\x00'))

    def test_new_framing_is_length_prefixed(self):
        """<H length> + <I verb> + data, length covering verb and data."""
        import struct as _struct

        session = self.bl.Session('10.0.0.1', b'\x01' * 6, 0x5f36)
        self.assertTrue(session.new_framing)

        payload = bytearray(session.build_data_payload(self.bl.DATA_SEND,
                                                       b'\xAA\xBB'))
        self.assertEqual(_struct.unpack('<H', bytes(payload[0:2]))[0], 6)
        self.assertEqual(payload[2], self.bl.DATA_SEND)
        self.assertEqual(bytes(payload[6:]), b'\xAA\xBB')

    def test_old_framing_has_no_prefix(self):
        session = self.bl.Session('10.0.0.1', b'\x01' * 6, 0x2712)
        self.assertFalse(session.new_framing)

        payload = bytearray(session.build_data_payload(self.bl.DATA_SEND,
                                                       b'\xAA\xBB'))
        self.assertEqual(payload[0], self.bl.DATA_SEND)
        self.assertEqual(bytes(payload[4:]), b'\xAA\xBB')

    def test_the_framing_guess_comes_from_the_device_type(self):
        self.assertIn(0x5f36, self.bl.NEW_FRAMING_TYPES)
        self.assertNotIn(0x2712, self.bl.NEW_FRAMING_TYPES)


class TestBroadlinkNewFraming(unittest.TestCase):
    """A device wanting the newer payload layout, e.g. a later RM Mini 3."""

    def setUp(self):
        # devtype 0x27c2 is on the old list, but this unit wants the new
        # layout -- exactly the mismatch that hardware sold as "RM Mini 3"
        # produces, and the reason the guess has to be correctable.
        self.device = FakeRM(devtype=0x27c2, name='Bedroom RM',
                             new_framing=True)
        self.addCleanup(self.device.close)
        import broadlink_lan as bl
        self.bl = bl
        self._saved = bl.DEVICE_PORT
        bl.DEVICE_PORT = self.device.port

    def tearDown(self):
        self.bl.DEVICE_PORT = self._saved

    def session(self):
        session = self.bl.Session('127.0.0.1', self.device.mac,
                                  self.device.devtype, timeout=2.0)
        session.authenticate()
        return session

    def test_authentication_succeeds_even_with_the_wrong_framing_guess(self):
        """Auth is layout-independent, which is why only commands failed."""
        session = self.session()
        self.assertTrue(session.authenticated)
        self.assertFalse(session.new_framing)   # guessed wrong, on purpose

    def test_a_command_corrects_the_framing_and_succeeds(self):
        session = self.session()
        code = b'\x26\x00\x10\x00' + b'\x44' * 12

        session.send_code(code)

        self.assertTrue(session.new_framing, 'framing was not corrected')
        self.assertEqual(len(self.device.sent_codes), 1)
        self.assertTrue(self.device.sent_codes[0].startswith(code))

    def test_the_correction_sticks_for_later_commands(self):
        session = self.session()
        session.send_code(b'\x26\x00\x04\x00')
        session.send_code(b'\x26\x00\x04\x00')
        self.assertEqual(len(self.device.sent_codes), 2)

    def test_learning_works_once_the_framing_is_right(self):
        session = self.session()
        session.enter_learning()
        learned = session.check_learned()

        self.assertIsNotNone(learned)
        self.assertTrue(learned.startswith(b'\x26\x00'))

    def test_a_genuinely_broken_command_reports_the_original_error(self):
        """Retrying the other way must not disguise a real failure."""
        session = self.session()
        session.new_framing = True          # already correct

        # Neither layout will satisfy a device that is simply refusing.
        self.device.refuse_all = True
        with self.assertRaises(self.bl.BroadlinkError) as caught:
            session.send_code(b'\x26\x00')

        # The error reported is the first one, from the layout we believed in,
        # not whatever the speculative retry produced.
        self.assertIn('-13', str(caught.exception))
        self.assertTrue(session.new_framing, 'framing flipped on a real error')

    def test_a_device_error_is_raised_not_swallowed(self):
        session = self.session()
        error = bytearray(0x38)
        error[0x22] = 0x0B
        with self.assertRaises(self.bl.BroadlinkError):
            session.parse_response(error)

    def test_a_short_reply_is_rejected(self):
        session = self.session()
        with self.assertRaises(self.bl.BroadlinkError):
            session.parse_response(bytearray(4))

    def test_an_unreachable_device_reports_clearly(self):
        session = self.bl.Session('127.0.0.1', self.device.mac,
                                  self.device.devtype, timeout=0.3)
        saved = self.bl.DEVICE_PORT
        self.bl.DEVICE_PORT = 1        # nothing listening
        try:
            with self.assertRaises(self.bl.BroadlinkError) as caught:
                session.authenticate()
        finally:
            self.bl.DEVICE_PORT = saved
        self.assertIn('127.0.0.1', str(caught.exception))

    def test_unknown_device_types_still_get_a_label(self):
        self.assertEqual(self.bl.device_label(0x27c2), 'RM Mini 3')
        self.assertEqual(self.bl.device_label(0xABCD), 'Broadlink abcd')


class TestBroadlinkDriver(unittest.TestCase):
    """The driver, against the fake RM speaking the real protocol."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'broadlink_driver',
                     'broadlink_lan', 'hub'):
            if name in sys.modules:
                del sys.modules[name]

        self.rm = FakeRM()
        self.addCleanup(self.rm.close)
        import broadlink_lan as bl
        self.bl = bl
        self._saved = bl.DEVICE_PORT
        bl.DEVICE_PORT = self.rm.port
        self.addCleanup(self._restore)

    def _restore(self):
        self.bl.DEVICE_PORT = self._saved
        clean_profile()

    def driver(self, codes=None):
        from broadlink_driver import BroadlinkDriver

        self.saved = []
        return BroadlinkDriver(
            transport=self.bl.BroadlinkTransport(timeout=2.0),
            codes=codes if codes is not None else {},
            save_codes=lambda: self.saved.append(True))

    def device(self):
        return Device('FF:EE:DD:CC:BB:AA', name='Lounge RM',
                      driver='broadlink', ip='127.0.0.1', lan=True,
                      devtype=0x27c2)

    def test_an_rm_claims_commands_and_nothing_else(self):
        from devices import CAP_COLOR, CAP_COMMANDS, CAP_POWER

        caps = self.driver().capabilities(self.device())
        self.assertEqual(caps, set([CAP_COMMANDS]))
        self.assertNotIn(CAP_POWER, caps)
        self.assertNotIn(CAP_COLOR, caps)

    def test_the_state_verbs_refuse_rather_than_pretend(self):
        driver, device = self.driver(), self.device()
        for call in (lambda: driver.turn(device, True),
                     lambda: driver.set_brightness(device, 50),
                     lambda: driver.set_color(device, 1, 2, 3),
                     lambda: driver.set_color_temp(device, 2700)):
            self.assertRaises(ControlError, call)
        self.assertIsNone(driver.get_state(device))

    def test_learning_captures_a_code_and_saves_it(self):
        driver, device = self.driver(), self.device()

        driver.start_learning(device)
        code = driver.collect_learned(device)
        self.assertIsNotNone(code)
        self.assertTrue(code.startswith('2600'))

        self.assertTrue(driver.save_command(device, 'AVR Power', code))
        self.assertEqual(driver.commands(device), ['AVR Power'])
        self.assertTrue(self.saved, 'codes were not persisted')

    def test_nothing_learned_yet_reads_as_still_waiting(self):
        driver, device = self.driver(), self.device()
        self.assertIsNone(driver.collect_learned(device))

    def test_a_learned_code_can_be_fired_back(self):
        driver, device = self.driver(), self.device()

        driver.start_learning(device)
        driver.save_command(device, 'AVR Power',
                            driver.collect_learned(device))
        driver.send_command(device, 'AVR Power')

        self.assertEqual(len(self.rm.sent_codes), 1)
        self.assertTrue(self.rm.sent_codes[0].startswith(b'\x26\x00'))

    def test_test_connection_reports_success(self):
        driver, device = self.driver(), self.device()
        ok, message = driver.test_connection(device)
        self.assertTrue(ok)
        self.assertIn('Lounge RM', message)

    def test_test_connection_explains_a_locked_device(self):
        """The RM Mini 3 case: discovered fine, refuses the handshake."""
        from broadlink_driver import BroadlinkDriver

        class Locked(object):
            def session(self, ip, mac, devtype):
                raise self_bl.BroadlinkError(
                    'Could not authenticate with %s.\n\n%s: %s\n\n%s'
                    % (ip, ip, self_bl.error_text(0xFFFF),
                       self_bl.AUTH_ADVICE))

            def forget_session(self, ip):
                pass

        self_bl = self.bl
        driver = BroadlinkDriver(transport=Locked())
        ok, message = driver.test_connection(self.device())

        self.assertFalse(ok)
        self.assertIn('Authentication failed', message)
        self.assertIn('Lock device', message)

    def test_an_unknown_command_is_refused_clearly(self):
        driver, device = self.driver(), self.device()
        with self.assertRaises(ControlError) as caught:
            driver.send_command(device, 'Nope')
        self.assertIn('Nope', str(caught.exception))

    def test_a_corrupt_saved_code_is_reported_not_sent(self):
        driver = self.driver(codes={'FF:EE:DD:CC:BB:AA':
                                    {'Bad': 'not hex at all'}})
        with self.assertRaises(ControlError):
            driver.send_command(self.device(), 'Bad')
        self.assertEqual(self.rm.sent_codes, [])

    def test_forgetting_a_command_persists(self):
        driver, device = self.driver(codes={'FF:EE:DD:CC:BB:AA':
                                            {'Old': '2600'}}), self.device()
        self.assertTrue(driver.forget_command(device, 'Old'))
        self.assertEqual(driver.commands(device), [])
        self.assertFalse(driver.forget_command(device, 'Old'))

    def test_mac_bytes_are_rebuilt_from_the_device_id_after_a_restart(self):
        """Nothing but device_id and devtype survives to disk."""
        driver = self.driver()
        restored = Device.from_dict(self.device().to_dict())
        self.assertFalse(hasattr(restored, 'mac_bytes'))
        self.assertEqual(restored.devtype, 0x27c2)

        session = driver._session(restored)
        self.assertEqual(bytearray(session.mac), self.rm.mac)

    def test_a_scene_action_fires_a_learned_code(self):
        """End to end: a scene reaching real protocol code."""
        import hub as hub_mod
        import scenes as fresh_scenes

        driver, device = self.driver(), self.device()
        driver.start_learning(device)
        driver.save_command(device, 'AVR Power',
                            driver.collect_learned(device))

        hub = hub_mod.Hub(drivers=[driver])
        scene = fresh_scenes.make_scene(
            'Movie Night', targets=['NOBODY'],
            actions=[{'device': 'FF:EE:DD:CC:BB:AA',
                      'command': 'AVR Power'}])

        _applied, errors = fresh_scenes.apply_scene(hub, scene, [device])
        self.assertEqual(errors, [])
        self.assertEqual(len(self.rm.sent_codes), 1)

    def test_discovery_builds_devices_the_registry_can_store(self):
        driver = self.driver()
        saved = (self.bl.BROADCAST_ADDRESS, self.bl.BROADCAST_PORT)
        self.bl.BROADCAST_ADDRESS = '127.0.0.1'
        self.bl.BROADCAST_PORT = self.rm.port
        try:
            found, warnings = driver.discover(timeout=1.5)
        finally:
            (self.bl.BROADCAST_ADDRESS, self.bl.BROADCAST_PORT) = saved

        self.assertEqual(warnings, [])
        rm = [d for d in found if d.device_id.startswith('FF:EE')]
        self.assertTrue(rm, 'RM not discovered')
        self.assertEqual(rm[0].driver, 'broadlink')
        self.assertEqual(rm[0].model, 'RM Mini 3')
        self.assertEqual(rm[0].devtype, 0x27c2)

        # And it round-trips through the device cache unchanged.
        restored = Device.from_dict(json.loads(json.dumps(rm[0].to_dict())))
        self.assertEqual(restored.driver, 'broadlink')
        self.assertEqual(restored.devtype, 0x27c2)

    def test_a_failed_search_becomes_a_warning_not_an_exception(self):
        from broadlink_driver import BroadlinkDriver

        class Broken(object):
            def discover(self, timeout=3.0):
                raise RuntimeError('no network')

        found, warnings = BroadlinkDriver(transport=Broken()).discover()
        self.assertEqual(found, [])
        self.assertTrue(any('no network' in w for w in warnings))


class TestTuyaDiscovery(unittest.TestCase):
    """Finding Tuya plugs, which announce themselves rather than answer."""

    @staticmethod
    def frame(payload, return_code=False):
        import struct as _struct
        import zlib as _zlib
        import tuya_lan as t

        if return_code:
            head = (_struct.pack('>4I', t.PREFIX, 0, 0, len(payload) + 12)
                    + _struct.pack('>I', 0))
        else:
            head = _struct.pack('>4I', t.PREFIX, 0, 0, len(payload) + 8)
        body = head + payload
        return (body + _struct.pack('>I', _zlib.crc32(body) & 0xFFFFFFFF)
                + _struct.pack('>I', t.SUFFIX))

    def test_a_clear_31_broadcast_is_understood(self):
        import tuya_lan as t

        payload = json.dumps({'gwId': 'wp9abc', 'ip': '10.0.0.55',
                              'version': '3.1'}).encode()
        for return_code in (False, True):
            found = t.parse_broadcast(self.frame(payload, return_code))
            self.assertEqual(found['device_id'], 'wp9abc')
            self.assertEqual(found['ip'], '10.0.0.55')
            self.assertEqual(found['version'], '3.1')

    def test_an_encrypted_33_broadcast_is_understood(self):
        import tuya_lan as t
        from aes import AESECB

        payload = AESECB(t.DISCOVERY_KEY).encrypt(json.dumps(
            {'gwId': 'wp9xyz', 'ip': '10.0.0.71',
             'version': '3.3'}).encode())
        for return_code in (False, True):
            found = t.parse_broadcast(self.frame(payload, return_code))
            self.assertEqual(found['device_id'], 'wp9xyz')
            self.assertEqual(found['version'], '3.3')

    def test_both_payload_offsets_are_tried(self):
        """Neither offset is announced, so guessing one would find nothing."""
        import tuya_lan as t

        payload = json.dumps({'gwId': 'a', 'ip': '1.2.3.4'}).encode()
        self.assertIsNotNone(t.parse_broadcast(self.frame(payload, False)))
        self.assertIsNotNone(t.parse_broadcast(self.frame(payload, True)))

    def test_rubbish_is_ignored(self):
        import tuya_lan as t

        for junk in (b'', b'short', b'garbage' * 10,
                     self.frame(b'not json at all')):
            self.assertIsNone(t.parse_broadcast(junk))

    def test_a_broadcast_without_an_id_is_ignored(self):
        import tuya_lan as t

        payload = json.dumps({'ip': '10.0.0.9'}).encode()
        self.assertIsNone(t.parse_broadcast(self.frame(payload)))

    def test_devid_is_accepted_as_well_as_gwid(self):
        import tuya_lan as t

        payload = json.dumps({'devId': 'alt', 'ip': '10.0.0.9'}).encode()
        self.assertEqual(t.parse_broadcast(self.frame(payload))['device_id'],
                         'alt')


class TestTuyaDiagnostics(unittest.TestCase):
    """Silence has several causes and they need different answers."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'diagnostics', 'tuya_lan'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    @staticmethod
    def report(**overrides):
        base = {'ports': {6666: 'listening', 6667: 'listening'},
                'raw': [], 'devices': [], 'listened': 8.0,
                'other_traffic': 0}
        base.update(overrides)
        return base

    def test_devices_heard_are_listed_with_their_protocol(self):
        import diagnostics

        text = diagnostics.tuya_summary(self.report(devices=[
            {'device_id': 'wp9abc', 'ip': '10.0.0.55', 'version': '3.3'}]))
        self.assertIn('wp9abc', text)
        self.assertIn('10.0.0.55', text)
        self.assertIn('3.3', text)

    def test_both_ports_blocked_blames_another_program(self):
        import diagnostics

        text = diagnostics.tuya_summary(self.report(
            ports={6666: 'could not bind: in use',
                   6667: 'could not bind: in use'}))
        self.assertIn('holding them', text)

    def test_silence_lists_the_causes_in_order(self):
        import diagnostics

        text = diagnostics.tuya_summary(self.report())
        self.assertIn('firewall', text)
        self.assertIn('different network', text)
        self.assertIn('GHome app', text)

    def test_traffic_that_is_not_tuya_is_its_own_verdict(self):
        import diagnostics

        text = diagnostics.tuya_summary(self.report(other_traffic=4))
        self.assertIn('none was a Tuya', text)

    def test_the_log_carries_raw_bytes_for_anything_unrecognised(self):
        import diagnostics

        lines = '\n'.join(diagnostics.tuya_lines(self.report(
            other_traffic=1,
            raw=[{'port': 6667, 'from': '10.0.0.55', 'bytes': 40,
                  'hex': 'deadbeef', 'parsed': False}])))
        self.assertIn('deadbeef', lines)
        self.assertIn('10.0.0.55', lines)
        self.assertIn('UDP 6666', lines)

    def test_a_probe_with_no_devices_still_reports_its_ports(self):
        import tuya_lan

        report = tuya_lan.probe(timeout=1.0)
        self.assertEqual(sorted(report['ports'].keys()), [6666, 6667])
        self.assertEqual(report['devices'], [])


class FakeTuyaPlug(object):
    """A Tuya plug that speaks the real wire protocol over loopback TCP.

    It is strict on purpose. The 3.3 rule that a status query carries no
    version header while a control does is the single easiest thing to get
    wrong in this protocol, and a lenient fake would accept both and prove
    nothing -- so a request framed the wrong way is answered with the same
    error a real device gives.
    """

    def __init__(self, key=b'0123456789abcdef', version='3.3', dps=None,
                 header_on_status_reply=True, envelope_replies=False):
        import tuya_lan

        self.tuya_lan = tuya_lan
        self.key = key
        self.version = version
        self.dps = dict(dps or {'1': False})
        self.header_on_status_reply = header_on_status_reply
        self.envelope_replies = envelope_replies
        self.requests = []
        self.connections = 0
        self.refuse = None
        self.negotiations = 0
        self.session_key = None
        self.crashed = None
        self.remote_nonce = None

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(5)
        self.server.settimeout(0.3)
        self.port = self.server.getsockname()[1]

        self.running = True
        self.thread = threading.Thread(target=self._serve)
        self.thread.daemon = True
        self.thread.start()

    # -- lifecycle ---------------------------------------------------------

    def close(self):
        self.running = False
        self.thread.join(2.0)
        self.server.close()

    def _serve(self):
        while self.running:
            try:
                conn, _addr = self.server.accept()
            except (socket.timeout, socket.error):
                continue
            self.connections += 1
            try:
                conn.settimeout(1.0)
                self.session_key = None
                # 3.4 negotiates a key before every command, so a connection
                # is a conversation rather than a single request -- and the
                # client is free to put two of its turns in one segment, so
                # this frames by declared length rather than by read.
                buffer = b''
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    buffer += data
                    while len(buffer) >= 16:
                        length = struct.unpack('>I', buffer[12:16])[0]
                        if len(buffer) < 16 + length:
                            break
                        packet, buffer = (buffer[:16 + length],
                                          buffer[16 + length:])
                        reply = self._handle(packet)
                        if reply:
                            conn.sendall(reply)
            except socket.error:
                pass
            except Exception as exc:  # surfaced by the test, not swallowed
                self.crashed = exc
            finally:
                try:
                    conn.close()
                except socket.error:
                    pass

    # -- protocol ----------------------------------------------------------

    @property
    def is_v34(self):
        return self.version.startswith('3.4')

    @property
    def tail(self):
        return 36 if self.is_v34 else 8

    def _cipher(self, key=None):
        from aes import AESECB

        return AESECB(key or self.key)

    def _handle(self, data):
        prefix, sequence, command, length = struct.unpack('>4I', data[0:16])
        assert prefix == self.tuya_lan.PREFIX
        payload = data[16:16 + length - self.tail]
        self.requests.append((command, payload))

        if self.is_v34:
            handshake = self._handshake(sequence, command, payload)
            if handshake is not None:
                return handshake

        if self.refuse is not None:
            return self._packet(sequence, command, b'device busy',
                                retcode=self.refuse)

        try:
            body = self._decode_request(command, payload)
        except ValueError:
            # A real device encrypts its error under its own key, so a client
            # whose key is wrong cannot read the reason either. Returning
            # plain text here would hand the client a legibility it does not
            # have on the wire.
            return self._packet(sequence, command,
                                self._encode_reply({'err': 'decrypt failed'}),
                                retcode=1)

        if command in (self.tuya_lan.CMD_STATUS, self.tuya_lan.CMD_STATUS_NEW):
            return self._packet(sequence, command,
                                self._encode_reply(self._reading()))

        dps = body.get('dps')
        if dps is None:
            dps = (body.get('data') or {}).get('dps') or {}
        for key, value in dps.items():
            self.dps[str(key)] = value
        return self._packet(sequence, command, b'')

    def _reading(self):
        body = {'dps': dict(self.dps)}
        if self.envelope_replies:
            return {'protocol': 4, 't': 1700000000, 'data': body}
        return body

    # -- 3.4 key negotiation ----------------------------------------------

    def _handshake(self, sequence, command, payload):
        """The three steps 3.4 requires before it will hear a command."""
        if command == self.tuya_lan.CMD_SESS_KEY_NEG_START:
            self.negotiations += 1
            self.local_nonce = self._cipher().decrypt(payload, unpad=False)[:16]
            self.remote_nonce = bytes(bytearray(range(0x40, 0x50)))
            proof = hmac.new(self.key, self.local_nonce,
                             hashlib.sha256).digest()
            # Deliberately without a return code: real 3.4 devices omit it
            # here and nothing in the header says so, which is exactly what
            # the client has to cope with.
            return self._packet(
                sequence, self.tuya_lan.CMD_SESS_KEY_NEG_RESP,
                self._cipher().encrypt(self.remote_nonce + proof),
                retcode=None)

        if command == self.tuya_lan.CMD_SESS_KEY_NEG_FINISH:
            given = self._cipher().decrypt(payload, unpad=False)[:32]
            expected = hmac.new(self.key, self.remote_nonce,
                                hashlib.sha256).digest()
            assert given == expected, 'client failed to prove it holds the key'
            mixed = bytearray(self.local_nonce)
            for index, byte in enumerate(bytearray(self.remote_nonce)):
                mixed[index] ^= byte
            self.session_key = self._cipher().encrypt(bytes(mixed), pad=False)
            return b''
        return None

    # -- payloads ----------------------------------------------------------

    def _decode_request(self, command, payload):
        if self.is_v34:
            assert self.session_key, 'command sent before the key was agreed'
            plain = self._cipher(self.session_key).decrypt(payload)
            headerless = command in self.tuya_lan.V34_HEADERLESS
            if headerless and plain[:3] == b'3.4':
                raise ValueError('a 3.4 status query carries no header')
            if not headerless:
                if plain[:3] != b'3.4':
                    raise ValueError('a 3.4 control needs the header')
                plain = plain[15:]
            return json.loads(plain.decode('utf-8'))

        if self.version.startswith('3.1'):
            if command == self.tuya_lan.CMD_STATUS:
                if payload[:3] == b'3.1':
                    raise ValueError('3.1 status queries are sent in clear')
                return json.loads(payload.decode('utf-8'))
            if payload[:3] != b'3.1':
                raise ValueError('3.1 controls need the version header')
            signature, encoded = payload[3:19], payload[19:]
            expected = hashlib.md5(
                b'data=' + encoded + b'||lpv=3.1||' + self.key
            ).hexdigest()[8:24].encode('utf-8')
            if signature != expected:
                raise ValueError('bad signature')
            return json.loads(
                self._cipher().decrypt(base64.b64decode(encoded))
                .decode('utf-8'))

        header = self.version[:3].encode('utf-8')
        if command == self.tuya_lan.CMD_STATUS:
            if payload[:3] == header:
                raise ValueError('status queries carry no version header')
        else:
            if payload[:3] != header:
                raise ValueError('controls need the version header')
            payload = payload[15:]
        return json.loads(self._cipher().decrypt(payload).decode('utf-8'))

    def _encode_reply(self, body):
        raw = json.dumps(body).encode('utf-8')
        if self.is_v34:
            return self._cipher(self.session_key).encrypt(
                b'3.4' + (b'\x00' * 12) + raw)
        if self.version.startswith('3.1'):
            encoded = base64.b64encode(self._cipher().encrypt(raw))
            signature = hashlib.md5(
                b'data=' + encoded + b'||lpv=3.1||' + self.key
            ).hexdigest()[8:24].encode('utf-8')
            return b'3.1' + signature + encoded
        encrypted = self._cipher().encrypt(raw)
        if not self.header_on_status_reply:
            return encrypted
        return self.version[:3].encode('utf-8') + (b'\x00' * 12) + encrypted

    def _packet(self, sequence, command, payload, retcode=0):
        """Frame a reply. retcode=None omits it, as 3.4 does mid-handshake."""
        head = bytearray()
        if retcode is not None:
            head.extend(struct.pack('>I', retcode))
        head.extend(payload)

        body = bytearray(struct.pack('>4I', self.tuya_lan.PREFIX, sequence,
                                     command, len(head) + self.tail))
        body.extend(head)
        if self.is_v34:
            key = self.session_key or self.key
            body.extend(hmac.new(key, bytes(body), hashlib.sha256).digest())
        else:
            body.extend(struct.pack(
                '>I', zlib.crc32(bytes(body)) & 0xFFFFFFFF))
        body.extend(struct.pack('>I', self.tuya_lan.SUFFIX))
        return bytes(body)


class TestTuyaDriver(unittest.TestCase):
    """A plug is discoverable long before it is controllable."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'tuya_driver',
                     'tuya_lan', 'hub'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def driver(self, keys=None):
        from tuya_driver import TuyaDriver

        self.saved = []
        return TuyaDriver(keys=keys if keys is not None else {},
                          save_keys=lambda: self.saved.append(True))

    def device(self):
        return Device('wp9abc', name='Lamp Plug', driver='tuya',
                      ip='10.0.0.55', lan=True, native_id='wp9abc')

    def test_a_plug_claims_power_and_state_only(self):
        from devices import CAP_COLOR, CAP_COMMANDS, CAP_POWER, CAP_STATE

        caps = self.driver().capabilities(self.device())
        self.assertEqual(caps, set([CAP_POWER, CAP_STATE]))
        self.assertNotIn(CAP_COLOR, caps)
        self.assertNotIn(CAP_COMMANDS, caps)

    def test_a_device_without_a_key_says_so_rather_than_looking_broken(self):
        driver, device = self.driver(), self.device()
        self.assertTrue(driver.needs_key(device))

        with self.assertRaises(ControlError) as caught:
            driver.turn(device, True)
        self.assertIn(device.name, str(caught.exception))

    def test_a_key_must_be_sixteen_characters(self):
        driver, device = self.driver(), self.device()

        self.assertFalse(driver.set_local_key(device, 'too short'))
        self.assertTrue(driver.needs_key(device))

        self.assertTrue(driver.set_local_key(device, 'a' * 16))
        self.assertFalse(driver.needs_key(device))
        self.assertTrue(self.saved, 'key was not persisted')

    def test_the_tuya_id_keeps_its_case(self):
        """Upper-casing a Tuya id would produce one the device disowns."""
        device = self.device()
        self.assertEqual(device.device_id, 'WP9ABC')   # for matching
        self.assertEqual(device.native_id, 'wp9abc')   # for the wire

        restored = Device.from_dict(json.loads(json.dumps(device.to_dict())))
        self.assertEqual(restored.native_id, 'wp9abc')

    def test_keys_are_stored_under_the_tuya_id(self):
        driver, device = self.driver(), self.device()
        driver.set_local_key(device, 'k' * 16)
        self.assertIn('wp9abc', driver.keys)

    def test_a_key_can_be_cleared(self):
        driver, device = self.driver(keys={'wp9abc': 'a' * 16}), self.device()
        self.assertFalse(driver.needs_key(device))
        self.assertTrue(driver.set_local_key(device, ''))
        self.assertTrue(driver.needs_key(device))

    def test_discovery_warns_about_unkeyed_devices(self):
        import tuya_lan as t
        from tuya_driver import TuyaDriver

        heard = [{'device_id': 'wp9abc', 'ip': '10.0.0.55', 'version': '3.3'}]
        saved_discover = t.discover
        t.discover = lambda timeout=6.0, log_func=None: heard
        try:
            found, warnings = TuyaDriver(keys={}).discover()
        finally:
            t.discover = saved_discover

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].driver, 'tuya')
        self.assertEqual(found[0].device_id, 'WP9ABC')
        self.assertTrue(any('local key' in w for w in warnings))

    def test_a_keyed_device_produces_no_warning(self):
        import tuya_lan as t
        from tuya_driver import TuyaDriver

        heard = [{'device_id': 'wp9abc', 'ip': '10.0.0.55', 'version': '3.3'}]
        saved_discover = t.discover
        t.discover = lambda timeout=6.0, log_func=None: heard
        try:
            _found, warnings = TuyaDriver(
                keys={'wp9abc': 'k' * 16}).discover()
        finally:
            t.discover = saved_discover
        self.assertEqual(warnings, [])

    def test_a_blocked_listen_port_becomes_a_warning(self):
        import tuya_lan as t
        from tuya_driver import TuyaDriver

        saved_discover = t.discover

        def blocked(timeout=6.0, log_func=None):
            raise t.TuyaError('Could not listen on UDP 6666 or 6667.')

        t.discover = blocked
        try:
            found, warnings = TuyaDriver().discover()
        finally:
            t.discover = saved_discover

        self.assertEqual(found, [])
        self.assertTrue(any('6666' in w for w in warnings))

    def test_discovery_listens_longer_than_a_request_response_sweep(self):
        """Tuya devices announce on their own schedule; 3 seconds is not enough."""
        import tuya_lan as t
        from tuya_driver import TuyaDriver

        seen = []
        saved_discover = t.discover
        t.discover = lambda timeout=6.0, log_func=None: seen.append(timeout) or []
        try:
            TuyaDriver().discover(timeout=3.0)
        finally:
            t.discover = saved_discover
        self.assertGreaterEqual(seen[0], 6.0)

    def test_the_session_refuses_without_a_key(self):
        import tuya_lan as t

        session = t.Session('10.0.0.55', 'wp9abc', '', version='3.3')
        self.assertFalse(session.keyed)
        self.assertRaises(t.TuyaKeyMissing, session._cipher)

    def test_packet_framing_carries_the_prefix_and_crc(self):
        import struct as _struct
        import tuya_lan as t
        import zlib as _zlib

        session = t.Session('10.0.0.55', 'wp9abc', 'k' * 16)
        packet = bytearray(session.build_packet(t.CMD_STATUS, b'{}'))

        self.assertEqual(_struct.unpack('>I', bytes(packet[0:4]))[0], t.PREFIX)
        self.assertEqual(_struct.unpack('>I', bytes(packet[-4:]))[0], t.SUFFIX)
        body = bytes(packet[:-8])
        self.assertEqual(_struct.unpack('>I', bytes(packet[-8:-4]))[0],
                         _zlib.crc32(body) & 0xFFFFFFFF)

    def test_a_device_error_reply_is_raised(self):
        import struct as _struct
        import tuya_lan as t

        session = t.Session('10.0.0.55', 'wp9abc', 'k' * 16)
        reply = bytearray(_struct.pack('>4I', t.PREFIX, 1, 10, 0))
        reply.extend(_struct.pack('>I', 1))       # non-zero return code
        reply.extend(b'json obj data unvalid')
        reply.extend(bytearray(8))

        with self.assertRaises(t.TuyaError) as caught:
            session.parse_packet(reply)
        self.assertIn('error 1', str(caught.exception))


class TestTuyaControl(unittest.TestCase):
    """Switching a plug, against a device that speaks the real protocol."""

    KEY = '0123456789abcdef'
    WP9_DPS = {
        '1': False, '2': True, '3': False,   # the three mains outlets
        '7': True,                           # the USB bank
        '9': 0, '10': 0, '11': 0, '15': 0,   # countdown timers
        '38': 'last', '40': False,           # relay memory, child lock
    }

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'tuya_driver',
                     'tuya_lan', 'hub'):
            if name in sys.modules:
                del sys.modules[name]
        import tuya_lan

        self.tuya_lan = tuya_lan
        self.real_port = tuya_lan.CONTROL_PORT
        self.plug = None

    def tearDown(self):
        self.tuya_lan.CONTROL_PORT = self.real_port
        if self.plug is not None:
            crashed = self.plug.crashed
            self.plug.close()
            # A fake that died in its thread answers nothing, which surfaces
            # as a timeout and reads like a client bug. Fail on the real
            # cause instead.
            self.assertIsNone(crashed, 'the fake plug crashed: %r' % crashed)
        clean_profile()

    def start(self, **kwargs):
        kwargs.setdefault('key', self.KEY.encode('utf-8'))
        self.plug = FakeTuyaPlug(**kwargs)
        self.tuya_lan.CONTROL_PORT = self.plug.port
        return self.plug

    def driver(self, keys=None):
        from tuya_driver import TuyaDriver

        return TuyaDriver(keys=keys if keys is not None else
                          {'wp9abc': self.KEY}, timeout=3.0)

    def device(self, dp=None, version='3.3'):
        data = {'version': version}
        if dp:
            data['dp'] = dp
        return Device('wp9abc%s' % ('#%s' % dp if dp else ''),
                      name='Office Plug', driver='tuya', ip='127.0.0.1',
                      lan=True, native_id='wp9abc', driver_data=data)

    # -- the wire ----------------------------------------------------------

    def test_a_33_plug_switches_on_and_off(self):
        plug = self.start(dps={'1': False})
        driver = self.driver()

        driver.turn(self.device(), True)
        self.assertTrue(plug.dps['1'])

        driver.turn(self.device(), False)
        self.assertFalse(plug.dps['1'])

    def test_a_31_plug_switches_too(self):
        """3.1 signs its payloads and sends status in clear; 3.3 does neither."""
        plug = self.start(version='3.1', dps={'1': False})
        driver = self.driver()

        driver.turn(self.device(version='3.1'), True)
        self.assertTrue(plug.dps['1'])
        self.assertEqual(driver.get_state(self.device(version='3.1')),
                         {'power': 'on', 'dps': {'1': True}})

    def test_a_status_query_carries_no_version_header_on_33(self):
        """The rule that catches everyone, asserted rather than assumed.

        A 3.3 control needs the version header and a 3.3 status query must not
        have it. Sending the header on a query comes back as a device error,
        which reads exactly like a wrong key and is not one.
        """
        self.start(dps={'1': True})
        driver = self.driver()

        driver.turn(self.device(), False)
        driver.get_state(self.device())

        by_command = dict((command, payload)
                          for command, payload in self.plug.requests)
        self.assertEqual(by_command[self.tuya_lan.CMD_CONTROL][:3], b'3.3')
        self.assertNotEqual(by_command[self.tuya_lan.CMD_STATUS][:3], b'3.3')

    def test_a_reply_without_the_version_header_is_read_too(self):
        """Devices differ on whether a status reply is headered. Both work."""
        self.start(dps={'1': True}, header_on_status_reply=False)

        self.assertEqual(self.driver().get_state(self.device()),
                         {'power': 'on', 'dps': {'1': True}})

    def test_a_wrong_key_is_named_as_the_likely_cause(self):
        """An unreadable reply on this protocol means one thing."""
        self.start(dps={'1': True})
        driver = self.driver(keys={'wp9abc': 'ffffffffffffffff'})

        self.assertIsNone(driver.get_state(self.device()))
        with self.assertRaises(ControlError) as caught:
            driver.turn(self.device(), True)
        self.assertIn('local key', str(caught.exception).lower())

    def test_a_device_error_reaches_the_user_as_words(self):
        plug = self.start()
        plug.refuse = 1
        driver = self.driver()

        with self.assertRaises(ControlError) as caught:
            driver.turn(self.device(), True)
        message = str(caught.exception)
        self.assertIn('Office Plug', message)
        self.assertIn('device busy', message)

    def test_a_reply_split_across_reads_is_reassembled(self):
        """TCP may cut anywhere; framing is by declared length, not by read."""
        packet = self.tuya_lan.Session(
            '127.0.0.1', 'wp9abc', self.KEY).build_packet(0x0A, b'payload')
        stream = b'\x00\x00' + packet + packet[:9]

        packets, leftover = self.tuya_lan.Session.split_packets(stream)
        self.assertEqual(len(packets), 1)
        self.assertEqual(packets[0], packet)
        self.assertEqual(leftover, packet[:9])

    def test_an_unsupported_version_says_which_and_why(self):
        self.start()
        driver = self.driver()

        with self.assertRaises(ControlError) as caught:
            driver.turn(self.device(version='3.5'), True)
        message = str(caught.exception)
        self.assertIn('3.5', message)
        self.assertIn('GCM', message)

    # -- protocol 3.4 ------------------------------------------------------

    def test_a_34_plug_switches_after_negotiating_a_session_key(self):
        plug = self.start(version='3.4', dps={'1': False})
        driver = self.driver()

        driver.turn(self.device(version='3.4'), True)

        self.assertTrue(plug.dps['1'])
        self.assertEqual(plug.negotiations, 1)

    def test_a_34_status_read_comes_back(self):
        self.start(version='3.4', dps=self.WP9_DPS)

        state = self.driver().get_state(self.device(dp='2', version='3.4'))

        self.assertEqual(state['power'], 'on')

    def test_a_34_reading_wrapped_in_an_envelope_is_unwrapped(self):
        """3.4 sends {"data":{"dps":..}} where 3.3 sends {"dps":..} flat."""
        self.start(version='3.4', dps=self.WP9_DPS, envelope_replies=True)

        state = self.driver().get_state(self.device(dp='3', version='3.4'))

        self.assertEqual(state['power'], 'off')
        self.assertEqual(state['dps']['1'], False)

    def test_a_34_connection_negotiates_every_time(self):
        """The session key belongs to the connection, not to the device."""
        plug = self.start(version='3.4', dps={'1': False})
        driver = self.driver()

        driver.turn(self.device(version='3.4'), True)
        driver.turn(self.device(version='3.4'), False)

        self.assertEqual(plug.negotiations, 2)
        self.assertFalse(plug.dps['1'])

    def test_a_34_session_key_is_not_kept_after_the_connection(self):
        self.start(version='3.4', dps={'1': False})
        session = self.tuya_lan.Session('127.0.0.1', 'wp9abc', self.KEY,
                                        version='3.4')

        session.set_switch(True)

        self.assertIsNone(session.session_key)

    def test_a_34_wrong_key_fails_at_the_handshake_not_five_steps_later(self):
        """The HMAC is a definite answer where an unreadable reply is a guess."""
        self.start(version='3.4', dps={'1': False})
        driver = self.driver(keys={'wp9abc': 'ffffffffffffffff'})

        with self.assertRaises(ControlError) as caught:
            driver.turn(self.device(version='3.4'), True)
        self.assertIn('local key', str(caught.exception))

    def test_a_34_control_carries_the_header_inside_the_encryption(self):
        """The counter-intuitive one: 3.3 puts it outside, 3.4 puts it inside.

        Asserted from the client's own output rather than trusted, because
        getting this backwards produces a packet the device rejects with an
        error that reads like a wrong key.
        """
        self.start(version='3.4', dps={'1': False})
        session = self.tuya_lan.Session('127.0.0.1', 'wp9abc', self.KEY,
                                        version='3.4')
        session.session_key = self.KEY.encode('utf-8')

        control = session.build_command_payload(
            self.tuya_lan.CMD_CONTROL,
            session._body(self.tuya_lan.CMD_CONTROL, {'1': True}))
        query = session.build_command_payload(
            self.tuya_lan.CMD_STATUS, session._body(self.tuya_lan.CMD_STATUS))

        self.assertNotEqual(control[:3], b'3.4')
        from aes import AESECB
        cipher = AESECB(self.KEY.encode('utf-8'))
        self.assertEqual(cipher.decrypt(control)[:3], b'3.4')
        self.assertNotEqual(cipher.decrypt(query)[:3], b'3.4')

    def test_a_34_plug_splits_into_outlets_like_any_other(self):
        """The version changes the wire, not what the device turns out to be."""
        self.start(version='3.4', dps=self.WP9_DPS)

        found = self.driver()._devices_for(
            {'device_id': 'wp9abc', 'ip': '127.0.0.1', 'version': '3.4'})

        self.assertEqual([d.driver_data['dp'] for d in found],
                         ['all', '1', '2', '3', '7'])
        self.assertEqual(found[0].model, 'Tuya 3.4')

    def test_a_34_reply_without_a_return_code_is_read_correctly(self):
        """Nothing in the header says whether one is there; block size does."""
        session = self.tuya_lan.Session('127.0.0.1', 'wp9abc', self.KEY,
                                        version='3.4')
        blocks = b'x' * 64

        with_code = (struct.pack('>4I', self.tuya_lan.PREFIX, 1, 4,
                                 len(blocks) + 4 + 36)
                     + struct.pack('>I', 0) + blocks + b'z' * 36)
        without = (struct.pack('>4I', self.tuya_lan.PREFIX, 1, 4,
                               len(blocks) + 36) + blocks + b'z' * 36)

        self.assertEqual(session._reply_payload(with_code), (0, blocks))
        self.assertEqual(session._reply_payload(without), (0, blocks))

    # -- outlets -----------------------------------------------------------

    def test_a_multi_outlet_plug_is_listed_once_per_outlet(self):
        """A WP9 is four switches in a box, not one."""
        self.start(dps=self.WP9_DPS)
        driver = self.driver()

        found = driver._devices_for(
            {'device_id': 'wp9abc', 'ip': '127.0.0.1', 'version': '3.3'})

        self.assertEqual([d.driver_data['dp'] for d in found],
                         ['all', '1', '2', '3', '7'])
        self.assertEqual([d.name for d in found],
                         ['Tuya 9ABC All outlets', 'Tuya 9ABC Outlet 1',
                          'Tuya 9ABC Outlet 2', 'Tuya 9ABC Outlet 3',
                          'Tuya 9ABC USB'])
        # Countdowns, relay memory and the child lock are not outlets.
        for device in found:
            self.assertNotIn(device.driver_data['dp'],
                             ('9', '10', '11', '15', '38', '40'))

    def test_every_outlet_keeps_the_plug_id_for_the_wire(self):
        """The suffix is ours; the device would disown it."""
        self.start(dps=self.WP9_DPS)

        found = self.driver()._devices_for(
            {'device_id': 'wp9abc', 'ip': '127.0.0.1', 'version': '3.3'})

        self.assertEqual(set(d.native_id for d in found), set(['wp9abc']))
        self.assertEqual(len(set(d.device_id for d in found)), 5)

    def test_each_outlet_switches_only_itself(self):
        plug = self.start(dps=self.WP9_DPS)
        driver = self.driver()

        driver.turn(self.device(dp='2'), False)

        self.assertFalse(plug.dps['2'])
        self.assertFalse(plug.dps['1'])
        self.assertTrue(plug.dps['7'])

    # -- the whole plug ----------------------------------------------------

    def master(self, members=('1', '2', '3', '7'), version='3.4'):
        return Device('wp9abc#all', name='Office Plug', driver='tuya',
                      ip='127.0.0.1', lan=True, native_id='wp9abc',
                      driver_data={'version': version, 'dp': 'all',
                                   'members': list(members)})

    def test_the_whole_plug_switches_off_in_one_command(self):
        """A WP9 has no master relay; the app's "all off" is every outlet at
        once. One packet also means the outlets go together, not in sequence."""
        plug = self.start(version='3.4', dps=self.WP9_DPS)

        self.driver().turn(self.master(), False)

        self.assertEqual([plug.dps[dp] for dp in ('1', '2', '3', '7')],
                         [False, False, False, False])
        controls = [c for c, _p in plug.requests
                    if c == self.tuya_lan.CMD_CONTROL_NEW]
        self.assertEqual(len(controls), 1)

    def test_the_whole_plug_switches_on_too(self):
        plug = self.start(version='3.4', dps=self.WP9_DPS)

        self.driver().turn(self.master(), True)

        self.assertEqual([plug.dps[dp] for dp in ('1', '2', '3', '7')],
                         [True, True, True, True])

    def test_the_whole_plug_reads_as_on_when_anything_is_drawing_power(self):
        """Requiring all of them would call a plug with one outlet live off."""
        self.start(version='3.4',
                   dps={'1': False, '2': True, '3': False, '7': False})

        state = self.driver().get_state(self.master())

        self.assertEqual(state['power'], 'on')

    def test_the_whole_plug_reads_as_off_only_when_everything_is(self):
        self.start(version='3.4',
                   dps={'1': False, '2': False, '3': False, '7': False})

        self.assertEqual(self.driver().get_state(self.master())['power'],
                         'off')

    def test_a_master_saved_before_members_were_recorded_still_works(self):
        """It falls back to every socket datapoint Tuya defines.

        A plug ignores a datapoint it does not have, so the fallback is
        harmless where switching nothing at all would not be.
        """
        plug = self.start(version='3.4', dps=self.WP9_DPS)
        old = Device('wp9abc#all', name='Office Plug', driver='tuya',
                     ip='127.0.0.1', lan=True, native_id='wp9abc',
                     driver_data={'version': '3.4', 'dp': 'all'})

        self.driver().turn(old, False)

        self.assertEqual([plug.dps[dp] for dp in ('1', '2', '3', '7')],
                         [False, False, False, False])

    def test_a_group_command_uses_the_master_instead_of_every_outlet(self):
        """Same instruction to all of them, so one packet beats four."""
        from tuya_driver import TuyaDriver

        outlets = [self.device(dp=dp) for dp in ('1', '2', '3', '7')]
        collapsed = TuyaDriver.collapse([self.master()] + outlets)

        self.assertEqual([d.device_id for d in collapsed], ['WP9ABC#ALL'])

    def test_collapsing_leaves_outlets_of_a_plug_with_no_master_alone(self):
        from tuya_driver import TuyaDriver

        outlets = [self.device(dp=dp) for dp in ('1', '2')]
        self.assertEqual(TuyaDriver.collapse(outlets), outlets)

    def test_collapsing_never_touches_another_driver(self):
        from tuya_driver import TuyaDriver

        bulb = Device('AA:BB', name='Lamp', driver='govee')
        collapsed = TuyaDriver.collapse([self.master(), bulb])

        self.assertIn(bulb, collapsed)

    def test_collapsing_only_folds_the_plug_that_has_the_master(self):
        from tuya_driver import TuyaDriver

        other = Device('otherplug#1', name='Hall', driver='tuya',
                       native_id='otherplug', driver_data={'dp': '1'})
        collapsed = TuyaDriver.collapse(
            [self.master(), self.device(dp='2'), other])

        self.assertEqual([d.device_id for d in collapsed],
                         ['WP9ABC#ALL', 'OTHERPLUG#1'])

    def test_one_key_covers_every_outlet(self):
        """The key belongs to the plug, so it is not typed in four times."""
        self.start(dps=self.WP9_DPS)
        driver = self.driver(keys={})
        outlets = [self.device(dp=dp) for dp in ('1', '2', '3')]

        self.assertTrue(driver.set_local_key(outlets[0], self.KEY))
        for outlet in outlets:
            self.assertFalse(driver.needs_key(outlet))

    def test_outlets_of_one_plug_share_a_single_round_trip(self):
        """Three outlets, one conversation -- and one consistent reading."""
        plug = self.start(dps=self.WP9_DPS)
        driver = self.driver()
        outlets = [self.device(dp=dp) for dp in ('1', '2', '3')]

        states = driver.get_states(outlets)

        self.assertEqual(plug.connections, 1)
        self.assertEqual(states['WP9ABC#1'],
                         {'power': 'off', 'dps': plug.dps})
        self.assertEqual(states['WP9ABC#2']['power'], 'on')
        self.assertEqual(states['WP9ABC#3']['power'], 'off')

    def test_a_single_outlet_plug_stays_one_device(self):
        """A one-outlet plug should not be called "Outlet 1"."""
        self.start(dps={'1': False, '9': 0})

        found = self.driver()._devices_for(
            {'device_id': 'wp9abc', 'ip': '127.0.0.1', 'version': '3.3'})

        self.assertEqual(len(found), 1)
        self.assertNotIn('dp', found[0].driver_data)
        self.assertEqual(found[0].device_id, 'WP9ABC')

    def test_an_unkeyed_plug_is_listed_whole_rather_than_guessed_at(self):
        """Without a key it cannot be asked, and guessing would invent outlets."""
        self.start(dps=self.WP9_DPS)

        found = self.driver(keys={})._devices_for(
            {'device_id': 'wp9abc', 'ip': '127.0.0.1', 'version': '3.3'})

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].device_id, 'WP9ABC')

    def test_a_plug_that_will_not_answer_is_still_listed(self):
        plug = self.start(dps=self.WP9_DPS)
        plug.refuse = 1

        found = self.driver()._devices_for(
            {'device_id': 'wp9abc', 'ip': '127.0.0.1', 'version': '3.3'})

        self.assertEqual(len(found), 1)

    def test_an_entry_from_before_outlets_still_switches(self):
        """A devices.json written by v2.2 has no datapoint recorded."""
        plug = self.start(dps={'1': False})
        old = Device('wp9abc', name='Old Entry', driver='tuya',
                     ip='127.0.0.1', lan=True, native_id='wp9abc')

        self.driver().turn(old, True)

        self.assertTrue(plug.dps['1'])

    # -- power-cut memory --------------------------------------------------

    def test_the_power_cut_setting_is_read_from_the_plug(self):
        """Datapoint 38: what the relay does when mains power returns."""
        self.start(dps=dict(self.WP9_DPS, **{'38': 'last'}))

        value, options = self.driver().power_memory(self.device())

        self.assertEqual(value, 'last')
        self.assertEqual([v for _label, v in options],
                         ['power_off', 'power_on', 'last'])

    def test_the_power_cut_setting_can_be_changed(self):
        plug = self.start(dps=dict(self.WP9_DPS, **{'38': 'last'}))

        self.driver().set_power_memory(self.device(), 'power_off')

        self.assertEqual(plug.dps['38'], 'power_off')

    def test_a_plug_without_the_datapoint_says_so_rather_than_guessing(self):
        """Not every Tuya plug has a relay memory, and none is not "off"."""
        self.start(dps={'1': True, '2': False})

        value, options = self.driver().power_memory(self.device())

        self.assertIsNone(value)
        self.assertEqual(options, [])

    def test_an_unreadable_plug_does_not_invent_a_setting(self):
        plug = self.start(dps=self.WP9_DPS)
        plug.refuse = 1

        self.assertEqual(self.driver().power_memory(self.device()), (None, []))

    def test_a_value_the_plug_would_not_understand_is_refused_here(self):
        self.start(dps=self.WP9_DPS)

        with self.assertRaises(ControlError):
            self.driver().set_power_memory(self.device(), 'sideways')

    def test_the_setting_belongs_to_the_plug_not_to_one_outlet(self):
        """One relay memory in the box, however many sockets it has."""
        plug = self.start(dps=dict(self.WP9_DPS, **{'38': 'power_on'}))
        driver = self.driver()

        driver.set_power_memory(self.device(dp='2'), 'power_off')

        self.assertEqual(driver.power_memory(self.device(dp='3'))[0],
                         'power_off')
        self.assertEqual(plug.dps['38'], 'power_off')

    # -- test connection ---------------------------------------------------

    def test_test_connection_reports_what_it_found(self):
        self.start(dps=self.WP9_DPS)

        ok, message = self.driver().test_connection(self.device(dp='2'))

        self.assertTrue(ok)
        self.assertIn('on', message)

    def test_test_connection_explains_a_refusal(self):
        plug = self.start()
        plug.refuse = 1

        ok, message = self.driver().test_connection(self.device())

        self.assertFalse(ok)
        self.assertIn('device busy', message)


class FakeKasaPlug(object):
    """An HS103 that speaks the real Kasa protocol over loopback TCP.

    Strict about the framing difference on purpose: a TCP request without the
    4-byte length prefix is the classic way to get this wrong, and a real
    device answers it with silence rather than an error.
    """

    def __init__(self, alias='Office Lamp', model='HS103(US)',
                 relay_state=0, children=None, device_id='8006ABCD1234'):
        import kasa_lan

        self.kasa_lan = kasa_lan
        self.device_id = device_id
        self.info = {
            'sw_ver': '1.0.6 Build 210524 Rel.162228',
            'hw_ver': '2.0',
            'model': model,
            'deviceId': device_id,
            'alias': alias,
            'mac': 'AA:BB:CC:DD:EE:FF',
            'relay_state': relay_state,
        }
        if children is not None:
            self.info['children'] = children
            del self.info['relay_state']
        self.requests = []
        self.connections = 0
        self.err_code = 0
        self.crashed = None

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(5)
        self.server.settimeout(0.3)
        self.port = self.server.getsockname()[1]

        self.running = True
        self.thread = threading.Thread(target=self._serve)
        self.thread.daemon = True
        self.thread.start()

    def close(self):
        self.running = False
        self.thread.join(2.0)
        self.server.close()

    def _serve(self):
        while self.running:
            try:
                conn, _addr = self.server.accept()
            except (socket.timeout, socket.error):
                continue
            self.connections += 1
            try:
                conn.settimeout(1.0)
                data = conn.recv(8192)
                if data:
                    reply = self._handle(data)
                    if reply:
                        conn.sendall(reply)
            except socket.error:
                pass
            except Exception as exc:  # surfaced by the test, not swallowed
                self.crashed = exc
            finally:
                try:
                    conn.close()
                except socket.error:
                    pass

    def _handle(self, data):
        assert len(data) >= 4, 'TCP requests carry a 4-byte length prefix'
        length = struct.unpack('>I', data[:4])[0]
        assert length == len(data) - 4, 'declared length did not match'

        body = json.loads(self.kasa_lan.decrypt(data[4:]).decode('utf-8'))
        self.requests.append(body)

        system = body.get('system') or {}
        if 'get_sysinfo' in system:
            return self._reply({'system': {'get_sysinfo': dict(self.info)}})

        relay = system.get('set_relay_state')
        if relay is not None:
            if self.err_code:
                return self._reply({'system': {'set_relay_state': {
                    'err_code': self.err_code, 'err_msg': 'device busy'}}})
            self._apply(body, relay.get('state'))
            return self._reply(
                {'system': {'set_relay_state': {'err_code': 0}}})
        return self._reply({'system': {}})

    def _apply(self, body, state):
        wanted = list((body.get('context') or {}).get('child_ids') or [])
        if not wanted:
            self.info['relay_state'] = state
            return
        for child in self.info.get('children') or []:
            if self.device_id + child['id'] in wanted \
                    or child['id'] in wanted:
                child['state'] = state

    def _reply(self, body):
        payload = self.kasa_lan.encrypt(json.dumps(body))
        return struct.pack('>I', len(payload)) + payload


class TestNativeStrings(unittest.TestCase):
    """Text meeting binary in one HTTP request, which Python 2 will not do.

    Python 2's httplib joins the request headers into one string and then
    appends the body. One unicode header there makes the join unicode, and
    appending a binary body forces an implicit ascii decode of it -- which
    fails as "'ascii' codec can't decode byte 0xcc in position 0", pointing
    at the payload rather than at the header that caused it.

    A device address read back from devices.json is unicode on Python 2,
    because that is what json.load produces, so this is the ordinary case.
    """

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        for name in ('addon_utils', 'compat', 'kasa_klap', 'govee_cloud'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def test_to_native_returns_the_interpreters_own_str(self):
        import compat

        self.assertIsInstance(compat.to_native(u'10.0.0.31'), str)
        self.assertIsInstance(compat.to_native(b'10.0.0.31'), str)

    def test_a_klap_request_url_is_never_text_beside_a_binary_body(self):
        import kasa_klap

        captured = {}

        class _Recorder(object):
            def __init__(self, url, data=None, headers=None):
                captured['url'] = url
                captured['data'] = data
                captured['headers'] = dict(headers or {})

            def add_header(self, key, value):
                captured['headers'][key] = value

        def refuse(request, timeout=None):
            raise kasa_klap.URLError('stop here')

        kasa_klap.Request = _Recorder
        kasa_klap.urlopen = refuse
        session = kasa_klap.Session(u'10.0.0.31', u'me@example.com',
                                    u'pw', port=80)

        self.assertRaises(kasa_klap.KlapError,
                          session._post, '/app/handshake1', b'\xcc' * 16)
        self.assertIsInstance(captured['url'], str)
        self.assertEqual(captured['data'], b'\xcc' * 16)
        for value in captured['headers'].values():
            self.assertIsInstance(value, str)

    def test_an_address_from_the_device_cache_is_made_native_at_the_door(self):
        import kasa_klap

        session = kasa_klap.Session(u'10.0.0.31', 'user', 'pw')

        self.assertIsInstance(session.ip, str)
        self.assertIsInstance(session.port, int)

    def test_a_govee_cloud_request_is_native_too(self):
        import govee_cloud

        captured = {}

        class _Recorder(object):
            def __init__(self, url, data=None, headers=None):
                captured['url'] = url
                captured['headers'] = dict(headers or {})

        def refuse(request, timeout=None, context=None):
            raise govee_cloud.URLError('stop here')

        govee_cloud.Request = _Recorder
        govee_cloud.urlopen = refuse
        client = govee_cloud.CloudTransport(api_key=u'a-key')

        self.assertRaises(govee_cloud.CloudError,
                          client._request, 'GET', u'https://example.test/x')
        self.assertIsInstance(captured['url'], str)
        for value in captured['headers'].values():
            self.assertIsInstance(value, str)


class TestKasaProtocol(unittest.TestCase):
    """The cipher and the two framings, which is most of this protocol."""

    def setUp(self):
        for name in ('kasa_lan', 'kasa_driver'):
            if name in sys.modules:
                del sys.modules[name]
        import kasa_lan
        self.kasa_lan = kasa_lan

    def test_the_cipher_round_trips(self):
        text = '{"system":{"set_relay_state":{"state":1}}}'
        self.assertEqual(
            self.kasa_lan.decrypt(self.kasa_lan.encrypt(text)).decode('utf-8'),
            text)

    def test_the_cipher_matches_the_published_prefix(self):
        """Every Kasa request starts {"system": and so encrypts identically.

        Pinned against the known bytes rather than only against my own
        decrypt, which would agree with itself however wrong it was.
        """
        encrypted = self.kasa_lan.encrypt('{"system":')
        self.assertEqual(''.join('%02x' % b for b in bytearray(encrypted)),
                         'd0f281f88bff9af7d5ef')

    def test_the_chain_runs_off_the_output_not_the_input(self):
        """Two identical plaintext bytes must not encrypt identically."""
        encrypted = bytearray(self.kasa_lan.encrypt('aa'))
        self.assertNotEqual(encrypted[0], encrypted[1])

    def test_tcp_framing_declares_the_length_and_udp_does_not(self):
        payload = 'ab'
        framed = self.kasa_lan.framed(payload)

        self.assertEqual(struct.unpack('>I', framed[:4])[0], len(payload))
        self.assertEqual(framed[4:], self.kasa_lan.encrypt(payload))

    def test_the_sweep_range_comes_from_devices_not_just_this_machine(self):
        """The fix for a search that found one plug and swept the wrong subnet.

        Working out which interface reaches the plugs is guesswork: a VPN, a
        container bridge or a hostname resolving somewhere unhelpful all give
        a confident wrong answer, and sweeping the wrong /24 looks exactly
        like sweeping the right one and finding nothing.
        """
        subnets = self.kasa_lan.sweep_subnets(
            local=['192.0.2.2'],            # a VPN address, reaches nothing
            found_ips=['10.0.0.25'],        # a plug the broadcast did find
            hints=['10.0.0.71'])            # a device already in the cache

        self.assertIn('10.0.0', subnets)

    def test_the_subnet_of_an_already_known_device_is_enough_on_its_own(self):
        """Even when the broadcast finds nothing and this host looks useless."""
        subnets = self.kasa_lan.sweep_subnets(
            local=[''], found_ips=[], hints=['10.0.0.71'])

        self.assertEqual(subnets, ['10.0.0'])

    def test_a_subnet_is_swept_once_however_many_devices_name_it(self):
        subnets = self.kasa_lan.sweep_subnets(
            local=['10.0.0.5'],
            found_ips=['10.0.0.25', '10.0.0.26'],
            hints=['10.0.0.71', '10.0.0.99'])

        self.assertEqual(subnets, ['10.0.0'])

    def test_loopback_and_rubbish_are_not_swept(self):
        subnets = self.kasa_lan.sweep_subnets(
            local=['127.0.0.1'], found_ips=['', 'not-an-address'],
            hints=[None])

        self.assertEqual(subnets, [])

    def test_sysinfo_without_an_id_is_not_a_device(self):
        self.assertIsNone(self.kasa_lan.parse_sysinfo({'alias': 'No id'}))

    def test_sysinfo_is_read_whether_wrapped_or_bare(self):
        bare = {'deviceId': '8006', 'alias': 'Lamp', 'relay_state': 1}
        wrapped = {'system': {'get_sysinfo': bare}}

        self.assertEqual(self.kasa_lan.parse_sysinfo(bare)['alias'], 'Lamp')
        self.assertEqual(self.kasa_lan.parse_sysinfo(wrapped)['alias'], 'Lamp')

    def test_a_child_id_is_made_whole_when_reported_relative(self):
        """Firmware differs on this and the device wants the whole one."""
        parsed = self.kasa_lan.parse_sysinfo({
            'deviceId': '8006ABCD',
            'children': [{'id': '00', 'alias': 'Left'},
                         {'id': '8006ABCD01', 'alias': 'Right'}],
        })

        self.assertEqual([c['id'] for c in parsed['children']],
                         ['8006ABCD00', '8006ABCD01'])


class FakeKasaResponder(object):
    """A Kasa plug answering discovery over UDP, on its own loopback address.

    `deaf_to_broadcast` models the failure that actually happens: an access
    point that drops broadcast traffic, so the plug never hears the search
    even though it is reachable and answers a datagram addressed to it.
    """

    def __init__(self, host, port, alias, deaf_to_broadcast=False):
        import kasa_lan

        self.kasa_lan = kasa_lan
        self.host = host
        self.alias = alias
        self.deaf_to_broadcast = deaf_to_broadcast
        self.asked = 0
        self.crashed = None

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind((host, port))
        self.sock.settimeout(0.2)

        self.running = True
        self.thread = threading.Thread(target=self._serve)
        self.thread.daemon = True
        self.thread.start()

    def close(self):
        self.running = False
        self.thread.join(2.0)
        self.sock.close()

    def _serve(self):
        while self.running:
            try:
                data, sender = self.sock.recvfrom(4096)
            except socket.error:
                continue
            try:
                self.asked += 1
                body = json.loads(
                    self.kasa_lan.decrypt(data).decode('utf-8'))
                if 'get_sysinfo' not in (body.get('system') or {}):
                    continue
                reply = {'system': {'get_sysinfo': {
                    'deviceId': '8006%s' % self.alias.replace(' ', ''),
                    'alias': self.alias, 'model': 'HS103(US)',
                    'sw_ver': '1.0.6', 'relay_state': 0}}}
                self.sock.sendto(self.kasa_lan.encrypt(json.dumps(reply)),
                                 sender)
            except Exception as exc:
                self.crashed = exc


class TestKasaDiscovery(unittest.TestCase):
    """Finding all of them, on a network that does not carry broadcast."""

    HOSTS = ['127.0.0.2', '127.0.0.3', '127.0.0.4']

    def setUp(self):
        for name in ('kasa_lan', 'kasa_driver'):
            if name in sys.modules:
                del sys.modules[name]
        import kasa_lan

        self.kasa_lan = kasa_lan
        self.port = _free_port()
        kasa_lan.PORT = self.port
        self.real_addresses = kasa_lan.local_addresses
        self.real_sweep = kasa_lan.sweep_addresses
        self.real_subnets = kasa_lan.sweep_subnets
        # The search runs on loopback, which the real helpers deliberately
        # refuse to sweep -- so both the subnet choice and the host list are
        # stood in for here.
        kasa_lan.local_addresses = lambda: ['127.0.0.1']
        kasa_lan.sweep_subnets = lambda local, found, hints: ['127.0.0']
        kasa_lan.sweep_addresses = lambda address: list(self.HOSTS)
        self.plugs = []

    def tearDown(self):
        for plug in self.plugs:
            crashed = plug.crashed
            plug.close()
            self.assertIsNone(crashed, 'a fake plug crashed: %r' % crashed)
        self.kasa_lan.local_addresses = self.real_addresses
        self.kasa_lan.sweep_addresses = self.real_sweep
        self.kasa_lan.sweep_subnets = self.real_subnets

    def start(self, *specs):
        for host, alias, deaf in specs:
            self.plugs.append(
                FakeKasaResponder(host, self.port, alias,
                                  deaf_to_broadcast=deaf))
        return self.plugs

    def test_every_plug_is_found_when_broadcast_does_not_reach_them(self):
        """The one that matters: three of four silent on broadcast.

        Loopback carries no broadcast at all, so every plug here is found by
        the sweep -- which is the point. A network that drops broadcast used
        to mean those devices simply never appeared.
        """
        self.start(('127.0.0.2', 'Tree', True), ('127.0.0.3', 'Lamp', True),
                   ('127.0.0.4', 'Fan', True))

        devices, report = self.kasa_lan.search(timeout=4.0)

        self.assertEqual(sorted(d['alias'] for d in devices),
                         ['Fan', 'Lamp', 'Tree'])
        self.assertEqual(report['sweep'], 3)

    def test_a_reply_to_a_send_is_not_thrown_away(self):
        """The bug this replaced: the socket that sent was closed at once.

        A device answers to the port the request came from, so closing that
        socket discarded every reply to it -- and the search found only the
        devices that happened to answer the one send from the socket that
        stayed open.
        """
        self.start(('127.0.0.2', 'Tree', True))

        devices, _report = self.kasa_lan.search(timeout=3.0)

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]['ip'], '127.0.0.2')

    def test_the_same_plug_answering_twice_is_listed_once(self):
        """Broadcast repeats, so a device is asked more than once."""
        self.start(('127.0.0.2', 'Tree', True))

        devices, _report = self.kasa_lan.search(timeout=3.0)

        self.assertGreater(self.plugs[0].asked, 1)
        self.assertEqual(len(devices), 1)

    def test_a_search_with_the_sweep_off_stays_on_broadcast(self):
        self.start(('127.0.0.2', 'Tree', True))

        devices, report = self.kasa_lan.search(timeout=2.0, sweep=False)

        self.assertEqual(devices, [])
        self.assertEqual(report['sweep'], 0)

    def test_a_pass_stops_once_everything_has_gone_quiet(self):
        """A search that found everything should not sit out its window."""
        self.start(('127.0.0.2', 'Tree', True))

        started = time.time()
        self.kasa_lan.search(timeout=20.0)
        elapsed = time.time() - started

        self.assertLess(elapsed, 12.0)

    def test_the_driver_says_when_broadcast_was_the_problem(self):
        """Silently working around it would hide a real network fault."""
        from kasa_driver import KasaDriver

        self.start(('127.0.0.2', 'Tree', True))

        devices, warnings = KasaDriver().discover(timeout=3.0)

        self.assertEqual(len(devices), 1)
        self.assertEqual(len(warnings), 1)
        self.assertIn('dropping broadcast', warnings[0])


class _KlapHandler(BaseHTTPRequestHandler):
    """The device half of the KLAP handshake, written from the spec.

    Deliberately not sharing helpers with the client beyond the AES itself:
    if both sides computed their hashes with the same function, the tests
    would prove the two agree rather than that either is right.
    """

    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def _body(self):
        return self.rfile.read(int(self.headers.get('Content-Length') or 0))

    def _send(self, payload, code=200, cookie=None):
        self.send_response(code)
        self.send_header('Content-Length', str(len(payload)))
        if cookie:
            self.send_header('Set-Cookie', '%s=%s;TIMEOUT=1440'
                             % (kasa_klap.SESSION_COOKIE, cookie))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        plug = self.server.plug
        path = self.path.split('?')[0]
        body = self._body()

        if path == '/app/handshake1':
            if plug.refuse_handshake:
                return self._send(b'', code=403)
            plug.local_seed = body[:16]
            digest = plug.digest()
            proof = hashlib.sha256(
                plug.local_seed + plug.remote_seed + digest).digest() \
                if plug.scheme == 'v2' else \
                hashlib.sha256(plug.local_seed + digest).digest()
            return self._send(plug.remote_seed + proof, cookie='fake-session')

        if path == '/app/handshake2':
            digest = plug.digest()
            expected = hashlib.sha256(
                plug.remote_seed + plug.local_seed + digest).digest() \
                if plug.scheme == 'v2' else \
                hashlib.sha256(plug.remote_seed + digest).digest()
            assert body == expected, 'client failed handshake2'
            plug.cookie_seen = self.headers.get('Cookie') or ''
            plug.derive(digest)
            return self._send(b'')

        if path == '/app/request':
            assert plug.key, 'a request arrived before the handshake'
            plug.cookie_seen = self.headers.get('Cookie') or ''
            # The sequence travels in the URL and is part of the IV. A device
            # answers under the same one it was asked with, so this is read
            # rather than counted independently.
            sequence = int(self.path.split('seq=')[1])
            request = json.loads(plug.decrypt(body, sequence).decode('utf-8'))
            plug.requests.append(request)
            reply = json.dumps(plug.answer(request)).encode('utf-8')
            return self._send(plug.encrypt(reply, sequence))

        return self._send(b'', code=404)


class FakeKlapPlug(object):
    """An HS103 hardware v5, answering KLAP over HTTP."""

    def __init__(self, username='me@example.com', password='hunter2',
                 scheme='v1', relay_state=0):
        import kasa_klap as klap_module

        globals()['kasa_klap'] = klap_module
        self.username = username
        self.password = password
        self.scheme = scheme
        self.relay_state = relay_state
        self.remote_seed = bytes(bytearray(range(0x10, 0x20)))
        self.local_seed = b''
        self.key = b''
        self.iv = b''
        self.signature = b''
        self.requests = []
        self.cookie_seen = ''
        self.refuse_handshake = False

        self.server = HTTPServer(('127.0.0.1', 0), _KlapHandler)
        self.server.plug = self
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2.0)

    def digest(self):
        if self.scheme == 'v2':
            return hashlib.sha256(
                hashlib.sha1(self.username.encode()).digest()
                + hashlib.sha1(self.password.encode()).digest()).digest()
        return hashlib.md5(
            hashlib.md5(self.username.encode()).digest()
            + hashlib.md5(self.password.encode()).digest()).digest()

    def derive(self, digest):
        """The session key, worked out here rather than borrowed from the
        client -- otherwise the tests would only prove the two agree."""
        material = self.local_seed + self.remote_seed + digest
        self.key = hashlib.sha256(b'lsk' + material).digest()[:16]
        self.iv = hashlib.sha256(b'iv' + material).digest()[:12]
        self.signature = hashlib.sha256(b'ldk' + material).digest()[:28]

    def _cipher(self, sequence):
        from aes import AES

        return AES(self.key, self.iv + struct.pack('>i', sequence))

    def decrypt(self, body, sequence):
        plain = bytearray(self._cipher(sequence).decrypt(body[32:]))
        return bytes(plain[:len(plain) - plain[-1]])

    def encrypt(self, message, sequence):
        padding = 16 - (len(message) % 16)
        padded = message + bytes(bytearray([padding] * padding))
        ciphertext = self._cipher(sequence).encrypt(padded)
        signed = hashlib.sha256(
            self.signature + struct.pack('>i', sequence) + ciphertext).digest()
        return signed + ciphertext

    def answer(self, request):
        system = request.get('system') or {}
        if 'get_sysinfo' in system:
            return {'system': {'get_sysinfo': {
                'deviceId': '8006KLAP', 'alias': 'Christmas Tree',
                'model': 'HS103(US)', 'sw_ver': '1.1.3', 'hw_ver': '5.0',
                'relay_state': self.relay_state}}}
        relay = system.get('set_relay_state')
        if relay is not None:
            self.relay_state = relay.get('state')
            return {'system': {'set_relay_state': {'err_code': 0}}}
        return {'system': {}}


class TestKasaKlap(unittest.TestCase):
    """Later Kasa hardware: same model number, different protocol."""

    USER = 'me@example.com'
    PASSWORD = 'hunter2'

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        for name in ('addon_utils', 'paragon_home', 'kasa_driver', 'kasa_lan',
                     'kasa_klap'):
            if name in sys.modules:
                del sys.modules[name]
        import kasa_klap

        self.klap = kasa_klap
        self.plug = None

    def tearDown(self):
        if self.plug is not None:
            self.plug.close()
        clean_profile()

    def start(self, **kwargs):
        kwargs.setdefault('username', self.USER)
        kwargs.setdefault('password', self.PASSWORD)
        self.plug = FakeKlapPlug(**kwargs)
        return self.plug

    def session(self, username=None, password=None):
        return self.klap.Session(
            '127.0.0.1',
            self.USER if username is None else username,
            self.PASSWORD if password is None else password,
            port=self.plug.port, timeout=5.0)

    # -- handshake ---------------------------------------------------------

    def test_the_handshake_agrees_a_key_and_a_command_goes_through(self):
        plug = self.start(relay_state=0)
        session = self.session()

        session.request({'system': {'set_relay_state': {'state': 1}}})

        self.assertEqual(plug.relay_state, 1)

    def test_the_hash_scheme_is_detected_rather_than_assumed(self):
        """The device never says which it uses; its handshake hash does."""
        self.start(scheme='v2')

        session = self.session()
        session.handshake()

        self.assertEqual(session.scheme, 'v2')

    def test_the_other_scheme_is_detected_too(self):
        self.start(scheme='v1')

        session = self.session()
        session.handshake()

        self.assertEqual(session.scheme, 'v1')

    def test_a_wrong_password_is_named_as_the_cause(self):
        self.start()

        with self.assertRaises(self.klap.KlapAuthError) as caught:
            self.session(password='wrong').handshake()
        message = str(caught.exception)
        self.assertIn('account it is registered to', message)

    def test_a_refused_handshake_is_an_auth_error_not_a_network_one(self):
        """HTTP 403 means the credentials, not the connection."""
        plug = self.start()
        plug.refuse_handshake = True

        with self.assertRaises(self.klap.KlapAuthError):
            self.session().handshake()

    def test_the_session_cookie_is_carried_after_the_handshake(self):
        plug = self.start()

        self.session().request({'system': {'get_sysinfo': {}}})

        self.assertIn(self.klap.SESSION_COOKIE, plug.cookie_seen)

    def test_an_unreachable_device_is_a_plain_reach_error(self):
        self.start()
        session = self.klap.Session('127.0.0.1', self.USER, self.PASSWORD,
                                    port=_free_port(), timeout=2.0)

        with self.assertRaises(self.klap.KlapError) as caught:
            session.handshake()
        self.assertIn('Could not reach', str(caught.exception))

    def test_a_plug_never_bound_to_an_account_is_handled(self):
        """Blank credentials are a real case, and the hash still settles it."""
        self.start(username='', password='')

        session = self.session(username='someone@example.com',
                               password='whatever')
        session.handshake()

        # The distinction that matters when some plugs work and others do
        # not: this one was reached without the account, so the account being
        # wrong would not have shown up here.
        self.assertFalse(session.used_account)

    def test_a_plug_reached_with_the_entered_account_says_so(self):
        """The other half of the same distinction."""
        self.start()

        session = self.session()
        session.handshake()

        self.assertTrue(session.used_account)

    def test_tp_links_own_setup_credentials_are_tried(self):
        """A device in setup state answers to those and nothing else."""
        self.start(username='kasa@tp-link.net', password='kasaSetup')

        session = self.session()
        session.handshake()

        self.assertFalse(session.used_account)

    def test_the_failure_message_has_real_line_breaks(self):
        """A quoted heredoc doubled the escape once; this pins it.

        The dialog is where a user reads this, and a literal backslash-n in
        the middle of a sentence is worse than no formatting at all.
        """
        self.start()

        with self.assertRaises(self.klap.KlapAuthError) as caught:
            self.session(password='wrong').handshake()
        message = str(caught.exception)

        self.assertIn('\n\n', message)
        self.assertNotIn('\\n', message)

    # -- through the driver ------------------------------------------------

    def driver(self, username=None, password=None):
        from kasa_driver import KasaDriver

        return KasaDriver(
            timeout=5.0,
            username=self.USER if username is None else username,
            password=self.PASSWORD if password is None else password)

    def klap_device(self):
        return Device('8006KLAP', name='Christmas Tree', driver='kasa',
                      ip='127.0.0.1', lan=True, native_id='8006KLAP',
                      driver_data={'protocol': 'klap',
                                   'http_port': self.plug.port})

    def test_a_klap_plug_switches_through_the_driver(self):
        plug = self.start(relay_state=0)

        self.driver().turn(self.klap_device(), True)

        self.assertEqual(plug.relay_state, 1)

    def test_a_klap_plug_reports_its_state(self):
        self.start(relay_state=1)

        self.assertEqual(self.driver().get_state(self.klap_device()),
                         {'power': 'on'})

    def test_without_credentials_it_says_what_is_needed_and_where(self):
        """Listed and honest, rather than hidden or looking broken."""
        self.start()

        with self.assertRaises(ControlError) as caught:
            self.driver(username='', password='').turn(self.klap_device(),
                                                       True)
        message = str(caught.exception)
        self.assertIn('TP-Link account', message)
        self.assertIn('Settings', message)
        self.assertIn('nothing is sent to TP-Link', message)

    def test_a_legacy_plug_is_untouched_by_any_of_this(self):
        """Hardware v2 must keep working with no account at all."""
        from kasa_driver import KasaDriver

        legacy = Device('8006OLD', name='Egg Maker', driver='kasa',
                        ip='10.0.0.25', lan=True, native_id='8006OLD',
                        driver_data={'protocol': 'legacy'})
        driver = KasaDriver(username='', password='')

        self.assertFalse(driver.is_klap(legacy))
        self.assertTrue(driver.is_klap(self.klap_device_stub()))

    def klap_device_stub(self):
        return Device('8006KLAP', name='Tree', driver='kasa',
                      driver_data={'protocol': 'klap'})

    def test_a_device_cached_before_klap_existed_is_treated_as_legacy(self):
        """A devices.json written by v2.7 records no protocol at all."""
        from kasa_driver import KasaDriver

        old = Device('8006OLD', name='Egg Maker', driver='kasa',
                     ip='10.0.0.25', lan=True, native_id='8006OLD')

        self.assertFalse(KasaDriver().is_klap(old))

    def test_discovery_warns_once_about_devices_needing_an_account(self):
        from kasa_driver import KasaDriver

        driver = KasaDriver(username='', password='')
        found = driver._devices_for({
            'device_id': '8006KLAP', 'ip': '10.0.0.31', 'model': 'HS103',
            'alias': '', 'children': [], 'protocol': 'klap', 'http_port': 80})

        self.assertEqual(len(found), 1)
        self.assertTrue(driver.is_klap(found[0]))
        # A KLAP announcement carries no alias, so a usable name is invented
        # rather than leaving the row blank.
        self.assertEqual(found[0].name, 'HS103 (KLAP)')

    # -- the cipher --------------------------------------------------------

    def test_each_request_uses_a_new_sequence_number(self):
        """The sequence is part of the IV, so reusing one repeats an IV."""
        session = self.klap.Encryption(b'a' * 16, b'b' * 16, b'c' * 16)

        _first, one = session.encrypt(b'{}')
        _second, two = session.encrypt(b'{}')

        self.assertEqual(two, one + 1)

    def test_the_sequence_stays_inside_a_signed_32_bit_range(self):
        """The device packs it as a signed int; overflowing would break it."""
        session = self.klap.Encryption(b'a' * 16, b'b' * 16, b'c' * 16)
        session.seq = 0x7FFFFFFF

        _body, sequence = session.encrypt(b'{}')

        self.assertEqual(sequence, -0x80000000)

    def test_a_payload_round_trips_through_the_cipher(self):
        session = self.klap.Encryption(b'a' * 16, b'b' * 16, b'c' * 16)
        mirror = self.klap.Encryption(b'a' * 16, b'b' * 16, b'c' * 16)

        body, _sequence = session.encrypt(b'{"system":{}}')
        mirror.seq = session.seq

        self.assertEqual(mirror.decrypt(body), b'{"system":{}}')

    def test_both_ends_derive_the_same_key_from_the_two_seeds(self):
        one = self.klap.Encryption(b'x' * 16, b'y' * 16, b'z' * 16)
        two = self.klap.Encryption(b'x' * 16, b'y' * 16, b'z' * 16)

        self.assertEqual(one.key, two.key)
        self.assertEqual(len(one.key), 16)
        self.assertEqual(len(one.iv), 12)


class TestKasaDiagnostics(unittest.TestCase):
    """A search that finds nothing has to say why, in order of likelihood."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        for name in ('addon_utils', 'diagnostics', 'kasa_lan'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def summary(self, report):
        import diagnostics

        return diagnostics.kasa_summary(report)

    def test_devices_found_are_listed_with_what_to_do_next(self):
        text = self.summary({'devices': [
            {'alias': 'Office Lamp', 'ip': '10.0.0.9', 'model': 'HS103'}]})

        self.assertIn('Office Lamp', text)
        self.assertIn('10.0.0.9', text)
        self.assertIn('Refresh devices', text)

    def test_silence_names_the_causes_and_the_one_with_no_workaround(self):
        text = self.summary({'devices': [], 'listened': 6,
                             'addresses': ['10.0.0.5']})

        self.assertIn('different network', text)
        self.assertIn('firewall', text)
        # Closed firmware is the one outcome the add-on cannot fix, so it says
        # so rather than leaving the user retrying a search forever.
        self.assertIn('firmware has closed the local protocol', text)
        self.assertIn('cannot be worked around', text)
        self.assertIn('10.0.0.5', text)

    def test_a_sweep_that_never_ran_is_not_reported_as_one_that_found_nothing(self):
        """The two need opposite responses, so they must not read alike.

        A sweep that ran and found nothing means the plugs are not there. A
        sweep that never ran means the search could not tell which subnet to
        cover -- which is a fault in the search, not evidence about the plugs.
        """
        never = self.summary({'devices': [], 'broadcast': 0, 'subnets': [],
                              'addresses': ['192.0.2.2']})
        ran = self.summary({'devices': [], 'broadcast': 0,
                            'subnets': ['10.0.0'], 'sweep': 0, 'targets': 253,
                            'addresses': ['10.0.0.5']})

        self.assertIn('did not run', never)
        self.assertNotIn('did not run', ran)
        self.assertIn('10.0.0.0/24', ran)
        self.assertIn('253 hosts', ran)

    def test_a_sweep_that_covered_everything_says_what_is_left(self):
        text = self.summary({
            'devices': [{'alias': 'Egg Maker', 'ip': '10.0.0.25',
                         'model': 'HS103'}],
            'broadcast': 1, 'sweep': 0, 'subnets': ['10.0.0'],
            'targets': 253, 'addresses': ['10.0.0.5']})

        self.assertIn('found nothing further', text)
        self.assertIn('closed the local protocol', text)

    def test_a_send_failure_is_reported_rather_than_read_as_silence(self):
        text = self.summary({'devices': [], 'error': 'no interfaces'})

        self.assertIn('could not be sent', text)
        self.assertIn('no interfaces', text)


class TestKasaDriver(unittest.TestCase):
    """Switching a Kasa plug, against a device speaking the real protocol."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'kasa_driver', 'kasa_lan',
                     'hub'):
            if name in sys.modules:
                del sys.modules[name]
        import kasa_lan

        self.kasa_lan = kasa_lan
        self.real_port = kasa_lan.PORT
        self.plug = None

    def tearDown(self):
        self.kasa_lan.PORT = self.real_port
        if self.plug is not None:
            crashed = self.plug.crashed
            self.plug.close()
            self.assertIsNone(crashed, 'the fake plug crashed: %r' % crashed)
        clean_profile()

    def start(self, **kwargs):
        self.plug = FakeKasaPlug(**kwargs)
        self.kasa_lan.PORT = self.plug.port
        return self.plug

    def driver(self):
        from kasa_driver import KasaDriver

        return KasaDriver(timeout=3.0)

    def device(self, child=None):
        return Device('8006ABCD1234%s' % ('#%s' % child if child else ''),
                      name='Office Lamp', driver='kasa', ip='127.0.0.1',
                      lan=True, native_id='8006ABCD1234',
                      driver_data={'child': child} if child else {})

    # -- switching ---------------------------------------------------------

    def test_a_plug_switches_on_and_off(self):
        plug = self.start(relay_state=0)
        driver = self.driver()

        driver.turn(self.device(), True)
        self.assertEqual(plug.info['relay_state'], 1)

        driver.turn(self.device(), False)
        self.assertEqual(plug.info['relay_state'], 0)

    def test_a_plug_reports_its_state(self):
        self.start(relay_state=1)

        self.assertEqual(self.driver().get_state(self.device()),
                         {'power': 'on'})

    def test_switching_needs_no_key_and_no_setup(self):
        """The whole difference from Tuya: found is the same as usable.

        A Tuya plug is discovered long before it can be switched, and the
        driver has to carry that half-state. A Kasa plug that answers a
        search can be switched with what the search already returned.
        """
        plug = self.start(relay_state=0)
        driver = self.driver()

        found = driver._devices_for({
            'device_id': plug.device_id, 'ip': '127.0.0.1',
            'alias': 'Office Lamp', 'model': 'HS103', 'children': []})

        driver.turn(found[0], True)

        self.assertEqual(plug.info['relay_state'], 1)

    def test_a_device_error_reaches_the_user_as_words(self):
        plug = self.start()
        plug.err_code = -3
        driver = self.driver()

        with self.assertRaises(ControlError) as caught:
            driver.turn(self.device(), True)
        message = str(caught.exception)
        self.assertIn('Office Lamp', message)
        self.assertIn('device busy', message)

    def test_silence_is_explained_as_closed_firmware(self):
        """A device that answers discovery but not a command has moved on."""
        self.kasa_lan.PORT = _free_port()
        driver = self.driver()

        with self.assertRaises(ControlError) as caught:
            driver.turn(self.device(), True)
        self.assertIn('Could not reach', str(caught.exception))

    def test_a_long_sysinfo_split_across_reads_is_reassembled(self):
        """Real sysinfo runs to a couple of kilobytes; one recv may not cover it."""
        self.start(alias='x' * 3000)

        state = self.driver().get_state(self.device())

        self.assertEqual(state, {'power': 'off'})

    # -- naming ------------------------------------------------------------

    def test_a_plug_arrives_with_the_name_it_already_has(self):
        """Kasa devices are named at pairing, so there is nothing to identify."""
        found = self.driver()._devices_for({
            'device_id': '8006ABCD1234', 'ip': '10.0.0.9',
            'alias': 'Christmas Tree', 'model': 'HS103', 'children': []})

        self.assertEqual(found[0].name, 'Christmas Tree')
        self.assertEqual(found[0].model, 'HS103')

    def test_a_single_outlet_plug_stays_one_device(self):
        found = self.driver()._devices_for({
            'device_id': '8006ABCD1234', 'ip': '10.0.0.9',
            'alias': 'Office Lamp', 'model': 'HS103', 'children': []})

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].device_id, '8006ABCD1234')
        self.assertIsNone(self.driver().child_id(found[0]))

    # -- power strips ------------------------------------------------------

    def test_a_strip_is_listed_once_per_outlet_under_its_own_names(self):
        found = self.driver()._devices_for({
            'device_id': '8006AAAA', 'ip': '10.0.0.9', 'model': 'KP303',
            'alias': 'Desk Strip',
            'children': [{'id': '8006AAAA00', 'alias': 'Monitor'},
                         {'id': '8006AAAA01', 'alias': 'Speakers'}],
        })

        self.assertEqual([d.name for d in found], ['Monitor', 'Speakers'])
        self.assertEqual(set(d.native_id for d in found), set(['8006AAAA']))

    def test_each_outlet_of_a_strip_switches_only_itself(self):
        plug = self.start(children=[{'id': '00', 'alias': 'Monitor',
                                     'state': 0},
                                    {'id': '01', 'alias': 'Speakers',
                                     'state': 0}])

        self.driver().turn(self.device(child='8006ABCD123401'), True)

        states = dict((c['id'], c['state']) for c in plug.info['children'])
        self.assertEqual(states, {'00': 0, '01': 1})

    def test_outlets_of_one_strip_share_a_single_round_trip(self):
        plug = self.start(children=[{'id': '00', 'alias': 'Monitor',
                                     'state': 1},
                                    {'id': '01', 'alias': 'Speakers',
                                     'state': 0}])
        outlets = [self.device(child='8006ABCD1234%s' % n)
                   for n in ('00', '01')]

        states = self.driver().get_states(outlets)

        self.assertEqual(plug.connections, 1)
        self.assertEqual(states[outlets[0].device_id], {'power': 'on'})
        self.assertEqual(states[outlets[1].device_id], {'power': 'off'})

    # -- capabilities ------------------------------------------------------

    def test_a_plug_claims_power_and_state_only(self):
        from devices import CAP_COLOR, CAP_COMMANDS, CAP_POWER, CAP_STATE

        caps = self.driver().capabilities(self.device())
        self.assertEqual(caps, set([CAP_POWER, CAP_STATE]))
        self.assertNotIn(CAP_COLOR, caps)
        self.assertNotIn(CAP_COMMANDS, caps)

    def test_test_connection_reports_the_model_and_state(self):
        self.start(relay_state=1)

        ok, message = self.driver().test_connection(self.device())

        self.assertTrue(ok)
        self.assertIn('HS103', message)
        self.assertIn('on', message)


class FakeBlaster(object):
    """A stand-in for a second vendor: emits commands, has no colour.

    Deliberately shaped like an IR blaster rather than a light. If the driver
    seam is real, this needs no changes anywhere else to work -- the registry,
    the Hub and the scene engine should route to it without knowing what it is.
    """

    DRIVER_ID = 'blaster'
    DRIVER_LABEL = 'Test Blaster'

    def __init__(self, devices=None, codes=None):
        self._devices = devices or []
        self._codes = codes or ['AVR Power', 'TV Input']
        self.sent = []

    def discover(self, timeout=3.0):
        return list(self._devices), []

    def capabilities(self, device):
        from devices import CAP_COMMANDS
        return set([CAP_COMMANDS])

    def commands(self, device):
        return list(self._codes)

    def send_command(self, device, name):
        from devices import ControlError
        if name not in self._codes:
            raise ControlError('%s has no code called "%s"'
                               % (device.name, name))
        self.sent.append((device.device_id, name))
        return True

    # State verbs exist but do nothing: this device has no state to set.
    def turn(self, device, on):
        from devices import ControlError
        raise ControlError('%s cannot be switched' % device.name)

    set_brightness = set_color = set_color_temp = turn

    def get_state(self, device):
        return None

    def get_states(self, devices, timeout=3.0):
        return dict((d.device_id, None) for d in devices)


class TestDriverSeam(unittest.TestCase):
    """A second vendor should need no changes outside its own driver."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'hub', 'scenes'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def build(self):
        import hub as hub_mod

        blaster_device = Device('RM:01', name='Lounge Blaster',
                                driver='blaster')
        blaster = FakeBlaster(devices=[blaster_device])
        lights = RecordingController()
        lights.DRIVER_ID = 'govee'
        lights.DRIVER_LABEL = 'Govee'
        lights.discover = lambda timeout=3.0: (
            [Device('AA:BB', name='Lamp', lan=True, ip='10.0.0.1')], [])
        lights.capabilities = lambda device: set(['power', 'brightness',
                                                  'color'])
        lights.commands = lambda device: []
        lights.get_states = lambda devices, timeout=3.0: {}
        return hub_mod.Hub(drivers=[lights, blaster]), lights, blaster

    def test_discovery_merges_both_drivers_and_tags_ownership(self):
        hub, _lights, _blaster = self.build()
        found, warnings = hub.discover()

        self.assertEqual(warnings, [])
        self.assertEqual(sorted(d.driver for d in found),
                         ['blaster', 'govee'])

    def test_one_driver_failing_does_not_hide_the_other(self):
        hub, lights, _blaster = self.build()

        def broken(timeout=3.0):
            raise RuntimeError('port busy')

        lights.discover = broken
        found, warnings = hub.discover()

        self.assertEqual([d.driver for d in found], ['blaster'])
        self.assertTrue(any('port busy' in w for w in warnings))

    def test_commands_route_to_the_owning_driver(self):
        hub, _lights, blaster = self.build()
        device = Device('RM:01', name='Lounge Blaster', driver='blaster')

        hub.send_command(device, 'AVR Power')
        self.assertEqual(blaster.sent, [('RM:01', 'AVR Power')])

    def test_a_light_refuses_commands_it_cannot_emit(self):
        hub, _lights, _blaster = self.build()
        lamp = Device('AA:BB', name='Lamp', lan=True)

        with self.assertRaises(ControlError):
            hub.send_command(lamp, 'AVR Power')

    def test_devices_cached_before_drivers_existed_still_work(self):
        """An old devices.json records no driver; every entry is Govee."""
        restored = Device.from_dict({'device_id': 'AA:BB', 'name': 'Old',
                                     'lan': True})
        self.assertEqual(restored.driver, 'govee')

        hub, lights, _blaster = self.build()
        hub.turn(restored, True)
        self.assertEqual(lights.calls, [('turn', 'AA:BB', True)])

    def test_a_scene_dims_the_lights_and_fires_a_command(self):
        """The whole point: one scene spanning two vendors."""
        import scenes as fresh_scenes

        hub, lights, blaster = self.build()
        lamp = Device('AA:BB', name='Lamp', lan=True, ip='10.0.0.1')
        blaster_device = Device('RM:01', name='Lounge Blaster',
                                driver='blaster')

        scene = fresh_scenes.make_scene(
            'Movie Night', brightness=8, mode=fresh_scenes.MODE_TEMP,
            kelvin=2000, targets=['AA:BB'],
            actions=[{'device': 'RM:01', 'command': 'AVR Power'}])

        applied, errors = fresh_scenes.apply_scene(
            hub, scene, [lamp, blaster_device])

        self.assertEqual(errors, [])
        self.assertEqual(applied, 2)          # one light, one command
        self.assertIn(('brightness', 'AA:BB', 8), lights.calls)
        self.assertEqual(blaster.sent, [('RM:01', 'AVR Power')])

    def test_a_scene_can_be_commands_only(self):
        """Switch the amp on; no lights involved."""
        import scenes as fresh_scenes

        hub, lights, blaster = self.build()
        blaster_device = Device('RM:01', name='Lounge Blaster',
                                driver='blaster')

        scene = fresh_scenes.make_scene(
            'Amp On', targets=['RM:01'],
            actions=[{'device': 'RM:01', 'command': 'AVR Power'}])
        applied, errors = fresh_scenes.apply_scene(hub, scene,
                                                   [blaster_device])

        # The blaster has no state, so the state pass reports it and the
        # command still goes out.
        self.assertEqual(blaster.sent, [('RM:01', 'AVR Power')])
        self.assertEqual(lights.calls, [])

    def test_a_missing_command_is_reported_not_silent(self):
        import scenes as fresh_scenes

        hub, _lights, blaster = self.build()
        blaster_device = Device('RM:01', name='Lounge Blaster',
                                driver='blaster')

        scene = fresh_scenes.make_scene(
            'Bad', targets=['NOBODY'],
            actions=[{'device': 'RM:01', 'command': 'Nonexistent'}])
        _applied, errors = fresh_scenes.apply_scene(hub, scene,
                                                    [blaster_device])

        self.assertEqual(blaster.sent, [])
        self.assertTrue(any('Nonexistent' in e for e in errors))

    def test_an_action_for_an_unknown_device_is_reported(self):
        import scenes as fresh_scenes

        hub, _lights, _blaster = self.build()
        scene = fresh_scenes.make_scene(
            'Bad', actions=[{'device': 'GONE', 'command': 'AVR Power'}])
        _applied, errors = fresh_scenes.apply_scene(hub, scene, [])
        self.assertTrue(any('AVR Power' in e for e in errors))

    def test_actions_survive_a_json_round_trip(self):
        import scenes as fresh_scenes

        scene = fresh_scenes.make_scene(
            'Movie Night',
            actions=[{'device': 'rm:01', 'command': ' AVR Power '}])
        restored = fresh_scenes.normalise(json.loads(json.dumps(scene)))
        self.assertEqual(restored['actions'],
                         [{'device': 'RM:01', 'command': 'AVR Power'}])

    def test_malformed_actions_are_dropped(self):
        import scenes as fresh_scenes

        scene = fresh_scenes.normalise({
            'name': 'Hand edited',
            'actions': ['junk', {'device': 'X'}, {'command': 'Y'},
                        {'device': 'Z', 'command': '   '},
                        {'device': 'OK', 'command': 'Fine'}]})
        self.assertEqual(scene['actions'],
                         [{'device': 'OK', 'command': 'Fine'}])

    def test_state_reads_are_grouped_per_driver(self):
        """Each driver keeps its own batching -- Govee's one-socket sweep."""
        hub, lights, _blaster = self.build()
        seen = []
        lights.get_states = lambda devices, timeout=3.0: (
            seen.append(len(devices)) or
            dict((d.device_id, {'power': 'on'}) for d in devices))

        states = hub.get_states([
            Device('AA:BB', name='One', lan=True),
            Device('CC:DD', name='Two', lan=True),
            Device('RM:01', name='Blaster', driver='blaster'),
        ])

        self.assertEqual(seen, [2])          # one call for both lights
        self.assertEqual(states['AA:BB']['power'], 'on')
        self.assertIsNone(states['RM:01'])

    def test_describe_mentions_commands(self):
        import scenes as fresh_scenes

        scene = fresh_scenes.make_scene(
            'Movie Night', brightness=8,
            actions=[{'device': 'RM:01', 'command': 'AVR Power'}])
        self.assertIn('1 command(s)', fresh_scenes.describe(scene))


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

class TestDriverData(unittest.TestCase):
    """What a driver must remember about a device has to survive a restart."""

    def test_driver_data_survives_a_round_trip(self):
        device = Device('wp9abc#2', driver='tuya', native_id='wp9abc',
                        driver_data={'version': '3.3', 'dp': '2'})

        restored = Device.from_dict(json.loads(json.dumps(device.to_dict())))

        self.assertEqual(restored.driver_data, {'version': '3.3', 'dp': '2'})
        self.assertEqual(restored.native_id, 'wp9abc')

    def test_a_device_cached_before_driver_data_existed_still_loads(self):
        restored = Device.from_dict({'device_id': 'AA:BB', 'name': 'Lamp'})

        self.assertEqual(restored.driver_data, {})

    def test_a_fresh_discovery_replaces_stale_driver_data(self):
        """A plug that has been re-flashed announces a new version."""
        cached = Device('wp9abc', driver='tuya', driver_data={'version': '3.1'})
        found = Device('wp9abc', driver='tuya', driver_data={'version': '3.3'})

        cached.merge(found)

        self.assertEqual(cached.driver_data['version'], '3.3')


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
        for name in ('addon_utils', 'paragon_home'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def app(self):
        from paragon_home import ParagonHome

        return ParagonHome()

    def test_a_plug_split_into_outlets_drops_its_old_single_entry(self):
        """The pre-split entry is the same hardware; it would switch nothing."""
        app = self.app()
        app._devices = [Device('wp9abc', name='Office Plug', driver='tuya',
                               native_id='wp9abc')]

        outlets = [Device('wp9abc#%s' % dp, name='Outlet %s' % dp,
                          driver='tuya', native_id='wp9abc')
                   for dp in ('1', '2', '3')]
        app.controller.discover = lambda timeout=3.0: (outlets, [])

        listed, _warnings = app.refresh_devices()

        self.assertEqual(sorted(d.device_id for d in listed),
                         ['WP9ABC#1', 'WP9ABC#2', 'WP9ABC#3'])

    def test_a_device_that_merely_missed_a_search_is_still_kept(self):
        """Superseding is narrow: it is not a licence to forget quiet lights."""
        app = self.app()
        app._devices = [Device('AA:BB', name='Hall Lamp')]
        app.controller.discover = lambda timeout=3.0: ([], [])

        listed, _warnings = app.refresh_devices()

        self.assertEqual([d.name for d in listed], ['Hall Lamp'])

    def test_transport_mode_setting_maps_to_constants(self):
        from paragon_home import ParagonHome

        xbmcaddon.SETTINGS['transport_mode'] = '1'
        self.assertEqual(ParagonHome.read_settings()['mode'],
                         devices_mod.TRANSPORT_LAN)

        xbmcaddon.SETTINGS['transport_mode'] = '2'
        self.assertEqual(ParagonHome.read_settings()['mode'],
                         devices_mod.TRANSPORT_CLOUD)

        # Out-of-range values fall back rather than raising.
        xbmcaddon.SETTINGS['transport_mode'] = '99'
        self.assertEqual(ParagonHome.read_settings()['mode'],
                         devices_mod.TRANSPORT_AUTO)

    def test_scenes_are_seeded_and_persisted_on_first_read(self):
        from paragon_home import ParagonHome

        app = ParagonHome()
        names = [s['name'] for s in app.scenes]
        self.assertIn('Movie Night', names)
        self.assertTrue(os.path.isfile(os.path.join(PROFILE, 'scenes.json')))

        # A second session reads them back off disk unchanged.
        app2 = ParagonHome()
        self.assertEqual([s['name'] for s in app2.scenes], names)

    def test_corrupt_scene_file_falls_back_to_defaults(self):
        from paragon_home import ParagonHome

        os.makedirs(PROFILE)
        handle = open(os.path.join(PROFILE, 'scenes.json'), 'w')
        handle.write('{ this is not json')
        handle.close()

        app = ParagonHome()
        self.assertIn('Movie Night', [s['name'] for s in app.scenes])

    def test_device_cache_round_trips(self):
        from paragon_home import ParagonHome

        app = ParagonHome()
        app._devices = [Device('AA:BB', name='Lamp', model='H6159',
                               ip='10.0.0.2', lan=True)]
        app.save_devices()

        reloaded = ParagonHome()
        self.assertEqual(len(reloaded.devices), 1)
        self.assertEqual(reloaded.devices[0].name, 'Lamp')
        self.assertEqual(reloaded.device_by_id('aa:bb').ip, '10.0.0.2')

    def test_refresh_preserves_custom_name_and_disabled_flag(self):
        from paragon_home import ParagonHome

        app = ParagonHome()
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
        from paragon_home import ParagonHome

        app = ParagonHome()

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

        reopened = ParagonHome()          # reloads devices.json from disk
        reopened.controller = LanOnly()
        reopened.refresh_devices()

        self.assertEqual(reopened.devices[0].name, 'KITCHEN RIGHT LOW')
        self.assertFalse(reopened.devices[0].cloud)
        self.assertTrue(reopened.devices[0].lan)

        saved = json.load(open(os.path.join(PROFILE, 'devices.json')))
        self.assertEqual(saved[0]['name'], 'KITCHEN RIGHT LOW')

    def test_a_light_that_misses_one_search_keeps_its_name(self):
        """A sleeping bulb used to be erased along with its name."""
        from paragon_home import ParagonHome

        app = ParagonHome()
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
        from paragon_home import ParagonHome

        app = ParagonHome()
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
        from paragon_home import ParagonHome

        app = ParagonHome()
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
        from paragon_home import ParagonHome

        app = ParagonHome()
        self.assertFalse(app.apply_scene_by_name('Does Not Exist'))
        self.assertTrue(any('Does Not Exist' in message
                            for _heading, message in xbmcgui.NOTIFICATIONS))

    def test_toggle_turns_the_group_off_when_any_light_is_on(self):
        from paragon_home import ParagonHome

        app = ParagonHome()
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
        from paragon_home import ParagonHome

        app = ParagonHome()
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

class TestCollapsingTargets(unittest.TestCase):
    """Folding several targets into one command, where that is safe."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        for name in ('addon_utils', 'paragon_home', 'tuya_driver',
                     'tuya_lan', 'hub'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def plug(self):
        master = Device('wp9abc#all', name='Office Plug', driver='tuya',
                        native_id='wp9abc',
                        driver_data={'dp': 'all', 'members': ['1', '2']})
        outlets = [Device('wp9abc#%s' % dp, name='Outlet %s' % dp,
                          driver='tuya', native_id='wp9abc',
                          driver_data={'dp': dp}) for dp in ('1', '2')]
        return master, outlets

    def app(self, devices):
        from paragon_home import ParagonHome

        app = ParagonHome()
        app._devices = devices
        return app

    def test_a_bulk_switch_sends_one_command_per_plug(self):
        master, outlets = self.plug()
        app = self.app([master] + outlets)
        switched = []
        app.controller.turn = lambda device, on: switched.append(device.name)

        done, errors = app.power_all(False)

        self.assertEqual(switched, ['Office Plug'])
        self.assertEqual((done, errors), (1, []))

    def test_a_hub_with_no_collapsing_driver_passes_the_list_through(self):
        from hub import Hub

        class Plain(object):
            DRIVER_ID = 'plain'
            DRIVER_LABEL = 'Plain'

        devices = [Device('AA:BB'), Device('CC:DD')]
        self.assertEqual(Hub([Plain()]).collapse(devices), devices)

    def test_a_scene_is_not_collapsed(self):
        """A scene can tell outlets different things, so folding them would
        silently drop one. Only same-instruction-to-all comes through the
        collapsing path."""
        import scenes as scenes_mod

        master, outlets = self.plug()
        app = self.app([master] + outlets)
        switched = []
        app.controller.turn = lambda device, on: switched.append(device.name)
        app.controller.capabilities = lambda device: set(['power', 'state'])

        scene = scenes_mod.make_scene('Bedtime', power=scenes_mod.POWER_OFF)
        app.apply_scene(scene, announce=False)

        self.assertEqual(sorted(switched),
                         ['Office Plug', 'Outlet 1', 'Outlet 2'])


class TestScriptArguments(unittest.TestCase):

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'default'):
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
        from paragon_home import ParagonHome

        app = ParagonHome()
        app._devices = [Device('AA:BB', name='Living Room', lan=True)]

        self.assertIsNone(default.resolve_targets(app, 'all'))
        self.assertIsNone(default.resolve_targets(app, ''))
        self.assertEqual(len(default.resolve_targets(app, 'living room')), 1)
        self.assertEqual(len(default.resolve_targets(app, 'aa:bb')), 1)
        self.assertEqual(default.resolve_targets(app, 'nowhere'), [])

    def test_unknown_target_is_reported_and_nothing_is_sent(self):
        import addon_utils
        import default
        from paragon_home import ParagonHome

        app = ParagonHome()
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
        from paragon_home import ParagonHome

        app = ParagonHome()
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

    def test_color_action_accepts_a_saved_colour_name(self):
        import addon_utils
        import default
        from paragon_home import ParagonHome

        app = ParagonHome()
        recorder = RecordingController()
        app._devices = [Device('AA:BB', name='Lamp', lan=True)]
        app.controller = recorder
        app.save_color('Govee Pink', (255, 40, 150))

        default.run_action(app, {'action': 'color', 'value': 'Govee Pink'},
                           addon_utils)
        self.assertEqual(recorder.calls, [('color', 'AA:BB', 255, 40, 150)])

    def test_color_action_rejects_a_name_that_is_not_saved(self):
        import addon_utils
        import default
        from paragon_home import ParagonHome

        app = ParagonHome()
        recorder = RecordingController()
        app._devices = [Device('AA:BB', name='Lamp', lan=True)]
        app.controller = recorder

        default.run_action(app, {'action': 'color', 'value': 'Nonexistent'},
                           addon_utils)
        self.assertEqual(recorder.calls, [])
        self.assertTrue(xbmcgui.NOTIFICATIONS)

    def test_out_of_range_values_are_clamped(self):
        import addon_utils
        import default
        from paragon_home import ParagonHome

        app = ParagonHome()
        recorder = RecordingController()
        app._devices = [Device('AA:BB', name='Lamp', lan=True)]
        app.controller = recorder

        default.run_action(app, {'action': 'brightness', 'value': '900'},
                           addon_utils)
        self.assertEqual(recorder.calls[0][2], 100)

    def test_bad_value_is_reported_not_sent(self):
        import addon_utils
        import default
        from paragon_home import ParagonHome

        app = ParagonHome()
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
        from paragon_home import ParagonHome

        app = ParagonHome()
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
        for name in ('addon_utils', 'paragon_home', 'service'):
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


class TestPalette(unittest.TestCase):
    """The colour speed dial: user-editable, persisted, order-sensitive."""

    def setUp(self):
        clean_profile()
        xbmcaddon.reset()
        xbmcgui.reset()
        for name in ('addon_utils', 'paragon_home', 'palette'):
            if name in sys.modules:
                del sys.modules[name]

    def tearDown(self):
        clean_profile()

    def app(self):
        from paragon_home import ParagonHome
        return ParagonHome()

    def test_seeded_and_persisted_on_first_read(self):
        import palette as palette_lib

        app = self.app()
        self.assertIn('Paragon Purple', [e['name'] for e in app.palette])
        self.assertTrue(os.path.isfile(os.path.join(PROFILE, 'palette.json')))

        # A second session reads the same list back off disk.
        self.assertEqual([e['name'] for e in self.app().palette],
                         [e['name'] for e in palette_lib.default_palette()])

    def test_add_and_remove_round_trip_to_disk(self):
        app = self.app()
        before = len(app.palette)

        self.assertIsNotNone(app.save_color('Sunset', (255, 94, 20)))
        self.assertEqual(len(app.palette), before + 1)

        saved = json.load(open(os.path.join(PROFILE, 'palette.json')))
        self.assertIn({'name': 'Sunset', 'color': [255, 94, 20]}, saved)

        self.assertTrue(app.remove_color(app.color_by_name('Sunset')))
        self.assertIsNone(app.color_by_name('Sunset'))
        saved = json.load(open(os.path.join(PROFILE, 'palette.json')))
        self.assertNotIn('Sunset', [e['name'] for e in saved])

    def test_saving_an_existing_name_replaces_it_in_place(self):
        """Editing a colour must not shuffle the menu order."""
        app = self.app()
        index = [e['name'] for e in app.palette].index('Amber')

        app.save_color('Amber', (200, 100, 0))
        self.assertEqual([e['name'] for e in app.palette].index('Amber'),
                         index)
        self.assertEqual(app.color_by_name('Amber')['color'], [200, 100, 0])
        self.assertEqual(len([e for e in app.palette
                              if e['name'] == 'Amber']), 1)

    def test_name_lookup_is_case_insensitive(self):
        app = self.app()
        self.assertIsNotNone(app.color_by_name('  deep RED '))
        self.assertIsNone(app.color_by_name('nope'))

    def test_moving_reorders_and_persists(self):
        app = self.app()
        names = [e['name'] for e in app.palette]
        moved = app.move_color(2, -2)

        self.assertEqual(moved, 0)
        self.assertEqual(app.palette[0]['name'], names[2])
        saved = json.load(open(os.path.join(PROFILE, 'palette.json')))
        self.assertEqual(saved[0]['name'], names[2])

    def test_moving_past_the_ends_is_a_no_op(self):
        app = self.app()
        names = [e['name'] for e in app.palette]
        self.assertEqual(app.move_color(0, -1), 0)
        self.assertEqual(app.move_color(len(names) - 1, 1), len(names) - 1)
        self.assertEqual([e['name'] for e in app.palette], names)

    def test_a_hand_edited_file_degrades_rather_than_throwing(self):
        os.makedirs(PROFILE)
        handle = open(os.path.join(PROFILE, 'palette.json'), 'w')
        handle.write(json.dumps([
            {'name': 'Good', 'color': [10, 20, 30]},
            {'name': 'Bad colour', 'color': ['x', 2, 3]},
            {'name': '   '},
            'not a dict',
            {'name': 'good', 'color': [1, 2, 3]},      # duplicate name
        ]))
        handle.close()

        app = self.app()
        self.assertEqual([e['name'] for e in app.palette], ['Good'])

    def test_reset_restores_the_built_in_set(self):
        import palette as palette_lib

        app = self.app()
        app.save_color('Sunset', (255, 94, 20))
        app.reset_palette()

        self.assertEqual([e['name'] for e in app.palette],
                         [e['name'] for e in palette_lib.default_palette()])
        saved = json.load(open(os.path.join(PROFILE, 'palette.json')))
        self.assertNotIn('Sunset', [e['name'] for e in saved])

    def test_to_hex_formatting(self):
        import palette as palette_lib

        self.assertEqual(palette_lib.to_hex([255, 40, 150]), '#FF2896')
        self.assertEqual(palette_lib.to_hex([0, 0, 0]), '#000000')
        self.assertEqual(palette_lib.to_hex('nonsense'), '#FFFFFF')


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
        for name in ('addon_utils', 'paragon_home', 'diagnostics'):
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
        from paragon_home import ParagonHome

        app = ParagonHome()

        class StubLAN(object):
            def probe(self, timeout=4.0):
                return TestDiagnostics.report()

        # Reach through the Hub to the Govee driver: the LAN probe belongs to
        # that driver, not to the device layer as a whole.
        app.controller.driver('govee').lan = StubLAN()
        del xbmc.LOG_LINES[:]

        text, report = diagnostics.run(app)
        self.assertEqual(report['cause'], diagnostics.CAUSE_NO_REPLIES)
        self.assertIn('nothing answered', text)
        logged = '\n'.join(message for _level, message in xbmc.LOG_LINES)
        self.assertIn('Paragon Home LAN diagnostics', logged)


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
        for name in ('addon_utils', 'paragon_home', 'diagnostics'):
            if name in sys.modules:
                del sys.modules[name]

        from paragon_home import ParagonHome
        self.app = ParagonHome()
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
        for name in ('addon_utils', 'paragon_home', 'gui'):
            if name in sys.modules:
                del sys.modules[name]

        from paragon_home import ParagonHome
        self.app = ParagonHome()
        self.recorder = RecordingController()
        self.app._devices = [Device('AA:BB', name='Lamp', lan=True,
                                    ip='10.0.0.2')]
        self.app.controller = self.recorder

    def tearDown(self):
        clean_profile()

    def panel(self):
        import gui
        return gui.ControlPanel(self.app)

    def driver_row(self, prefix='Govee'):
        return menu_row(self.panel().main_menu, prefix)[0]

    def test_the_main_menu_lists_a_row_per_kind_of_device(self):
        """One row per driver, not one row per device."""
        self.app._devices = [
            Device('AA:BB', name='Lamp', driver='govee', lan=True),
            Device('CC:DD', name='Plug', driver='tuya', lan=True),
            Device('EE:FF', name='Blaster', driver='broadlink', lan=True),
        ]

        _row, labels = menu_row(self.panel().main_menu, 'Govee')

        self.assertEqual(labels[:3],
                         ['Govee (1)', 'Broadlink (1)', 'Tuya (1)'])
        self.assertNotIn('Lamp', labels)

    def test_a_driver_with_nothing_found_is_not_offered(self):
        """A dead row is worse than no row; the search lives above this."""
        _row, labels = menu_row(self.panel().main_menu, 'Govee')

        self.assertNotIn('Tuya (0)', labels)
        self.assertIn('Diagnose device search...', labels)

    def test_main_menu_group_then_on(self):
        # Govee, then "All Govee", then "On".
        xbmcgui.SELECT_QUEUE.extend([self.driver_row(), 0, 1])
        self.panel().run()
        self.assertEqual(self.recorder.calls, [('turn', 'AA:BB', True)])

    def test_main_menu_device_row_selects_that_device(self):
        self.app._devices.append(Device('CC:DD', name='Strip', lan=True,
                                        ip='10.0.0.3'))
        # Govee, then 0 = All Govee, 1 = first device, 2 = second device.
        xbmcgui.SELECT_QUEUE.extend([self.driver_row(), 2, 2])
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

    def test_a_plug_is_not_offered_brightness_or_colour(self):
        """The point of sorting by driver: only what the device can do."""
        plug = Device('wp9abc#1', name='Office Plug', driver='tuya', lan=True)
        self.app._devices = [plug]
        self.recorder.caps = ['power', 'state']

        _row, labels = menu_row(
            lambda: self.panel().control_menu([plug], 'Office Plug'), 'Toggle')

        self.assertEqual(labels, ['Toggle', 'On', 'Off', 'Show status'])

    def test_a_blaster_opens_its_codes_rather_than_a_light_menu(self):
        """A blaster has no brightness to offer and no colour to apologise for."""
        blaster = Device('EE:FF', name='Bedroom RM', driver='broadlink',
                         lan=True)
        self.app._devices = [blaster]
        self.recorder.caps = ['commands']
        self.recorder.commands = lambda device: ['TV power']

        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().device_menu(blaster)

        heading, labels = xbmcgui.SELECT_CALLS[-1]
        self.assertIn('TV power', labels)
        self.assertNotIn('Brightness...', labels)

    def test_a_driver_of_blasters_offers_no_all_row(self):
        """There is nothing to switch as a group, so the row is absent."""
        self.app._devices = [
            Device('EE:FF', name='Bedroom RM', driver='broadlink', lan=True)]
        self.recorder.caps = ['commands']

        _row, labels = menu_row(
            lambda: self.panel().driver_menu('broadlink'), 'Bedroom RM')

        # No [LAN] tag: a blaster has no other way to be reached.
        self.assertEqual(labels[0], 'Bedroom RM')
        self.assertTrue(labels[-1].startswith('Manage Broadlink devices'))

    def test_a_bulb_keeps_every_row_it_had(self):
        """The filtering must not quietly cost the Govee menu anything."""
        _row, labels = menu_row(
            lambda: self.panel().control_menu(
                [self.app.devices[0]], 'Lamp'), 'Toggle')

        self.assertEqual(labels,
                         ['Toggle', 'On', 'Off', 'Brightness...', 'Colour...',
                          'Colour temperature...', 'Show status',
                          'Check status reporting...'])

    def test_managing_from_a_driver_menu_shows_only_that_driver(self):
        self.app._devices = [
            Device('AA:BB', name='Lamp', driver='govee', lan=True),
            Device('CC:DD', name='Plug', driver='tuya', lan=True),
        ]
        # A plug has no colour, so the light-by-light naming walkthrough has
        # nothing to light up and should not be offered.
        self.recorder.caps = ['power', 'state']

        _row, labels = menu_row(
            lambda: self.panel().manage_devices('tuya'), '[x] Plug')

        self.assertEqual(len(labels), 1)
        self.assertNotIn('Name lights one by one...', labels)

    def test_managing_govee_still_offers_the_naming_walkthrough(self):
        _row, labels = menu_row(
            lambda: self.panel().manage_devices('govee'), 'Name lights')

        self.assertEqual(labels[0], 'Name lights one by one...')

    def test_a_plug_row_is_not_tagged_with_a_transport_it_cannot_choose(self):
        plug = Device('wp9abc#1', name='Office Plug', driver='tuya', lan=True)
        self.app._devices = [plug]
        self.recorder.caps = ['power', 'state']

        _row, labels = menu_row(
            lambda: self.panel().driver_menu('tuya'), 'Office Plug')

        self.assertIn('Office Plug', labels)
        self.assertNotIn('Office Plug  [LAN]', labels)

    def test_a_govee_row_still_says_how_it_is_reached(self):
        """Where there is a genuine choice, the tag earns its place."""
        self.app._devices = [Device('AA:BB', name='Lamp', driver='govee',
                                    lan=True, cloud=True)]

        _row, labels = menu_row(
            lambda: self.panel().driver_menu('govee'), 'Lamp')

        self.assertIn('Lamp  [LAN+CLOUD]', labels)

    def test_an_unreadable_plug_is_explained_in_its_own_terms(self):
        """Not Govee's. A Kasa plug has never heard of UDP 4002."""
        plug = Device('8006KLAP', name='Christmas Tree', driver='kasa',
                      lan=True, ip='10.0.0.31')
        self.app._devices = [plug]
        self.recorder.caps = ['power', 'state']
        self.recorder.get_state = lambda d: None
        self.app.test_device = lambda d: (False, 'It needs your TP-Link '
                                                 'account. Settings -> Kasa.')

        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().show_status(plug)

        text = xbmcgui.OK_DIALOGS[-1][1]
        self.assertIn('TP-Link account', text)
        self.assertNotIn('4002', text)

    def test_a_govee_bulb_keeps_the_explanation_that_fits_it(self):
        """Govee is the one driver with no connection test and a busy port."""
        from devices import ControlError as _Error

        bulb = self.app.devices[0]
        self.recorder.get_state = lambda d: None

        def no_test(device):
            raise _Error('%s cannot be tested' % device.name)
        self.app.test_device = no_test

        self.panel().show_status(bulb)

        self.assertIn('4002', xbmcgui.OK_DIALOGS[-1][1])

    def test_a_plug_is_offered_a_connection_test_not_a_flash(self):
        plug = Device('8006KLAP', name='Christmas Tree', driver='kasa',
                      lan=True, ip='10.0.0.31')
        self.app._devices = [plug]
        self.recorder.caps = ['power', 'state']

        class _Kasa(object):
            def test_connection(self, device):
                return True, 'ok'
        self.recorder.driver_for = lambda device: _Kasa()

        _row, labels = menu_row(lambda: self.panel()._edit_device(plug),
                                'Rename')

        self.assertIn('Test connection', labels)
        self.assertNotIn('Identify (flash this light)', labels)

    def _sequence_app(self):
        by_driver = {'govee': ['power', 'brightness', 'color', 'color_temp',
                               'state'],
                     'tuya': ['power', 'state'],
                     'broadlink': ['commands']}
        self.recorder.capabilities = lambda d: set(by_driver[d.driver])
        self.recorder.commands = lambda device: ['TV power', 'Volume up']
        self.app._devices = [
            Device('AA:BB', name='Back Office Left Low', driver='govee',
                   lan=True),
            Device('WP9ABC#ALL', name='Office Plug All outlets',
                   driver='tuya', lan=True, native_id='wp9abc'),
            Device('EE:FF', name='Bedroom Broadlink', driver='broadlink',
                   lan=True),
        ]
        self.app._scenes = [scene_lib.make_scene('Warshade',
                                                 targets=['AA:BB'])]

    def test_the_main_menu_offers_sequences(self):
        _row, labels = menu_row(self.panel().main_menu, 'Sequences')

        self.assertIn('Sequences...', labels)

    def test_a_sequence_is_built_through_three_choices_per_step(self):
        """Driver, then which one, then what it does -- as spoken aloud."""
        import sequences as sequence_lib

        self._sequence_app()
        self.app._sequences = [sequence_lib.make_sequence('Wind Down')]
        panel = self.panel()

        # Step 1 -- Scene, Warshade.
        kinds = menu_row(lambda: panel.edit_step(self.app.sequences[0], 0),
                         'Scene')[1]
        xbmcgui.SELECT_QUEUE.extend([kinds.index('Scene'), 0])
        panel.edit_step(self.app.sequences[0], 0)

        # Step 2 -- Tuya, the plug, On.
        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend([kinds.index('Tuya'), 1, 0])
        panel.edit_step(self.app.sequences[0], 1)

        # Step 3 -- Broadlink, the blaster, TV power.
        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend([kinds.index('Broadlink'), 1, 0])
        panel.edit_step(self.app.sequences[0], 2)

        steps = self.app.sequences[0]['steps']
        self.assertEqual(
            [sequence_lib.describe_step(s) for s in steps[:3]],
            ['Scene: Warshade', 'WP9ABC#ALL: On', 'EE:FF: TV power'])
        self.assertEqual(steps[2]['kind'], 'command')

    def test_the_editor_shows_all_ten_slots_including_the_empty_ones(self):
        import sequences as sequence_lib

        self._sequence_app()
        sequence = sequence_lib.make_sequence('Wind Down', [
            {'kind': 'scene', 'target': 'Warshade'}])
        self.app._sequences = [sequence]

        _row, labels = menu_row(lambda: self.panel().edit_sequence(sequence),
                                ' 1.')

        slots = [l for l in labels if l[:3].strip().rstrip('.').isdigit()]
        self.assertEqual(len(slots), 10)
        self.assertIn('Scene: Warshade', slots[0])
        self.assertIn('Empty', slots[1])

    def test_clearing_a_step_empties_that_slot_and_no_other(self):
        import sequences as sequence_lib

        self._sequence_app()
        sequence = sequence_lib.make_sequence('Wind Down', [
            {'kind': 'scene', 'target': 'Warshade'},
            {'kind': 'power', 'driver': 'tuya', 'target': 'WP9ABC#ALL',
             'action': 'on'}])
        self.app._sequences = [sequence]
        panel = self.panel()

        kinds = menu_row(lambda: panel.edit_step(sequence, 0), 'Scene')[1]
        xbmcgui.SELECT_QUEUE.extend([kinds.index('Clear this step')])
        panel.edit_step(sequence, 0)

        self.assertEqual(sequence['steps'][0]['kind'], 'none')
        self.assertEqual(sequence['steps'][1]['action'], 'on')

    def test_a_pause_survives_the_step_being_changed(self):
        """The gap belongs to the slot, not to what happens to be in it."""
        import sequences as sequence_lib

        self._sequence_app()
        sequence = sequence_lib.make_sequence('Wind Down', [
            {'kind': 'scene', 'target': 'Warshade', 'pause': 12}])
        self.app._sequences = [sequence]
        panel = self.panel()

        kinds = menu_row(lambda: panel.edit_step(sequence, 0), 'Scene')[1]
        xbmcgui.SELECT_QUEUE.extend([kinds.index('Tuya'), 1, 1])
        panel.edit_step(sequence, 0)

        self.assertEqual(sequence['steps'][0]['action'], 'off')
        self.assertEqual(sequence['steps'][0]['pause'], 12)

    def test_a_new_sequence_will_not_take_a_name_already_used(self):
        import sequences as sequence_lib

        self._sequence_app()
        self.app._sequences = [sequence_lib.make_sequence('Wind Down')]

        xbmcgui.INPUT_QUEUE.append('wind down')
        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().new_sequence()

        self.assertEqual(len(self.app.sequences), 1)
        self.assertIn('already a sequence', xbmcgui.OK_DIALOGS[-1][1])

    def test_the_scene_editor_says_which_all_it_means(self):
        """"All lights" was the old wording and stopped being true."""
        self.app._scenes = [
            scene_lib.make_scene('Warshade', mode=scene_lib.MODE_COLOR,
                                 color=[80, 0, 120]),
            scene_lib.make_scene('All Off', power=scene_lib.POWER_OFF),
        ]

        colour = menu_row(lambda: self.panel().edit_scene(0), 'Lights:')[1]
        xbmcgui.reset()
        plain = menu_row(lambda: self.panel().edit_scene(1), 'Lights:')[1]

        self.assertIn('Lights: all colour devices', colour)
        self.assertIn('Lights: all devices', plain)

    def _blaster_app(self):
        blaster = Device('EE:FF', name='Bedroom Broadlink', driver='broadlink',
                         lan=True)
        self.app._devices = [self.app.devices[0], blaster]
        self.recorder.capabilities = lambda d: set(
            ['commands'] if d.driver == 'broadlink'
            else ['power', 'brightness', 'color', 'color_temp', 'state'])
        self.recorder.commands = lambda d: ['AVR Power', 'TV power']
        return blaster

    def test_the_scene_editor_offers_the_commands_a_scene_sends(self):
        """They worked in the engine all along; there was no way to add one."""
        self._blaster_app()
        self.app._scenes = [scene_lib.make_scene('Movie Night')]

        _row, labels = menu_row(lambda: self.panel().edit_scene(0),
                                'Commands to send')

        self.assertIn('Commands to send: none', labels)

    def test_a_command_is_added_by_picking_a_device_then_a_code(self):
        self._blaster_app()
        scene = scene_lib.make_scene('Movie Night')
        self.app._scenes = [scene]
        panel = self.panel()

        rows = menu_row(lambda: panel.edit_scene(0), 'Commands to send')[1]
        commands = [i for i, l in enumerate(rows)
                    if l.startswith('Commands to send')][0]
        # Commands, "Add a command...", the blaster, "AVR Power", back out,
        # then Save -- the editor works on a copy until then, on purpose.
        xbmcgui.SELECT_QUEUE.extend([commands, 0, 0, 0, -1,
                                     rows.index('Save')])
        panel.edit_scene(0)

        self.assertEqual(self.app.scenes[0]['actions'],
                         [{'device': 'EE:FF', 'command': 'AVR Power'}])

    def test_a_scene_command_fires_when_the_scene_is_applied(self):
        """The whole point of the row: it reaches the engine that existed."""
        blaster = self._blaster_app()
        scene = scene_lib.make_scene(
            'Movie Night', actions=[{'device': 'EE:FF',
                                     'command': 'AVR Power'}])

        fired, errors = scene_lib.fire_actions(
            self.recorder, scene_lib.normalise(scene),
            [self.app.devices[0], blaster])

        self.assertEqual((fired, errors), (1, []))
        self.assertEqual(self.recorder.calls,
                         [('command', 'EE:FF', 'AVR Power')])

    def test_leaving_the_editor_without_saving_changes_nothing(self):
        """The copy is deliberate: cancelling has to discard."""
        self._blaster_app()
        scene = scene_lib.normalise(scene_lib.make_scene('Movie Night'))
        self.app._scenes = [scene]
        panel = self.panel()

        rows = menu_row(lambda: panel.edit_scene(0), 'Commands to send')[1]
        commands = [i for i, l in enumerate(rows)
                    if l.startswith('Commands to send')][0]
        xbmcgui.SELECT_QUEUE.extend([commands, 0, 0, 0, -1, -1])
        panel.edit_scene(0)

        self.assertEqual(self.app.scenes[0]['actions'], [])

    def test_a_command_can_be_taken_off_a_scene_again(self):
        self._blaster_app()
        scene = scene_lib.normalise(scene_lib.make_scene(
            'Movie Night', actions=[{'device': 'EE:FF',
                                     'command': 'AVR Power'}]))
        self.app._scenes = [scene]
        panel = self.panel()

        rows = menu_row(lambda: panel.edit_scene(0), 'Commands to send')[1]
        commands = [i for i, l in enumerate(rows)
                    if l.startswith('Commands to send')][0]
        xbmcgui.YESNO_QUEUE.append(True)
        xbmcgui.SELECT_QUEUE.extend([commands, 0, -1, rows.index('Save')])
        panel.edit_scene(0)

        self.assertEqual(self.app.scenes[0]['actions'], [])

    def test_the_editor_says_when_nothing_can_send_a_command(self):
        self.app._scenes = [scene_lib.make_scene('Movie Night')]
        self.recorder.caps = ['power', 'color', 'state']
        panel = self.panel()

        row = menu_row(lambda: panel.edit_scene(0), 'Commands to send')[0]
        xbmcgui.SELECT_QUEUE.extend([row, -1])
        panel.edit_scene(0)

        self.assertTrue(any('sends commands' in message
                            for _heading, message in xbmcgui.NOTIFICATIONS))

    def test_every_scene_editor_row_survives_a_new_row_being_added(self):
        """Rows are looked up by label now, not counted.

        This editor grew a row four times and the numbering under it had to be
        re-counted by hand each time. It no longer can be.
        """
        self._blaster_app()
        self.app._scenes = [scene_lib.make_scene('Movie Night')]

        _row, labels = menu_row(lambda: self.panel().edit_scene(0), 'Name:')

        for expected in ('Name:', 'Power:', 'Brightness:', 'Appearance:',
                         'Lights:', 'Commands to send:', 'Cycle colours:',
                         'Test this scene', 'Save', 'Delete'):
            self.assertTrue(any(l.startswith(expected) for l in labels),
                            'lost the "%s" row' % expected)

    def test_any_switchable_device_gets_its_own_switch_rows(self):
        """Not only the drivers that need a key, which was Tuya alone."""
        plug = Device('8006ABCD', name='Christmas Tree', driver='kasa',
                      lan=True, ip='10.0.0.31')
        self.app._devices = [plug]
        self.recorder.caps = ['power', 'state']

        class _Kasa(object):
            def test_connection(self, device):
                return True, 'ok'
        self.recorder.driver_for = lambda device: _Kasa()

        _row, labels = menu_row(lambda: self.panel()._edit_device(plug),
                                'Rename')

        self.assertIn('Switch on', labels)
        self.assertIn('Switch off', labels)

    def test_a_blaster_gets_no_switch_rows(self):
        blaster = Device('EE:FF', name='Bedroom RM', driver='broadlink',
                         lan=True)
        self.app._devices = [blaster]
        self.recorder.caps = ['commands']

        _row, labels = menu_row(lambda: self.panel()._edit_device(blaster),
                                'Rename')

        self.assertNotIn('Switch on', labels)

    def test_a_tuya_plug_is_offered_its_power_cut_setting(self):
        plug = Device('WP9ABC#ALL', name='Office Plug', driver='tuya',
                      lan=True, ip='10.0.0.99', native_id='wp9abc')
        self.app._devices = [plug]
        self.recorder.caps = ['power', 'state']

        class _Tuya(object):
            def set_local_key(self, device, key):
                return True

            def local_key(self, device):
                return '0123456789abcdef'

            def test_connection(self, device):
                return True, 'ok'

            def set_power_memory(self, device, value):
                return True
        self.recorder.driver_for = lambda device: _Tuya()

        _row, labels = menu_row(lambda: self.panel()._edit_device(plug),
                                'Rename')

        self.assertIn('After a power cut...', labels)

    def test_a_govee_bulb_is_not_offered_a_power_cut_setting(self):
        bulb = self.app.devices[0]

        _row, labels = menu_row(lambda: self.panel()._edit_device(bulb),
                                'Rename')

        self.assertNotIn('After a power cut...', labels)

    def test_main_menu_shows_the_version(self):
        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().run()

        heading, _labels = xbmcgui.SELECT_CALLS[-1]
        self.assertIn(xbmcaddon._INFO['version'], heading)

    def test_capture_is_reachable_through_the_scenes_menu(self):
        self.recorder.get_states = lambda devices, timeout=3.0: {
            'AA:BB': {'power': 'on', 'brightness': 30, 'colorTem': 2200}}

        scenes_row = menu_row(self.panel().main_menu, 'Scenes')[0]
        capture_row = menu_row(self.panel().scene_menu, 'Capture')[0]

        xbmcgui.SELECT_QUEUE.extend([scenes_row, capture_row, -1])
        xbmcgui.INPUT_QUEUE.append('From The Menu')
        self.panel().main_menu()

        self.assertIsNotNone(self.app.scene_by_name('From The Menu'))

    def test_every_device_row_targets_its_own_light(self):
        """Guards against a loop-variable closure pointing all rows at one light."""
        self.app._devices = [
            Device('AA:BB', name='One', lan=True, ip='10.0.0.1'),
            Device('CC:DD', name='Two', lan=True, ip='10.0.0.2'),
            Device('EE:FF', name='Three', lan=True, ip='10.0.0.3'),
        ]

        driver = self.driver_row()
        _row, labels = menu_row(
            lambda: self.panel().driver_menu('govee'), 'All Govee')

        for device in self.app.devices:
            row = [i for i, label in enumerate(labels)
                   if label.startswith(device.name)][0]
            del self.recorder.calls[:]
            xbmcgui.reset()
            # Govee, the device row, then "Off" inside the control menu.
            xbmcgui.SELECT_QUEUE.extend([driver, row, 2])
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

    def test_colour_row_matches_its_label(self):
        panel = self.panel()
        index = palette_row(panel, 'Paragon Purple')
        expected = tuple(panel.app.palette[index]['color'])

        xbmcgui.SELECT_QUEUE.extend([index])
        panel.color_menu(None, 'All Lights')
        self.assertEqual(self.recorder.calls,
                         [('color', 'AA:BB') + expected])

    def test_colour_rows_show_their_hex(self):
        xbmcgui.SELECT_QUEUE.extend([-1])
        self.panel().color_menu(None, 'All Lights')
        labels = xbmcgui.SELECT_CALLS[-1][1]
        self.assertIn('Paragon Purple  #963CDC', labels)

    def test_colour_custom_hex(self):
        panel = self.panel()
        xbmcgui.SELECT_QUEUE.extend([len(panel.app.palette)])
        xbmcgui.INPUT_QUEUE.append('#00FF00')
        panel.color_menu(None, 'All Lights')
        self.assertEqual(self.recorder.calls, [('color', 'AA:BB', 0, 255, 0)])

    def test_eight_digit_govee_code_reaches_the_lights(self):
        xbmcgui.SELECT_QUEUE.extend([len(self.app.palette)])
        xbmcgui.INPUT_QUEUE.append('FFFF2896')
        self.panel().color_menu(None, 'All Lights')

        self.assertEqual(self.recorder.calls,
                         [('color', 'AA:BB', 255, 40, 150)])
        shown = ' '.join(message for _h, message in xbmcgui.NOTIFICATIONS)
        self.assertIn('AARRGGBB', shown)

    def test_an_eight_digit_code_can_be_saved_into_a_scene(self):
        panel = self.panel()
        # Appearance -> Colour -> Custom hex, then Name, then Save.
        xbmcgui.SELECT_QUEUE.extend([scene_row(panel, 'Appearance:'), 1,
                                     len(panel.app.palette),
                                     scene_row(panel, 'Name:'),
                                     scene_row(panel, 'Save')])
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

    def test_adding_a_colour_from_the_menu_puts_it_in_the_list(self):
        panel = self.panel()
        before = len(panel.app.palette)

        # Manage colours -> "Add a colour...", hex, then name.
        xbmcgui.SELECT_QUEUE.extend([before])
        xbmcgui.INPUT_QUEUE.extend(['FFFF2896', 'Govee Pink'])
        panel.manage_colors()

        entry = panel.app.color_by_name('Govee Pink')
        self.assertIsNotNone(entry)
        self.assertEqual(entry['color'], [255, 40, 150])

        # And it now shows up as a row in the colour menu.
        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend([-1])
        panel.color_menu(None, 'All Lights')
        self.assertIn('Govee Pink  #FF2896', xbmcgui.SELECT_CALLS[-1][1])

    def test_deleting_a_colour_removes_it_from_the_menu(self):
        panel = self.panel()
        index = palette_row(panel, 'Lime')

        # The colour, then "Delete", then confirm.
        xbmcgui.SELECT_QUEUE.extend([index, 4])
        xbmcgui.YESNO_QUEUE.append(True)
        panel.manage_colors()

        self.assertIsNone(panel.app.color_by_name('Lime'))
        xbmcgui.reset()
        xbmcgui.SELECT_QUEUE.extend([-1])
        panel.color_menu(None, 'All Lights')
        labels = ' '.join(xbmcgui.SELECT_CALLS[-1][1])
        self.assertNotIn('Lime', labels)

    def test_declining_the_delete_keeps_the_colour(self):
        panel = self.panel()
        index = palette_row(panel, 'Lime')

        xbmcgui.SELECT_QUEUE.extend([index, 4])
        xbmcgui.YESNO_QUEUE.append(False)
        panel.manage_colors()

        self.assertIsNotNone(panel.app.color_by_name('Lime'))

    def test_changing_a_colours_hex_keeps_its_place(self):
        panel = self.panel()
        index = palette_row(panel, 'Teal')

        xbmcgui.SELECT_QUEUE.extend([index, 1])   # the colour, "Change colour"
        xbmcgui.INPUT_QUEUE.append('112233')
        panel.manage_colors()

        self.assertEqual(panel.app.color_by_name('Teal')['color'],
                         [17, 34, 51])
        self.assertEqual(palette_row(panel, 'Teal'), index)

    def test_renaming_a_colour_keeps_its_place_and_colour(self):
        panel = self.panel()
        index = palette_row(panel, 'Amber')
        original = list(panel.app.color_by_name('Amber')['color'])

        xbmcgui.SELECT_QUEUE.extend([index, 0])   # the colour, "Rename"
        xbmcgui.INPUT_QUEUE.append('Sunset')
        panel.manage_colors()

        self.assertIsNone(panel.app.color_by_name('Amber'))
        entry = panel.app.color_by_name('Sunset')
        self.assertIsNotNone(entry)
        self.assertEqual(entry['color'], original)
        self.assertEqual(palette_row(panel, 'Sunset'), index)

    def test_building_a_mix_scene_from_the_editor(self):
        panel = self.panel()
        red = palette_row(panel, 'Deep Red')
        blue = palette_row(panel, 'Ocean Blue')
        done = len(panel.app.palette)

        # Appearance -> "Mix of colours..." -> tick two -> Done -> Name -> Save
        xbmcgui.SELECT_QUEUE.extend([scene_row(panel, 'Appearance:'), 2,
                                     red, blue, done,
                                     scene_row(panel, 'Name:'),
                                     scene_row(panel, 'Save')])
        xbmcgui.INPUT_QUEUE.append('Party')
        panel.edit_scene(None)

        scene = panel.app.scene_by_name('Party')
        self.assertIsNotNone(scene)
        self.assertEqual(scene['mode'], scene_lib.MODE_MIX)
        self.assertEqual(sorted(e['name'] for e in scene['colors']),
                         ['Deep Red', 'Ocean Blue'])

    def test_ticking_a_mix_colour_twice_removes_it(self):
        panel = self.panel()
        red = palette_row(panel, 'Deep Red')
        done = len(panel.app.palette)
        scene = scene_lib.make_scene('Test')

        xbmcgui.SELECT_QUEUE.extend([red, red, done])
        panel._edit_mix(scene)
        # Nothing ticked at Done, so it refuses and stays in the picker.
        self.assertEqual(scene.get('colors'), [])

    def test_a_mix_scene_spreads_colours_over_the_real_lights(self):
        panel = self.panel()
        panel.app._devices = [
            Device('AA:BB', name='One', lan=True, ip='10.0.0.1'),
            Device('CC:DD', name='Two', lan=True, ip='10.0.0.2'),
            Device('EE:FF', name='Three', lan=True, ip='10.0.0.3'),
            Device('11:22', name='Four', lan=True, ip='10.0.0.4'),
        ]
        panel.app.save_scene(scene_lib.make_scene(
            'Party', mode=scene_lib.MODE_MIX,
            colors=[{'name': 'Red', 'color': [255, 0, 0]},
                    {'name': 'Blue', 'color': [0, 0, 255]}]))

        panel.app.apply_scene(panel.app.scene_by_name('Party'))

        colors = [c[2:] for c in self.recorder.calls if c[0] == 'color']
        self.assertEqual(len(colors), 4)
        self.assertEqual(sorted([colors.count((255, 0, 0)),
                                 colors.count((0, 0, 255))]), [2, 2])

    def test_deleting_a_palette_colour_does_not_change_a_saved_mix(self):
        """Mix colours are copied into the scene, not referenced by name."""
        panel = self.panel()
        entry = panel.app.color_by_name('Deep Red')
        panel.app.save_scene(scene_lib.make_scene(
            'Party', mode=scene_lib.MODE_MIX, colors=[dict(entry)]))

        panel.app.remove_color(panel.app.color_by_name('Deep Red'))

        scene = panel.app.scene_by_name('Party')
        self.assertEqual(scene['colors'][0]['color'], entry['color'])

    def test_a_custom_colour_is_usable_from_the_scene_editor(self):
        panel = self.panel()
        panel.app.save_color('Govee Pink', (255, 40, 150))
        index = palette_row(panel, 'Govee Pink')

        # Appearance -> Colour -> the new entry, then Name, then Save.
        xbmcgui.SELECT_QUEUE.extend([scene_row(panel, 'Appearance:'), 1, index,
                                     scene_row(panel, 'Name:'),
                                     scene_row(panel, 'Save')])
        xbmcgui.INPUT_QUEUE.append('Pink Scene')
        panel.edit_scene(None)

        scene = panel.app.scene_by_name('Pink Scene')
        self.assertEqual(scene['color'], [255, 40, 150])

    def test_manage_colours_is_reachable_from_the_colour_menu(self):
        panel = self.panel()
        xbmcgui.SELECT_QUEUE.extend([-1])
        panel.color_menu(None, 'All Lights')
        self.assertIn('Manage colours...', xbmcgui.SELECT_CALLS[-1][1])

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
        save = scene_row(panel, 'Save')

        xbmcgui.SELECT_QUEUE.extend([scene_row(panel, 'Name:'), save])
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

        xbmcgui.SELECT_QUEUE.extend([scene_row(panel, 'Name:', index),
                                     scene_row(panel, 'Save', index)])
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
