# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Turns a LAN probe into an answer.

"No lights found" has several very different causes that look identical from
the control panel: the reply port being held by another program, inbound UDP
blocked by a firewall, the scan leaving the wrong interface, or lights that
simply do not implement Govee's LAN protocol. This module distinguishes them
and says which one it is.
"""

import addon_utils as utils

# Causes, most specific first.
CAUSE_PORT_BUSY = 'port_busy'
CAUSE_NO_SEND = 'no_send'
CAUSE_NO_REPLIES = 'no_replies'
CAUSE_UNPARSED = 'unparsed'
CAUSE_OK = 'ok'


def collect(app, timeout=4.0):
    """Run the probe and attach the settings context it should be read with."""
    report = app.controller.lan.probe(timeout=timeout)
    report['mode'] = app.controller.mode
    report['api_key_set'] = bool(app.controller.cloud
                                 and app.controller.cloud.configured)
    report['cause'] = classify(report)
    return report


def classify(report):
    """Work out which failure this is."""
    if report.get('bind_error'):
        return CAUSE_PORT_BUSY
    if not any(error is None for _label, error in report.get('attempts', [])):
        return CAUSE_NO_SEND
    if report.get('devices'):
        return CAUSE_OK
    if report.get('raw_replies'):
        return CAUSE_UNPARSED
    return CAUSE_NO_REPLIES


def format_lines(report):
    """Full detail for the Kodi log."""
    lines = ['--- %s LAN diagnostics ---' % utils.ADDON_NAME]
    lines.append('Transport mode: %s' % report.get('mode'))
    lines.append('Cloud API key set: %s' % report.get('api_key_set'))
    lines.append('Configured send address: %s'
                 % (report.get('bind_address') or '(automatic)'))

    addresses = report.get('addresses') or []
    lines.append('Local IPv4 addresses: %s'
                 % (', '.join(addresses) if addresses else 'none detected'))

    lines.append('Listening on UDP %s: %s'
                 % (report.get('listen_port'),
                    'yes' if report.get('bound') else 'NO'))
    if report.get('bind_error'):
        lines.append('Bind error: %s' % report['bind_error'])

    for label, error in report.get('attempts', []):
        lines.append('Scan sent (%s): %s' % (label, error or 'ok'))

    replies = report.get('raw_replies') or []
    lines.append('Datagrams received: %d' % len(replies))
    for ip, text in replies[:12]:
        lines.append('  from %s: %s' % (ip, text))

    devices = report.get('devices') or []
    lines.append('Devices parsed: %d' % len(devices))
    for device in devices:
        lines.append('  %s  sku=%s  ip=%s'
                     % (device.get('device'), device.get('sku'),
                        device.get('ip')))

    lines.append('Verdict: %s' % report.get('cause'))
    lines.append('--- end diagnostics ---')
    return lines


def summary(report):
    """Short, actionable text for the on-screen dialog."""
    cause = report.get('cause')
    replies = len(report.get('raw_replies') or [])
    devices = report.get('devices') or []

    if cause == CAUSE_OK:
        skus = sorted({d.get('sku') or '?' for d in devices})
        return ('Found %d light(s) on the LAN.\n\nModels: %s\n\n'
                'Run "Refresh devices" to save them.'
                % (len(devices), ', '.join(skus)))

    if cause == CAUSE_PORT_BUSY:
        return ('Could not open UDP port %s to listen for replies.\n\n'
                'Another Govee program is holding it. Close the Govee '
                'Desktop app (and any Home Assistant Govee integration on '
                'this machine), then search again.\n\n%s'
                % (report.get('listen_port'), report.get('bind_error') or ''))

    if cause == CAUSE_NO_SEND:
        failures = '; '.join('%s: %s' % (label, error)
                             for label, error in report.get('attempts', [])
                             if error)
        return ('The scan could not be sent on any interface.\n\n%s'
                % failures)

    if cause == CAUSE_UNPARSED:
        return ('Got %d reply/replies, but none was a Govee scan response.\n\n'
                'Something else on the network answered. Check the Kodi log '
                'for the raw contents.' % replies)

    # CAUSE_NO_REPLIES -- the interesting one.
    addresses = ', '.join(report.get('addresses') or []) or 'none detected'
    return ('The scan went out but nothing answered.\n\n'
            'Sent from: %s\n\n'
            'Most likely, in order:\n'
            '1. Your bulbs do not support the Govee LAN API. It covers only '
            'certain models, and the Govee Desktop app uses its own protocol, '
            'so the app finding them does not prove LAN API support.\n'
            '2. Inbound UDP %s is blocked for Kodi by the firewall.\n'
            '3. LAN Control is off for the lights in the Govee Home app.\n\n'
            'If the lights have no LAN Control toggle, set a Govee API key in '
            'Settings and use cloud mode instead.'
            % (addresses, report.get('listen_port')))


def run(app, timeout=4.0):
    """Probe, write the detail to the Kodi log, and return (summary, report)."""
    report = collect(app, timeout=timeout)
    for line in format_lines(report):
        utils.log(line)
    return summary(report), report


# ---------------------------------------------------------------------------
# Status round-trip
# ---------------------------------------------------------------------------

# Distinctive probe colours. The alternate is used when the bulb already
# happens to be showing something close to the first, which would make a
# stale reading indistinguishable from a correct one.
PROBE_COLOR = (255, 0, 255)
PROBE_ALT = (0, 255, 0)
PROBE_BRIGHTNESS = 40
PROBE_TOLERANCE = 40

VERDICT_TRACKS = 'tracks'
VERDICT_STALE = 'stale'
VERDICT_NO_READBACK = 'no_readback'
VERDICT_CONTROL_FAILED = 'control_failed'


def _rgb_of(state):
    """Pull an (r, g, b) tuple out of a state reading, or None."""
    if not state:
        return None
    color = state.get('color')
    if not isinstance(color, dict):
        return None
    try:
        return tuple(int(color.get(k) or 0) for k in ('r', 'g', 'b'))
    except (TypeError, ValueError):
        return None


def _close_to(rgb, wanted):
    if not rgb:
        return False
    return all(abs(a - b) <= PROBE_TOLERANCE for a, b in zip(rgb, wanted))


def verify_status(app, device, settle=1.5, sleep_func=None):
    """Set a known colour on one bulb, read it back, and see if it matches.

    Capture, toggle and "Show status" all trust devStatus. On some models it
    reports a fixed or long-stale payload no matter what the bulb is actually
    doing, which makes every one of those features quietly wrong. Guessing
    from a single capture cannot tell that apart from a bulb that was simply
    set by something else -- driving the bulb ourselves and reading it back
    can.

    The bulb's previous state is restored afterwards on a best-effort basis.
    """
    import time as _time
    from devices import ControlError
    import scenes as scene_lib

    sleep = sleep_func or _time.sleep
    controller = app.controller
    report = {'device': device.name, 'model': device.model, 'ip': device.ip,
              'before': None, 'probe': None, 'readback': None,
              'verdict': None, 'error': None}

    before = controller.get_state(device)
    report['before'] = before

    probe = PROBE_COLOR
    if _close_to(_rgb_of(before), probe):
        probe = PROBE_ALT
    report['probe'] = probe

    try:
        controller.turn(device, True)
        controller.set_brightness(device, PROBE_BRIGHTNESS)
        controller.set_color(device, probe[0], probe[1], probe[2])
    except ControlError as exc:
        report['verdict'] = VERDICT_CONTROL_FAILED
        report['error'] = str(exc)
        return report

    sleep(settle)
    readback = controller.get_state(device)
    report['readback'] = readback

    if not readback:
        report['verdict'] = VERDICT_NO_READBACK
    elif _close_to(_rgb_of(readback), probe):
        report['verdict'] = VERDICT_TRACKS
    else:
        report['verdict'] = VERDICT_STALE

    # Put the bulb back roughly where it was. Best effort only: if the state
    # could not be read going in, there is nothing to restore to.
    restore = scene_lib.state_to_settings(before)
    if restore:
        try:
            scene_lib.apply_settings(controller, device, restore)
        except ControlError as exc:
            utils.log('Could not restore %s after probe: %s'
                      % (device.name, exc))

    for line in format_verify_lines(report):
        utils.log(line)
    return report


def format_verify_lines(report):
    return [
        '--- %s status round-trip ---' % utils.ADDON_NAME,
        'Device: %s (%s) at %s' % (report.get('device'), report.get('model'),
                                   report.get('ip')),
        'Before: %s' % (report.get('before'),),
        'Set to: RGB %s at %d%%' % (report.get('probe'), PROBE_BRIGHTNESS),
        'Read back: %s' % (report.get('readback'),),
        'Verdict: %s' % report.get('verdict'),
        '--- end round-trip ---',
    ]


def verify_summary(report):
    """On-screen wording for each round-trip verdict."""
    verdict = report.get('verdict')
    name = report.get('device')
    probe = report.get('probe') or ()

    if verdict == VERDICT_TRACKS:
        return ('%s reports back what it was set to.\n\n'
                'Status reporting works on this model, so Capture, Toggle and '
                'Show status are trustworthy. A capture that disagrees with '
                'the room means those lights were set by a Govee app scene, '
                'which the LAN protocol cannot see.' % name)

    if verdict == VERDICT_STALE:
        readback = _rgb_of(report.get('readback'))
        return ('%s did NOT report back what it was set to.\n\n'
                'Set to RGB %s, reported %s.\n\n'
                'This model does not keep its LAN status up to date, so '
                'Capture cannot work on it and Toggle cannot tell whether a '
                'light is already on. Build scenes by hand instead '
                '(Scenes - Manage scenes - Add).'
                % (name, probe, readback))

    if verdict == VERDICT_NO_READBACK:
        return ('%s accepted the command but never answered a status '
                'request.\n\nStatus replies arrive on UDP 4002 -- close the '
                'Govee Desktop app and try again.' % name)

    return ('Could not drive %s at all:\n\n%s'
            % (name, report.get('error') or 'unknown error'))


# ---------------------------------------------------------------------------
# Tuya search
# ---------------------------------------------------------------------------

def tuya_lines(report):
    """Full detail for the Kodi log."""
    lines = ['--- Paragon Home Tuya diagnostics ---']
    lines.append('Listened for %.0f seconds' % report.get('listened', 0))
    for port in sorted(report.get('ports', {})):
        lines.append('UDP %d: %s' % (port, report['ports'][port]))

    lines.append('Datagrams that were not Tuya: %d'
                 % report.get('other_traffic', 0))
    for entry in report.get('raw', []):
        lines.append('  port %s from %s, %d bytes, parsed=%s'
                     % (entry['port'], entry['from'], entry['bytes'],
                        entry['parsed']))
        lines.append('    %s' % entry['hex'])

    devices = report.get('devices') or []
    lines.append('Tuya devices heard: %d' % len(devices))
    for device in devices:
        lines.append('  %s  ip=%s  version=%s  product=%s'
                     % (device.get('device_id'), device.get('ip'),
                        device.get('version'), device.get('product_key')))
    lines.append('--- end Tuya diagnostics ---')
    return lines


def tuya_summary(report):
    """Short, actionable text for the on-screen dialog."""
    ports = report.get('ports', {})
    blocked = [port for port, state in ports.items()
               if state != 'listening']
    devices = report.get('devices') or []

    if devices:
        rows = ['%s  %s  (protocol %s)' % (d.get('device_id'), d.get('ip'),
                                           d.get('version'))
                for d in devices[:6]]
        return ('Heard %d Tuya device(s):\n\n%s\n\nRun "Refresh devices" '
                'to add them.' % (len(devices), '\n'.join(rows)))

    if blocked and len(blocked) == len(ports):
        detail = '; '.join('%s: %s' % (p, ports[p]) for p in blocked)
        return ('Could not listen on either Tuya port.\n\n%s\n\nAnother '
                'Tuya program on this machine is probably holding them.'
                % detail)

    if report.get('other_traffic'):
        return ('Heard %d broadcast(s) on the Tuya ports, but none was a Tuya '
                'announcement.\n\nSomething else on the network is using '
                'those ports. The raw bytes are in the Kodi log.'
                % report['other_traffic'])

    return ('Nothing was heard on UDP 6666 or 6667 in %.0f seconds.\n\n'
            'Tuya devices announce themselves every few seconds, so silence '
            'means the announcements are not reaching Kodi:\n\n'
            '1. Inbound UDP is blocked for Kodi by the firewall. This is the '
            'most common cause on Windows.\n'
            '2. The plug is on a different network from Kodi -- a 2.4GHz '
            'guest SSID or a separate VLAN. Broadcasts do not cross subnets.\n'
            '3. The plug is not on WiFi at all. Check it responds in the '
            'GHome app first.\n'
            '4. It is not a Tuya device. If the GHome app also works as '
            '"Smart Life" or "Tuya Smart" for this plug, it is Tuya.'
            % report.get('listened', 0))


def run_tuya(app, timeout=8.0):
    """Probe for Tuya devices, log the detail, return (summary, report)."""
    import tuya_lan

    report = tuya_lan.probe(timeout=timeout, log_func=utils.debug)
    for line in tuya_lines(report):
        utils.log(line)
    return tuya_summary(report), report


# ---------------------------------------------------------------------------
# Kasa search
# ---------------------------------------------------------------------------

def kasa_summary(report):
    """Short, actionable text for the on-screen dialog."""
    devices = report.get('devices') or []
    if devices:
        rows = ['%s  %s  (%s)' % (d.get('alias') or d.get('device_id'),
                                  d.get('ip'), d.get('model') or '?')
                for d in devices[:8]]
        text = ('Found %d Kasa device(s):\n\n%s\n\nRun "Refresh devices" to '
                'add them.' % (len(devices), '\n'.join(rows)))
        if report.get('sweep'):
            # The interesting half of the result: which ones broadcast could
            # not reach says something about the network, not the plugs.
            text += ('\n\n%d of them answered only when addressed directly, '
                     'so your access point is dropping broadcast traffic. '
                     'They will still be found every search, and the Kasa app '
                     'may show fewer devices than this does.'
                     % report['sweep'])
        return text

    if report.get('error'):
        return ('The search could not be sent.\n\n%s' % report['error'])

    return ('No Kasa device answered in %.0f seconds.\n\n'
            'A Kasa plug replies to a broadcast straight away, so silence '
            'means the broadcast never reached it or its reply never came '
            'back:\n\n'
            '1. The plug is on a different network from Kodi -- a 2.4GHz '
            'guest SSID or a separate VLAN. Broadcasts do not cross subnets, '
            'and these plugs are 2.4GHz only.\n'
            '2. Inbound UDP is blocked for Kodi by the firewall. This is the '
            'most common cause on Windows.\n'
            '3. "Local control" or the local API is switched off in the Kasa '
            'app, under the device settings.\n'
            '4. The firmware has closed the local protocol. Newer TP-Link '
            'firmware on some models talks only to the cloud. Check the plug '
            'still works in the Kasa app first -- if it does, this is the '
            'likely answer, and it is not something the add-on can work '
            'around.\n\n'
            'Addresses the search went out from:\n%s'
            % (report.get('listened', 0),
               '\n'.join(report.get('addresses') or ['(none found)'])))


def run_kasa(app, timeout=6.0):
    """Search for Kasa devices, log the detail, return (summary, report)."""
    import kasa_lan
    from govee_lan import local_addresses

    report = {'listened': timeout, 'devices': [], 'error': '',
              'broadcast': 0, 'sweep': 0,
              'addresses': list(local_addresses()) + ['default route']}
    utils.log('--- Paragon Home Kasa diagnostics ---')
    utils.log('Broadcasting UDP %d from: %s'
              % (kasa_lan.PORT, ', '.join(report['addresses'])))
    try:
        report['devices'], counts = kasa_lan.search(timeout=timeout,
                                                    log_func=utils.debug)
        report.update(counts)
    except kasa_lan.KasaError as exc:
        report['error'] = str(exc)
        utils.log('Kasa search failed: %s' % exc)

    for device in report['devices']:
        utils.log('  %s  %s  %s  relay=%s'
                  % (device.get('device_id'), device.get('ip'),
                     device.get('model'), device.get('relay_state')))
    utils.log('--- end Kasa diagnostics ---')
    return kasa_summary(report), report
