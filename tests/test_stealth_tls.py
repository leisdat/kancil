"""Tests untuk kancil/stealth_tls.py (FASE C, eksperimental).

Strategi:
- Bagian murni (database fingerprint, builder ClientHello, parser, JA3,
  verifikasi rantai RSA) diuji TANPA butuh tlslite-ng.
- Bagian yang butuh tlslite-ng (round-trip parse/write, injeksi
  ClientHello, verifikasi ECDSA) di-skip bila dependensi tidak ada
  (bukan fail).
- TIDAK ada handshake live / network di unit test ini: injeksi diuji via
  socketpair, sertifikat diuji dengan CA buatan sendiri.
"""
import hashlib
import os
import tempfile
import unittest

from kancil import stealth_tls as st
from kancil.stealth_tls import (
    StealthTLSClient,
    StealthTLSError,
    StealthTLSUnavailable,
    CertVerifyError,
    FingerprintError,
    build_client_hello,
    parse_client_hello,
    ja3_string,
    ja3_hash,
    verify_chain,
    FINGERPRINTS,
)

# Vektor ground truth untuk chrome_android (dari FINGERPRINTS; string ini
# disalin eksplisit agar test gagal bila DB berubah diam-diam).
CHROME_ANDROID_JA3 = (
    "771,"
    "4865-4866-4867-49195-49199-49196-49200-52393-52392-"
    "49171-49172-156-157-47-53,"
    "0-23-65281-10-11-35-16-5-13-18-51-45-43-27-17513,"
    "29-23-24,"
    "0"
)
CHROME_ANDROID_CIPHERS = [4865, 4866, 4867, 49195, 49199, 49196, 49200,
                          52393, 52392, 49171, 49172, 156, 157, 47, 53]
CHROME_ANDROID_EXTS = [0, 23, 65281, 10, 11, 35, 16, 5, 13, 18, 51, 45,
                       43, 27, 17513]


def butuh_tlslite(test):
    return unittest.skipUnless(
        st.available(), "tlslite-ng tidak terpasang")(test)


class DatabaseFingerprintTest(unittest.TestCase):
    """Integritas database fingerprint (murni, tanpa dependensi)."""

    def test_chrome_android_ada(self):
        self.assertIn("chrome_android", FINGERPRINTS)

    def test_vektor_ja3_sesuai_ground_truth(self):
        self.assertEqual(FINGERPRINTS["chrome_android"]["ja3"],
                         CHROME_ANDROID_JA3)

    def test_komponen_konsisten_dengan_string_ja3(self):
        fp = FINGERPRINTS["chrome_android"]
        versi, ciphers, exts, groups, ecpf = fp["ja3"].split(",")
        self.assertEqual(versi, "771")
        self.assertEqual([int(x) for x in ciphers.split("-")], fp["ciphers"])
        self.assertEqual([int(x) for x in exts.split("-")], fp["extensions"])
        self.assertEqual([int(x) for x in groups.split("-")], fp["groups"])
        self.assertEqual([int(x) for x in ecpf.split("-")],
                         fp["ec_point_formats"])

    def test_tidak_ada_grease_di_vektor(self):
        # GREASE di vektor akan merusak exact-match JA3.
        for kunci in ("ciphers", "extensions", "groups"):
            for v in FINGERPRINTS["chrome_android"][kunci]:
                self.assertNotIn(v, st._GREASE)

    def test_tidak_ada_duplikat(self):
        fp = FINGERPRINTS["chrome_android"]
        for kunci in ("ciphers", "extensions", "groups"):
            self.assertEqual(len(fp[kunci]), len(set(fp[kunci])))


