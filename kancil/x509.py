"""Pure-stdlib X.509/RSA for the MITM proxy.

Generates a local CA and per-host certificates without the openssl CLI,
so Level 2 (--mitm) has zero external binary dependencies.

Only what a MITM proxy needs: RSA-2048 keygen (Miller-Rabin, secrets-based),
PKCS#1 v1.5 SHA-256 signing, DER encoding, and minimal v3 certificate
building (basicConstraints, keyUsage, SAN, EKU). Not a general PKI library.
"""

import base64
import hashlib
import math
import secrets
import time

# ---------------- DER ----------------

def _len(n):
    if n < 128:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag, content):
    return bytes([tag]) + _len(len(content)) + content


def der_int(n):
    if n == 0:
        return _tlv(0x02, b"\x00")
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    if b[0] & 0x80:
        b = b"\x00" + b
    return _tlv(0x02, b)


def der_null():
    return _tlv(0x05, b"")


def der_bool(v):
    return _tlv(0x01, b"\xff" if v else b"\x00")


def der_seq(*parts):
    return _tlv(0x30, b"".join(parts))


def der_set(*parts):
    return _tlv(0x31, b"".join(parts))


def der_utf8(s):
    return _tlv(0x0C, s.encode("utf-8"))


def der_octets(b):
    return _tlv(0x04, b)


def der_bitstring(data, unused=0):
    return _tlv(0x03, bytes([unused]) + data)


def _b128(n):
    if n == 0:
        return b"\x00"
    out = bytearray()
    while n:
        out.append(n & 0x7F)
        n >>= 7
    out = bytes(reversed(out))
    return bytes(b | 0x80 for b in out[:-1]) + out[-1:]


def der_oid(*arcs):
    body = bytes([40 * arcs[0] + arcs[1]])
    for a in arcs[2:]:
        body += _b128(a)
    return _tlv(0x06, body)


def der_utctime(t):
    return _tlv(0x17, time.strftime("%y%m%d%H%M%SZ",
                                   time.gmtime(t)).encode("ascii"))


# ---------------- minimal DER reader (for reloading our own keys) ----------------

class _Reader:
    def __init__(self, data):
        self.d = data
        self.i = 0

    def _read_len(self):
        b0 = self.d[self.i]
        self.i += 1
        if b0 < 128:
            return b0
        n = b0 & 0x7F
        v = int.from_bytes(self.d[self.i:self.i + n], "big")
        self.i += n
        return v

    def tlv(self):
        tag = self.d[self.i]
        self.i += 1
        ln = self._read_len()
        v = self.d[self.i:self.i + ln]
        self.i += ln
        return tag, v

    def seq(self):
        tag, v = self.tlv()
        assert tag == 0x30, "expected SEQUENCE"
        return _Reader(v)

    def integer(self):
        tag, v = self.tlv()
        assert tag == 0x02, "expected INTEGER"
        return int.from_bytes(v, "big", signed=False)


def parse_rsa_private_pem(pem_text):
    """Parse a PKCS#1 PEM we generated -> key dict."""
    b64 = "".join(l.strip() for l in pem_text.splitlines()
                  if l.strip() and "-----" not in l)
    r = _Reader(base64.b64decode(b64)).seq()
    r.integer()  # version
    n, e, d = r.integer(), r.integer(), r.integer()
    p, q = r.integer(), r.integer()
    return {"n": n, "e": e, "d": d, "p": p, "q": q}


def pem_encode(kind, der):
    b64 = base64.encodebytes(der).decode("ascii")
    return "-----BEGIN %s-----\n%s-----END %s-----\n" % (kind, b64, kind)


def pem_decode(pem_text):
    b64 = "".join(l.strip() for l in pem_text.splitlines()
                  if l.strip() and "-----" not in l)
    return base64.b64decode(b64)


# ---------------- RSA ----------------

