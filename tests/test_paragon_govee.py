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


def clean_profile():
    if os.path.isdir(PROFILE):
        shutil.rmtree(PROFILE)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeGoveeDevice(object):
    """A UDP socket that answers scan and devStatus like a real Govee light."""

    def __init__(self, device_id, sku, port):
        self.device_id = device_id
        self.sku = sku
        self.received = []
        self.state = {'onOff': 1, 'brightness': 42,
                      'color': {'r': 10, 'g': 20, 'b': 30},
                      'colorTemInKelvin': 0}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', port))
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
                    'ip': '127.0.0.1', 'device': self.device_id,
                    'sku': self.sku, 'wifiVersionSoft': '1.02.03'}}})
            elif cmd == 'devStatus':
                self._reply(address, {'msg': {'cmd': 'devStatus',
                                              'data': self.state}})

    def _reply(self, address, message):
        out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
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
        deadline = __import__('time').time() + 2
        while __import__('time').time() < deadline:
            if self.cmd_device.commands('brightness'):
                break
            __import__('time').sleep(0.05)
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
        deadline = __import__('time').time() + 2
        while __import__('time').time() < deadline:
            if len(self.cmd_device.commands('turn')) >= 3:
                break
            __import__('time').sleep(0.05)
        self.assertEqual(len(self.cmd_device.commands('turn')), 3)

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

    def test_scene_menu_last_row_opens_the_editor(self):
        scene_count = len(self.app.scenes)
        # Last row is "Manage scenes...", then cancel out of both menus.
        xbmcgui.SELECT_QUEUE.extend([scene_count])
        self.panel().scene_menu()
        headings = [heading for heading, _options in xbmcgui.SELECT_CALLS]
        self.assertIn('Manage scenes', headings)

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

    def test_device_rename_persists(self):
        panel = self.panel()
        xbmcgui.SELECT_QUEUE.extend([0, 0])  # first device, then "Rename"
        xbmcgui.INPUT_QUEUE.append('Behind the TV')
        panel.manage_devices()

        self.assertEqual(self.app.devices[0].name, 'Behind the TV')
        saved = json.load(open(os.path.join(PROFILE, 'devices.json')))
        self.assertEqual(saved[0]['name'], 'Behind the TV')

    def test_device_can_be_disabled_and_drops_out_of_the_group(self):
        panel = self.panel()
        xbmcgui.SELECT_QUEUE.extend([0, 1])  # first device, then "Disable"
        panel.manage_devices()

        self.assertFalse(self.app.devices[0].enabled)
        self.assertEqual(self.app.enabled_devices, [])

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
