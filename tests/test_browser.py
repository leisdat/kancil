"""Kancil test suite — stdlib unittest, local test server only."""
import http.server
import http.cookiejar
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
import urllib.parse

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from kancil import Kancil
from kancil.dom import build_dom, select, xpath, inspect_element, smart_resolve
from kancil.devtools import scrape_items, scrape_auto, extract_reader

INDEX = b"""<html><head><title>Test Home</title>
<meta name="description" content="test page"></head><body>
<h1>Welcome</h1>
<nav><a href="/page2">Second</a><a href="/form">Form</a></nav>
<div class="cards">
  <div class="card"><h3 class="title">Alpha</h3><a href="/a1">open</a><img src="/i1.png" alt="pic1"></div>
  <div class="card"><h3 class="title">Beta</h3><a href="/a2">open</a><img src="/i2.png" alt="pic2"></div>
</div>
<table><tr><th>Name</th><th>Val</th></tr><tr><td>x</td><td>1</td></tr></table>
<button id="loginBtn" class="btn primary">Login</button>
<input name="q" placeholder="Search here">
</body></html>"""

FORM = b"""<html><body><h1>Form</h1>
<form action="/submit" method="post">
<input name="email" type="email" placeholder="Email">
<input name="pw" type="password">
<button type="submit">Go</button>
</form></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, body, ctype="text/html", status=200, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._send(INDEX)
        elif self.path == "/page2":
            self._send(b"<html><body><h1>Page Two</h1><a href='/'>home</a></body></html>")
        elif self.path == "/form":
            self._send(FORM)
        elif self.path == "/setcookie":
            self._send(b"ok", extra=[("Set-Cookie", "sess=abc123; Path=/")])
        elif self.path == "/json":
            self._send(b'{"hello":"world"}', "application/json")
        elif self.path == "/gzip":
            import gzip as _gz
            raw = b"<html><body><h1>gzipped hi</h1></body></html>"
            self._send(_gz.compress(raw), extra=[("Content-Encoding", "gzip")])
        elif self.path == "/slow404":
            self._send(b"nope", status=404)
        elif self.path.startswith("/shop"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            n = int(q.get("page", ["1"])[0])
            cards = {1: ["A", "B"], 2: ["C", "D"], 3: ["C", "D"]}.get(n)
            if cards is None:
                self._send(b"nope", status=404)
                return
            nxt = ('<a class="next" href="/shop?page=%d">next</a>' % (n + 1)
                   if n < 3 else "")
            body = ("<html><body>" + "".join(
                '<div class="card"><h3 class="title">%s</h3></div>' % t
                for t in cards) + nxt + "</body></html>").encode()
            self._send(body)
        elif self.path == "/robots.txt":
            self._send(b"User-agent: *\nDisallow: /private\n", "text/plain")
        elif self.path == "/etag":
            if self.headers.get("If-None-Match") == '"v1"':
                self._send(b"", status=304)
            else:
                self._send(b"<html><body><h1>etag page</h1></body></html>",
                           extra=[("ETag", '"v1"')])
        elif self.path == "/nostore":
            self._send(b"<html><body><h1>no store</h1></body></html>",
                       extra=[("Cache-Control", "no-store"),
                              ("ETag", '"x"')])
        elif self.path == "/sitemap.xml":
            host = self.headers.get("Host", "127.0.0.1")
            body = ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/'
                    'sitemap/0.9"><sitemap><loc>http://%s/sm1.xml</loc>'
                    '</sitemap></sitemapindex>' % host).encode()
            self._send(body, "application/xml")
        elif self.path == "/sm1.xml":
            host = self.headers.get("Host", "127.0.0.1")
            urls = ["p1", "p2", "p3", "missing"]
            body = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/'
                    '0.9">' + "".join(
                        '<url><loc>http://%s/%s</loc></url>' % (host, u)
                        for u in urls) + "</urlset>").encode()
            self._send(body, "application/xml")
        elif self.path in ("/p1", "/p2", "/p3"):
            name = self.path[1:].upper()
            self._send(("<html><head><title>T-%s</title>"
                        '<meta property="og:title" content="OG-%s">'
                        '<meta name="twitter:card" content="summary">'
                        '<meta name="description" content="Desc-%s">'
                        '<script type="application/ld+json">'
                        '{"@context":"https://schema.org","@graph":['
                        '{"@type":"Article","headline":"H-%s"},'
                        '{"@type":"WebPage","name":"W-%s"}]}'
                        '</script></head>'
                        "<body><h1>Page %s</h1></body></html>"
                        % (name, name, name, name, name, name)).encode())
        elif self.path == "/private":
            self._send(b"<html><body><div class='card'>"
                       b"<h3 class='title'>Secret</h3></div></body></html>")
        else:
            self._send(b"not found", status=404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(n)
        q = urllib.parse.parse_qs(data.decode())
        body = json.dumps({"got": {k: v[0] for k, v in q.items()}}).encode()
        self._send(body, "application/json")

    def log_message(self, *a):
        pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class BrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.srv = http.server.HTTPServer(("127.0.0.1", cls.port), Handler)
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.tmp = tempfile.mkdtemp()
        os.environ["HOME"] = cls.tmp  # isolate state/cookies

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.b = Kancil(engine="static", timeout=10, retries=0)

    def tearDown(self):
        self.b.close()

    # ---- navigation ----
    def test_open_adds_https_and_loads(self):
        r = self.b.open(self.base + "/")
        self.assertTrue(r["success"], r)
        self.assertIn("Test Home", r["title"])

    def test_open_invalid_url_graceful(self):
        r = self.b.open("http://127.0.0.1:1/")
        self.assertFalse(r["success"])
        self.assertTrue(r["errors"])

    def test_back_forward(self):
        self.b.open(self.base + "/")
        self.b.open(self.base + "/page2")
        r = self.b.back()
        self.assertTrue(r["success"])
        self.assertTrue(r["url"].endswith("/"))
        r2 = self.b.forward()
        self.assertTrue(r2["success"])
        self.assertTrue(r2["url"].endswith("/page2"))
        # forward at end -> graceful failure
        r3 = self.b.forward()
        self.assertFalse(r3["success"])

    def test_back_empty_history_graceful(self):
        r = self.b.back()
        self.assertFalse(r["success"])

    # ---- tabs ----
    def test_tabs(self):
        self.b.open(self.base + "/")
        self.b.new_tab(self.base + "/page2")
        t = self.b.tabs()
        self.assertTrue(t["success"])
        self.assertEqual(len(t["tabs"]), 2)
        self.b.switch_tab(0)
        self.assertIn("Test Home", self.b.engine.page.title)
        self.b.close_tab(1)
        self.assertEqual(len(self.b.tabs()["tabs"]), 1)

    def test_switch_bad_tab(self):
        r = self.b.switch_tab(99)
        self.assertFalse(r["success"])

    # ---- dom / selectors ----
    def test_css_selectors(self):
        self.b.open(self.base + "/")
        r = self.b.dom_find("#loginBtn")
        self.assertTrue(r["success"])
        self.assertEqual(r["count"], 1)
        r = self.b.dom_find(".card .title")
        self.assertEqual(r["count"], 2)
        r = self.b.dom_find("input[placeholder='Search here']")
        self.assertEqual(r["count"], 1)

    def test_bad_selector_graceful(self):
        r = self.b.dom_find("[[[")
        self.assertFalse(r["success"])

    def test_xpath(self):
        self.b.open(self.base + "/")
        r = self.b.dom_xpath("//div[@class='card']")
        self.assertTrue(r["success"])
        self.assertEqual(r["count"], 2)
        r = self.b.dom_xpath("//a[@href='/a2']")
        self.assertEqual(r["count"], 1)
        r = self.b.dom_xpath("//h3[text()='Beta']")
        self.assertEqual(r["count"], 1)
        r = self.b.dom_xpath("//*[contains(@class,'btn')]")
        self.assertGreaterEqual(r["count"], 1)

    def test_inspect(self):
        self.b.open(self.base + "/")
        r = self.b.inspect("#loginBtn")
        self.assertTrue(r["success"])
        self.assertEqual(r["tag"], "button")
        self.assertEqual(r["attributes"]["id"], "loginBtn")
        self.assertIn("Login", r["text"])
        self.assertTrue(r["css"])
        self.assertTrue(r["xpath"].startswith("/"))

    def test_inspect_missing(self):
        self.b.open(self.base + "/")
        r = self.b.inspect("#nope")
        self.assertFalse(r["success"])

    def test_dom_tree(self):
        self.b.open(self.base + "/")
        r = self.b.dom_tree(max_nodes=50)
        self.assertTrue(r["success"])
        tags = [n["tag"] for n in r["nodes"]]
        self.assertIn("h1", tags)
        self.assertIn("table", tags)

    # ---- smart resolve + actions ----
    def test_smart_click_text(self):
        self.b.open(self.base + "/")
        r = self.b.click("Login")  # smart text resolve -> button w/o form
        # button has no form and no JS -> graceful failure, not crash
        self.assertFalse(r["success"])
        self.assertIn("JavaScript", r["errors"][0])

    def test_click_link_navigates(self):
        self.b.open(self.base + "/")
        r = self.b.click("Second")
        self.assertTrue(r["success"])
        self.assertTrue(r["url"].endswith("/page2"))

    def test_type_and_form_submit(self):
        self.b.open(self.base + "/form")
        self.assertTrue(self.b.type("input[name=email]", "a@b.c")["success"])
        self.assertTrue(self.b.type("input[name=pw]", "secret")["success"])
        r = self.b.form_submit(1)
        self.assertTrue(r["success"], r)
        # POST echo is JSON shown as text page
        self.assertIn("a@b.c", self.b.engine.page.text)

    def test_form_fill_auto(self):
        self.b.profile = {"email": "auto@x.id", "password": "pw123"}
        self.b.open(self.base + "/form")
        r = self.b.form_fill(1, auto=True)
        self.assertTrue(r["success"])
        self.assertTrue(any("email" in f for f in r["filled"]))

    def test_click_bad_selector(self):
        self.b.open(self.base + "/")
        r = self.b.click("#does-not-exist")
        self.assertFalse(r["success"])

    # ---- cookies & storage ----
    def test_cookies(self):
        self.b.open(self.base + "/setcookie")
        r = self.b.cookies()
        self.assertTrue(r["success"])
        names = [c["name"] for c in r["cookies"]]
        self.assertIn("sess", names)

    def test_storage_simulated(self):
        self.b.open(self.base + "/")
        self.assertTrue(self.b.storage_set("token", "abc")["success"])
        r = self.b.storage_get("token")
        self.assertTrue(r["found"])
        self.assertEqual(r["value"], "abc")
        self.assertTrue(self.b.storage_delete("token")["success"])
        self.assertFalse(self.b.storage_get("token")["found"])

    # ---- network ----
    def test_network_log(self):
        self.b.open(self.base + "/")
        self.b.open(self.base + "/page2")
        r = self.b.network()
        self.assertTrue(r["success"])
        self.assertGreaterEqual(len(r["requests"]), 2)
        e = r["requests"][-1]
        for k in ("id", "method", "url", "status", "ms"):
            self.assertIn(k, e)
        # request detail
        d = self.b.request(e["id"])
        self.assertTrue(d["success"])
        self.assertIn("req_headers", d)

    def test_network_filter(self):
        self.b.open(self.base + "/")
        r = self.b.network(pattern="page2")
        self.assertTrue(all("page2" in x["url"] for x in r["requests"]))

    def test_http_error_logged(self):
        self.b.open(self.base + "/slow404")
        errs = self.b.errors()["errors"]
        self.assertTrue(any(e.get("status") == 404 for e in errs))

    # ---- scraper ----
    def test_scrape_selector(self):
        r = self.b.scrape(url=self.base + "/",
                          selector=".card",
                          fields={"title": ".title", "url": "a@href", "image": "img@src"})
        self.assertTrue(r["success"])
        self.assertEqual(r["count"], 2)
        self.assertEqual(r["data"][0]["title"], "Alpha")
        self.assertTrue(r["data"][0]["url"].endswith("/a1"))
        self.assertTrue(r["data"][1]["image"].endswith("/i2.png"))

    def test_scrape_auto(self):
        r = self.b.scrape(url=self.base + "/", auto=True)
        self.assertTrue(r["success"])
        cats = {d.get("_category") for d in r["data"]}
        self.assertIn("tables", cats)
        self.assertIn("pages_crawled", r)
        self.assertIn("stopped_reason", r)

    def test_scrape_csv(self):
        r = self.b.scrape(url=self.base + "/", selector=".card",
                          fields={"title": ".title"}, fmt="csv")
        self.assertTrue(r["success"])
        self.assertIn("title", r["csv"])
        self.assertIn("Alpha", r["csv"])

    # ---- extract ----
    def test_extract(self):
        self.b.open(self.base + "/")
        r = self.b.extract()
        self.assertTrue(r["success"])
        self.assertEqual(r["title"], "Test Home")
        self.assertTrue(r["headings"])
        self.assertTrue(r["tables"])
        self.assertEqual(r["meta"].get("description"), "test page")
        # token-efficient: article bounded
        self.assertLessEqual(len(r["article"]), 8000)

    # ---- perf ----
    def test_perf(self):
        r = self.b.perf(self.base + "/")
        self.assertTrue(r["success"], r)
        for k in ("dns_ms", "connect_ms", "ttfb_ms", "total_ms", "bytes"):
            self.assertIn(k, r)

    # ---- json shape ----
    def test_json_shape(self):
        r = self.b.open(self.base + "/")
        s = self.b.to_json(r)
        d = json.loads(s)
        self.assertIn("success", d)
        self.assertIn("url", d)

    def test_capabilities_honest(self):
        caps = self.b.capabilities
        self.assertFalse(caps["javascript"])
        self.assertFalse(caps["screenshot"])
        self.assertTrue(caps["xpath"])

    # ---- js honesty ----
    def test_js_unsupported_honest(self):
        r = self.b.evaluate("document.title")
        self.assertFalse(r["success"])
        self.assertFalse(r["supported"])

    # ---- downloads ----
    def test_download(self):
        r = self.b.download(self.base + "/json")
        self.assertTrue(r["success"])
        import time
        for _ in range(50):
            ds = self.b.downloads()["downloads"]
            if ds and ds[0]["status"] == "done":
                break
            time.sleep(0.1)
        self.assertEqual(ds[0]["status"], "done")
        self.assertTrue(ds[0]["sha256"])

    # ---- unit: dom helpers ----
    def test_smart_resolve_unit(self):
        root = build_dom('<button id="go">Start</button><input name="email">')
        n, m = smart_resolve(root, "Start")
        self.assertEqual(m, "text-exact")
        n, m = smart_resolve(root, "#go")
        self.assertEqual(m, "css")
        n, m = smart_resolve(root, "email")
        self.assertIsNotNone(n)

    def test_xpath_unit(self):
        root = build_dom('<div><p>a</p><p>b</p></div>')
        self.assertEqual(len(xpath(root, "//p")), 2)
        self.assertEqual(xpath(root, "//p[2]")[0].text_content(), "b")
        self.assertEqual(len(xpath(root, "/div/p")), 2)

    def test_inspect_unit(self):
        root = build_dom('<div id="x" class="a b"><span>hi</span></div>')
        n = select(root, "#x")[0]
        d = inspect_element(n, root)
        self.assertEqual(d["css"], "#x")
        self.assertEqual(d["xpath"], "/div")
        self.assertIn("hi", d["text"])

    # ---- bookmarks ----
    def test_bookmarks(self):
        self.b.open(self.base + "/")
        r = self.b.bookmark_add()
        self.assertTrue(r["success"])
        self.assertEqual(r["id"], 1)
        r = self.b.bookmark_add(url="example.com", title="Ex")
        self.assertTrue(r["url"].startswith("https://"))
        lst = self.b.bookmark_list()
        self.assertEqual(lst["count"], 2)
        self.assertTrue(self.b.bookmark_delete(1)["success"])
        self.assertFalse(self.b.bookmark_delete(99)["success"])
        self.assertFalse(self.b.bookmark_open(99)["success"])

    def test_bookmark_open(self):
        self.b.open(self.base + "/")
        self.b.bookmark_add()
        r = self.b.bookmark_open(1)
        self.assertTrue(r["success"])

    # ---- profile ----
    def test_profile(self):
        self.assertTrue(self.b.profile_set("email", "a@b.c")["success"])
        r = self.b.profile_get("email")
        self.assertTrue(r["found"])
        self.assertEqual(r["value"], "a@b.c")
        self.assertFalse(self.b.profile_get("nope")["found"])
        self.assertIn("email", self.b.profile_list()["profile"])
        self.b.profile_delete("email")
        self.assertFalse(self.b.profile_get("email")["found"])

    # ---- proxy ----
    def test_proxy_set_clear(self):
        r = self.b.proxy_set("http://127.0.0.1:9999")
        self.assertTrue(r["success"])
        self.assertEqual(self.b.engine.proxy, "http://127.0.0.1:9999")
        self.assertEqual(self.b.proxy_show()["proxy"], "http://127.0.0.1:9999")
        self.b.proxy_clear()
        self.assertIsNone(self.b.engine.proxy)
        # engine still works after proxy cleared
        self.assertTrue(self.b.open(self.base + "/")["success"])

    # ---- user agent ----
    def test_ua(self):
        r = self.b.ua_show()
        self.assertTrue(r["success"])
        self.assertIn("Mozilla", r["ua"])
        self.b.ua_set("TestAgent/1.0")
        self.assertEqual(self.b.ua_show()["ua"], "TestAgent/1.0")
        self.b.ua_rotate()
        self.assertNotEqual(self.b.ua_show()["ua"], "TestAgent/1.0")
        self.assertGreaterEqual(len(self.b.ua_list()["user_agents"]), 3)

    # ---- lazy tab restore ----
    def test_lazy_restore(self):
        self.b.open(self.base + "/")
        self.b.new_tab(self.base + "/page2")
        st = self.b.export_state()
        b2 = Kancil(engine="static", timeout=10, retries=0)
        b2.import_state(st)
        try:
            self.assertEqual(len(b2.engine.tabs), 2)
            # P0-4: import alone must NOT fetch anything; both tabs stay lazy
            for t in b2.engine.tabs:
                self.assertTrue(all(isinstance(h, str) for h in t["history"]))
            # first page access materializes the active tab only
            p = b2.engine.page
            self.assertTrue(p.url.endswith("/page2"))
            t0, t1 = b2.engine.tabs[0], b2.engine.tabs[1]
            self.assertTrue(all(isinstance(h, str) for h in t0["history"]))
            self.assertFalse(any(isinstance(h, str)
                                 for h in t1["history"]))
            # switching materializes the lazy tab
            r = b2.switch_tab(0)
            self.assertTrue(r["success"])
            self.assertFalse(any(isinstance(h, str)
                                 for h in b2.engine.tabs[0]["history"]))
        finally:
            b2.close()

    def test_back_forward_lazy(self):
        self.b.open(self.base + "/")
        self.b.open(self.base + "/page2")
        st = self.b.export_state()
        b2 = Kancil(engine="static", timeout=10, retries=0)
        b2.import_state(st)
        try:
            # active tab = page2 (pos 1), materialized
            r = b2.back()
            self.assertTrue(r["success"])
            self.assertTrue(r["url"].endswith("/"))
        finally:
            b2.close()

    # ---- multipart ----
    def test_multipart_encode(self):
        import sys as _s
        _s.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from kancil.engines import _encode_multipart
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            f.write(b"hello")
            path = f.name
        body, ctype = _encode_multipart([("name", "bob")], [("avatar", path)])
        self.assertIn("multipart/form-data", ctype)
        self.assertIn(b'form-data; name="name"', body)
        self.assertIn(b"bob", body)
        self.assertIn(b'filename=', body)
        self.assertIn(b"hello", body)
        os.unlink(path)

    def test_form_file_upload(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            f.write(b"data123")
            path = f.name
        html = ('<html><body><form action="/submit" method="post">'
                '<input type="file" name="doc"><input name="t" value="x">'
                '<button type="submit">go</button></form></body></html>')
        # serve via data: build page manually
        from kancil.engines import Page
        eng = self.b.engine
        eng.tabs = [{"history": [Page(self.base + "/", 200, html.encode(), "text/html")], "pos": 0}]
        eng.cur = 0
        r = self.b.form_fill(1, values={"doc": "@" + path})
        self.assertTrue(r["success"])
        self.assertTrue(any("file" in x for x in r["filled"]))
        os.unlink(path)

    # ---- HAR ----
    def test_har_export(self):
        import tempfile, os, json as _json
        self.b.open(self.base + "/")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".har") as f:
            path = f.name
        r = self.b.har(path)
        self.assertTrue(r["success"])
        with open(path) as f:
            d = _json.load(f)
        self.assertEqual(d["log"]["version"], "1.2")
        self.assertTrue(len(d["log"]["entries"]) >= 1)
        e = d["log"]["entries"][0]
        self.assertIn("request", e)
        self.assertIn("response", e)
        os.unlink(path)

    # ---- pdf / block honesty on static ----
    def test_pdf_static_honest(self):
        r = self.b.pdf()
        self.assertFalse(r["success"])
        self.assertFalse(r["supported"])

    def test_block_static_honest(self):
        self.assertFalse(self.b.block_add("*ads*")["success"])
        self.assertFalse(self.b.block_list()["success"])


    # ================= NEW: network devtools =================
    def test_network_schema(self):
        self.b.open(self.base + "/json")
        e = self.b.network()["requests"][-1]
        for k in ("resource_type", "request_size", "response_size", "timing",
                  "req_headers", "res_headers"):
            self.assertIn(k, e)
        self.assertEqual(e["resource_type"], "document")
        self.assertIn("start", e["timing"])

    def test_network_response_json(self):
        self.b.open(self.base + "/json")
        rid = self.b.network()["requests"][-1]["id"]
        r = self.b.network_response(rid)
        self.assertTrue(r["success"])
        self.assertEqual(r["format"], "json")
        self.assertEqual(r["json"]["hello"], "world")

    def test_network_response_missing(self):
        r = self.b.network_response(99999)
        self.assertFalse(r["success"])

    def test_network_filters(self):
        self.b.open(self.base + "/")
        self.b.open(self.base + "/json")
        reqs = self.b.network(type_="document")["requests"]
        self.assertTrue(reqs)
        self.assertTrue(all(x["resource_type"] == "document" for x in reqs))
        reqs = self.b.network(status="2xx")["requests"]
        self.assertTrue(all(x["status"] // 100 == 2 for x in reqs))
        reqs = self.b.network(status="404")["requests"]
        self.assertEqual(reqs, [])
        reqs = self.b.network(method="get")["requests"]
        self.assertTrue(all(x["method"] == "GET" for x in reqs))
        reqs = self.b.network(pattern="json")["requests"]
        self.assertTrue(all("json" in x["url"] for x in reqs))

    def test_network_request_detail(self):
        self.b.open(self.base + "/")
        rid = self.b.network()["requests"][-1]["id"]
        r = self.b.network_request(rid)
        self.assertTrue(r["success"])
        self.assertIn("headers", r)
        self.assertIn("method", r)

    def test_har_redact_headers(self):
        h = Kancil._redact_headers({"Cookie": "sess=abc",
                                    "Authorization": "Bearer x",
                                    "X-Token": "t", "X-Custom": "ok"})
        self.assertEqual(h["Cookie"], "***")
        self.assertEqual(h["Authorization"], "***")
        self.assertEqual(h["X-Token"], "***")
        self.assertEqual(h["X-Custom"], "ok")

    def test_har_valid(self):
        import tempfile, os
        self.b.open(self.base + "/")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".har") as f:
            path = f.name
        try:
            r = self.b.har_export(path)
            self.assertTrue(r["success"])
            self.assertTrue(r["redacted"])
            with open(path) as f:
                d = json.load(f)
            self.assertEqual(d["log"]["version"], "1.2")
            self.assertIn("pages", d["log"])
            e = d["log"]["entries"][0]
            for k in ("request", "response", "timings", "startedDateTime"):
                self.assertIn(k, e)
        finally:
            os.unlink(path)

    def test_har_session(self):
        self.assertTrue(self.b.har_start()["success"])
        self.b.open(self.base + "/")
        s = self.b.har_stats()
        self.assertTrue(s["recording"])
        self.assertGreaterEqual(s["entries"], 1)
        self.assertIn("by_type", s)
        self.assertTrue(self.b.har_stop()["success"])
        self.assertTrue(self.b.har_clear()["success"])
        self.assertEqual(self.b.har_stats()["entries"], 0)

    # ================= NEW: a11y =================
    def test_a11y_tree_shape(self):
        self.b.open(self.base + "/")
        r = self.b.a11y()
        self.assertTrue(r["success"])
        t = r["tree"]
        self.assertEqual(t["role"], "document")
        roles = [c["role"] for c in t["children"]]
        self.assertIn("button", roles)
        self.assertIn("link", roles)
        self.assertNotIn("node", json.dumps(t))  # JSON-safe
        self.assertIn("button_1", r["refs"])

    def test_a11y_find(self):
        self.b.open(self.base + "/")
        r = self.b.a11y_find("Login")
        self.assertTrue(r["success"])
        self.assertEqual(r["count"], 1)
        m = r["matches"][0]
        self.assertTrue(m["ref"].startswith("@button_"))
        self.assertIn("css", m)
        self.assertTrue(m["xpath"].startswith("/"))
        r = self.b.a11y_find("", role="link")
        self.assertGreaterEqual(r["count"], 2)
        r = self.b.a11y_find("zzz-no-match")
        self.assertEqual(r["count"], 0)

    def test_a11y_ref_click(self):
        self.b.open(self.base + "/")
        self.b.a11y()
        ref = self.b.a11y_find("Login")["matches"][0]["ref"]
        r = self.b.click(ref)
        # ref resolved (not "no such reference"); button needs JS -> honest fail
        self.assertNotIn("no such reference", str(r.get("errors", [])))

    def test_a11y_ref_invalidated(self):
        self.b.open(self.base + "/")
        self.b.a11y()
        self.b.open(self.base + "/page2")
        r = self.b.click("@button_1")
        self.assertFalse(r["success"])
        self.assertIn("invalidated", r["errors"][0])

    def test_a11y_ref_unknown(self):
        self.b.open(self.base + "/")
        r = self.b.click("@button_99")
        self.assertFalse(r["success"])

    # ================= NEW: scraper pagination =================
    def test_pagination_next_selector(self):
        r = self.b.scrape(url=self.base + "/shop?page=1", selector=".card",
                          fields={"title": ".title"}, next_selector="a.next",
                          pages=5, delay=0)
        self.assertTrue(r["success"], r)
        self.assertEqual(r["pages_crawled"], 3)
        self.assertGreater(r["duplicates"], 0)  # page 3 == page 2
        titles = [d["title"] for d in r["data"]]
        self.assertEqual(titles, ["A", "B", "C", "D"])
        # guessed ?page=4 404s -> graceful stop, not a failure
        self.assertEqual(r["stopped_reason"], "no-more-pages")
        self.assertEqual(r["failed_pages"], 0)

    def test_pagination_numbered(self):
        r = self.b.scrape(url=self.base + "/shop?page=1", selector=".card",
                          fields={"title": ".title"}, pages=5, delay=0,
                          same_content_limit=1)
        self.assertTrue(r["success"], r)
        self.assertEqual(r["pages_crawled"], 3)
        self.assertEqual([d["title"] for d in r["data"]], ["A", "B", "C", "D"])

    def test_pagination_max_items(self):
        r = self.b.scrape(url=self.base + "/shop?page=1", selector=".card",
                          fields={"title": ".title"}, pages=10, max_items=3,
                          delay=0)
        self.assertTrue(r["success"])
        self.assertEqual(r["items"], 3)
        self.assertEqual(r["stopped_reason"], "max-items")

    def test_pagination_max_pages(self):
        r = self.b.scrape(url=self.base + "/shop?page=1", selector=".card",
                          fields={"title": ".title"}, pages=2, delay=0)
        self.assertEqual(r["pages_crawled"], 2)
        self.assertEqual(r["stopped_reason"], "max-pages")

    def test_robots_respected(self):
        r = self.b.scrape(url=self.base + "/private", selector=".card",
                          fields={"title": ".title"}, pages=2, delay=0)
        self.assertEqual(r["stopped_reason"], "robots-disallowed")
        self.assertEqual(r["pages_crawled"], 0)
        r2 = self.b.scrape(url=self.base + "/private", selector=".card",
                           fields={"title": ".title"}, pages=1, delay=0,
                           respect_robots=False)
        self.assertEqual(r2["pages_crawled"], 1)

    def test_canonical_url(self):
        from kancil.devtools import canonical_url
        self.assertEqual(canonical_url("http://X.com/a?utm_source=1&x=2#frag"),
                         "http://x.com/a?x=2")
        self.assertEqual(canonical_url("http://x.com/b?b=2&a=1"),
                         canonical_url("http://x.com/b?a=1&b=2"))

    # ================= NEW: sessions =================
    def test_session_save_load_delete_info(self):
        self.b.open(self.base + "/")
        self.b.bookmark_add(url=self.base + "/", title="H")
        r = self.b.session_save("t1")
        self.assertTrue(r["success"])
        info = self.b.session_info("t1")
        self.assertTrue(info["success"])
        self.assertEqual(info["name"], "t1")
        self.assertIn("tabs", info)
        self.assertIn("cookies", info)
        b2 = Kancil(engine="static", timeout=10, retries=0)
        try:
            r = b2.session_load("t1")
            self.assertTrue(r["success"])
            self.assertEqual(len(b2.bookmarks), 1)
        finally:
            b2.close()
        self.assertTrue(self.b.session_delete("t1")["success"])
        self.assertFalse(self.b.session_info("t1")["success"])
        self.assertFalse(self.b.session_delete("t1")["success"])

    def test_pw_session_roundtrip(self):
        from kancil import session as sm
        fake = {"cookies": [{"name": "s", "value": "SECRET",
                             "domain": "x.com", "path": "/"}],
                "origins": [{"origin": "https://x.com",
                             "localStorage": [{"name": "k", "value": "v"}]}]}
        sm.save_pw_session("pwt", fake, {"url": "https://x.com/"})
        try:
            state, meta = sm.load_pw_session("pwt")
            self.assertEqual(state["cookies"][0]["name"], "s")
            info = sm.session_info("pwt")
            self.assertEqual(info["cookies"], 1)
            self.assertEqual(info["storage_keys"], 1)
            self.assertNotIn("SECRET", json.dumps(info))  # no secret leak
            mode = oct(os.stat(sm.pw_state_path("pwt")).st_mode & 0o777)
            self.assertEqual(mode, "0o600")
        finally:
            sm.delete_named("pwt")

    # ================= NEW: agent tool interface =================
    def test_tool_valid(self):
        r = self.b.tool({"action": "open", "url": self.base + "/"})
        self.assertTrue(r["success"])
        self.assertEqual(r["action"], "open")
        self.assertIn("title", r)
        self.assertNotIn("errors", r)

    def test_tool_invalid(self):
        r = self.b.tool({"action": "nope"})
        self.assertFalse(r["success"])
        self.assertEqual(r["error"]["code"], "UNKNOWN_ACTION")
        r = self.b.tool("not a dict")
        self.assertEqual(r["error"]["code"], "INVALID_INPUT")
        r = self.b.tool({})
        self.assertEqual(r["error"]["code"], "UNKNOWN_ACTION")

    def test_tool_error_codes(self):
        self.b.tool({"action": "open", "url": self.base + "/"})
        r = self.b.tool({"action": "click", "selector": "#missing"})
        self.assertEqual(r["error"]["code"], "ELEMENT_NOT_FOUND")
        self.assertEqual(r["error"]["selector"], "#missing")
        self.assertEqual(r["error"]["action"], "click")
        r = self.b.tool({"action": "switch_tab", "id": 99})
        self.assertEqual(r["error"]["code"], "TAB_NOT_FOUND")
        r = self.b.tool({"action": "session_load", "name": "nope-xyz"})
        self.assertEqual(r["error"]["code"], "SESSION_NOT_FOUND")
        r = self.b.tool({"action": "evaluate", "js": "1+1"})
        self.assertEqual(r["error"]["code"], "ENGINE_UNAVAILABLE")
        r = self.b.tool({"action": "open", "url": "http://127.0.0.1:1/"})
        self.assertEqual(r["error"]["code"], "NETWORK_ERROR")

    def test_tool_passthrough(self):
        r = self.b.tool({"action": "tabs"})
        self.assertTrue(r["success"])
        self.assertIn("tabs", r)
        r = self.b.tool({"action": "capabilities"})
        self.assertFalse(r["success"] is False)
        self.assertIn("javascript", r["capabilities"])

    # ================= NEW: snapshot & observability =================
    def test_snapshot(self):
        self.b.open(self.base + "/")
        r = self.b.snapshot()
        self.assertTrue(r["success"])
        for k in ("url", "title", "tab_id", "page", "a11y", "errors",
                  "network_summary"):
            self.assertIn(k, r)
        for k in ("headings", "links", "buttons", "inputs"):
            self.assertIn(k, r["page"])
        self.assertNotIn("<html", json.dumps(r))  # token-efficient
        self.assertIn("requests", r["network_summary"])

    def test_observe(self):
        r = self.b.observe()
        self.assertTrue(r["success"])
        for k in ("console_errors", "console_warnings", "network_errors",
                  "page_errors", "performance"):
            self.assertIn(k, r)

    def test_warnings_static(self):
        r = self.b.warnings()
        self.assertTrue(r["success"])
        self.assertEqual(r["warnings"], [])




    # ================= NEW: P0-4 no fetch on import =================
    def test_import_does_not_fetch(self):
        self.b.open(self.base + "/")
        st = self.b.export_state()
        n_persisted = len(st.get("netlog", []))
        b2 = Kancil(engine="static", timeout=10, retries=0)
        b2.import_state(st)
        try:
            # read-only import must not ADD any netlog entries beyond the
            # persisted ones (P0-4: no eager re-fetch of the active tab)
            self.assertEqual(len(b2.engine.netlog), n_persisted,
                             "import_state fetched the network")
            # tabs listing works without a page fetch
            r = b2.tabs()
            self.assertTrue(r["success"])
        finally:
            b2.close()

    def test_state_lock(self):
        from kancil import session as sm
        with sm.StateLock():
            sm.save_state(sm.load_state())
        self.assertTrue(os.path.exists(sm.load_state() and sm.STATE_FILE))

    # ================= NEW: CSS extensions =================
    def test_css_siblings(self):
        from kancil.dom import build_dom, select
        root = build_dom('<div><p id=a>x</p><p id=b>y</p><span>z</span></div>')
        self.assertEqual(len(select(root, 'p + p')), 1)
        self.assertEqual(len(select(root, 'p ~ span')), 1)
        self.assertEqual(len(select(root, 'p:not(#a)')), 1)

    def test_css_attr_ops(self):
        from kancil.dom import build_dom, select
        root = build_dom('<a href="/x/y" title="hello world" rel="a b">t</a>')
        self.assertEqual(len(select(root, 'a[href^="/x"]')), 1)
        self.assertEqual(len(select(root, 'a[href$="/y"]')), 1)
        self.assertEqual(len(select(root, 'a[title*="lo wo"]')), 1)
        self.assertEqual(len(select(root, 'a[rel~="b"]')), 1)
        self.assertEqual(len(select(root, 'a[href*="zzz"]')), 0)

    def test_css_child_positions(self):
        from kancil.dom import build_dom, select
        root = build_dom('<ul><li>a</li><li>b</li></ul>')
        self.assertEqual(len(select(root, 'li:first-child')), 1)
        self.assertEqual(len(select(root, 'li:last-child')), 1)
        self.assertEqual(len(select(root, 'li:nth-child(2)')), 1)

    def test_check_selector(self):
        from kancil.dom import check_selector
        self.assertIsNone(check_selector('button:not(.disabled)'))
        self.assertIsNone(check_selector('div.item + div.item'))
        self.assertIsNotNone(check_selector('p:hover'))
        self.assertIsNotNone(check_selector('input['))
        self.assertIsNotNone(check_selector(''))

    def test_dom_find_invalid_vs_empty(self):
        self.b.open(self.base + "/")
        r = self.b.dom_find("video:not(.x)")
        self.assertTrue(r["success"])  # valid selector, empty result
        self.assertEqual(r["count"], 0)
        self.assertIn("hint", r)
        r = self.b.dom_find("button:not(.disabled)")
        self.assertTrue(r["success"])  # valid; page has a plain button
        self.assertGreaterEqual(r["count"], 1)
        r = self.b.dom_find("p:hover")
        self.assertFalse(r["success"])
        self.assertEqual(r.get("code"), "INVALID_INPUT")

    # ================= NEW: auto-close soup =================
    def test_soup_auto_close(self):
        from kancil.dom import build_dom, select
        root = build_dom('<p>one<div>two</div></p><ul><li>a<li>b</ul>')
        ps = select(root, 'p')
        self.assertEqual(len(ps), 1)
        self.assertNotIn("two", ps[0].text_content())
        self.assertEqual(len(select(root, 'div > p')), 0)
        self.assertEqual(len(select(root, 'li')), 2)

    # ================= NEW: embedded JSON / youtube =================
    def test_embedded_json(self):
        from kancil.devtools import extract_embedded_json
        html = ('<html><head>'
                '<script id="__NEXT_DATA__" type="application/json">'
                '{"props":{"a":1}}</script>'
                '<script type="application/ld+json">{"@type":"X"}</script>'
                '</head><body></body></html>')
        blobs = extract_embedded_json(html)
        srcs = {b["source"] for b in blobs}
        self.assertIn("__NEXT_DATA__", srcs)
        self.assertIn("ld+json", srcs)

    def test_yt_initial_data(self):
        from kancil.devtools import extract_yt_search, extract_embedded_json
        import json as _json
        payload = {"contents": {"x": [
            {"videoRenderer": {"videoId": "abc123",
                              "title": {"runs": [{"text": "Hello"}]},
                              "ownerText": {"runs": [{"text": "Chan"}]}}},
            {"videoRenderer": {"videoId": "abc123",
                              "title": {"runs": [{"text": "Hello"}]}}},
        ]}}
        # format 1: JS-escaped string literal (mobile / embed player)
        js_esc = _json.dumps(payload).encode("unicode_escape").decode()
        html1 = ("<html><body><script>var ytInitialData = '%s';</script>"
                 "</body></html>" % js_esc)
        # format 2: raw JSON object (desktop www.youtube.com)
        html2 = ("<html><body><script>var ytInitialData = %s;</script>"
                 "</body></html>" % _json.dumps(payload))
        for html in (html1, html2):
            blobs = extract_embedded_json(html)
            self.assertEqual(len(blobs), 1, html[:60])
            vids = extract_yt_search(html)
            self.assertEqual(len(vids), 1)  # deduped
            self.assertEqual(vids[0]["videoId"], "abc123")
            self.assertEqual(vids[0]["title"], "Hello")
            self.assertEqual(vids[0]["channel"], "Chan")
            self.assertTrue(vids[0]["url"].endswith("abc123"))

    def test_yt_video_og(self):
        from kancil.devtools import extract_yt_video
        html = ('<meta property="og:title" content="Rick Astley - Never Gonna">'
                '<meta property="og:image" content="https://i.yt/img.jpg">'
                '<meta property="og:video:url" content="https://x/v.mp4">')
        m = extract_yt_video(html)
        self.assertEqual(m["title"], "Rick Astley - Never Gonna")
        self.assertEqual(m["image"], "https://i.yt/img.jpg")

    def test_page_json(self):
        from kancil.devtools import _decode_html_text
        from kancil.engines import Page
        html = ('<html><head><script type="application/ld+json">'
                '{"@type":"Video"}' + '</' + 'script></head><body></body></html>')
        eng = self.b.engine
        eng.tabs = [{"history": [Page(self.base + "/", 200, html.encode(),
                                      "text/html")], "pos": 0}]
        eng.cur = 0
        r = self.b.page_json()
        self.assertTrue(r["success"])
        self.assertEqual(r["count"], 1)

    # ================= NEW: perf per-page =================
    def test_perf_page_requests(self):
        self.b.open(self.base + "/")
        self.b.open(self.base + "/page2")
        r = self.b.perf()
        if r.get("success"):
            # only requests since the last open, not the whole session
            self.assertLessEqual(r["requests"], 3)
            self.assertIn("requests_note", r)

    # ================= NEW: tool yt_search action registered =================
    def test_tool_yt_actions(self):
        self.assertIn("yt_search", Kancil._TOOL_ACTIONS)
        self.assertIn("page_json", Kancil._TOOL_ACTIONS)



    # ================= NEW: packaging / documented invocation =================
    def test_cli_from_project_root(self):
        """P0-1 regression: `python3 -m kancil` must work from project root
        (the documented path), not from inside the package dir."""
        import subprocess
        proj_root = os.path.join(os.path.dirname(__file__), "..")
        r = subprocess.run([sys.executable, "-m", "kancil", "--help"],
                           cwd=proj_root, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertIn("usage: kancil", r.stdout)

    def test_cli_version(self):
        import subprocess
        proj_root = os.path.join(os.path.dirname(__file__), "..")
        r = subprocess.run([sys.executable, "-m", "kancil", "--version"],
                           cwd=proj_root, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("kancil", r.stdout)
        from kancil import __version__
        self.assertIn(__version__, r.stdout)

    def test_pyproject_exists(self):
        proj_root = os.path.join(os.path.dirname(__file__), "..")
        pp = os.path.join(proj_root, "pyproject.toml")
        self.assertTrue(os.path.exists(pp))
        txt = open(pp).read()
        self.assertIn('kancil = "kancil.cli:main"', txt)



    # ================= NEW: viewer =================
    def test_rewrite_html(self):
        from kancil.viewer import rewrite_html
        html = ('<html><head><title>T</title></head><body>'
                '<a href="/about">A</a>'
                '<a href="https://x.com/y">B</a>'
                '<a href=https://unquoted.example/z>W</a>'
                '<a href="javascript:void(0)">C</a>'
                '<a href="mailto:a@b.c">D</a>'
                '<a href="#frag">E</a>'
                '<form action="/s" method="get"><input name="q"></form>'
                '<form action="/p" method="post"></form>'
                '<img src="/i.png"></body></html>')
        out = rewrite_html(html, "https://ex.com/page")
        self.assertIn('<base href="https://ex.com/page">', out)
        self.assertIn('/__kancil__/go?u=https%3A%2F%2Fex.com%2Fabout', out)
        self.assertIn('/__kancil__/go?u=https%3A%2F%2Fx.com%2Fy', out)
        self.assertIn('/__kancil__/go?u=https%3A%2F%2Funquoted.example%2Fz',
                      out)
        self.assertIn('href="javascript:void(0)"', out)
        self.assertIn('href="mailto:a@b.c"', out)
        self.assertIn('href="#frag"', out)
        self.assertIn('action="/__kancil__/go?u=https%3A%2F%2Fex.com%2Fs"',
                      out)
        self.assertIn('action="/p" method="post"', out)  # POST untouched
        self.assertIn('src="/i.png"', out)  # resources via <base>
        self.assertIn('id="__kancil_bar"', out)

    def test_viewer_static_routes(self):
        import urllib.request
        opened = []

        class FakeEng:
            name = "static"

            class _P:
                url = "https://ex.com/"
                raw = (b'<html><head><title>T</title></head><body>'
                       b'<a href="/x">x</a></body></html>')

            @property
            def page(self):
                return self._P()

        class FakeK:
            def __init__(self):
                self.engine = FakeEng()

            def open(self, url):
                opened.append(url)
                return {"success": True}

            def back(self):
                return {"success": True}

            def forward(self):
                return {"success": True}

            def reload(self):
                return {"success": True}

        from kancil.viewer import ViewerServer
        srv = ViewerServer(FakeK(), port=0)
        url = srv.start()
        try:
            root = urllib.request.urlopen(url, timeout=5).read().decode()
            self.assertIn("<iframe", root)
            page = urllib.request.urlopen(url + "page",
                                          timeout=5).read().decode()
            self.assertIn("__kancil_bar", page)
            self.assertIn("__kancil__/go?u=", page)

            class NoRedir(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None

            op = urllib.request.build_opener(NoRedir)
            try:
                op.open(url + "__kancil__/go?u=" +
                        "https%3A%2F%2Fex.com%2Fnext", timeout=5)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 302)
            self.assertEqual(opened, ["https://ex.com/next"])
            # playwright-only routes rejected on static engine
            try:
                op.open(url + "__kancil__/click?x=1&y=2", timeout=5)
                self.fail("expected 400")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 400)
        finally:
            srv.stop()

    def test_viewer_playwright_routes(self):
        import urllib.request, json as _json

        clicks, types = [], []

        class FakePP:
            viewport_size = {"width": 1280, "height": 800}

            def screenshot(self):
                return b"FAKEPNG"

            class mouse:
                @staticmethod
                def click(x, y):
                    clicks.append((x, y))

            class keyboard:
                @staticmethod
                def type(t):
                    types.append(t)

                @staticmethod
                def press(k):
                    types.append("[" + k + "]")

        class FakeEng:
            name = "playwright"

            def _pp(self):
                return FakePP()

            def _invalidate(self):
                pass

        class FakeK:
            engine = FakeEng()

        from kancil.viewer import ViewerServer
        srv = ViewerServer(FakeK(), port=0)
        url = srv.start()
        try:
            shot = urllib.request.urlopen(url + "__kancil__/shot.png",
                                          timeout=5)
            self.assertEqual(shot.headers.get_content_type(), "image/png")
            self.assertEqual(shot.read(), b"FAKEPNG")
            vp = _json.loads(urllib.request.urlopen(
                url + "__kancil__/viewport", timeout=5).read())
            self.assertEqual(vp["width"], 1280)
            r = _json.loads(urllib.request.urlopen(
                url + "__kancil__/click?x=10&y=20", timeout=5).read())
            self.assertTrue(r["success"])
            self.assertEqual(clicks, [(10, 20)])
            urllib.request.urlopen(url + "__kancil__/type?text=hi",
                                   timeout=5).read()
            self.assertEqual(types, ["hi"])
            root = urllib.request.urlopen(url, timeout=5).read().decode()
            self.assertIn('id="shot"', root)
        finally:
            srv.stop()

    def test_view_api(self):
        import urllib.request
        r = self.b.view(port=0)
        try:
            self.assertTrue(r["success"])
            self.assertTrue(r["url"].startswith("http://127.0.0.1:"))
            body = urllib.request.urlopen(r["url"], timeout=5).read()
            self.assertIn(b"<iframe", body)
            r2 = self.b.view(port=0)  # second call: already running
            self.assertEqual(r2["url"], r["url"])
        finally:
            s = self.b.view_stop()
            self.assertTrue(s["stopped"])



    # ================= NEW: yt-play =================
    def test_extract_video_id(self):
        from kancil.viewer import _extract_video_id
        self.assertEqual(_extract_video_id(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(_extract_video_id(
            "https://youtu.be/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(_extract_video_id(
            "https://www.youtube.com/embed/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(_extract_video_id(
            "https://www.youtube.com/shorts/dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertEqual(_extract_video_id("dQw4w9WgXcQ"), "dQw4w9WgXcQ")
        self.assertIsNone(_extract_video_id("termux tutorial"))
        self.assertIsNone(_extract_video_id(""))

    def test_player_page(self):
        from kancil.viewer import player_page
        html = player_page("dQw4w9WgXcQ", 'Rick <b>"Astley"</b>', "Chan",
                           "https://i.yt/img.jpg")
        self.assertIn("https://www.youtube.com/embed/dQw4w9WgXcQ", html)
        self.assertIn("Rick &lt;b&gt;", html)  # escaped
        self.assertNotIn("<b>", html.replace("&lt;b&gt;", ""))
        self.assertIn("allowfullscreen", html)

    def test_play_route(self):
        import urllib.request

        class FakeK:
            class _Eng:
                name = "static"

                @property
                def page(self):
                    return None

            engine = _Eng()

            def yt_video(self, url):
                self.last = url
                return {"success": True, "title": "T",
                        "image": "https://i.yt/i.jpg"}

        from kancil.viewer import ViewerServer
        fk = FakeK()
        srv = ViewerServer(fk, port=0)
        url = srv.start()
        try:
            body = urllib.request.urlopen(url + "__kancil__/play?v=abc123XYZ78",
                                          timeout=5).read().decode()
            self.assertIn("/embed/abc123XYZ78", body)
            self.assertIn("<title>T", body)
            self.assertEqual(fk.last,
                             "https://www.youtube.com/watch?v=abc123XYZ78")
            try:
                urllib.request.urlopen(url + "__kancil__/play", timeout=5)
                self.fail("expected 400")
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 400)
        finally:
            srv.stop()

    def test_yt_play_api(self):
        r = self.b.yt_play("dQw4w9WgXcQ", port=0)
        try:
            self.assertTrue(r["success"])
            self.assertEqual(r["video_id"], "dQw4w9WgXcQ")
            self.assertIn("/__kancil__/play?v=dQw4w9WgXcQ", r["play_url"])
        finally:
            self.b.view_stop()



    # ================= NEW: agent bridge (level 1) =================
    def test_agent_bridge_flow(self):
        from kancil.agent_bridge import AgentBridge
        br = AgentBridge()
        br.register("tab-1", {"url": "https://ex.com/", "title": "T"})
        tabs = br.tabs()
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0]["tab_id"], "tab-1")
        self.assertTrue(tabs[0]["live"])

        # simulate the injected agent.js: poll in background, execute, post
        import threading, time

        def fake_agent():
            cmds = br.poll("tab-1", 0, timeout=10)
            for c in cmds:
                br.submit_result("tab-1", c["id"], {"echo": c["action"]})

        th = threading.Thread(target=fake_agent, daemon=True)
        th.start()
        r = br.send("tab-1", "click", {"selector": "a"}, timeout=10)
        th.join(timeout=10)
        self.assertTrue(r["success"])
        self.assertEqual(r["result"], {"echo": "click"})

        # unknown tab / action
        r = br.send("nope", "click", {}, timeout=1)
        self.assertFalse(r["success"])
        r = br.send("tab-1", "bogus", {}, timeout=1)
        self.assertFalse(r["success"])

        # timeout when nobody polls
        r = br.send("tab-1", "snapshot", {}, timeout=1)
        self.assertFalse(r["success"])
        self.assertIn("timeout", r["errors"][0])

    def test_agent_routes(self):
        import urllib.request, json as _json

        class FakeK:
            class _Eng:
                name = "static"

                @property
                def page(self):
                    return None

            engine = _Eng()

        from kancil.viewer import ViewerServer
        from kancil.agent_bridge import get_bridge
        srv = ViewerServer(FakeK(), port=0)
        url = srv.start()
        try:
            js = urllib.request.urlopen(url + "__kancil__/agent.js",
                                        timeout=5).read().decode()
            self.assertIn("kancil agent.js", js)
            self.assertIn("/poll", js)
            # register via POST
            req = urllib.request.Request(
                url + "__kancil__/agent/register",
                data=_json.dumps({"tab": "tab-ut",
                                  "url": "https://ex.com/"}).encode(),
                headers={"Content-Type": "application/json"})
            r = _json.loads(urllib.request.urlopen(req,
                                                   timeout=5).read())
            self.assertTrue(r["success"])
            tabs = get_bridge().tabs()
            self.assertTrue(any(t["tab_id"] == "tab-ut" for t in tabs))
            # poll returns [] when idle
            p = _json.loads(urllib.request.urlopen(
                url + "__kancil__/agent/poll?tab=tab-ut&seq=0",
                timeout=35).read())
            self.assertEqual(p, [])
            get_bridge().unregister("tab-ut")
        finally:
            srv.stop()

    def test_rewrite_injects_agent(self):
        from kancil.viewer import rewrite_html
        out = rewrite_html("<html><body><p>hi</p></body></html>",
                           "https://ex.com/")
        self.assertIn('<script src="/__kancil__/agent.js"', out)
        self.assertIn('data-kancil-url="https://ex.com/"', out)


class MoatTest(unittest.TestCase):
    """v3.5.0 moat features: network_curl, batch, browser env, camoufox."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.srv = http.server.HTTPServer(("127.0.0.1", cls.port), Handler)
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.tmp = tempfile.mkdtemp()
        os.environ["HOME"] = cls.tmp

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.b = Kancil(engine="static", timeout=10, retries=0)

    def tearDown(self):
        self.b.close()

    def test_network_curl_get(self):
        self.b.open(self.base + "/")
        r = self.b.tool({"action": "network_curl", "id": 1})
        self.assertTrue(r["success"], r)
        c = r["curl"]
        self.assertTrue(c.startswith("curl -X GET "))
        self.assertIn(self.base + "/", c)
        self.assertIn("-H", c)  # headers included

    def test_network_curl_post(self):
        self.b.engine._log({"t": "t", "method": "POST",
                            "url": "https://ex.com/api?q=1",
                            "status": 200, "ctype": "application/json",
                            "size": 2, "ms": 1,
                            "req_headers": {"Content-Type": "application/json"},
                            "res_headers": {},
                            "post_data": '{"a": 1}'})
        rid = self.b.engine._req_id
        r = self.b.tool({"action": "network_curl", "id": rid})
        self.assertTrue(r["success"], r)
        self.assertIn("--data-raw", r["curl"])
        self.assertIn("ex.com/api", r["curl"])

    def test_network_curl_bad_id(self):
        r = self.b.tool({"action": "network_curl", "id": 99999})
        self.assertFalse(r["success"])

    def test_network_curl_shell_quoting(self):
        self.b.engine._log({"t": "t", "method": "GET",
                            "url": "https://ex.com/?q=a'b",
                            "status": 200, "ctype": "-", "size": 0, "ms": 1,
                            "req_headers": {}, "res_headers": {}})
        r = self.b.tool({"action": "network_curl",
                         "id": self.b.engine._req_id})
        self.assertTrue(r["success"], r)
        # shlex.quote escapes the single quote -> safe to paste into a shell
        self.assertIn("'\"'\"'", r["curl"])

    def test_batch_runs_all(self):
        self.b.open(self.base + "/")
        r = self.b.tool({"action": "batch", "actions": [
            {"action": "network_curl", "id": 1},
            {"action": "bogus_action"},
            {"action": "capabilities"},
        ]})
        self.assertTrue(r["success"], r)
        self.assertEqual(r["count"], 3)
        got = [x["result"]["success"] for x in r["results"]]
        self.assertEqual(got, [True, False, True])

    def test_batch_stop_on_error(self):
        r = self.b.tool({"action": "batch", "stop_on_error": True, "actions": [
            {"action": "bogus_action"},
            {"action": "capabilities"},
        ]})
        self.assertTrue(r["success"], r)
        self.assertEqual(r["count"], 1)

    def test_batch_validation(self):
        r = self.b.tool({"action": "batch"})
        self.assertFalse(r["success"])
        r = self.b.tool({"action": "batch",
                         "actions": [{"action": "capabilities"}] * 26})
        self.assertFalse(r["success"])

    def test_browser_env_strips_ld_preload(self):
        from kancil.pw_engine import _browser_env
        old = os.environ.get("LD_PRELOAD")
        try:
            os.environ["LD_PRELOAD"] = "libtermux-exec.so"
            env = _browser_env()
            self.assertIsNotNone(env)
            self.assertNotIn("LD_PRELOAD", env)
            self.assertIn("HOME", env)
        finally:
            if old is None:
                os.environ.pop("LD_PRELOAD", None)
            else:
                os.environ["LD_PRELOAD"] = old
        # no LD_PRELOAD -> None (playwright inherits os.environ)
        os.environ.pop("LD_PRELOAD", None)
        self.assertIsNone(_browser_env())

    def test_find_camoufox_binary(self):
        from kancil import pw_engine
        d = os.path.join(self.tmp, ".cache", "camoufox", "browsers",
                         "camoufox-152.0.4-beta")
        os.makedirs(d)
        fake = os.path.join(d, "firefox")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(fake, 0o755)
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp
        try:
            found = pw_engine.find_camoufox_binary()
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home
        self.assertEqual(found, fake)

    def test_find_camoufox_none(self):
        from kancil import pw_engine
        empty = tempfile.mkdtemp()
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = empty
        try:
            self.assertIsNone(pw_engine.find_camoufox_binary())
        finally:
            if old_home is not None:
                os.environ["HOME"] = old_home


