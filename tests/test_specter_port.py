"""Test port SPECTER: provider detection, Turnstile, clearance cache."""
import os
import tempfile
import time
import unittest


class ProviderDetectTest(unittest.TestCase):
    def test_cloudflare_body(self):
        from kancil import providers
        r = providers.detect_provider(
            body="<div class=cf-turnstile><script src=challenges.cloudflare.com>")
        self.assertEqual(r["provider"], "cloudflare")
        self.assertGreaterEqual(r["confidence"], 0.8)

    def test_cloudflare_headers(self):
        from kancil import providers
        r = providers.detect_provider(
            headers={"server": "cloudflare", "cf-ray": "abc123"})
        self.assertEqual(r["provider"], "cloudflare")

    def test_hcaptcha(self):
        from kancil import providers
        r = providers.detect_provider(
            body='<script src="https://hcaptcha.com/1/api.js">')
        self.assertEqual(r["provider"], "hcaptcha")

    def test_recaptcha(self):
        from kancil import providers
        self.assertEqual(providers.detect_provider(
            body="grecaptcha.render")["provider"], "recaptcha")

    def test_datadome(self):
        from kancil import providers
        self.assertEqual(providers.detect_provider(
            body="datadome.js")["provider"], "datadome")

    def test_akamai(self):
        from kancil import providers
        r = providers.detect_provider(
            headers={"x-akamai-transformed": "1"})
        self.assertEqual(r["provider"], "akamai")

    def test_imperva(self):
        from kancil import providers
        r = providers.detect_provider(
            headers={"x-iinfo": "1"}, cookies="incap_ses_123=abc")
        self.assertEqual(r["provider"], "imperva")

    def test_arkose(self):
        from kancil import providers
        self.assertEqual(providers.detect_provider(
            body="funcaptcha.com/fc")["provider"], "arkose")

    def test_aws_waf(self):
        from kancil import providers
        r = providers.detect_provider(
            headers={"x-amzn-waf-action": "challenge"})
        self.assertEqual(r["provider"], "aws_waf")

    def test_none(self):
        from kancil import providers
        r = providers.detect_provider(body="<html>halo dunia</html>")
        self.assertIsNone(r["provider"])
        self.assertEqual(r["confidence"], 0.0)

    def test_hint(self):
        from kancil import providers
        self.assertIn("Cloudflare", providers.provider_hint("cloudflare"))
        self.assertEqual(providers.provider_hint("tidak-ada"), "")


class ClearanceCacheTest(unittest.TestCase):
    def _home(self):
        d = tempfile.mkdtemp()
        old = os.environ.get("HOME")
        os.environ["HOME"] = d
        return d, old

    def _restore(self, old):
        if old is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = old

    def test_save_load(self):
        from kancil import clearance
        d, old = self._home()
        try:
            self.assertTrue(clearance.save(
                "example.com", {"cf_clearance": "abc"}))
            self.assertEqual(clearance.load("example.com"),
                             {"cf_clearance": "abc"})
            # file 0600
            import stat
            p = os.path.join(d, ".kancil", "clearance.json")
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        finally:
            self._restore(old)

    def test_expired(self):
        from kancil import clearance
        d, old = self._home()
        try:
            clearance.save("x.com", {"cf_clearance": "a"}, ttl_s=60)
            # paksa kedaluwarsa
            import json as _j
            p = os.path.join(d, ".kancil", "clearance.json")
            data = _j.load(open(p))
            data["x.com"]["expires"] = time.time() - 1
            _j.dump(data, open(p, "w"))
            self.assertIsNone(clearance.load("x.com"))
        finally:
            self._restore(old)

    def test_clear(self):
        from kancil import clearance
        d, old = self._home()
        try:
            clearance.save("a.com", {"cf_clearance": "1"})
            clearance.save("b.com", {"cf_clearance": "2"})
            clearance.clear("a.com")
            self.assertIsNone(clearance.load("a.com"))
            self.assertIsNotNone(clearance.load("b.com"))
            clearance.clear()
            self.assertIsNone(clearance.load("b.com"))
        finally:
            self._restore(old)

    def test_host_of(self):
        from kancil import clearance
        self.assertEqual(clearance.host_of(
            "https://sub.example.com:8443/path"), "sub.example.com")
        self.assertEqual(clearance.host_of("example.com"), "example.com")

    def test_save_from_kancil(self):
        from kancil import clearance

        class E:
            def cookies(self):
                return [{"name": "cf_clearance", "value": "zz"},
                        {"name": "sesi", "value": "qq"}]

        class K:
            _engine_name = "webview"
            engine = E()

            def tabs(self):
                return {"tabs": [{"current": True,
                                  "url": "https://cf.example/"}]}

        d, old = self._home()
        try:
            r = clearance.save_from_kancil(K())
            self.assertTrue(r["success"])
            self.assertEqual(r["host"], "cf.example")
            self.assertEqual(r["cookies"], ["cf_clearance"])
            # cookie non-clearance tidak ikut
            self.assertEqual(clearance.load("cf.example"),
                             {"cf_clearance": "zz"})
        finally:
            self._restore(old)

    def test_save_from_kancil_no_clearance(self):
        from kancil import clearance

        class E:
            def cookies(self):
                return [{"name": "sesi", "value": "qq"}]

        class K:
            _engine_name = "webview"
            engine = E()

            def tabs(self):
                return {"tabs": []}

        d, old = self._home()
        try:
            r = clearance.save_from_kancil(K(), host="x.com")
            self.assertFalse(r["success"])
        finally:
            self._restore(old)


