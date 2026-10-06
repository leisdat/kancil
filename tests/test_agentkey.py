"""Tests for agent API key auth (APK 1.28+): kancil/agent_key.py,
X-Kancil-Key header, 401 handling, am start extra, sync.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from kancil import agent_key as ak
from kancil.webview_engine import WebViewEngine as WebviewEngine
from kancil.engines import EngineError


def make_engine(key="testkey123"):
    e = WebviewEngine.__new__(WebviewEngine)
    e.base = "http://127.0.0.1:8080"
    e.timeout = 5
    e.auto_launch = False
    e._healing = False
    e._resyncing = False
    e._api_key = key
    e.errors = []
    return e


class TestAgentKeyModule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp.name

    def tearDown(self):
        if self._home is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = self._home
        self.tmp.cleanup()

    def test_create_and_persist(self):
        k1 = ak.get_or_create()
        self.assertTrue(len(k1) >= 32)
        self.assertEqual(ak.get_or_create(), k1)
        self.assertEqual(ak.read(), k1)

    def test_mode_0600(self):
        ak.get_or_create()
        mode = os.stat(ak.key_path()).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        self.assertTrue(ak.key_mode_ok())

    def test_regenerate(self):
        k1 = ak.get_or_create()
        k2 = ak.regenerate()
        self.assertNotEqual(k1, k2)
        self.assertEqual(ak.read(), k2)

    def test_read_missing(self):
        self.assertIsNone(ak.read())


class TestApiKeyHeader(unittest.TestCase):
    def test_header_sent(self):
        e = make_engine("sekret")
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["key"] = req.get_header("X-kancil-key")
            m = mock.MagicMock()
            m.read.return_value = b'{"ok": true}'
            m.headers.get.return_value = "application/json"
            m.__enter__.return_value = m
            return m

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            r = e._req("GET", "/status")
        self.assertEqual(r, {"ok": True})
        self.assertEqual(seen["key"], "sekret")

    def test_401_raises_with_sync_hint(self):
        import urllib.error
        e = make_engine("wrong")  # auto_launch=False -> no auto-heal

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized",
                                         {}, None)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with mock.patch.object(e, "sync_agent_key") as sy:
                with self.assertRaises(EngineError) as cm:
                    e._req("GET", "/status")
        self.assertIn("agent-key sync", str(cm.exception))
        sy.assert_not_called()

    def test_401_auto_sync_then_retry_ok(self):
        import urllib.error
        e = make_engine("k1")
        e.auto_launch = True
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(req.full_url, 401,
                                             "Unauthorized", {}, None)
            m = mock.MagicMock()
            m.read.return_value = b'{"ok": true}'
            m.headers.get.return_value = "application/json"
            m.__enter__.return_value = m
            return m

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with mock.patch.object(
                    e, "sync_agent_key",
                    return_value={"success": True,
                                 "key_synced": True}) as sy:
                r = e._req("GET", "/status")
        self.assertEqual(r, {"ok": True})
        sy.assert_called_once()
        self.assertEqual(calls["n"], 2)

    def test_401_still_401_after_sync_raises_once(self):
        import urllib.error
        e = make_engine("k1")
        e.auto_launch = True

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized",
                                         {}, None)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with mock.patch.object(
                    e, "sync_agent_key",
                    return_value={"success": False}) as sy:
                with self.assertRaises(EngineError):
                    e._req("GET", "/status")
        sy.assert_called_once()  # exactly one sync attempt, no loop

    def test_401_inside_sync_does_not_recurse(self):
        import urllib.error
        e = make_engine("k1")
        e.auto_launch = True
        e._resyncing = True  # simulate being inside sync_agent_key

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized",
                                         {}, None)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with mock.patch.object(e, "sync_agent_key") as sy:
                with self.assertRaises(EngineError):
                    e._req("GET", "/status")
        sy.assert_not_called()

    def test_no_key_no_header(self):
        e = make_engine(None)
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["key"] = req.get_header("X-kancil-key")
            m = mock.MagicMock()
            m.read.return_value = b'{"ok": true}'
            m.headers.get.return_value = "application/json"
            m.__enter__.return_value = m
            return m

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            e._req("GET", "/status")
        self.assertIsNone(seen["key"])


class TestAmStartArgs(unittest.TestCase):
    def test_extra_included(self):
        e = make_engine("kunci-rahasia")
        args = e._am_start_args()
        self.assertIn("com.kancil.browser/.MainActivity", args)
        self.assertIn("--es", args)
        i = args.index("--es")
        self.assertEqual(args[i + 1], "kancil_agent_key")
        self.assertEqual(args[i + 2], "kunci-rahasia")

    def test_sync_force_stops(self):
        e = make_engine("k1")
        calls = []
        with mock.patch("subprocess.run",
                         lambda *a, **k: calls.append(a[0]) or mock.Mock()):
            with mock.patch.object(e, "ensure_alive",
                                   return_value={"success": True}) as ea:
                r = e.sync_agent_key()
        self.assertTrue(r["success"])
        self.assertTrue(r["key_synced"])
        self.assertIn(["am", "force-stop", "com.kancil.browser"], calls)
        ea.assert_called_once()


class TestAgentKeyApi(unittest.TestCase):
    def test_manifest(self):
        from kancil.api import Kancil
        self.assertIn("agent_key", Kancil._TOOL_ACTIONS)
        m = Kancil._action_meta("agent_key")
        self.assertEqual(m["engines"], ["webview"])
        self.assertEqual(len(Kancil._TOOL_ACTIONS), 141)

    def test_show(self):
        from kancil.api import Kancil
        k = Kancil.__new__(Kancil)
        k._engine_name = "webview"
        eng = mock.Mock()
        eng.sync_agent_key = mock.Mock(return_value={"success": True})
        k.engine = eng
        k._wrap = lambda r: r
        with mock.patch.dict(os.environ, {"HOME": tempfile.mkdtemp()}):
            r = k.agent_key("show")
        self.assertTrue(r["success"])
        self.assertTrue(r["key"])
        self.assertTrue(r["mode_ok"])


if __name__ == "__main__":
    unittest.main()
