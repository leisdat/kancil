"""Test autopilot: watchdog, settled nav, stuck detector, plan runner,
parameterized replay, scroll_until."""
import json
import os
import tempfile
import unittest


class StuckDetectTest(unittest.TestCase):
    def test_captcha(self):
        from kancil import stuck
        d = stuck.detect("Please verify you are human. I'm not a robot.")
        self.assertTrue(d["stuck"])
        self.assertEqual(d["primary"], "captcha")

    def test_consent(self):
        from kancil import stuck
        d = stuck.detect("We value your privacy. Accept all cookies!")
        self.assertEqual(d["primary"], "consent")

    def test_login_wall(self):
        from kancil import stuck
        d = stuck.detect("Log in to continue")
        self.assertEqual(d["primary"], "login_wall")

    def test_paywall(self):
        from kancil import stuck
        d = stuck.detect("Subscribe to continue reading")
        self.assertEqual(d["primary"], "paywall")

    def test_challenge(self):
        from kancil import stuck
        d = stuck.detect("Just a moment... checking your browser")
        self.assertEqual(d["primary"], "challenge")

    def test_clean(self):
        from kancil import stuck
        d = stuck.detect("Halo dunia, ini artikel biasa tentang kancil.")
        self.assertFalse(d["stuck"])
        self.assertIsNone(d["primary"])

    def test_priority_captcha_over_consent(self):
        from kancil import stuck
        d = stuck.detect("Accept all cookies. Also verify you are human "
                         "with recaptcha.")
        self.assertEqual(d["primary"], "captcha")

    def test_resolve_captcha_needs_human(self):
        from kancil import stuck

        class K:
            _engine_name = "static"

            def tabs(self):
                return {"tabs": []}

            class engine:
                page = None

        # page_text untuk static baca engine.page.text
        K.engine.page = type("P", (), {"text": "verify you are human",
                                       "url": ""})()
        r = stuck.resolve(K())
        self.assertFalse(r["resolved"])
        self.assertTrue(r["needs_human"])
        self.assertEqual(r["kind"], "captcha")


class SubstituteTest(unittest.TestCase):
    def test_nested(self):
        from kancil.autopilot import substitute_variables as sub
        o = {"a": "{{x}}/{{y}}", "b": [{"c": "{{x}}"}], "d": 5}
        self.assertEqual(sub(o, {"x": "A", "y": "B"}),
                         {"a": "A/B", "b": [{"c": "A"}], "d": 5})

    def test_no_vars(self):
        from kancil.autopilot import substitute_variables as sub
        self.assertEqual(sub({"a": "b"}, None), {"a": "b"})


class _Stub:
    """Kancil stub untuk menguji runner autopilot."""
    _engine_name = "static"

    def __init__(self, fail_on=()):
        self.calls = []
        self._null_streak = 0
        self._fail_on = set(fail_on)

    def tool(self, payload):
        self.calls.append(payload)
        if payload["action"] in self._fail_on:
            return {"success": False,
                    "error": {"code": "X", "message": "boom"}}
        return {"success": True, "action": payload["action"]}

    def tabs(self):
        return {"tabs": []}

    def evaluate(self, js):
        return {"success": True, "result": 2}

    def _page_has(self, text=None, selector=None):
        return True

    def restore_vault(self, name):
        self.restored = name
        return {"success": True}

    def screenshot(self, path=None):
        return {"success": False}


class AutopilotRunTest(unittest.TestCase):
    def _plan(self, **kw):
        p = {"name": "t", "stuck": "off", "retries": 1, "backoff_s": 0,
             "screenshot_on_fail": False,
             "steps": [{"name": "s1", "action": "open",
                        "params": {"url": "https://x.com/{{p}}"}},
                       {"name": "s2", "action": "click",
                        "params": {"selector": "#go"},
                        "verify_text": "ok"}]}
        p.update(kw)
        return p

    def test_success_and_checkpoint_cleanup(self):
        from kancil import autopilot
        with tempfile.TemporaryDirectory() as d:
            ck = os.path.join(d, "c.json")
            s = _Stub()
            r = autopilot.run(s, self._plan(), checkpoint_path=ck,
                              variables={"p": "abc"})
            self.assertTrue(r["success"])
            self.assertEqual(r["steps_ok"], 2)
            self.assertEqual(s.calls[0]["url"], "https://x.com/abc")
            self.assertFalse(os.path.exists(ck))

    def test_fail_stops_and_checkpoint_kept(self):
        from kancil import autopilot
        with tempfile.TemporaryDirectory() as d:
            ck = os.path.join(d, "c.json")
            s = _Stub(fail_on={"click"})
            r = autopilot.run(s, self._plan(), checkpoint_path=ck)
            self.assertFalse(r["success"])
            self.assertEqual(r["steps_ran"], 2)
            self.assertEqual(len(r["failed"]), 1)
            ckd = json.load(open(ck))
            self.assertEqual(ckd["next_step"], 1)
            # resume lanjut dari s2
            s2 = _Stub()
            r2 = autopilot.run(s2, self._plan(), checkpoint_path=ck,
                               resume=True)
            self.assertEqual(r2["report"][0]["name"], "s2")

    def test_on_error_continue(self):
        from kancil import autopilot
        with tempfile.TemporaryDirectory() as d:
            ck = os.path.join(d, "c.json")
            s = _Stub(fail_on={"click"})
            r = autopilot.run(s, self._plan(on_error="continue"),
                              checkpoint_path=ck)
            self.assertEqual(r["steps_ran"], 2)
            self.assertFalse(r["success"])

    def test_vault_restore_called(self):
        from kancil import autopilot
        with tempfile.TemporaryDirectory() as d:
            ck = os.path.join(d, "c.json")
            s = _Stub()
            autopilot.run(s, self._plan(vault="sesi-fb"),
                          checkpoint_path=ck)
            self.assertEqual(s.restored, "sesi-fb")

    def test_stuck_pause(self):
        from kancil import autopilot
        from kancil import stuck as _st

        class StuckStub(_Stub):
            def tabs(self):
                return {"tabs": []}

        # paksa check() mengembalikan captcha via monkeypatch
        orig = _st.check
        _st.check = lambda k: {"stuck": True, "kinds": ["captcha"],
                               "primary": "captcha", "url": ""}
        orig_resolve = _st.resolve
        _st.resolve = lambda k, kind=None: {"success": True,
                                            "resolved": False,
                                            "kind": "captcha",
                                            "needs_human": True,
                                            "hint": "manual"}
        try:
            with tempfile.TemporaryDirectory() as d:
                ck = os.path.join(d, "c.json")
                s = StuckStub()
                r = autopilot.run(s, self._plan(stuck="auto"),
                                  checkpoint_path=ck)
                self.assertFalse(r["success"])
                self.assertIsNotNone(r["paused"])
                self.assertEqual(r["paused"]["kind"], "captcha")
                self.assertEqual(s.calls, [])  # tidak ada step dieksekusi
        finally:
            _st.check = orig
            _st.resolve = orig_resolve


