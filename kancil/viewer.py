"""kancil view -- local viewer: see the page in YOUR browser, take over navigation.

Static engine (zero deps): serves the current page's HTML through a local
gateway. Your phone/desktop browser renders it for real; clicks and the
address bar navigate *through Kancil* (cookies/session preserved).

Playwright engine: serves live screenshots; clicks on the image are
forwarded as real mouse clicks into Chromium.

Binds to 127.0.0.1 by default. Do NOT expose to a network without auth.
"""

import re
import threading
import time
import urllib.parse as up
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GW = "/__kancil__"


# ---------------- HTML rewriting (static engine) ----------------

def _abs(base, url):
    try:
        return up.urljoin(base, url)
    except Exception:
        return url


def rewrite_html(html, page_url, gw=GW):
    """gw: absolute gateway base, e.g. http://127.0.0.1:8901/__kancil__.
    Must be absolute: pages carry a <base> tag pointing at the origin."""
    """Inject <base>, toolbar, and route links/forms through the gateway."""
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")

    def _go(m):
        # quoted or unquoted href value
        url = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        low = url.lower()
        if (not url or low.startswith(("javascript:", "mailto:", "tel:", "#"))
                or low.startswith("data:")):
            return m.group(0)
        absu = _abs(page_url, url)
        if not absu.startswith(("http://", "https://")):
            return m.group(0)
        return 'href="%s/go?u=%s"' % (gw, up.quote(absu, safe=""))

    # route <a href> through the gateway (quoted or unquoted values)
    html = re.sub(r'href=(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', _go,
                  html, flags=re.I)

    # GET forms -> gateway (fields are appended as query by the browser)
    def _form(m):
        tag = m.group(0)
        mu = re.search(r'action=(["\'])(.*?)\1', tag, flags=re.I)
        mm = re.search(r'method=(["\'])(.*?)\1', tag, flags=re.I)
        method = (mm.group(2) if mm else "get").lower()
        if method != "get":
            return tag  # POST forms left to origin (v1 limitation)
        absu = _abs(page_url, mu.group(2)) if mu else page_url
        tag2 = re.sub(r'action=(["\']).*?\1',
                      'action="%s/go?u=%s"' % (gw, up.quote(absu, safe="")),
                      tag, flags=re.I)
        if not mu:
            tag2 = tag2.replace("<form", '<form action="%s/go?u=%s"'
                                % (gw, up.quote(absu, safe="")), 1)
        return tag2
    html = re.sub(r'<form\b[^>]*>', _form, html, flags=re.I)

    # <base> so images/css/js load straight from the origin
    base_tag = '<base href="%s">' % page_url.replace('"', "%22")
    if re.search(r"<head\b[^>]*>", html, flags=re.I):
        html = re.sub(r"(<head\b[^>]*>)", r"\1" + base_tag, html,
                      count=1, flags=re.I)
    else:
        html = base_tag + html

    # toolbar
    bar = (
        '<div id="__kancil_bar" style="position:fixed;top:0;left:0;right:0;'
        'z-index:2147483647;background:#141414;color:#eee;'
        'font:12px/1.4 system-ui,sans-serif;padding:6px 10px;'
        'display:flex;gap:8px;align-items:center;'
        'border-bottom:1px solid #333">'
        '<a href="%s/back" style="color:#8cf;text-decoration:none">\u25c0</a>'
        '<a href="%s/forward" style="color:#8cf;text-decoration:none">\u25b6</a>'
        '<a href="%s/reload" style="color:#8cf;text-decoration:none">\u27f3</a>'
        '<form action="%s/go" method="get" style="flex:1;display:flex;margin:0">'
        '<input name="u" value="%s" style="flex:1;background:#222;color:#eee;'
        'border:1px solid #444;padding:3px 8px;font-size:12px"></form>'
        '<span style="color:#888">kancil view</span></div>'
        '<div style="height:36px"></div>' % (
            gw, gw, gw, gw, page_url.replace('"', "&quot;")))
    if re.search(r"<body\b[^>]*>", html, flags=re.I):
        html = re.sub(r"(<body\b[^>]*>)", r"\1" + bar, html,
                      count=1, flags=re.I)
    else:
        html = bar + html

    # agent.js: lets Kancil drive the REAL rendered page (level 1)
    agent_tag = ('<script src="%s/agent.js" data-kancil-url="%s"></script>'
                 % (gw, page_url.replace('"', '&quot;')))
    if "</body>" in html.lower():
        html = re.sub(r"</body\s*>", agent_tag + "</body>", html,
                      count=1, flags=re.I)
    else:
        html += agent_tag
    return html


# ---------------- viewer shells ----------------

def _shell_static():
    return """<!doctype html><html><head><meta charset="utf-8">
<title>kancil view</title><style>html,body{margin:0;height:100%%}
iframe{border:0;width:100%%;height:100vh}</style></head>
<body><iframe src="/page" title="kancil page"></iframe></body></html>"""