class TurnstileTest(unittest.TestCase):
    def test_non_webview_rejected(self):
        from kancil import stuck
        from kancil.api import Kancil
        r = stuck.solve_turnstile(Kancil(engine="static"))
        self.assertFalse(r["success"])
        self.assertFalse(r["solved"])

    def test_no_widget(self):
        from kancil import stuck

        class K:
            _engine_name = "webview"

            def evaluate(self, js):
                return {"success": True, "result": "[]"}

        r = stuck.solve_turnstile(K(), timeout=1)
        self.assertFalse(r["solved"])

    def test_resolve_turnstile_solves(self):
        from kancil import stuck

        class K:
            _engine_name = "webview"
            tapped = None
            clean = False

            def evaluate(self, js):
                if "outerHTML" in js:
                    return {"success": True,
                            "result": "<div class=cf-turnstile>"}
                if "innerText" in js:
                    return {"success": True,
                            "result": "" if self.clean
                            else "verify you are human, please complete"}
                if "querySelectorAll" in js:
                    return {"success": True,
                            "result": '[{"x": 100, "y": 200}]'}
                return {"success": True, "result": "2"}

            def tabs(self):
                return {"tabs": [{"current": True,
                                  "url": "https://cf.example/"}]}

            def touch(self, action, x=None, y=None, human=False):
                self.tapped = (x, y)
                self.clean = True  # setelah klik, challenge hilang
                return {"success": True}

        import tempfile as _tf
        import os as _os
        old_home = _os.environ.get("HOME")
        _os.environ["HOME"] = _tf.mkdtemp()
        try:
            k = K()
            r = stuck.resolve(k)
            self.assertTrue(r["resolved"], r)
            self.assertFalse(r["needs_human"])
            self.assertEqual(r["provider"], "cloudflare")
            self.assertEqual(k.tapped, (100, 200))
        finally:
            if old_home is None:
                del _os.environ["HOME"]
            else:
                _os.environ["HOME"] = old_home


class AutopilotClearanceTest(unittest.TestCase):
    def test_inject_before_open(self):
        from kancil import autopilot, clearance

        injected = []

        class K:
            _engine_name = "webview"
            _null_streak = 0

            def tool(self, payload):
                return {"success": True}

            def tabs(self):
                return {"tabs": []}

            def evaluate(self, js):
                return {"success": True, "result": 2}

            def _page_has(self, text=None, selector=None):
                return True

        orig = clearance.inject_to_kancil

        def fake(kancil, host=None, url=None):
            injected.append(clearance.host_of(url or ""))
            return {"success": True}

        clearance.inject_to_kancil = fake
        try:
            with tempfile.TemporaryDirectory() as d:
                ck = os.path.join(d, "c.json")
                r = autopilot.run(
                    K(), {"name": "t", "stuck": "off", "clearance": True,
                          "steps": [{"name": "buka", "action": "open",
                                     "params": {"url":
                                                "https://cf.example/x"}}]},
                    checkpoint_path=ck)
                self.assertTrue(r["success"])
                self.assertEqual(injected, ["cf.example"])
        finally:
            clearance.inject_to_kancil = orig


if __name__ == "__main__":
    unittest.main()
