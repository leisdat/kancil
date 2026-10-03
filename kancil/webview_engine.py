"""WebViewEngine — drive the Kancil Browser APK on the same phone.

The APK runs a tiny agent HTTP server on 127.0.0.1:8080 backed by a real
System WebView (Chromium): full JS, video, logins, captchas. The user browses
by hand once (login/captcha), the agent reuses that live session.

Same interface as StaticEngine/PlaywrightEngine; unsupported ops raise
EngineError instead of pretending to work.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import engines
from .engines import EngineError
from . import session as session_mod


class WebViewEngine:
    name = "webview"
    # Honest capabilities: only what the APK agent actually implements.
    # (xpath works via document.evaluate; indexeddb/computed_style/forms/
    #  HAR/fill_form are NOT implemented -> False/raises.
    #  network_interception = pattern-based request *blocking* via the
    #  agent blocklist (no response mocking like CDP Fetch).)
    capabilities = {
        "javascript": True,
        "screenshot": True,
        "real_localstorage": True,
        "indexeddb": False,
        "network_interception": True,
        "computed_style": False,
        "bounding_box": False,
        "console_capture": True,
        "video": True,
        "cookies": True,
        "forms": False,
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
            # Verify the app really switched: the tab list may be stale
            # (Android can recreate the app, reassigning tab IDs).
            try:
                st = self._get("/status")
                real = int(st.get("tab", -1))
            except Exception:
                real = -1
            self.cur = int(tab_id)
            self._invalidate()
            if real != self.cur:
                return {"success": False, "tab": real,
                        "errors": ["tab %d no longer exists (app recreated? "
                                   "active tab is now %d; run 'tabs' again)"
                                   % (self.cur, real)]}
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

    # ---------- selectors: CSS, XPath, @ref, visible text ----------

    def _is_xpath(self, q):
        q = (q or "").strip()
        return (q.startswith("xpath=") or q.startswith("//")
                or q.startswith("(//"))

    def _xpath_expr(self, q):
        q = q.strip()
        return q[6:] if q.startswith("xpath=") else q

    def _looks_like_text(self, q):
        """Plain visible text (not CSS/XPath/@ref)? e.g. click "Login"."""
        if not q or self._is_xpath(q):
            return False
        if engines.StaticEngine.A11Y_REF_RE.match(q):
            return False
        return not re.search(r'[#.\[\]>+~:()@=/"\'*|]', q)

    def _el_err(self, r, query):
        """Error message if an element action failed, else None.

        The APK returns 'not-found' for missing elements and 'ERR:...'
        for JS exceptions (e.g. invalid selector syntax) — both must be
        failures, never silent success.
        """
        res = r.get("result") if isinstance(r, dict) else None
        if res == "not-found":
            return "no element matches %r" % (query,)
        if isinstance(res, str) and res.startswith("ERR:"):
            return "JS error for %r: %s" % (query, res[4:100])
        return None

    def _target_js(self, query):
        """JS expression evaluating to the target element (or null).

        Tries, in order: CSS selector, XPath, @a11y ref (converted to CSS
        by _sel), then visible-text match (exact, then contains).
        """
        q = self._sel(query)
        if self._is_xpath(q):
            xp = json.dumps(self._xpath_expr(q))
            return ("(function(){var r=document.evaluate(%s,document,null,"
                    "XPathResult.FIRST_ORDERED_NODE_TYPE,null);"
                    "return r.singleNodeValue;})()" % xp)
        qj = json.dumps(q)
        if self._looks_like_text(q):
            tq = json.dumps(q.strip())
            return ("(function(){var el=document.querySelector(%s);"
                    "if(el)return el;"
                    "var x=function(xp){var r=document.evaluate(xp,document,"
                    "null,XPathResult.FIRST_ORDERED_NODE_TYPE,null);"
                    "return r.singleNodeValue;};"
                    "el=x(\"//*[normalize-space(.)=\"+%s+\"]\");"
                    "if(el)return el;"
                    "return x(\"//*[contains(normalize-space(.),\"+%s+\")]\");"
                    "})()" % (qj, tq, tq))
        return "document.querySelector(%s)" % qj

    def _count_js(self, query):
        """JS expression evaluating to the number of matching elements."""
        q = self._sel(query)
        if self._is_xpath(q):
            xp = json.dumps(self._xpath_expr(q))
            return ("(function(){var r=document.evaluate(%s,document,null,"
                    "XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,null);"
                    "return r.snapshotLength})()" % xp), "xpath"
        qj = json.dumps(q)
        js = ("(function(){var n=document.querySelectorAll(%s).length;"
              "if(n>0)return n;" % qj)
        if self._looks_like_text(q):
            tq = json.dumps(q.strip())
            js += ("var x=function(xp){var r=document.evaluate(xp,document,"
                   "null,XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,null);"
                   "return r.snapshotLength;};"
                   "n=x(\"//*[normalize-space(.)=\"+%s+\"]\");"
                   "if(n>0)return n;"
                   "return x(\"//*[contains(normalize-space(.),\"+%s+\")]\");"
                   % (tq, tq))
        else:
            js += "return 0;"
        return js + "})()", "webview"

    def resolve(self, query):
        try:
            js, method = self._count_js(query)
            r = self._post("/js", {"expr": js})
            try:
                n = int(r.get("result") or 0)
            except (TypeError, ValueError):
                n = 0
            if n == 0:
                return {"success": False,
                        "errors": ["no element matches %r" % query]}
            return {"success": True, "method": method, "count": n}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:150]]}

    # ---------- actions ----------

    def click(self, query):
        try:
            r = self._post("/js", {"expr":
                "(function(){var el=%s;"
                "if(!el) return 'not-found';"
                "el.scrollIntoView({block:'center'});el.click();"
                "return 'clicked'})()" % self._target_js(query)})
            err = self._el_err(r, query)
            if err:
                return {"success": False, "errors": [err]}
            self._invalidate()
            return {"success": True, "selector": query}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def type(self, query, text):
        # Native value setter (not execCommand): works with React/Vue/Angular
        # controlled inputs because it triggers their value tracking.
        try:
            r = self._post("/js", {"expr":
                "(function(){var el=%s;"
                "if(!el) return 'not-found';"
                "var v=%s;"
                "el.focus();"
                "try{"
                "var proto=el instanceof HTMLTextAreaElement"
                "?HTMLTextAreaElement.prototype"
                ":(el instanceof HTMLSelectElement"
                "?HTMLSelectElement.prototype:HTMLInputElement.prototype);"
                "var d=Object.getOwnPropertyDescriptor(proto,'value');"
                "if(d&&d.set)d.set.call(el,v);else el.value=v;"
                "}catch(e){el.value=v;}"
                "el.dispatchEvent(new Event('input',{bubbles:true}));"
                "el.dispatchEvent(new Event('change',{bubbles:true}));"
                "return 'typed'})()"
                % (self._target_js(query), json.dumps(text or ""))})
            err = self._el_err(r, query)
            if err:
                return {"success": False, "errors": [err]}
            self._invalidate()
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def clear(self, query):
        try:
            r = self._post("/js", {"expr":
                "(function(){var el=%s;"
                "if(!el) return 'not-found';"
                "el.focus();"
                "try{"
                "var proto=el instanceof HTMLTextAreaElement"
                "?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;"
                "var d=Object.getOwnPropertyDescriptor(proto,'value');"
                "if(d&&d.set)d.set.call(el,'');else el.value='';"
                "}catch(e){el.value='';}"
                "el.dispatchEvent(new Event('input',{bubbles:true}));"
                "return 'cleared'})()" % self._target_js(query)})
            err = self._el_err(r, query)
            if err:
                return {"success": False, "errors": [err]}
            self._invalidate()
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def select_option(self, query, choice):
        try:
            r = self._post("/js", {"expr":
                "(function(){var el=%s;"
                "if(!el) return 'not-found';"
                "var v=%s, ok=false;"
                "[...el.options].forEach(function(o){"
                "if(o.label===v||o.text===v||o.value===v){el.value=o.value;ok=true}});"
                "el.dispatchEvent(new Event('change',{bubbles:true}));"
                "return ok?'selected':'no-match'})()"
                % (self._target_js(query), json.dumps(choice))})
            res = r.get("result")
            err = self._el_err(r, query)
            if err:
                return {"success": False, "errors": [err]}
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
                "(function(){var el=%s;"
                "if(!el) return 'not-found';el.checked=%s;"
                "el.dispatchEvent(new Event('change',{bubbles:true}));"
                "el.dispatchEvent(new MouseEvent('click',{bubbles:true}));"
                "return 'ok'})()"
                % (self._target_js(query),
                   "true" if val else "false")})
            err = self._el_err(r, query)
            if err:
                return {"success": False, "errors": [err]}
            self._invalidate()
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def hover(self, query):
        # No mouse in WebView, but mouse events trigger :hover CSS/JS.
        try:
            r = self._post("/js", {"expr":
                "(function(){var el=%s;"
                "if(!el) return 'not-found';"
                "['mouseover','mouseenter','mousemove'].forEach(function(t){"
                "el.dispatchEvent(new MouseEvent(t,{bubbles:true,"
                "cancelable:true,view:window}));});"
                "return 'hovered'})()" % self._target_js(query)})
            err = self._el_err(r, query)
            if err:
                return {"success": False, "errors": [err]}
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def focus(self, query):
        try:
            r = self._post("/js", {"expr":
                "(function(){var el=%s;"
                "if(!el) return 'not-found';el.focus();return 'ok'})()"
                % self._target_js(query)})
            err = self._el_err(r, query)
            if err:
                return {"success": False, "errors": [err]}
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def scroll(self, target="bottom"):
        try:
            if target in ("bottom", None):
                js = "window.scrollTo(0,document.body.scrollHeight)"
            elif target == "top":
                js = "window.scrollTo(0,0)"
            else:
                try:
                    js = "window.scrollBy(0,%d)" % int(target or 0)
                except (ValueError, TypeError):
                    # treat as selector -> scroll element into view
                    r = self._post("/js", {"expr":
                        "(function(){var el=%s;"
                        "if(!el)return 'not-found';"
                        "el.scrollIntoView({block:'center'});"
                        "return 'ok'})()" % self._target_js(str(target))})
                    err = self._el_err(r, target)
                    if err:
                        return {"success": False, "errors": [err]}
                    self._invalidate()
                    return {"success": True}
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
            if text:
                tj = json.dumps(text)
                end = time.time() + self.timeout
                while time.time() < end:
                    r = self._post("/js", {"expr":
                        "(function(){return document.body.innerText"
                        ".includes(%s)})()" % tj})
                    if r.get("result") == "true":
                        return {"success": True, "text": text}
                    time.sleep(0.5)
                return {"success": False,
                        "errors": ["timeout waiting for text %r" % text]}
            if selector:
                end = time.time() + self.timeout
                while time.time() < end:
                    js, _m = self._count_js(selector)
                    r = self._post("/js", {"expr": js})
                    try:
                        n = int(r.get("result") or 0)
                    except (TypeError, ValueError):
                        n = 0  # JS error -> treat as no match, keep polling
                    if n > 0:
                        return {"success": True, "selector": selector}
                    time.sleep(0.5)
                return {"success": False,
                        "errors": ["timeout waiting for %r" % selector]}
            return {"success": False,
                    "errors": ["need selector, ms, or text"]}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def console(self):
        """JS console messages captured by the APK's console override."""
        try:
            r = self._get("/console")
            logs = r.get("logs", []) if isinstance(r, dict) else []
            if isinstance(logs, str):
                try:
                    logs = json.loads(logs or "[]")
                except Exception:
                    logs = []
            return {"success": True,
                    "logs": logs if isinstance(logs, list) else []}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:150]]}

    def console_clear(self):
        try:
            self._post("/console/clear", {})
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:150]]}

    # ---------- screenshot / cookies / network ----------

    def screenshot(self, path=None, full=False, selector=None):
        # Element screenshots are NOT supported: fail loudly instead of
        # silently ignoring the flag. Full-page uses the APK's native
        # scroll-and-stitch capture.
        if selector:
            return {"success": False,
                    "errors": ["element screenshot not supported "
                               "by the webview agent (window/full only)"]}
        path = path or os.path.join(session_mod.SCREEN_DIR,
                                    "shot-%d.png" % int(time.time()))
        try:
            r = self._req("GET", "/screenshot/full" if full else "/screenshot")
            raw = r.get("raw", b"")
            if not raw or raw[:8] != b"\x89PNG\r\n\x1a\n":
                return {"success": False,
                        "errors": ["agent did not return a PNG"]}
            with open(path, "wb") as f:
                f.write(raw)
            return {"success": True, "supported": True, "path": path,
                    "bytes": len(raw), "full": bool(full)}
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
        if type_:
            out = [e for e in out if type_ in e.get("mime", "")]
        if status is not None:
            out = [e for e in out if e.get("status") == status]
        return out

    def network_clear(self):
        try:
            self._post("/network/clear", {})
        except Exception:
            pass
        self.netlog = []
        return {"success": True}

    # ---------- storage (real, via WebView) ----------

    def forms(self):
        """List forms on the page (same shape as the static engine)."""
        try:
            p = self.page
        except Exception as e:
            raise EngineError(str(e)[:150])
        if not p:
            return []
        out = []
        for i, fm in enumerate(p.forms or []):
            out.append({"id": i + 1, "action": fm["action"],
                        "method": fm["method"],
                        "fields": [{"name": x["name"], "type": x["type"],
                                    "label": x.get("label", "")[:40]}
                                   for x in fm["inputs"] if x["name"]]})
        return out

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
            # result may be the string "null"/"" (empty storage, blocked
            # origin) -> json.loads gives None -> normalize to {}
            data = json.loads(r.get("result") or "{}")
        except Exception:
            data = {}
        return data if isinstance(data, dict) else {}

    def storage_get(self, key, kind="local"):
        r = self.evaluate("%sStorage.getItem(%s)" % (kind, json.dumps(key)))
        if not r.get("success"):
            return None
        v = r.get("result")
        # missing key -> JS null -> the string "null"
        return None if v in (None, "null") else v

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
        """Agent request blocklist (URL substring patterns)."""
        try:
            r = self._get("/blocklist")
            pats = r.get("patterns", []) if isinstance(r, dict) else []
            return list(pats) if isinstance(pats, list) else []
        except Exception:
            return []

    def block_add(self, pattern):
        """Add URL substring patterns to the agent blocklist."""
        try:
            if isinstance(pattern, str):
                pattern = [pattern]
            cur = self.block_list()
            for p in pattern:
                p = (p or "").strip().lower()
                if p and p not in cur:
                    cur.append(p)
            r = self._post("/blocklist", {"patterns": cur})
            if isinstance(r, dict) and r.get("ok"):
                return {"success": True, "patterns": cur}
            return {"success": False,
                    "errors": [str(r)[:200]]}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def block_clear(self):
        try:
            r = self._post("/blocklist", {"patterns": []})
            if isinstance(r, dict) and r.get("ok"):
                return {"success": True}
            return {"success": False, "errors": [str(r)[:200]]}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def perf(self, url=None):
        raise EngineError("perf not supported by the webview agent v1")

    def har_export(self, *a, **kw):
        raise EngineError("har_export not supported by the webview agent v1")

    def request(self, *a, **kw):
        raise EngineError("raw request() is static-engine only")

    def close(self):
        pass
