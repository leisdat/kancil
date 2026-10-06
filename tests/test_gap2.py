"""Tests for the 2026-10-05 gap round 2:
wait_for, session_export/import, cookies_export_netscape, markdown(),
form_fill_submit, back/forward verify, rate-limit-aware retry.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from kancil.api import Kancil
from kancil.markdown import html_to_markdown
from kancil import engines


# ---------- markdown converter ----------

class TestHtmlToMarkdown(unittest.TestCase):
    def test_headings_paragraph(self):
        md, _ = html_to_markdown(
            "<h1>Title</h1><p>Hello <strong>world</strong>.</p>")
        self.assertIn("# Title", md)
        self.assertIn("Hello **world**.", md)

    def test_links_images(self):
        md, _ = html_to_markdown(
            '<p>see <a href="https://ex.com">this</a> '
            '<img src="i.png" alt="pic"></p>')
        self.assertIn("[this](https://ex.com)", md)
        self.assertIn("![pic](i.png)", md)

    def test_lists(self):
        md, _ = html_to_markdown("<ul><li>a</li><li>b</li></ul>")
        self.assertIn("- a", md)
        self.assertIn("- b", md)

    def test_table(self):
        md, _ = html_to_markdown(
            "<table><tr><th>A</th><th>B</th></tr>"
            "<tr><td>1</td><td>2</td></tr></table>")
        self.assertIn("| A | B |", md)
        self.assertIn("| --- | --- |", md)
        self.assertIn("| 1 | 2 |", md)

    def test_code_pre(self):
        md, _ = html_to_markdown("<p>run <code>x()</code></p><pre>y = 2</pre>")
        self.assertIn("`x()`", md)
        self.assertIn("```\ny = 2\n```", md)

    def test_skips_noise(self):
        md, _ = html_to_markdown(
            "<head><title>T</title></head>"
            "<script>var x=1</script><nav>menu</nav><p>real</p>")
        self.assertNotIn("var x=1", md)
        self.assertNotIn("menu", md)
        self.assertNotIn("\nT\n", md)
        self.assertIn("real", md)

    def test_truncation(self):
        md, stats = html_to_markdown("<p>" + "x" * 100 + "</p>",
                                     max_chars=10)
        self.assertTrue(stats["truncated"])
        self.assertIn("[truncated]", md)


# ---------- rate-limit retry ----------

class TestParseRetryAfter(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(engines._parse_retry_after("120"), 120.0)

    def test_http_date_future(self):
        self.assertGreater(engines._parse_retry_after(
            "Wed, 21 Oct 2026 07:28:00 GMT"), 0)

    def test_garbage(self):
        self.assertIsNone(engines._parse_retry_after("soon"))
        self.assertIsNone(engines._parse_retry_after(None))
        self.assertIsNone(engines._parse_retry_after(""))


class TestRateLimitRetry(unittest.TestCase):
    def _engine_429_then_ok(self, retry_after="1"):
        import urllib.error
        eng = engines.StaticEngine.__new__(engines.StaticEngine)
        eng.retries = 2
        eng.timeout = 5
        eng.ua = "kancil-test"
        eng.BROWSER_HEADERS = {}
        eng.cache_enabled = False
        eng.log = []
        eng.errors = []
        eng.opener = mock.Mock()
        calls = {"n": 0}

        def fake_open(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError(
                    req.full_url, 429, "Too Many Requests",
                    {"Retry-After": retry_after}, None)
            m = mock.MagicMock()
            m.geturl.return_value = req.full_url
            m.status = 200
            m.headers.get_content_type.return_value = "text/html"
            m.headers.get.return_value = ""
            m.headers.items.return_value = []
            m.read.return_value = b"<html><body>ok</body></html>"
            m.__enter__.return_value = m
            return m

        eng.opener.open.side_effect = fake_open
        eng._log = lambda e: eng.log.append(e)
        eng._decode_body = lambda raw, enc: raw
        eng._body_preview = lambda raw: (raw[:100], False)
        return eng, calls

    def test_429_retries_after_cooldown(self):
        eng, calls = self._engine_429_then_ok("0")
        with mock.patch("time.sleep") as sl:
            url, status, ctype, raw, rh = eng.fetch("http://ex.com/")
        self.assertEqual(calls["n"], 2)
        self.assertEqual(status, 200)
        sl.assert_called_once()
        rl = [e for e in eng.errors if e["type"] == "rate_limit"]
        self.assertEqual(len(rl), 1)
        self.assertEqual(rl[0]["status"], 429)
        self.assertIn("rate_limited", eng.log[0])

    def test_429_no_retry_after_uses_backoff(self):
        eng, calls = self._engine_429_then_ok(None)
        with mock.patch("time.sleep") as sl:
            eng.fetch("http://ex.com/")
        self.assertEqual(calls["n"], 2)
        sl.assert_called_once()


# ---------- api-level: fake engines ----------

class FakeWebviewEngine:
    """wait_for / cookies_dump+load / storage / tabs for session tests."""
    def __init__(self):
        self.tabs_data = [
            {"id": 1, "url": "https://a.com/", "current": True},
            {"id": 2, "url": "https://b.com/x", "current": False},
        ]
        self.current = 1
        self.storage_data = {"https://a.com/": {"tok": "abc"},
                             "https://b.com/x": {}}
        self.loaded = {}

    # wait_for
    def wait_for(self, expr, timeout_ms=10000, poll_ms=300):
        return {"success": True, "matches": True, "waited_ms": 120,
                "value": "true"}

    # cookies
    def cookies_dump(self):
        return {"success": True,
                "dumps": {"https://a.com/": "sid=1; theme=dark"}}

    def cookies_load(self, dumps):
        self.loaded = dumps
        return {"success": True, "loaded": sum(
            d.count("=") for d in dumps.values())}

    # tabs
    def list_tabs(self):
        out = []
        for t in self.tabs_data:
            d = dict(t)
            d["current"] = (t["id"] == self.current)
            out.append(d)
        return out

    def switch_tab(self, tid):
        self.current = tid
        return {"success": True}

    def new_tab(self, url=None):
        tid = 99
        self.tabs_data.append({"id": tid, "url": url or "about:blank"})
        self.current = tid
        return {"success": True, "tab": tid}

    def close_tab(self, tid):
        self.tabs_data = [t for t in self.tabs_data if t["id"] != tid]
        return {"success": True}

    def wait_idle(self, timeout=10):
        return {"success": True}

    def reload(self):
        return {"success": True}

    # storage
    def storage(self, kind="local"):
        cur = next(t for t in self.tabs_data if t["id"] == self.current)
        return dict(self.storage_data.get(cur["url"], {}))

    def storage_set(self, key, value, kind="local"):
        return {"success": True}


def make_kancil(engine):
    k = Kancil.__new__(Kancil)
    k.engine = engine
    k._engine_name = "webview"
    k.dry_run = False
    return k


class TestWaitForApi(unittest.TestCase):
    def test_ok(self):
        k = make_kancil(FakeWebviewEngine())
        r = k.wait_for("!document.querySelector('.x')")
        self.assertTrue(r["success"])
        self.assertTrue(r["matches"])

    def test_needs_webview(self):
        k = make_kancil(object())
        r = k.wait_for("true")
        self.assertFalse(r["success"])
        self.assertIn("1.27", r["errors"][0])


class TestSessionExportImport(unittest.TestCase):
    def test_roundtrip(self):
        eng = FakeWebviewEngine()
        k = make_kancil(eng)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            r = k.session_export(p)
            self.assertTrue(r["success"], r)
            self.assertEqual(r["cookie_origins"], 1)
            self.assertEqual(r["storage_origins"], 1)
            data = json.load(open(p))
            self.assertEqual(data["kancil_session"], 1)
            self.assertIn("https://a.com/", data["cookies"])
            self.assertEqual(data["storage"]["https://a.com/"],
                             {"tok": "abc"})
            # import into a fresh engine
            eng2 = FakeWebviewEngine()
            k2 = make_kancil(eng2)
            r2 = k2.session_import(p)
            self.assertTrue(r2["success"], r2)
            self.assertGreater(r2["cookies_loaded"], 0)
            self.assertEqual(eng2.loaded["https://a.com/"],
                             "sid=1; theme=dark")
            self.assertEqual(len(r2["storage_injected"]), 1)

    def test_bad_file(self):
        k = make_kancil(FakeWebviewEngine())
        r = k.session_import("/nonexistent/x.json")
        self.assertFalse(r["success"])


class TestNetscapeExport(unittest.TestCase):
    def test_format(self):
        k = make_kancil(FakeWebviewEngine())
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "cookies.txt")
            r = k.cookies_export_netscape(p)
            self.assertTrue(r["success"], r)
            self.assertEqual(r["cookies"], 2)
            lines = open(p).read().splitlines()
            self.assertTrue(lines[0].startswith("# Netscape HTTP Cookie File"))
            body = [l for l in lines if not l.startswith("#")]
            self.assertEqual(len(body), 2)
            f = body[0].split("\t")
            self.assertEqual(len(f), 7)
            self.assertEqual(f[0], "a.com")
            self.assertEqual(f[5], "sid")


class TestFormFillSubmit(unittest.TestCase):
    def test_fill_submit_verify(self):
        k = make_kancil(FakeWebviewEngine())
        k.form_fill = lambda fid, values=None, auto=False: {
            "success": True, "filled": ["q"]}
        k.form_submit = lambda fid, confirm=False: {"success": True}
        k.wait = lambda selector=None, text=None, ms=None: {"success": True}
        r = k.form_fill_submit(1, values={"q": "x"}, verify="#ok")
        self.assertTrue(r["success"])
        self.assertTrue(r["filled"])
        self.assertTrue(r["submitted"])
        self.assertEqual(r["verified"], "#ok")

    def test_verify_failed(self):
        k = make_kancil(FakeWebviewEngine())
        k.form_fill = lambda fid, values=None, auto=False: {"success": True}
        k.form_submit = lambda fid, confirm=False: {"success": True}
        k.wait = lambda selector=None, text=None, ms=None: {
            "success": False, "errors": ["timeout"]}
        r = k.form_fill_submit(1, verify="#nope")
        self.assertFalse(r["success"])
        self.assertTrue(r["submitted"])  # submit happened, verify failed


class TestBackForwardVerify(unittest.TestCase):
    def _k(self, wait_ok):
        eng = mock.Mock()
        eng.back.return_value = {"success": True, "url": "https://a.com/"}
        eng.forward.return_value = {"success": True, "url": "https://b.com/"}
        k = make_kancil(eng)
        k.wait = lambda selector=None, text=None, ms=None: (
            {"success": True} if wait_ok
            else {"success": False, "errors": ["timeout"]})
        # bypass _with_delta wrapping of _wrap for simplicity
        return k

    def test_back_plain(self):
        k = self._k(True)
        r = k.back()
        self.assertTrue(r["success"])

    def test_back_verified(self):
        k = self._k(True)
        r = k.back(verify="#main")
        self.assertTrue(r["success"])
        self.assertEqual(r["verified"], "#main")

    def test_back_verify_failed(self):
        k = self._k(False)
        r = k.back(verify="#main")
        self.assertFalse(r["success"])
        self.assertIn("back verify failed", r["errors"][0])

    def test_forward_verify_text(self):
        k = self._k(True)
        r = k.forward(verify_text="hello")
        self.assertTrue(r["success"])
        self.assertEqual(r["verified"], "hello")


class TestMarkdownApi(unittest.TestCase):
    def test_static_page(self):
        eng = engines.StaticEngine.__new__(engines.StaticEngine)
        page = engines.Page("http://ex.com/", 200,
                            b"<h1>Hi</h1><p>Body <b>text</b>.</p>",
                            "text/html")
        # StaticEngine.page is a read-only property -> stub at class level
        with mock.patch.object(engines.StaticEngine, "page",
                               new_callable=mock.PropertyMock) as mp:
            mp.return_value = page
            k = Kancil.__new__(Kancil)
            k.engine = eng
            k._engine_name = "static"
            r = k.markdown()
        self.assertTrue(r["success"], r)
        self.assertIn("# Hi", r["markdown"])
        self.assertIn("Body **text**.", r["markdown"])


class TestManifestNew(unittest.TestCase):
    def test_all_present(self):
        acts = Kancil._TOOL_ACTIONS
        for a in ("wait_for", "session_export", "session_import",
                  "form_fill_submit", "cookies_export_netscape", "markdown"):
            self.assertIn(a, acts)
            m = Kancil._action_meta(a)
            self.assertTrue(m["description"])
            self.assertTrue(m["returns"])

    def test_total(self):
        self.assertEqual(len(Kancil._TOOL_ACTIONS), 130)


if __name__ == "__main__":
    unittest.main()