class BuildClientHelloTest(unittest.TestCase):
    """Perakit byte ClientHello — assert byte-level (murni)."""

    def test_parse_ulang_urutan_cipher(self):
        b = build_client_hello("chrome_android", "example.com")
        p = parse_client_hello(b.body)
        self.assertEqual(p["ciphers"], CHROME_ANDROID_CIPHERS)

    def test_parse_ulang_urutan_extension(self):
        b = build_client_hello("chrome_android", "example.com")
        p = parse_client_hello(b.body)
        self.assertEqual(p["extensions"], CHROME_ANDROID_EXTS)

    def test_parse_ulang_groups_dan_point_formats(self):
        b = build_client_hello("chrome_android", "example.com")
        p = parse_client_hello(b.body)
        self.assertEqual(p["groups"], [29, 23, 24])
        self.assertEqual(p["ec_point_formats"], [0])

    def test_legacy_version_771(self):
        # RFC 8446: ClientHello TLS 1.3 menulis 0x0303 (=771) di field
        # legacy_version; dari sinilah "771" di string JA3 berasal.
        b = build_client_hello("chrome_android", "example.com")
        p = parse_client_hello(b.body)
        self.assertEqual(p["version"], 771)
        self.assertEqual(b.body[0:2], b"\x03\x03")

    def test_ja3_sama_dengan_vektor(self):
        b = build_client_hello("chrome_android", "example.com")
        self.assertEqual(ja3_string(b.body), CHROME_ANDROID_JA3)

    def test_ja3_hash_md5(self):
        b = build_client_hello("chrome_android", "example.com")
        j = ja3_string(b.body)
        self.assertEqual(ja3_hash(j), hashlib.md5(j.encode("ascii")).hexdigest())
        self.assertEqual(len(ja3_hash(j)), 32)

    def test_grease_diabaikan_di_ja3(self):
        # Sisipkan nilai GREASE di posisi cipher pertama; JA3 harus tetap
        # sama dengan vektor (GREASE di-strip sesuai spec).
        b = build_client_hello("chrome_android", "example.com")
        p = parse_client_hello(b.body)
        # posisi cipher pertama di body: 2 (versi) + 32 (random) + 1 +
        # 32 (session id) + 2 (panjang) = 69
        off = 2 + 32 + 1 + 32 + 2
        raw = bytearray(b.body)
        raw[off:off + 2] = b"\x0a\x0a"  # GREASE
        # GREASE di-strip -> JA3 sama dengan vektor TANPA cipher 4865
        # yang tadi ditimpa.
        vektor_tanpa_4865 = CHROME_ANDROID_JA3.replace("771,4865-", "771,", 1)
        self.assertEqual(ja3_string(bytes(raw)), vektor_tanpa_4865)
        # tapi parse mentah tetap melihat nilainya (urutan byte-level utuh)
        self.assertEqual(parse_client_hello(bytes(raw))["ciphers"][0], 0x0A0A)

    def test_sni_masuk_extension_server_name(self):
        b = build_client_hello("chrome_android", "contoh.id")
        body = b.body
        # cari extension 0 lalu pastikan hostname ada di datanya
        p = parse_client_hello(body)
        self.assertEqual(p["extensions"][0], 0)
        self.assertIn(b"contoh.id", body)

    def test_randomize_mengacak_tapi_lengkap(self):
        b = build_client_hello("chrome_android", "example.com",
                               randomize_extensions=True)
        p = parse_client_hello(b.body)
        # himpunan sama, cipher tidak ikut diacak
        self.assertEqual(sorted(p["extensions"]), sorted(CHROME_ANDROID_EXTS))
        self.assertEqual(p["ciphers"], CHROME_ANDROID_CIPHERS)
        # JA3 pasti beda dari vektor (urutan extension berubah)
        self.assertNotEqual(ja3_string(b.body), CHROME_ANDROID_JA3)

    def test_fingerprint_tidak_dikenal(self):
        with self.assertRaises(FingerprintError):
            build_client_hello("netscape_navigator", "example.com")

    def test_key_share_ada_untuk_x25519_dan_p256(self):
        b = build_client_hello("chrome_android", "example.com")
        body = b.body
        # extension 51 harus ada dan memuat 2 share (grup 29 dan 23)
        pos = 0
        ketemu = None
        p = parse_client_hello(body)
        idx = p["extensions"].index(51)
        # hitung offset mentah extension ke-idx
        off = 2 + 32 + 1 + 32 + 2 + 2 * len(p["ciphers"]) + 1 + 1 + 2
        for _ in range(idx):
            elen = int.from_bytes(body[off + 2:off + 4], "big")
            off += 4 + elen
        elen = int.from_bytes(body[off + 2:off + 4], "big")
        edata = body[off + 4:off + 4 + elen]
        # key_share: list len(2) + share{grup(2), len(2), pub}
        total = int.from_bytes(edata[0:2], "big")
        q = 2
        grups = []
        while q < 2 + total:
            g = int.from_bytes(edata[q:q + 2], "big")
            ln = int.from_bytes(edata[q + 2:q + 4], "big")
            grups.append((g, ln))
            q += 4 + ln
        self.assertEqual(grups, [(29, 32), (23, 65)])


