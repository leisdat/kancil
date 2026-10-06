"""TLS client eksperimental dengan JA3-spoofing ala Chrome-Android (FASE C).

Modul OPSIONAL dan standalone (tidak di-wire ke engines.py; integrasi
diputuskan terpisah). Idenya dipinjam dari teknik AnCry1596/httpx-tls:
bukan memakai ClientHello bawaan library TLS, melainkan MERAKIT byte
ClientHello sendiri sesuai vektor fingerprint (urutan cipher suites,
urutan extensions, elliptic curves/groups, signature algorithms, ALPN)
sehingga hash JA3-nya sama persis dengan browser yang ditiru.

Arsitektur
---------
1. ``FINGERPRINTS`` — database vektor JA3 (murni data, tanpa dependensi).
2. ``build_client_hello()`` — merakit byte ClientHello (RFC 8446 §4.1.2)
   hanya dengan stdlib (``struct``-style manual packing). Hasilnya bisa
   di-parse ulang dan dihitung JA3-nya tanpa dependensi apa pun.
3. ``StealthTLSClient.get()`` — handshake betulan memakai ``tlslite-ng``
   (dependensi OPSIONAL, murni Python). Triknya: subclass
   ``tlslite.tlsconnection.TLSConnection`` yang menimpa
   ``_clientSendClientHello`` agar mengirim ClientHello rakitan sendiri
   (di-parse ke objek tlslite-ng supaya transcript hash tetap konsisten)
   sambil menempelkan private key ephemeral ke tiap KeyShareEntry agar
   turunan kunci sesi tetap benar.
4. Verifikasi sertifikat WAJIB — ini lubang yang disengaja TIDAK ditambal
   oleh httpx-tls ("does no certificate verification at all"). Di sini
   ``get()`` menolak melanjutkan bila: rantai tidak bisa ditautkan ke CA
   di bundle tepercaya (sistem/certifi), ada signature rantai yang tidak
   valid, sertifikat kedaluwarsa, atau hostname tidak cocok (RFC 6125-ish).
   RSA diverifikasi dengan stdlib (``pow`` + PKCS#1 v1.5); ECDSA memakai
   primitif milik tlslite-ng sendiri.

Status dependensi
-----------------
``tlslite-ng`` (plus transitive dep-nya ``ecdsa``) adalah SATU-SATUNYA
dependensi opsional dan keduanya murni Python (terverifikasi: sdist dari
PyPI bisa di-build tanpa wheel biner). Tanpa dependensi itu modul ini
tetap bisa diimpor: builder ClientHello, parser, perhitungan JA3, dan
verifikasi rantai RSA tetap jalan; ``available()`` mengembalikan False
dan ``get()`` menolak dengan pesan yang jelas (bukan ImportError samar).

APA YANG TERVERIFIKASI (oleh unit test, tanpa network)
------------------------------------------------------
- Byte ClientHello yang dirakit: urutan cipher suites & extensions sama
  persis dengan vektor fingerprint (assert byte-level).
- String JA3 yang dihitung dari byte tersebut == vektor JA3 yang
  diharapkan (GREASE diabaikan, sesuai spec JA3).
- Round-trip: byte -> objek ``tlslite.ClientHello`` -> byte identik
  (syarat injeksi handshake).
- Injeksi: ``_clientSendClientHello`` yang ditimpa mengirim byte persis
  seperti rakitan (diuji via socketpair, tanpa network).
- Verifikasi rantai: RSA (CA + leaf buatan ``kancil.x509``) dan ECDSA
  (sertifikat buatan dengan lib ``ecdsa``); kasus negatif (hostname salah,
  kedaluwarsa, signature rusak, self-signed tak tepercaya) ditolak.

APA YANG **BELUM** TERVERIFIKASI
--------------------------------
- Handshake live ke server sungguhan. Egress VM ini lewat MITM proxy yang
  hanya paham HTTP(S), sehingga uji live TIDAK MUNGKIN dari sini.
- JANGAN klaim modul ini "bypass Cloudflare" / WAF / anti-bot. JA3
  hanyalah satu sinyal; server modern juga memeriksa HTTP/2 fingerprint,
  urutan header, TLS 1.3 extension contents, dsb.

KETERBATASAN YANG DIKETAHUI
---------------------------
- ``randomize_extensions`` default MATI: randomisasi merusak exact-match
  JA3 (Chrome 110+ di dunia nyata justru mengacak urutan extension per
  koneksi — vektor exact-match di sini menarget era pra-randomisasi /
  pola kanonis yang terdokumentasi).
- Bila server mengirim HelloRetryRequest, tlslite-ng membuat ulang
  key share sendiri (urutan extension lain tetap milik kita).
- Hanya HTTP/1.1; bila server menegosiasi ``h2`` via ALPN, ``get()``
  menolak dengan error yang jelas.
- Signature ``rsassa-pss`` (OID 1.2.840.113549.1.1.10) belum didukung ->
  rantai yang memakainya ditolak dengan pesan jelas (fail closed).
- GREASE tidak dikirim (agar JA3 exact-match); ini sendiri sedikit
  berbeda dari Chrome asli yang menyisipkan nilai GREASE acak.

Sumber vektor ``chrome_android``
-------------------------------
- String JA3: ``Chrome.ja3_versions['120-136']`` dari database
  AnCry1596/httpx-tls (database itu sendiri disadur dari perubahan
  open-source browser + database fingerprint curl_cffi). Vektor era
  Chrome 120-136 dipilih karena paling banyak terdokumentasi publik
  (pola cipher/extension-nya cocok dengan capture JA3 Chrome yang
  dipublikasikan, mis. vector Chrome 103 di issue gospider007/fp#5).
- Urutan signature_algorithms: urutan kanonis BoringSSL/Chromium
  (ecdsa_secp256r1_sha256, rsa_pss_rsae_sha256, rsa_pkcs1_sha256,
  ecdsa_secp384r1_sha384, ...).
- ALPN ``h2,http/1.1``: perilaku standar Chrome.
- Isi extension di luar cakupan JA3 (key_share, status_request, dst.)
  diisi best-effort meniru Chrome dan didokumentasikan di kode.
"""

import base64
import calendar
import hashlib
import hmac
import ipaddress
import os
import secrets
import socket
import time
from urllib.parse import urlsplit

__all__ = [
    "available",
    "FINGERPRINTS",
    "StealthTLSError",
    "StealthTLSUnavailable",
    "CertVerifyError",
    "FingerprintError",
    "BuiltHello",
    "build_client_hello",
    "parse_client_hello",
    "ja3_string",
    "ja3_hash",
    "verify_chain",
    "StealthTLSClient",
]


