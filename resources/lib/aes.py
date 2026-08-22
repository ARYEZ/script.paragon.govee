# -*- coding: utf-8 -*-
"""
Paragon Home
Creator: Aryez
Year: 2026
Part of: Paragon TV Project

AES-128-CBC in pure Python.

Broadlink's LAN protocol encrypts every packet with AES-128-CBC, and Kodi
17.6's Python 2.7 ships no crypto at all -- no hashlib.aes, no pycryptodome,
and no Krypton-era Kodi module that reliably provides one. Rather than depend
on something that may not be installable, the cipher is here.

Speed is irrelevant for this use: Broadlink packets are well under a kilobyte
and are sent when a person presses a button, not in a loop.

The S-box is generated from its algebraic definition rather than pasted as a
256-entry table. A mistyped constant in a hand-copied table produces a cipher
that is subtly wrong rather than obviously broken, and the known-answer tests
would be the only thing standing between that and a very confusing debugging
session. Generating it removes the possibility.
"""

BLOCK_SIZE = 16


def _rotl8(value, shift):
    return ((value << shift) | (value >> (8 - shift))) & 0xFF


def _build_sbox():
    """The Rijndael S-box, from multiplicative inverse plus affine transform."""
    sbox = [0] * 256
    p = 1
    q = 1
    while True:
        # p *= 3 in GF(2^8)
        p = (p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)) & 0xFF
        # q /= 3 in GF(2^8)
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09
        q &= 0xFF
        sbox[p] = (q ^ _rotl8(q, 1) ^ _rotl8(q, 2) ^ _rotl8(q, 3)
                   ^ _rotl8(q, 4) ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


SBOX = _build_sbox()
INV_SBOX = [0] * 256
for _i, _v in enumerate(SBOX):
    INV_SBOX[_v] = _i

RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36,
        0x6C, 0xD8, 0xAB, 0x4D]


def _xtime(value):
    """Multiply by 2 in GF(2^8)."""
    value <<= 1
    if value & 0x100:
        value ^= 0x11B
    return value & 0xFF


def _mul(a, b):
    """Multiply two bytes in GF(2^8)."""
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result & 0xFF


def _expand_key(key):
    """128-bit key -> 11 round keys of 16 bytes."""
    if len(key) != 16:
        raise ValueError('AES-128 needs a 16 byte key, got %d' % len(key))

    words = [list(key[i * 4:i * 4 + 4]) for i in range(4)]
    for index in range(4, 44):
        word = list(words[index - 1])
        if index % 4 == 0:
            word = word[1:] + word[:1]                       # rotate
            word = [SBOX[b] for b in word]                    # substitute
            word[0] ^= RCON[index // 4 - 1]
        words.append([word[j] ^ words[index - 4][j] for j in range(4)])

    return [[b for word in words[r * 4:r * 4 + 4] for b in word]
            for r in range(11)]


def _add_round_key(state, round_key):
    return [state[i] ^ round_key[i] for i in range(16)]


def _shift_rows(state):
    # Column-major: index = column * 4 + row.
    out = list(state)
    for row in range(1, 4):
        for column in range(4):
            out[column * 4 + row] = state[((column + row) % 4) * 4 + row]
    return out


def _inv_shift_rows(state):
    out = list(state)
    for row in range(1, 4):
        for column in range(4):
            out[((column + row) % 4) * 4 + row] = state[column * 4 + row]
    return out


def _mix_columns(state):
    out = [0] * 16
    for column in range(4):
        base = column * 4
        a = state[base:base + 4]
        out[base + 0] = _mul(a[0], 2) ^ _mul(a[1], 3) ^ a[2] ^ a[3]
        out[base + 1] = a[0] ^ _mul(a[1], 2) ^ _mul(a[2], 3) ^ a[3]
        out[base + 2] = a[0] ^ a[1] ^ _mul(a[2], 2) ^ _mul(a[3], 3)
        out[base + 3] = _mul(a[0], 3) ^ a[1] ^ a[2] ^ _mul(a[3], 2)
    return out


def _inv_mix_columns(state):
    out = [0] * 16
    for column in range(4):
        base = column * 4
        a = state[base:base + 4]
        out[base + 0] = (_mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13)
                         ^ _mul(a[3], 9))
        out[base + 1] = (_mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11)
                         ^ _mul(a[3], 13))
        out[base + 2] = (_mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14)
                         ^ _mul(a[3], 11))
        out[base + 3] = (_mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9)
                         ^ _mul(a[3], 14))
    return out


