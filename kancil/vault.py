"""Encrypted session vault — zero dependency.

Menyimpan data sesi (hasil session_export: cookies incl. HttpOnly +
localStorage) terenkripsi di ~/.kancil/vault/<name>.kv.

Kripto (semua pure-python + hashlib/hmac stdlib):
- KDF: PBKDF2-HMAC-SHA256, 200.000 iterasi, salt 16 byte
- Enkripsi: AES-256-CBC, PKCS#7 padding, IV 16 byte random
- Integritas: HMAC-SHA256 Encrypt-then-MAC (verifikasi SEBELUM dekripsi)

Format file: b"KANCILVAULT1" + salt(16) + iv(16) + ciphertext + mac(32)
"""

import hashlib
import hmac as _hmac
import os

MAGIC = b"KANCILVAULT1"
SALT_LEN = 16
IV_LEN = 16
MAC_LEN = 32
PBKDF2_ITERS = 200_000

# ---------- AES-256 (FIPS-197) pure python ----------
_SBOX = (
    0x63, 0x7c, 0x77, 0x7b, 0xf2, 0x6b, 0x6f, 0xc5, 0x30, 0x01, 0x67, 0x2b,
    0xfe, 0xd7, 0xab, 0x76, 0xca, 0x82, 0xc9, 0x7d, 0xfa, 0x59, 0x47, 0xf0,
    0xad, 0xd4, 0xa2, 0xaf, 0x9c, 0xa4, 0x72, 0xc0, 0xb7, 0xfd, 0x93, 0x26,
    0x36, 0x3f, 0xf7, 0xcc, 0x34, 0xa5, 0xe5, 0xf1, 0x71, 0xd8, 0x31, 0x15,
    0x04, 0xc7, 0x23, 0xc3, 0x18, 0x96, 0x05, 0x9a, 0x07, 0x12, 0x80, 0xe2,
    0xeb, 0x27, 0xb2, 0x75, 0x09, 0x83, 0x2c, 0x1a, 0x1b, 0x6e, 0x5a, 0xa0,
    0x52, 0x3b, 0xd6, 0xb3, 0x29, 0xe3, 0x2f, 0x84, 0x53, 0xd1, 0x00, 0xed,
    0x20, 0xfc, 0xb1, 0x5b, 0x6a, 0xcb, 0xbe, 0x39, 0x4a, 0x4c, 0x58, 0xcf,
    0xd0, 0xef, 0xaa, 0xfb, 0x43, 0x4d, 0x33, 0x85, 0x45, 0xf9, 0x02, 0x7f,
    0x50, 0x3c, 0x9f, 0xa8, 0x51, 0xa3, 0x40, 0x8f, 0x92, 0x9d, 0x38, 0xf5,
    0xbc, 0xb6, 0xda, 0x21, 0x10, 0xff, 0xf3, 0xd2, 0xcd, 0x0c, 0x13, 0xec,
    0x5f, 0x97, 0x44, 0x17, 0xc4, 0xa7, 0x7e, 0x3d, 0x64, 0x5d, 0x19, 0x73,
    0x60, 0x81, 0x4f, 0xdc, 0x22, 0x2a, 0x90, 0x88, 0x46, 0xee, 0xb8, 0x14,
    0xde, 0x5e, 0x0b, 0xdb, 0xe0, 0x32, 0x3a, 0x0a, 0x49, 0x06, 0x24, 0x5c,
    0xc2, 0xd3, 0xac, 0x62, 0x91, 0x95, 0xe4, 0x79, 0xe7, 0xc8, 0x37, 0x6d,
    0x8d, 0xd5, 0x4e, 0xa9, 0x6c, 0x56, 0xf4, 0xea, 0x65, 0x7a, 0xae, 0x08,
    0xba, 0x78, 0x25, 0x2e, 0x1c, 0xa6, 0xb4, 0xc6, 0xe8, 0xdd, 0x74, 0x1f,
    0x4b, 0xbd, 0x8b, 0x8a, 0x70, 0x3e, 0xb5, 0x66, 0x48, 0x03, 0xf6, 0x0e,
    0x61, 0x35, 0x57, 0xb9, 0x86, 0xc1, 0x1d, 0x9e, 0xe1, 0xf8, 0x98, 0x11,
    0x69, 0xd9, 0x8e, 0x94, 0x9b, 0x1e, 0x87, 0xe9, 0xce, 0x55, 0x28, 0xdf,
    0x8c, 0xa1, 0x89, 0x0d, 0xbf, 0xe6, 0x42, 0x68, 0x41, 0x99, 0x2d, 0x0f,
    0xb0, 0x54, 0xbb, 0x16)
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36]


def _xtime(a):
    return ((a << 1) ^ 0x1b) & 0xff if a & 0x80 else (a << 1) & 0xff


def _gmul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        a = _xtime(a)
        b >>= 1
    return p