# ---------------------------------------------------------------------------
# Dependensi opsional: tlslite-ng (murni Python). Nama package PyPI-nya
# "tlslite-ng", nama modul import-nya "tlslite".
# ---------------------------------------------------------------------------
try:
    from tlslite.tlsconnection import TLSConnection as _TLSConnection
    from tlslite.messages import ClientHello as _TLSClientHello
    from tlslite.utils.codec import Parser as _TLSParser
    from tlslite.handshakesettings import HandshakeSettings as _TLSHandshakeSettings
    from tlslite.keyexchange import ECDHKeyExchange as _TLSECDHKeyExchange
    from tlslite.constants import ExtensionType as _TLSExtensionType
    from tlslite.x509 import X509 as _TLSX509
    from tlslite import errors as _tls_errors
    _TLS_AVAILABLE = True
    _TLS_IMPORT_ERROR = None
except ImportError as _e:  # pragma: no cover - cabang tanpa dep
    _TLSConnection = None
    _TLSClientHello = None
    _TLSParser = None
    _TLSHandshakeSettings = None
    _TLSECDHKeyExchange = None
    _TLSExtensionType = None
    _TLSX509 = None
    _tls_errors = None
    _TLS_AVAILABLE = False
    _TLS_IMPORT_ERROR = _e


def available():
    """True bila dependensi opsional ``tlslite-ng`` terpasang.

    Tanpa dependensi ini, builder ClientHello / parser / JA3 / verifikasi
    RSA tetap bisa dipakai; hanya handshake live (``get()``) yang butuh.
    """
    return _TLS_AVAILABLE


def _need_tls():
    if not _TLS_AVAILABLE:
        sebab = ""
        if _TLS_IMPORT_ERROR is not None:
            sebab = " (penyebab: %s)" % (_TLS_IMPORT_ERROR,)
        raise StealthTLSUnavailable(
            "tlslite-ng tidak terpasang%s. Modul stealth_tls tetap bisa "
            "diimpor dan dipakai untuk merakit/memeriksa ClientHello serta "
            "menghitung JA3, tetapi handshake TLS butuh dependensi opsional "
            "ini.\nPasang dengan: pip install tlslite-ng" % sebab
        )


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class StealthTLSError(Exception):
    """Error umum modul stealth_tls."""


class StealthTLSUnavailable(StealthTLSError):
    """Dependensi opsional (tlslite-ng) belum terpasang."""


class CertVerifyError(StealthTLSError):
    """Verifikasi rantai sertifikat gagal (fail closed)."""


class FingerprintError(StealthTLSError):
    """Nama fingerprint tidak dikenal / vektor tidak valid."""


# ---------------------------------------------------------------------------
# Database fingerprint. Satu-satunya sumber kebenaran untuk byte yang
# dirakit; tiap entri mendokumentasikan asal nilainya (lihat docstring
# modul). GREASE sengaja TIDAK ada di vektor agar JA3 exact-match.
# ---------------------------------------------------------------------------
def _cipher_names():
    # Dokumentasi saja (tidak dipakai kode): nama IANA tiap cipher suite.
    return {
        4865: "TLS_AES_128_GCM_SHA256",
        4866: "TLS_AES_256_GCM_SHA384",
        4867: "TLS_CHACHA20_POLY1305_SHA256",
        49195: "TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256",
        49199: "TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256",
        49196: "TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384",
        49200: "TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384",
        52393: "TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256",
        52392: "TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256",
        49171: "TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA",
        49172: "TLS_ECDHE_RSA_WITH_AES_128_CBC_SHA",
        156: "TLS_RSA_WITH_AES_128_GCM_SHA256",
        157: "TLS_RSA_WITH_AES_256_GCM_SHA384",
        47: "TLS_RSA_WITH_AES_128_CBC_SHA",
        53: "TLS_RSA_WITH_AES_256_CBC_SHA",
    }


def _ext_names():
    # Dokumentasi saja: nama extension tiap ID yang dipakai.
    return {
        0: "server_name (SNI)",
        5: "status_request (OCSP stapling)",
        10: "supported_groups",
        11: "ec_point_formats",
        13: "signature_algorithms",
        16: "application_layer_protocol_negotiation (ALPN)",
        18: "signed_certificate_timestamp (SCT)",
        23: "extended_master_secret",
        27: "compress_certificate",
        35: "session_ticket",
        43: "supported_versions",
        45: "psk_key_exchange_modes",
        51: "key_share",
        17513: "application_settings (ALPS)",
        65281: "renegotiation_info",
    }


FINGERPRINTS = {
    "chrome_android": {
        "label": "Chrome/Chromium Android era 120-136 (BoringSSL)",
        # Vektor kanonis dari AnCry1596/httpx-tls:
        # Chrome.ja3_versions['120-136']. Versi JA3 "771" = legacy_version
        # 0x0303 (TLS 1.2) sesuai RFC 8446 — semua ClientHello TLS 1.3
        # menulis 0x0303 di field ini.
        "ja3": (
            "771,"
            "4865-4866-4867-49195-49199-49196-49200-52393-52392-"
            "49171-49172-156-157-47-53,"
            "0-23-65281-10-11-35-16-5-13-18-51-45-43-27-17513,"
            "29-23-24,"
            "0"
        ),
        "ciphers": [4865, 4866, 4867, 49195, 49199, 49196, 49200, 52393,
                    52392, 49171, 49172, 156, 157, 47, 53],
        "extensions": [0, 23, 65281, 10, 11, 35, 16, 5, 13, 18, 51, 45,
                       43, 27, 17513],
        # 29 = x25519, 23 = secp256r1, 24 = secp384r1
        "groups": [29, 23, 24],
        "ec_point_formats": [0],  # 0 = uncompressed
        # Urutan kanonis BoringSSL/Chromium untuk signature_algorithms:
        # 0x0403 ecdsa_secp256r1_sha256, 0x0804 rsa_pss_rsae_sha256,
        # 0x0401 rsa_pkcs1_sha256, 0x0503 ecdsa_secp384r1_sha384,
        # 0x0805 rsa_pss_rsae_sha384, 0x0603 ecdsa_secp521r1_sha512,
        # 0x0806 rsa_pss_rsae_sha512, 0x0501 rsa_pkcs1_sha384,
        # 0x0601 rsa_pkcs1_sha512.
        "sigalgs": [0x0403, 0x0804, 0x0401, 0x0503, 0x0805, 0x0603,
                    0x0806, 0x0501, 0x0601],
        "alpn": [b"h2", b"http/1.1"],
        # Chrome mengirim 2 key share: x25519 dulu, lalu secp256r1.
        "key_share_groups": [29, 23],
        # Isi best-effort di luar cakupan JA3 (terdokumentasi, bukan klaim):
        # - compress_certificate: [2] = brotli (Chrome memakai Brotli
        #   menurut JEP OpenJDK 8281710; urutan pasti tidak dipublikasikan
        #   luas, jadi ini best-effort).
        # - application_settings (ALPS, 17513): Chrome mengirim kosong di
        #   ClientHello awal.
        # - status_request: OCSP minimal (type=1, list kosong).
        "compress_certificate_algs": [2],
        "sumber": [
            "AnCry1596/httpx-tls database.py :: Chrome.ja3_versions['120-136']",
            "Urutan sigalgs: urutan kanonis BoringSSL (Chromium)",
            "ALPN h2,http/1.1: perilaku standar Chrome",
        ],
    },
}


