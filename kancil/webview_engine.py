"""WebViewEngine — drive the Kancil Browser APK on the same phone.

The APK runs a tiny agent HTTP server on 127.0.0.1:8080 backed by a real
System WebView (Chromium): full JS, video, logins, captchas. The user browses
by hand once (login/captcha), the agent reuses that live session.

Same interface as StaticEngine/PlaywrightEngine; unsupported ops raise
EngineError instead of pretending to work.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from . import engines
from .engines import EngineError
from . import session as session_mod


class WebViewEngine:
    name = "webview"
    capabilities = {
        "javascript": True,
        "screenshot": True,
        "real_localstorage": True,
        "indexeddb": True,
        "network_interception": False,
        "computed_style": True,
        "bounding_box": False,
        "console_capture": False,
        "video": True,
        "cookies": True,
        "forms": True,
        "downloads": False,
        "xpath": True,
        "css_selectors": True,
    }

    def __init__(self, host="127.0.0.1", port=8080, timeout=25, **kwargs):
        self.host = host or "127.0.0.1"
        self.port = int(port or 8080)
        self.timeout = timeout
        self.base = "http://%s:%d" % (self.host, self.port)
        self.errors = []
        self.netlog = []
        self.cur = 0
        self._hist = {}  # tab_id -> {"history": [], "pos": -1}
        self._a11y_refs = {}
        self._a11y_sig = None
        self._a11y_pending = ({}, None)
        self._a11y_css = {}
        self._page_cache = None
        self._page_url = None
        # fail fast with a clear message when the app isn't running
        try:
            st = self._get("/status")
            if not st.get("ok"):
                raise Exception("bad /status response")
            self.cur = st.get("tab", 0)
        except Exception as e:
            raise EngineError(
                "webview agent unreachable at %s — open the Kancil Browser "
                "app on this phone first (%s)" % (self.base, str(e)[:120]))

    # ---------- transport ----------

    def _req(self, method, path, body=None):
        url = self.base + path
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                ct = r.headers.get("Content-Type", "")
                if "application/json" in ct:
                    return json.loads(raw.decode("utf-8", "replace"))
                return {"ok": True, "raw": raw,
                        "content_type": ct}
        except urllib.error.HTTPError as e:
            # the app answers errors as JSON too: {"ok": false, "error": ...}
            try:
                err = json.loads(e.read().decode("utf-8", "replace"))
                if isinstance(err, dict) and "error" in err:
                    return err
            except Exception:
                pass
            raise EngineError("webview agent %s %s: HTTP %d"
                              % (method, path, e.code))

    def _get(self, path):
        return self._req("GET", path)

    def _post(self, path, body):
        return self._req("POST", path, body)

    def _ok(self, r):
        if isinstance(r, dict) and r.get("ok"):
            return True
        self.errors.append({"type": "agent",
                            "error": str(r)[:150],
                            "t": time.strftime("%H:%M:%S")})
        return False

    # ---------- navigation ----------

    def open(self, url, data=None):
        if not url.startswith(("http://", "https://", "file://")):
            url = "https://" + url
        r = self._post("/navigate", {"url": url})
        if not self._ok(r):
            return {"success": False, "url": url,
                    "errors": [str(r)[:200]]}
        time.sleep(0.4)  # let the page start loading
        self._invalidate()
        st = self._get("/status")
        final = st.get("url", url) if isinstance(st, dict) else url
        tab_id = st.get("tab", self.cur) if isinstance(st, dict) else self.cur
        self.cur = tab_id
        h = self._hist.setdefault(tab_id, {"history": [], "pos": -1})
        h["history"].append(final)
        h["pos"] = len(h["history"]) - 1
        return {"success": True, "url": final,
                "title": st.get("title", "") if isinstance(st, dict) else "",
                "tab": tab_id}

    def back(self):
        self._get("/back")
        self._invalidate()
        return {"success": True, "url": self._cur_url()}

    def forward(self):
        self._get("/forward")
        self._invalidate()
        return {"success": True, "url": self._cur_url()}

    def reload(self):
        self._get("/reload")
        time.sleep(0.4)
        self._invalidate()
        return {"success": True, "url": self._cur_url()}

    def _cur_url(self):
        try:
            st = self._get("/status")
            return st.get("url", "") if isinstance(st, dict) else ""
        except Exception:
            return ""

    def history(self):
        h = self._hist.get(self.cur, {"history": [], "pos": -1})
        return [{"pos": i, "current": i == h["pos"], "url": u, "title": ""}
                for i, u in enumerate(h["history"])]

    # tabs (the app supports multiple WebViews)
    def list_tabs(self):
        r = self._get("/tabs")
        out = []
        for t in r.get("tabs", []):
            out.append({"id": t.get("id"), "url": t.get("url", ""),
                        "title": t.get("title", ""),
                        "current": t.get("active", False)})
        for t in out:
            if t["current"]:
                self.cur = t["id"]
        return out

    def new_tab(self, url=None):
        r = self._post("/tabs/new", {"url": url or "https://www.google.com"})
        if not self._ok(r):
            return {"success": False, "errors": [str(r)[:200]]}
        t = r.get("tab", {})
        self.cur = t.get("id", self.cur)
        self._invalidate()
        return {"success": True, "tab": self.cur,
                "url": t.get("url", "")}

    def switch_tab(self, tab_id):
        r = self._post("/tabs/activate", {"id": str(tab_id)})
        if isinstance(r, dict) and r.get("ok"):
            self.cur = int(tab_id)
            self._invalidate()
            return {"success": True, "tab": self.cur}
        return {"success": False,
                "errors": [r.get("error", "switch failed")[:200]]
                if isinstance(r, dict) else ["switch failed"]}

    def close_tab(self, tab_id):
        r = self._post("/tabs/close", {"id": str(tab_id)})
        if isinstance(r, dict) and r.get("ok"):
            self._invalidate()
            return {"success": True}
        return {"success": False,
                "errors": [r.get("error", "close failed")[:200]]
                if isinstance(r, dict) else ["close failed"]}

    # ---------- page ----------

    def _invalidate(self):
        self._page_cache = None
        self._page_url = None

    @property
    def page(self):
        url = self._cur_url()
        if self._page_cache is not None and self._page_url == url:
            return self._page_cache
        try:
            r = self._get("/dom")
            html = r.get("html") or "" if isinstance(r, dict) else ""
        except Exception:
            html = ""
        p = engines.Page(url, 200, html.encode("utf-8", "replace"),
                         "text/html")
        self._page_cache = p
        self._page_url = url
        return p

    # ---------- selectors ----------

    def _sel(self, query):
        m = engines.StaticEngine.A11Y_REF_RE.match(query or "")
        if m:
            css = self._a11y_css.get(m.group(1))
            if not css:
                raise EngineError("no such reference @%s (run a11y first)"
                                  % m.group(1))
            return css
        return query

    def resolve(self, query):
        try:
            css = self._sel(query)
            r = self._post("/js", {"expr":
                "(function(){return document.querySelectorAll(%s).length})()"
                % json.dumps(css)})
            n = int(r.get("result") or 0)
            if n == 0:
                return {"success": False,
                        "errors": ["no element matches %r" % query]}
            return {"success": True, "method": "webview", "count": n}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:150]]}

    # ---------- actions ----------

    def click(self, query):
        try:
            r = self._post("/click", {"selector": self._sel(query)})
            if r.get("result") == "not-found":
                return {"success": False,
                        "errors": ["no element matches %r" % query]}
            self._invalidate()
            return {"success": True, "selector": query}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def type(self, query, text):
        try:
            r = self._post("/type", {"selector": self._sel(query),
                                     "text": text or ""})
            if r.get("result") == "not-found":
                return {"success": False,
                        "errors": ["no element matches %r" % query]}
            self._invalidate()
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def clear(self, query):
        try:
            r = self._post("/js", {"expr":
                "(function(){var el=document.querySelector(%s);"
                "if(!el) return 'not-found';"
                "el.focus();el.value='';"
                "el.dispatchEvent(new Event('input',{bubbles:true}));"
                "return 'cleared'})()" % json.dumps(self._sel(query))})
            if r.get("result") == "not-found":
                return {"success": False,
                        "errors": ["no element matches %r" % query]}
            self._invalidate()
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def select_option(self, query, choice):
        try:
            r = self._post("/js", {"expr":
                "(function(){var el=document.querySelector(%s);"
                "if(!el) return 'not-found';"
                "var v=%s, ok=false;"
                "[...el.options].forEach(function(o){"
                "if(o.label===v||o.text===v||o.value===v){el.value=o.value;ok=true}});"
                "el.dispatchEvent(new Event('change',{bubbles:true}));"
                "return ok?'selected':'no-match'})()"
                % (json.dumps(self._sel(query)), json.dumps(choice))})
            res = r.get("result")
            if res == "not-found":
                return {"success": False,
                        "errors": ["no element matches %r" % query]}
            if res != "selected":
                return {"success": False,
                        "errors": ["no option %r" % choice]}
            self._invalidate()
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def check(self, query):
        return self._set_checked(query, True)

    def uncheck(self, query):
        return self._set_checked(query, False)

    def _set_checked(self, query, val):
        try:
            r = self._post("/js", {"expr":
                "(function(){var el=document.querySelector(%s);"
                "if(!el) return 'not-found';el.checked=%s;"
                "el.dispatchEvent(new Event('change',{bubbles:true}));"
                "return 'ok'})()"
                % (json.dumps(self._sel(query)),
                   "true" if val else "false")})
            if r.get("result") == "not-found":
                return {"success": False,
                        "errors": ["no element matches %r" % query]}
            self._invalidate()
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def hover(self, query):
        # WebView has no hover; focus is the closest equivalent
        return self.focus(query)

    def focus(self, query):
        try:
            r = self._post("/js", {"expr":
                "(function(){var el=document.querySelector(%s);"
                "if(!el) return 'not-found';el.focus();return 'ok'})()"
                % json.dumps(self._sel(query))})
            if r.get("result") == "not-found":
                return {"success": False,
                        "errors": ["no element matches %r" % query]}
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def scroll(self, target="bottom"):
        js = ("window.scrollTo(0,document.body.scrollHeight)"
              if target in ("bottom", None)
              else "window.scrollTo(0,0)" if target == "top"
              else "window.scrollBy(0,%d)" % int(target or 0))
        try:
            self._post("/js", {"expr": js})
            self._invalidate()
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def evaluate(self, js):
        try:
            r = self._post("/js", {"expr": js})
            return {"success": True, "result": r.get("result")}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def wait(self, selector=None, ms=None, text=None):
        try:
            if ms:
                time.sleep(min(ms, 30000) / 1000.0)
                return {"success": True}
            if selector:
                css = self._sel(selector)
                end = time.time() + self.timeout
                while time.time() < end:
                    r = self._post("/js", {"expr":
                        "(function(){return !!document.querySelector(%s)})()"
                        % json.dumps(css)})
                    if r.get("result") == "true":
                        return {"success": True, "selector": selector}
                    time.sleep(0.5)
                return {"success": False,
                        "errors": ["timeout waiting for %r" % selector]}
            return {"success": False,
                    "errors": ["need selector or ms"]}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def console(self):
        return {"success": True, "logs": []}

    # ---------- screenshot / cookies / network ----------

    def screenshot(self, path=None, full=False, selector=None):
        path = path or os.path.join(session_mod.SCREEN_DIR,
                                    "shot-%d.png" % int(time.time()))
        try:
            r = self._req("GET", "/screenshot")
            raw = r.get("raw", b"")
            if not raw or raw[:8] != b"\x89PNG\r\n\x1a\n":
                return {"success": False,
                        "errors": ["agent did not return a PNG"]}
            with open(path, "wb") as f:
                f.write(raw)
            return {"success": True, "supported": True, "path": path,
                    "bytes": len(raw), "full": full}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def cookies(self):
        try:
            r = self._get("/cookies")
            out = []
            for c in r.get("cookies", []):
                out.append({"name": c.get("name", ""),
                            "value": str(c.get("value", ""))[:60],
                            "domain": "", "path": ""})
            return out
        except Exception:
            return []

    def network(self, pattern=None, limit=50, type_=None, status=None,
                method=None):
        try:
            r = self._get("/network")
            reqs = r.get("requests", []) if isinstance(r, dict) else []
            self.netlog = reqs
        except Exception:
            reqs = self.netlog
        out = reqs[-limit:]
        if pattern:
            out = [e for e in out if pattern in e.get("url", "")]
        if method:
            out = [e for e in out
                   if e.get("method", "").upper() == method.upper()]
        return out

    def network_clear(self):
        try:
            self._post("/network/clear", {})
        except Exception:
            pass
        self.netlog = []
        return {"success": True}

    # ---------- storage (real, via WebView) ----------

    def _origin(self):
        return ""

    def storage(self, kind="local"):
        r = self.evaluate(
            "JSON.stringify((function(){var s=%sStorage,o={};"
            "for(var i=0;i<s.length;i++){var k=s.key(i);o[k]=s.getItem(k)}"
            "return o})())" % kind)
        if not r.get("success"):
            return {"__error__": str(r.get("errors"))[:150]}
        try:
            return json.loads(r.get("result") or "{}")
        except Exception:
            return {}

    def storage_get(self, key, kind="local"):
        r = self.evaluate("%sStorage.getItem(%s)" % (kind, json.dumps(key)))
        return r.get("result") if r.get("success") else None

    def storage_set(self, key, value, kind="local"):
        r = self.evaluate("%sStorage.setItem(%s,%s)"
                          % (kind, json.dumps(key), json.dumps(value)))
        if r.get("success"):
            return {"success": True, "key": key}
        return {"success": False, "errors": r.get("errors")}

    def storage_delete(self, key, kind="local"):
        r = self.evaluate("%sStorage.removeItem(%s)" % (kind, json.dumps(key)))
        if r.get("success"):
            return {"success": True, "key": key}
        return {"success": False, "errors": r.get("errors")}

    # ---------- unsupported (honest) ----------

    def fill_form(self, fidx, values=None, auto=False, profile=None):
        raise EngineError("fill_form not implemented for webview engine")

    def submit_form(self, fidx):
        raise EngineError("submit_form not implemented for webview engine")

    def download(self, *a, **kw):
        raise EngineError("downloads not supported by the webview agent v1")

    def download_list(self):
        return []

    def download_pause(self, *a):
        raise EngineError("downloads not supported by the webview agent v1")

    def download_resume(self, *a):
        raise EngineError("downloads not supported by the webview agent v1")

    def set_proxy(self, url):
        raise EngineError("proxy must be set in the app (not supported v1)")

    def set_user_agent(self, ua):
        raise EngineError("user-agent must be set in the app (not supported v1)")

    def save_storage_state(self, path):
        raise EngineError("use the app's own session (WebView profile)")

    def load_storage_state(self, state):
        raise EngineError("use the app's own session (WebView profile)")

    def session_meta(self):
        return {"engine": "webview", "agent": self.base}

    def block_list(self):
        return []

    def perf(self, url=None):
        raise EngineError("perf not supported by the webview agent v1")

    def har_export(self, *a, **kw):
        raise EngineError("har_export not supported by the webview agent v1")

    def request(self, *a, **kw):
        raise EngineError("raw request() is static-engine only")

    def close(self):
        pass