def encrypt_block(block, round_keys):
    state = _add_round_key(list(block), round_keys[0])
    for round_index in range(1, 10):
        state = [SBOX[b] for b in state]
        state = _shift_rows(state)
        state = _mix_columns(state)
        state = _add_round_key(state, round_keys[round_index])
    state = [SBOX[b] for b in state]
    state = _shift_rows(state)
    return _add_round_key(state, round_keys[10])


def decrypt_block(block, round_keys):
    state = _add_round_key(list(block), round_keys[10])
    for round_index in range(9, 0, -1):
        state = _inv_shift_rows(state)
        state = [INV_SBOX[b] for b in state]
        state = _add_round_key(state, round_keys[round_index])
        state = _inv_mix_columns(state)
    state = _inv_shift_rows(state)
    state = [INV_SBOX[b] for b in state]
    return _add_round_key(state, round_keys[0])


def _as_bytes(data):
    """bytearray view that behaves the same on Python 2 and 3."""
    return bytearray(data)


class AESECB(object):
    """AES-128 in ECB mode.

    Tuya uses ECB rather than CBC. ECB is a poor choice in general -- equal
    plaintext blocks give equal ciphertext blocks -- but the protocol is not
    ours to design, and it is what the devices speak.
    """

    def __init__(self, key):
        self.round_keys = _expand_key(_as_bytes(key))

    def encrypt(self, data, pad=True):
        data = _as_bytes(data)
        if pad:
            padding = BLOCK_SIZE - (len(data) % BLOCK_SIZE)
            data = data + bytearray([padding] * padding)
        elif len(data) % BLOCK_SIZE:
            raise ValueError('ECB input must be a multiple of %d bytes'
                             % BLOCK_SIZE)

        out = bytearray()
        for offset in range(0, len(data), BLOCK_SIZE):
            out.extend(encrypt_block(data[offset:offset + BLOCK_SIZE],
                                     self.round_keys))
        return bytes(out)

    def decrypt(self, data, unpad=True):
        data = _as_bytes(data)
        if len(data) % BLOCK_SIZE:
            raise ValueError('ECB input must be a multiple of %d bytes'
                             % BLOCK_SIZE)

        out = bytearray()
        for offset in range(0, len(data), BLOCK_SIZE):
            out.extend(decrypt_block(data[offset:offset + BLOCK_SIZE],
                                     self.round_keys))
        if unpad and out:
            padding = out[-1]
            # Only strip what is a valid PKCS#7 tail; a device that pads
            # differently should give a short read, not a corrupted one.
            if 0 < padding <= BLOCK_SIZE and len(out) >= padding:
                if all(b == padding for b in out[-padding:]):
                    out = out[:-padding]
        return bytes(out)


class AES(object):
    """AES-128 in CBC mode.

    No padding is applied. Broadlink payloads are already built to a multiple
    of the block size, and silently padding would change packets the device
    parses by offset.
    """

    def __init__(self, key, iv):
        self.round_keys = _expand_key(_as_bytes(key))
        self.iv = _as_bytes(iv)
        if len(self.iv) != BLOCK_SIZE:
            raise ValueError('AES-CBC needs a 16 byte IV')

    def encrypt(self, data):
        data = _as_bytes(data)
        if len(data) % BLOCK_SIZE:
            raise ValueError('CBC input must be a multiple of %d bytes'
                             % BLOCK_SIZE)
        out = bytearray()
        previous = self.iv
        for offset in range(0, len(data), BLOCK_SIZE):
            block = data[offset:offset + BLOCK_SIZE]
            block = bytearray(block[i] ^ previous[i]
                              for i in range(BLOCK_SIZE))
            encrypted = bytearray(encrypt_block(block, self.round_keys))
            out.extend(encrypted)
            previous = encrypted
        return bytes(out)

    def decrypt(self, data):
        data = _as_bytes(data)
        if len(data) % BLOCK_SIZE:
            raise ValueError('CBC input must be a multiple of %d bytes'
                             % BLOCK_SIZE)
        out = bytearray()
        previous = self.iv
        for offset in range(0, len(data), BLOCK_SIZE):
            block = data[offset:offset + BLOCK_SIZE]
            decrypted = bytearray(decrypt_block(block, self.round_keys))
            out.extend(bytearray(decrypted[i] ^ previous[i]
                                 for i in range(BLOCK_SIZE)))
            previous = block
        return bytes(out)