# ---------------------------------------------------------------------------
# Perakit byte ClientHello (murni stdlib — tanpa dependensi apa pun).
# Struktur mengikuti RFC 8446 §4.1.2.
# ---------------------------------------------------------------------------
def _u8(n):
    return bytes((n & 0xFF,))


def _u16(n):
    return n.to_bytes(2, "big")


def _lp8(data):
    data = bytes(data)
    return _u8(len(data)) + data


def _lp16(data):
    data = bytes(data)
    return _u16(len(data)) + data


def _ext(etype, data):
    """Satu extension: type(2) + length(2) + data."""
    return _u16(etype) + _lp16(data)


def _ext_server_name(host):
    # host sudah dalam bentuk IDNA/ascii.
    h = host.encode("ascii")
    entry = b"\x00" + _u16(len(h)) + h  # name_type=0 (host_name)
    return _ext(0, _lp16(entry))


def _ext_alpn(protos):
    inner = b"".join(_lp8(p) for p in protos)
    return _ext(16, _lp16(inner))


def _ext_supported_groups(groups):
    return _ext(10, _lp16(b"".join(_u16(g) for g in groups)))


def _ext_ec_point_formats(formats):
    return _ext(11, _lp8(bytes(formats)))


def _ext_signature_algorithms(sigalgs):
    return _ext(13, _lp16(b"".join(_u16(s) for s in sigalgs)))


def _ext_status_request():
    # OCSP minimal: status_type=1 (ocsp), responder_id_list kosong,
    # request_extensions kosong — seperti yang dikirim Chrome.
    return _ext(5, b"\x01\x00\x00\x00\x00")


def _ext_renegotiation_info():
    # renegotiated_connection kosong -> satu byte panjang 0.
    return _ext(65281, b"\x00")


def _ext_key_share(shares):
    # shares: list of (group_id, public_bytes)
    inner = b"".join(_u16(g) + _lp16(pub) for g, pub in shares)
    return _ext(51, _lp16(inner))


def _ext_psk_key_exchange_modes():
    return _ext(45, _lp8(b"\x01"))  # psk_dhe_ke


def _ext_supported_versions():
    return _ext(43, _lp8(_u16(0x0304)))  # TLS 1.3


def _ext_compress_certificate(algs):
    return _ext(27, _lp8(b"".join(_u16(a) for a in algs)))


# Panjang public key yang benar per grup (agar parseable walau acak).
_KEY_SHARE_PUB_LEN = {29: 32, 23: 65, 24: 97}  # x25519, secp256r1, secp384r1


def _random_key_material(group):
    """Key share palsu (byte acak, panjang benar).

    Cukup untuk uji byte-level/JA3 dan inspeksi. TIDAK untuk handshake
    sungguhan (private=None) — handshake memakai key material asli dari
    tlslite-ng.
    """
    ln = _KEY_SHARE_PUB_LEN.get(group, 32)
    pub = secrets.token_bytes(ln)
    if group in (23, 24):
        pub = b"\x04" + pub[1:]  # format uncompressed point
    return pub, None


def _tlslite_key_material(group):
    """Key share asli memakai primitif kripto tlslite-ng."""
    _need_tls()
    kex = _TLSECDHKeyExchange(group, (3, 4))
    priv = kex.get_random_private_key()
    pub = bytes(kex.calc_public_value(priv))
    return pub, priv


def _default_key_material(group):
    if _TLS_AVAILABLE:
        return _tlslite_key_material(group)
    return _random_key_material(group)


class BuiltHello:
    """Hasil perakitan ClientHello.

    - ``body``: byte handshake message ClientHello (diawali legacy_version
      0x0303), siap dihitung JA3-nya atau dibungkus header handshake.
    - ``privates``: dict {group_id: private} untuk key share — dipakai
      kelas koneksi injeksi agar turunan kunci sesi benar. Kosong/None
      bila key material acak (tidak untuk handshake).
    - ``fingerprint``: nama vektor yang dipakai.
    - ``server_name``: SNI yang dipakai.
    """

    __slots__ = ("body", "privates", "fingerprint", "server_name")

    def __init__(self, body, privates, fingerprint, server_name):
        self.body = bytes(body)
        self.privates = dict(privates)
        self.fingerprint = fingerprint
        self.server_name = server_name

    def handshake_message(self):
        """Body dibungkus header handshake TLS (type=1 client_hello)."""
        return b"\x01" + len(self.body).to_bytes(3, "big") + self.body


def build_client_hello(fingerprint="chrome_android", server_name="example.com",
                       randomize_extensions=False, key_material=None):
    """Rakit byte ClientHello sesuai vektor fingerprint.

    :param fingerprint: kunci di ``FINGERPRINTS``.
    :param server_name: hostname untuk SNI (akan di-IDNA-kan).
    :param randomize_extensions: bila True, urutan extension diacak
        (default False — randomisasi merusak exact-match JA3).
    :param key_material: callable ``(group_id) -> (public_bytes,
        private_or_None)``. Default: pakai tlslite-ng bila ada, else
        byte acak (tidak untuk handshake).
    :returns: ``BuiltHello``.
    """
    try:
        spec = FINGERPRINTS[fingerprint]
    except KeyError:
        raise FingerprintError(
            "fingerprint tidak dikenal: %r (pilihan: %s)"
            % (fingerprint, ", ".join(sorted(FINGERPRINTS))))
    if key_material is None:
        key_material = _default_key_material
    try:
        sni = server_name.encode("idna").decode("ascii")
    except Exception:
        sni = server_name

    ciphers = list(spec["ciphers"])
    ext_order = list(spec["extensions"])
    if randomize_extensions:
        # Acak di tempat dengan RNG sistem; isi extension tetap sama.
        import random as _random
        _random.SystemRandom().shuffle(ext_order)

    # Bangun key share dulu (butuh sebelum extension 51 dirakit).
    shares = []
    privates = {}
    for group in spec["key_share_groups"]:
        pub, priv = key_material(group)
        shares.append((group, bytes(pub)))
        if priv is not None:
            privates[group] = priv

    builders = {
        0: lambda: _ext_server_name(sni),
        5: _ext_status_request,
        10: lambda: _ext_supported_groups(spec["groups"]),
        11: lambda: _ext_ec_point_formats(spec["ec_point_formats"]),
        13: lambda: _ext_signature_algorithms(spec["sigalgs"]),
        16: lambda: _ext_alpn(spec["alpn"]),
        18: lambda: _ext(18, b""),
        23: lambda: _ext(23, b""),
        27: lambda: _ext_compress_certificate(
            spec["compress_certificate_algs"]),
        35: lambda: _ext(35, b""),
        43: _ext_supported_versions,
        45: _ext_psk_key_exchange_modes,
        51: lambda: _ext_key_share(shares),
        17513: lambda: _ext(17513, b""),
        65281: _ext_renegotiation_info,
    }
    exts = []
    for etype in ext_order:
        try:
            build = builders[etype]
        except KeyError:
            raise FingerprintError(
                "extension %r di vektor %r belum ada builder-nya"
                % (etype, fingerprint))
        exts.append(build())

    body = (
        _u16(0x0303)  # legacy_version: TLS 1.2 (wajib RFC 8446)
        + secrets.token_bytes(32)  # random
        + _lp8(secrets.token_bytes(32))  # legacy_session_id (middlebox compat)
        + _lp16(b"".join(_u16(c) for c in ciphers))
        + _lp8(b"\x00")  # legacy_compression_methods = null
        + _lp16(b"".join(exts))
    )
    return BuiltHello(body, privates, fingerprint, sni)


