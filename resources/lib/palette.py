# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

The colour speed dial.

The named colours offered in the colour menus. Kept as a plain list of dicts
in the add-on profile so it round-trips through JSON, can be hand-edited, and
can be copied to another Kodi box the same way scenes and device names can.

Order is meaningful -- it is the order of the menu -- so this is a list rather
than a mapping.
"""

PALETTE_FILE = 'palette.json'


def default_palette():
    """The starter set, written on first run and editable afterwards."""
    return [
        {'name': 'Warm White', 'color': [255, 180, 107]},
        {'name': 'Cool White', 'color': [255, 255, 255]},
        {'name': 'Paragon Purple', 'color': [150, 60, 220]},
        {'name': 'Deep Red', 'color': [255, 0, 0]},
        {'name': 'Amber', 'color': [255, 120, 0]},
        {'name': 'Lime', 'color': [120, 255, 60]},
        {'name': 'Teal', 'color': [0, 200, 180]},
        {'name': 'Ocean Blue', 'color': [0, 80, 255]},
        {'name': 'Magenta', 'color': [255, 0, 150]},
    ]


def to_hex(rgb):
    """#RRGGBB for display."""
    try:
        return '#%02X%02X%02X' % (int(rgb[0]), int(rgb[1]), int(rgb[2]))
    except (TypeError, ValueError, IndexError):
        return '#FFFFFF'


def normalise(entry):
    """Coerce one loaded entry into shape, or None if it cannot be salvaged.

    The palette is user-editable JSON, so anything read back off disk is
    treated as untrusted -- a hand-edited file should degrade rather than
    throw somewhere inside a menu.
    """
    if not isinstance(entry, dict):
        return None

    name = entry.get('name')
    # Duck-typing on strip() rather than isinstance(str): on Python 2 a name
    # loaded from JSON comes back as unicode.
    if not name or not hasattr(name, 'strip'):
        return None
    name = name.strip()
    if not name:
        return None

    color = entry.get('color')
    try:
        rgb = [max(0, min(255, int(c))) for c in color][:3]
    except (TypeError, ValueError):
        return None
    if len(rgb) != 3:
        return None

    return {'name': name, 'color': rgb}


def normalise_all(raw):
    """Clean a loaded palette, dropping junk and duplicate names."""
    if not isinstance(raw, list):
        return []
    cleaned = []
    seen = set()
    for entry in raw:
        item = normalise(entry)
        if item is None:
            continue
        key = item['name'].lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(item)
    return cleaned


def find(palette, name):
    """Look a colour up by name, case-insensitively. None if absent."""
    if not name:
        return None
    wanted = name.strip().lower()
    for entry in palette or []:
        if entry.get('name', '').strip().lower() == wanted:
            return entry
    return None


def move(palette, index, offset):
    """Shift an entry within the list. Returns the new index.

    Out-of-range moves are clamped rather than refused, so holding "move up"
    on the top entry does nothing instead of erroring.
    """
    if not palette or index < 0 or index >= len(palette):
        return index
    target = max(0, min(len(palette) - 1, index + offset))
    if target == index:
        return index
    entry = palette.pop(index)
    palette.insert(target, entry)
    return target
