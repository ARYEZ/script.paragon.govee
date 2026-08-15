# -*- coding: utf-8 -*-
"""
Paragon Govee
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
    lines = ['--- Paragon Govee LAN diagnostics ---']
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
