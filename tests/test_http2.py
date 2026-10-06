"""Transport httpx+HTTP/2 opsional untuk StaticEngine.

Semua test tanpa network asli: request lewat modul httpx palsu yang
disisipkan ke sys.modules, atau lewat jalur fallback urllib. Tidak ada
koneksi keluar.
"""
import http.cookiejar
import os
import sys
import types
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kancil import Kancil
from kancil import engines
from kancil.engines import StaticEngine, EngineError

_MISSING = object()


# ---------------- httpx palsu ----------------

class _FakeHeaders:
    """Meniru subset httpx.Headers yang dipakai adaptor."""

    def __init__(self, pairs):
        self._pairs = list(pairs)

    def get(self, key, default=None):
        kl = key.lower()
        for k, v in self._pairs:
            if k.lower() == kl:
                return v
        return default

    def get_list(self, key, split_commas=False):
        kl = key.lower()
        return [v for k, v in self._pairs if k.lower() == kl]

    def items(self):
        return list(self._pairs)


class _FakeResponse:
    """Meniru subset httpx.Response yang dipakai adaptor."""

    def __init__(self, status_code=200, headers=(), content=b"",
                 url="", history=()):
        self.status_code = status_code
        self.headers = _FakeHeaders(headers)
        self.content = content
        self.url = url
        self.history = list(history)

    def close(self):
        pass


class _FakeClient:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        self.make_response = None  # (method, url, kwargs) -> _FakeResponse
        _FakeClient.instances.append(self)

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if self.make_response is not None:
            return self.make_response(method, url, kwargs)
        return _FakeResponse(
            200, [("Content-Type", "text/html; charset=utf-8")],
            b"<html><body>halo</body></html>", url)


def _pasang_fake_httpx(testcase, h2=True):
    """Sisipkan modul httpx palsu ke sys.modules (dengan cleanup)."""
    mod = types.ModuleType("httpx")
    mod.Client = _FakeClient
    saved_httpx = sys.modules.get("httpx", _MISSING)
    saved_h2 = sys.modules.get("h2", _MISSING)
    sys.modules["httpx"] = mod
    if h2:
        sys.modules["h2"] = types.ModuleType("h2")
    else:
        sys.modules.pop("h2", None)
    _FakeClient.instances.clear()

    def _undo():
        if saved_httpx is _MISSING:
            sys.modules.pop("httpx", None)
        else:
            sys.modules["httpx"] = saved_httpx
        if saved_h2 is _MISSING:
            sys.modules.pop("h2", None)
        else:
            sys.modules["h2"] = saved_h2

    testcase.addCleanup(_undo)
    return mod


def _blokir_httpx(testcase):
    """Simulasikan httpx tidak terinstal: import httpx -> ImportError."""
    saved_httpx = sys.modules.get("httpx", _MISSING)
    saved_h2 = sys.modules.get("h2", _MISSING)
    sys.modules["httpx"] = None
    sys.modules.pop("h2", None)

    def _undo():
        if saved_httpx is _MISSING:
            sys.modules.pop("httpx", None)
        else:
            sys.modules["httpx"] = saved_httpx
        if saved_h2 is _MISSING:
            sys.modules.pop("h2", None)
        else:
            sys.modules["h2"] = saved_h2

    testcase.addCleanup(_undo)


def _seed_cookie(jar, name, value, domain="example.test"):
    jar.set_cookie(http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=True, domain_initial_dot=False,
        path="/", path_specified=True, secure=False, expires=None,
        discard=True, comment=None, comment_url=None, rest={}))