def _shell_pw():
    return """<!doctype html><html><head><meta charset="utf-8">
<title>kancil view (playwright)</title>
<style>html,body{margin:0;background:#111;color:#eee;
font:13px system-ui,sans-serif}
#bar{padding:8px 12px;display:flex;gap:10px;align-items:center;
background:#1a1a1a;border-bottom:1px solid #333;position:sticky;top:0;z-index:9}
#bar a{color:#8cf;text-decoration:none}
#wrap{text-align:center;padding:10px}
#shot{max-width:100%%;border:1px solid #444;cursor:crosshair}
#msg{color:#888}</style></head><body>
<div id="bar">
<a href="/__kancil__/back">&#9660;</a><a href="/__kancil__/forward">&#9654;</a>
<a href="/__kancil__/reload">&#10227;</a>
<input id="t" placeholder="type text, Enter to send" style="flex:1;max-width:300px;
background:#222;color:#eee;border:1px solid #444;padding:4px 8px">
<button id="refresh">refresh</button>
<span id="msg">click the image = click in Chromium</span>
<span style="margin-left:auto;color:#888">kancil view</span></div>
<div id="wrap"><img id="shot" src="/shot.png"></div>
<script>
const shot=document.getElementById('shot'),msg=document.getElementById('msg');
let vw={width:1280,height:800};
fetch('/__kancil__/viewport').then(r=>r.json()).then(v=>{vw=v}).catch(()=>{});
function refresh(){shot.src='/shot.png?t='+Date.now()}
document.getElementById('refresh').onclick=refresh;
setInterval(refresh,3000);
shot.onclick=e=>{
  const r=shot.getBoundingClientRect();
  const x=Math.round((e.clientX-r.left)/r.width*vw.width);
  const y=Math.round((e.clientY-r.top)/r.height*vw.height);
  msg.textContent='click '+x+','+y+' ...';
  fetch('/__kancil__/click?x='+x+'&y='+y).then(()=>{refresh();
    msg.textContent='clicked '+x+','+y;}).catch(err=>{msg.textContent=err;});
};
document.getElementById('t').addEventListener('keydown',e=>{
  if(e.key==='Enter'){const v=e.target.value;e.target.value='';
    fetch('/__kancil__/type?text='+encodeURIComponent(v)).then(refresh);}
});
</script></body></html>"""


def _extract_video_id(target):
    """Video ID from a watch URL, youtu.be, /embed/, /shorts/, or bare ID."""
    t = (target or "").strip()
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", t)
    if m:
        return m.group(1)
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", t)
    if m:
        return m.group(1)
    m = re.search(r"/(?:embed|shorts|live)/([A-Za-z0-9_-]{11})", t)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", t):
        return t
    return None


def player_page(vid, title="", channel="", thumb=""):
    """Mini player page: official YouTube embed (audio+video, fullscreen)."""
    import html as _html
    title = _html.escape(title or vid)
    channel = _html.escape(channel or "")
    thumb_tag = ('<meta property="og:image" content="%s">' % _html.escape(thumb)
                 if thumb else "")
    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s — kancil play</title>%s