def _key_expansion(key):
    # Nk=8 word untuk 256-bit -> 60 word (15 round key);
    # Nk=4 word untuk 128-bit -> 44 word (11 round key).
    nk = len(key) // 4
    nr = {4: 10, 6: 12, 8: 14}[nk]
    w = [int.from_bytes(key[i:i + 4], "big")
         for i in range(0, len(key), 4)]
    for i in range(nk, 4 * (nr + 1)):
        t = w[i - 1]
        if i % nk == 0:
            t = (_SBOX[(t >> 16) & 0xff] << 24 | _SBOX[(t >> 8) & 0xff] << 16
                 | _SBOX[t & 0xff] << 8 | _SBOX[(t >> 24) & 0xff])
            t ^= _RCON[i // nk - 1] << 24
        elif nk > 6 and i % nk == 4:
            t = (_SBOX[(t >> 24) & 0xff] << 24 | _SBOX[(t >> 16) & 0xff] << 16
                 | _SBOX[(t >> 8) & 0xff] << 8 | _SBOX[t & 0xff])
        w.append(w[i - nk] ^ t)
    return b"".join(x.to_bytes(4, "big") for x in w), nr


def _add_rk(st, rk, rnd):
    off = rnd * 16
    return bytes(s ^ rk[off + i] for i, s in enumerate(st))


# state: list 16 byte, column-major — indeks(r, c) = r + 4*c
def _shift_rows(st):
    return [st[r + 4 * ((c + r) % 4)] for c in range(4) for r in range(4)]


def _inv_shift_rows(st):
    return [st[r + 4 * ((c - r) % 4)] for c in range(4) for r in range(4)]


def _mix_columns(st):
    st = list(st)
    for c in range(4):
        a0, a1, a2, a3 = st[4 * c:4 * c + 4]
        st[4 * c:4 * c + 4] = (
            _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3,
            a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3,
            a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3),
            _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2))
    return st


def _inv_mix_columns(st):
    st = list(st)
    for c in range(4):
        a0, a1, a2, a3 = st[4 * c:4 * c + 4]
        st[4 * c:4 * c + 4] = (
            _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9),
            _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13),
            _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11),
            _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14))
    return st


def _enc_block(key, blk):
    rk, nr = _key_expansion(key)
    st = [b ^ rk[i] for i, b in enumerate(blk)]
    for rnd in range(1, nr):
        st = [_SBOX[b] for b in st]
        st = _shift_rows(st)
        st = _mix_columns(st)
        st = [b ^ rk[rnd * 16 + i] for i, b in enumerate(st)]
    st = [_SBOX[b] for b in st]
    st = _shift_rows(st)
    st = [b ^ rk[nr * 16 + i] for i, b in enumerate(st)]
    return bytes(st)


def _dec_block(key, blk):
    rk, nr = _key_expansion(key)
    st = [b ^ rk[nr * 16 + i] for i, b in enumerate(blk)]
    for rnd in range(nr - 1, 0, -1):
        st = _inv_shift_rows(st)
        st = [_INV_SBOX[b] for b in st]
        st = [b ^ rk[rnd * 16 + i] for i, b in enumerate(st)]
        st = _inv_mix_columns(st)
    st = _inv_shift_rows(st)
    st = [_INV_SBOX[b] for b in st]
    st = [b ^ rk[i] for i, b in enumerate(st)]
    return bytes(st)


def _pkcs7_pad(data):
    n = 16 - (len(data) % 16)
    return data + bytes([n]) * n


def _pkcs7_unpad(data):
    if not data or len(data) % 16:
        raise ValueError("bad padding")
    n = data[-1]
    if n < 1 or n > 16 or data[-n:] != bytes([n]) * n:
        raise ValueError("bad padding")
    return data[:-n]


def _cbc_encrypt(key, iv, data):
    out, prev = b"", iv
    for i in range(0, len(data), 16):
        blk = _enc_block(key, bytes(a ^ b for a, b in
                                    zip(data[i:i + 16], prev)))
        out += blk
        prev = blk
    return out


def _cbc_decrypt(key, iv, data):
    out, prev = b"", iv
    for i in range(0, len(data), 16):
        enc = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(_dec_block(key, enc), prev))
        prev = enc
    return out


# ---------- vault ----------
def _vault_dir():
    d = os.path.expanduser("~/.kancil/vault")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def _path(name):
    safe = "".join(c for c in name if c.isalnum() or c in "-_") or "default"
    return os.path.join(_vault_dir(), safe + ".kv")


def _derive(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                               salt, PBKDF2_ITERS, dklen=32)


def encrypt(password, plaintext: bytes) -> bytes:
    salt = os.urandom(SALT_LEN)
    iv = os.urandom(IV_LEN)
    key = _derive(password, salt)
    ct = _cbc_encrypt(key, iv, _pkcs7_pad(plaintext))
    body = MAGIC + salt + iv + ct
    mac = _hmac.new(key, body, hashlib.sha256).digest()
    return body + mac


def decrypt(password, blob: bytes) -> bytes:
    if not blob.startswith(MAGIC):
        raise ValueError("not a kancil vault file")
    if len(blob) < len(MAGIC) + SALT_LEN + IV_LEN + 16 + MAC_LEN:
        raise ValueError("vault file truncated")
    body, mac = blob[:-MAC_LEN], blob[-MAC_LEN:]
    salt = body[len(MAGIC):len(MAGIC) + SALT_LEN]
    iv = body[len(MAGIC) + SALT_LEN:len(MAGIC) + SALT_LEN + IV_LEN]
    ct = body[len(MAGIC) + SALT_LEN + IV_LEN:]
    key = _derive(password, salt)
    if not _hmac.compare_digest(
            _hmac.new(key, body, hashlib.sha256).digest(), mac):
        raise ValueError("wrong password or corrupted vault")
    return _pkcs7_unpad(_cbc_decrypt(key, iv, ct))


def save(name, password, data: bytes):
    p = _path(name)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encrypt(password, data))
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    return p


def load(name, password) -> bytes:
    p = _path(name)
    if not os.path.exists(p):
        raise FileNotFoundError("no vault entry %r" % name)
    with open(p, "rb") as f:
        return decrypt(password, f.read())


def list_entries():
    d = _vault_dir()
    return sorted(f[:-3] for f in os.listdir(d) if f.endswith(".kv"))


def delete(name):
    p = _path(name)
    if not os.path.exists(p):
        raise FileNotFoundError("no vault entry %r" % name)
    os.remove(p)