# ---------------------------------------------------------------------------
# Parser ClientHello + perhitungan JA3 (murni stdlib).
# ---------------------------------------------------------------------------
# Nilai GREASE (RFC 8701) — diabaikan JA3 agar satu hash untuk semua
# variasi acak Chrome.
_GREASE = frozenset({
    0x0A0A, 0x1A1A, 0x2A2A, 0x3A3A, 0x4A4A, 0x5A5A, 0x6A6A, 0x7A7A,
    0x8A8A, 0x9A9A, 0xAAAA, 0xBABA, 0xCACA, 0xDADA, 0xEAEA, 0xFAFA,
})


def parse_client_hello(body):
    """Parse byte body ClientHello -> dict.

    Mengembalikan ``version`` (int, mis. 771), ``ciphers`` (list int,
    sesuai urutan di kawat), ``extensions`` (list int sesuai urutan),
    ``groups`` (dari ext 10), ``ec_point_formats`` (dari ext 11).
    Urutan dipertahankan — inilah yang diuji byte-level.
    """
    body = bytes(body)
    pos = 0

    def need(n):
        if pos + n > len(body):
            raise StealthTLSError("ClientHello terpotong saat parsing")

    need(2)
    version = int.from_bytes(body[pos:pos + 2], "big")
    pos += 2
    need(32)
    pos += 32  # random
    need(1)
    ln = body[pos]
    pos += 1
    need(ln)
    pos += ln  # legacy_session_id
    need(2)
    ln = int.from_bytes(body[pos:pos + 2], "big")
    pos += 2
    need(ln)
    ciphers = [int.from_bytes(body[i:i + 2], "big")
               for i in range(pos, pos + ln, 2)]
    pos += ln
    need(1)
    ln = body[pos]
    pos += 1
    need(ln)
    pos += ln  # legacy_compression_methods
    need(2)
    ln = int.from_bytes(body[pos:pos + 2], "big")
    pos += 2
    need(ln)
    end = pos + ln

    extensions = []
    groups = []
    ec_point_formats = []
    while pos < end:
        need(4)
        etype = int.from_bytes(body[pos:pos + 2], "big")
        elen = int.from_bytes(body[pos + 2:pos + 4], "big")
        pos += 4
        need(elen)
        edata = body[pos:pos + elen]
        pos += elen
        extensions.append(etype)
        if etype == 10 and len(edata) >= 2:  # supported_groups
            glen = int.from_bytes(edata[0:2], "big")
            groups = [int.from_bytes(edata[i:i + 2], "big")
                      for i in range(2, min(2 + glen, len(edata)), 2)]
        elif etype == 11 and len(edata) >= 1:  # ec_point_formats
            flen = edata[0]
            ec_point_formats = list(edata[1:1 + flen])
    return {
        "version": version,
        "ciphers": ciphers,
        "extensions": extensions,
        "groups": groups,
        "ec_point_formats": ec_point_formats,
    }


def _tanpa_grease(vals):
    return [v for v in vals if v not in _GREASE]


def ja3_string(body):
    """Hitung string JA3 dari byte body ClientHello.

    Format: ``TLSVersion,Ciphers,Extensions,EllipticCurves,ECPointFormats``
    dengan nilai desimal dipisah "-". GREASE diabaikan (spec JA3).
    """
    p = parse_client_hello(body)
    return "%d,%s,%s,%s,%s" % (
        p["version"],
        "-".join(str(v) for v in _tanpa_grease(p["ciphers"])),
        "-".join(str(v) for v in _tanpa_grease(p["extensions"])),
        "-".join(str(v) for v in _tanpa_grease(p["groups"])),
        "-".join(str(v) for v in p["ec_point_formats"]),
    )


def ja3_hash(ja3_str):
    """Hash MD5 dari string JA3 (fingerprint 32 hex char)."""
    return hashlib.md5(ja3_str.encode("ascii")).hexdigest()


# ---------------------------------------------------------------------------
# Injeksi ClientHello ke handshake tlslite-ng.
#
# Teknik: tlslite-ng vanilla membangun ClientHello dengan urutan extension
# yang fixed di kode (tidak ada hook untuk mengaturnya — kemampuan itu
# hanya ada di fork httpx-tls). Karena ``_sendMsg`` meng-update transcript
# hash dari ``msg.write()``, cukup parse byte rakitan kita menjadi objek
# ``ClientHello`` tlslite-ng lalu kirim lewat jalur normal: byte di kawat
# == byte rakitan, dan transcript tetap konsisten.
#
# Satu-satunya state yang harus ditempel manual: private key ephemeral
# untuk tiap KeyShareEntry (hasil parse tidak punya private key), karena
# ``_clientTLS13Handshake`` menghitung shared secret dari
# ``cl_kex.private``.
# ---------------------------------------------------------------------------
if _TLS_AVAILABLE:  # pragma: no cover - butuh tlslite-ng

    class _StealthTLSConnection(_TLSConnection):
        """TLSConnection yang mengirim ClientHello rakitan sendiri."""

        def __init__(self, sock, _ch_body, _ch_privates):
            super().__init__(sock)
            self._stealth_ch_body = bytes(_ch_body)
            self._stealth_ch_privates = dict(_ch_privates)

        def _clientSendClientHello(self, settings, session, srpUsername,
                                   srpParams, certParams, anonParams,
                                   serverName, nextProtos, reqTack, alpn):
            body = self._stealth_ch_body
            # Parser diposisikan seperti di jalur receive _getMsg: 1 byte
            # tipe handshake sudah dikonsumsi, parse() membaca 3 byte
            # panjang lalu body.
            msg = _TLSClientHello()
            msg.parse(_TLSParser(bytearray(
                len(body).to_bytes(3, "big") + body)))
            ks = msg.getExtension(_TLSExtensionType.key_share)
            if ks is not None:
                for entry in ks.client_shares:
                    priv = self._stealth_ch_privates.get(entry.group)
                    if priv is None:
                        raise StealthTLSError(
                            "tidak ada private key untuk key share grup "
                            "%r — handshake tidak bisa dilanjutkan" % (entry.group,))
                    entry.private = priv
            for result in self._sendMsg(msg):
                yield result
            # Kontrak generator ini: yield terakhir = objek ClientHello
            # (dipakai _clientGetServerHello & _clientTLS13Handshake).
            yield msg

