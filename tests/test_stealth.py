"""Tests for kancil/stealth.py and StaticEngine impersonation wiring."""
import gzip
import http.cookiejar
import http.server
import json
import threading
import unittest

from kancil import stealth
from kancil.engines import StaticEngine, EngineError


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/cookie":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Set-Cookie", "sess=abc123; Path=/")
            self.end_headers()
            self.wfile.write(b"cookie-set")
        elif self.path == "/echo-cookie":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write((self.headers.get("Cookie") or "").encode())
        elif self.path == "/gzip":
            body = gzip.compress(b"<html>gzip-ok</html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/missing":
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"not here")
        elif self.path == "/ua":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write((self.headers.get("User-Agent") or "").encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html><head><title>T</title></head><body>hi</body></html>")


class StealthServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)


# ---------- profiles & JS ----------

class TestProfiles(unittest.TestCase):
    def test_profiles_structure(self):
        self.assertIn("chrome_android", stealth.list_profiles())
        for name in stealth.list_profiles():
            p = stealth.BROWSER_PROFILES[name]
            self.assertIsInstance(p["impersonate"], str)
            self.assertTrue(p["impersonate"])
            self.assertIn("Mozilla", p["ua"])
            self.assertIsInstance(p.get("headers", {}), dict)

    def test_default_profile_exists(self):
        self.assertIn(stealth.DEFAULT_PROFILE, stealth.BROWSER_PROFILES)

    def test_stealth_js_vectors(self):
        js = stealth.STEALTH_JS
        for needle in ["webdriver", "plugins", "toDataURL", "37445",
                       "37446", "userAgentData", "window.chrome",
                       "Permissions", "getBattery", "AudioBuffer",
                       "getChannelData", "hardwareConcurrency",
                       "deviceMemory", "__kancil_stealth__",
                       "[native code]"]:
            self.assertIn(needle, js, "STEALTH_JS missing vector: " + needle)

    def test_stealth_js_idempotent_guard(self):
        self.assertIn("already-applied", stealth.STEALTH_JS)


class FakeEngine:
    """Minimal evaluate() stand-in for apply_stealth()."""
    def __init__(self):
        self.applied = False

    def evaluate(self, js):
        if "__kancil_stealth__" in js and "return 'stealth-applied'" in js:
            if self.applied:
                return {"success": True, "result": "'already-applied'"}
            self.applied = True
            return {"success": True, "result": "'stealth-applied'"}
        if "JSON.stringify" in js:
            return {"success": True, "result": json.dumps(
                {"stealth": self.applied, "webdriver": False,
                 "plugins": 3, "ua_data": True, "chrome": True})}
        return {"success": False, "errors": ["unexpected js"]}


class TestApplyStealth(unittest.TestCase):
    def test_apply_and_verify(self):
        eng = FakeEngine()
        out = stealth.apply_stealth(eng)
        self.assertTrue(out["success"])
        self.assertTrue(out["applied"])
        self.assertEqual(out["verified"]["webdriver"], False)
        self.assertEqual(out["verified"]["plugins"], 3)

    def test_idempotent(self):
        eng = FakeEngine()
        stealth.apply_stealth(eng)
        out = stealth.apply_stealth(eng)
        self.assertTrue(out["success"])
        self.assertTrue(out["verified"]["stealth"])

    def test_evaluate_failure(self):
        class Boom:
            def evaluate(self, js):
                raise ConnectionError("no agent")
        out = stealth.apply_stealth(Boom())
        self.assertFalse(out["success"])
        self.assertTrue(out["errors"])


# ---------- impersonated transport (needs curl_cffi) ----------

needs_cffi = unittest.skipUnless(stealth.HAVE_CURL_CFFI, "curl_cffi missing")


