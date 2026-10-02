"""Live tests against PUBLIC URLs (need network + Chromium).

Run: python3 -m unittest tests.test_live -v
These prove the agent bridge drives a real rendered page.
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from kancil import Kancil

CHROME = os.path.expanduser(
    "~/.cache/ms-playwright/chromium-1243/chrome-linux64/chrome")
CHROMIUM_ARGS = ["--no-sandbox", "--disable-quic", "--disable-gpu"]


def need_chromium():
    if not os.path.exists(CHROME):
        raise unittest.SkipTest("chromium not installed")


class LiveAgentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        need_chromium()
        from playwright.sync_api import sync_playwright
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch(
            headless=True, executable_path=CHROME, args=CHROMIUM_ARGS)
        cls.k = Kancil(engine="static", timeout=30, retries=1)
        r = cls.k.open("https://example.com")
        assert r["success"], r
        v = cls.k.view(port=0)
        assert v["success"], v
        cls.view_url = v["url"]
        cls.page = cls.browser.new_page()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.k.view_stop()
        except Exception:
            pass
        try:
            cls.browser.close()
        except Exception:
            pass
        try:
            cls._pw.stop()
        except Exception:
            pass
        try:
            cls.k.close()
        except Exception:
            pass

    def _wait_tab(self, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            tabs = self.k.agent_tabs()["tabs"]
            live = [t for t in tabs if t["live"]]
            if live:
                return live[0]["tab_id"]
            time.sleep(1)
        self.fail("agent.js did not register within %ds" % timeout)

    def test_01_agent_drives_real_page(self):
        # real Chromium renders the viewer gateway page; agent.js registers
        self.page.goto(self.view_url + "page", timeout=30000)
        tab = self._wait_tab()

        snap = self.k.agent_snapshot(tab, timeout=30)
        self.assertTrue(snap["success"], snap)
        res = snap["result"]
        self.assertEqual(res["title"], "Example Domain")
        self.assertIn("example.com", res["url"])
        self.assertGreater(res["count"], 0)
        links = [e for e in res["elements"] if e["tag"] == "a"]
        self.assertTrue(links, "no links in snapshot")

        # click "More information..." -> navigates THROUGH the gateway
        # (href was rewritten to /__kancil__/go)
        target = [e for e in links if "more" in e["text"].lower()]
        self.assertTrue(target, "more-information link not found")
        r = self.k.agent_cmd(tab, "click",
                             {"selector": target[0]["sel"]}, timeout=30)
        self.assertTrue(r["success"], r)
        self.assertTrue(r["result"].get("clicked"))

        # the click navigated through the gateway; the new page load
        # registers a NEW tab id -> wait for a live tab on iana.org
        end = time.time() + 30
        new_tab = None
        while time.time() < end:
            for t in self.k.agent_tabs()["tabs"]:
                if t["live"] and "iana.org" in t.get("url", ""):
                    new_tab = t["tab_id"]
                    break
            if new_tab:
                break
            time.sleep(1)
        self.assertTrue(new_tab, "click did not navigate to iana.org")
        s2 = self.k.agent_snapshot(new_tab, timeout=30)
        self.assertTrue(s2["success"], s2)
        self.assertIn("iana.org", s2["result"].get("url", ""))
        self.assertTrue(s2["result"].get("title"))

    def test_02_agent_eval_sees_live_dom(self):
        tabs = [t for t in self.k.agent_tabs()["tabs"] if t["live"]]
        self.assertTrue(tabs)
        tab = tabs[-1]["tab_id"]  # newest registration (older ones are stale)
        r = self.k.agent_cmd(tab, "eval",
                             {"js": "document.querySelectorAll('p').length"},
                             timeout=30)
        self.assertTrue(r["success"], r)
        self.assertGreater(int(r["result"]["result"]), 0)


if __name__ == "__main__":
    unittest.main()


class LiveProxyTest(unittest.TestCase):
    """Level 2: proxy HTTP + MITM HTTPS against public URLs."""

    def _fetch_via_proxy(self, url, proxy_url, cafile=None):
        import urllib.request
        import ssl as _ssl
        handlers = [urllib.request.ProxyHandler(
            {"http": proxy_url, "https": proxy_url})]
        if cafile:
            ctx = _ssl.create_default_context(cafile=cafile)
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        op = urllib.request.build_opener(*handlers)
        return op.open(url, timeout=30)

    def test_10_http_proxy_injects_agent(self):
        from kancil.proxy_server import ProxyServer
        srv = ProxyServer(port=0)
        proxy_url = srv.start()
        try:
            r = self._fetch_via_proxy("http://example.com/", proxy_url)
            body = r.read().decode("utf-8", "replace")
            self.assertEqual(r.status, 200)
            self.assertIn("Example Domain", body)
            self.assertIn("__kancil__/agent.js", body,
                          "agent.js not injected by proxy")
            self.assertTrue(
                any(e.get("url", "").startswith("http://example.com")
                    for e in srv.log), "proxy log empty: %s" % srv.log[:2])
        finally:
            srv.stop()

    def test_11_mitm_proxy_injects_agent(self):
        from kancil.proxy_server import (ProxyServer, ensure_ca, ca_paths,
                                         openssl_ok)
        if not openssl_ok():
            self.skipTest("openssl CLI not available")
        crt, _key = ensure_ca()
        self.assertTrue(os.path.exists(crt))
        srv = ProxyServer(port=0, mitm=True)
        self.assertTrue(srv.mitm, "MITM did not enable")
        proxy_url = srv.start()
        try:
            r = self._fetch_via_proxy("https://example.com/", proxy_url,
                                      cafile=crt)
            body = r.read().decode("utf-8", "replace")
            self.assertEqual(r.status, 200)
            self.assertIn("__kancil__/agent.js", body,
                          "agent.js not injected through MITM")
            self.assertTrue(
                any(e.get("mitm") and "example.com" in e.get("url", "")
                    for e in srv.log), "mitm log entry missing")
        finally:
            srv.stop()

    def test_12_blind_connect_tunnel(self):
        import socket
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        from kancil.proxy_server import ProxyServer

        # local origin (raw TCP to 127.0.0.1 is allowed here)
        class _H(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"tunnel-ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        origin = HTTPServer(("127.0.0.1", 0), _H)
        o_port = origin.server_address[1]
        threading.Thread(target=origin.serve_forever, daemon=True).start()

        srv = ProxyServer(port=0)  # no mitm -> blind tunnel
        proxy_url = srv.start()
        p_host, p_port = proxy_url.replace("http://", "").rstrip("/").split(":")
        try:
            s = socket.create_connection((p_host, int(p_port)), timeout=20)
            s.sendall(("CONNECT 127.0.0.1:%d HTTP/1.1\r\n"
                       "Host: 127.0.0.1:%d\r\n\r\n" % (o_port, o_port)
                       ).encode())
            resp = s.recv(4096).decode("latin-1")
            self.assertIn("200", resp.split("\r\n")[0])
            s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n"
                      b"Connection: close\r\n\r\n")
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
            s.close()
            self.assertIn(b"tunnel-ok", data)
            self.assertTrue(any(e.get("tunneled") for e in srv.log),
                            "tunnel not logged")
        finally:
            srv.stop()
            origin.shutdown()
