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
import subprocess
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
    # (xpath works via document.evaluate; indexeddb/computed_style/HAR
    #  are NOT implemented -> False/raises.
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
        "forms": True,
        "downloads": True,
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
        # auto_launch: when the agent is unreachable, try `am start` to
        # (re)launch the app instead of failing immediately.
        self.auto_launch = bool(kwargs.get("auto_launch", True))
        self._healing = False  # re-entrancy guard for ensure_alive/_req
        self._hist = {}  # tab_id -> {"history": [], "pos": -1}
        self._a11y_refs = {}
        self._a11y_sig = None
        self._a11y_pending = ({}, None)
        self._a11y_css = {}
        self._page_cache = None
        self._page_url = None
        # dry_run: verify text/state all you want, but block actuating
        # clicks and form submits unless confirm=True is passed explicitly.
        # Guards against an agent accidentally publishing (e.g. FB composer).
        self.dry_run = bool(kwargs.get("dry_run", False))
        # fail fast with a clear message when the app isn't running;
        # with auto_launch, try starting it first (agent self-heal).
        st = self._status_or_none()
        if st is None and self.auto_launch:
            if self.ensure_alive().get("success"):
                st = self._status_or_none()
                if st is not None:
                    self.cur = st.get("tab", 0)
        if st is None:
            raise EngineError(
                "webview agent unreachable at %s — open the Kancil Browser "
                "app on this phone first (auto-launch %s)" %
                (self.base, "tried" if self.auto_launch else "disabled"))

    # ---------- transport ----------

    def _req(self, method, path, body=None, _relaunched=False):
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
        except (urllib.error.URLError, ConnectionError, OSError) as e:
            # connection-level failure (app killed / not started). HTTPError
            # is a URLError subclass but means the agent IS up, so it is
            # handled above and never reaches here.
            if (self.auto_launch and not _relaunched
                    and not getattr(self, "_healing", False)):
                if self.ensure_alive().get("success"):
                    try:
                        self.sync_tab()
                    except Exception:
                        pass
                    self._invalidate()
                    return self._req(method, path, body, _relaunched=True)
            raise EngineError(
                "webview agent unreachable at %s (%s)%s" %
                (self.base, str(e)[:100],
                 " — auto-launch failed" if self.auto_launch else ""))

    def _get(self, path):
        return self._req("GET", path)

    def _post(self, path, body):
        return self._req("POST", path, body)

    def _status_or_none(self):
        try:
            st = self._get("/status")
            if isinstance(st, dict) and st.get("ok"):
                return st
        except Exception:
            pass
        return None

    def _ok(self, r):
        if isinstance(r, dict) and r.get("ok"):
            return True
        self.errors.append({"type": "agent",
                            "error": str(r)[:150],
                            "t": time.strftime("%H:%M:%S")})
        return False

    # ---------- navigation ----------

    def sync_tab(self):
        """Reconcile engine tab id with the app's active tab.

        Android can kill/recreate the app in the background; tab ids are
        persisted, but the user may also close tabs by hand. If our idea
        of the current tab no longer matches the app, adopt the app's.
        """
        try:
            st = self._get("/status")
            real = st.get("tab") if isinstance(st, dict) else None
            if real is not None and int(real) != self.cur:
                old = self.cur
                self.cur = int(real)
                return {"success": True, "recovered": True,
                        "was": old, "tab": self.cur}
            return {"success": True, "recovered": False, "tab": self.cur}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def open(self, url, data=None, idle=False, idle_timeout=15, verify=None):
        if not url.startswith(("http://", "https://", "file://")):
            url = "https://" + url
        self.sync_tab()  # cheap; keeps cur honest after app restarts
        r = self._post("/navigate", {"url": url})
        if not self._ok(r):
            return {"success": False, "url": url,
                    "errors": [str(r)[:200]]}
        time.sleep(0.4)  # let the page start loading
        self._invalidate()
        out = {"success": True, "url": url, "title": "", "tab": self.cur}
        if idle:
            w = self.wait_idle(timeout=idle_timeout)
            out["idle"] = w.get("idle", False)
            out["waited_ms"] = w.get("waited_ms", 0)
        st = self._get("/status")
        final = st.get("url", url) if isinstance(st, dict) else url
        tab_id = st.get("tab", self.cur) if isinstance(st, dict) else self.cur
        self.cur = tab_id
        h = self._hist.setdefault(tab_id, {"history": [], "pos": -1})
        h["history"].append(final)
        h["pos"] = len(h["history"]) - 1
        out.update({"url": final,
                    "title": st.get("title", "") if isinstance(st, dict) else "",
                    "tab": tab_id})
        if idle:
            sig = self._content_signal()
            out["content"] = sig
            if sig.get("shell"):
                out["warning"] = (
                    "page looks like an empty shell (title renders, no "
                    "content) — likely a login gate. Log in once in the "
                    "Kancil Browser app, then retry.")
        if verify:
            # Validator (Artemis pattern): the nav only counts if
            # the expected content actually shows up.
            vr = self.wait(selector=verify)
            out["verify"] = vr.get("success", False)
            if not vr.get("success"):
                out["errors"] = vr.get("errors")
        return out

    def _content_signal(self):
        """Heuristic: did the page render content, or just a shell?
        Returns {text_chars, media, feed, shell(bool)}. Soft signal —
        a login page itself is 'empty' by this measure."""
        try:
            r = self._post("/js", {"expr":
                "(function(){var b=document.body;"
                "var t=b?b.innerText.length:0;"
                "var m=document.querySelectorAll('video,img').length;"
                "var f=document.querySelectorAll('[role=feed],ytd-browse,"
                "ytd-rich-grid-renderer,[data-pagelet]').length;"
                "return t+'|'+m+'|'+f})()"})
            parts = str(r.get("result") or "0|0|0").split("|")
            t, m, f = (int(float(parts[0])) if len(parts) > 0 else 0,
                       int(float(parts[1])) if len(parts) > 1 else 0,
                       int(float(parts[2])) if len(parts) > 2 else 0)
            shell = (t < 500 and m == 0 and f == 0)
            return {"text_chars": t, "media": m, "feed_markers": f,
                    "shell": shell}
        except Exception:
            return {"text_chars": 0, "media": 0, "feed_markers": 0,
                    "shell": False}

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

    # Deep-query shorthands (the APK injects window.__kancilQ/QA/QX;
    # fall back to the shallow DOM calls if not injected yet).
    _DQ = ("(window.__kancilQ||function(s){"
           "return document.querySelector(s)})")
    _DQA = ("(window.__kancilQA||function(s){"
            "return document.querySelectorAll(s)})")
    _DQX = ("(window.__kancilQX||function(x){var r=document.evaluate(x,"
            "document,null,XPathResult.FIRST_ORDERED_NODE_TYPE,null);"
            "return r.singleNodeValue})")
    _DQXA = ("(window.__kancilQXA||function(x){var r=document.evaluate(x,"
             "document,null,XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,null);"
             "var o=[];for(var i=0;i<r.snapshotLength;i++)"
             "o.push(r.snapshotItem(i));return o})")

    def _target_js(self, query):
        """JS expression evaluating to the target element (or null).

        Tries, in order: CSS selector, XPath, @a11y ref (converted to CSS
        by _sel), then visible-text match (exact, then contains).
        All strategies pierce shadow DOM + same-origin iframes via the
        APK's __kancilQ/QX helpers (cross-origin frames unreachable).
        """
        q = self._sel(query)
        if self._is_xpath(q):
            xp = json.dumps(self._xpath_expr(q))
            return "%s(%s)" % (self._DQX, xp)
        qj = json.dumps(q)
        if self._looks_like_text(q):
            tq = json.dumps(q.strip())
            return ("(function(){var el=%s(%s);"
                    "if(el)return el;"
                    "el=%s(\"//*[normalize-space(.)=\"+%s+\"]\");"
                    "if(el)return el;"
                    "return %s(\"//*[contains(normalize-space(.),\"+%s+\")]\");"
                    "})()" % (self._DQ, qj, self._DQX, tq, self._DQX, tq))
        return "%s(%s)" % (self._DQ, qj)

    def _count_js(self, query):
        """JS expression evaluating to the number of matching elements."""
        q = self._sel(query)
        if self._is_xpath(q):
            xp = json.dumps(self._xpath_expr(q))
            return "%s(%s).length" % (self._DQXA, xp), "xpath"
        qj = json.dumps(q)
        js = ("(function(){var n=%s(%s).length;"
              "if(n>0)return n;" % (self._DQA, qj))
        if self._looks_like_text(q):
            tq = json.dumps(q.strip())
            js += ("n=%s(\"//*[normalize-space(.)=\"+%s+\"]\").length;"
                   "if(n>0)return n;"
                   "return %s(\"//*[contains(normalize-space(.),\"+%s+\")]\").length;"
                   % (self._DQXA, tq, self._DQXA, tq))
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

    def click(self, query, confirm=False, verify=None):
        """Click like Artemis' operator: the click only counts if its
        expected effect actually shows up. verify = selector that must
        appear after the click (waited for, same convention as scroll).
        """
        if self.dry_run and not confirm:
            return {"success": False, "errors": [
                "dry-run: click blocked — pass confirm=True to actuate"]}
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
            # Human-like: report where the click landed.
            st = self._get("/status")
            out = {"success": True, "selector": query}
            if isinstance(st, dict):
                out["url"] = st.get("url", "")
                out["title"] = st.get("title", "")
            if verify:
                # Validator: wait for the expected effect, don't assume.
                r = self.wait(selector=verify)
                out["verify"] = r.get("success", False)
                if not r.get("success"):
                    out["errors"] = r.get("errors")
            return out
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def type(self, query, text, verify=None):
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
            out = {"success": True}
            if verify:
                # Validator (Artemis pattern): typing only counts
                # if its expected effect shows up.
                vr = self.wait(selector=verify)
                out["verify"] = vr.get("success", False)
                if not vr.get("success"):
                    out["errors"] = vr.get("errors")
            return out
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

    def scroll(self, target="bottom", settle_ms=800, verify=None):
        """Scroll like a human: after moving, let the page settle so
        infinite-scroll content actually renders, and report the scroll
        delta (0 delta = nothing moved / nothing new).

        target: "bottom"/"top"/pixels(int)/selector (scroll into view).
        settle_ms: ms to wait for render after the scroll.
        verify: selector-or-text that must appear after scrolling
            (waited for instead of the blind sleep).
        """
        try:
            y0 = self._scroll_y()
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
                    if settle_ms:
                        time.sleep(min(settle_ms, 10000) / 1000.0)
                    return {"success": True, "target": target,
                            "delta": self._scroll_y() - y0}
            self._post("/js", {"expr": js})
            self._invalidate()
            out = {"success": True, "target": target}
            if verify:
                r = self.wait(selector=verify)
                out["verify"] = r.get("success", False)
                if not r.get("success"):
                    out["errors"] = r.get("errors")
            elif settle_ms:
                time.sleep(min(settle_ms, 10000) / 1000.0)
            out["delta"] = self._scroll_y() - y0
            return out
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def _scroll_y(self):
        try:
            r = self._post("/js", {"expr": "window.scrollY"})
            return int(float(r.get("result") or 0))
        except Exception:
            return 0

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

    def wait_idle(self, timeout=15, quiet_ms=800):
        """Wait until the page settles like a human would: document
        complete AND no network activity for quiet_ms. One round trip,
        no sleep-guessing."""
        try:
            r = self._post("/wait/idle",
                           {"timeout": int(timeout * 1000),
                            "quiet_ms": quiet_ms})
            if isinstance(r, dict) and r.get("ok"):
                return {"success": True, "idle": r.get("idle", False),
                        "ready_state": r.get("ready_state", "?"),
                        "waited_ms": r.get("waited_ms", 0)}
            return {"success": False, "errors": [str(r)[:200]]}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def press(self, key="Enter", selector=None, submit_fallback=True):
        """Dispatch a real KeyboardEvent (keydown/keypress/keyup), like a
        human hitting a key.

        submit_fallback: synthetic Enter does NOT trigger native form
        submission. When Enter visibly does nothing (no navigation, no new
        network requests), fall back to a real submit of the enclosing
        form — clicking its submit button first (runs validation), else
        requestSubmit()/submit(). Reported as ``submitted`` in the result.
        """
        try:
            n0 = url0 = None
            if key == "Enter" and submit_fallback:
                n0 = len(self._netlog_snapshot())
                url0 = self._cur_url()
            body = {"key": key}
            if selector:
                body["selector"] = selector
            r = self._post("/press", body)
            if isinstance(r, dict) and r.get("ok"):
                res = r.get("result", "")
                if res == "not-found":
                    return {"success": False,
                            "errors": ["element not found"]}
                self._invalidate()
                out = {"success": True, "key": key, "result": res}
                if n0 is not None:
                    out["submitted"] = self._enter_fallback(n0, url0)
                return out
            return {"success": False, "errors": [str(r)[:200]]}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def _netlog_snapshot(self):
        try:
            r = self._get("/network")
            reqs = r.get("requests", []) if isinstance(r, dict) else []
            return reqs
        except Exception:
            return []

    def _enter_fallback(self, n0, url0):
        """True submit after a synthetic Enter that did nothing observable."""
        time.sleep(0.6)
        try:
            n1 = len(self._netlog_snapshot())
            url1 = self._cur_url()
        except Exception:
            return False
        if n1 != n0 or url1 != url0:
            return False  # the key already did something; don't double-fire
        try:
            r = self._post("/js", {"expr":
                "(function(){var el=document.activeElement;"
                "var f=el&&el.form?el.form:"
                "(el&&el.closest?el.closest('form'):null);"
                "if(!f&&document.forms.length===1)f=document.forms[0];"
                "if(!f)return 'no-form';"
                "var b=f.querySelector("
                "'button[type=submit],input[type=submit]');"
                "if(b){b.click();return 'clicked-submit';}"
                "if(f.requestSubmit){f.requestSubmit();"
                "return 'requestSubmit';}"
                "f.submit();return 'submitted';})()"})
            res = r.get("result") if isinstance(r, dict) else ""
            return res if res and res != "no-form" else False
        except Exception:
            return False

    def longpress(self, selector):
        """Mobile long-press: touchstart + contextmenu on the element."""
        try:
            r = self._post("/longpress", {"selector": selector})
            if isinstance(r, dict) and r.get("ok"):
                res = r.get("result", "")
                if res == "not-found":
                    return {"success": False,
                            "errors": ["element not found: %s" % selector]}
                self._invalidate()
                return {"success": True, "selector": selector}
            return {"success": False, "errors": [str(r)[:200]]}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def click_through(self, url, click_selector, wait_selector,
                      timeout=25, confirm=False):
        """SPA warm navigation in ONE process: open url (settled) ->
        click click_selector -> wait for wait_selector.

        Many mobile SPAs (e.g. m.facebook composer) don't render their
        editor on a direct-URL load — they need the warm state from a
        click. Doing the three steps here keeps one live session, so tab
        IDs can't shift between commands (the CLI-per-command flake).
        """
        try:
            steps = []
            r = self.open(url, idle=True)
            steps.append(("open", r.get("success", False)))
            if not r.get("success"):
                return {"success": False, "errors": r.get("errors", ["open failed"]),
                        "steps": steps}
            r = self.click(click_selector, confirm=confirm)
            steps.append(("click", r.get("success", False)))
            if not r.get("success"):
                return {"success": False, "errors": r.get("errors", ["click failed"]),
                        "steps": steps}
            old_timeout, self.timeout = self.timeout, timeout
            try:
                r = self.wait(selector=wait_selector)
            finally:
                self.timeout = old_timeout
            steps.append(("wait", r.get("success", False)))
            if not r.get("success"):
                return {"success": False, "errors": r.get("errors", ["wait failed"]),
                        "steps": steps}
            return {"success": True, "steps": steps,
                    "wait_selector": wait_selector}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def composer_open(self, site="facebook", timeout=25):
        """Open a social composer via warm navigation (not direct URL).

        m.facebook's composer only renders its contenteditable editor when
        reached by clicking from the feed — a direct URL load shows an
        empty shell. This bakes in that known-good flow.
        """
        if site != "facebook":
            return {"success": False,
                    "errors": ["unknown site: %s" % site]}
        return self.click_through(
            "https://m.facebook.com/",
            "Posting status baru",
            '[contenteditable="true"]',
            timeout=timeout)

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

    def crashes(self, clear=False):
        """Last app crash recorded by the APK's UncaughtExceptionHandler
        (auto-restart guard: 3 restarts per 5 min). Lets the agent see WHY
        the app died without needing the Android crash dialog."""
        try:
            if clear:
                r = self._post("/crashes", {})
            else:
                r = self._get("/crashes")
            if isinstance(r, dict):
                r["success"] = r.get("ok", True)
                return r
            return {"success": False, "errors": ["bad /crashes response"]}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    # ---------- screenshot / cookies / network ----------

    def screenshot(self, path=None, full=False, selector=None):
        # Element screenshots via the agent's native rect+scroll+crop.
        # One retry: full-page capture can flake ("Connection reset") when
        # the app is busy rendering.
        path = path or os.path.join(session_mod.SCREEN_DIR,
                                    "shot-%d.png" % int(time.time()))
        last_err = None
        for _ in range(2):
            try:
                if selector:
                    r = self._req("GET", "/screenshot/element?selector="
                                  + urllib.parse.quote(selector, safe=""))
                else:
                    r = self._req("GET",
                                  "/screenshot/full" if full else "/screenshot")
                raw = r.get("raw", b"")
                if not raw or raw[:8] != b"\x89PNG\r\n\x1a\n":
                    last_err = "agent did not return a PNG"
                    time.sleep(1)
                    continue
                with open(path, "wb") as f:
                    f.write(raw)
                return {"success": True, "supported": True, "path": path,
                        "bytes": len(raw), "full": bool(full),
                        "selector": selector}
            except Exception as e:
                last_err = str(e)[:200]
                time.sleep(1)
        return {"success": False, "errors": [last_err or "screenshot failed"]}

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

    def cookies_clear(self, domain=None):
        """Clear WebView cookies via the app agent (all, or one domain)."""
        try:
            r = self._post("/cookies/clear",
                           {"domain": domain} if domain else {})
            if isinstance(r, dict) and r.get("ok"):
                return {"success": True, "domain": domain or "all"}
            return {"success": False,
                    "errors": [str(r)[:200] if isinstance(r, dict)
                               else "bad response"]}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def cache_clear(self):
        """Clear the WebView HTTP cache via the app agent."""
        try:
            self._post("/cache/clear", {})
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def cookies_set(self, name, value="", domain=None, path="/", max_age=0):
        """Set a cookie in the WebView (session injection). Needs agent 1.18+."""
        if not name:
            return {"success": False, "errors": ["missing name"]}
        try:
            r = self._post("/cookies/set", {
                "name": name, "value": value,
                "domain": domain or "", "path": path or "/",
                "maxAge": max_age or 0})
            if isinstance(r, dict) and r.get("ok"):
                return {"success": True, "name": name,
                        "domain": r.get("domain", domain or "")}
            return {"success": False,
                    "errors": [str(r)[:200] if isinstance(r, dict)
                               else "bad response"]}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def set_user_agent(self, ua):
        """Override the active tab's UA (agent 1.18+). Persists for the tab
        until reset_user_agent() or tab close."""
        try:
            r = self._post("/ua/set", {"ua": ua or ""})
            if isinstance(r, dict) and r.get("ok"):
                return {"success": True, "ua": (ua or "")[:80]}
            return {"success": False, "errors": ["agent rejected UA override"]}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def reset_user_agent(self):
        try:
            self._post("/ua/reset", {})
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def find(self, text, next=False):
        """Find-in-page: highlight matches, return match count."""
        if not text:
            return {"success": False, "errors": ["missing text"]}
        try:
            r = self._post("/find", {"text": text, "next": bool(next)})
            if isinstance(r, dict) and r.get("ok"):
                return {"success": True, "text": text,
                        "matches": r.get("matches", -1)}
            return {"success": False, "errors": ["find failed"]}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    _CLEAR_WHATS = ("cookies", "tabs", "netlog", "cache")

    def clear_session(self, what="all", domain=None):
        """Wipe current session state (webview engine).

        "all" clears tabs, netlog and cache but NOT cookies — those hold
        your app logins (FB, YT, ...). Pass what="cookies" explicitly
        (optionally with domain=) to wipe cookies.
        """
        if what == "all":
            items = ["tabs", "netlog", "cache"]
        else:
            items = [w.strip() for w in str(what).split(",") if w.strip()]
            bad = [w for w in items if w not in self._CLEAR_WHATS]
            if bad:
                return {"success": False,
                        "errors": ["unknown clear target(s): %s (choose from %s)"
                                   % (", ".join(bad),
                                      "all, " + ", ".join(self._CLEAR_WHATS))]}
        cleared, skipped = [], {}
        if "cookies" in items:
            r = self.cookies_clear(domain)
            if r.get("success"):
                cleared.append("cookies" + ("(%s)" % domain if domain else ""))
            else:
                skipped["cookies"] = "; ".join(r.get("errors", ["failed"]))
        if "tabs" in items:
            try:
                tabs = self.list_tabs()
                for t in tabs:
                    if not t.get("current"):
                        try:
                            self.close_tab(t["id"])
                        except Exception:
                            pass
                try:
                    self.open("about:blank")
                except Exception:
                    pass
                cleared.append("tabs")
            except Exception as e:
                skipped["tabs"] = "app unreachable: %s" % str(e)[:80]
        if "netlog" in items:
            self.network_clear()
            cleared.append("netlog")
        if "cache" in items:
            r = self.cache_clear()
            if r.get("success"):
                cleared.append("cache")
            else:
                skipped["cache"] = "; ".join(r.get("errors", ["failed"]))
        out = {"success": True, "cleared": cleared}
        if skipped:
            out["skipped"] = skipped
        return out

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

    def fill_form(self, fidx, values=None, auto=False, profile=None):
        """Fill form #fidx (0-based). values: {field_name: value}.
        Values starting with '@' + existing path = file upload."""
        try:
            forms = self.forms()
            if not (0 <= fidx < len(forms)):
                return {"success": False, "errors": ["no such form"]}
            fm = forms[fidx]
            vals = dict(values or {})
            if auto and profile:
                for fld in fm.get("fields", []):
                    if fld["name"] not in vals:
                        key = engines.guess_field(
                            fld["name"], fld.get("label", ""),
                            fld["type"])
                        if key and key in profile:
                            vals[fld["name"]] = profile[key]
            # validate @file paths early (honest failure, like static)
            for k, v in list(vals.items()):
                if isinstance(v, str) and v.startswith("@") \
                        and not os.path.exists(v[1:]):
                    return {"success": False, "errors": [
                        "upload file not found: %s" % v[1:]]}
            r = self._post("/form/fill",
                           {"form": fidx, "fields": vals, "submit": False})
            if not isinstance(r, dict) or not r.get("ok"):
                return {"success": False,
                        "errors": [str(r.get("error") if isinstance(
                            r, dict) else r)[:200]]}
            out = {"success": True, "filled": r.get("filled", []),
                   "missing": r.get("missing", [])}
            if r.get("error"):
                out["errors"] = [r["error"]]
            return out
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def submit_form(self, fidx, confirm=False):
        if self.dry_run and not confirm:
            return {"success": False, "errors": [
                "dry-run: submit blocked — pass confirm=True to actuate"]}
        try:
            r = self._post("/form/fill",
                           {"form": fidx, "fields": {}, "submit": True})
            if isinstance(r, dict) and r.get("ok", True):
                return {"success": True, "submitted": True}
            return {"success": False, "errors": [str(r)[:200]]}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def videos(self):
        """List <video> elements: src, duration, size, playback state."""
        try:
            r = self._get("/videos")
            vids = r.get("videos", []) if isinstance(r, dict) else []
            return {"success": True,
                    "videos": vids if isinstance(vids, list) else []}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:150]]}

    def touch(self, action="tap", x=None, y=None, x2=None, y2=None,
              selector=None, duration_ms=None, distance_start=None,
              distance_end=None, human=False):
        """Synthesized touch (agent 1.22+): tap / swipe / longpress / pinch.

        Coordinates are CSS px (like getBoundingClientRect); the app
        converts to view pixels. selector uses the engine's standard
        targeting: CSS, XPath, @a11y ref, or visible text ("Putar") —
        same as click(). pinch_in/pinch_out zoom around (x, y).
        For things JS click() can't drive: canvas, maps, custom gestures.
        """
        try:
            if action in ("pinch_in", "pinch_out"):
                action, distance_start, distance_end = (
                    "pinch",
                    420 if action == "pinch_in" else 120,
                    120 if action == "pinch_in" else 420,
                ) if distance_start is None else (
                    "pinch", distance_start, distance_end)
            if selector and (x is None or y is None):
                # standard targeting (CSS/XPath/@ref/text, pierces shadow
                # DOM) -> element center. Note: rect is in the element's
                # own frame; cross-frame elements may be offset.
                r = self.evaluate(
                    "(function(){var el=%s;"
                    "if(!el) return null;var b=el.getBoundingClientRect();"
                    "return b.left+b.width/2+','+b.top+b.height/2;})()"
                    % self._target_js(selector))
                res = (r.get("result") or "").strip().strip('"')
                if not res or res == "null":
                    return {"success": False,
                            "errors": ["no element matches %r" % (selector,)]}
                try:
                    px, py = res.split(",")
                    x, y = float(px), float(py)
                except ValueError:
                    return {"success": False,
                            "errors": ["bad rect: %s" % res[:60]]}
            body = {"action": action}
            if x is not None:
                body["x"] = x
            if y is not None:
                body["y"] = y
            if x2 is not None:
                body["x2"] = x2
            if y2 is not None:
                body["y2"] = y2
            if duration_ms:
                body["duration_ms"] = duration_ms
            if distance_start is not None:
                body["distance_start"] = distance_start
            if distance_end is not None:
                body["distance_end"] = distance_end
            if human:
                body["human"] = True
            r = self._post("/touch", body)
            if isinstance(r, dict) and r.get("ok"):
                return {"success": True, "action": r.get("action", action),
                        "x": r.get("x"), "y": r.get("y")}
            return {"success": False, "errors": [str(r)[:200]]}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def upload(self, path):
        """Stage a file for the next file-chooser (input[type=file] click).

        The file bytes are base64'd to the app (agent 1.19+), so ANY local
        path works — including Termux-private dirs the app cannot read
        itself. Older agents (<1.19) only accept the path-staging mode.
        """
        import base64
        try:
            if not os.path.exists(path):
                return {"success": False,
                        "errors": ["file not found: %s" % path]}
            size = os.path.getsize(path)
            if size > 5 * 1024 * 1024:
                return {"success": False,
                        "errors": ["file too large (max 5MB): %s" % path]}
            with open(path, "rb") as fh:
                data = base64.b64encode(fh.read()).decode("ascii")
            r = self._post("/upload", {"filename": os.path.basename(path),
                                       "data": data})
            if isinstance(r, dict) and r.get("ok"):
                return {"success": True, "path": r.get("path", path),
                        "hint": r.get("hint", "")}
            # pre-1.19 agent: fall back to path staging (file must be
            # readable by the app, e.g. shared storage)
            if isinstance(r, dict) and "missing path" in str(
                    r.get("error", "")):
                r = self._post("/upload", {"path": path})
                if isinstance(r, dict) and r.get("ok"):
                    return {"success": True, "path": path,
                            "hint": r.get("hint", "")}
            return {"success": False, "errors": [str(r)[:200]]}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def download(self, url, path=None):
        """Enqueue a download via the app's DownloadManager."""
        try:
            r = self._post("/download", {"url": url})
            if isinstance(r, dict) and r.get("ok"):
                return {"success": True, "id": r.get("id"), "url": url}
            return {"success": False, "errors": [str(r)[:200]]}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def download_list(self):
        try:
            r = self._get("/downloads")
            dls = r.get("downloads", []) if isinstance(r, dict) else []
            return dls if isinstance(dls, list) else []
        except Exception:
            return []

    def download_pause(self, did):
        return {"success": False,
                "errors": ["pause not supported by the webview agent"]}

    def download_resume(self, did):
        return {"success": False,
                "errors": ["resume not supported by the webview agent"]}

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

    def set_proxy(self, url):
        raise EngineError("proxy must be set in the app (not supported v1)")

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
                return {"success": True, "patterns": cur,
                        "cache_mode": r.get("cache_mode", "?")}
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

    def request(self, *a, **kw):
        raise EngineError("raw request() is static-engine only")

    def close(self):
        pass

    # ---------- resilience & captcha helpers (farm-grade) ----------
    # Added for real-world sessions: tab hangs, app kills, React forms,
    # and Turnstile auto-solve flows found during TokenHarbor testing.

    APP_PACKAGE = "com.kancil.browser"
    APP_ACTIVITY = "com.kancil.browser/.MainActivity"

    def ensure_alive(self, retries=2, relaunch=True, wait=1.5):
        """Check the app agent is reachable; optionally relaunch it.

        Android kills background WebView apps under memory pressure —
        Connection refused means the agent is gone, not that the engine
        is broken. If `relaunch` is true and /status fails, fire
        `am start` (Android only) and poll until it answers again.

        Re-entrancy guard: the _req auto-heal hook calls this method, and
        this method probes via _get/_req — without the guard that's
        infinite recursion when the agent is down.
        """
        _was_healing = getattr(self, "_healing", False)
        self._healing = True
        try:
            return self._ensure_alive_inner(retries, relaunch, wait)
        finally:
            self._healing = _was_healing

    def _ensure_alive_inner(self, retries, relaunch, wait):
        for attempt in range(retries + 1):
            try:
                st = self._get("/status")
                if isinstance(st, dict) and st.get("ok"):
                    return {"success": True, "agent": st.get("agent"),
                            "relaunched": attempt > 0}
                raise Exception("bad /status: %s" % str(st)[:120])
            except Exception as e:
                if attempt >= retries or not relaunch:
                    return {"success": False,
                            "errors": [str(e)[:200]]}
                try:
                    subprocess.run(
                        ["am", "start", "-n", self.APP_ACTIVITY],
                        capture_output=True, timeout=8)
                except Exception:
                    pass  # not Android / no `am` — give up quietly
                end = time.time() + 8
                while time.time() < end:
                    time.sleep(wait)
                    try:
                        r = self.ensure_alive(retries=0, relaunch=False)
                        if isinstance(r, dict) and r.get("success"):
                            r["relaunched"] = True
                        return r
                    except Exception:
                        pass
        return {"success": False, "errors": ["unreachable"]}

    def wait_ready(self, timeout=25, poll=0.7, auto_reload=True,
                   reload_after=10):
        """Wait until the page reaches readyState 'complete'.

        The APK can hang in `loading` forever (renderer stall) — this
        polls, and if still loading after `reload_after` seconds it
        triggers /reload once and keeps waiting. Returns the final
        readyState; success=False on timeout (caller may open a fresh
        tab instead of fighting the stale one).
        """
        end = time.time() + timeout
        reloaded = False
        last_state = "?"
        while time.time() < end:
            try:
                r = self._post("/js", {"expr":
                    "JSON.stringify({ready: document.readyState, "
                    "len: document.body ? document.body.innerText.length : -1})"})
                raw = r.get("result", "{}")
                try:
                    d = json.loads(raw)
                except Exception:
                    d = {}
                last_state = d.get("ready", "?")
                if last_state == "complete" and int(d.get("len", 0)) >= 0:
                    return {"success": True, "ready": "complete",
                            "body_len": d.get("len")}
            except Exception:
                pass
            if auto_reload and not reloaded and \
                    time.time() - (end - timeout) > reload_after:
                try:
                    self._post("/reload", {})
                except Exception:
                    pass
                reloaded = True
            time.sleep(poll)
        return {"success": False, "ready": last_state,
                "errors": ["tab appears stuck in %r — open a fresh tab"
                           % last_state]}

    def has_button(self, text, exact=True, partial=True):
        """True if a <button>/[role=button] with innerText exists."""
        try:
            r = self._post("/js", {"expr":
                '''(function(){
                  var hay=[...document.querySelectorAll(
                    'button,[role=button],[data-testid*=button]')]
                    .map(b=>(b.innerText||b.value||'').trim())
                    .filter(Boolean);
                  var t=%s;
                  if(%s) return hay.some(x=>x===t)?1:0;
                  return hay.some(x=>x.indexOf(t)>=0)?1:0;
                })()''' % (json.dumps(text),
                           "true" if exact else "false")})
            return r.get("result") == "1"
        except Exception:
            return False

    def click_button(self, text, exact=True, wait_ms=300):
        """Click a button by its visible text (React/Next friendly).

        Handles the common Next.js/React signup forms where buttons have
        no stable id. Returns not-found if no button matches, so callers
        can branch instead of guessing selectors.
        """
        flags = ("true" if exact else "false", "true")
        try:
            r = self._post("/js", {"expr":
                '''(function(){
                  var t=%s;
                  var found=null;
                  var all=[...document.querySelectorAll(
                    'button,[role=button]')];
                  for (var i=0;i<all.length;i++){
                    var x=(all[i].innerText||all[i].value||'').trim();
                    if (%s ? x===t : x.indexOf(t)>=0){found=all[i];break;}
                  }
                  if(!found) return 'not-found';
                  found.scrollIntoView({block:'center'});
                  found.click();
                  return 'clicked';
                })()''' % (json.dumps(text), flags[0])})
            res = r.get("result")
            if res == "not-found":
                return {"success": False,
                        "errors": ["no button with text %r" % text]}
            if wait_ms:
                time.sleep(wait_ms / 1000.0)
            self._invalidate()
            return {"success": True, "button": text, "clicked": res}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def wait_token(self, selector='input[name=cf-turnstile-response]',
                   min_len=100, timeout=30, poll=1.0):
        """Wait until a hidden token input is populated (Turnstile flow).

        Cloudflare Turnstile renders the widget after form submit and
        fills a hidden input 3-8s later on trusted WebViews. Polls until
        value length >= min_len. Returns token length + head preview.
        """
        end = time.time() + timeout
        n = -1
        while time.time() < end:
            try:
                r = self._post("/js", {"expr":
                    '''(function(){
                      var el=document.querySelector(%s);
                      return el ? el.value.length : -1;
                    })()''' % json.dumps(selector)})
                n = int(r.get("result") or -1)
            except Exception:
                n = -1
            if n >= min_len:
                head = ""
                try:
                    r = self._post("/js", {"expr":
                        '''(function(){
                          var el=document.querySelector(%s);
                          return el ? el.value.slice(0,18) : '';
                        })()''' % json.dumps(selector)})
                    head = str(r.get("result") or "")
                except Exception:
                    pass
                return {"success": True, "token_len": n,
                        "token_head": head}
            time.sleep(poll)
        return {"success": False, "token_len": n,
                "errors": ["token input %r never reached %d chars"
                           % (selector, min_len)]}

    def submit_with_captcha(self, button_text="Create account",
                            token_selector='input[name=cf-turnstile-response]',
                            settle=5, timeout=30, success_url_sub=None):
        """Two-step form submit for sites with post-click Turnstile.

        Flow observed on TokenHarbor: click submit -> widget renders ->
        token fills in 3-8s -> clicking submit again with a populated
        token passes. This encapsulates that dance and returns which
        step failed if any.
        """
        out = {"step1_click": False, "token": None, "step2_click": False}
        r1 = self.click_button(button_text)
        out["step1_click"] = r1.get("success", False)
        if not r1.get("success"):
            if "not-found" in " ".join(r1.get("errors", [])):
                return {**out, "success": False,
                        "phase": "button-missing",
                        "errors": r1.get("errors")}
        tok = self.wait_token(selector=token_selector, timeout=timeout)
        out["token"] = tok.get("success", False)
        if not tok.get("success"):
            return {**out, "success": False, "phase": "token",
                    "errors": tok.get("errors")}
        if settle:
            time.sleep(settle)
        r2 = self.click_button(button_text, wait_ms=400)
        out["step2_click"] = r2.get("success", False)
        if not r2.get("success"):
            return {**out, "success": False, "phase": "submit",
                    "errors": r2.get("errors")}
        out["success"] = True
        return out

    def type_verified(self, query, text, attempts=2, poll=0.5):
        """type() + verify the value actually landed.

        React controlled inputs can silently drop synthetic values; this
        types, reads back, and retries once if the DOM value doesn't
        match. Returns success only when verified.
        """
        for _ in range(max(attempts, 1)):
            r = self.type(query, text)
            if not r.get("success"):
                return r
            time.sleep(poll)
            got = ""
            try:
                v = self._post("/js", {"expr":
                    "(function(){var el=%s;if(!el)return '';"
                    "return el.value||''})()" % self._target_js(query)})
                got = str(v.get("result") or "")
            except Exception:
                got = ""
            if got == (text or ""):
                return {"success": True, "value": got}
        return {"success": False,
                "errors": ["value did not stick: got %r" % got[:60]]}