@butuh_tlslite
class RoundtripTlsliteTest(unittest.TestCase):
    """Byte rakitan -> objek tlslite.ClientHello -> byte identik.

    Syarat mutlak teknik injeksi: transcript hash tlslite-ng dihitung
    dari ``msg.write()``, jadi write() harus mereproduksi byte rakitan
    persis (termasuk urutan extension).
    """

    def test_parse_write_identik(self):
        from tlslite.messages import ClientHello
        from tlslite.utils.codec import Parser
        b = build_client_hello("chrome_android", "example.com")
        ch = ClientHello()
        # seperti _getMsg: tipe handshake sudah dikonsumsi
        ch.parse(Parser(bytearray(len(b.body).to_bytes(3, "big") + b.body)))
        self.assertEqual(bytes(ch.write()), b.handshake_message())

    def test_urutan_extension_lestari_lewat_objek(self):
        from tlslite.messages import ClientHello
        from tlslite.utils.codec import Parser
        b = build_client_hello("chrome_android", "example.com")
        ch = ClientHello()
        ch.parse(Parser(bytearray(len(b.body).to_bytes(3, "big") + b.body)))
        self.assertEqual([e.extType for e in ch.extensions],
                         CHROME_ANDROID_EXTS)
        self.assertEqual(list(ch.cipher_suites), CHROME_ANDROID_CIPHERS)


@butuh_tlslite
class InjeksiClientHelloTest(unittest.TestCase):
    """_clientSendClientHello yang ditimpa mengirim byte rakitan persis.

    Memakai socketpair (tanpa network): generator dijalankan, byte mentah
    dibaca dari sisi lain dan dibandingkan dengan rakitan.
    """

    def test_byte_di_kawat_sama_dengan_rakitan(self):
        import socket
        from tlslite.handshakesettings import HandshakeSettings
        from tlslite.constants import ExtensionType
        s1, s2 = socket.socketpair()
        try:
            dirakit = build_client_hello(
                "chrome_android", "example.com",
                key_material=st._tlslite_key_material)
            conn = st._StealthTLSConnection(
                s1, _ch_body=dirakit.body, _ch_privates=dirakit.privates)
            conn.version = (3, 1)
            settings = HandshakeSettings()
            terkirim = None
            gen = conn._clientSendClientHello(
                settings, None, None, None, None, None,
                "example.com", None, False, None)
            for r in gen:
                if r not in (0, 1):
                    terkirim = r
            s2.settimeout(5)
            raw = b""
            while len(raw) < 5:
                raw += s2.recv(5 - len(raw))
            self.assertEqual(raw[0], 0x16)  # handshake record
            self.assertEqual(raw[1:3], b"\x03\x01")
            reclen = int.from_bytes(raw[3:5], "big")
            while len(raw) < 5 + reclen:
                raw += s2.recv(5 + reclen - len(raw))
            hs = raw[5:]
            self.assertEqual(hs[0], 1)  # client_hello
            self.assertEqual(hs[4:], dirakit.body)
            # private key tertempel di KeyShareEntry (syarat turunan kunci)
            ks = terkirim.getExtension(ExtensionType.key_share)
            self.assertIsNotNone(ks)
            for entry in ks.client_shares:
                self.assertIsNotNone(entry.private)
                self.assertEqual(entry.private,
                                 dirakit.privates[entry.group])
        finally:
            s1.close()
            s2.close()