<style>body{margin:0;background:#0f0f0f;color:#eee;
font:14px system-ui,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:12px}
.player{position:relative;padding-bottom:56.25%%;height:0;
background:#000;border-radius:8px;overflow:hidden}
.player iframe{position:absolute;top:0;left:0;width:100%%;height:100%%;
border:0}
h1{font-size:17px;margin:12px 0 4px}
.ch{color:#aaa;margin:0 0 12px}
.row{display:flex;gap:10px;flex-wrap:wrap}
.btn{background:#272727;color:#fff;text-decoration:none;
padding:8px 14px;border-radius:18px}
.note{color:#777;font-size:12px;margin-top:14px}</style></head>
<body><div class="wrap">
<div class="player"><iframe
src="https://www.youtube.com/embed/%s?autoplay=1&rel=0"
allow="autoplay; encrypted-media; picture-in-picture; fullscreen"
allowfullscreen title="YouTube player"></iframe></div>
<h1>%s</h1><p class="ch">%s</p>
<div class="row">
<a class="btn" href="https://www.youtube.com/watch?v=%s">Buka di YouTube</a>
<a class="btn" href="/">kancil view</a>
</div>
<p class="note">Player resmi YouTube — kalau pemilik video mematikan
embed, buka lewat tombol di atas.</p>
</div></body></html>""" % (title, thumb_tag, vid, title, channel, vid)


# ---------------- server ----------------

class _Handler(BaseHTTPRequestHandler):
    server_version = "kancil-view/1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        import json
        self._send(code, json.dumps(obj), "application/json")

    def _read_json(self):
        import json
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except Exception:
            n = 0
        if n <= 0 or n > 2_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _redir(self, to="/"):
        self.send_response(302)
        self.send_header("Location", to)
        self.end_headers()

    @property
    def k(self):
        return self.server.kancil

    def do_GET(self):
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._json({"success": False, "errors": [str(e)[:200]]}, 500)

    def do_POST(self):
        self.do_GET()  # _route handles POST bodies itself

    def _page_or_none(self):
        try:
            return self.k.engine.page
        except Exception:
            return None

    def _route(self):
        u = up.urlsplit(self.path)
        path, q = u.path, up.parse_qs(u.query)
        eng = self.k.engine

        if path == "/":
            shell = _shell_pw() if eng.name == "playwright" \
                else _shell_static()
            return self._send(200, shell)

        if path == "/page":
            # static engine: the live page HTML through the gateway
            p = self._page_or_none()
            if p is None:
                return self._send(200, "<h1>no page open</h1>"
                                      "<p>open a URL first: "
                                      "<code>kancil open https://…</code></p>")
            raw = getattr(p, "raw", b"") or b""
            url = getattr(p, "url", "")
            host, port = self.server.server_address[:2]
            gw = "http://%s:%d%s" % (host, port, GW)
            return self._send(200, rewrite_html(raw, url, gw))

        if not path.startswith(GW + "/"):
            return self._send(404, "not found", "text/plain")
        action = path[len(GW) + 1:]

        # ---- level 1: injected JS agent (shared routing) ----
        if action.startswith("agent"):
            from .agent_bridge import route_agent
            import urllib.parse as _up
            routed = route_agent(path, {k: v[0] for k, v in
                                        _up.parse_qs(
                                            _up.urlsplit(self.path).query
                                        ).items()},
                                 self._read_json() if self.command == "POST"
                                 else {})
            if routed is not None:
                code, ctype, body = routed
                return self._send(code, body, ctype)

        if action == "go":
            target = q.get("u", [""])[0]
            # merge extra form fields into the target URL (GET forms)
            rest = {k: v for k, v in q.items() if k != "u"}
            if target and rest:
                extra = up.urlencode({k: v[0] for k, v in rest.items()})
                target += ("&" if "?" in target else "?") + extra
            if not target:
                return self._redir("/")
            r = self.k.open(target)
            if not r.get("success"):
                return self._send(200,
                                  "<h1>open failed</h1><pre>%s</pre>"
                                  % str(r.get("errors"))[:500])
            return self._redir("/")

        if action in ("back", "forward", "reload"):
            getattr(self.k, action)()
            return self._redir("/")

        if action == "play":
            # mini YouTube player: /__kancil__/play?v=VIDEOID
            vid = (q.get("v", [""])[0] or "").strip()
            if not vid:
                return self._send(400, "missing ?v=VIDEOID", "text/plain")
            title, thumb = "", ""
            try:
                m = self.k.yt_video("https://www.youtube.com/watch?v=" + vid)
                if m.get("success"):
                    title = m.get("title", "") or ""
                    thumb = m.get("image", "") or ""
            except Exception:
                pass
            return self._send(200, player_page(vid, title, "", thumb))

        # ---- playwright-only live control ----
        if eng.name != "playwright":
            return self._json({"success": False,
                               "errors": ["needs playwright engine"]}, 400)

        if action == "shot.png":
            shot = _pw_shot(eng)
            if shot is None:
                return self._send(502, "screenshot failed", "text/plain")
            return self._send(200, shot, "image/png",
                              {"Cache-Control": "no-store"})

        if action == "viewport":
            try:
                vp = eng._pp().viewport_size or {}
                return self._json({"width": vp.get("width", 1280),
                                   "height": vp.get("height", 800)})
            except Exception:
                return self._json({"width": 1280, "height": 800})

        if action == "click":
            try:
                x, y = int(q.get("x", [0])[0]), int(q.get("y", [0])[0])
                eng._pp().mouse.click(x, y)
                eng._invalidate()
                return self._json({"success": True, "x": x, "y": y})
            except Exception as e:
                return self._json({"success": False,
                                   "errors": [str(e)[:200]]}, 500)

        if action == "type":
            try:
                eng._pp().keyboard.type(q.get("text", [""])[0])
                eng._invalidate()
                return self._json({"success": True})
            except Exception as e:
                return self._json({"success": False,
                                   "errors": [str(e)[:200]]}, 500)

        if action == "key":
            try:
                eng._pp().keyboard.press(q.get("key", ["Enter"])[0])
                eng._invalidate()
                return self._json({"success": True})
            except Exception as e:
                return self._json({"success": False,
                                   "errors": [str(e)[:200]]}, 500)

        return self._send(404, "unknown action", "text/plain")


def _pw_shot(eng):
    try:
        return eng._pp().screenshot()
    except Exception:
        return None


class ViewerServer:
    """Local HTTP viewer for a Kancil instance."""

    def __init__(self, kancil, host="127.0.0.1", port=0):
        self.kancil = kancil
        self.host = host
        self.port = port
        self._httpd = None
        self._thread = None

    def start(self):
        self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        self._httpd.kancil = self.kancil
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="kancil-view", daemon=True)
        self._thread.start()
        # tiny wait so first request doesn't race bind
        time.sleep(0.05)
        return "http://%s:%d/" % (self.host, self.port)

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def running(self):
        return self._httpd is not None