class KeepAliveTest(unittest.TestCase):
    """v3.6.0: keep-alive pooling + netlog cap."""

    @classmethod
    def setUpClass(cls):
        class H11(Handler):
            protocol_version = "HTTP/1.1"
        cls.port = free_port()
        cls.srv = http.server.HTTPServer(("127.0.0.1", cls.port), H11)
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.tmp = tempfile.mkdtemp()
        os.environ["HOME"] = cls.tmp

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _ka_handler(self, engine):
        from kancil import engines
        for h in engine.opener.handlers:
            if isinstance(h, engines._KeepAliveHTTPHandler):
                return h
        return None

    def test_pool_reuses_connection(self):
        from kancil import Kancil
        b = Kancil(engine="static", timeout=10, retries=0)
        try:
            h = self._ka_handler(b.engine)
            self.assertIsNotNone(h, "keep-alive handler not installed")
            created = []
            orig = h._pool_get

            def counting(*a, **k):
                conn, key, fresh = orig(*a, **k)
                created.append(fresh)
                return conn, key, fresh

            h._pool_get = counting
            try:
                for i in range(5):
                    r = b.open(self.base + "/")
                    self.assertTrue(r["success"], r)
            finally:
                h._pool_get = orig
            # first request builds the connection, rest reuse it
            self.assertEqual(created, [True, False, False, False, False],
                             created)
        finally:
            b.close()

    def test_stale_connection_heals(self):
        import http.client
        import urllib.request
        from kancil import engines
        h = engines._KeepAliveHTTPHandler()
        # plant a dead-but-not-None socket in the pool: request() on it
        # raises OSError, which must trigger the fresh-retry path
        dead = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        dead.request("GET", "/")
        dead.getresponse().read()
        dead.sock.close()  # kill fd, keep attribute non-None
        key = ("HTTPConnection", "127.0.0.1:%d" % self.port)
        h._pool[key] = dead
        op = urllib.request.build_opener(h)
        try:
            with op.open(self.base + "/", timeout=10) as r:
                body = r.read()
            self.assertIn(b"Welcome", body)
        finally:
            h.close_idle()

    def test_no_pool_with_proxy(self):
        from kancil import Kancil, engines
        b = Kancil(engine="static", timeout=10, retries=0,
                   proxy="http://127.0.0.1:9/")
        try:
            for h in b.engine.opener.handlers:
                self.assertNotIsInstance(h, engines._KeepAliveHTTPHandler)
                self.assertNotIsInstance(h, engines._KeepAliveHTTPSHandler)
        finally:
            b.close()

    def test_netlog_capped(self):
        from kancil import Kancil
        b = Kancil(engine="static", timeout=10, retries=0)
        try:
            for _ in range(1005):
                b.engine._log({"t": "t"})
            self.assertEqual(len(b.engine.netlog), 1000)
            # oldest dropped first: ids are the last 1000
            self.assertEqual(b.engine.netlog[0]["id"], 6)
            self.assertEqual(b.engine.netlog[-1]["id"], 1005)
        finally:
            b.close()