def _small_primes(limit=2000):
    sieve = bytearray(b"\x01") * (limit + 1)
    sieve[0:2] = b"\x00\x00"
    for i in range(2, int(limit ** 0.5) + 1):
        if sieve[i]:
            sieve[i * i:limit + 1:i] = b"\x00" * ((limit - i * i) // i + 1)
    return [i for i, v in enumerate(sieve) if v]


_SMALL_PRIMES = _small_primes()


def _is_prime(n, rounds=16):
    if n < 2:
        return False
    for p in _SMALL_PRIMES:
        if n % p == 0:
            return n == p
    r, d = 0, n - 1
    while d % 2 == 0:
        r += 1
        d //= 2
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = (x * x) % n
            if x == n - 1:
                break
        else:
            return False
    return True


def _gen_prime(bits):
    while True:
        c = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _is_prime(c):
            return c


def gen_rsa(bits=2048):
    """Generate RSA key. Slow in pure Python (seconds) — call rarely."""
    e = 65537
    while True:
        p = _gen_prime(bits // 2)
        q = _gen_prime(bits // 2)
        if p == q:
            continue
        n = p * q
        if n.bit_length() != bits:
            continue
        phi = (p - 1) * (q - 1)
        if math.gcd(e, phi) != 1:
            continue
        d = pow(e, -1, phi)
        return {"n": n, "e": e, "d": d, "p": p, "q": q}


_SHA256_DIGESTINFO_PREFIX = bytes.fromhex(
    "3031300d060960864801650304020105000420")


def rsa_sign(key, data):
    """PKCS#1 v1.5 sign with SHA-256. Returns signature bytes."""
    t = _SHA256_DIGESTINFO_PREFIX + hashlib.sha256(data).digest()
    k = (key["n"].bit_length() + 7) // 8
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    s = pow(int.from_bytes(em, "big"), key["d"], key["n"])
    return s.to_bytes(k, "big")


def rsa_private_pem(key):
    dp = key["d"] % (key["p"] - 1)
    dq = key["d"] % (key["q"] - 1)
    qinv = pow(key["q"], -1, key["p"])
    der = der_seq(der_int(0), der_int(key["n"]), der_int(key["e"]),
                  der_int(key["d"]), der_int(key["p"]), der_int(key["q"]),
                  der_int(dp), der_int(dq), der_int(qinv))
    return pem_encode("RSA PRIVATE KEY", der)


# ---------------- X.509 ----------------

_OID_RSA = (1, 2, 840, 113549, 1, 1, 1)
_OID_SHA256_RSA = (1, 2, 840, 113549, 1, 1, 11)
_OID_CN = (2, 5, 4, 3)
_OID_BASIC = (2, 5, 29, 19)
_OID_KEYUSE = (2, 5, 29, 15)
_OID_SAN = (2, 5, 29, 17)
_OID_EKU = (2, 5, 29, 37)
_OID_SERVERAUTH = (1, 3, 6, 1, 5, 5, 7, 3, 1)


def _name(cn):
    return der_seq(der_set(der_seq(der_oid(*_OID_CN), der_utf8(cn))))


def _sig_alg():
    return der_seq(der_oid(*_OID_SHA256_RSA), der_null())


def _pubkey(key):
    rsa = der_seq(der_int(key["n"]), der_int(key["e"]))
    return der_seq(der_seq(der_oid(*_OID_RSA), der_null()),
                   der_bitstring(rsa))


def _ext(oid_arcs, critical, value_der):
    parts = [der_oid(*oid_arcs)]
    if critical:
        parts.append(der_bool(True))
    parts.append(der_octets(value_der))
    return der_seq(*parts)


def _extensions(exts):
    return _tlv(0xA3, der_seq(*exts))  # [3] EXPLICIT


def _basic_constraints(ca):
    # SEQUENCE { cA BOOLEAN } ; absent/empty = FALSE
    inner = der_seq(der_bool(True)) if ca else der_seq()
    return _ext(_OID_BASIC, True, inner)


def _key_usage(bits):
    """bits: list of bit numbers (0=MSB). Returns keyUsage extension DER."""
    if not bits:
        body = b""
        unused = 0
    else:
        hi = max(bits)
        nbytes = hi // 8 + 1
        raw = bytearray(nbytes)
        for b in bits:
            raw[b // 8] |= 0x80 >> (b % 8)
        unused = nbytes * 8 - hi - 1
        body = bytes(raw)
    return _ext(_OID_KEYUSE, True, der_bitstring(body, unused))


def make_ca(cn, key, days=825):
    """Self-signed CA certificate. Returns DER bytes."""
    now = time.time()
    tbs = der_seq(
        _tlv(0xA0, der_int(2)),  # [0] EXPLICIT version 3
        der_int(secrets.randbits(63) | 1),
        _sig_alg(),
        _name(cn),
        der_seq(der_utctime(now - 86400),
                der_utctime(now + days * 86400)),
        _name(cn),
        _pubkey(key),
        _extensions([
            _basic_constraints(True),
            _key_usage([5, 6]),  # keyCertSign, cRLSign
        ]),
    )
    return der_seq(tbs, _sig_alg(), der_bitstring(rsa_sign(key, tbs)))


def make_host_cert(host, host_key, ca_cn, ca_key, days=825):
    """Server certificate for `host`, signed by the CA. Returns DER bytes."""
    now = time.time()
    san = der_seq(_tlv(0x82, host.encode("ascii", "replace")))  # dNSName
    eku = der_seq(der_oid(*_OID_SERVERAUTH))
    tbs = der_seq(
        _tlv(0xA0, der_int(2)),
        der_int(secrets.randbits(63) | 1),
        _sig_alg(),
        _name(ca_cn),
        der_seq(der_utctime(now - 86400),
                der_utctime(now + days * 86400)),
        _name(host),
        _pubkey(host_key),
        _extensions([
            _basic_constraints(False),
            _key_usage([0, 2]),  # digitalSignature, keyEncipherment
            _ext(_OID_EKU, False, eku),
            _ext(_OID_SAN, False, san),
        ]),
    )
    return der_seq(tbs, _sig_alg(), der_bitstring(rsa_sign(ca_key, tbs)))


def cert_fingerprint_sha256(cert_der):
    h = hashlib.sha256(cert_der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))