else:  # pragma: no cover - cabang tanpa dep

    class _StealthTLSConnection:  # type: ignore
        def __init__(self, *a, **k):
            _need_tls()


# ---------------------------------------------------------------------------
# Verifikasi rantai sertifikat X.509 (murni stdlib, kecuali ECDSA yang
# memakai primitif tlslite-ng).
#
# Cakupan yang disengaja minimal tapi fail-closed:
#  - tiap sertifikat: masa berlaku, signature oleh penandatangan
#    (RSA: pow()+PKCS#1 v1.5 via stdlib; ECDSA: via tlslite-ng),
#  - rantai ditautkan issuer->subject sampai anchor di trust store
#    (CA bundle sistem / certifi / path eksplisit),
#  - hostname leaf dicocokkan ke SAN dNSName / iPAddress, fallback CN
#    (wildcard hanya label paling kiri, satu level — "RFC 6125-ish").
# Yang TIDAK diperiksa (didokumentasikan): revocation (OCSP/CRL),
#   name constraints, policy, EKU.
# ---------------------------------------------------------------------------
_OID_RSA = (1, 2, 840, 113549, 1, 1, 1)
_OID_CN = (2, 5, 4, 3)
_OID_SAN = (2, 5, 29, 17)
_OID_SHA1_RSA = (1, 2, 840, 113549, 1, 1, 5)
_OID_SHA256_RSA = (1, 2, 840, 113549, 1, 1, 11)
_OID_SHA384_RSA = (1, 2, 840, 113549, 1, 1, 12)
_OID_SHA512_RSA = (1, 2, 840, 113549, 1, 1, 13)
_OID_ECDSA_SHA256 = (1, 2, 840, 10045, 4, 3, 2)
_OID_ECDSA_SHA384 = (1, 2, 840, 10045, 4, 3, 3)
_OID_ECDSA_SHA512 = (1, 2, 840, 10045, 4, 3, 4)

# OID signature -> (nama hash hashlib, tipe kunci)
_SIG_OIDS = {
    _OID_SHA1_RSA: ("sha1", "rsa"),
    _OID_SHA256_RSA: ("sha256", "rsa"),
    _OID_SHA384_RSA: ("sha384", "rsa"),
    _OID_SHA512_RSA: ("sha512", "rsa"),
    _OID_ECDSA_SHA256: ("sha256", "ecdsa"),
    _OID_ECDSA_SHA384: ("sha384", "ecdsa"),
    _OID_ECDSA_SHA512: ("sha512", "ecdsa"),
}

# Prefix DigestInfo PKCS#1 v1.5 per hash (untuk verifikasi RSA).
_DIGESTINFO_PREFIX = {
    "sha1": bytes.fromhex("3021300906052b0e03021a05000414"),
    "sha256": bytes.fromhex("3031300d060960864801650304020105000420"),
    "sha384": bytes.fromhex("3041300d060960864801650304020205000430"),
    "sha512": bytes.fromhex("3051300d060960864801650304020305000440"),
}


class _DERReader:
    """Reader TLV DER minimal (cukup untuk parsing sertifikat)."""

    def __init__(self, data):
        self.d = bytes(data)
        self.i = 0

    def _read_len(self):
        if self.i >= len(self.d):
            raise CertVerifyError("DER terpotong (panjang)")
        b0 = self.d[self.i]
        self.i += 1
        if b0 < 0x80:
            return b0
        n = b0 & 0x7F
        if n == 0 or n > 4 or self.i + n > len(self.d):
            raise CertVerifyError("panjang DER tidak didukung/terpotong")
        v = int.from_bytes(self.d[self.i:self.i + n], "big")
        self.i += n
        return v

    def tlv(self):
        """Baca satu TLV -> (tag, content, full_bytes)."""
        start = self.i
        if self.i >= len(self.d):
            raise CertVerifyError("DER terpotong (tag)")
        tag = self.d[self.i]
        self.i += 1
        ln = self._read_len()
        if self.i + ln > len(self.d):
            raise CertVerifyError("DER terpotong (isi)")
        content = self.d[self.i:self.i + ln]
        self.i += ln
        return tag, content, self.d[start:self.i]

    def sisa(self):
        return len(self.d) - self.i


