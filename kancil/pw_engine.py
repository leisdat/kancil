"""Playwright engine for Kancil.

Requires: pip install playwright  +  playwright install chromium [--only-shell]
Works in Termux via proot-distro Ubuntu (ARM64).

Honest capabilities: real JS, screenshots, real localStorage/IndexedDB,
network interception, console capture, bounding boxes.
"""
import os
import time

from . import engines
from . import session as session_mod
from .dom import inspect_element


def _browser_env():
    """Environment for the browser child process.

    Termux exports LD_PRELOAD=libtermux-exec.so, which breaks Chromium/Firefox
    child processes (CANNOT LINK EXECUTABLE). Strip it for the browser only.
    Returns None when no fixup is needed (playwright then inherits os.environ).
    """
    if "LD_PRELOAD" not in os.environ:
        return None
    return {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}


def find_camoufox_binary():
    """Find a cached Camoufox firefox binary (Termux + desktop Linux).

    Camoufox downloads go to the app cache; playwright can drive the binary
    directly via executable_path. Returns the path or None.
    """
    import glob
    home = os.path.expanduser("~")
    patterns = [
        "/data/data/com.termux/cache/camoufox/browsers/*/firefox",
        os.path.join(home, ".cache/camoufox/browsers/*/firefox"),
        os.path.join(home, ".cache/camoufox/*/firefox"),
    ]
    for pat in patterns:
        for p in sorted(glob.glob(pat)):
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


