# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

Python 2.7 / Python 3 compatibility shims.

Kodi 17.6 (Krypton) ships Python 2.7, so this add-on's baseline is 2.7 syntax:
no f-strings, no keyword-only arguments, no annotations. Everything that moved
between the two versions is funnelled through this module so the rest of the
code can stay version-agnostic and the tree can be re-targeted at Kodi 19+ by
changing addon.xml alone.
"""

import sys

PY2 = sys.version_info[0] == 2

if PY2:  # pragma: no cover - exercised on Kodi 17.6 only
    import urllib2 as _urllib_request
    from urllib2 import HTTPError, URLError
    from urllib import urlencode, unquote
    from urlparse import parse_qs

    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
    from SocketServer import ThreadingMixIn

    string_types = (str, unicode)  # noqa: F821 - `unicode` only exists on PY2
    text_type = unicode  # noqa: F821
else:
    import urllib.request as _urllib_request
    from urllib.error import HTTPError, URLError
    from urllib.parse import urlencode, unquote, parse_qs

    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn

    string_types = (str,)
    text_type = str

Request = _urllib_request.Request
urlopen = _urllib_request.urlopen

__all__ = [
    'PY2', 'HTTPError', 'URLError', 'urlencode', 'unquote', 'parse_qs',
    'Request', 'urlopen', 'BaseHTTPRequestHandler', 'HTTPServer',
    'ThreadingMixIn', 'string_types', 'text_type', 'to_text', 'to_bytes',
    'to_native', 'same_secret',
]


def to_text(value, encoding='utf-8'):
    """Return `value` as the native text type, decoding bytes if needed."""
    if isinstance(value, bytes):
        return value.decode(encoding, 'replace')
    if isinstance(value, string_types):
        return value
    return text_type(value)


def to_bytes(value, encoding='utf-8'):
    """Return `value` as bytes, encoding text if needed."""
    if isinstance(value, bytes):
        return value
    if not isinstance(value, string_types):
        value = text_type(value)
    return value.encode(encoding, 'replace')


def to_native(value):
    """Return `value` as the native `str`: bytes on Python 2, text on 3.

    Needed wherever text meets binary in the same operation. Python 2's
    httplib joins the request headers into one string and then appends the
    body to it -- so a single unicode header, which a URL built from a
    unicode address produces, makes the whole thing unicode and forces an
    implicit ascii decode of a binary body. The failure surfaces as an
    ascii codec error pointing at byte 0 of the payload, which says nothing
    about the actual cause.
    """
    if PY2:
        return to_bytes(value)
    return to_text(value)


def same_secret(left, right):
    """Compare two secrets without leaking their length or content in time.

    `hmac.compare_digest` would do this, but it only arrived in Python 2.7.7
    and the interpreter embedded in a given Krypton build is not something
    this add-on gets to choose. The fallback is the standard trick: compare
    every byte of the longer of the two, accumulating differences, so the
    loop takes the same time whether the first byte differs or the last.

    A PIN is short enough that a remote timing attack over a LAN is a stretch;
    this is here because the alternative is `==`, and `==` on a secret is a
    habit worth not having.
    """
    left = to_bytes(left or '')
    right = to_bytes(right or '')
    difference = len(left) ^ len(right)
    # bytearray indexes to ints on both versions; `left[i]` alone gives a
    # one-character str on Python 2 and an int on Python 3.
    left_bytes = bytearray(left)
    right_bytes = bytearray(right)
    for index in range(max(len(left_bytes), len(right_bytes))):
        first = left_bytes[index] if index < len(left_bytes) else 0
        second = right_bytes[index] if index < len(right_bytes) else 0
        difference |= first ^ second
    return difference == 0