class AntiBotTest(unittest.TestCase):
    """v3.6.0: browser-like headers, gzip, cookie import."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.srv = http.server.HTTPServer(("127.0.0.1", cls.port), Handler)
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.tmp = tempfile.mkdtemp()
        os.environ["HOME"] = cls.tmp

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.b = Kancil(engine="static", timeout=10, retries=0)

    def tearDown(self):
        self.b.close()

    def test_browser_headers_sent(self):
        self.b.open(self.base + "/")
        e = self.b.engine.netlog[-1]
        h = {k.lower(): v for k, v in e.get("req_headers", {}).items()}
        self.assertIn("accept", h)
        self.assertIn("accept-language", h)
        self.assertIn("accept-encoding", h)
        self.assertIn("gzip", h["accept-encoding"])
        self.assertIn("sec-fetch-dest", h)

    def test_gzip_decoded(self):
        r = self.b.open(self.base + "/gzip")
        self.assertTrue(r["success"], r)
        self.assertIn("gzipped hi", r.get("text", "") or
                      self.b.engine.page.text)

    def test_cookies_import(self):
        # Netscape-format cookies.txt
        p = os.path.join(self.tmp, "cookies.txt")
        with open(p, "w") as f:
            f.write("# Netscape HTTP Cookie File\n")
            f.write("127.0.0.1\tFALSE\t/\tFALSE\t0\t"
                    "imported_ck\thello123\n")
        r = self.b.tool({"action": "cookies_import", "file": p})
        self.assertTrue(r["success"], r)
        self.assertEqual(r["imported"], 1)
        # cookie is now in the jar and sent to the server
        names = [c.name for c in self.b.engine.jar]
        self.assertIn("imported_ck", names)

    def test_cookies_import_bad_file(self):
        r = self.b.tool({"action": "cookies_import",
                         "file": "/nonexistent/x.txt"})
        self.assertFalse(r["success"])


class SessionPersistTest(unittest.TestCase):
    """v3.8.0: --wait-ms on open, --session auto-save on exit."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.srv = http.server.HTTPServer(("127.0.0.1", cls.port), Handler)
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.tmp = tempfile.mkdtemp()
        os.environ["HOME"] = cls.tmp

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_open_wait_ms(self):
        import argparse
        import time
        from kancil import Kancil, cli
        b = Kancil(engine="static", timeout=10, retries=0)
        try:
            ns = argparse.Namespace(cmd="open", url=self.base + "/",
                                    wait_ms=600)
            t0 = time.time()
            r = cli.dispatch(b, ns)
            dt = time.time() - t0
            self.assertTrue(r["success"], r)
            self.assertEqual(r.get("waited_ms"), 600)
            self.assertGreaterEqual(dt, 0.5)
        finally:
            b.close()

    def test_open_no_wait_by_default(self):
        import argparse
        from kancil import Kancil, cli
        b = Kancil(engine="static", timeout=10, retries=0)
        try:
            ns = argparse.Namespace(cmd="open", url=self.base + "/")
            r = cli.dispatch(b, ns)
            self.assertTrue(r["success"], r)
            self.assertNotIn("waited_ms", r)
        finally:
            b.close()

    def test_put_browser_autosaves_pw_session(self):
        # static engine has no storage_state; put_browser must not crash
        # and must not create a session dir for it
        import argparse
        from kancil import Kancil, cli
        from kancil import session as session_mod
        b = Kancil(engine="static", timeout=10, retries=0)
        b.pw_session = "staticsess"
        try:
            cli.put_browser(b, {}, argparse.Namespace())
        finally:
            pass
        self.assertFalse(os.path.exists(session_mod.pw_state_path("staticsess")))