class PlaywrightEngine(engines.StaticEngine):
    name = "playwright"
    capabilities = {
        "javascript": True,
        "screenshot": True,
        "real_localstorage": True,
        "indexeddb": True,
        "network_interception": True,
        "computed_style": True,
        "bounding_box": True,
        "console_capture": True,
        "video": False,
        "cookies": True,
        "forms": True,
        "downloads": True,
        "xpath": True,
        "css_selectors": True,
    }

    def __init__(self, timeout=25, headless=True, proxy=None, user_agent=None,
                 storage_state=None, viewport=None, browser="chromium",
                 executable_path=None):
        # don't call StaticEngine.__init__ (we override networking),
        # but set the attributes its shared helpers need
        import http.cookiejar
        import urllib.request
        self.timeout = timeout
        self.retries = 0
        self.ua = user_agent or engines.UA_DEFAULT
        self.proxy = proxy
        self.viewport = viewport or {"width": 1280, "height": 800}
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self._ls = {}
        self._ss = {}
        self.tabs = []          # list of {"pw_page": page, "history": [urls], "pos": int}
        self.cur = 0
        self.netlog = []
        self._req_id = 0
        self.errors = []
        self.downloads = {}
        self._dl_id = 0
        self._console = []
        self.block_patterns = []
        self._route_installed = False
        self._a11y_refs = {}
        self._a11y_sig = None
        self._a11y_css = {}
        self._a11y_pending = ({}, None)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise engines.EngineError(
                "playwright not installed. Run: pip install playwright && "
                "playwright install chromium --only-shell")
        self._pw = sync_playwright().start()
        try:
            launch_kw = {"headless": headless}
            if proxy:
                launch_kw["proxy"] = {"server": proxy}
            if browser == "firefox" and not executable_path:
                executable_path = find_camoufox_binary()
            if executable_path:
                launch_kw["executable_path"] = executable_path
            env = _browser_env()
            if env is not None:
                launch_kw["env"] = env
            launcher = getattr(self._pw, browser, None)
            if launcher is None:
                raise engines.EngineError(
                    "unknown playwright browser %r (choose chromium, firefox, webkit)"
                    % browser)
            self._browser = launcher.launch(**launch_kw)
        except engines.EngineError:
            self._pw.stop()
            raise
        except Exception as e:
            self._pw.stop()
            raise engines.EngineError(
                "%s launch failed: %s. Run: playwright install %s"
                % (browser, e, browser))
        self._ctx = self._browser.new_context(
            viewport=self.viewport, user_agent=self.ua,
            **({"storage_state": storage_state} if storage_state else {}))
        self._ctx.on("console", self._on_console)
        self._ctx.on("pageerror", lambda e: self.errors.append(
            {"type": "js", "error": str(e)[:200], "t": time.strftime("%H:%M:%S")}))
        self.new_tab()

    # ---------- proxy / UA are start-time options for playwright ----------
    def set_proxy(self, url):
        return {"success": False,
                "errors": ["playwright applies proxy at start only; "
                           "re-run with --proxy %s --engine playwright" % (url or "")]}

    def set_user_agent(self, ua):
        return {"success": False,
                "errors": ["playwright applies user-agent at start only; "
                           "re-run with --ua ... --engine playwright"]}

    # ---------- internal ----------
    def _on_console(self, msg):
        self._console.append({"type": msg.type, "text": msg.text[:300],
                              "t": time.strftime("%H:%M:%S")})
        if msg.type == "error":
            self.errors.append({"type": "console.error", "error": msg.text[:200],
                                "t": time.strftime("%H:%M:%S")})

    def _pp(self):
        return self.tabs[self.cur]["pw_page"]

    def _sync_page(self):
        """Build a static-style Page snapshot for DOM-dependent helpers."""
        import urllib.parse
        p = self._pp()
        try:
            html = p.content()
        except Exception:
            html = ""
        from .engines import Page
        pg = Page(p.url, 200, html.encode("utf-8", "html"), "text/html")
        return pg

    @property
    def page(self):
        t = self.tabs[self.cur]
        if t.get("_snap") is None:
            t["_snap"] = self._sync_page()
        return t["_snap"]

    def _invalidate(self):
        self.tabs[self.cur]["_snap"] = None

    def _nav_done(self, url):
        t = self.tabs[self.cur]
        t["history"] = t["history"][:t["pos"] + 1] + [url]
        t["pos"] = len(t["history"]) - 1
        self._invalidate()

    # ---------- tabs ----------
    def new_tab(self, url=None):
        pg = self._ctx.new_page()
        pg.on("request", self._on_request)
        pg.on("response", self._on_response)
        self.tabs.append({"pw_page": pg, "history": [], "pos": -1, "_snap": None})
        self.cur = len(self.tabs) - 1
        if url:
            return self.open(url)
        return {"success": True, "tab": self.cur}

    def switch_tab(self, idx):
        if 0 <= idx < len(self.tabs):
            self.cur = idx
            self.tabs[idx]["pw_page"].bring_to_front()
            return {"success": True, "tab": idx}
        return {"success": False, "errors": ["no such tab"]}

    def close_tab(self, idx=None):
        idx = self.cur if idx is None else idx
        if len(self.tabs) <= 1:
            return {"success": False, "errors": ["cannot close last tab"]}
        self.tabs[idx]["pw_page"].close()
        del self.tabs[idx]
        self.cur = min(self.cur, len(self.tabs) - 1)
        return {"success": True, "tab": self.cur}

    def list_tabs(self):
        out = []
        for i, t in enumerate(self.tabs):
            try:
                title, url = t["pw_page"].title(), t["pw_page"].url
            except Exception:
                title, url = "(closed)", ""
            out.append({"id": i, "active": i == self.cur,
                        "title": (title or "")[:80], "url": url})
        return out

    def _on_request(self, req):
        import datetime
        self._req_id += 1
        post = None
        try:
            post = req.post_data
        except Exception:
            pass
        self.netlog.append({"id": self._req_id, "t": time.strftime("%H:%M:%S"),
                            "started": datetime.datetime.now().isoformat(timespec="seconds"),
                            "method": req.method, "url": req.url, "status": None,
                            "ctype": "-", "size": 0, "ms": 0,
                            "req_headers": dict(req.headers), "res_headers": {},
                            "resource_type": req.resource_type,
                            "request_size": len(post) if post else 0,
                            "response_size": 0,
                            "timing": {"start": datetime.datetime.now().isoformat(timespec="seconds"),
                                       "duration": 0},
                            "post_data": (post[:4096] if post else None),
                            "post_truncated": bool(post and len(post) > 4096),
                            "_t0": time.time()})

    def _on_response(self, res):
        import datetime
        for e in reversed(self.netlog):
            if e["url"] == res.url and e["status"] is None:
                e["status"] = res.status
                e["ctype"] = res.headers.get("content-type", "-").split(";")[0]
                e["res_headers"] = dict(res.headers)
                e["ms"] = int((time.time() - e.pop("_t0", time.time())) * 1000)
                e["timing"]["duration"] = e["ms"]
                # capped body for text-ish resources only (never unbounded)
                rt = e.get("resource_type", "")
                if rt in ("document", "xhr", "fetch", "script"):
                    try:
                        raw = res.body()
                        e["response_size"] = len(raw)
                        e["size"] = len(raw)
                        e["res_body"], e["res_truncated"] = \
                            engines.StaticEngine._body_preview(raw)
                    except Exception:
                        pass
                else:
                    try:
                        cl = res.headers.get("content-length")
                        e["response_size"] = int(cl) if cl else 0
                        e["size"] = e["response_size"]
                    except Exception:
                        pass
                break
        if res.status >= 400:
            self.errors.append({"type": "http", "url": res.url, "status": res.status,
                                "t": time.strftime("%H:%M:%S")})

    # ---------- navigation ----------
    def open(self, url, data=None):
        import re
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
            url = "https://" + url
        try:
            self._pp().goto(url, wait_until="domcontentloaded",
                            timeout=self.timeout * 1000)
            self._nav_done(self._pp().url)
            return {"success": True, "url": self._pp().url,
                    "title": self._pp().title(), "tab": self.cur}
        except Exception as e:
            self.errors.append({"type": "network", "url": url,
                                "error": str(e)[:150], "t": time.strftime("%H:%M:%S")})
            return {"success": False, "url": url, "errors": [str(e)[:200]]}

    def back(self):
        self._pp().go_back()
        self._invalidate()
        return {"success": True, "url": self._pp().url}

    def forward(self):
        self._pp().go_forward()
        self._invalidate()
        return {"success": True, "url": self._pp().url}

    def reload(self):
        self._pp().reload()
        self._invalidate()
        return {"success": True, "url": self._pp().url}

    def history(self):
        t = self.tabs[self.cur]
        return [{"pos": i, "current": i == t["pos"], "url": u, "title": ""}
                for i, u in enumerate(t["history"])]

    # ---------- actions ----------
    def _a11y_sig_valid(self):
        # force a fresh DOM snapshot so invalidation is actually detected
        self.tabs[self.cur]["_snap"] = None
        return super()._a11y_sig_valid()

    def _loc(self, query):
        p = self._pp()
        if query.startswith("xpath=") or query.startswith("//") or query.startswith("/"):
            xp = query[6:] if query.startswith("xpath=") else query
            return p.locator("xpath=" + xp)
        if query.startswith("text="):
            return p.locator(query)
        return p.locator(query)

    def _loc_for(self, query):
        """Locator for CSS/xpath/text selectors AND @a11y refs."""
        import re
        m = engines.StaticEngine.A11Y_REF_RE.match(query or "")
        if m:
            node, err = self.resolve_a11y_ref(m.group(1))
            if err or node is None:
                raise Exception(err or "no such reference @%s" % m.group(1))
            from .dom import generate_css
            return self._pp().locator(generate_css(node))
        return self._loc(query)

    def resolve(self, query):
        # playwright resolves natively; report smart method for compat
        try:
            loc = self._loc(query)
            if loc.count() == 0:
                return {"success": False, "errors": ["no element matches %r" % query]}
            return {"success": True, "method": "playwright", "count": loc.count()}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:150]]}

    def click(self, query):
        try:
            self._loc_for(query).first.click(timeout=self.timeout * 1000)
            self._invalidate()
            return {"success": True, "selector": query}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def type(self, query, text):
        try:
            self._loc_for(query).first.press_sequentially(text, timeout=self.timeout * 1000)
            self._invalidate()
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def clear(self, query):
        try:
            self._loc_for(query).first.clear(timeout=self.timeout * 1000)
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def select_option(self, query, choice):
        try:
            self._loc_for(query).first.select_option(label=choice, timeout=self.timeout * 1000)
            return {"success": True}
        except Exception as e:
            # try by value
            try:
                self._loc_for(query).first.select_option(value=choice, timeout=self.timeout * 1000)
                return {"success": True}
            except Exception as e2:
                return {"success": False, "errors": [str(e2)[:200]]}

    def check(self, query):
        try:
            self._loc_for(query).first.check(timeout=self.timeout * 1000)
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def uncheck(self, query):
        try:
            self._loc_for(query).first.uncheck(timeout=self.timeout * 1000)
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def hover(self, query):
        try:
            self._loc_for(query).first.hover(timeout=self.timeout * 1000)
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def focus(self, query):
        try:
            self._loc_for(query).first.focus(timeout=self.timeout * 1000)
            return {"success": True}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def scroll(self, target="bottom"):
        p = self._pp()
        try:
            if target == "bottom":
                p.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            elif target == "top":
                p.evaluate("window.scrollTo(0, 0)")
            else:
                self._loc_for(target).first.scroll_into_view_if_needed(timeout=self.timeout * 1000)
            self._invalidate()
            return {"success": True, "target": target}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    # ---------- js ----------
    def evaluate(self, js):
        try:
            r = self._pp().evaluate(js)
            self._invalidate()
            return {"success": True, "supported": True, "result": r}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:300]]}

    def wait(self, selector=None, ms=None, text=None):
        p = self._pp()
        try:
            if ms:
                p.wait_for_timeout(ms)
                return {"success": True, "waited_ms": ms}
            if text:
                p.get_by_text(text).first.wait_for(timeout=self.timeout * 1000)
                return {"success": True, "text": text}
            if selector:
                p.locator(selector).first.wait_for(timeout=self.timeout * 1000)
                return {"success": True, "selector": selector}
            return {"success": False, "errors": ["need selector, ms, or text"]}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def console(self):
        return {"success": True, "logs": self._console[-50:]}

    # ---------- screenshot ----------
    def screenshot(self, path=None, full=False, selector=None):
        path = path or session_mod.SCREEN_DIR + "/shot-%d.png" % int(time.time())
        try:
            if selector:
                self._loc_for(selector).first.screenshot(path=path)
            else:
                self._pp().screenshot(path=path, full_page=full)
            import os
            return {"success": True, "supported": True, "path": path,
                    "bytes": os.path.getsize(path), "full": full}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    # ---------- storage (real) ----------
    def _origin(self):
        import urllib.parse
        return urllib.parse.urlparse(self._pp().url).netloc

    def cookies(self):
        out = []
        for c in self._ctx.cookies():
            out.append({"name": c["name"], "value": c["value"][:60],
                        "domain": c["domain"], "path": c["path"]})
        return out

    def storage(self, kind="local"):
        try:
            data = self._pp().evaluate(
                "() => { const s = %sStorage; const o = {}; "
                "for (let i=0;i<s.length;i++){ const k=s.key(i); o[k]=s.getItem(k);} "
                "return o; }" % kind)
            return dict(data)
        except Exception as e:
            return {"__error__": str(e)[:150]}

    def storage_get(self, key, kind="local"):
        try:
            return self._pp().evaluate(
                "([k, kind]) => %sStorage.getItem(k)" % kind, [key, kind])
        except Exception:
            return None

    def storage_set(self, key, value, kind="local"):
        try:
            self._pp().evaluate(
                "([k,v,kind]) => %sStorage.setItem(k,v)" % kind, [key, value, kind])
            return {"success": True, "key": key}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def storage_delete(self, key, kind="local"):
        try:
            self._pp().evaluate(
                "([k,kind]) => %sStorage.removeItem(k)" % kind, [key, kind])
            return {"success": True, "key": key}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    # ---------- request blocking (adblock-ish, saves mobile data) ----------
    def block_add(self, pattern):
        self.block_patterns.append(pattern)
        if not self._route_installed:
            self._ctx.route("**/*", self._route_handler)
            self._route_installed = True
        return {"success": True, "pattern": pattern,
                "count": len(self.block_patterns)}

    def block_list(self):
        return list(self.block_patterns)

    def block_clear(self):
        self.block_patterns = []
        if self._route_installed:
            try:
                self._ctx.unroute("**/*")
            except Exception:
                pass
            self._route_installed = False
        return {"success": True}

    def _route_handler(self, route):
        import fnmatch
        url = route.request.url
        for pat in self.block_patterns:
            if fnmatch.fnmatch(url, pat) or pat in url:
                route.abort()
                return
        route.continue_()

    # ---------- pdf ----------
    def pdf(self, path=None):
        import os
        path = path or session_mod.SCREEN_DIR + "/page-%d.pdf" % int(time.time())
        try:
            self._pp().pdf(path=path)
            return {"success": True, "supported": True, "path": path,
                    "bytes": os.path.getsize(path)}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    # ---------- forms via static snapshot ----------
    def forms(self):
        return engines.StaticEngine.forms(self)

    def fill_form(self, fidx, values=None, auto=False, profile=None):
        # fill via real DOM for reliability
        p = self.page
        if not p or not (0 <= fidx < len(p.forms)):
            return {"success": False, "errors": ["no such form"]}
        fm = p.forms[fidx]
        values = values or {}
        filled = []
        for inp in fm["inputs"]:
            if not inp["name"] or inp["type"] in ("submit", "button", "hidden"):
                continue
            sel = None
            nid = inp["node"].get("id")
            if nid:
                sel = "#" + nid
            elif inp["name"]:
                sel = "%s[name='%s']" % (inp["tag"], inp["name"])
            if not sel:
                continue
            val = values.get(inp["name"])
            if val is None and auto and profile:
                from .engines import guess_field
                key = guess_field(inp["name"], inp.get("label", ""), inp["type"])
                val = profile.get(key) if key else None
            if val is None:
                continue
            try:
                import os as _os
                if isinstance(val, str) and val.startswith("@") and _os.path.exists(val[1:]):
                    self._pp().locator(sel).first.set_input_files(val[1:], timeout=8000)
                elif inp["type"] in ("checkbox", "radio"):
                    (self._pp().locator(sel).first.check(timeout=8000)
                     if str(val).lower() not in ("", "0", "no", "false")
                     else self._pp().locator(sel).first.uncheck(timeout=8000))
                elif inp["type"] == "select":
                    self._pp().locator(sel).first.select_option(label=str(val), timeout=8000)
                else:
                    self._pp().locator(sel).first.fill(str(val), timeout=8000)
                filled.append(inp["name"])
            except Exception:
                pass
        self._invalidate()
        return {"success": True, "filled": filled, "form": fidx + 1}

    def submit_form(self, fidx):
        p = self.page
        if not p or not (0 <= fidx < len(p.forms)):
            return {"success": False, "errors": ["no such form"]}
        # click the submit button natively if present
        try:
            btn = self._pp().locator("form >> input[type=submit], form >> button[type=submit], form >> button:not([type])")
            if btn.count():
                btn.first.click(timeout=self.timeout * 1000)
                self._pp().wait_for_load_state("domcontentloaded", timeout=self.timeout * 1000)
                self._nav_done(self._pp().url)
                return {"success": True, "url": self._pp().url, "title": self._pp().title()}
        except Exception:
            pass
        return engines.StaticEngine.submit_form(self, fidx)

    # ---------- downloads via playwright ----------
    def download(self, url, path=None):
        import os
        path = path or os.path.join(engines._dl_dir(), "dl-%d.bin" % int(time.time()))
        try:
            with self._pp().expect_download(timeout=self.timeout * 1000) as dl_info:
                self._pp().evaluate("(u) => { const a=document.createElement('a'); a.href=u; a.download=''; document.body.appendChild(a); a.click(); }", url)
            dl = dl_info.value
            dl.save_as(path)
            return {"success": True, "path": path,
                    "suggested": dl.suggested_filename}
        except Exception:
            # fallback: static download
            return engines.StaticEngine.download(self, url, path)

    def perf(self, url=None):
        url = url or self._pp().url
        t0 = time.time()
        nav = self._pp().evaluate("""() => {
            const t = performance.getEntriesByType('navigation')[0];
            if (!t) return null;
            return {dns: t.domainLookupEnd - t.domainLookupStart,
                    connect: t.connectEnd - t.connectStart,
                    ttfb: t.responseStart - t.requestStart,
                    dom: t.domContentLoadedEventEnd - t.navigationStart,
                    load: t.loadEventEnd - t.navigationStart,
                    total: t.duration};
        }""")
        if not nav:
            return engines.StaticEngine.perf(self, url)
        nav["requests"] = len(self.netlog)
        nav["bytes"] = sum(e.get("size", 0) for e in self.netlog)
        nav["success"] = True
        nav["url"] = url
        nav["engine"] = "playwright-navigation-timing"
        return nav

    # ---------- persistent session (storage_state) ----------
    def save_storage_state(self, path):
        """Persist cookies + localStorage via Playwright storage_state."""
        try:
            self._ctx.storage_state(path=path)
            import os
            os.chmod(path, 0o600)
            return {"success": True, "path": path}
        except Exception as e:
            return {"success": False, "errors": [str(e)[:200]]}

    def load_storage_state(self, state):
        """Hot-swap context to a saved storage_state; re-opens tab URLs."""
        urls = []
        for t in self.tabs:
            try:
                urls.append(t["pw_page"].url)
            except Exception:
                pass
        try:
            self._ctx.close()
        except Exception:
            pass
        self._ctx = self._browser.new_context(
            viewport=self.viewport, user_agent=self.ua,
            **({"storage_state": state} if state else {}))
        self._ctx.on("console", self._on_console)
        self._ctx.on("pageerror", lambda e: self.errors.append(
            {"type": "js", "error": str(e)[:200], "t": time.strftime("%H:%M:%S")}))
        self.tabs, self.cur = [], 0
        self.new_tab()
        for u in urls[1:]:
            self.new_tab()
        for i, u in enumerate(urls):
            if u and not u.startswith("about:"):
                self.cur = i
                try:
                    self.open(u)
                except Exception:
                    pass
        self.cur = 0
        return {"success": True, "tabs": len(self.tabs)}

    def session_meta(self):
        urls = []
        for t in self.tabs:
            try:
                urls.append(t["pw_page"].url)
            except Exception:
                urls.append("")
        n_cookies = len(self.cookies())
        return {"url": urls[self.cur] if urls else "",
                "tabs": urls, "tabs_count": len(urls),
                "viewport": self.viewport, "user_agent": self.ua,
                "cookies_count": n_cookies}

    def close(self):
        try:
            self._ctx.close()
            self._browser.close()
        finally:
            self._pw.stop()