class TestHttpxFallback(unittest.TestCase):
    """http2=True tanpa httpx -> diam-diam urllib (perilaku lama)."""

    def test_tanpa_httpx_pakai_urllib(self):
        _blokir_httpx(self)
        self.assertFalse(engines._httpx_importable())
        eng = StaticEngine(http2=True)
        self.assertFalse(isinstance(eng.opener, engines._HttpxOpener))
        st = eng.set_http2(True)
        self.assertTrue(st["success"])
        self.assertTrue(st["http2"])
        self.assertFalse(st["active"])
        self.assertEqual(st["mode"], "urllib")
        self.assertIn("tidak terinstal", st["note"])

    def test_default_tetap_urllib(self):
        _blokir_httpx(self)
        eng = StaticEngine()  # http2=False default
        self.assertFalse(eng.http2)
        self.assertFalse(isinstance(eng.opener, engines._HttpxOpener))

    def test_set_http2_off(self):
        _pasang_fake_httpx(self)
        eng = StaticEngine(http2=True)
        self.assertTrue(isinstance(eng.opener, engines._HttpxOpener))
        st = eng.set_http2(False)
        self.assertTrue(st["success"])
        self.assertFalse(st["active"])
        self.assertEqual(st["mode"], "urllib")


class TestHttpxTransport(unittest.TestCase):
    """Dengan httpx palsu: transport dipakai, response diterjemahkan."""

    def test_opener_dan_klien_dibangun_benar(self):
        _pasang_fake_httpx(self)
        eng = StaticEngine(http2=True, proxy="http://127.0.0.1:8888",
                           timeout=11)
        self.assertTrue(isinstance(eng.opener, engines._HttpxOpener))
        self.assertEqual(eng.opener.mode, "http2")
        client = _FakeClient.instances[-1]
        self.assertTrue(client.kwargs["http2"])
        self.assertTrue(client.kwargs["follow_redirects"])
        self.assertFalse(client.kwargs["trust_env"])
        self.assertEqual(client.kwargs["proxy"], "http://127.0.0.1:8888")
        self.assertEqual(client.kwargs["timeout"], 11)
        # TLS tidak pernah di-disable: verify default True
        self.assertTrue(client.kwargs.get("verify", True))

    def test_fetch_mengembalikan_tuple_benar(self):
        _pasang_fake_httpx(self)
        eng = StaticEngine(http2=True)
        url, status, ctype, body, hdrs = eng.fetch("http://example.test/")
        self.assertEqual(url, "http://example.test/")
        self.assertEqual(status, 200)
        self.assertEqual(ctype, "text/html")
        self.assertEqual(body, b"<html><body>halo</body></html>")
        self.assertEqual(hdrs["Content-Type"], "text/html; charset=utf-8")
        # netlog tercatat seperti jalur urllib
        entry = eng.netlog[-1]
        self.assertEqual(entry["status"], 200)
        self.assertEqual(entry["url"], "http://example.test/")
        self.assertIn("User-agent", entry["req_headers"])
        # request yang dikirim membawa header engine
        sent = _FakeClient.instances[-1].calls[0]
        self.assertEqual(sent["method"], "GET")
        self.assertIn("User-agent", sent["kwargs"]["headers"])

    def test_http_error_diraise_seperti_urllib(self):
        _pasang_fake_httpx(self)
        eng = StaticEngine(http2=True)

        def _resp_404(method, url, kwargs):
            return _FakeResponse(404, [("Content-Type", "text/plain")],
                                 b"tidak ada", url)

        _FakeClient.instances[-1].make_response = _resp_404
        req = urllib.request.Request("http://example.test/hilang")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            eng.opener.open(req, timeout=5)
        self.assertEqual(cm.exception.code, 404)
        with self.assertRaises(EngineError) as cm2:
            eng.fetch("http://example.test/hilang")
        self.assertIn("HTTP 404", str(cm2.exception))

    def test_read_bertahap(self):
        _pasang_fake_httpx(self)
        eng = StaticEngine(http2=True)
        req = urllib.request.Request("http://example.test/")
        with eng.opener.open(req, timeout=5) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(r.geturl(), "http://example.test/")
            part = r.read(6)
            rest = r.read()
            self.assertEqual(part + rest, b"<html><body>halo</body></html>")
            self.assertEqual(r.read(), b"")

    def test_content_encoding_disembunyikan(self):
        # httpx sudah men-decode body; adaptor tidak boleh mengklaim
        # encoding agar fetch() tidak men-decode dua kali.
        _pasang_fake_httpx(self)
        eng = StaticEngine(http2=True)

        def _resp_gzip(method, url, kwargs):
            return _FakeResponse(
                200, [("Content-Type", "text/html"),
                      ("Content-Encoding", "gzip")],
                b"<html>plain</html>", url)

        _FakeClient.instances[-1].make_response = _resp_gzip
        req = urllib.request.Request("http://example.test/")
        with eng.opener.open(req, timeout=5) as r:
            self.assertIsNone(r.headers.get("Content-Encoding"))
            self.assertEqual(r.read(), b"<html>plain</html>")