class WatchdogTest(unittest.TestCase):
    def test_probe_static_na(self):
        from kancil import watchdog
        from kancil.api import Kancil
        p = watchdog.probe(Kancil(engine="static"))
        self.assertTrue(p["responsive"])
        self.assertFalse(p["zombie_suspect"])

    def test_null_streak_tracking(self):
        from kancil.api import Kancil

        class E:
            def evaluate(self, js):
                return {"success": True, "result": None}

        k = Kancil(engine="static")
        k.engine = E()
        k.evaluate("1+1")
        k.evaluate("1+1")
        self.assertEqual(k._null_streak, 2)
        # ekspresi user yang null tidak dihitung
        k.evaluate("document.querySelector('.x')")
        self.assertEqual(k._null_streak, 2)

    def test_health_check_shape(self):
        from kancil.api import Kancil
        r = Kancil(engine="static").health_check()
        self.assertTrue(r["success"])
        self.assertIn("zombie_suspect", r)

    def test_recover_no_url(self):
        from kancil.api import Kancil
        k = Kancil(engine="static")

        class E:
            page = None

            def list_tabs(self):
                return []

        k.engine = E()
        r = k.recover_zombie()
        self.assertFalse(r["success"])


class SettledTest(unittest.TestCase):
    def test_static_always_settled(self):
        from kancil.api import Kancil
        r = Kancil(engine="static").wait_settled()
        self.assertTrue(r["success"])
        self.assertTrue(r["settled"])


class ReplayVariablesTest(unittest.TestCase):
    def test_variables_substitution(self):
        from kancil.api import Kancil
        k = Kancil(engine="static")
        seen = []
        orig_tool = k.tool
        k.tool = lambda payload: (seen.append(payload),
                                  {"success": True})[1]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.jsonl")
            with open(p, "w") as f:
                f.write(json.dumps(
                    {"action": "type",
                     "params": {"selector": "#q", "text": "{{kw}}"}}
                ) + "\n")
            r = k.session_replay(p, variables={"kw": "kancil browser"})
            self.assertTrue(r["success"])
            self.assertEqual(seen[0]["text"], "kancil browser")
            self.assertEqual(r["variables"], {"kw": "kancil browser"})
        k.tool = orig_tool


class ScrollUntilTest(unittest.TestCase):
    def test_needs_target(self):
        from kancil.api import Kancil
        r = Kancil(engine="static").scroll_until()
        self.assertFalse(r["success"])

    def test_found_immediately(self):
        from kancil.api import Kancil
        k = Kancil(engine="static")

        class P:
            text = "halo dunia kancil"
            dom = None

        class E:
            page = P()

            def scroll(self, target):
                return {"success": True, "delta": 0}

        k.engine = E()
        r = k.scroll_until(text="kancil", max_scrolls=3)
        self.assertTrue(r["success"])
        self.assertTrue(r["found"])
        self.assertEqual(r["scrolls"], 0)

    def test_exhausted(self):
        from kancil.api import Kancil
        k = Kancil(engine="static")

        class P:
            text = "halo dunia"
            dom = None

        class E:
            page = P()

            def scroll(self, target):
                return {"success": True, "delta": 100}

        k.engine = E()
        r = k.scroll_until(text="tidak-ada-xyz", max_scrolls=3)
        self.assertTrue(r["success"])
        self.assertFalse(r["found"])
        self.assertEqual(r["scrolls"], 3)


if __name__ == "__main__":
    unittest.main()
