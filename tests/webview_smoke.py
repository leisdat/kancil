#!/usr/bin/env python3
"""Live smoke test for the Kancil Browser APK + webview engine.

Run ON THE PHONE (Termux) with the Kancil Browser app OPEN:
    python3 tests/webview_smoke.py

Spins up a local fixture page (no internet needed) and drives it through
the real WebView: type/click/checkbox/select/fill, console capture,
JS dialogs, popups, videos, blocklist, screenshots. Verifies state via JS.

Exit 0 = all green, 1 = failures (listed at the end).
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, ".")
from kancil import webview_engine as wv  # noqa: E402

FIXTURE = """<!DOCTYPE html><html><head><title>smoke</title></head><body>
<h1>smoke fixture</h1>
<script>console.log("smoke-loaded");</script>
<form id="f1" action="#" onsubmit="return false">
  <input type="text" name="uname" id="uname" value="">
  <input type="checkbox" name="agree" id="agree">
  <select name="color" id="color">
    <option value="r">red</option><option value="g">green</option>
  </select>
  <input type="file" name="avatar" id="avatar">
  <button type="submit" id="go">Go</button>
</form>
<button id="alertbtn" onclick="alert('hi-alert')">alert</button>
<a id="pop" href="/popup" target="_blank">popup</a>
<video id="v1" src="/v.mp4" width="320" height="240"></video>
<div id="tall" style="height:3000px">tall</div>
</body></html>"""

POPUP = "<html><body><h1>popup ok</h1></body></html>"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split("?")[0]
        body = POPUP if p == "/popup" else FIXTURE
        if p == "/v.mp4":
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name
          + (" — " + str(detail)[:100] if detail and not cond else ""))


def main():
    srv = HTTPServer(("127.0.0.1", 8902), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    eng = wv.WebViewEngine(port=8080)
    try:
        eng._get("/status")
    except Exception as e:
        print("App not reachable at 127.0.0.1:8080 — open Kancil Browser "
              "first. (%s)" % str(e)[:80])
        return 1
    base = "http://127.0.0.1:8902/"

    r = eng.open(base)
    check("open fixture", r.get("success"), r)
    time.sleep(1)

    # 1. type() with native setter
    r = eng.type("#uname", "hello-react")
    v = eng.evaluate("document.querySelector('#uname').value")
    check("type native setter", v.get("result") == "hello-react", v)

    # 2. checkbox via click
    r = eng.click("#agree")
    v = eng.evaluate("document.querySelector('#agree').checked")
    check("checkbox click", v.get("result") == "true", v)

    # 3. select via fill_form
    r = eng.fill_form(0, {"color": "g"})
    v = eng.evaluate("document.querySelector('#color').value")
    check("fill_form select", r.get("success") and v.get("result") == "g",
          (r, v))

    # 4. console capture (page logged on load)
    r = eng.console()
    texts = [m.get("text", "") for m in r.get("logs", [])]
    check("console capture", any("smoke-loaded" in t for t in texts),
          texts[:3])

    # 5. JS alert auto-dismiss (must not hang)
    t0 = time.time()
    r = eng.click("#alertbtn")
    dt = time.time() - t0
    r2 = eng.console()
    texts = [m.get("text", "") for m in r2.get("logs", [])]
    check("js alert dismissed", dt < 10 and
          any("js alert" in t for t in texts), "%.1fs" % dt)

    # 6. popup -> new tab
    before = eng.list_tabs()
    eng.click("#pop")
    time.sleep(1.5)
    after = eng.list_tabs()
    check("popup opens tab", len(after) > len(before),
          "%d -> %d tabs" % (len(before), len(after)))

    # 7. videos
    if before:
        eng.switch_tab(before[0]["id"])
    eng.open(base)
    time.sleep(1)
    r = eng.videos()
    check("videos listed", r.get("success") and len(r.get("videos", [])) == 1
          and r["videos"][0]["src"].endswith("/v.mp4"), r)

    # 8. blocklist
    eng.block_add("v.mp4")
    eng.open(base)
    time.sleep(1)
    net = eng.network(pattern="v.mp4")
    blocked = [e for e in net if "blocked" in str(e.get("error", ""))]
    check("blocklist blocks", len(blocked) > 0, net[-1] if net else "none")
    eng.block_clear()

    # 9. element screenshot
    import tempfile, os
    p = os.path.join(tempfile.gettempdir(), "smoke-el.png")
    r = eng.screenshot(path=p, selector="#uname")
    ok = r.get("success") and os.path.getsize(p) > 100
    check("element screenshot", ok, r)

    # 10. full screenshot
    p2 = os.path.join(tempfile.gettempdir(), "smoke-full.png")
    r = eng.screenshot(path=p2, full=True)
    ok = r.get("success") and os.path.getsize(p2) > 1000
    check("full screenshot", ok, r)

    srv.shutdown()
    fails = [n for n, c, d in results if not c]
    print("\n%d/%d passed" % (len(results) - len(fails), len(results)))
    if fails:
        print("FAILED:", ", ".join(fails))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