class TestHttpxCookies(unittest.TestCase):
    """Cookie dua arah dengan engine jar (nyata, best-effort)."""

    def test_cookie_round_trip(self):
        _pasang_fake_httpx(self)
        eng = StaticEngine(http2=True)
        _seed_cookie(eng.jar, "session", "abc")

        def _resp(method, url, kwargs):
            return _FakeResponse(
                200, [("Content-Type", "text/html"),
                      ("Set-Cookie", "token=xyz; Path=/")],
                b"ok", url)

        _FakeClient.instances[-1].make_response = _resp
        url, status, ctype, body, hdrs = eng.fetch("http://example.test/")
        self.assertEqual(status, 200)
        self.assertEqual(url, "http://example.test/")
        # cookie dari jar terkirim di request ...
        sent_headers = _FakeClient.instances[-1].calls[0]["kwargs"]["headers"]
        self.assertIn("session=abc", sent_headers.get("Cookie", ""))
        # ... dan netlog mencatatnya seperti jalur urllib
        self.assertEqual(eng.netlog[-1]["cookies_sent"].get("session"), "abc")
        # ... Set-Cookie respons terserap kembali ke jar
        names = [c.name for c in eng.jar]
        self.assertIn("token", names)
        self.assertIn("session", names)

    def test_cookie_dikirim_ulang_di_request_berikutnya(self):
        _pasang_fake_httpx(self)
        eng = StaticEngine(http2=True)

        def _resp(method, url, kwargs):
            return _FakeResponse(
                200, [("Set-Cookie", "pertama=1; Path=/")], b"ok", url)

        _FakeClient.instances[-1].make_response = _resp
        with eng.opener.open(urllib.request.Request("http://example.test/"),
                             timeout=5):
            pass
        # request kedua harus membawa cookie dari respons pertama
        with eng.opener.open(urllib.request.Request("http://example.test/"),
                             timeout=5):
            pass
        sent2 = _FakeClient.instances[-1].calls[1]["kwargs"]["headers"]
        self.assertIn("pertama=1", sent2.get("Cookie", ""))


class TestHttpxTanpaH2(unittest.TestCase):
    """httpx ada tapi h2 hilang -> HTTP/1.1 + peringatan (bukan urllib)."""

    def test_turun_ke_http1_dengan_peringatan(self):
        _pasang_fake_httpx(self, h2=False)
        self.assertFalse(engines._h2_importable())
        eng = StaticEngine(http2=True)
        self.assertTrue(isinstance(eng.opener, engines._HttpxOpener))
        self.assertEqual(eng.opener.mode, "http1")
        client = _FakeClient.instances[-1]
        self.assertFalse(client.kwargs["http2"])
        st = eng.set_http2(True)
        self.assertTrue(st["active"])
        self.assertEqual(st["mode"], "http1")
        self.assertIsNotNone(st["note"])
        self.assertTrue(any(e.get("type") == "http2" for e in eng.errors))


class TestKancilMeneruskanHttp2(unittest.TestCase):
    """Kancil(...) meneruskan opsi http2 ke StaticEngine."""

    def test_kancil_http2_true(self):
        _pasang_fake_httpx(self)
        k = Kancil(engine="static", http2=True)
        self.assertTrue(k.engine.http2)
        self.assertTrue(isinstance(k.engine.opener, engines._HttpxOpener))

    def test_kancil_default_tetap_urllib(self):
        _blokir_httpx(self)
        k = Kancil(engine="static")
        self.assertFalse(k.engine.http2)
        self.assertFalse(isinstance(k.engine.opener, engines._HttpxOpener))


if __name__ == "__main__":
    unittest.main()