class VerifikasiRantaiRSATest(unittest.TestCase):
    """Verifikasi rantai RSA — murni stdlib (CA buatan kancil.x509)."""

    @classmethod
    def setUpClass(cls):
        from kancil import x509 as kx509
        cls.kx509 = kx509
        # 1024-bit cukup untuk unit test (cepat); path verifikasi sama.
        ca_key = kx509.gen_rsa(1024)
        cls.ca_key = ca_key
        cls.ca_der = kx509.make_ca("Test CA stealth_tls", ca_key, days=30)
        host_key = kx509.gen_rsa(1024)
        cls.leaf_der = kx509.make_host_cert(
            "example.com", host_key, "Test CA stealth_tls", ca_key, days=30)
        fd, cls.bundle = tempfile.mkstemp(suffix=".pem")
        os.write(fd, kx509.pem_encode("CERTIFICATE", cls.ca_der).encode())
        os.close(fd)
        cls.addClassCleanup(os.unlink, cls.bundle)

    def test_rantai_valid(self):
        self.assertTrue(verify_chain([self.leaf_der, self.ca_der],
                                     "example.com", ca_bundle=self.bundle))

    def test_rantai_tanpa_intermediate(self):
        # Server boleh hanya mengirim leaf; anchor dicari di bundle.
        self.assertTrue(verify_chain([self.leaf_der],
                                     "example.com", ca_bundle=self.bundle))

    def test_hostname_salah_ditolak(self):
        with self.assertRaises(CertVerifyError):
            verify_chain([self.leaf_der, self.ca_der],
                         "salah.com", ca_bundle=self.bundle)

    def test_signature_rusak_ditolak(self):
        rusak = bytearray(self.leaf_der)
        rusak[-10] ^= 0xFF
        with self.assertRaises(CertVerifyError):
            verify_chain([bytes(rusak), self.ca_der],
                         "example.com", ca_bundle=self.bundle)

    def test_self_signed_tak_tepercaya_ditolak(self):
        kunci = self.kx509.gen_rsa(1024)
        jahat = self.kx509.make_ca("Evil", kunci, days=30)
        with self.assertRaises(CertVerifyError):
            verify_chain([jahat], "evil.com", ca_bundle=self.bundle)

    def test_wildcard_satu_level_cocok(self):
        kunci = self.kx509.gen_rsa(1024)
        wild = self.kx509.make_host_cert(
            "*.example.com", kunci, "Test CA stealth_tls", self.ca_key,
            days=30)
        self.assertTrue(verify_chain([wild, self.ca_der], "foo.example.com",
                                     ca_bundle=self.bundle))

    def test_wildcard_dua_level_ditolak(self):
        kunci = self.kx509.gen_rsa(1024)
        wild = self.kx509.make_host_cert(
            "*.example.com", kunci, "Test CA stealth_tls", self.ca_key,
            days=30)
        with self.assertRaises(CertVerifyError):
            verify_chain([wild, self.ca_der], "a.b.example.com",
                         ca_bundle=self.bundle)

    def test_wildcard_tidak_cocok_beda_domain(self):
        kunci = self.kx509.gen_rsa(1024)
        wild = self.kx509.make_host_cert(
            "*.example.com", kunci, "Test CA stealth_tls", self.ca_key,
            days=30)
        with self.assertRaises(CertVerifyError):
            verify_chain([wild, self.ca_der], "foo.contoh.com",
                         ca_bundle=self.bundle)

    def test_kedaluwarsa_ditolak(self):
        kunci = self.kx509.gen_rsa(1024)
        ca_key = self.kx509.gen_rsa(1024)
        ca = self.kx509.make_ca("CA Exp", ca_key, days=30)
        fd, bundle2 = tempfile.mkstemp(suffix=".pem")
        os.write(fd, self.kx509.pem_encode("CERTIFICATE", ca).encode())
        os.close(fd)
        try:
            leaf = self.kx509.make_host_cert(
                "exp.com", kunci, "CA Exp", ca_key, days=-30)
            with self.assertRaises(CertVerifyError):
                verify_chain([leaf, ca], "exp.com", ca_bundle=bundle2)
        finally:
            os.unlink(bundle2)


