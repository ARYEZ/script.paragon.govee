# -*- coding: utf-8 -*-
"""
Paragon Govee - artwork generator (development tool, not shipped behaviour).

Renders icon.png and fanart.png from code so the artwork can be regenerated or
recoloured without a binary editing round-trip. Uses only zlib and struct, so
it needs no imaging library.

    python3 tools/make_assets.py
"""

from __future__ import print_function

import math
import os
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The Paragon accent, plus the warm end of a Govee bulb.
PURPLE = (150, 60, 220)
MAGENTA = (232, 62, 140)
AMBER = (255, 176, 74)
CORE = (255, 244, 226)
TEAL = (40, 190, 190)
BACKDROP = (14, 12, 22)


def write_png(path, width, height, rows):
    """Write 8-bit RGB rows (each a bytearray of width*3) as a PNG."""
    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter type 0 (None)
        raw.extend(row)

    def chunk(tag, payload):
        out = struct.pack('>I', len(payload)) + tag + payload
        return out + struct.pack('>I', zlib.crc32(tag + payload) & 0xFFFFFFFF)

    header = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    data = b'\x89PNG\r\n\x1a\n'
    data += chunk(b'IHDR', header)
    data += chunk(b'IDAT', zlib.compress(bytes(raw), 9))
    data += chunk(b'IEND', b'')

    handle = open(path, 'wb')
    try:
        handle.write(data)
    finally:
        handle.close()
    return len(data)


def mix(low, high, amount):
    amount = max(0.0, min(1.0, amount))
    return tuple(int(round(a + (b - a) * amount)) for a, b in zip(low, high))


def add_light(base, color, amount):
    """Screen-style additive blend, so overlapping glows stay bright."""
    amount = max(0.0, min(1.0, amount))
    return tuple(min(255, int(base[i] + color[i] * amount)) for i in range(3))


def smoothstep(edge0, edge1, x):
    if edge1 == edge0:
        return 0.0
    t = max(0.0, min(1.0, (x - edge0) / float(edge1 - edge0)))
    return t * t * (3 - 2 * t)


def render_icon(size=512):
    """A glowing lamp orb: bright core, warm mid, purple halo, thin ring."""
    rows = []
    centre = size / 2.0
    radius = size * 0.34

    for y in range(size):
        row = bytearray()
        for x in range(size):
            dx = (x - centre) / radius
            dy = (y - centre) / radius
            dist = math.sqrt(dx * dx + dy * dy)

            # Vignetted backdrop so the tile reads well on light skins too.
            corner = math.sqrt(((x - centre) / centre) ** 2 +
                               ((y - centre) / centre) ** 2)
            pixel = mix(BACKDROP, (6, 5, 11), smoothstep(0.4, 1.4, corner))

            if dist < 1.35:
                # Body of the orb: core -> amber -> magenta -> purple.
                if dist < 0.34:
                    body = mix(CORE, AMBER, smoothstep(0.0, 0.34, dist))
                elif dist < 0.68:
                    body = mix(AMBER, MAGENTA, smoothstep(0.34, 0.68, dist))
                else:
                    body = mix(MAGENTA, PURPLE, smoothstep(0.68, 1.0, dist))

                # Hard-ish edge at dist == 1, then a soft halo beyond it.
                solid = 1.0 - smoothstep(0.94, 1.02, dist)
                halo = (1.0 - smoothstep(1.0, 1.35, dist)) * 0.33
                pixel = mix(pixel, body, solid)
                pixel = add_light(pixel, PURPLE, halo * 0.7)

                # A crisp ring gives the shape definition at small sizes.
                ring = (1.0 - smoothstep(0.0, 0.055, abs(dist - 1.08)))
                pixel = add_light(pixel, (150, 120, 255), ring * 0.55)

            row.extend(pixel)
        rows.append(row)
    return rows


def render_fanart(width=1280, height=720):
    """Three soft coloured pools on a dark ground."""
    lights = [
        (0.22, 0.34, 0.52, AMBER, 0.95),
        (0.63, 0.62, 0.60, PURPLE, 1.0),
        (0.86, 0.24, 0.40, TEAL, 0.55),
    ]
    rows = []
    for y in range(height):
        row = bytearray()
        vy = y / float(height)
        for x in range(width):
            vx = x / float(width)
            # Gentle top-to-bottom gradient behind everything.
            pixel = mix((18, 15, 28), (7, 6, 12), vy)

            for cx, cy, size, color, strength in lights:
                dx = (vx - cx)
                dy = (vy - cy) * (height / float(width))
                dist = math.sqrt(dx * dx + dy * dy) / size
                falloff = max(0.0, 1.0 - dist)
                pixel = add_light(pixel, color,
                                  (falloff ** 2.4) * strength * 0.9)

            row.extend(pixel)
        rows.append(row)
    return rows


def main():
    icon_path = os.path.join(ROOT, 'icon.png')
    fanart_path = os.path.join(ROOT, 'fanart.png')

    size = write_png(icon_path, 512, 512, render_icon(512))
    print('icon.png    512x512   %d bytes' % size)

    size = write_png(fanart_path, 1280, 720, render_fanart(1280, 720))
    print('fanart.png  1280x720  %d bytes' % size)
    return 0


if __name__ == '__main__':
    sys.exit(main())