def _parse_oid(content):
    """OID DER -> tuple int."""
    if not content:
        raise CertVerifyError("OID kosong")
    arcs = [content[0] // 40, content[0] % 40]
    val = 0
    for b in content[1:]:
        val = (val << 7) | (b & 0x7F)
        if not (b & 0x80):
            arcs.append(val)
            val = 0
    if val:
        raise CertVerifyError("OID terpotong")
    return tuple(arcs)


def _parse_waktu(tag, content):
    """UTCTime/GeneralizedTime -> epoch (float)."""
    try:
        s = content.decode("ascii")
    except Exception:
        raise CertVerifyError("waktu sertifikat bukan ASCII")
    try:
        if tag == 0x17:  # UTCTime YYMMDDHHMMSSZ
            yy = int(s[0:2])
            tahun = 2000 + yy if yy < 50 else 1900 + yy
            tup = (tahun, int(s[2:4]), int(s[4:6]), int(s[6:8]),
                   int(s[8:10]), int(s[10:12]))
        elif tag == 0x18:  # GeneralizedTime YYYYMMDDHHMMSSZ
            tup = (int(s[0:4]), int(s[4:6]), int(s[6:8]), int(s[8:10]),
                   int(s[10:12]), int(s[12:14]))
        else:
            raise CertVerifyError("tipe waktu tak dikenal: 0x%02x" % tag)
        return calendar.timegm(tup + (0, 0, 0))
    except (ValueError, IndexError):
        raise CertVerifyError("waktu sertifikat tidak valid: %r" % s)


def _baca_int(r):
    tag, content, _ = r.tlv()
    if tag != 0x02:
        raise CertVerifyError("INTEGER diharapkan, dapat 0x%02x" % tag)
    return int.from_bytes(content, "big", signed=False)


def _parse_san(content):
    """Isi OCTET SAN -> (daftar dNSName str, daftar IP bytes)."""
    dns, ips = [], []
    r = _DERReader(content)
    tag, seq_content, _ = r.tlv()
    if tag != 0x30:
        raise CertVerifyError("SAN bukan SEQUENCE")
    g = _DERReader(seq_content)
    while g.sisa():
        gtag, gcontent, _ = g.tlv()
        if gtag == 0x82:  # dNSName (IA5String)
            try:
                dns.append(gcontent.decode("ascii").lower())
            except Exception:
                raise CertVerifyError("dNSName bukan ASCII")
        elif gtag == 0x87:  # iPAddress
            if len(gcontent) not in (4, 16):
                raise CertVerifyError("iPAddress SAN panjang aneh")
            ips.append(bytes(gcontent))
        # tipe GeneralName lain diabaikan (bukan untuk hostname/IP)
    return dns, ips


def _cari_cn(name_content):
    """RDNSequence -> CN (str) atau None."""
    r = _DERReader(name_content)
    tag, seq_content, _ = r.tlv()
    if tag != 0x30:
        return None
    rdns = _DERReader(seq_content)
    while rdns.sisa():
        stag, set_content, _ = rdns.tlv()
        if stag != 0x31:
            continue
        atv = _DERReader(set_content)
        while atv.sisa():
            atag, atv_content, _ = atv.tlv()
            if atag != 0x30:
                continue
            a = _DERReader(atv_content)
            otag, oid_content, _ = a.tlv()
            if otag != 0x06 or _parse_oid(oid_content) != _OID_CN:
                continue
            vtag, vcontent, _ = a.tlv()
            try:
                # CN bisa UTF8String/PrintableString/BMPString/dsb.
                return vcontent.decode("utf-8", "replace")
            except Exception:
                return None
    return None


def _parse_cert(der):
    """Parse satu sertifikat DER -> dict info. Raise CertVerifyError."""
    der = bytes(der)
    r = _DERReader(der)
    tag, content, _ = r.tlv()
    if tag != 0x30 or r.sisa():
        raise CertVerifyError("bukan sertifikat DER (SEQUENCE)")
    t = _DERReader(content)
    # tbsCertificate (butuh full DER-nya untuk verifikasi signature)
    ttag, tbs_content, tbs_full = t.tlv()
    if ttag != 0x30:
        raise CertVerifyError("tbsCertificate bukan SEQUENCE")
    tb = _DERReader(tbs_content)
    # version [0] EXPLICIT opsional
    if tb.sisa() and tb.d[tb.i] == 0xA0:
        tb.tlv()
    _baca_int(tb)  # serialNumber
    tb.tlv()  # signature (algoritma; yang dipakai verifikasi = yg luar)
    _, issuer_content, issuer_full = tb.tlv()  # issuer Name
    # validity
    vtag, validity_content, _ = tb.tlv()
    if vtag != 0x30:
        raise CertVerifyError("validity bukan SEQUENCE")
    vr = _DERReader(validity_content)
    nbt, nbc, _ = vr.tlv()
    nat, nac, _ = vr.tlv()
    not_before = _parse_waktu(nbt, nbc)
    not_after = _parse_waktu(nat, nac)
    # subject
    _, subject_content, subject_full = tb.tlv()
    # subjectPublicKeyInfo
    stag, spki_content, _ = tb.tlv()
    if stag != 0x30:
        raise CertVerifyError("subjectPublicKeyInfo bukan SEQUENCE")
    sr = _DERReader(spki_content)
    _, alg_content, _ = sr.tlv()  # AlgorithmIdentifier
    ar = _DERReader(alg_content)
    _, oid_content, _ = ar.tlv()
    key_oid = _parse_oid(oid_content)
    _, bit_content, _ = sr.tlv()  # BIT STRING
    if not bit_content or bit_content[0] != 0:
        raise CertVerifyError("BIT STRING public key aneh")
    pubkey = bit_content[1:]
    rsa_n = rsa_e = None
    if key_oid == _OID_RSA:
        pr = _DERReader(pubkey)
        ptag, pcontent, _ = pr.tlv()
        if ptag != 0x30:
            raise CertVerifyError("RSAPublicKey bukan SEQUENCE")
        kr = _DERReader(pcontent)
        rsa_n, rsa_e = _baca_int(kr), _baca_int(kr)
    # lewati sisa tbs (uniqueID dsb.), cari extensions [3]
    san_dns, san_ip = [], []
    while tb.sisa():
        ctag, ccontent, _ = tb.tlv()
        if ctag != 0xA3:  # bukan [3] extensions -> lewati
            continue
        er = _DERReader(ccontent)
        etag, econtent, _ = er.tlv()  # SEQ of Extension
        if etag != 0x30:
            raise CertVerifyError("extensions bukan SEQUENCE")
        xr = _DERReader(econtent)
        while xr.sisa():
            _, xcontent, _ = xr.tlv()  # Extension SEQ
            x = _DERReader(xcontent)
            _, ocontent, _ = x.tlv()  # OID
            oid = _parse_oid(ocontent)
            if x.sisa() and x.d[x.i] == 0x01:
                x.tlv()  # critical BOOLEAN
            _, vcontent, _ = x.tlv()  # OCTET STRING value
            if oid == _OID_SAN:
                san_dns, san_ip = _parse_san(vcontent)
    # signatureAlgorithm (luar)
    _, sigalg_content, _ = t.tlv()
    sar = _DERReader(sigalg_content)
    _, said_content, _ = sar.tlv()
    sig_oid = _parse_oid(said_content)
    # signatureValue BIT STRING
    _, sig_content, _ = t.tlv()
    if not sig_content or sig_content[0] != 0:
        raise CertVerifyError("BIT STRING signature aneh")
    signature = sig_content[1:]
    if t.sisa() or r.sisa():
        raise CertVerifyError("trailing bytes setelah sertifikat")
    return {
        "der": der,
        "tbs": tbs_full,
        "sig_oid": sig_oid,
        "signature": signature,
        "issuer_der": issuer_full,
        "subject_der": subject_full,
        "not_before": not_before,
        "not_after": not_after,
        "san_dns": san_dns,
        "san_ip": san_ip,
        "cn": _cari_cn(subject_content),
        "key_oid": key_oid,
        "rsa_n": rsa_n,
        "rsa_e": rsa_e,
    }


def _rsa_verify(n, e, signature, tbs, hash_name):
    """Verifikasi PKCS#1 v1.5 (stdlib: pow + hashlib). -> bool."""
    prefix = _DIGESTINFO_PREFIX.get(hash_name)
    if prefix is None:
        return False
    digest = hashlib.new(hash_name, tbs).digest()
    t = prefix + digest
    k = (n.bit_length() + 7) // 8
    if len(signature) != k or k < len(t) + 11:
        return False
    m = pow(int.from_bytes(signature, "big"), e, n)
    em = m.to_bytes(k, "big")
    # EM = 0x00 || 0x01 || PS (0xFF...) || 0x00 || DigestInfo
    if em[0] != 0 or em[1] != 1:
        return False
    try:
        sep = em.index(b"\x00", 2)
    except ValueError:
        return False
    if sep < 10:  # PS minimal 8 byte 0xFF
        return False
    if any(b != 0xFF for b in em[2:sep]):
        return False
    return hmac.compare_digest(em[sep + 1:], t)


def _verifikasi_signature(cert, penandatangan):
    """Verifikasi signature ``cert`` dengan public key ``penandatangan``.

    Raise CertVerifyError bila algoritma tak didukung atau signature
    tidak valid (fail closed).
    """
    try:
        hash_name, tipe = _SIG_OIDS[cert["sig_oid"]]
    except KeyError:
        raise CertVerifyError(
            "algoritma signature tidak didukung: %s"
            % (".".join(str(a) for a in cert["sig_oid"]),))
    if tipe == "rsa":
        if penandatangan["rsa_n"] is None:
            raise CertVerifyError(
                "signature RSA tetapi kunci penandatangan bukan RSA")
        ok = _rsa_verify(penandatangan["rsa_n"], penandatangan["rsa_e"],
                         cert["signature"], cert["tbs"], hash_name)
        if not ok:
            raise CertVerifyError("signature RSA rantai tidak valid")
    elif tipe == "ecdsa":
        # Primitif kripto milik tlslite-ng (pure Python).
        _need_tls()
        try:
            x = _TLSX509()
            x.parseBinary(bytearray(penandatangan["der"]))
            digest = hashlib.new(hash_name, cert["tbs"]).digest()
            ok = x.publicKey.verify(bytearray(cert["signature"]),
                                    bytearray(digest))
        except CertVerifyError:
            raise
        except Exception as e:
            raise CertVerifyError("verifikasi ECDSA gagal: %s" % (e,))
        if not ok:
            raise CertVerifyError("signature ECDSA rantai tidak valid")
    else:  # pragma: no cover - tak terjangkau
        raise CertVerifyError("tipe signature tak dikenal")


def _baca_pem_ders(path):
    """Baca file -> list DER (mendukung multi-PEM dan DER mentah)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise CertVerifyError("tidak bisa baca %r: %s" % (path, e))
    ders = []
    if b"-----BEGIN" in data:
        for baris in data.split(b"-----END CERTIFICATE-----"):
            if b"-----BEGIN CERTIFICATE-----" not in baris:
                continue
            b64 = baris.split(b"-----BEGIN CERTIFICATE-----", 1)[1]
            b64 = b"".join(b64.split())
            try:
                ders.append(base64.b64decode(b64))
            except Exception:
                continue
    elif data[:2] == b"\x30\x82" or data[:1] == b"\x30":
        ders.append(data)
    return ders


# Kandidat CA bundle sistem (urutan: Debian/Ubuntu/Termux, RHEL, lain).
_SYSTEM_CA_FILES = [
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",
    "/etc/pki/tls/cacert.pem",
    "/usr/local/share/certs/ca-root-nss.crt",
]
_ANDROID_CA_DIR = "/system/etc/security/cacerts"


def _muat_ca_tepercaya(ca_bundle=None):
    """Muat CA tepercaya -> list info cert (hasil _parse_cert).

    Prioritas: path eksplisit ``ca_bundle`` > ``certifi`` (bila ada) >
    bundle sistem > direktori CA Android.
    """
    ders = []
    if ca_bundle:
        ders += _baca_pem_ders(ca_bundle)
    else:
        try:
            import certifi  # opsional, bukan dependensi wajib
            ders += _baca_pem_ders(certifi.where())
        except ImportError:
            pass
        for p in _SYSTEM_CA_FILES:
            if os.path.isfile(p):
                ders += _baca_pem_ders(p)
        prefix = os.environ.get("PREFIX")  # Termux: $PREFIX/etc/tls/cert.pem
        if prefix:
            tp = os.path.join(prefix, "etc", "tls", "cert.pem")
            if os.path.isfile(tp):
                ders += _baca_pem_ders(tp)
        if os.path.isdir(_ANDROID_CA_DIR):
            for nama in sorted(os.listdir(_ANDROID_CA_DIR)):
                fp = os.path.join(_ANDROID_CA_DIR, nama)
                if os.path.isfile(fp):
                    ders += _baca_pem_ders(fp)
    cas = []
    for d in ders:
        try:
            cas.append(_parse_cert(d))
        except CertVerifyError:
            continue  # CA yang tak terparse dilewati, bukan digagalkan
    if not cas:
        raise CertVerifyError(
            "tidak ada CA tepercaya yang bisa dimuat (cek ca_bundle / "
            "certifi / paket ca-certificates)")
    return cas


def _cocok_dns(host, pola):
    """Cocokkan hostname ke pola SAN/CN (RFC 6125-ish).

    Wildcard hanya untuk label paling kiri dan tepat satu level:
    ``*.example.com`` cocok dengan ``foo.example.com`` tapi TIDAK dengan
    ``a.b.example.com`` atau ``example.com``.
    """
    if pola.startswith("*."):
        akhir = pola[2:]
        if "." not in host or not akhir:
            return False
        depan, _, sisa = host.partition(".")
        return bool(depan) and sisa == akhir
    return host == pola


def _cek_hostname(hostname, leaf):
    """Raise CertVerifyError bila hostname tidak cocok dengan leaf."""
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None
    if ip is not None:
        want = ip.packed
        if any(raw == want for raw in leaf["san_ip"]):
            return True
        raise CertVerifyError(
            "IP %r tidak tercantum di SAN sertifikat" % (hostname,))
    try:
        host = hostname.encode("idna").decode("ascii")
    except Exception:
        host = hostname
    host = host.lower().rstrip(".")
    nama = list(leaf["san_dns"])
    if not nama and leaf["cn"]:
        nama = [leaf["cn"].lower()]
    for pola in nama:
        if _cocok_dns(host, pola.lower()):
            return True
    raise CertVerifyError(
        "hostname %r tidak cocok dengan sertifikat (nama di cert: %r)"
        % (hostname, nama))


def verify_chain(chain_ders, hostname, ca_bundle=None):
    """Verifikasi rantai sertifikat server. Return True bila valid.

    :param chain_ders: list byte DER, leaf dulu (dari
        ``conn.session.serverCertChain.x509List`` -> ``.bytes``).
    :param hostname: hostname yang diharapkan (untuk SNI/hostname check).
    :param ca_bundle: path file PEM CA tepercaya (opsional).
    :raises CertVerifyError: untuk SEMUA kegagalan (fail closed).
    """
    if not chain_ders:
        raise CertVerifyError("server tidak mengirim rantai sertifikat")
    certs = [_parse_cert(d) for d in chain_ders]
    sekarang = time.time()
    for c in certs:
        if not (c["not_before"] <= sekarang <= c["not_after"]):
            raise CertVerifyError(
                "sertifikat %r kedaluwarsa/belum berlaku"
                % (c["cn"] or c["san_dns"] or "?",))
    tepercaya = _muat_ca_tepercaya(ca_bundle)
    der_tepercaya = {c["der"] for c in tepercaya}
    pool = certs + tepercaya
    saat_ini = certs[0]
    # Tautkan issuer->subject sampai anchor tepercaya tercapai.
    for _ in range(len(pool) + 2):
        penandatangan = None
        for kand in pool:
            if kand["der"] == saat_ini["der"]:
                # Sertifikat yang sama: hanya boleh bila self-signed.
                if kand["subject_der"] != saat_ini["issuer_der"]:
                    continue
            if kand["subject_der"] == saat_ini["issuer_der"]:
                penandatangan = kand
                break
        if penandatangan is None:
            raise CertVerifyError(
                "rantai tidak lengkap: issuer tidak ditemukan / tidak "
                "tepercaya")
        _verifikasi_signature(saat_ini, penandatangan)
        if penandatangan["der"] in der_tepercaya:
            break  # anchor tepercaya tercapai
        if penandatangan["der"] == saat_ini["der"]:
            raise CertVerifyError(
                "sertifikat self-signed tidak ada di trust store")
        saat_ini = penandatangan
    else:
        raise CertVerifyError("rantai terlalu dalam (kemungkinan siklus)")
    _cek_hostname(hostname, certs[0])
    return True


# ---------------------------------------------------------------------------
# HTTP/1.1 minimal di atas koneksi TLS (cukup untuk GET sederhana).
# ---------------------------------------------------------------------------
def _dechunk(data):
    """Decode Transfer-Encoding: chunked -> bytes."""
    out = bytearray()
    pos = 0
    while True:
        eol = data.find(b"\r\n", pos)
        if eol < 0:
            raise StealthTLSError("chunked encoding rusak (tanpa CRLF)")
        try:
            ukuran = int(data[pos:eol].split(b";", 1)[0].strip(), 16)
        except ValueError:
            raise StealthTLSError("ukuran chunk tidak valid")
        pos = eol + 2
        if ukuran == 0:
            break
        if pos + ukuran > len(data):
            raise StealthTLSError("chunked encoding terpotong")
        out += data[pos:pos + ukuran]
        pos += ukuran
        if data[pos:pos + 2] != b"\r\n":
            raise StealthTLSError("chunked encoding rusak (CRLF akhir)")
        pos += 2
    return bytes(out)


def _parse_respons_http(data):
    """byte respons -> (status_code, headers_dict, body_bytes).

    headers_dict memakai kunci lower-case.
    """
    head, sep, sisa = data.partition(b"\r\n\r\n")
    if not sep:
        raise StealthTLSError("respons HTTP tak lengkap (header terpotong)")
    baris = head.split(b"\r\n")
    potong = baris[0].split(b" ", 2)
    try:
        status = int(potong[1])
    except (IndexError, ValueError):
        raise StealthTLSError("status line tak valid: %r" % (baris[0][:80],))
    headers = {}
    for b in baris[1:]:
        if b":" not in b:
            continue
        k, v = b.split(b":", 1)
        headers[k.strip().decode("latin-1").lower()] = v.strip().decode("latin-1")
    body = sisa
    if headers.get("transfer-encoding", "").lower() == "chunked":
        body = _dechunk(sisa)
    elif "content-length" in headers:
        try:
            body = sisa[:int(headers["content-length"])]
        except ValueError:
            pass
    return status, headers, body


class StealthTLSClient:
    """HTTPS client dengan ClientHello fingerprint Chrome-Android.

    Contoh::

        client = StealthTLSClient()  # butuh tlslite-ng untuk get()
        status, headers, body = client.get("https://example.com/")

    ``build_client_hello()`` / ``ja3_string()`` tetap bisa dipakai tanpa
    tlslite-ng (lihat ``available()``).
    """

    # User-Agent yang dipasangkan dengan fingerprint (Chrome Android era
    # vektor 120-136). Hanya untuk header HTTP, tidak memengaruhi JA3.
    DEFAULT_UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36")

    def __init__(self, fingerprint="chrome_android",
                 randomize_extensions=False, ca_bundle=None, user_agent=None):
        if fingerprint not in FINGERPRINTS:
            raise FingerprintError(
                "fingerprint tidak dikenal: %r (pilihan: %s)"
                % (fingerprint, ", ".join(sorted(FINGERPRINTS))))
        self._fingerprint = fingerprint
        self._randomize = bool(randomize_extensions)
        self._ca_bundle = ca_bundle
        self._ua = user_agent or self.DEFAULT_UA

    @staticmethod
    def available():
        """True bila handshake live didukung (tlslite-ng terpasang)."""
        return _TLS_AVAILABLE

    def get(self, url, timeout=10):
        """GET HTTPS -> ``(status_code, headers_dict, body_bytes)``.

        ClientHello yang dikirim dirakit byte-per-byte sesuai fingerprint
        (``randomize_extensions`` default False agar JA3 exact-match).
        Verifikasi sertifikat WAJIB dan selalu dijalankan; kegagalan =
        ``CertVerifyError`` (tidak pernah fail-open seperti httpx-tls).
        """
        _need_tls()
        parts = urlsplit(url)
        if parts.scheme.lower() != "https":
            raise StealthTLSError(
                "hanya mendukung skema https, bukan %r" % (parts.scheme,))
        host = parts.hostname or ""
        if not host:
            raise StealthTLSError("URL tidak punya hostname: %r" % (url,))
        port = parts.port or 443
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        try:
            sni = host.encode("idna").decode("ascii")
        except Exception:
            sni = host

        raw = socket.create_connection((sni, port), timeout=timeout)
        raw.settimeout(timeout)
        conn = None
        try:
            dirakit = build_client_hello(
                self._fingerprint, sni,
                randomize_extensions=self._randomize,
                key_material=_tlslite_key_material)
            conn = _StealthTLSConnection(
                raw, _ch_body=dirakit.body, _ch_privates=dirakit.privates)
            settings = _TLSHandshakeSettings()
            settings.minVersion = (3, 3)
            settings.maxVersion = (3, 4)
            for _ in conn.handshakeClientCert(serverName=sni,
                                              settings=settings):
                pass
            # ALPN: modul ini baru bisa HTTP/1.1.
            app_proto = conn.session.appProto
            if app_proto == b"h2":
                raise StealthTLSError(
                    "server menegosiasikan HTTP/2 (h2) via ALPN; modul "
                    "eksperimental ini baru mendukung HTTP/1.1")
            # --- Verifikasi sertifikat: WAJIB, tidak bisa dimatikan. ---
            rantai = conn.session.serverCertChain
            if rantai is None or rantai.getNumCerts() == 0:
                raise CertVerifyError(
                    "server tidak mengirim rantai sertifikat")
            ders = [bytes(c.bytes) for c in rantai.x509List]
            verify_chain(ders, host, self._ca_bundle)
            # --- HTTP/1.1 GET sederhana. ---
            req = ("GET %s HTTP/1.1\r\n"
                   "Host: %s\r\n"
                   "User-Agent: %s\r\n"
                   "Accept: */*\r\n"
                   "Accept-Encoding: identity\r\n"
                   "Connection: close\r\n"
                   "\r\n" % (path, sni, self._ua))
            conn.send(req.encode("latin-1"))
            data = bytearray()
            while True:
                try:
                    potong = conn.recv(16384)
                except socket.timeout:
                    break  # server diam; pakai data yang sudah ada
                except Exception as e:
                    # Server menutup tanpa close_notify (umum) -> selesai.
                    if _tls_errors is not None and isinstance(
                            e, _tls_errors.TLSAbruptCloseError):
                        break
                    raise
                if not potong:
                    break
                data += potong
                if len(data) > 32 * 1024 * 1024:
                    raise StealthTLSError(
                        "respons >32MB, dibatalkan (batas modul eksperimental)")
            return _parse_respons_http(bytes(data))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            try:
                raw.close()
            except Exception:
                pass