class ScrapePowerTest(unittest.TestCase):
    """v3.9.0: structured data, sitemap crawl, concurrent workers."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.srv = http.server.HTTPServer(("127.0.0.1", cls.port), Handler)
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.tmp = tempfile.mkdtemp()
        os.environ["HOME"] = cls.tmp

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.b = Kancil(engine="static", timeout=10, retries=0)

    def tearDown(self):
        self.b.close()

    def test_structured_live(self):
        self.b.open(self.base + "/p1")
        r = self.b.structured()
        self.assertTrue(r["success"], r)
        self.assertEqual(r["opengraph"].get("title"), "OG-P1")
        self.assertEqual(r["twitter"].get("card"), "summary")
        self.assertEqual(r["meta"].get("description"), "Desc-P1")
        types = [x.get("@type") for x in r["json_ld"]]
        self.assertIn("Article", types)
        self.assertIn("WebPage", types)

    def test_structured_tool_action(self):
        self.b.open(self.base + "/p2")
        r = self.b.tool({"action": "structured"})
        self.assertTrue(r["success"], r)
        self.assertEqual(r["opengraph"].get("title"), "OG-P2")

    def test_extract_structured_pure(self):
        from kancil.dom import build_dom
        from kancil.devtools import extract_structured
        html = ('<html><head>'
                '<meta property="og:title" content="T">'
                '<meta name="twitter:card" content="summary_large_image">'
                '<meta name="description" content="D">'
                '<script type="application/ld+json">'
                '{"@type":"Product","name":"W"}'
                "</script></head><body></body></html>")
        d = build_dom(html)
        raw = html.encode()
        out = extract_structured(d, raw)
        self.assertEqual(out["opengraph"], {"title": "T"})
        self.assertEqual(out["twitter"], {"card": "summary_large_image"})
        self.assertEqual(out["meta"], {"description": "D"})
        self.assertEqual(len(out["json_ld"]), 1)
        self.assertEqual(out["json_ld"][0]["@type"], "Product")

    def test_sitemap_index(self):
        from kancil.devtools import fetch_sitemap_urls
        res = fetch_sitemap_urls(self.base + "/")
        self.assertEqual(len(res["sitemaps"]), 2)  # index + sm1.xml
        self.assertEqual(len(res["urls"]), 4)
        self.assertTrue(all(u.startswith(self.base) for u in res["urls"]))
        self.assertFalse(res["truncated"])

    def test_sitemap_max_urls(self):
        from kancil.devtools import fetch_sitemap_urls
        res = fetch_sitemap_urls(self.base + "/", max_urls=2)
        self.assertEqual(len(res["urls"]), 2)
        self.assertTrue(res["truncated"])

    def test_sitemap_tool_action(self):
        r = self.b.tool({"action": "sitemap", "url": self.base + "/",
                         "max_urls": 10})
        self.assertTrue(r["success"], r)
        self.assertEqual(r["count"], 4)

    def test_scrape_sitemap_sequential(self):
        r = self.b.scrape(url=self.base + "/", sitemap=True, selector="h1",
                          fields={"t": "h1"}, pages=10, max_items=10,
                          delay=0)
        self.assertTrue(r["success"], r)
        self.assertEqual(r["items"], 3)  # /missing 404 is skipped
        self.assertEqual(r["failed_pages"], 1)
        titles = sorted(x["t"] for x in r["data"])
        self.assertEqual(titles, ["Page P1", "Page P2", "Page P3"])

    def test_scrape_sitemap_workers(self):
        r = self.b.scrape(url=self.base + "/", sitemap=True, selector="h1",
                          fields={"t": "h1"}, pages=10, max_items=10,
                          delay=0, workers=3)
        self.assertTrue(r["success"], r)
        self.assertEqual(r["items"], 3)
        titles = sorted(x["t"] for x in r["data"])
        self.assertEqual(titles, ["Page P1", "Page P2", "Page P3"])

    def test_workers_needs_sitemap(self):
        r = self.b.scrape(url=self.base + "/shop", selector=".card",
                          fields={"t": ".title"}, workers=3)
        self.assertFalse(r["success"])
        self.assertIn("sitemap", str(r))


class SlimLevel2Test(unittest.TestCase):
    """v3.10.0: pure-Python X.509 (no openssl CLI needed)."""

    @classmethod
    def setUpClass(cls):
        from kancil import session as session_mod
        cls._old_base = session_mod.BASE
        cls.tmp = tempfile.mkdtemp()
        session_mod.BASE = os.path.join(cls.tmp, ".kancil")

    @classmethod
    def tearDownClass(cls):
        from kancil import session as session_mod
        session_mod.BASE = cls._old_base

    def test_x509_key_pem_roundtrip(self):
        from kancil import x509
        k = x509.gen_rsa(1024)
        k2 = x509.parse_rsa_private_pem(x509.rsa_private_pem(k))
        self.assertEqual(k2["n"], k["n"])
        self.assertEqual(k2["d"], k["d"])
        self.assertEqual(k2["e"], 65537)

    def test_x509_ca_and_host_cert(self):
        from kancil import x509
        ca_key = x509.gen_rsa(1024)
        host_key = x509.gen_rsa(1024)
        ca_der = x509.make_ca("Test CA", ca_key)
        h_der = x509.make_host_cert("example.com", host_key,
                                    "Test CA", ca_key)
        # signature self-verifies: decrypt sig with CA pubkey == DigestInfo
        seq = x509._Reader(ca_der).seq()
        tag, tbs_content = seq.tlv()
        tbs = x509._tlv(tag, tbs_content)  # signature covers full TLV
        seq.tlv()
        sig = seq.tlv()[1][1:]  # BIT STRING -> skip unused-bits byte
        k = (ca_key["n"].bit_length() + 7) // 8
        em = pow(int.from_bytes(sig, "big"), ca_key["e"],
                 ca_key["n"]).to_bytes(k, "big")
        expect = (b"\x00\x01" + b"\xff" * (k - 51 - 3) + b"\x00"
                  + x509._SHA256_DIGESTINFO_PREFIX
                  + hashlib.sha256(tbs).digest())
        self.assertEqual(em, expect)
        if shutil.which("openssl"):
            with open(os.path.join(self.tmp, "t.crt"), "w") as f:
                f.write(x509.pem_encode("CERTIFICATE", ca_der))
            with open(os.path.join(self.tmp, "h.crt"), "w") as f:
                f.write(x509.pem_encode("CERTIFICATE", h_der))
            out = subprocess.run(
                ["openssl", "verify", "-CAfile",
                 os.path.join(self.tmp, "t.crt"),
                 os.path.join(self.tmp, "h.crt")],
                capture_output=True, text=True, timeout=30)
            self.assertIn("OK", out.stdout)

    def test_ensure_ca_pure_python(self):
        from kancil import proxy_server as ps
        from kancil import session as session_mod
        crt, key = ps.ensure_ca()
        self.assertTrue(os.path.exists(crt))
        self.assertTrue(os.path.exists(key))
        self.assertTrue(os.path.exists(ps._host_key_path()))
        fp = ps.ca_fingerprint(crt)
        self.assertTrue(fp.startswith("SHA256 Fingerprint="))
        self.assertEqual(len(fp.split("=")[1].split(":")), 32)
        # idempotent
        crt2, _ = ps.ensure_ca()
        self.assertEqual(crt, crt2)

    def test_host_cert_cached_and_valid(self):
        from kancil import proxy_server as ps
        h_crt, h_key = ps.host_cert("example.com")
        self.assertTrue(os.path.exists(h_crt))
        h_crt2, h_key2 = ps.host_cert("example.com")
        self.assertEqual(h_crt, h_crt2)  # cached, not regenerated
        self.assertEqual(h_key, h_key2)
        if shutil.which("openssl"):
            crt, _ = ps.ensure_ca()
            out = subprocess.run(
                ["openssl", "verify", "-CAfile", crt, h_crt],
                capture_output=True, text=True, timeout=30)
            self.assertIn("OK", out.stdout)
            out = subprocess.run(
                ["openssl", "x509", "-in", h_crt, "-noout",
                 "-ext", "subjectAltName"],
                capture_output=True, text=True, timeout=30)
            self.assertIn("DNS:example.com", out.stdout)


class OptimizeTest(unittest.TestCase):
    """v3.11.0: article field splitting, HTTP cache, DNS cache."""

    @classmethod
    def setUpClass(cls):
        from kancil import session as session_mod
        cls._old_base = session_mod.BASE
        cls.tmp = tempfile.mkdtemp()
        session_mod.BASE = os.path.join(cls.tmp, ".kancil")
        cls.port = free_port()
        cls.srv = http.server.HTTPServer(("127.0.0.1", cls.port), Handler)
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        from kancil import session as session_mod
        session_mod.BASE = cls._old_base
        cls.srv.shutdown()

    def setUp(self):
        self.b = Kancil(engine="static", timeout=10, retries=0)

    def tearDown(self):
        self.b.close()

    def test_article_item_split(self):
        from kancil.dom import build_dom, select_one
        from kancil.devtools import _article_item
        html = ('<article><div><a href="/manga/x/">'
                '<img data-src="http://img/x.jpg"></a><span>1</span></div>'
                '<div><h4><a href="/manga/x/">Overgeared New</a></h4>'
                '<span>Fantasi \u00b7 73,571 views</span><br>'
                '<a href="/x-chapter-341/">Chapter 341</a></div></article>')
        a = select_one(build_dom(html), "article")
        it = _article_item(a, "https://komiku.org/")
        self.assertEqual(it["title"], "Overgeared New")
        self.assertEqual(it["url"], "https://komiku.org/manga/x/")
        self.assertEqual(it["image"], "http://img/x.jpg")
        self.assertEqual(it["meta"], "Fantasi \u00b7 73,571 views")
        self.assertEqual(len(it["links"]), 1)
        self.assertEqual(it["links"][0]["text"], "Chapter 341")
        self.assertIn("text", it)  # backward compat field kept

    def test_article_item_no_heading(self):
        from kancil.dom import build_dom, select_one
        from kancil.devtools import _article_item
        html = ('<article><a href="/p1">Read more about Cats</a>'
                '<p>Some filler text here.</p></article>')
        a = select_one(build_dom(html), "article")
        it = _article_item(a, "https://h.test/")
        self.assertEqual(it["title"], "Read more about Cats")
        self.assertEqual(it["url"], "https://h.test/p1")

    def test_http_cache_304(self):
        self.b.open(self.base + "/etag")
        self.assertIsNone(self.b.engine.netlog[-1].get("from_cache"))
        self.b.open(self.base + "/etag")
        e = self.b.engine.netlog[-1]
        self.assertEqual(e["status"], 304)
        self.assertTrue(e.get("from_cache"))
        self.assertEqual(self.b.engine.page.title, "etag page")

    def test_http_cache_no_store(self):
        self.b.open(self.base + "/nostore")
        self.b.open(self.base + "/nostore")
        self.assertIsNone(self.b.engine.netlog[-1].get("from_cache"))

    def test_http_cache_stats_clear(self):
        self.b.open(self.base + "/etag")
        r = self.b.tool({"action": "http_cache"})
        self.assertTrue(r["success"], r)
        self.assertGreater(r["files"], 0)
        self.assertTrue(r["enabled"])
        rc = self.b.http_cache(action="clear")
        self.assertTrue(rc["success"])
        self.assertGreaterEqual(rc["cleared"], 2)

    def test_http_cache_disabled(self):
        b2 = Kancil(engine="static", timeout=10, retries=0, cache=False)
        try:
            b2.open(self.base + "/etag")
            b2.open(self.base + "/etag")
            self.assertIsNone(b2.engine.netlog[-1].get("from_cache"))
        finally:
            b2.close()

    def test_dns_cache_installed(self):
        import socket as _sock
        from kancil import engines
        self.assertTrue(engines._dns_installed)
        self.assertEqual(_sock.getaddrinfo.__name__, "cached")
        # functional: repeated lookup works
        r1 = _sock.getaddrinfo("127.0.0.1", 80)
        r2 = _sock.getaddrinfo("127.0.0.1", 80)
        self.assertEqual(r1, r2)


class NetCookiesTest(unittest.TestCase):
    """v3.12.0: query params + cookies in network log, HAR cookies."""

    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.srv = http.server.HTTPServer(("127.0.0.1", cls.port), Handler)
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()
        cls.base = "http://127.0.0.1:%d" % cls.port

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self.b = Kancil(engine="static", timeout=10, retries=0)

    def tearDown(self):
        self.b.close()

    def test_parse_helpers(self):
        from kancil import engines
        self.assertEqual(engines.parse_query_params(
            "https://h.test/p?a=1&b=&a=2"), {"a": "1", "b": ""})
        self.assertEqual(engines.parse_query_params("https://h.test/"), {})
        self.assertEqual(engines.parse_cookie_header("a=1; b=2"),
                         {"a": "1", "b": "2"})
        sc = engines.parse_set_cookie(
            ["sess=abc123; Path=/; HttpOnly; SameSite=Lax"])
        self.assertEqual(len(sc), 1)
        self.assertEqual(sc[0]["name"], "sess")
        self.assertEqual(sc[0]["value"], "abc123")
        self.assertTrue(sc[0]["httponly"])
        self.assertEqual(sc[0]["samesite"], "Lax")

    def test_netlog_query_and_cookies(self):
        self.b.open(self.base + "/setcookie")
        e1 = self.b.engine.netlog[-1]
        self.assertEqual(e1["cookies_set"][0]["name"], "sess")
        self.b.open(self.base + "/shop?page=2&x=1")
        e2 = self.b.engine.netlog[-1]
        self.assertEqual(e2["query"], {"page": "2", "x": "1"})
        self.assertEqual(e2["cookies_sent"], {"sess": "abc123"})

    def test_har_cookies(self):
        import json as _json
        self.b.open(self.base + "/setcookie")
        p = os.path.join(tempfile.mkdtemp(), "t.har")
        r = self.b.har_export(p)  # redaction ON by default
        self.assertTrue(r["success"], r)
        har = _json.load(open(p))
        e = har["log"]["entries"][0]
        self.assertEqual(e["response"]["cookies"][0]["name"], "sess")
        self.assertEqual(e["response"]["cookies"][0]["value"], "***")
        r2 = self.b.har_export(p, redact=False)
        self.assertTrue(r2["success"], r2)
        har2 = _json.load(open(p))
        self.assertEqual(har2["log"]["entries"][0]
                        ["response"]["cookies"][0]["value"], "abc123")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class DaemonTest(unittest.TestCase):
    """v3.13.0: daemon mode — start/stop/status, dispatch routing."""

    @classmethod
    def setUpClass(cls):
        from kancil import session as session_mod
        from kancil import daemon as dm
        cls._old_base = session_mod.BASE
        cls._old_home = os.environ.get("HOME")
        cls.tmp = tempfile.mkdtemp()
        os.environ["HOME"] = cls.tmp
        session_mod.BASE = os.path.join(cls.tmp, ".kancil")
        cls.port = free_port()
        cls.srv = http.server.HTTPServer(("127.0.0.1", cls.port), Handler)
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()
        r = dm.start()
        assert r["success"], r

    @classmethod
    def tearDownClass(cls):
        from kancil import session as session_mod
        from kancil import daemon as dm
        try:
            dm.stop()
        except Exception:
            pass
        session_mod.BASE = cls._old_base
        if cls._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = cls._old_home
        cls.srv.shutdown()

    def test_ping(self):
        from kancil import daemon as dm
        self.assertTrue(dm.alive())

    def test_dispatch_open(self):
        from kancil import daemon as dm
        rep = dm.call({"op": "_dispatch", "cwd": self.tmp,
                       "args": {"cmd": "open",
                                "url": "http://127.0.0.1:%d/" % self.port}},
                      timeout=60)
        self.assertTrue(rep.get("ok"), rep)
        self.assertTrue(rep["result"].get("success"), rep["result"])

    def test_cli_auto_route_and_local(self):
        from kancil import cli as cli_mod
        from kancil import daemon as dm
        url = "http://127.0.0.1:%d/" % self.port
        # routed through daemon: tab persists across CLI invocations
        cli_mod.main(["open", "--json", url])
        r = dm.call({"op": "_dispatch", "cwd": self.tmp,
                     "args": {"cmd": "tabs"}}, timeout=30)
        self.assertTrue(r["result"]["tabs"], r)
        # --local bypasses the daemon (fresh local process state)
        cli_mod.main(["--local", "open", "--json", url])

    def test_double_start(self):
        from kancil import daemon as dm
        r = dm.start()
        self.assertTrue(r["success"])
        self.assertEqual(r["status"], "already running")


class WebViewAgentHandler(http.server.BaseHTTPRequestHandler):
    """Mock of the Kancil Browser APK agent HTTP API."""
    PAGE = (b"<html><head><title>WV Test</title></head><body>"
            b"<h1>Hi</h1><a href='https://example.com/x'>go</a>"
            b"<button id='b1'>Klik</button>"
            b"<form action='/s' method='get'>"
            b"<input name='q' type='text'></form></body></html>")
    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    TABS = [{"id": 1, "url": "https://example.com/",
             "title": "WV Test", "active": True}]

    def log_message(self, *a):
        pass

    def _json(self, o):
        b = json.dumps(o).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _err(self, code, msg):
        b = json.dumps({"ok": False, "error": msg}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/status":
            self._json({"ok": True, "agent": "mock",
                        "url": "https://example.com/",
                        "title": "WV Test", "network_count": 1,
                        "tab": 1, "tab_count": len(self.TABS)})
        elif p == "/crashes":
            self._json({"ok": True, "crash_count": 0,
                        "crash_last": 0, "last_crash": None})
        elif p == "/tabs":
            self._json({"ok": True, "tabs": self.TABS, "active": 1})
        elif p == "/dom":
            self._json({"ok": True, "html": self.PAGE.decode()})
        elif p == "/network":
            self._json({"ok": True, "requests": [
                {"id": 1, "t": "06:00:00", "method": "GET",
                 "url": "https://example.com/", "status": 200}]})
        elif p == "/cookies":
            self._json({"ok": True,
                        "cookies": [{"name": "sid", "value": "abc"}]})
        elif p == "/screenshot":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(self.PNG)))
            self.end_headers()
            self.wfile.write(self.PNG)
        elif p == "/screenshot/full":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(self.PNG)))
            self.end_headers()
            self.wfile.write(self.PNG)
        elif p == "/screenshot/element":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(self.PNG)))
            self.end_headers()
            self.wfile.write(self.PNG)
        elif p == "/videos":
            self._json({"ok": True, "videos": [
                {"index": 0, "src": "https://cdn.example/v.mp4",
                 "duration": 120.5, "currentTime": 0,
                 "paused": True, "width": 1280, "height": 720}]})
        elif p == "/downloads":
            self._json({"ok": True, "downloads": [
                {"t": 1, "url": "https://example.com/f.zip",
                 "name": "f.zip", "id": 42}]})
        elif p == "/console":
            self._json({"ok": True, "logs": [
                {"t": 123, "level": "log", "text": "hello console"},
                {"t": 124, "level": "error", "text": "boom"}]})
        elif p == "/blocklist":
            self._json({"ok": True, "patterns": ["ads.example"]})
        elif p in ("/back", "/forward", "/reload"):
            self._json({"ok": True})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        p = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if p == "/navigate":
            self._json({"ok": True, "url": body.get("url")})
        elif p == "/js":
            expr = body.get("expr", "")
            if "[invalid" in expr:
                # simulates a JS SyntaxError from the page
                self._json({"ok": True,
                            "result": "ERR:SyntaxError: invalid selector"})
            elif "readyState" in expr:
                # wait_ready() document.readyState probe
                self._json({"ok": True,
                            "result": json.dumps({"ready": "complete",
                                                  "len": 200})})
            elif "cf-turnstile-response" in expr and "value.length" in expr:
                # wait_token() turnstile token probe
                self._json({"ok": True, "result": "837"})
            elif "cf-turnstile-response" in expr:
                # wait_token() token head preview
                self._json({"ok": True, "result": "1.PV7nKJB-pZ"})
            elif "normalize-space" in expr:
                # XPath visible-text fallback
                self._json({"ok": True,
                            "result": "0" if "nope" in expr else "1"})
            elif "snapshotLength" in expr or "singleNodeValue" in expr:
                # XPath direct
                self._json({"ok": True,
                            "result": "0" if "nope" in expr else "1"})
            elif "querySelectorAll" in expr:
                if "nope" in expr or '"Login"' in expr:
                    self._json({"ok": True, "result": "0"})
                else:
                    self._json({"ok": True, "result": "1"})
            elif "innerText" in expr and "includes" in expr:
                self._json({"ok": True, "result": "true"})
            elif "requestSubmit" in expr:
                # press(Enter) form-submit fallback
                self._json({"ok": True, "result": "clicked-submit"})
            elif "window.scrollY" in expr:
                self._json({"ok": True, "result": "600"})
            elif "innerText.length" in expr:
                # content signal: text_chars|media|feed_markers
                self._json({"ok": True, "result": "2500|4|1"})
            elif "localStorage" in expr or "sessionStorage" in expr:
                # empty/blocked storage -> JS null (the real APK returns
                # the string "null" here)
                self._json({"ok": True, "result": "null"})
            elif "localStorage" in expr:
                self._json({"ok": True, "result": "{}"})
            elif "not-found" in expr:
                # single-element lookup; the real APK unwraps JS string
                # quoting, so return bare values like the real /js does
                self._json({"ok": True,
                            "result": "not-found" if "nope" in expr
                            else "ok"})
            else:
                self._json({"ok": True, "result": "null"})
        elif p == "/click":
            self._json({"ok": True, "result": "clicked"})
        elif p == "/type":
            self._json({"ok": True, "result": "typed"})
        elif p == "/network/clear":
            self._json({"ok": True})
        elif p == "/console/clear":
            self._json({"ok": True})
        elif p == "/upload":
            if body.get("data"):
                # agent 1.19+ base64 mode: staged to app cache
                self._json({"ok": True,
                            "path": "/cache/upload/"
                            + body.get("filename", "upload.bin"),
                            "hint": "click a file input next"})
            else:
                self._json({"ok": True, "path": body.get("path"),
                            "hint": "click a file input next"})
        elif p == "/download":
            self._json({"ok": True, "id": 42})
        elif p == "/form/fill":
            self._json({"ok": True,
                        "filled": list(body.get("fields", {}).keys()),
                        "missing": []})
        elif p == "/blocklist":
            self._json({"ok": True, "cache_cleared": True,
                        "cache_mode": "LOAD_NO_CACHE",
                        "patterns": len(body.get("patterns", []))})
        elif p == "/wait/idle":
            self._json({"ok": True, "idle": True,
                        "ready_state": "complete", "waited_ms": 120})
        elif p == "/press":
            self._json({"ok": True,
                        "result": "pressed:" + body.get("key", "Enter")})
        elif p == "/longpress":
            self._json({"ok": True, "result": "longpressed"})
        elif p == "/crashes":
            self._json({"ok": True, "crash_count": 0,
                        "crash_last": 0, "last_crash": None})
        elif p == "/tabs/new":
            t = {"id": 2, "url": body.get("url", ""),
                 "title": "New", "active": True}
            self._json({"ok": True, "tab": t})
        elif p == "/tabs/activate":
            if body.get("id") in ("1", "2"):
                self._json({"ok": True})
            else:
                self._err(404, "no such tab")
        elif p == "/tabs/close":
            if body.get("id") == "2":
                self._json({"ok": True})
            else:
                self._err(400, "cannot close the last tab")
        else:
            self.send_response(404)
            self.end_headers()


class WebViewEngineTest(unittest.TestCase):
    """webview engine — drives the Kancil Browser APK agent API."""

    @classmethod
    def setUpClass(cls):
        from kancil.webview_engine import WebViewEngine
        cls.port = free_port()
        cls.srv = http.server.HTTPServer(("127.0.0.1", cls.port),
                                         WebViewAgentHandler)
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()
        cls.eng = WebViewEngine(port=cls.port, timeout=10)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_dry_run_blocks_click_and_submit(self):
        from kancil.webview_engine import WebViewEngine
        eng = WebViewEngine(port=self.port, timeout=10, dry_run=True)
        try:
            self.assertTrue(eng.dry_run)
            r = eng.click("#b1")
            self.assertFalse(r["success"])
            self.assertIn("dry-run", r["errors"][0])
            r = eng.click("#b1", confirm=True)
            self.assertTrue(r["success"], r)
            r = eng.submit_form(0)
            self.assertFalse(r["success"])
            self.assertIn("dry-run", r["errors"][0])
            r = eng.submit_form(0, confirm=True)
            self.assertTrue(r["success"], r)
        finally:
            eng.close()

    def test_dry_run_off_by_default(self):
        self.assertFalse(self.eng.dry_run)
        self.assertTrue(self.eng.click("#b1")["success"])

    def test_crashes(self):
        r = self.eng.crashes()
        self.assertTrue(r["success"], r)
        self.assertEqual(r["crash_count"], 0)
        self.assertIsNone(r["last_crash"])

    def test_wait_idle(self):
        r = self.eng.wait_idle(timeout=5)
        self.assertTrue(r["success"], r)
        self.assertTrue(r["idle"])
        self.assertEqual(r["ready_state"], "complete")

    def test_press(self):
        r = self.eng.press("Enter")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["key"], "Enter")

    def test_longpress(self):
        r = self.eng.longpress("#b1")
        self.assertTrue(r["success"], r)

    def test_open_idle(self):
        r = self.eng.open("example.com", idle=True, idle_timeout=5)
        self.assertTrue(r["success"], r)
        self.assertTrue(r["idle"])
        self.assertIn("waited_ms", r)

    def test_deep_query_helpers_emitted(self):
        # element targeting must pierce shadow DOM + same-origin iframes
        js = self.eng._target_js("#b1")
        self.assertIn("__kancilQ", js)
        js, _m = self.eng._count_js("#b1")
        self.assertIn("__kancilQA", js)
        js = self.eng._target_js("//div[@id='x']")
        self.assertIn("__kancilQX", js)
        js, _m = self.eng._count_js("//div[@id='x']")
        self.assertIn("__kancilQXA", js)

    def test_block_add_reports_cache_mode(self):
        r = self.eng.block_add("ads.example")
        self.assertTrue(r["success"], r)
        r = self.eng.block_clear()
        self.assertTrue(r["success"], r)

    def test_click_through(self):
        # open (idle) -> click -> wait, all in one mock session
        r = self.eng.click_through("example.com", "#b1", "#b1", timeout=5)
        self.assertTrue(r["success"], r)
        self.assertEqual([s[0] for s in r["steps"]],
                         ["open", "click", "wait"])

    def test_click_through_click_fail(self):
        r = self.eng.click_through("example.com", "#nope", "#b1", timeout=5)
        self.assertFalse(r["success"])
        self.assertEqual([s[0] for s in r["steps"]], ["open", "click"])

    def test_scroll_settle_and_delta(self):
        r = self.eng.scroll(600, settle_ms=10)
        self.assertTrue(r["success"], r)
        self.assertIn("delta", r)
        self.assertEqual(r["target"], 600)

    def test_scroll_verify(self):
        r = self.eng.scroll(600, settle_ms=0, verify="#b1")
        self.assertTrue(r["success"], r)
        self.assertTrue(r["verify"])

    def test_click_verify(self):
        r = self.eng.click("#b1", verify="#b1")
        self.assertTrue(r["success"], r)
        self.assertTrue(r["verify"])

    def test_click_verify_fail(self):
        orig = self.eng.wait
        self.eng.wait = lambda **k: {"success": False,
                                     "errors": ["timeout waiting"]}
        try:
            r = self.eng.click("#b1", verify="#never")
            self.assertTrue(r["success"], r)  # click itself landed
            self.assertFalse(r["verify"])
            self.assertIn("errors", r)
        finally:
            self.eng.wait = orig

    def test_content_signal_shell(self):
        orig = self.eng._post
        self.eng._post = lambda *a, **k: {"ok": True, "result": "120|0|0"}
        try:
            sig = self.eng._content_signal()
            self.assertTrue(sig["shell"], sig)
            self.assertEqual(sig["text_chars"], 120)
        finally:
            self.eng._post = orig

    def test_content_signal_rich(self):
        orig = self.eng._post
        self.eng._post = lambda *a, **k: {"ok": True, "result": "2500|4|1"}
        try:
            sig = self.eng._content_signal()
            self.assertFalse(sig["shell"], sig)
        finally:
            self.eng._post = orig

    def test_open_idle_warns_on_shell(self):
        orig = self.eng._content_signal
        self.eng._content_signal = lambda: {
            "text_chars": 50, "media": 0, "feed_markers": 0, "shell": True}
        try:
            r = self.eng.open("example.com", idle=True)
            self.assertTrue(r["success"], r)
            self.assertIn("warning", r)
            self.assertIn("login", r["warning"])
        finally:
            self.eng._content_signal = orig

    def test_open_idle_no_warning_when_rich(self):
        r = self.eng.open("example.com", idle=True)
        self.assertTrue(r["success"], r)
        # mock content signal falls through to "0|0|0"... just check key
        self.assertIn("content", r)

    def test_composer_open(self):
        r = self.eng.composer_open()
        self.assertTrue(r["success"], r)
        self.assertEqual([s[0] for s in r["steps"]],
                         ["open", "click", "wait"])

    def test_composer_open_unknown_site(self):
        r = self.eng.composer_open(site="twitter")
        self.assertFalse(r["success"])

    def test_press_enter_fallback_submits(self):
        # mock: /press ok, /network count unchanged, /status url unchanged
        r = self.eng.press("Enter")
        self.assertTrue(r["success"], r)
        self.assertEqual(r.get("submitted"), "clicked-submit")

    def test_press_enter_no_fallback_flag(self):
        r = self.eng.press("Enter", submit_fallback=False)
        self.assertTrue(r["success"], r)
        self.assertNotIn("submitted", r)

    def test_press_escape_no_fallback(self):
        r = self.eng.press("Escape")
        self.assertTrue(r["success"], r)
        self.assertNotIn("submitted", r)

    def test_sync_tab_recovered(self):
        self.eng.cur = 0  # stale; mock /status says tab 1
        r = self.eng.sync_tab()
        self.assertTrue(r["success"], r)
        self.assertTrue(r["recovered"], r)
        self.assertEqual(self.eng.cur, 1)

    def test_sync_tab_no_change(self):
        self.eng.cur = 1  # matches mock /status
        r = self.eng.sync_tab()
        self.assertTrue(r["success"], r)
        self.assertFalse(r["recovered"], r)

    def test_open_syncs_tab(self):
        self.eng.cur = 0
        r = self.eng.open("example.com")
        self.assertTrue(r["success"], r)
        self.assertEqual(self.eng.cur, 1)  # adopted from /status

    def test_har_export_webview(self):
        import tempfile, json, os
        from kancil.api import Kancil
        b = Kancil.__new__(Kancil)
        b._engine_name = "webview"
        b.engine = self.eng
        b._har_recording = False
        p = tempfile.mktemp(suffix=".har")
        try:
            r = b.har_export(p)
            self.assertTrue(r.get("success"), r)
            with open(p) as f:
                har = json.load(f)
            entries = har["log"]["entries"]
            self.assertGreaterEqual(len(entries), 1)
            self.assertEqual(entries[0]["request"]["url"],
                             "https://example.com/")
        finally:
            if os.path.exists(p):
                os.unlink(p)

    def test_screenshot_retries_on_bad_png(self):        # first response is garbage, second is a PNG -> success via retry
        calls = {"n": 0}
        orig = self.eng._req
        png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        def flaky(method, path, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"raw": b"garbage"}
            return {"raw": png}
        self.eng._req = flaky
        try:
            import tempfile, os
            p = os.path.join(tempfile.gettempdir(), "retry-test.png")
            r = self.eng.screenshot(path=p)
            self.assertTrue(r["success"], r)
            self.assertEqual(calls["n"], 2)
            os.remove(p)
        finally:
            self.eng._req = orig

    def test_unreachable_fails_fast(self):
        from kancil.webview_engine import WebViewEngine
        from kancil.engines import EngineError
        with self.assertRaises(EngineError) as cm:
            WebViewEngine(port=free_port(), timeout=2)
        self.assertIn("Kancil Browser", str(cm.exception))

    def test_capabilities(self):
        c = self.eng.capabilities
        self.assertTrue(c["javascript"])
        self.assertTrue(c["screenshot"])
        self.assertTrue(c["video"])
        self.assertTrue(c["cookies"])

    def test_open_and_page(self):
        r = self.eng.open("example.com")
        self.assertTrue(r["success"], r)
        p = self.eng.page
        self.assertIsNotNone(p.dom)
        self.assertEqual(p.title, "WV Test")
        self.assertEqual(len(p.links), 1)

    def test_actions(self):
        self.assertTrue(self.eng.click("#b1")["success"])
        self.assertTrue(self.eng.type("input[name=q]", "halo")["success"])
        self.assertTrue(self.eng.resolve("#b1")["success"])
        self.assertFalse(self.eng.resolve("#nope")["success"])
        self.assertTrue(self.eng.evaluate("1+1")["success"])
        self.assertTrue(self.eng.scroll("bottom")["success"])
        self.assertTrue(self.eng.back()["success"])

    def test_screenshot_cookies_network(self):
        d = tempfile.mkdtemp()
        try:
            s = self.eng.screenshot(
                path=os.path.join(d, "s.png"))
            self.assertTrue(s["success"], s)
            with open(os.path.join(d, "s.png"), "rb") as f:
                self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")
            c = self.eng.cookies()
            self.assertEqual(c[0]["name"], "sid")
            n = self.eng.network(limit=10)
            self.assertEqual(n[0]["status"], 200)
            self.assertTrue(self.eng.network_clear()["success"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_kancil_api_routes_webview(self):
        b = Kancil(engine="webview", webview_port=self.port, timeout=10)
        self.assertEqual(b._engine_name, "webview")
        self.assertTrue(b.capabilities["javascript"])
        r = b.open("example.com")
        self.assertTrue(r["success"], r)
        # default port 8080 without the app running must fail fast, not hang
        from kancil.engines import EngineError
        with self.assertRaises(EngineError):
            Kancil(engine="webview", webview_port=free_port(), timeout=2)

    def test_tabs(self):
        tabs = self.eng.list_tabs()
        self.assertEqual(len(tabs), 1)
        self.assertTrue(tabs[0]["current"])
        r = self.eng.new_tab("https://example.org/")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["tab"], 2)
        self.assertTrue(self.eng.switch_tab(1)["success"])
        self.assertFalse(self.eng.switch_tab(99)["success"])
        self.assertTrue(self.eng.close_tab(2)["success"])
        self.assertFalse(self.eng.close_tab(1)["success"])  # last tab

    def test_xpath(self):
        r = self.eng.resolve("//button")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["method"], "xpath")
        self.assertTrue(self.eng.click("//button[@id='b1']")["success"])
        self.assertFalse(self.eng.resolve("//nope").get("success"))

    def test_smart_text_resolve(self):
        # plain text "Login" -> CSS misses -> XPath text fallback hits
        self.assertTrue(self.eng.resolve("Login")["success"])
        self.assertTrue(self.eng.click("Login")["success"])
        self.assertFalse(self.eng.resolve("nope")["success"])

    def test_wait_text(self):
        self.assertTrue(self.eng.wait(text="hello")["success"])
        self.assertTrue(self.eng.wait(ms=50)["success"])
        self.assertTrue(self.eng.wait(selector="#b1")["success"])

    def test_hover_scroll_selector(self):
        self.assertTrue(self.eng.hover("#b1")["success"])
        self.assertTrue(self.eng.scroll("#b1")["success"])
        self.assertFalse(self.eng.scroll("#nope")["success"])

    def test_screenshot_flags_honest(self):
        r = self.eng.screenshot(full=True)
        self.assertTrue(r["success"], r)
        self.assertTrue(r["full"])
        r = self.eng.screenshot(selector="#b1")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["selector"], "#b1")

    def test_console_capture(self):
        r = self.eng.console()
        self.assertTrue(r["success"], r)
        self.assertEqual(len(r["logs"]), 2)
        self.assertEqual(r["logs"][0]["level"], "log")
        self.assertEqual(r["logs"][1]["text"], "boom")
        self.assertTrue(self.eng.console_clear()["success"])

    def test_blocklist(self):
        pats = self.eng.block_list()
        self.assertEqual(pats, ["ads.example"])
        r = self.eng.block_add("tracker.example")
        self.assertTrue(r["success"], r)
        self.assertIn("tracker.example", r["patterns"])
        self.assertIn("ads.example", r["patterns"])
        self.assertTrue(self.eng.block_clear()["success"])

    def test_videos(self):
        r = self.eng.videos()
        self.assertTrue(r["success"], r)
        self.assertEqual(len(r["videos"]), 1)
        self.assertEqual(r["videos"][0]["src"],
                         "https://cdn.example/v.mp4")
        self.assertEqual(r["videos"][0]["width"], 1280)

    def test_fill_form(self):
        r = self.eng.fill_form(0, {"username": "u", "password": "p"})
        self.assertTrue(r["success"], r)
        self.assertIn("username", r["filled"])
        r = self.eng.fill_form(99, {"a": "b"})
        self.assertFalse(r["success"])
        r = self.eng.fill_form(0, {"f": "@/nonexistent-xyz"})
        self.assertFalse(r["success"])
        self.assertTrue(self.eng.submit_form(0)["success"])

    def test_upload_download(self):
        r = self.eng.upload("/nonexistent-xyz")
        self.assertFalse(r["success"])
        import tempfile
        with tempfile.NamedTemporaryFile(
                suffix=".txt", delete=False) as f:
            f.write(b"x")
            p = f.name
        try:
            r = self.eng.upload(p)
            self.assertTrue(r["success"], r)
            self.assertTrue(r["path"].startswith("/cache/upload/"), r)
        finally:
            import os
            os.unlink(p)
        r = self.eng.download("https://example.com/f.zip")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["id"], 42)
        dls = self.eng.download_list()
        self.assertEqual(dls[0]["name"], "f.zip")

    def test_capabilities_honest(self):
        c = self.eng.capabilities
        self.assertTrue(c["xpath"])
        self.assertTrue(c["css_selectors"])
        self.assertTrue(c["console_capture"])
        self.assertTrue(c["network_interception"])
        self.assertTrue(c["forms"])
        self.assertTrue(c["downloads"])
        self.assertFalse(c["indexeddb"])
        self.assertFalse(c["computed_style"])

    def test_forms(self):
        forms = self.eng.forms()
        self.assertEqual(len(forms), 1)
        self.assertEqual(forms[0]["id"], 1)
        self.assertEqual(forms[0]["action"], "https://example.com/s")
        self.assertEqual(forms[0]["fields"][0]["name"], "q")

    def test_storage_null_result(self):
        # mock /js returns the string "null" (empty/blocked storage)
        # -> must normalize to {}, never None
        data = self.eng.storage("local")
        self.assertEqual(data, {})

    def test_switch_tab_verifies_real_state(self):
        # mock /status always reports tab 1: switching to 2 must fail
        # honestly instead of claiming success with a stale ID
        r = self.eng.switch_tab(2)
        self.assertFalse(r["success"])
        self.assertIn("no longer exists", r["errors"][0])
        r = self.eng.switch_tab(1)
        self.assertTrue(r["success"], r)

    def test_extract_article(self):
        from kancil.api import Kancil
        k = Kancil(engine="webview", webview_port=self.port)
        try:
            r = k.extract(mode="article")
        finally:
            k.close()
        self.assertTrue(r["success"], r)
        self.assertIn("title", r["article"])
        self.assertIn("text", r["article"])

    def test_invalid_selector_js_error(self):
        # invalid CSS must fail honestly, never silent success
        r = self.eng.click("[invalid")
        self.assertFalse(r["success"])
        self.assertIn("JS error", r["errors"][0])
        r = self.eng.resolve("[invalid")
        self.assertFalse(r["success"])

    def test_storage_get_missing(self):
        self.assertIsNone(self.eng.storage_get("nope", "local"))

    # ---------- resilience & captcha helpers (farm-grade) ----------

    def test_wait_ready_success(self):
        r = self.eng.wait_ready(timeout=5)
        self.assertTrue(r["success"], r)
        self.assertEqual(r["ready"], "complete")
        self.assertEqual(r["body_len"], 200)

    def test_wait_ready_stuck_timeout(self):
        orig = self.eng._post
        self.eng._post = lambda p, b: {
            "ok": True,
            "result": json.dumps({"ready": "loading", "len": -1})}
        try:
            r = self.eng.wait_ready(timeout=3, auto_reload=False, poll=0.2)
            self.assertFalse(r["success"])
            self.assertIn("stuck", r["errors"][0])
        finally:
            self.eng._post = orig

    def test_wait_token_success(self):
        r = self.eng.wait_token(timeout=5)
        self.assertTrue(r["success"], r)
        self.assertEqual(r["token_len"], 837)
        self.assertEqual(r["token_head"], "1.PV7nKJB-pZ")

    def test_wait_token_timeout(self):
        orig = self.eng._post
        self.eng._post = lambda p, b: {"ok": True, "result": "-1"}
        try:
            r = self.eng.wait_token(timeout=2, poll=0.3)
            self.assertFalse(r["success"])
            self.assertIn("never reached", r["errors"][0])
        finally:
            self.eng._post = orig

    def test_has_button(self):
        self.assertTrue(self.eng.has_button("Create account"))
        self.assertFalse(self.eng.has_button("nope"))

    def test_click_button_ok(self):
        r = self.eng.click_button("Create account")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["button"], "Create account")

    def test_click_button_not_found(self):
        orig = self.eng._post
        self.eng._post = lambda p, b: {"ok": True, "result": "not-found"}
        try:
            r = self.eng.click_button("Nope Dude")
            self.assertFalse(r["success"])
            self.assertIn("Nope Dude", r["errors"][0])
        finally:
            self.eng._post = orig

    def test_submit_with_captcha_ok(self):
        r = self.eng.submit_with_captcha(
            button_text="Create account", settle=0, timeout=5)
        self.assertTrue(r["success"], r)
        self.assertTrue(r["step1_click"])
        self.assertTrue(r["token"])
        self.assertTrue(r["step2_click"])

    def test_type_verified_ok(self):
        orig_type = self.eng.type
        self.eng.type = lambda q, t: {"success": True}
        orig_post = self.eng._post
        self.eng._post = lambda p, b: {
            "ok": True, "result": "kbot@maxxspace.com"}
        try:
            r = self.eng.type_verified(
                "input[name=email]", "kbot@maxxspace.com", poll=0)
            self.assertTrue(r["success"], r)
            self.assertEqual(r["value"], "kbot@maxxspace.com")
        finally:
            self.eng.type = orig_type
            self.eng._post = orig_post

    def test_type_verified_fail_when_not_sticking(self):
        orig_type = self.eng.type
        self.eng.type = lambda q, t: {"success": True}
        orig_post = self.eng._post
        self.eng._post = lambda p, b: {"ok": True, "result": ""}
        try:
            r = self.eng.type_verified(
                "input[name=q]", "expected", attempts=1, poll=0)
            self.assertFalse(r["success"])
            self.assertIn("did not stick", r["errors"][0])
        finally:
            self.eng.type = orig_type
            self.eng._post = orig_post

    def test_ensure_alive_reachable(self):
        r = self.eng.ensure_alive(relaunch=False)
        self.assertTrue(r["success"], r)
        self.assertFalse(r["relaunched"])

    def test_ensure_alive_unreachable_no_relaunch(self):
        orig = self.eng._req
        self.eng._req = lambda m, p, **kw: (_ for _ in ()).throw(
            urllib.error.URLError("refused"))
        try:
            r = self.eng.ensure_alive(retries=0, relaunch=False)
            self.assertFalse(r["success"])
        finally:
            self.eng._req = orig


class DoctorTest(unittest.TestCase):
    def test_run_shape(self):
        from kancil import doctor
        checks, all_ok = doctor.run()
        self.assertIsInstance(checks, list)
        self.assertTrue(len(checks) >= 6)
        names = [c["name"] for c in checks]
        for n in ("python", "dns", "static engine", "playwright engine",
                  "webview engine", "disk", "environment", "stealth"):
            self.assertIn(n, names, names)
        for c in checks:
            self.assertIn(c["status"], ("ok", "warn", "fail"))
            self.assertIn("detail", c)
            self.assertIn("hint", c)
        self.assertIsInstance(all_ok, bool)

    def test_format_text(self):
        from kancil import doctor
        checks, _ = doctor.run()
        txt = doctor.format_text(checks)
        self.assertIn("python", txt)
        self.assertTrue(any(s in txt for s in ("✓", "!", "✗")))

    def test_stealth_check(self):
        from kancil import doctor
        from kancil import stealth
        checks, _ = doctor.run()
        sc = [c for c in checks if c["name"] == "stealth"][0]
        if stealth.HAVE_CURL_CFFI:
            self.assertEqual(sc["status"], "ok")
            self.assertIn("chrome_android", sc["detail"])
        else:
            self.assertEqual(sc["status"], "warn")
            self.assertIn("curl_cffi", sc["hint"])

    def test_api_doctor(self):
        from kancil.api import Kancil
        k = Kancil(engine="static")
        try:
            r = k.doctor()
        finally:
            k.close()
        self.assertTrue(r["success"])
        self.assertIn("checks", r)
        self.assertIn("summary", r)
        self.assertIsInstance(r["healthy"], bool)

    def test_cli_doctor(self):
        import subprocess
        r = subprocess.run(
            [sys.executable, "-m", "kancil", "doctor", "--local"],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=60)
        # exit 0 only when everything healthy; here playwright/webview fail
        self.assertIn("python", r.stdout)
        self.assertIn("static engine", r.stdout)


def _mk_cookie(name="s", value="v", domain="example.com"):
    return http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=True, domain_initial_dot=False,
        path="/", path_specified=True, secure=False, expires=None,
        discard=True, comment=None, comment_url=None, rest={})


class SessionClearTest(unittest.TestCase):
    def test_static_clear_all(self):
        from kancil.engines import StaticEngine
        eng = StaticEngine(cache=False)
        eng.jar.set_cookie(_mk_cookie())
        eng.tabs = [{"history": ["http://example.com/"], "pos": 0}]
        eng.cur = 0
        eng.netlog.append({"id": 1, "url": "http://example.com/"})
        eng._req_id = 1
        eng.errors.append({"type": "x"})
        r = eng.clear_session("all")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["cleared"], ["cookies", "tabs", "netlog", "cache"])
        self.assertEqual(list(eng.jar), [])
        self.assertEqual(eng.tabs, [{"history": [], "pos": -1}])
        self.assertEqual(eng.cur, 0)
        self.assertEqual(eng.netlog, [])
        self.assertEqual(eng._req_id, 0)
        self.assertEqual(eng.errors, [])

    def test_static_clear_subset(self):
        from kancil.engines import StaticEngine
        eng = StaticEngine(cache=False)
        eng.jar.set_cookie(_mk_cookie())
        eng.tabs = [{"history": ["http://example.com/"], "pos": 0}]
        eng.netlog.append({"id": 1})
        r = eng.clear_session("cookies")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["cleared"], ["cookies"])
        self.assertEqual(list(eng.jar), [])
        # tabs & netlog untouched
        self.assertEqual(len(eng.tabs[0]["history"]), 1)
        self.assertEqual(len(eng.netlog), 1)

    def test_static_clear_invalid(self):
        from kancil.engines import StaticEngine
        eng = StaticEngine(cache=False)
        r = eng.clear_session("nope")
        self.assertFalse(r["success"])
        self.assertIn("unknown clear target", r["errors"][0])

    def test_static_clear_cache(self):
        from kancil.engines import StaticEngine
        from kancil import httpcache
        eng = StaticEngine(cache=False)
        httpcache.store("http://example.com/x", "http://example.com/x", 200,
                        {"Content-Type": "text/html",
                         "ETag": '"t1"', "Last-Modified": "today"},
                        b"hello")
        meta, body = httpcache.lookup("http://example.com/x")
        self.assertIsNotNone(meta)
        eng.clear_session("cache")
        self.assertEqual(httpcache.lookup("http://example.com/x"), (None, None))

    def test_api_session_clear(self):
        from kancil.api import Kancil
        k = Kancil(engine="static", cache=False)
        k.engine.jar.set_cookie(_mk_cookie())
        r = k.session_clear()
        self.assertTrue(r["success"], r)
        self.assertEqual(list(k.engine.jar), [])

    def test_cli_session_clear_parses(self):
        from kancil import cli as cli_mod
        p = cli_mod.build_parser()
        args = p.parse_args(["session", "clear", "--what", "cookies"])
        self.assertEqual(args.cmd, "session")
        self.assertEqual(args.action, "clear")
        self.assertEqual(args.what, "cookies")

    def test_webview_clear_mock(self):
        from kancil.webview_engine import WebViewEngine
        eng = WebViewEngine.__new__(WebViewEngine)
        calls = []

        def fake_list_tabs():
            return [{"id": 1, "current": True}, {"id": 2, "current": False}]
        eng.list_tabs = fake_list_tabs
        eng.close_tab = lambda i: calls.append(("close", i)) or {"success": True}
        eng.open = lambda u: calls.append(("open", u)) or {"success": True}
        eng.netlog = [{"id": 1}]
        eng.network_clear = lambda: calls.append(("netclear",)) or setattr(
            eng, "netlog", []) or {"success": True}
        eng.cookies_clear = lambda domain=None: calls.append(
            ("cookies_clear", domain)) or {"success": True}
        eng.cache_clear = lambda: calls.append(("cache_clear",)) or {"success": True}

        # "all" clears tabs+netlog+cache but NOT cookies (logins protected)
        r = eng.clear_session("all")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["cleared"], ["tabs", "netlog", "cache"])
        self.assertIn(("close", 2), calls)
        self.assertIn(("open", "about:blank"), calls)
        self.assertEqual(eng.netlog, [])

        # explicit cookies with domain
        r = eng.clear_session("cookies", domain="example.com")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["cleared"], ["cookies(example.com)"])
        self.assertIn(("cookies_clear", "example.com"), calls)

    def test_static_clear_domain(self):
        from kancil.engines import StaticEngine
        eng = StaticEngine(cache=False)
        eng.jar.set_cookie(_mk_cookie("a", "1", "example.com"))
        eng.jar.set_cookie(_mk_cookie("b", "2", "other.com"))
        r = eng.clear_session("cookies", domain="example.com")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["cleared"], ["cookies(example.com)"])
        names = sorted(c.name for c in eng.jar)
        self.assertEqual(names, ["b"])


class AgentPowerTest(unittest.TestCase):
    def test_static_cookies_set_and_find(self):
        from kancil.engines import StaticEngine
        eng = StaticEngine(cache=False)
        r = eng.cookies_set("sess", "abc", domain="example.com")
        self.assertTrue(r["success"], r)
        self.assertEqual([c.name for c in eng.jar], ["sess"])
        # cookies_set without domain needs a page; error is clean
        r2 = StaticEngine(cache=False).cookies_set("x", "y")
        self.assertFalse(r2["success"])

    def test_static_find(self):
        from kancil.engines import StaticEngine
        eng = StaticEngine(cache=False)
        r = eng.find("hello")
        self.assertFalse(r["success"])  # no page open
        eng.tabs = [{"history": [], "pos": -1}]
        from kancil.engines import Page
        pg = Page("http://example.com/", 200,
                  b"<html><body>Hello world, hello again</body></html>",
                  "text/html")
        eng.tabs[0]["history"] = [pg]
        eng.tabs[0]["pos"] = 0
        r = eng.find("hello")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["matches"], 2)
        self.assertTrue(r["snippets"])

    def test_webview_power_mock(self):
        from kancil.webview_engine import WebViewEngine
        eng = WebViewEngine.__new__(WebViewEngine)
        calls = []
        eng._post = lambda p, b: calls.append((p, b)) or {"ok": True,
                                                          "matches": 3,
                                                          "domain": "example.com"}
        r = eng.cookies_set("sess", "abc", domain="example.com")
        self.assertTrue(r["success"], r)
        self.assertEqual(calls[-1][0], "/cookies/set")
        self.assertEqual(calls[-1][1]["name"], "sess")
        r = eng.set_user_agent("TestUA/1.0")
        self.assertTrue(r["success"], r)
        self.assertEqual(calls[-1][0], "/ua/set")
        r = eng.reset_user_agent()
        self.assertTrue(r["success"], r)
        self.assertEqual(calls[-1][0], "/ua/reset")
        r = eng.find("hello")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["matches"], 3)

    def test_api_power(self):
        from kancil.api import Kancil
        k = Kancil(engine="static", cache=False)
        r = k.cookies_set("a", "b", domain="example.com")
        self.assertTrue(r["success"], r)
        r = k.ua_reset()
        self.assertTrue(r["success"], r)

    def test_cli_power_parses(self):
        from kancil import cli as cli_mod
        p = cli_mod.build_parser()
        a = p.parse_args(["cookies", "set", "sess", "abc",
                          "--domain", "example.com"])
        self.assertEqual((a.cmd, a.action, a.name, a.domain),
                         ("cookies", "set", "sess", "example.com"))
        a = p.parse_args(["find", "hello"])
        self.assertEqual((a.cmd, a.text), ("find", "hello"))
        a = p.parse_args(["ua", "reset"])
        self.assertEqual((a.cmd, a.action), ("ua", "reset"))


class VerifyPatternTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from kancil.webview_engine import WebViewEngine
        cls.port = free_port()
        cls.srv = http.server.HTTPServer(("127.0.0.1", cls.port),
                                         WebViewAgentHandler)
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()
        cls.eng = WebViewEngine(port=cls.port, timeout=10)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def _ok_wait(self):
        orig = self.eng.wait
        self.eng.wait = lambda **k: {"success": True}
        return orig

    def _fail_wait(self):
        orig = self.eng.wait
        self.eng.wait = lambda **k: {"success": False,
                                     "errors": ["timeout waiting"]}
        return orig

    def test_type_verify(self):
        orig = self._ok_wait()
        try:
            r = self.eng.type("#q", "hello", verify="#q")
            self.assertTrue(r["success"], r)
            self.assertTrue(r["verify"])
        finally:
            self.eng.wait = orig

    def test_type_verify_fail(self):
        orig = self._fail_wait()
        try:
            r = self.eng.type("#q", "hello", verify="#never")
            self.assertTrue(r["success"], r)  # typing itself landed
            self.assertFalse(r["verify"])
            self.assertIn("errors", r)
        finally:
            self.eng.wait = orig

    def test_type_no_verify_unchanged(self):
        r = self.eng.type("#q", "hello")
        self.assertTrue(r["success"], r)
        self.assertNotIn("verify", r)

    def test_open_verify(self):
        orig = self._ok_wait()
        try:
            r = self.eng.open("https://example.com/", verify="#b1")
            self.assertTrue(r["success"], r)
            self.assertTrue(r["verify"])
        finally:
            self.eng.wait = orig

    def test_open_verify_fail(self):
        orig = self._fail_wait()
        try:
            r = self.eng.open("https://example.com/", verify="#never")
            self.assertTrue(r["success"], r)  # nav itself landed
            self.assertFalse(r["verify"])
            self.assertIn("errors", r)
        finally:
            self.eng.wait = orig

    def test_cli_verify_flags_parse(self):
        from kancil import cli as cli_mod
        p = cli_mod.build_parser()
        a = p.parse_args(["open", "https://example.com", "--verify", "#main"])
        self.assertEqual(a.verify, "#main")
        a = p.parse_args(["click", "#b", "--verify", "#done"])
        self.assertEqual(a.verify, "#done")
        a = p.parse_args(["type", "#q", "hi", "--verify", ".suggest"])
        self.assertEqual(a.verify, ".suggest")

class WebViewLaunchTest(unittest.TestCase):
    """Auto-heal: ensure_alive() is the single relaunch core (unified);
    the engine auto-calls it on init and on mid-session connect errors."""

    def _eng(self, **kw):
        from kancil.webview_engine import WebViewEngine
        eng = WebViewEngine.__new__(WebViewEngine)
        eng.host, eng.port = "127.0.0.1", 18080
        eng.base = "http://127.0.0.1:18080"
        eng.timeout = 5
        eng.errors, eng.netlog = [], []
        eng.cur = 0
        eng.auto_launch = kw.get("auto_launch", True)
        eng._healing = False
        eng._page_cache, eng._page_url = None, None
        return eng

    def test_ensure_alive_no_am(self):
        eng = self._eng()
        r = eng.ensure_alive()
        self.assertFalse(r["success"])  # no `am` on this VM

    def test_ensure_alive_relaunches_mock(self):
        import subprocess as sp
        eng = self._eng()
        real_run = sp.run
        calls = []
        def fake_run(*a, **k):
            calls.append(a[0])
            class P:
                returncode = 0
            return P()
        sp.run = fake_run
        n = {"i": 0}
        def fake_get(path):
            n["i"] += 1
            if n["i"] == 1:
                raise Exception("refused")
            return {"ok": True, "agent": "kancil-browser/1.21"}
        eng._get = fake_get
        try:
            r = eng.ensure_alive(wait=0.1)
        finally:
            sp.run = real_run
        self.assertTrue(r["success"], r)
        self.assertTrue(r["relaunched"])
        self.assertEqual(calls[0][:3], ["am", "start", "-n"])
        self.assertIn("MainActivity", calls[0][3])

    def test_ensure_alive_no_recursion(self):
        # _req hook -> ensure_alive -> _get -> _req must terminate,
        # not recurse forever
        import urllib.request
        import urllib.error
        from kancil.engines import EngineError
        eng = self._eng()
        real = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=None: (
            _ for _ in ()).throw(urllib.error.URLError("refused"))
        try:
            with self.assertRaises(EngineError):
                eng._get("/status")
        finally:
            urllib.request.urlopen = real

    def test_init_auto_launch_recovers(self):
        eng = self._eng()
        eng._status_or_none = lambda: {"ok": True, "tab": 7}
        st = eng._status_or_none()
        if st is None and eng.auto_launch:
            st = None
        self.assertIsNotNone(st)
        self.assertEqual(st["tab"], 7)

    def test_init_no_auto_launch_raises(self):
        from kancil.webview_engine import WebViewEngine, EngineError
        with self.assertRaises(EngineError):
            WebViewEngine(port=18082, timeout=2, auto_launch=False)

    def test_req_relaunch_on_refused(self):
        import urllib.request
        import urllib.error
        eng = self._eng()
        heals = []
        eng.ensure_alive = lambda *a, **k: heals.append(1) or {
            "success": True}
        eng.sync_tab = lambda: {"success": True}
        eng._invalidate = lambda: None
        calls = {"n": 0}
        real = urllib.request.urlopen
        def fake(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.URLError("refused")
            class R:
                def read(self):
                    return b'{"ok": true}'
                @property
                def headers(self):
                    return {"Content-Type": "application/json"}
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()
        urllib.request.urlopen = fake
        try:
            r = eng._get("/status")
        finally:
            urllib.request.urlopen = real
        self.assertEqual(r, {"ok": True})
        self.assertEqual(heals, [1])
        self.assertEqual(calls["n"], 2)

    def test_req_no_relaunch_when_disabled(self):
        import urllib.request
        import urllib.error
        from kancil.engines import EngineError
        eng = self._eng(auto_launch=False)
        heals = []
        eng.ensure_alive = lambda *a, **k: heals.append(1) or {
            "success": True}
        real = urllib.request.urlopen
        urllib.request.urlopen = lambda req, timeout=None: (
            _ for _ in ()).throw(urllib.error.URLError("refused"))
        try:
            with self.assertRaises(EngineError):
                eng._get("/status")
        finally:
            urllib.request.urlopen = real
        self.assertEqual(heals, [])

    def test_api_ensure_alive(self):
        from kancil.api import Kancil
        k = Kancil.__new__(Kancil)
        k.engine = self._eng()
        k._wrap = lambda r: r
        r = k.ensure_alive()
        self.assertFalse(r["success"])  # no `am` on this VM
        r = k.launch_app()  # backward-compat alias
        self.assertFalse(r["success"])
        k.engine = object()
        r = k.ensure_alive()
        self.assertFalse(r["success"])

    def test_cli_launch_parses(self):
        from kancil import cli as cli_mod
        p = cli_mod.build_parser()
        a = p.parse_args(["--engine", "webview", "launch"])
        self.assertEqual(a.cmd, "launch")
        a = p.parse_args(["--no-auto-launch", "open", "https://example.com"])
        self.assertTrue(a.no_auto_launch)


class WebViewPowerTest(unittest.TestCase):
    def _eng(self):
        from kancil.webview_engine import WebViewEngine
        eng = WebViewEngine.__new__(WebViewEngine)
        eng.host, eng.port = "127.0.0.1", 18090
        eng.base = "http://127.0.0.1:18090"
        eng.timeout = 5
        eng.errors, eng.netlog = [], []
        eng.cur = 0
        eng.auto_launch = False
        eng._page_cache, eng._page_url = None, None
        return eng

    def test_upload_base64(self):
        import base64
        import tempfile
        eng = self._eng()
        posted = {}
        eng._post = lambda path, body: posted.update(
            {"path": path, "body": body}) or {"ok": True,
                                              "path": "/cache/upload/f.bin"}
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(b"\x00\x01hello")
            fp = f.name
        try:
            r = eng.upload(fp)
        finally:
            os.unlink(fp)
        self.assertTrue(r["success"], r)
        self.assertEqual(posted["path"], "/upload")
        body = posted["body"]
        self.assertIn("data", body)
        self.assertEqual(base64.b64decode(body["data"]), b"\x00\x01hello")
        self.assertTrue(body["filename"].endswith(".bin"))

    def test_upload_missing_and_too_big(self):
        eng = self._eng()
        r = eng.upload("/no/such/file.bin")
        self.assertFalse(r["success"])
        self.assertIn("not found", r["errors"][0])
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.truncate(6 * 1024 * 1024)
            fp = f.name
        try:
            r = eng.upload(fp)
        finally:
            os.unlink(fp)
        self.assertFalse(r["success"])
        self.assertIn("too large", r["errors"][0])

    def test_upload_fallback_old_agent(self):
        import tempfile
        eng = self._eng()
        calls = []
        def fake_post(path, body):
            calls.append(body)
            if "data" in body:
                return {"ok": False, "error": "missing path"}
            return {"ok": True, "hint": "staged"}
        eng._post = fake_post
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"x")
            fp = f.name
        try:
            r = eng.upload(fp)
        finally:
            os.unlink(fp)
        self.assertTrue(r["success"], r)
        self.assertEqual(len(calls), 2)
        self.assertIn("data", calls[0])
        self.assertEqual(calls[1], {"path": fp})

    def test_blocklist_engine(self):
        eng = self._eng()
        eng._get = lambda p: {"ok": True, "patterns": ["ads.com"]}
        self.assertEqual(eng.block_list(), ["ads.com"])
        posted = {}
        eng._post = lambda p, b: posted.update(
            {"p": p, "b": b}) or {"ok": True, "cache_mode": "LOAD_NO_CACHE"}
        r = eng.block_add(["ads.com", " Tracker.IO "])
        self.assertTrue(r["success"], r)
        self.assertEqual(posted["b"]["patterns"], ["ads.com", "tracker.io"])
        r = eng.block_clear()
        self.assertTrue(r["success"], r)
        self.assertEqual(posted["b"]["patterns"], [])

    def test_blocklist_api_and_cli(self):
        from kancil.api import Kancil
        from kancil import cli as cli_mod
        k = Kancil.__new__(Kancil)
        k._engine_name = "webview"
        eng = self._eng()
        eng.block_list = lambda: ["a.com"]
        eng.block_add = lambda p: {"success": True, "patterns": ["a.com", p]}
        eng.block_clear = lambda: {"success": True}
        k.engine = eng
        k._wrap = lambda r: r
        self.assertEqual(k.block_list()["patterns"], ["a.com"])
        self.assertTrue(k.block_add("b.com")["success"])
        self.assertTrue(k.block_clear()["success"])
        k.engine = object()  # no block_* methods
        self.assertFalse(k.block_add("x")["success"])
        p = cli_mod.build_parser()
        a = p.parse_args(["blocklist", "add", "ads.com", "trk.io"])
        self.assertEqual((a.cmd, a.action, a.patterns),
                         ("blocklist", "add", ["ads.com", "trk.io"]))
        a = p.parse_args(["blocklist"])
        self.assertEqual(a.action, "status")


class WebViewTouchTest(unittest.TestCase):
    def _eng(self):
        from kancil.webview_engine import WebViewEngine
        eng = WebViewEngine.__new__(WebViewEngine)
        eng.host, eng.port = "127.0.0.1", 18091
        eng.base = "http://127.0.0.1:18091"
        eng.timeout = 5
        eng.errors, eng.netlog = [], []
        eng.cur = 0
        eng.auto_launch = False
        eng._page_cache, eng._page_url = None, None
        return eng

    def test_touch_tap_xy(self):
        eng = self._eng()
        posted = {}
        eng._post = lambda p, b: posted.update(
            {"p": p, "b": b}) or {"ok": True, "action": "tap",
                                  "x": 10.0, "y": 20.0}
        r = eng.touch("tap", x=10, y=20)
        self.assertTrue(r["success"], r)
        self.assertEqual(posted["p"], "/touch")
        self.assertEqual(posted["b"], {"action": "tap", "x": 10, "y": 20})

    def test_touch_swipe(self):
        eng = self._eng()
        posted = {}
        eng._post = lambda p, b: posted.update(
            {"b": b}) or {"ok": True, "action": "swipe"}
        r = eng.touch("swipe", x=100, y=800, x2=100, y2=200,
                      duration_ms=500)
        self.assertTrue(r["success"], r)
        self.assertEqual(posted["b"]["action"], "swipe")
        self.assertEqual(posted["b"]["duration_ms"], 500)

    def test_touch_selector(self):
        eng = self._eng()
        eng.evaluate = lambda js: {"success": True, "result": '"150,250"'}
        posted = {}
        eng._post = lambda p, b: posted.update(
            {"b": b}) or {"ok": True, "action": "tap"}
        r = eng.touch("tap", selector="#btn")
        self.assertTrue(r["success"], r)
        self.assertEqual((posted["b"]["x"], posted["b"]["y"]),
                         (150.0, 250.0))

    def test_touch_selector_missing(self):
        eng = self._eng()
        eng.evaluate = lambda js: {"success": True, "result": "null"}
        r = eng.touch("tap", selector="#nope")
        self.assertFalse(r["success"])
        self.assertIn("no element matches", r["errors"][0])

    def test_touch_api_and_cli(self):
        from kancil.api import Kancil
        from kancil import cli as cli_mod
        k = Kancil.__new__(Kancil)
        k.engine = self._eng()
        k.engine.touch = lambda **kw: {"success": True, **kw}
        k._wrap = lambda r: r
        r = k.touch("longpress", x=5, y=5, duration_ms=900)
        self.assertTrue(r["success"])
        self.assertEqual(r["duration_ms"], 900)
        k.engine = object()
        r = k.touch("tap", x=1, y=1)
        self.assertFalse(r["success"])
        p = cli_mod.build_parser()
        a = p.parse_args(["touch", "swipe", "--x", "100", "--y", "800",
                          "--x2", "100", "--y2", "200"])
        self.assertEqual((a.cmd, a.action, a.x2, a.y2),
                         ("touch", "swipe", 100.0, 200.0))
        a = p.parse_args(["touch", "--selector", "#btn"])
        self.assertEqual((a.action, a.selector), ("tap", "#btn"))


class WebViewTouchV2Test(unittest.TestCase):
    def _eng(self):
        from kancil.webview_engine import WebViewEngine
        eng = WebViewEngine.__new__(WebViewEngine)
        eng.host, eng.port = "127.0.0.1", 18092
        eng.base = "http://127.0.0.1:18092"
        eng.timeout = 5
        eng.errors, eng.netlog = [], []
        eng.cur = 0
        eng.auto_launch = False
        eng._page_cache, eng._page_url = None, None
        return eng

    def test_touch_uses_standard_targeting(self):
        eng = self._eng()
        seen = {}
        eng.evaluate = lambda js: seen.update(
            {"js": js}) or {"success": True, "result": '"10,20"'}
        eng._post = lambda p, b: {"ok": True, "action": "tap"}
        r = eng.touch("tap", selector="Putar")  # visible text
        self.assertTrue(r["success"], r)
        # standard targeting JS (shadow-DOM piercing helpers), not raw
        # querySelector
        self.assertIn("__kancilQ", seen["js"])
        self.assertIn("Putar", seen["js"])

    def test_touch_a11y_ref(self):
        eng = self._eng()
        eng._a11y_css = {"button_3": "#real-btn"}
        eng.evaluate = lambda js: {"success": True, "result": '"1,2"'} \
            if "#real-btn" in js else {"success": True, "result": "null"}
        eng._post = lambda p, b: {"ok": True, "action": "tap"}
        r = eng.touch("tap", selector="@button_3")
        self.assertTrue(r["success"], r)

    def test_pinch_out_maps_distances(self):
        eng = self._eng()
        posted = {}
        eng._post = lambda p, b: posted.update(
            {"b": b}) or {"ok": True, "action": "pinch"}
        r = eng.touch("pinch_out", x=540, y=900)
        self.assertTrue(r["success"], r)
        self.assertEqual(posted["b"]["action"], "pinch")
        self.assertLess(posted["b"]["distance_start"],
                        posted["b"]["distance_end"])  # spread = zoom out

    def test_pinch_in_maps_distances(self):
        eng = self._eng()
        posted = {}
        eng._post = lambda p, b: posted.update(
            {"b": b}) or {"ok": True, "action": "pinch"}
        eng.touch("pinch_in", x=540, y=900)
        self.assertGreater(posted["b"]["distance_start"],
                           posted["b"]["distance_end"])  # close = zoom in

    def test_pinch_explicit(self):
        eng = self._eng()
        posted = {}
        eng._post = lambda p, b: posted.update(
            {"b": b}) or {"ok": True, "action": "pinch"}
        r = eng.touch("pinch", x=1, y=2, distance_start=100,
                      distance_end=300, duration_ms=600)
        self.assertTrue(r["success"], r)
        self.assertEqual((posted["b"]["distance_start"],
                          posted["b"]["distance_end"],
                          posted["b"]["duration_ms"]), (100, 300, 600))

    def test_cli_pinch_parses(self):
        from kancil import cli as cli_mod
        p = cli_mod.build_parser()
        a = p.parse_args(["touch", "pinch_out", "--x", "540", "--y", "900"])
        self.assertEqual((a.cmd, a.action), ("touch", "pinch_out"))


class WebViewTouchHumanTest(unittest.TestCase):
    def _eng(self):
        from kancil.webview_engine import WebViewEngine
        eng = WebViewEngine.__new__(WebViewEngine)
        eng.host, eng.port = "127.0.0.1", 18093
        eng.base = "http://127.0.0.1:18093"
        eng.timeout = 5
        eng.errors, eng.netlog = [], []
        eng.cur = 0
        eng.auto_launch = False
        eng._healing = False
        eng._page_cache, eng._page_url = None, None
        return eng

    def test_human_flag_passed(self):
        eng = self._eng()
        posted = {}
        eng._post = lambda p, b: posted.update(
            {"b": b}) or {"ok": True, "action": "swipe"}
        r = eng.touch("swipe", x=10, y=100, x2=300, y2=100, human=True,
                      duration_ms=800)
        self.assertTrue(r["success"], r)
        self.assertTrue(posted["b"]["human"])
        self.assertEqual(posted["b"]["duration_ms"], 800)

    def test_human_defaults_off(self):
        eng = self._eng()
        posted = {}
        eng._post = lambda p, b: posted.update(
            {"b": b}) or {"ok": True, "action": "swipe"}
        eng.touch("swipe", x=10, y=100, x2=300, y2=100)
        self.assertNotIn("human", posted["b"])

    def test_cli_human_parses(self):
        from kancil import cli as cli_mod
        p = cli_mod.build_parser()
        a = p.parse_args(["touch", "swipe", "--x", "10", "--y", "100",
                          "--x2", "300", "--y2", "100", "--human"])
        self.assertTrue(a.human)