@needs_cffi
class TestImpersonatedTransport(StealthServer):
    def test_opener_type_and_ua(self):
        eng = StaticEngine(impersonate="chrome")
        self.assertIsInstance(eng.opener, stealth.ImpersonatedOpener)
        self.assertIn("Chrome/124", eng.ua)

    def test_fetch_ok_and_netlog(self):
        eng = StaticEngine(impersonate="chrome_android")
        final, status, ctype, raw, hdrs = eng.fetch(self.url("/"))
        self.assertEqual(status, 200)
        self.assertIn(b"hi", raw)
        self.assertTrue(eng.netlog)
        self.assertEqual(eng.netlog[-1]["status"], 200)

    def test_profile_ua_on_wire(self):
        eng = StaticEngine(impersonate="firefox")
        _, _, _, raw, _ = eng.fetch(self.url("/ua"))
        self.assertIn(b"Firefox/133", raw)

    def test_cookie_roundtrip(self):
        eng = StaticEngine(impersonate="chrome")
        eng.fetch(self.url("/cookie"))
        names = [c.name for c in eng.jar]
        self.assertIn("sess", names)
        _, _, _, raw, _ = eng.fetch(self.url("/echo-cookie"))
        self.assertIn(b"sess=abc123", raw)

    def test_http_error_parity(self):
        eng = StaticEngine(impersonate="chrome")
        with self.assertRaises(EngineError) as cm:
            eng.fetch(self.url("/missing"))
        self.assertIn("HTTP 404", str(cm.exception))
        # urllib path behaves the same
        plain = StaticEngine()
        with self.assertRaises(EngineError) as cm2:
            plain.fetch(self.url("/missing"))
        self.assertIn("HTTP 404", str(cm2.exception))

    def test_gzip_not_double_decoded(self):
        eng = StaticEngine(impersonate="chrome")
        _, status, _, raw, _ = eng.fetch(self.url("/gzip"))
        self.assertEqual(status, 200)
        self.assertIn(b"gzip-ok", raw)

    def test_set_impersonate_invalid(self):
        eng = StaticEngine()
        r = eng.set_impersonate("nope")
        self.assertFalse(r["success"])

    def test_set_impersonate_toggle(self):
        eng = StaticEngine()
        r = eng.set_impersonate("safari")
        self.assertTrue(r["success"])
        self.assertTrue(r["active"])
        self.assertIsInstance(eng.opener, stealth.ImpersonatedOpener)
        r = eng.set_impersonate(None)
        self.assertTrue(r["success"])
        self.assertNotIsInstance(eng.opener, stealth.ImpersonatedOpener)

    def test_open_works_end_to_end(self):
        eng = StaticEngine(impersonate="chrome_android")
        r = eng.open(self.url("/"))
        self.assertTrue(r["success"])
        self.assertEqual(r["status"], 200)
        self.assertEqual(r["title"], "T")

    def test_clear_session_cookies(self):
        eng = StaticEngine(impersonate="chrome")
        eng.fetch(self.url("/cookie"))
        self.assertTrue(list(eng.jar))
        r = eng.clear_session("cookies")
        self.assertTrue(r["success"], r)
        self.assertEqual(list(eng.jar), [])
        # impersonated session jar cleared too: no Cookie header next fetch
        _, _, _, raw, _ = eng.fetch(self.url("/echo-cookie"))
        self.assertNotIn(b"sess=", raw)


class TestFallbackWithoutCffi(unittest.TestCase):
    def test_fallback_to_urllib(self):
        real = stealth.HAVE_CURL_CFFI
        stealth.HAVE_CURL_CFFI = False
        try:
            eng = StaticEngine(impersonate="chrome")
            self.assertNotIsInstance(eng.opener, stealth.ImpersonatedOpener)
            r = eng.set_impersonate("chrome")
            self.assertTrue(r["success"])
            self.assertFalse(r["active"])
            self.assertIn("urllib", r["note"])
        finally:
            stealth.HAVE_CURL_CFFI = real


# ---------- Kancil API + CLI surface ----------

@needs_cffi
class TestStealthAPI(unittest.TestCase):
    def test_api_kwarg(self):
        from kancil.api import Kancil
        b = Kancil(engine="static", impersonate="chrome")
        self.assertIsInstance(b.engine.opener, stealth.ImpersonatedOpener)

    def test_stealth_status(self):
        from kancil.api import Kancil
        b = Kancil(engine="static")
        r = b.stealth_status()
        self.assertTrue(r["success"])
        self.assertTrue(r["curl_cffi"])
        self.assertIn("chrome_android", r["profiles"])
        self.assertIsNone(r["impersonate"])
        self.assertFalse(r["impersonate_active"])

    def test_stealth_impersonate_api(self):
        from kancil.api import Kancil
        b = Kancil(engine="static")
        r = b.stealth_impersonate("firefox")
        self.assertTrue(r["success"], r)
        self.assertTrue(r["active"])
        self.assertEqual(b.stealth_status()["impersonate"], "firefox")
        r = b.stealth_impersonate("off")
        self.assertTrue(r["success"])
        self.assertIsNone(b.stealth_status()["impersonate"])

    def test_state_roundtrip(self):
        from kancil.api import Kancil
        b = Kancil(engine="static", impersonate="tor")
        st = b.export_state()
        self.assertEqual(st["impersonate"], "tor")


class TestStealthCLI(unittest.TestCase):
    def test_flag_parses(self):
        from kancil import cli as cli_mod
        p = cli_mod.build_parser()
        args = p.parse_args(["--impersonate", "tor", "open", "http://example.com"])
        self.assertEqual(args.impersonate, "tor")

    def test_stealth_subcommand_parses(self):
        from kancil import cli as cli_mod
        p = cli_mod.build_parser()
        args = p.parse_args(["stealth", "impersonate", "chrome"])
        self.assertEqual(args.cmd, "stealth")
        self.assertEqual(args.action, "impersonate")
        self.assertEqual(args.profile, "chrome")


if __name__ == "__main__":
    unittest.main()