@butuh_tlslite
class VerifikasiRantaiECDSATest(unittest.TestCase):
    """Verifikasi rantai ECDSA — memakai primitif tlslite-ng + lib ecdsa
    untuk membuat sertifikat uji (tanpa network)."""

    @staticmethod
    def _buat_cert_ecdsa(cn, sk, issuer_cn, issuer_sk, san=(), is_ca=False):
        import hashlib
        import secrets as _secrets
        import time as _time
        from ecdsa import util as _util
        from kancil import x509 as kx509
        oid_sig = (1, 2, 840, 10045, 4, 3, 2)  # ecdsa-with-SHA256
        point = b"\x04" + sk.get_verifying_key().to_string()
        now = _time.time()
        exts = [kx509.der_seq(
            kx509.der_oid(2, 5, 29, 19), kx509.der_bool(True),
            kx509.der_octets(
                kx509.der_seq(kx509.der_bool(True)) if is_ca
                else kx509.der_seq()))]
        if san:
            san_seq = kx509.der_seq(
                *[kx509._tlv(0x82, s.encode("ascii")) for s in san])
            exts.append(kx509.der_seq(
                kx509.der_oid(2, 5, 29, 17), kx509.der_octets(san_seq)))
        tbs = kx509.der_seq(
            kx509._tlv(0xA0, kx509.der_int(2)),
            kx509.der_int(int.from_bytes(_secrets.token_bytes(8), "big") | 1),
            kx509.der_seq(kx509.der_oid(*oid_sig)),
            kx509._name(issuer_cn),
            kx509.der_seq(kx509.der_utctime(now - 86400),
                          kx509.der_utctime(now + 30 * 86400)),
            kx509._name(cn),
            kx509.der_seq(
                kx509.der_seq(kx509.der_oid(1, 2, 840, 10045, 2, 1),
                              kx509.der_oid(1, 2, 840, 10045, 3, 1, 7)),
                kx509.der_bitstring(point)),
            kx509._tlv(0xA3, kx509.der_seq(*exts)),
        )
        sig = issuer_sk.sign_digest(
            hashlib.sha256(tbs).digest(), sigencode=_util.sigencode_der)
        return kx509.der_seq(
            tbs, kx509.der_seq(kx509.der_oid(*oid_sig)),
            kx509.der_bitstring(sig))

    def test_rantai_ecdsa_valid(self):
        from ecdsa import SigningKey, NIST256p
        from kancil import x509 as kx509
        ca_sk = SigningKey.generate(curve=NIST256p)
        ca_der = self._buat_cert_ecdsa("EC Test CA", ca_sk, "EC Test CA",
                                       ca_sk, is_ca=True)
        leaf_sk = SigningKey.generate(curve=NIST256p)
        leaf_der = self._buat_cert_ecdsa("ec.example.com", leaf_sk,
                                         "EC Test CA", ca_sk,
                                         san=("ec.example.com",))
        fd, bundle = tempfile.mkstemp(suffix=".pem")
        os.write(fd, kx509.pem_encode("CERTIFICATE", ca_der).encode())
        os.close(fd)
        try:
            self.assertTrue(verify_chain([leaf_der, ca_der],
                                         "ec.example.com", ca_bundle=bundle))
            with self.assertRaises(CertVerifyError):
                verify_chain([leaf_der, ca_der],
                             "salah.com", ca_bundle=bundle)
        finally:
            os.unlink(bundle)


class KlienTest(unittest.TestCase):
    """API StealthTLSClient (tanpa network)."""

    def test_fingerprint_tidak_dikenal(self):
        with self.assertRaises(FingerprintError):
            StealthTLSClient(fingerprint="tidak_ada")

    def test_get_tanpa_tlslite_pesan_jelas(self):
        # Simulasi dependensi hilang: get() harus menolak dengan pesan
        # yang jelas, bukan ImportError samar.
        asli = st._TLS_AVAILABLE
        st._TLS_AVAILABLE = False
        try:
            klien = StealthTLSClient()
            self.assertFalse(klien.available())
            with self.assertRaises(StealthTLSUnavailable) as cm:
                klien.get("https://example.com/")
            self.assertIn("pip install tlslite-ng", str(cm.exception))
        finally:
            st._TLS_AVAILABLE = asli

    @butuh_tlslite
    def test_get_hanya_https(self):
        klien = StealthTLSClient()
        with self.assertRaises(StealthTLSError):
            klien.get("http://example.com/")

    def test_parse_respons(self):
        mentah = (b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                  b"Content-Length: 5\r\n\r\nhelloEXTRA")
        status, headers, body = st._parse_respons_http(mentah)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/plain")
        self.assertEqual(body, b"hello")

    def test_parse_respons_chunked(self):
        mentah = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                  b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n")
        status, _, body = st._parse_respons_http(mentah)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"hello world")


if __name__ == "__main__":
    unittest.main()
