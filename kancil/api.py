"""Kancil Agent API — structured, JSON-friendly browser automation.

Every method returns a dict: {"success": bool, ...data..., "errors": []}
Designed so Hermes (or any agent) never parses terminal text.
"""
import json
import os
import re
import time
import urllib.parse

from . import engines
from . import session as session_mod
from .devtools import extract_reader
from .dom import inspect_element, select, xpath, generate_css, generate_xpath


def ok(**data):
    d = {"success": True}
    d.update(data)
    return d


def fail(*errors, **data):
    d = {"success": False, "errors": list(errors)}
    d.update(data)
    return d


class Kancil:
    """Unified browser: static engine by default, playwright when requested+available."""

    def __init__(self, engine="static", timeout=25, retries=2,
                 cookie_file=None, profile=None, proxy=None, ua=None,
                 pw_session=None, pw_browser="chromium",
                 pw_executable_path=None, cache=True,
                 webview_host=None, webview_port=None, dry_run=False,
                 impersonate=None, webview_auto_launch=True):
        self.profile = profile or {}
        self.proxy = proxy
        self.ua = ua
        self.cache = cache
        self.dry_run = dry_run
        self.impersonate = impersonate  # static engine TLS impersonation profile
        self.pw_session = pw_session
        self.pw_browser = pw_browser
        self.pw_executable_path = pw_executable_path
        self.webview_host = webview_host
        self.webview_port = webview_port
        self.webview_auto_launch = webview_auto_launch
        self.bookmarks = []
        self._bm_id = 0
        self._engine_name = engine
        self.engine = self._make_engine(engine, timeout, retries, cookie_file)
        self._start_t = time.time()
        self._har_recording = False
        self._har_started = None

    def _make_engine(self, name, timeout, retries, cookie_file):
        if name == "playwright":
            from .pw_engine import PlaywrightEngine
            storage_state = None
            if self.pw_session:
                storage_state, _meta = session_mod.load_pw_session(self.pw_session)
            return PlaywrightEngine(timeout=timeout, proxy=self.proxy,
                                    user_agent=self.ua,
                                    storage_state=storage_state,
                                    browser=self.pw_browser,
                                    executable_path=self.pw_executable_path)
        if name == "webview":
            from .webview_engine import WebViewEngine
            kw = {"timeout": timeout}
            if self.webview_host:
                kw["host"] = self.webview_host
            if self.webview_port:
                kw["port"] = self.webview_port
            kw["dry_run"] = self.dry_run
            kw["auto_launch"] = self.webview_auto_launch
            return WebViewEngine(**kw)
        return engines.StaticEngine(timeout=timeout, retries=retries,
                                    cookie_file=cookie_file,
                                    proxy=self.proxy,
                                    user_agent=self.ua or engines.UA_DEFAULT,
                                    cache=self.cache,
                                    impersonate=self.impersonate)

    @property
    def capabilities(self):
        caps = dict(self.engine.capabilities)
        caps["engine"] = self._engine_name
        return caps

    # Hermes #4: cascade selectors. click(sel=["#a", ".b", "text=Login"])
    # tries each in order until one hits. Only ELEMENT_NOT_FOUND
    # cascades — any other error (or a verify miss) is terminal, so we
    # never double-actuate. Returns matched_selector / tried_selectors.
    _CASCADE_METHODS = {"click", "type", "clear", "select", "check",
                        "uncheck", "hover", "focus"}

    def _cascade(self, method_name, selectors, **kw):
        tried = []
        last = None
        for sel in selectors:
            if not isinstance(sel, str) or not sel:
                continue
            r = getattr(self, method_name)(sel, **kw)
            if not isinstance(r, dict):
                r = {"success": False, "errors": ["bad result"]}
            if r.get("success"):
                r["matched_selector"] = sel
                if tried:
                    r["tried_selectors"] = tried
                return r
            last = r
            tried.append(sel)
            code = (r.get("error") or {}).get("code")
            if code != "ELEMENT_NOT_FOUND":
                break
        if last is None:
            return {"success": False,
                    "errors": ["no selectors given"],
                    "error": self._envelope("INVALID_INPUT",
                                            "no selectors given")}
        last["tried_selectors"] = tried
        return last

    # Hermes #1: mutating actions auto-attach a cheap state delta
    # {url, title, text_chars, media, shell} so the agent learns "what
    # changed?" without a second call. 0 extra RT on static (from the
    # parsed Page), 1 /js probe on webview, best-effort {} elsewhere.
    @staticmethod
    def _with_delta(fn):
        import functools

        @functools.wraps(fn)
        def w(self, *a, **kw):
            r = fn(self, *a, **kw)
            if isinstance(r, dict) and r.get("success") \
                    and "delta" not in r:
                try:
                    d = self._state_delta()
                except Exception:
                    d = {}
                if d:
                    r["delta"] = d
            return r

        return w

    def _state_delta(self):
        try:
            if self._engine_name == "webview":
                fn = getattr(self.engine, "state_delta", None)
                if fn:
                    return fn() or {}
            elif self._engine_name == "static":
                page, err = self._page_or_fail()
                if not err and page is not None:
                    return {"url": getattr(page, "url", ""),
                            "title": getattr(page, "title", ""),
                            "text_chars": len(getattr(page, "text", "")
                                              or "")}
        except Exception:
            pass
        return {}

    # ---------- navigation ----------
    @_with_delta
    def open(self, url, idle=False, idle_timeout=15, verify=None):
        if self._engine_name == "webview":
            r = self.engine.open(url, idle=idle, idle_timeout=idle_timeout,
                                 verify=verify)
        else:
            r = self.engine.open(url)
        return self._wrap(r)

    @_with_delta
    def back(self, verify=None, verify_text=None):
        """Go back; with verify/verify_text, assert the landed page instead
        of silently sitting on an error page."""
        r = self._wrap(self.engine.back())
        if not r.get("success"):
            return r
        return self._nav_verify(r, verify, verify_text, "back")

    @_with_delta
    def forward(self, verify=None, verify_text=None):
        """Go forward; with verify/verify_text, assert the landed page."""
        r = self._wrap(self.engine.forward())
        if not r.get("success"):
            return r
        return self._nav_verify(r, verify, verify_text, "forward")

    def _nav_verify(self, r, verify, verify_text, op):
        if verify is None and verify_text is None:
            return r
        w = self.wait(selector=verify, text=verify_text)
        if w.get("success"):
            r["verified"] = verify if verify is not None else verify_text
            return r
        errs = w.get("errors") or ["verify failed"]
        return self._wrap(fail(
            "%s verify failed (landed on error page?): %s"
            % (op, str(errs[0])[:150]),
            verify=verify if verify is not None else verify_text))

    @_with_delta
    def reload(self):
        return self._wrap(self.engine.reload())

    def history(self):
        return ok(history=self.engine.history())

    # ---------- tabs ----------
    def tabs(self):
        return ok(tabs=self.engine.list_tabs())

    def new_tab(self, url=None):
        return self._wrap(self.engine.new_tab(url))

    def switch_tab(self, tab_id):
        return self._wrap(self.engine.switch_tab(tab_id))

    def close_tab(self, tab_id=None):
        return self._wrap(self.engine.close_tab(tab_id))

    # ---------- dom ----------
    def _page_or_fail(self):
        p = self.engine.page
        if not p:
            return None, fail("no page loaded")
        return p, None

    def dom_tree(self, max_nodes=200):
        p, err = self._page_or_fail()
        if err:
            return err
        if not p.dom:
            return fail("page has no DOM (non-HTML?)")
        out, count = [], [0]

        def rec(n, depth=0):
            if count[0] >= max_nodes:
                return
            from .dom import Node
            if isinstance(n, Node):
                count[0] += 1
                attrs = " ".join('%s="%s"' % (k, v[:30]) for k, v in list(n.attrs.items())[:4])
                out.append({"depth": depth, "tag": n.tag, "attrs": attrs,
                            "text": n.text_content()[:60] if not any(
                                isinstance(c, Node) for c in n.children) else ""})
                for ch in n.children:
                    rec(ch, depth + 1)
        for ch in p.dom.children:
            rec(ch)
        return ok(url=p.url, nodes=out, truncated=count[0] >= max_nodes)

    def dom_find(self, selector):
        from .dom import check_selector
        p, err = self._page_or_fail()
        if err:
            return err
        why = check_selector(selector)
        if why:
            return fail("invalid selector %r: %s" % (selector, why),
                        code="INVALID_INPUT",
                        hint=("supported: tag, #id, .class, [attr], "
                              "[attr=val], [attr^=/$=/*=/~=val], "
                              ":nth-child(N), :nth-of-type(N), :first-child, "
                              ":last-child, :not(x), :disabled, "
                              "descendant, >, +, ~"))
        try:
            nodes = select(p.dom, selector)
        except Exception as e:
            return fail("bad selector: %s" % e, code="INVALID_INPUT")
        if not nodes:
            return ok(selector=selector, count=0, matches=[],
                      hint=("selector is valid but matched nothing on this page; "
                            "run `dom tree` to see the actual structure"))
        return ok(selector=selector, count=len(nodes),
                  matches=[{"tag": n.tag, "text": n.text_content()[:80],
                            "css": generate_css(n), "xpath": generate_xpath(n)}
                           for n in nodes[:50]])

    def dom_xpath(self, expr):
        p, err = self._page_or_fail()
        if err:
            return err
        try:
            nodes = xpath(p.dom, expr)
        except Exception as e:
            return fail("bad xpath: %s" % e)
        return ok(xpath=expr, count=len(nodes),
                  matches=[{"tag": n.tag, "text": n.text_content()[:80],
                            "css": generate_css(n)} for n in nodes[:50]])

    def inspect(self, selector):
        from .dom import check_selector
        p, err = self._page_or_fail()
        if err:
            return err
        if selector.startswith("@e"):
            el = p.el(selector)
            if not el:
                return fail("no such element %s" % selector)
            node = el["node"]
        else:
            m = self.engine.A11Y_REF_RE.match(selector or "")
            r = self.engine.resolve(selector)
            if not r.get("success"):
                # Only blame the selector when it looks like CSS was intended;
                # plain-text queries ("Learn more") go through smart matching.
                if not m and re.search(r"[#.\[\]>+~:]", selector or ""):
                    why = check_selector(selector)
                    if why:
                        return fail("invalid selector %r: %s" % (selector, why),
                                    code="INVALID_INPUT")
                return self._wrap(r)
            node = r["node"]
        d = inspect_element(node, p.dom)
        d["supported"] = {"computed_style": self.capabilities["computed_style"],
                          "bounding_box": self.capabilities["bounding_box"]}
        return ok(**d)

    # ---------- elements ----------
    def elements(self):
        p, err = self._page_or_fail()
        if err:
            return err
        return ok(url=p.url, elements=[
            {"ref": e["ref"], "role": e["role"], "name": e["name"][:80]}
            for e in p.elements])

    def _ensure_a11y_refs(self, p):
        """Build tree + stable refs, cache on engine. Returns (tree, refs)."""
        from .dom import (a11y_tree, assign_a11y_refs, strip_a11y_nodes,
                          dom_sig, generate_css)
        doc, tnodes = a11y_tree(p.dom)
        refs = assign_a11y_refs(tnodes)
        self.engine._a11y_refs = refs
        self.engine._a11y_sig = dom_sig(p.dom)
        self.engine._a11y_pending = ({}, None)  # fresh refs supersede pending
        # persistable form: ref -> css (nodes can't be serialized)
        try:
            self.engine._a11y_css = {r: generate_css(n)
                                     for r, n in refs.items()}
        except Exception:
            self.engine._a11y_css = {}
        return strip_a11y_nodes(doc), refs

    def a11y(self):
        """Accessibility tree (nested) with stable refs like button_1."""
        p, err = self._page_or_fail()
        if err:
            return err
        if not p.dom:
            return fail("page has no DOM")
        tree, refs = self._ensure_a11y_refs(p)
        return ok(url=p.url, tree=tree, refs=sorted(refs))

    def a11y_list(self):
        """Flat element list (legacy)."""
        return self.elements()

    def a11y_find(self, query, role=None):
        from .dom import a11y_find, generate_css, generate_xpath
        p, err = self._page_or_fail()
        if err:
            return err
        if not p.dom:
            return fail("page has no DOM")
        tree, refs = self._ensure_a11y_refs(p)
        node2ref = {id(n): r for r, n in refs.items()}
        matches = []
        for it in a11y_find(p.dom, query, role):
            node = it["node"]
            matches.append({"role": it["role"], "name": it["name"][:80],
                            "ref": "@" + node2ref.get(id(node), "?"),
                            "css": generate_css(node),
                            "xpath": generate_xpath(node)})
        return ok(query=query, role=role, count=len(matches),
                  matches=matches[:20])

    @_with_delta
    def click(self, selector, confirm=False, verify=None):
        if isinstance(selector, (list, tuple)):
            return self._cascade("click", selector, confirm=confirm, verify=verify)
        if self._engine_name == "webview":
            return self._wrap(self.engine.click(selector, confirm=confirm,
                                                verify=verify))
        return self._wrap(self.engine.click(selector))

    @_with_delta
    def type(self, selector, text, verify=None):
        if isinstance(selector, (list, tuple)):
            return self._cascade("type", selector, text=text, verify=verify)
        try:
            return self._wrap(self.engine.type(selector, text, verify=verify))
        except TypeError:
            return self._wrap(self.engine.type(selector, text))

    @_with_delta
    def clear(self, selector):
        if isinstance(selector, (list, tuple)):
            return self._cascade("clear", selector)
        return self._wrap(self.engine.clear(selector))

    @_with_delta
    def select(self, selector, value):
        if isinstance(selector, (list, tuple)):
            return self._cascade("select", selector, value=value)
        return self._wrap(self.engine.select_option(selector, value))

    @_with_delta
    def check(self, selector):
        if isinstance(selector, (list, tuple)):
            return self._cascade("check", selector)
        return self._wrap(self.engine.check(selector))

    @_with_delta
    def uncheck(self, selector):
        if isinstance(selector, (list, tuple)):
            return self._cascade("uncheck", selector)
        return self._wrap(self.engine.uncheck(selector))

    @_with_delta
    def hover(self, selector):
        if isinstance(selector, (list, tuple)):
            return self._cascade("hover", selector)
        return self._wrap(self.engine.hover(selector))

    @_with_delta
    def focus(self, selector):
        if isinstance(selector, (list, tuple)):
            return self._cascade("focus", selector)
        return self._wrap(self.engine.focus(selector))

    @_with_delta
    def scroll(self, target="bottom", settle_ms=800, verify=None):
        if self._engine_name == "webview":
            return self._wrap(
                self.engine.scroll(target, settle_ms=settle_ms,
                                   verify=verify))
        return self._wrap(self.engine.scroll(target))

    # ---------- js / wait / console ----------
    def evaluate(self, js):
        return self._wrap(self.engine.evaluate(js))

    def wait(self, selector=None, ms=None, text=None):
        return self._wrap(self.engine.wait(selector=selector, ms=ms, text=text))

    def wait_idle(self, timeout=15, quiet_ms=800):
        if self._engine_name == "webview":
            return self._wrap(
                self.engine.wait_idle(timeout=timeout, quiet_ms=quiet_ms))
        return {"success": False,
                "errors": ["wait_idle() is webview-engine only"]}

    def press(self, key="Enter", selector=None, submit_fallback=True):
        if self._engine_name == "webview":
            return self._wrap(self.engine.press(
                key, selector, submit_fallback=submit_fallback))
        return {"success": False,
                "errors": ["press() is webview-engine only"]}

    def longpress(self, selector):
        if self._engine_name == "webview":
            return self._wrap(self.engine.longpress(selector))
        return {"success": False,
                "errors": ["longpress() is webview-engine only"]}

    def click_through(self, url, click_selector, wait_selector,
                      timeout=25, confirm=False):
        if self._engine_name == "webview":
            return self._wrap(self.engine.click_through(
                url, click_selector, wait_selector,
                timeout=timeout, confirm=confirm))
        return {"success": False,
                "errors": ["click_through() is webview-engine only"]}

    def composer_open(self, site="facebook", timeout=25):
        if self._engine_name == "webview":
            return self._wrap(
                self.engine.composer_open(site=site, timeout=timeout))
        return {"success": False,
                "errors": ["composer_open() is webview-engine only"]}

    def console(self):
        return self._wrap(self.engine.console())

    @_with_delta
    def touch(self, action="tap", x=None, y=None, x2=None, y2=None,
              selector=None, duration_ms=None, distance_start=None,
              distance_end=None, human=False, confirm=False):
        fn = getattr(self.engine, "touch", None)
        if not fn:
            return fail("touch needs the webview engine (agent 1.22+)",
                        supported=False)
        return self._wrap(fn(action=action, x=x, y=y, x2=x2, y2=y2,
                             selector=selector, duration_ms=duration_ms,
                             distance_start=distance_start,
                             distance_end=distance_end, human=human,
                             confirm=confirm))

    def crashes(self, clear=False):
        if self._engine_name == "webview":
            return self._wrap(self.engine.crashes(clear=clear))
        return {"success": False,
                "errors": ["crashes() is webview-engine only"]}

    def errors(self):
        errs = self.engine.errors
        return ok(errors=list(errs() if callable(errs) else errs)[-50:])

    def doctor(self):
        """One-command health check: python, dns, static, playwright,
        webview server+session, disk, env. Engine-independent."""
        from kancil import doctor as _doctor
        checks, all_ok = _doctor.run()
        return ok(checks=checks, healthy=all_ok,
                  summary=_doctor.format_text(checks))

    # ---------- screenshot ----------
    def screenshot(self, path=None, full=False, selector=None):
        path = path or os.path.join(session_mod.SCREEN_DIR,
                                    "shot-%d.png" % int(time.time()))
        return self._wrap(self.engine.screenshot(path=path, full=full, selector=selector))

    # ---------- network ----------
    def network(self, pattern=None, limit=50, type_=None, status=None, method=None):
        return ok(requests=self.engine.network(pattern=pattern, limit=limit,
                                               type_=type_, status=status,
                                               method=method))

    def network_clear(self):
        return self._wrap(self.engine.network_clear())

    def request(self, rid):
        e = self.engine.request(rid)
        if not e:
            return fail("no such request id %s" % rid)
        return ok(**{k: v for k, v in e.items() if not k.startswith("_")})

    def network_request(self, rid):
        fn = getattr(self.engine, "network_request_detail", None)
        if fn:
            return self._wrap(fn(rid))
        return self.request(rid)

    def network_response(self, rid, max_bytes=65536):
        fn = getattr(self.engine, "network_response", None)
        if fn:
            return self._wrap(fn(rid, max_bytes=max_bytes))
        return fail("response inspection not available on this engine")

    def network_bodies(self, clear=False):
        """Captured XHR/fetch response bodies (webview, agent 1.23+)."""
        fn = getattr(self.engine, "network_bodies", None)
        if fn:
            return self._wrap(fn(clear=clear))
        return fail("body capture not available on this engine")

    def solve_aliyun_puzzle(self, max_tries=4, handle_sel=".slider-move",
                            puzzle_sel="img.puzzle", verbose=True,
                            confirm=False):
        """Solve an Aliyun FeiLin slide/puzzle CAPTCHA in the current tab.

        Closed-loop human-like drag (webview, agent 1.24+). Approach
        ported from 0xgetz/aliyun-puzzle-solver (MIT). No guarantee —
        Aliyun's risk engine also weighs IP/behavior history.
        Respects dry_run: pass confirm=True to actuate."""
        from kancil import aliyun as _al
        return {"success": True,
                **_al.solve_aliyun_puzzle(
                    self, max_tries=max_tries, handle_sel=handle_sel,
                    puzzle_sel=puzzle_sel, verbose=verbose,
                    confirm=confirm)}

    def aliyun_analyze(self, handle_sel=None, puzzle_sel=None):
        """Dry-run Aliyun gap detection (no dragging). For tuning."""
        from kancil import aliyun as _al
        return {"success": True,
                **_al.analyze_aliyun(
                    self, handle_sel=handle_sel,
                    puzzle_sel=puzzle_sel, verbose=True)}

    # ---------- vision ("mata" buat agent text-only) ----------
    def _vision_engine_or_fail(self):
        if self._engine_name not in ("webview", "playwright"):
            return fail("vision needs the webview or playwright engine "
                        "(screenshot + evaluate)")
        return None

    def _vision_result(self, r):
        """Normalize vision.py plain dicts into the error-envelope format."""
        if r.get("success"):
            return self._wrap(r)
        extra = {k: v for k, v in r.items()
                 if k not in ("success", "error", "errors")}
        code = r.get("code") or self._code_for("vision",
                                               r.get("error", ""))
        return self._wrap(fail(r.get("error") or "vision failed",
                               code=code, **extra))

    def vision_locate(self, description, backend=None, template=None,
                      multiple=False):
        """Locate a UI element by visual description; 0-1000 candidates, no selector needed."""
        err = self._vision_engine_or_fail()
        if err:
            return err
        from kancil import vision as _v
        g = _v.grab_png(self.engine)
        if not g["success"]:
            return self._vision_result(g)
        return self._vision_result(
            _v.locate(g["png"], description, backend=backend,
                      template=template, multiple=multiple))

    def vision_describe(self, question=""):
        """Describe the current screen in words for a blind operator (vision API)."""
        err = self._vision_engine_or_fail()
        if err:
            return err
        from kancil import vision as _v
        g = _v.grab_png(self.engine)
        if not g["success"]:
            return self._vision_result(g)
        return self._vision_result(_v.describe_screen(g["png"],
                                                      question=question))

    def vision_calibrate(self):
        """Lock the screenshot->CSS-px mapping; run once if taps miss."""
        err = self._vision_engine_or_fail()
        if err:
            return err
        from kancil import vision as _v
        return self._vision_result(_v.calibrate(self.engine))

    @_with_delta
    def see_tap(self, description, backend=None, template=None,
                verify=None, max_tries=3, human=False, confirm=False):
        """See the screen and tap the described target (verify + retry)."""
        err = self._vision_engine_or_fail()
        if err:
            return err
        from kancil import vision as _v
        return self._vision_result(_v.see_tap(
            self.engine, description, backend=backend, template=template,
            verify=verify, max_tries=max_tries, dry_run=self.dry_run,
            confirm=confirm, human=human))

    @_with_delta
    def see_type(self, description, text, backend=None, template=None,
                 confirm=False):
        """See an input field and type into it (no selector needed)."""
        err = self._vision_engine_or_fail()
        if err:
            return err
        from kancil import vision as _v
        return self._vision_result(_v.see_type(
            self.engine, description, text, backend=backend,
            template=template, dry_run=self.dry_run, confirm=confirm))

    @_with_delta
    def see_drag(self, from_desc, to_desc, backend=None, template=None,
                 human=True, confirm=False):
        """Drag from one visual target to another (e.g. slider handle)."""
        err = self._vision_engine_or_fail()
        if err:
            return err
        from kancil import vision as _v
        return self._vision_result(_v.see_drag(
            self.engine, from_desc, to_desc, backend=backend,
            template=template, dry_run=self.dry_run, confirm=confirm,
            human=human))

    # ---------- wait_for / session / forms / cookies / markdown (1.27) ----------
    def wait_for(self, expr, timeout_ms=10000, poll_ms=300):
        """Wait until a JS expression is truthy — polled server-side (agent 1.27+).

        e.g. wait_for("!document.querySelector('.spinner')") — spinner gone;
        wait_for("document.querySelectorAll('.item').length>=5") — count reached.
        One round trip instead of a manual polling loop."""
        fn = getattr(self.engine, "wait_for", None)
        if not fn:
            return fail("wait_for needs the webview engine (agent 1.27+)")
        return self._wrap(fn(expr, timeout_ms=timeout_ms, poll_ms=poll_ms))

    def session_export(self, path):
        """Save the login session to a JSON file: cookies (incl. HttpOnly)
        per origin + localStorage of every open tab's origin.

        Restore later (even after the app was killed) with session_import().
        Swap accounts by saving/loading different files."""
        fn = getattr(self.engine, "cookies_dump", None)
        if not fn:
            return fail("session_export needs the webview engine (agent 1.27+)")
        cd = self.engine.cookies_dump()
        if not cd.get("success"):
            return self._wrap(cd)
        data = {"kancil_session": 1,
                "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "cookies": cd.get("dumps", {}) or {},
                "storage": {}}
        try:
            tabs = self.engine.list_tabs()
        except Exception:
            tabs = []
        cur = next((t["id"] for t in tabs if t.get("current")), None)
        for t in tabs:
            url = t.get("url") or ""
            if not url.startswith("http"):
                continue
            try:
                self.engine.switch_tab(t["id"])
                st = self.engine.storage("local")
                if isinstance(st, dict) and "__error__" not in st and st:
                    data["storage"][url] = st
            except Exception:
                continue
        if cur is not None:
            try:
                self.engine.switch_tab(cur)
            except Exception:
                pass
        try:
            with open(path, "w") as f:
                json.dump(data, f)
        except Exception as e:
            return fail("cannot write %s: %s" % (path, str(e)[:120]))
        return ok(path=path, cookie_origins=len(data["cookies"]),
                  storage_origins=len(data["storage"]))

    def session_import(self, path):
        """Restore a session_export() file: cookies globally, then localStorage
        per origin (temp tab per origin: navigate, inject, reload, close).

        Login once, recover instantly — even after the app was killed."""
        fn = getattr(self.engine, "cookies_load", None)
        if not fn:
            return fail("session_import needs the webview engine (agent 1.27+)")
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception as e:
            return fail("cannot read %s: %s" % (path, str(e)[:120]))
        if not isinstance(data, dict) or data.get("kancil_session") != 1:
            return fail("not a kancil session file: %s" % path)
        out = {}
        cd = self.engine.cookies_load(data.get("cookies") or {})
        out["cookies_loaded"] = cd.get("loaded", 0) if cd.get("success") \
            else 0
        if not cd.get("success"):
            out["cookies_error"] = cd.get("errors")
        storage = data.get("storage") or {}
        injected, failed = [], []
        try:
            tabs = self.engine.list_tabs()
        except Exception:
            tabs = []
        cur = next((t["id"] for t in tabs if t.get("current")), None)
        for url, kv in storage.items():
            if not isinstance(kv, dict) or not kv:
                continue
            tmp = None
            try:
                tmp = self.engine.new_tab(url)
                tid = tmp.get("tab")
                self.engine.wait_idle(timeout=10)
                n = 0
                for k, v in kv.items():
                    sr = self.engine.storage_set(k, v, "local")
                    if isinstance(sr, dict) and sr.get("success"):
                        n += 1
                self.engine.reload()
                injected.append({"url": url, "keys": n})
            except Exception as e:
                failed.append({"url": url, "error": str(e)[:120]})
            finally:
                try:
                    if tmp and tmp.get("tab"):
                        self.engine.close_tab(tmp["tab"])
                except Exception:
                    pass
        if cur is not None:
            try:
                self.engine.switch_tab(cur)
            except Exception:
                pass
        out["storage_injected"] = injected
        if failed:
            out["storage_failed"] = failed
        return ok(**out)

    @_with_delta
    def form_fill_submit(self, form_id, values=None, verify=None,
                         verify_text=None, auto=False, confirm=False):
        """Fill a form, submit it, and assert the result — one call.

        verify: CSS selector expected after submit; verify_text: text
        instead. For farm/automation loops that shouldn't guess."""
        fill = self.form_fill(form_id, values=values, auto=auto)
        if not fill.get("success"):
            return fill
        sub = self.form_submit(form_id, confirm=confirm)
        if not sub.get("success"):
            return self._wrap({**sub, "filled": True})
        if verify is None and verify_text is None:
            return self._wrap({"success": True, "filled": True,
                               "submitted": True})
        w = self.wait(selector=verify, text=verify_text)
        if w.get("success"):
            return self._wrap({"success": True, "filled": True,
                               "submitted": True,
                               "verified": verify
                               if verify is not None else verify_text})
        errs = w.get("errors") or ["verify failed"]
        return self._wrap(fail(
            "form submit verify failed: %s" % str(errs[0])[:150],
            filled=True, submitted=True,
            verify=verify if verify is not None else verify_text))

    def cookies_export_netscape(self, path):
        """Export cookies as Netscape cookies.txt (curl/playwright interop).

        Pairs with the existing cookies_import. Domain flags are honest:
        exact host, not assumed subdomain-wide."""
        fn = getattr(self.engine, "cookies_dump", None)
        if not fn:
            return fail("cookies_export_netscape needs the webview engine "
                        "(agent 1.27+)")
        cd = self.engine.cookies_dump()
        if not cd.get("success"):
            return self._wrap(cd)
        lines = ["# Netscape HTTP Cookie File",
                 "# exported by kancil session"]
        n = 0
        for url, raw in (cd.get("dumps") or {}).items():
            host = urllib.parse.urlparse(url).hostname or ""
            if not host:
                continue
            for part in raw.split(";"):
                if "=" not in part:
                    continue
                name, val = part.split("=", 1)
                name, val = name.strip(), val.strip()
                if not name:
                    continue
                lines.append("\t".join(
                    [host, "FALSE", "/", "FALSE", "0", name, val]))
                n += 1
        try:
            with open(path, "w") as f:
                f.write("\n".join(lines) + "\n")
        except Exception as e:
            return fail("cannot write %s: %s" % (path, str(e)[:120]))
        return ok(path=path, cookies=n)

    def markdown(self, max_chars=60000):
        """Current page as clean Markdown (JS-rendered pages included).

        Zero-dependency HTML->markdown: headings, links, lists, tables,
        code — nav/header/footer/script stripped."""
        p, err = self._page_or_fail()
        if err:
            return err
        raw = getattr(p, "raw", None)
        html = raw.decode("utf-8", "replace") \
            if isinstance(raw, bytes) else (raw or "")
        if not html.strip():
            return fail("page has no HTML")
        from .markdown import html_to_markdown
        md, stats = html_to_markdown(html, max_chars=max_chars)
        if not md:
            return fail("no readable content found")
        return ok(markdown=md, **stats,
                  url=getattr(p, "url", ""))

    def network_curl(self, rid):
        """Replay a logged request as a copy-pasteable curl command."""
        import shlex
        e = self.engine.request(rid)
        if not e:
            return fail("no such request id %s" % rid)
        parts = ["curl", "-X", e.get("method", "GET"),
                 shlex.quote(e.get("url", ""))]
        for k, v in (e.get("req_headers") or {}).items():
            if k.lower() in ("content-length", "host"):
                continue
            parts += ["-H", shlex.quote("%s: %s" % (k, v))]
        post = e.get("post_data")
        if post:
            if e.get("post_truncated"):
                return fail("request body truncated in netlog",
                            hint="curl would be incomplete; use har_export")
            parts += ["--data-raw", shlex.quote(post)]
        return ok(id=rid, curl=" ".join(parts))

    def tool_batch(self, payload):
        """Run several tool actions in one call. Saves LLM roundtrips."""
        actions = payload.get("actions")
        if not isinstance(actions, list):
            return {"success": False, "error": {
                "code": "INVALID_INPUT",
                "message": "batch needs an 'actions' list of tool payloads"}}
        if len(actions) > 25:
            return {"success": False, "error": {
                "code": "INVALID_INPUT",
                "message": "batch capped at 25 actions per call"}}
        stop = bool(payload.get("stop_on_error"))
        out = []
        for i, sub in enumerate(actions):
            r = self.tool(sub)
            out.append({"index": i,
                        "action": sub.get("action") if isinstance(sub, dict) else None,
                        "result": r})
            if stop and not r.get("success"):
                break
        return {"success": True, "count": len(out), "results": out}

    # ---------- storage ----------
    def cookies(self):
        return ok(cookies=self.engine.cookies())

    def cookies_import(self, path):
        """Import cookies from a Netscape-format cookies.txt file
        (as exported by browsers / extensions).

        Practical anti-bot bridge: solve a challenge once in your real
        browser, export its cookies, and the static engine inherits the
        trusted session.
        """
        import http.cookiejar
        mcj = http.cookiejar.MozillaCookieJar(path)
        try:
            mcj.load(ignore_discard=True, ignore_expires=True)
        except Exception as e:
            return fail("cannot load cookies file %r: %s" % (path, e))
        jar = self.engine.jar
        n = 0
        for c in mcj:
            try:
                jar.set_cookie(c)
                n += 1
            except Exception:
                pass
        try:
            if hasattr(jar, "filename") and jar.filename:
                jar.save(ignore_discard=True)
        except OSError:
            pass
        return ok(imported=n, file=path)

    def cookies_set(self, name, value="", domain=None, path="/", max_age=0):
        """Set one cookie (session injection). Works on static + webview."""
        fn = getattr(self.engine, "cookies_set", None)
        if not fn:
            return {"success": False,
                    "errors": ["engine %s does not support cookies_set"
                               % self.engine.name]}
        return self._wrap(fn(name, value, domain=domain, path=path,
                             max_age=max_age))

    def ua_reset(self):
        """Reset UA override (webview: active tab back to default)."""
        self.ua = None
        fn = getattr(self.engine, "reset_user_agent", None)
        if fn:
            return self._wrap(fn())
        fn2 = getattr(self.engine, "set_user_agent", None)
        if fn2:
            return self._wrap(fn2(None))
        return ok(ua=None)

    def find(self, text, next=False):
        """Find-in-page: match count (+snippets on static, highlight on webview)."""
        fn = getattr(self.engine, "find", None)
        if not fn:
            return {"success": False,
                    "errors": ["engine %s does not support find"
                               % self.engine.name]}
        return self._wrap(fn(text, next=next))

    def storage(self, kind="local"):
        data = self.engine.storage(kind) or {}
        return ok(kind=kind, origin=self.engine._origin(),
                  data=data, count=len(data),
                  real=self.capabilities["real_localstorage"],
                  note=None if self.capabilities["real_localstorage"]
                  else "simulated per-origin KV (static engine has no JS)")

    def storage_get(self, key, kind="local"):
        v = self.engine.storage_get(key, kind)
        return ok(key=key, value=v, found=v is not None)

    def storage_set(self, key, value, kind="local"):
        return self._wrap(self.engine.storage_set(key, value, kind))

    def storage_delete(self, key, kind="local"):
        return self._wrap(self.engine.storage_delete(key, kind))

    # ---------- scraper ----------
    def scrape(self, url=None, selector=None, fields=None, auto=False,
               fmt="json", pages=1, next_selector=None, delay=1.0,
               max_items=1000, timeout=180, same_content_limit=3,
               scroll_pages=0, respect_robots=True, sitemap=False,
               workers=1):
        """fields: {name: 'css' | 'css@attr'}.

        pages>1: paginated crawl (next_selector, numbered ?page=N, or
        template increment) with duplicate detection, robots.txt, and
        hard limits. scroll_pages>0: infinite scroll (playwright only).

        sitemap=True: discover page URLs from sitemap.xml instead of
        following next-page links (full coverage, no guessing).
        workers=N: fetch a sitemap URL list with N concurrent workers
        (static engine only, capped at 8).
        """
        from .devtools import paginate_scrape
        eng = self.engine
        if not (selector and fields) and not auto:
            return fail("need selector+fields or auto=True")
        start = url
        if not start:
            p, err = self._page_or_fail()
            if err:
                return err
            start = p.url if hasattr(p, "url") else None
            if not start:
                return fail("no url")
        if scroll_pages and not eng.capabilities.get("javascript"):
            return fail("infinite scroll needs the playwright engine",
                        supported=False)
        res = None
        url_list = None
        if sitemap:
            from .devtools import fetch_sitemap_urls
            sm = fetch_sitemap_urls(start, max_urls=max(100, max_items * 2),
                                    respect_robots=respect_robots)
            url_list = sm["urls"][:max(1, pages)]
            if not url_list:
                return fail("no URLs found in sitemap (checked: %s)" %
                            ", ".join(sm["sitemaps"][:3]) or "none")
        if workers and int(workers) > 1 and not url_list:
            return fail("workers=N needs sitemap=True (a known URL list); "
                        "plain pagination is sequential by nature")
        if url_list and workers and int(workers) > 1:
            from .devtools import scrape_url_list
            from .engines import StaticEngine
            base = eng
            if getattr(base, "capabilities", {}).get("javascript"):
                return fail("workers need the static engine "
                            "(playwright can't fan out browsers)",
                            supported=False)

            def factory(base=base):
                return StaticEngine(user_agent=base.ua, timeout=base.timeout,
                                    retries=0, proxy=base.proxy,
                                    cache=base.cache_enabled)
            res = scrape_url_list(
                url_list, factory, selector=selector, fields=fields,
                auto=auto, max_items=max_items, workers=workers,
                delay=delay, timeout=timeout, respect_robots=respect_robots)
        else:
            res = paginate_scrape(
                eng, start, selector=selector, fields=fields, auto=auto,
                max_pages=max(1, pages), max_items=max_items,
                next_selector=next_selector, delay=delay, timeout=timeout,
                same_content_limit=same_content_limit,
                scroll_pages=scroll_pages, respect_robots=respect_robots,
                url_list=url_list)
        data = res["data"]
        if fmt == "csv":
            import csv, io
            if not data:
                return ok(format="csv", csv="", pages_crawled=res["pages_crawled"],
                          items=0, duplicates=res["duplicates"],
                          failed_pages=res["failed_pages"],
                          stopped_reason=res["stopped_reason"])
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=list(data[0].keys()))
            w.writeheader()
            w.writerows(data)
            return ok(format="csv", csv=buf.getvalue(),
                      pages_crawled=res["pages_crawled"], items=len(data),
                      duplicates=res["duplicates"], failed_pages=res["failed_pages"],
                      stopped_reason=res["stopped_reason"])
        return ok(format="json", pages_crawled=res["pages_crawled"],
                  items=len(data), count=len(data),  # count = legacy alias
                  duplicates=res["duplicates"],
                  failed_pages=res["failed_pages"],
                  stopped_reason=res["stopped_reason"], data=data)

    # ---------- extract ----------
    def extract(self, mode="auto"):
        p, err = self._page_or_fail()
        if err:
            return err
        if not p.dom:
            return fail("page has no DOM")
        data = extract_reader(p)
        if mode == "article":
            return ok(mode=mode, article={"title": data["title"],
                                          "text": data["article"][:20000]})
        if mode == "links":
            return ok(mode=mode, links=data["links"])
        if mode == "images":
            return ok(mode=mode, images=data["images"])
        if mode == "tables":
            return ok(mode=mode, tables=data["tables"])
        return ok(mode="auto", **{k: v for k, v in data.items()
                                  if k != "html"})

    # ---------- view (local viewer: see + take over) ----------
    def view(self, port=0, host="127.0.0.1"):
        """Start the local viewer. Open the returned URL in your browser:
        static engine -> the live page through a gateway (clicks navigate
        via Kancil); playwright engine -> live screenshots, clicks forwarded
        as real mouse clicks into Chromium."""
        from .viewer import ViewerServer
        old = getattr(self, "_viewer", None)
        if old is not None and old.running:
            return ok(url="http://%s:%d/" % (old.host, old.port),
                      engine=self.engine.name, note="already running")
        srv = ViewerServer(self, host=host, port=int(port or 0))
        url = srv.start()
        self._viewer = srv
        mode = ("live screenshots + click/keyboard takeover"
                if self.engine.name == "playwright"
                else "page through local gateway (clicks navigate via Kancil)")
        return ok(url=url, engine=self.engine.name, mode=mode,
                  note="viewer lives while this process runs; "
                       "binds 127.0.0.1 only")

    def view_stop(self):
        srv = getattr(self, "_viewer", None)
        if srv is not None and srv.running:
            srv.stop()
            return ok(stopped=True)
        return ok(stopped=False, note="viewer not running")

    # ---------- agent: drive the real rendered page (level 1) ----------
    def agent_tabs(self):
        """Live tabs running the injected agent.js (real rendered pages)."""
        from .agent_bridge import get_bridge
        return ok(tabs=get_bridge().tabs())

    def agent_cmd(self, tab, action, args=None, timeout=30):
        """Send a command to a live tab: click/type/scroll/eval/snapshot/text/console.

        Example: agent_cmd(tab="tab-abc", action="click",
                           args={"selector": "a[href='/login']"})
        """
        from .agent_bridge import get_bridge
        try:
            timeout = int(timeout)
        except Exception:
            timeout = 30
        r = get_bridge().send(tab, action, args or {}, timeout=timeout)
        return r if r.get("success") else fail(
            "; ".join(r.get("errors", ["agent failed"])))

    def agent_snapshot(self, tab, timeout=30):
        """DOM snapshot of the real rendered page (post-JS)."""
        return self.agent_cmd(tab, "snapshot", {}, timeout=timeout)

    def yt_play(self, target, port=0, host="127.0.0.1"):
        """Mini YouTube player. target = watch URL, video ID, or a search
        query (first result is played). Returns the player page URL to open
        in your browser — official YouTube embed, audio+video+fullscreen."""
        from .viewer import _extract_video_id
        vid = _extract_video_id(target)
        title = ""
        if not vid:
            # treat as search query -> first result
            r = self.yt_search(target, max_results=1)
            if not r.get("success") or not r.get("videos"):
                return fail("no video found for %r" % target)
            vid = r["videos"][0]["videoId"]
            title = r["videos"][0].get("title", "")
        r = self.view(port=port, host=host)
        if not r.get("success"):
            return r
        play_url = (r["url"].rstrip("/") + "/__kancil__/play?v=" + vid)
        return ok(play_url=play_url, video_id=vid, title=title,
                  viewer=r["url"], engine=self.engine.name,
                  note="open play_url in your browser")

    # ---------- embedded JSON / youtube ----------
    def page_json(self):
        """Extract JSON blobs embedded in the current page's raw HTML
        (ytInitialData, __NEXT_DATA__, ld+json, ...)."""
        from .devtools import extract_embedded_json, _decode_html_text
        p, err = self._page_or_fail()
        if err:
            return err
        raw = getattr(p, "raw", None)
        if not raw:
            return fail("page has no raw HTML (content-type: %s)" %
                        getattr(p, "ctype", "?"))
        blobs = extract_embedded_json(raw)
        return ok(url=getattr(p, "url", ""), count=len(blobs),
                  sources=[{"source": b["source"],
                            "keys": list(b["data"].keys())[:20]
                            if isinstance(b["data"], dict) else "list"}
                           for b in blobs])

    def structured(self):
        """Extract machine-readable structured data from the current page:
        JSON-LD blocks, OpenGraph tags, Twitter Card tags, basic meta.
        Often cleaner than scraping visible text (no selector guessing)."""
        from .devtools import extract_structured
        p, err = self._page_or_fail()
        if err:
            return err
        if not getattr(p, "dom", None):
            return fail("page has no DOM (content-type: %s)" %
                        getattr(p, "ctype", "?"))
        data = extract_structured(p.dom, getattr(p, "raw", None))
        return ok(url=getattr(p, "url", ""), json_ld=data["json_ld"],
                  opengraph=data["opengraph"], twitter=data["twitter"],
                  meta=data["meta"],
                  json_ld_blocks=len(data["json_ld"]))

    def sitemap(self, url=None, max_urls=5000):
        """Discover page URLs from the site's sitemap.xml (via robots.txt
        Sitemap: lines, then /sitemap.xml fallback). Handles sitemapindex
        recursion and .xml.gz."""
        from .devtools import fetch_sitemap_urls
        start = url
        if not start:
            p, err = self._page_or_fail()
            if err:
                return err
            start = p.url if hasattr(p, "url") else None
            if not start:
                return fail("no url")
        res = fetch_sitemap_urls(start, max_urls=max_urls)
        return ok(url=start, count=len(res["urls"]), urls=res["urls"],
                  sitemaps=res["sitemaps"], truncated=res["truncated"])

    def http_cache(self, action="stats"):
        """Inspect/clear the static engine's HTTP cache (ETag/Last-Modified)."""
        from . import httpcache
        if action == "clear":
            return ok(cleared=httpcache.clear())
        n, size = httpcache.stats()
        return ok(files=n, bytes=size,
                  enabled=bool(getattr(self.engine, "cache_enabled", False)))

    def yt_search(self, query, max_results=20):
        """YouTube search via ytInitialData (no JS needed).

        Uses a desktop User-Agent: the mobile layout lazy-loads results via
        continuation, while desktop embeds videoRenderer items inline.
        """
        from .devtools import extract_yt_search, _decode_html_text
        import urllib.parse as up
        if not query:
            return fail("yt_search needs a query")
        url = ("https://www.youtube.com/results?search_query=" +
               up.quote_plus(query))
        eng = self.engine
        old_ua, swapped = getattr(eng, "ua", None), False
        if eng.name == "static" and hasattr(eng, "set_user_agent"):
            try:
                eng.set_user_agent(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0 Safari/537.36")
                swapped = True
            except Exception:
                pass
        try:
            r = eng.open(url)
        finally:
            if swapped:
                try:
                    eng.set_user_agent(old_ua)
                except Exception:
                    pass
        if not r.get("success"):
            return self._wrap(r)
        p = eng.page
        raw = getattr(p, "raw", b"")
        videos = extract_yt_search(_decode_html_text(raw))[:max_results]
        return ok(query=query, count=len(videos), videos=videos)

    def yt_video(self, url):
        """YouTube watch-page metadata via og: tags (no JS needed)."""
        from .devtools import extract_yt_video, _decode_html_text
        r = self.engine.open(url)
        if not r.get("success"):
            return self._wrap(r)
        p = self.engine.page
        meta = extract_yt_video(_decode_html_text(getattr(p, "raw", b"")))
        meta.setdefault("url", url)
        return ok(**meta)
        p, err = self._page_or_fail()
        if err:
            return err
        if not p.dom:
            return fail("page has no DOM")
        data = extract_reader(p)
        if mode == "article":
            return ok(title=data["title"], url=data["url"], article=data["article"])
        if mode == "links":
            return ok(title=data["title"], links=data["links"])
        if mode == "images":
            return ok(title=data["title"], images=data["images"])
        if mode == "tables":
            return ok(title=data["title"], tables=data["tables"])
        return ok(**data)

    # ---------- forms ----------
    def forms(self):
        return ok(forms=self.engine.forms())

    @_with_delta
    def form_fill(self, form_id, values=None, auto=False):
        return self._wrap(self.engine.fill_form(form_id - 1, values, auto, self.profile))

    @_with_delta
    def form_submit(self, form_id, confirm=False):
        if self._engine_name == "webview":
            return self._wrap(
                self.engine.submit_form(form_id - 1, confirm=confirm))
        return self._wrap(self.engine.submit_form(form_id - 1))

    # ---------- downloads ----------
    def download(self, url, path=None, confirm=False):
        if self._engine_name == "webview":
            return self._wrap(
                self.engine.download(url, path, confirm=confirm))
        return self._wrap(self.engine.download(url, path))

    def downloads(self):
        return ok(downloads=self.engine.download_list())

    def download_pause(self, did):
        return self._wrap(self.engine.download_pause(did))

    def download_resume(self, did):
        return self._wrap(self.engine.download_resume(did))

    def videos(self):
        fn = getattr(self.engine, "videos", None)
        if not fn:
            return fail("video listing needs the webview engine",
                        supported=False)
        return self._wrap(fn())

    def upload(self, path, confirm=False):
        fn = getattr(self.engine, "upload", None)
        if not fn:
            return fail("agent upload needs the webview engine",
                        supported=False)
        # webview-only feature; engine here is always WebViewEngine
        return self._wrap(fn(path, confirm=confirm))

    # ---------- perf ----------
    def perf(self, url=None):
        return self._wrap(self.engine.perf(url))

    # ---------- pdf ----------
    def pdf(self, path=None):
        fn = getattr(self.engine, "pdf", None)
        if not fn:
            return fail("PDF export needs the playwright engine",
                        supported=False)
        return self._wrap(fn(path))

    # ---------- bookmarks ----------
    def bookmark_add(self, url=None, title=None):
        p = self.engine.page
        url = url or (p.url if p and hasattr(p, "url") else None)
        if not url:
            return fail("no url (open a page or pass one)")
        if isinstance(url, str) and not url.startswith("http"):
            url = "https://" + url
        title = title or (p.title if p and hasattr(p, "title") else url)[:80]
        self._bm_id += 1
        self.bookmarks.append({"id": self._bm_id, "title": title[:80],
                               "url": url, "added": time.strftime("%Y-%m-%d %H:%M")})
        return ok(id=self._bm_id, title=title[:80], url=url)

    def bookmark_list(self):
        return ok(count=len(self.bookmarks), bookmarks=self.bookmarks)

    def bookmark_open(self, bid):
        for b in self.bookmarks:
            if b["id"] == bid:
                return self.open(b["url"])
        return fail("no such bookmark %s" % bid)

    def bookmark_delete(self, bid):
        before = len(self.bookmarks)
        self.bookmarks = [b for b in self.bookmarks if b["id"] != bid]
        if len(self.bookmarks) == before:
            return fail("no such bookmark %s" % bid)
        return ok(deleted=bid)

    # ---------- profile ----------
    def profile_set(self, key, value):
        self.profile[key] = value
        return ok(key=key)

    def profile_get(self, key):
        v = self.profile.get(key)
        return ok(key=key, value=v, found=v is not None)

    def profile_list(self):
        return ok(profile={k: (v[:20] + "…" if k == "password" and len(v) > 20 else v)
                           for k, v in self.profile.items()})

    def profile_delete(self, key):
        self.profile.pop(key, None)
        return ok(deleted=key)

    # ---------- proxy ----------
    def proxy_set(self, url):
        """Persistent proxy. Note: playwright engine needs restart to apply."""
        self.proxy = url or None
        r = self.engine.set_proxy(self.proxy) if hasattr(self.engine, "set_proxy") else None
        if self._engine_name == "playwright":
            return {"success": False,
                    "errors": ["playwright applies proxy at start; "
                               "re-run with --proxy %s" % url],
                    "proxy": self.proxy}
        return self._wrap(r) if r else ok(proxy=self.proxy)

    def proxy_show(self):
        return ok(proxy=self.proxy)

    def proxy_clear(self):
        return self.proxy_set(None)

    # ---------- user agent ----------
    def ua_set(self, ua):
        self.ua = ua
        if hasattr(self.engine, "set_user_agent"):
            return self._wrap(self.engine.set_user_agent(ua))
        return ok(ua=(ua or "")[:60])

    def ua_show(self):
        ua = self.ua or getattr(self.engine, "ua", "")
        return ok(ua=ua)

    def ua_rotate(self):
        from .engines import UA_LIST
        cur = self.ua or getattr(self.engine, "ua", "")
        try:
            i = UA_LIST.index(cur)
        except ValueError:
            i = -1
        return self.ua_set(UA_LIST[(i + 1) % len(UA_LIST)])

    def ua_list(self):
        from .engines import UA_LIST
        return ok(user_agents=UA_LIST)

    # ---------- stealth (browser impersonation) ----------
    def stealth_status(self):
        from . import stealth as _st
        eng = self.engine
        return ok(engine=eng.name,
                  curl_cffi=_st.HAVE_CURL_CFFI,
                  profiles=_st.list_profiles(),
                  impersonate=getattr(eng, "impersonate", None),
                  impersonate_active=isinstance(
                      getattr(eng, "opener", None), _st.ImpersonatedOpener))

    def stealth_impersonate(self, profile):
        """profile: key of stealth.BROWSER_PROFILES, or "off" to disable.
        Static engine only; needs curl_cffi for the impersonated transport."""
        if profile == "off":
            profile = None
        fn = getattr(self.engine, "set_impersonate", None)
        if not fn:
            return {"success": False,
                    "errors": ["engine %s does not support impersonation "
                               "(static engine only)" % self.engine.name]}
        self.impersonate = profile
        return self._wrap(fn(profile))

    def stealth_apply(self):
        """Inject the anti-detect JS snippet (webview engine). Re-apply
        after every navigation."""
        from . import stealth as _st
        return self._wrap(_st.apply_stealth(self.engine))

    def ensure_alive(self, retries=2, relaunch=True):
        """Check the agent is reachable; relaunch the app if dead
        (webview engine only)."""
        fn = getattr(self.engine, "ensure_alive", None)
        if not fn:
            return {"success": False,
                    "errors": ["engine %s has no app to relaunch"
                               % getattr(self.engine, "name", "?")]}
        return self._wrap(fn(retries=retries, relaunch=relaunch))

    # backward-compat alias (was the auto-launch entry point)
    def launch_app(self, timeout=20):
        return self.ensure_alive()

    # ---------- session ----------
    def session_clear(self, what="all", domain=None):
        """Wipe current session state (cookies, tabs, netlog, cache —
        per-engine support varies; see engine.clear_session).
        domain limits cookie clearing to one site."""
        fn = getattr(self.engine, "clear_session", None)
        if not fn:
            return {"success": False,
                    "errors": ["engine %s does not support session clear"
                               % self.engine.name]}
        try:
            return self._wrap(fn(what, domain=domain))
        except TypeError:
            return self._wrap(fn(what))

    # ---------- request blocking (playwright, webview) ----------
    def block_add(self, pattern):
        fn = getattr(self.engine, "block_add", None)
        if not fn:
            return fail("request blocking needs the playwright or webview engine",
                        supported=False)
        return self._wrap(fn(pattern))

    def block_list(self):
        fn = getattr(self.engine, "block_list", None)
        if not fn:
            return fail("request blocking needs the playwright or webview engine",
                        supported=False)
        return ok(patterns=fn())

    def block_clear(self):
        fn = getattr(self.engine, "block_clear", None)
        if not fn:
            return fail("request blocking needs the playwright or webview engine",
                        supported=False)
        return self._wrap(fn())

    # ---------- HAR export ----------
    # ---------- HAR ----------
    # Recording reuses the network log; har start = clear + mark, export
    # serializes to valid HAR 1.2 with safe-by-default redaction.
    def _sync_netlog(self):
        # WebView keeps its log in the APK; pull it before serializing so
        # `har export` never writes an empty file after a live session.
        # Static/playwright engines keep theirs in-process (no-op refresh).
        try:
            self.engine.network()
        except Exception:
            pass

    def har_start(self):
        self.engine.network_clear()
        self._har_recording = True
        self._har_started = time.time()
        return ok(recording=True)

    def har_stop(self):
        self._har_recording = False
        self._sync_netlog()
        return ok(recording=False, entries=len(self.engine.netlog))

    def har_clear(self):
        self.engine.network_clear()
        self._har_recording = False
        return ok(cleared=True)

    def har_stats(self):
        self._sync_netlog()
        n = len(self.engine.netlog)
        failed = sum(1 for e in self.engine.netlog
                     if isinstance(e.get("status"), int) and e["status"] >= 400)
        by_type = {}
        for e in self.engine.netlog:
            by_type[e.get("resource_type", "?")] = by_type.get(e.get("resource_type", "?"), 0) + 1
        total = sum(e.get("response_size", e.get("size", 0)) or 0 for e in self.engine.netlog)
        return ok(recording=getattr(self, "_har_recording", False),
                  entries=n, failed=failed, by_type=by_type,
                  total_bytes=total)

    @staticmethod
    def _redact_headers(headers, redact_cookie=True, redact_auth=True,
                        redact_token=True):
        out = {}
        for k, v in (headers or {}).items():
            kl = k.lower()
            if redact_cookie and kl == "cookie":
                out[k] = "***"
            elif redact_cookie and kl == "set-cookie":
                out[k] = "***"
            elif redact_auth and kl == "authorization":
                out[k] = "***"
            elif redact_token and "token" in kl:
                out[k] = "***"
            else:
                out[k] = v
        return out

    def har(self, path="network.har", redact=True):
        """One-shot HAR export (also used by `har export`). Redaction ON by default."""
        return self.har_export(path, redact=redact)

    def har_export(self, path="network.har", redact=True,
                   redact_cookie=True, redact_authorization=True, redact_token=True):
        """Export netlog as HAR. Honest limitation: the webview engine's
        netlog records request (method/url/timing/headers) but NOT response
        status — webview HAR entries always show status 0. Static/playwright
        engines record real statuses."""
        import datetime
        import urllib.parse as up
        if not redact:
            redact_cookie = redact_authorization = redact_token = False
        self._sync_netlog()
        entries = []
        for e in self.engine.netlog:
            started = e.get("started") or ("%sT%s" % (
                datetime.date.today().isoformat(), e.get("t", "00:00:00")))
            u = up.urlparse(e.get("url", ""))
            qs = [{"name": k, "value": v} for k, v in up.parse_qsl(u.query)]
            req_hdrs = self._redact_headers(e.get("req_headers"), redact_cookie,
                                            redact_authorization, redact_token)
            res_hdrs = self._redact_headers(e.get("res_headers"), redact_cookie,
                                            redact_authorization, redact_token)
            post = None
            if e.get("post_data"):
                post = {"mimeType": "application/x-www-form-urlencoded",
                        "text": e["post_data"][:8192]}
            req_cookies = [{"name": k, "value": ("***" if redact_cookie else v)}
                           for k, v in (e.get("cookies_sent") or {}).items()]
            res_cookies = []
            for c in e.get("cookies_set") or []:
                res_cookies.append({
                    "name": c.get("name", ""),
                    "value": "***" if redact_cookie else c.get("value", ""),
                    "path": c.get("path", ""), "domain": c.get("domain", ""),
                    "expires": c.get("expires", ""),
                    "httpOnly": bool(c.get("httponly")),
                    "secure": bool(c.get("secure"))})
            entries.append({
                "startedDateTime": started,
                "time": e.get("ms", 0),
                "request": {
                    "method": e.get("method"), "url": e.get("url"),
                    "httpVersion": "HTTP/1.1", "cookies": req_cookies,
                    "headers": [{"name": k, "value": str(v)} for k, v in req_hdrs.items()],
                    "queryString": qs, "headersSize": -1,
                    "bodySize": e.get("request_size", 0),
                    **({"postData": post} if post else {}),
                },
                "response": {
                    "status": e.get("status") if isinstance(e.get("status"), int) else 0,
                    "statusText": "", "httpVersion": "HTTP/1.1", "cookies": res_cookies,
                    "headers": [{"name": k, "value": str(v)} for k, v in res_hdrs.items()],
                    "content": {"size": e.get("response_size", e.get("size", 0)),
                                "mimeType": e.get("ctype") or e.get("mime", "")},
                    "redirectURL": "", "headersSize": -1,
                    "bodySize": e.get("response_size", e.get("size", 0)),
                },
                "cache": {},
                "timings": {"send": 0, "wait": e.get("ms", 0), "receive": 0},
            })
        har = {"log": {
            "version": "1.2",
            "creator": {"name": "kancil", "version": "3.1"},
            "pages": [{"startedDateTime": entries[0]["startedDateTime"] if entries else
                       datetime.datetime.now().isoformat(timespec="seconds"),
                       "id": "page_1", "title": "kancil capture",
                       "pageTimings": {}}],
            "entries": entries,
        }}
        with open(path, "w") as f:
            json.dump(har, f, indent=1)
        return ok(path=path, entries=len(entries), redacted=redact)

    # ---------- sessions ----------
    def session_save(self, name):
        if not name:
            return fail("session save needs a name")
        if self._engine_name == "playwright":
            import json, os
            d = os.path.join(session_mod.SESSIONS_DIR, name)
            os.makedirs(d, exist_ok=True)
            sp = session_mod.pw_state_path(name)
            r = self.engine.save_storage_state(sp)
            if not r.get("success"):
                return self._wrap(r)
            meta = self.engine.session_meta()
            prev = session_mod.session_info(name) or {}
            meta["created"] = prev.get("created") or time.strftime("%Y-%m-%d %H:%M:%S")
            meta["name"] = name
            meta["engine"] = "playwright"
            meta["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(session_mod.meta_path(name), "w") as f:
                json.dump(meta, f, indent=1)
            session_mod._secure(session_mod.meta_path(name))
            return ok(session=name, engine="playwright",
                      cookies=meta.get("cookies_count"), tabs=meta.get("tabs_count"))
        st = self.export_state()
        st["session"] = name
        session_mod.save_named(name, st)
        session_mod.save_state(st)
        return ok(session=name, engine="static")

    def session_load(self, name):
        if not name:
            return fail("session load needs a name")
        if self._engine_name == "playwright":
            state, meta = session_mod.load_pw_session(name)
            if state is None and not meta:
                return fail("no such session %r" % name, code="SESSION_NOT_FOUND")
            self.pw_session = name
            return self._wrap(self.engine.load_storage_state(state))
        try:
            st = session_mod.load_named(name)
        except OSError:
            return fail("no such session %r" % name, code="SESSION_NOT_FOUND")
        self.import_state(st)
        return ok(session=name, tabs=len(self.engine.tabs))

    def session_delete(self, name):
        if not name:
            return fail("session delete needs a name")
        if session_mod.delete_named(name):
            return ok(deleted=name)
        return fail("no such session %r" % name, code="SESSION_NOT_FOUND")

    def session_info(self, name):
        if not name:
            return fail("session info needs a name")
        info = session_mod.session_info(name)
        if not info:
            return fail("no such session %r" % name, code="SESSION_NOT_FOUND")
        return ok(**info)

    def session_list(self):
        return ok(sessions=session_mod.list_named())

    # ---------- state (for CLI persistence) ----------
    def export_state(self):
        tabs = []
        for t in self.engine.tabs:
            urls = [h.url if hasattr(h, "url") else h for h in t["history"]][:12]
            tabs.append({"history": urls, "pos": t["pos"]})
        # Persist netlog as metadata only (no bodies/headers): a long session
        # would otherwise bloat state.json into tens of MB. Full entries
        # (incl. res_body) stay in-memory for `network response <id>`.
        _keep = ("id", "t", "started", "method", "url", "status", "ctype",
                 "size", "ms", "resource_type", "request_size",
                 "response_size", "timing", "attempt", "attempts")
        netlog = []
        for e in self.engine.netlog[-200:]:
            netlog.append({k: e.get(k) for k in _keep if k in e})
        block = []
        if hasattr(self.engine, "block_list"):
            try:
                block = self.engine.block_list()
            except Exception:
                pass
        return {"engine": self._engine_name, "tabs": tabs, "cur": self.engine.cur,
                "profile": self.profile, "netlog": netlog,
                "bookmarks": self.bookmarks, "bm_id": self._bm_id,
                "proxy": self.proxy, "ua": self.ua, "block": block,
                "impersonate": self.impersonate,
                "a11y_refs": getattr(self.engine, "_a11y_css", {}),
                "a11y_sig": getattr(self.engine, "_a11y_sig", None),
                "storage": {"local": self.engine._ls, "session": self.engine._ss}
                if hasattr(self.engine, "_ls") else {}}

    def import_state(self, st):
        # rebuild tabs by replaying history urls
        eng = self.engine
        # close existing playwright pages to avoid leaking them
        if eng.name == "playwright":
            for t in eng.tabs:
                try:
                    t["pw_page"].close()
                except Exception:
                    pass
        # restore recent network log BEFORE replay (replay appends fresh entries)
        try:
            eng.netlog = list(st.get("netlog", []))
            eng._req_id = max([e.get("id", 0) for e in eng.netlog] + [0])
        except Exception:
            pass
        eng.tabs = []
        eng.cur = 0
        stored = st.get("tabs", []) or [{"history": [], "pos": -1}]
        if eng.name == "static":
            # LAZY restore: keep URL strings, fetch only the active tab now.
            # Other tabs materialize on switch_tab / back / forward.
            for t in stored:
                eng.tabs.append({"history": list(t.get("history", [])),
                                 "pos": t.get("pos", -1)})
            eng.cur = min(st.get("cur", 0), len(eng.tabs) - 1)
            # NOTE: no eager materialization here. The `page` property
            # materializes lazy URL strings on first access, so read-only
            # commands (tabs, network, session...) never trigger a fetch
            # and never pollute the netlog. (P0-4)
        else:
            for _ in stored:
                eng.new_tab()
            for i, t in enumerate(stored):
                eng.cur = i
                for u in t.get("history", [])[:12]:
                    try:
                        eng.open(u)
                    except Exception:
                        pass
            eng.cur = min(st.get("cur", 0), len(eng.tabs) - 1)
        self.profile = st.get("profile", {})
        self.bookmarks = st.get("bookmarks", [])
        self._bm_id = st.get("bm_id", len(self.bookmarks))
        # proxy & UA
        self.proxy = st.get("proxy")
        if self.proxy and hasattr(eng, "set_proxy") and eng.name != "playwright":
            eng.set_proxy(self.proxy)
        self.ua = st.get("ua")
        if self.ua and hasattr(eng, "set_user_agent"):
            try:
                eng.set_user_agent(self.ua)
            except Exception:
                pass
        # request blocking (playwright only)
        if hasattr(eng, "block_add"):
            for pat in st.get("block", []):
                try:
                    eng.block_add(pat)
                except Exception:
                    pass
        if hasattr(eng, "_ls") and st.get("storage"):
            eng._ls = st["storage"].get("local", {})
            eng._ss = st["storage"].get("session", {})
        # a11y refs: park the css map; actual restore is deferred until a
        # ref is first used (see resolve_a11y_ref in engines.py), so that
        # read-only commands never trigger a page fetch just for refs.
        eng._a11y_refs, eng._a11y_sig, eng._a11y_css = {}, None, {}
        eng._a11y_pending = (st.get("a11y_refs") or {}, st.get("a11y_sig"))

    def close(self):
        self.engine.close()

    # ---------- internal ----------
    def _wrap(self, r):
        if isinstance(r, dict) and "success" in r:
            if not r["success"] and "error" not in r:
                errs = r.get("errors") or ["unknown error"]
                code = r.get("code") or self._code_for(None, errs[0])
                r["error"] = self._envelope(code, errs[0])
            return r
        return ok(**(r or {}))

    def to_json(self, result):
        return json.dumps(result, indent=1, ensure_ascii=False, default=str)

    # ================= AGENT TOOL INTERFACE =================
    # Single structured entry point: tool({"action": "click", "selector": "#x"})
    # -> {"success": true, "action": "click", ...}
    # -> {"success": false, "error": {"code": "ELEMENT_NOT_FOUND", "message": ...}}

    @staticmethod
    def _code_for(action, message):
        m = (message or "").lower()
        if "no such tab" in m or "cannot close last tab" in m:
            return "TAB_NOT_FOUND"
        if ("no such element" in m or "no element matches" in m
                or "no such reference" in m or "invalidated" in m
                or "element is not actionable" in m):
            return "ELEMENT_NOT_FOUND"
        if "timeout" in m or "timed out" in m:
            return "TIMEOUT"
        if "no such session" in m:
            return "SESSION_NOT_FOUND"
        if "dry-run" in m:
            return "DRY_RUN_BLOCKED"
        if "needs the playwright engine" in m or "not installed" in m \
                or "launch failed" in m:
            return "ENGINE_UNAVAILABLE"
        if action == "open" and ("no url" in m or "invalid" in m):
            return "INVALID_URL"
        if "no page" in m or "no url" in m:
            return "NAVIGATION_FAILED"
        if "http " in m and "http" in m[:6]:
            return "NAVIGATION_FAILED"
        if "no such request" in m or "no such bookmark" in m \
                or "no such download" in m or "needs an id" in m:
            return "INVALID_INPUT"
        if action in ("evaluate",) or "js_error" in m:
            return "JS_ERROR"
        if action and action.startswith("scrape"):
            return "SCRAPE_FAILED"
        if action and action.startswith("download"):
            return "DOWNLOAD_FAILED"
        if ("connection" in m or "dns" in m or "network" in m
                or "name resolution" in m):
            return "NETWORK_ERROR"
        return "UNKNOWN_ERROR"

    # Hermes #2: branch-able errors — every failure carries
    # {code, message, retriable, hint}, not just free text.
    _ERROR_RETRIABLE = {
        "TIMEOUT": True, "NETWORK_ERROR": True, "NAVIGATION_FAILED": True,
        "ELEMENT_NOT_FOUND": True, "DOWNLOAD_FAILED": True,
        "UPLOAD_FAILED": True, "SCRAPE_FAILED": True,
        "SESSION_NOT_FOUND": True, "RATE_LIMITED": True,
        "CAPTCHA_REQUIRED": True, "TAB_NOT_FOUND": False,
        "INVALID_INPUT": False, "INVALID_URL": False,
        "UNKNOWN_ACTION": False, "ENGINE_UNAVAILABLE": False,
        "DRY_RUN_BLOCKED": False, "JS_ERROR": False,
        "INTERNAL_ERROR": False, "UNKNOWN_ERROR": False,
    }
    _ERROR_HINTS = {
        "ELEMENT_NOT_FOUND": "re-run snapshot()/a11y() for fresh refs, "
            "then retry; prefer semantic a11y_find over raw selectors",
        "TIMEOUT": "retry once; bump wait ms if the page is slow",
        "NETWORK_ERROR": "retry with backoff; check proxy/network",
        "NAVIGATION_FAILED": "verify the URL; retry once on flaky networks",
        "DRY_RUN_BLOCKED": "pass confirm=True to actuate (dry-run guard)",
        "ENGINE_UNAVAILABLE": "install/enable the engine or switch engine",
        "INVALID_INPUT": "check tool_schema() for required params",
        "UNKNOWN_ACTION": "list actions via tool({'action':'capabilities'}) "
            "or kancil agent-info",
        "TAB_NOT_FOUND": "list tabs() and use a live id",
        "SESSION_NOT_FOUND": "reload the saved session, then retry",
        "RATE_LIMITED": "back off and retry after Retry-After",
        "CAPTCHA_REQUIRED": "solve/refresh the challenge, then retry",
        "JS_ERROR": "simplify the expression; check the page context",
        "DOWNLOAD_FAILED": "verify the URL is downloadable; retry once",
        "UPLOAD_FAILED": "verify the file exists and is <=5MB",
        "SCRAPE_FAILED": "retry; try the webview engine for JS pages",
    }

    @classmethod
    def _envelope(cls, code, message, **extra):
        e = {"code": code, "message": message,
             "retriable": cls._ERROR_RETRIABLE.get(code, False)}
        hint = cls._ERROR_HINTS.get(code)
        if hint:
            e["hint"] = hint
        e.update(extra)
        return e

    # Hermes #3: hand-verified return shapes per action (manifest).
    _TOOL_RETURNS = {
        "open": "{url, title, tab, delta?}",
        "back": "{url, delta?}", "forward": "{url, delta?}",
        "reload": "{url, delta?}", "history": "{history[], pos}",
        "tabs": "{tabs[{id,url,title}]}", "new_tab": "{tab}",
        "switch_tab": "{tab, url}", "close_tab": "{closed, tabs}",
        "inspect": "{selector, found, html?, text?}",
        "elements": "{elements[{ref,tag,text}]}",
        "a11y": "{tree, refs}",
        "a11y_find": "{ref, role, name} | not-found",
        "dom_tree": "{nodes[]}", "dom_find": "{matches[]}",
        "dom_xpath": "{matches[]}",
        "click": "{clicked, delta?, verify?}",
        "type": "{typed, filled?, delta?}", "clear": "{cleared, delta?}",
        "select": "{selected, delta?}", "check": "{checked, delta?}",
        "uncheck": "{unchecked, delta?}", "hover": "{hovered, delta?}",
        "focus": "{focused, delta?}", "scroll": "{scrolled, delta?}",
        "evaluate": "{result}", "wait": "{found, waited_ms}",
        "wait_for": "{matches, waited_ms, value}",
        "session_export": "{path, cookie_origins, storage_origins}",
        "session_import": "{cookies_loaded, storage_injected[]}",
        "cookies_export_netscape": "{path, cookies}",
        "form_fill_submit": "{filled, submitted, verified?}",
        "markdown": "{markdown, chars}",
        "network": "{requests[{id,t,method,url,status}]}",
        "network_request": "{id, headers, ...}",
        "network_response": "{id, status, body}",
        "network_curl": "{curl}",
        "batch": "{results[]}", "cookies_import": "{imported}",
        "har_export": "{path, entries}",
        "cookies": "{cookies[]}", "storage": "{keys[]}",
        "storage_get": "{value}", "storage_set": "{set}",
        "storage_delete": "{deleted}",
        "screenshot": "{path?, bytes?, width, height}",
        "pdf": "{path}", "scrape": "{items[]}",
        "extract": "{data}", "download": "{id, url, delta?}",
        "downloads": "{downloads[]}", "elements": "{elements[]}",
        "errors": "{errors[]}", "forms": "{forms[]}",
        "form_fill": "{filled[], missing[], delta?}",
        "form_submit": "{submitted, delta?}",
        "structured": "{schema, data}",
        "snapshot": "{url,title,headings,links,buttons,inputs,a11y}",
        "view": "{streaming}", "view_stop": "{stopped}",
        "observe": "{events[]}", "console": "{logs[]}",
        "perf": "{metrics}", "sitemap": "{urls[]}",
        "page_json": "{json}", "yt_play": "{playing, delta?}",
        "yt_search": "{results[]}", "yt_video": "{info}",
        "session_save": "{saved}", "session_load": "{loaded}",
        "session_list": "{sessions[]}", "session_info": "{info}",
        "session_delete": "{deleted}",
        "bookmark_add": "{added}", "bookmark_list": "{bookmarks[]}",
        "http_cache": "{stats}", "warnings": "{warnings[]}",
        "capabilities": "{capabilities{...}}",
        "agent_cmd": "{result}", "agent_snapshot": "{snapshot}",
        "agent_tabs": "{tabs}",
        "touch": "{tapped/swiped, delta?}", "upload": "{path, staged}",
        "videos": "{videos[]}", "crashes": "{crash_count}",
        "network_bodies": "{bodies[]}", "network_clear": "{cleared}",
        "solve_aliyun_puzzle": "{ok, tries, mode}",
        "aliyun_analyze": "{ok, targetLeft?, analysis}",
        "vision_locate": "{candidates[]}",
        "vision_describe": "{description}",
        "vision_calibrate": "{viewport_css, mapping}",
        "see_tap": "{tapped, x, y, tries, delta?}",
        "see_type": "{typed, x, y, delta?}",
        "see_drag": "{dragged, from, to, delta?}",
        "stealth_apply": "{applied, verified?}",
        "stealth_impersonate": "{profile}",
        "stealth_status": "{profile, active}",
        "block_add": "{added}", "block_clear": "{cleared}",
        "block_list": "{patterns[]}",
        "ua_set": "{ua}", "ua_reset": "{reset}", "ua_show": "{ua}",
        "proxy_set": "{proxy}", "proxy_clear": "{cleared}",
        "proxy_show": "{proxy}",
        "cookies_set": "{set}", "session_clear": "{cleared}",
        "find": "{found, matches}", "press": "{pressed, delta?}",
        "longpress": "{longpressed, delta?}",
        "wait_idle": "{idle, waited_ms}",
        "click_through": "{navigated, clicked, verified}",
        "har_start": "{started}", "har_stop": "{path, entries}",
        "har_clear": "{cleared}",
        "download_pause": "{paused}", "download_resume": "{resumed}",
        "bookmark_open": "{opened}", "bookmark_delete": "{deleted}",
        "a11y_list": "{elements[]}",
    }

    _TOOL_ACTIONS = {}

    # Hermes #3: self-describing tool manifest. Generated from
    # _TOOL_ACTIONS at runtime — never hand-written docs that drift.
    _TOOL_DESCRIPTIONS = {
        "a11y": "accessibility tree",
        "agent": "drive the real rendered page (injected agent.js)",
        "aliyun_analyze": "dry-run Aliyun gap detection (no dragging)",
        "aliyun_solve": "solve Aliyun FeiLin slide/puzzle CAPTCHA (closed-loop)",
        "back": "go back",
        "block": "request blocking (playwright/webview engines)",
        "blocklist": "agent request blocklist (URL substrings)",
        "bookmark": "bookmarks",
        "check": "check checkbox/radio",
        "clear": "clear element",
        "click": "click element",
        "click_through": "SPA warm nav in one process: open URL (settled) -> ",
        "close_tab": "close tab",
        "composer_open": "open m.facebook composer via warm nav (feed -> click)",
        "console": "JS console logs",
        "cookies": "cookies: list, set (session injection), clear",
        "cookies_import": "import Netscape-format cookies.txt ",
        "crashes": "last app crash report (webview engine)",
        "crawl": "crawl site",
        "daemon": "persistent background engine: zero per-command ",
        "devtools": "capabilities & engine info",
        "dlpause": "pause download",
        "dlresume": "resume download",
        "doctor": "health check: engines, server, env",
        "dom": "DOM inspector",
        "download": "download URL",
        "downloads": "download manager",
        "errors": "error console",
        "extract": "reader mode",
        "find": "find text in the current page",
        "form": "form fill/submit",
        "forms": "list forms",
        "fwd": "go forward",
        "har": "HAR recording session",
        "hover": "hover element",
        "http_cache": "inspect/clear the HTTP cache",
        "js": "evaluate JavaScript (playwright engine)",
        "launch": "launch the Kancil Browser app ",
        "longpress": "mobile long-press on element (context menu)",
        "network": "network log",
        "new_tab": "new tab",
        "observe": "observability: console/network/page errors + perf",
        "open": "open URL",
        "page_json": "extract JSON blobs embedded in the page HTML",
        "pdf": "export page as PDF (playwright engine)",
        "perf": "performance timing",
        "press": "press a key like a human (Enter/Escape/Tab/arrows)",
        "profile": "form-fill profile",
        "proxy": "proxy settings",
        "proxy_ca": "generate MITM CA for serve-proxy ",
        "reload": "reload page",
        "scrape": "scrape structured data",
        "screenshot": "take screenshot (playwright engine)",
        "scroll": "scroll page/element",
        "search": "web search",
        "select": "choose dropdown option",
        "serve_proxy": "level 2: local HTTP(S) proxy — your real ",
        "session": "sessions",
        "shell": "interactive REPL",
        "sitemap": "list page URLs from sitemap.xml",
        "snapshot": "agent context snapshot",
        "stealth": "browser impersonation / anti-detect",
        "storage": "storage devtools",
        "structured": "extract JSON-LD + OpenGraph/Twitter meta tags",
        "switch": "switch tab",
        "tabs": "list tabs",
        "tool": "structured agent tool call (JSON in, JSON out)",
        "touch": "synthesized touch: tap / swipe / longpress / ",
        "type": "type into element",
        "ua": "user-agent",
        "uncheck": "uncheck",
        "upload": "stage file for next file-chooser (webview engine)",
        "videos": "list <video> elements (webview engine)",
        "view": "local viewer: see the page in your browser, ",
        "wait": "wait for selector/ms/text",
        "wait_idle": "wait for page settle: readyState + network quiet",
        "warnings": "console warnings",
        "yt_play": "mini YouTube player: URL/ID or search query -> ",
        "yt_search": "YouTube search via ytInitialData (no JS)",
        "yt_video": "YouTube video metadata via og: tags",
    }
    _TOOL_DESCRIPTIONS_EXTRA = {
        "block_add": "block request pattern", "block_clear": "clear blocklist",
        "block_list": "list blocklist", "bookmark_delete": "delete bookmark",
        "bookmark_open": "open bookmark", "download_pause": "pause download",
        "download_resume": "resume download", "har_clear": "clear HAR",
        "har_start": "start HAR capture", "har_stop": "stop HAR capture",
        "network_clear": "clear network log", "proxy_clear": "clear proxy",
        "proxy_show": "show proxy", "stealth_status": "stealth status",
        "ua_set": "set user agent", "ua_show": "show user agent",
        "a11y_find": "find element by semantic query + role",
        "agent_cmd": "agent session command", "agent_snapshot": "agent snapshot",
        "agent_tabs": "agent tab list", "batch": "run multiple tool actions",
        "bookmark_add": "add bookmark", "bookmark_list": "list bookmarks",
        "dom_find": "find via CSS selector", "dom_tree": "DOM tree",
        "dom_xpath": "find via XPath", "elements": "actionable elements",
        "evaluate": "run JavaScript", "focus": "focus element",
        "form_fill": "fill form fields", "form_submit": "submit form",
        "forward": "go forward", "har_export": "export HAR",
        "history": "nav history", "inspect": "inspect element",
        "network_curl": "request as curl", "network_request": "request detail",
        "network_response": "response body", "session_delete": "delete session",
        "session_info": "session info", "session_list": "list sessions",
        "session_load": "load session", "session_save": "save session",
        "storage_delete": "delete storage key", "storage_get": "get storage key",
        "storage_set": "set storage key", "switch_tab": "switch tab",
        "view_stop": "stop live view",
    }
    _TOOL_PARAM_EXAMPLES = {
        "selector": "#submit", "url": "https://example.com",
        "text": "hello", "query": "search box", "xpath": "//button",
        "js": "document.title", "expression": "document.title",
        "ms": 3000, "id": 1, "tab": 1, "limit": 50, "max_nodes": 200,
        "path": "/sdcard/shot.png", "file": "cookies.json",
        "key": "session", "kind": "local", "name": "q",
        "value": "option1", "role": "button", "target": "bottom",
        "pattern": "api", "method": "GET", "action": "click",
        "domain": "example.com", "description": "tombol Login biru",
        "question": "apa isi keranjang?", "from_desc": "gagang slider",
        "to_desc": "ujung kanan trek", "template": "/sdcard/icon.png",
        "backend": "api", "verify": "#success",
        "expr": "!document.querySelector('.spinner')",
        "verify_text": "Pesanan diterima",
    }
    _TOOL_STR_HINTS = {"selector", "url", "text", "query", "xpath", "js",
                       "expression", "path", "file", "key", "kind", "name",
                       "value", "role", "target", "pattern", "type",
                       "method", "action", "domain", "title", "question",
                       "description", "from_desc", "to_desc", "template",
                       "backend", "verify", "expr", "verify_text"}
    _TOOL_INT_HINTS = {"ms", "id", "tab", "limit", "max_nodes", "fidx",
                       "did", "index", "n", "retries", "timeout",
                       "max_tries", "timeout_ms", "poll_ms", "max_chars"}
    _TOOL_BOOL_HINTS = {"confirm", "idle", "full", "clear", "verbose"}

    @classmethod
    def _action_meta(cls, action):
        """JSON-Schema-ish manifest for one action, derived from the
        _TOOL_ACTIONS lambda + the api method it calls. No hand docs."""
        import inspect as _inspect
        import re as _re
        import ast as _ast
        handler = cls._TOOL_ACTIONS[action]
        meta = {"action": action, "description": "",
                "params": {}, "returns": cls._TOOL_RETURNS.get(
                    action, "{success:bool, ...}"),
                "engines": ["static", "webview", "playwright"]}
        try:
            src = _inspect.getsource(handler)
        except Exception:
            return meta
        if "s.capabilities" in src and "s.capabilities}" in src.replace(
                " ", ""):
            meta["description"] = "engine capability flags"
            return meta
        m = _re.search(r"s\.(\w+)\s*\(", src)
        if not m:
            return meta
        method_name = m.group(1)
        meta["method"] = method_name
        fn = getattr(cls, method_name, None)
        if fn is not None:
            doc = (_inspect.getdoc(fn) or "").split("\n")[0]
            # unwrap the _with_delta decorator for the real docstring
            if not doc and hasattr(fn, "__wrapped__"):
                doc = (_inspect.getdoc(fn.__wrapped__) or ""
                       ).split("\n")[0]
            if not doc:
                doc = cls._TOOL_DESCRIPTIONS.get(action, "")
            if not doc:
                doc = cls._TOOL_DESCRIPTIONS_EXTRA.get(action, "")
            meta["description"] = doc
            try:
                sig = _inspect.signature(
                    fn.__wrapped__ if hasattr(fn, "__wrapped__")
                    else fn)
                sig_defaults = {
                    n: prm.default for n, prm in sig.parameters.items()
                    if prm.default is not _inspect.Parameter.empty}
            except Exception:
                sig_defaults = {}
            # engine gating, source-grounded: webview-only api
            # methods delegate via getattr(self.engine, "x", None) +
            # fail("... needs the webview engine"). Owners are checked
            # against the real engine classes (branching per engine,
            # e.g. click(), means all-engines — not gated).
            try:
                fsrc = _inspect.getsource(
                    fn.__wrapped__ if hasattr(fn, "__wrapped__") else fn)
                for gm in _re.finditer(
                        r'getattr\(self\.engine,\s*"([\w]+)"', fsrc):
                    attr = gm.group(1)
                    owners = [e for e, c in
                              (("static", cls._engine_cls("static")),
                               ("webview", cls._engine_cls("webview")),
                               ("playwright",
                                cls._engine_cls("playwright")))
                              if hasattr(c, attr)]
                    if len(owners) == 1:
                        meta["engines"] = owners
                        break
            except Exception:
                pass
        else:
            sig_defaults = {}

        def _jtype(py, name):
            if isinstance(py, bool):
                return "boolean"
            if isinstance(py, int):
                return "integer"
            if isinstance(py, float):
                return "number"
            if isinstance(py, str):
                return "string"
            if isinstance(py, (list, tuple)):
                return "array"
            if name in cls._TOOL_BOOL_HINTS:
                return "boolean"
            if name in cls._TOOL_INT_HINTS:
                return "integer"
            return "string"

        seen = set()
        # p["k"] -> required
        for mm in _re.finditer(r'p\["([\w]+)"\]', src):
            k = mm.group(1)
            if k in seen:
                continue
            seen.add(k)
            meta["params"][k] = {
                "type": _jtype(sig_defaults.get(k), k),
                "required": True,
                "example": cls._TOOL_PARAM_EXAMPLES.get(k)}
        # p.get("k"[, default]) -> optional
        for mm in _re.finditer(
                r'(?:(int|bool|float)\(\s*)?p\.get\("([\w]+)"'
                r'(?:,\s*(.+?))?\)', src):
            wrapper, k, dflt_src = mm.groups()
            if k in seen:
                continue
            seen.add(k)
            default = None
            if dflt_src is not None:
                try:
                    default = _ast.literal_eval(dflt_src.strip())
                except Exception:
                    default = dflt_src.strip()
            elif k in sig_defaults:
                default = sig_defaults[k]
                if default is _inspect.Parameter.empty:
                    default = None
            ptype = {"int": "integer", "bool": "boolean",
                     "float": "number"}.get(wrapper or "")
            if not ptype:
                base = default if default is not None else \
                    sig_defaults.get(k)
                ptype = _jtype(base, k)
            pentry = {
                "type": ptype, "required": False, "default": default,
                "example": cls._TOOL_PARAM_EXAMPLES.get(k)}
            # nested fallback: p.get("id", p.get("tab", 0)) -> alias.
            # (dflt_src is truncated at the first ")" by the main
            # regex, so match the inner form tolerantly.)
            if isinstance(dflt_src, str) and dflt_src.strip(
                    ).startswith("p.get("):
                am = _re.match(r'p\.get\("(\w+)"\s*,\s*([^,)]+)',
                               dflt_src.strip())
                if am:
                    try:
                        pentry["default"] = _ast.literal_eval(
                            am.group(2).strip())
                    except Exception:
                        pentry["default"] = am.group(2).strip()
                    pentry["alias"] = am.group(1)
                    if pentry["example"] is None:
                        pentry["example"] = cls._TOOL_PARAM_EXAMPLES.get(
                            am.group(1))
            meta["params"][k] = pentry
        if method_name in cls._CASCADE_METHODS and "selector" in meta[
                "params"]:
            meta["params"]["selector"]["cascade"] = True
        return meta

    @classmethod
    def _engine_cls(cls, name):
        if name == "static":
            from .engines import StaticEngine
            return StaticEngine
        if name == "webview":
            from .webview_engine import WebViewEngine
            return WebViewEngine
        from .pw_engine import PlaywrightEngine
        return PlaywrightEngine

    @classmethod
    def _manifest_cache(cls):
        if not hasattr(cls, "_manifest_built"):
            cls._manifest_built = {
                a: cls._action_meta(a)
                for a in sorted(cls._TOOL_ACTIONS)}
        return cls._manifest_built

    def tool_schema(self, action=None):
        """Self-describing manifest for agents (Hermes #3).

        tool_schema() -> all 74 actions; tool_schema("click") -> one.
        Generated from _TOOL_ACTIONS at runtime: params (type/required/
        default/example), return shape, engine support. Load once,
        never guess from stale docs."""
        from . import __version__
        if action is not None:
            if action not in self._TOOL_ACTIONS:
                return {"success": False, "errors": [
                    "unknown action %r" % (action,)],
                    "error": self._envelope(
                        "UNKNOWN_ACTION", "unknown action %r" % (action,),
                        actions=sorted(self._TOOL_ACTIONS))}
            return {"success": True, "action": action,
                    "schema": self._manifest_cache()[action]}
        return {"success": True, "kancil": __version__,
                "actions": self._manifest_cache(),
                "error_envelope": "{success:false, errors[], "
                    "error:{code, message, retriable, hint}}",
                "delta": "mutating actions attach "
                    "delta{url,title,text_chars,media,shell}"}

    def tool(self, payload):
        """Structured agent tool call. payload: {"action": ..., ...params}."""
        if not isinstance(payload, dict):
            return {"success": False, "error": self._envelope(
                "INVALID_INPUT", "payload must be a JSON object")}
        action = payload.get("action")
        handler = self._TOOL_ACTIONS.get(action)
        if not handler:
            return {"success": False, "error": self._envelope(
                "UNKNOWN_ACTION", "unknown action %r" % (action,),
                actions=sorted(self._TOOL_ACTIONS))}
        try:
            result = handler(self, payload)
        except Exception as e:
            return {"success": False, "error": self._envelope(
                "INTERNAL_ERROR",
                "%s: %s" % (type(e).__name__, str(e)[:200]),
                action=action)}
        if not isinstance(result, dict):
            return {"success": False, "error": {
                "code": "INTERNAL_ERROR", "message": "bad handler result"}}
        if result.get("success"):
            out = {"success": True, "action": action}
            for k, v in result.items():
                if k != "success":
                    out[k] = v
            return out
        errs = result.get("errors") or ["unknown error"]
        code = result.get("code") or self._code_for(action, errs[0])
        err = self._envelope(code, errs[0], action=action)
        for k in ("selector", "url", "id", "tab", "name", "key"):
            if k in payload:
                err[k] = payload[k]
        if len(errs) > 1:
            err["details"] = errs[1:4]
        return {"success": False, "error": err}

    # ---------- snapshot ----------
    def snapshot(self, full=False, dom=False, a11y=True, links=True, forms=True):
        """One-response page overview for the agent (token-efficient)."""
        p, err = self._page_or_fail()
        if err:
            return err
        page = {"headings": [], "links": [], "buttons": [], "inputs": []}
        a11y_data = None
        dom_data = None
        if p.dom:
            from .dom import select as _sel
            page["headings"] = [
                {"level": h.tag, "text": h.text_content()[:100]}
                for h in _sel(p.dom, "h1,h2,h3")[:20]]
            if links:
                page["links"] = [{"text": t[:60], "url": u}
                                 for t, u in p.links[:40]]
            page["buttons"] = [
                {"name": e["name"][:60], "ref": e["ref"]}
                for e in p.elements if e["role"] == "button"][:20]
            if forms:
                page["inputs"] = [
                    {"name": e["name"][:60], "ref": e["ref"], "role": e["role"]}
                    for e in p.elements
                    if e["role"] in ("textbox", "checkbox", "radio", "combobox")][:20]
            if a11y:
                tree, _refs = self._ensure_a11y_refs(p)
                a11y_data = tree
            if dom or full:
                r = self.dom_tree(max_nodes=300 if full else 120)
                dom_data = r.get("nodes") if r.get("success") else None
        errs = self.engine.errors
        errs = list(errs() if callable(errs) else errs)[-5:]
        net = self.engine.netlog
        failed = sum(1 for e in net if isinstance(e.get("status"), int)
                     and e["status"] >= 400)
        return ok(url=p.url if hasattr(p, "url") else "",
                  title=p.title if hasattr(p, "title") else "",
                  tab_id=self.engine.cur,
                  engine=self._engine_name,
                  page=page, a11y=a11y_data, dom=dom_data,
                  errors=errs,
                  network_summary={"requests": len(net), "failed": failed})

    # ---------- observability ----------
    def warnings(self):
        eng = self.engine
        if hasattr(eng, "_console"):
            logs = [l for l in eng._console if l.get("type") == "warning"][-30:]
            return ok(warnings=logs, count=len(logs))
        return ok(warnings=[], count=0,
                  note="no JS console on static engine")

    def observe(self):
        eng = self.engine
        console_errors, console_warnings, network_errors, page_errors = [], [], [], []
        if hasattr(eng, "_console"):
            for l in eng._console[-50:]:
                if l.get("type") == "error":
                    console_errors.append(l)
                elif l.get("type") == "warning":
                    console_warnings.append(l)
        errs = eng.errors
        for e in (list(errs() if callable(errs) else errs))[-30:]:
            t = e.get("type", "")
            if t == "js":
                page_errors.append(e)
            elif t in ("network", "http"):
                network_errors.append(e)
            elif t == "console.error":
                console_errors.append(e)
        return ok(console_errors=console_errors[-20:],
                  console_warnings=console_warnings[-20:],
                  network_errors=network_errors[-20:],
                  page_errors=page_errors[-20:],
                  performance={"uptime_s": int(time.time() - self._start_t),
                               "tabs": len(eng.tabs),
                               "requests": len(eng.netlog)})


# Action table for Kancil.tool(). Maps action -> (instance, payload) -> result.
Kancil._TOOL_ACTIONS = {
    # navigation
    "open": lambda s, p: s.open(p.get("url", ""),
                        idle=p.get("idle", False),
                        verify=p.get("verify")),
    "back": lambda s, p: s.back(verify=p.get("verify"),
                               verify_text=p.get("verify_text")),
    "forward": lambda s, p: s.forward(verify=p.get("verify"),
                                       verify_text=p.get("verify_text")),
    "reload": lambda s, p: s.reload(),
    "history": lambda s, p: s.history(),
    # tabs
    "tabs": lambda s, p: s.tabs(),
    "new_tab": lambda s, p: s.new_tab(p.get("url")),
    "switch_tab": lambda s, p: s.switch_tab(int(p.get("id", p.get("tab", 0)))),
    "close_tab": lambda s, p: s.close_tab(
        int(p["id"]) if p.get("id") is not None else None),
    # dom / a11y
    "inspect": lambda s, p: s.inspect(p.get("selector", "body")),
    "elements": lambda s, p: s.elements(),
    "a11y": lambda s, p: s.a11y(),
    "a11y_find": lambda s, p: s.a11y_find(p.get("query", ""), role=p.get("role")),
    "dom_tree": lambda s, p: s.dom_tree(max_nodes=int(p.get("max_nodes", 200))),
    "dom_find": lambda s, p: s.dom_find(p.get("selector", "")),
    "dom_xpath": lambda s, p: s.dom_xpath(p.get("xpath", "")),
    # interaction
    "click": lambda s, p: s.click(p.get("selector", ""),
                         confirm=p.get("confirm", False),
                         verify=p.get("verify")),
    "type": lambda s, p: s.type(p.get("selector", ""), p.get("text", ""),
                        verify=p.get("verify")),
    "clear": lambda s, p: s.clear(p.get("selector", "")),
    "select": lambda s, p: s.select(p.get("selector", ""), p.get("value", "")),
    "check": lambda s, p: s.check(p.get("selector", "")),
    "uncheck": lambda s, p: s.uncheck(p.get("selector", "")),
    "hover": lambda s, p: s.hover(p.get("selector", "")),
    "focus": lambda s, p: s.focus(p.get("selector", "")),
    "scroll": lambda s, p: s.scroll(p.get("target", "bottom")),
    # js / wait
    "evaluate": lambda s, p: s.evaluate(p.get("js", p.get("expression", ""))),
    "wait": lambda s, p: s.wait(selector=p.get("selector"),
                                ms=p.get("ms"), text=p.get("text")),
    "wait_for": lambda s, p: s.wait_for(
        p.get("expr", ""), timeout_ms=int(p.get("timeout_ms", 10000)),
        poll_ms=int(p.get("poll_ms", 300))),
    # session persist (agent 1.27+)
    "session_export": lambda s, p: s.session_export(p.get("path", "")),
    "session_import": lambda s, p: s.session_import(p.get("path", "")),
    "cookies_export_netscape": lambda s, p: s.cookies_export_netscape(
        p.get("path", "")),
    # forms + markdown
    "form_fill_submit": lambda s, p: s.form_fill_submit(
        int(p.get("id", 1)), values=p.get("values"), verify=p.get("verify"),
        verify_text=p.get("verify_text"), auto=bool(p.get("auto", False)),
        confirm=bool(p.get("confirm", False))),
    "markdown": lambda s, p: s.markdown(
        max_chars=int(p.get("max_chars", 60000))),
    # network
    "network": lambda s, p: s.network(
        pattern=p.get("pattern"), limit=int(p.get("limit", 50)),
        type_=p.get("type"), status=p.get("status"), method=p.get("method")),
    "network_request": lambda s, p: s.network_request(int(p.get("id", -1))),
    "network_response": lambda s, p: s.network_response(int(p.get("id", -1))),
    "network_curl": lambda s, p: s.network_curl(int(p.get("id", -1))),
    "batch": lambda s, p: s.tool_batch(p),
    "cookies_import": lambda s, p: s.cookies_import(p.get("file", "")),
    "har_export": lambda s, p: s.har_export(p.get("path", "network.har")),
    # storage
    "cookies": lambda s, p: s.cookies(),
    "storage": lambda s, p: s.storage(p.get("kind", "local")),
    "storage_get": lambda s, p: s.storage_get(p.get("key", ""), p.get("kind", "local")),
    "storage_set": lambda s, p: s.storage_set(p.get("key", ""), p.get("value", ""),
                                              p.get("kind", "local")),
    "storage_delete": lambda s, p: s.storage_delete(p.get("key", ""),
                                                    p.get("kind", "local")),
    # capture
    "screenshot": lambda s, p: s.screenshot(path=p.get("path"),
                                            full=bool(p.get("full")),
                                            selector=p.get("selector")),
    "pdf": lambda s, p: s.pdf(path=p.get("path")),
    # scrape / extract / download
    "scrape": lambda s, p: s.scrape(
        url=p.get("url"), selector=p.get("selector"), fields=p.get("fields"),
        auto=bool(p.get("auto")), fmt=p.get("format", "json"),
        pages=int(p.get("pages", p.get("max_pages", 1))),
        next_selector=p.get("next_selector"),
        max_items=int(p.get("max_items", 1000)),
        timeout=int(p.get("timeout", 180)),
        sitemap=bool(p.get("sitemap")),
        workers=int(p.get("workers", 1)),
        scroll_pages=int(p.get("scroll_pages", 0))),
    "extract": lambda s, p: s.extract(mode=p.get("mode", "auto")),
    "download": lambda s, p: s.download(p.get("url", ""), p.get("path")),
    "downloads": lambda s, p: s.downloads(),
    # sessions
    "session_save": lambda s, p: s.session_save(p.get("name", "")),
    "session_load": lambda s, p: s.session_load(p.get("name", "")),
    "session_delete": lambda s, p: s.session_delete(p.get("name", "")),
    "session_info": lambda s, p: s.session_info(p.get("name", "")),
    "session_list": lambda s, p: s.session_list(),
    # misc agent tools
    "snapshot": lambda s, p: s.snapshot(
        full=bool(p.get("full")), dom=bool(p.get("dom")),
        a11y=p.get("a11y", True), links=p.get("links", True),
        forms=p.get("forms", True)),
    "observe": lambda s, p: s.observe(),
    "console": lambda s, p: s.console(),
    "warnings": lambda s, p: s.warnings(),
    "errors": lambda s, p: s.errors(),
    "perf": lambda s, p: s.perf(p.get("url")),
    "forms": lambda s, p: s.forms(),
    "form_fill": lambda s, p: s.form_fill(int(p.get("id", 1)),
                                          p.get("values"), bool(p.get("auto"))),
    "form_submit": lambda s, p: s.form_submit(int(p.get("id", 1))),
    "bookmark_add": lambda s, p: s.bookmark_add(url=p.get("url"), title=p.get("title")),
    "bookmark_list": lambda s, p: s.bookmark_list(),
    "page_json": lambda s, p: s.page_json(),
    "structured": lambda s, p: s.structured(),
    "sitemap": lambda s, p: s.sitemap(url=p.get("url"),
                                     max_urls=int(p.get("max_urls", 5000))),
    "http_cache": lambda s, p: s.http_cache(action=p.get("action", "stats")),
    "yt_search": lambda s, p: s.yt_search(p.get("query", ""),
                                          int(p.get("max_results", 20))),
    "yt_video": lambda s, p: s.yt_video(p.get("url", "")),
    "view": lambda s, p: s.view(port=int(p.get("port", 0)),
                                host=p.get("host", "127.0.0.1")),
    "view_stop": lambda s, p: s.view_stop(),
    "yt_play": lambda s, p: s.yt_play(p.get("target", ""),
                                      port=int(p.get("port", 0)),
                                      host=p.get("host", "127.0.0.1")),
    "agent_tabs": lambda s, p: s.agent_tabs(),
    "agent_cmd": lambda s, p: s.agent_cmd(p.get("tab", ""),
                                          p.get("action", ""),
                                          p.get("args") or {},
                                          p.get("timeout", 30)),
    "agent_snapshot": lambda s, p: s.agent_snapshot(p.get("tab", ""),
                                                    p.get("timeout", 30)),
    "capabilities": lambda s, p: {"success": True, "capabilities": s.capabilities},
    "touch": lambda s, p: s.touch(action=p.get("action", "tap"),
                        x=p.get("x"), y=p.get("y"),
                        x2=p.get("x2"), y2=p.get("y2"),
                        selector=p.get("selector"),
                        duration_ms=p.get("duration_ms"),
                        human=bool(p.get("human", False)),
                        confirm=bool(p.get("confirm", False))),
    "upload": lambda s, p: s.upload(p.get("path", ""),
                          confirm=bool(p.get("confirm", False))),
    "videos": lambda s, p: s.videos(),
    "crashes": lambda s, p: s.crashes(clear=bool(p.get("clear", False))),
    "network_bodies": lambda s, p: s.network_bodies(
        clear=bool(p.get("clear", False))),
    "network_clear": lambda s, p: s.network_clear(),
    "solve_aliyun_puzzle": lambda s, p: s.solve_aliyun_puzzle(
        max_tries=int(p.get("max_tries", 4)),
        handle_sel=p.get("handle_sel"), puzzle_sel=p.get("puzzle_sel"),
        verbose=bool(p.get("verbose", True)),
        confirm=bool(p.get("confirm", False))),
    "aliyun_analyze": lambda s, p: s.aliyun_analyze(
        handle_sel=p.get("handle_sel"), puzzle_sel=p.get("puzzle_sel")),
    "vision_locate": lambda s, p: s.vision_locate(
        p.get("description", ""), backend=p.get("backend"),
        template=p.get("template"),
        multiple=bool(p.get("multiple", False))),
    "vision_describe": lambda s, p: s.vision_describe(
        question=p.get("question", "")),
    "vision_calibrate": lambda s, p: s.vision_calibrate(),
    "see_tap": lambda s, p: s.see_tap(
        p.get("description", ""), backend=p.get("backend"),
        template=p.get("template"), verify=p.get("verify"),
        max_tries=int(p.get("max_tries", 3)),
        human=bool(p.get("human", False)),
        confirm=bool(p.get("confirm", False))),
    "see_type": lambda s, p: s.see_type(
        p.get("description", ""), p.get("text", ""),
        backend=p.get("backend"), template=p.get("template"),
        confirm=bool(p.get("confirm", False))),
    "see_drag": lambda s, p: s.see_drag(
        p.get("from_desc", ""), p.get("to_desc", ""),
        backend=p.get("backend"), template=p.get("template"),
        human=bool(p.get("human", True)),
        confirm=bool(p.get("confirm", False))),
    "stealth_apply": lambda s, p: s.stealth_apply(),
    "stealth_impersonate": lambda s, p: s.stealth_impersonate(
        p.get("profile", "")),
    "stealth_status": lambda s, p: s.stealth_status(),
    "block_add": lambda s, p: s.block_add(p.get("pattern", "")),
    "block_clear": lambda s, p: s.block_clear(),
    "block_list": lambda s, p: s.block_list(),
    "ua_set": lambda s, p: s.ua_set(p.get("ua", "")),
    "ua_reset": lambda s, p: s.ua_reset(),
    "ua_show": lambda s, p: s.ua_show(),
    "proxy_set": lambda s, p: s.proxy_set(p.get("url", "")),
    "proxy_clear": lambda s, p: s.proxy_clear(),
    "proxy_show": lambda s, p: s.proxy_show(),
    "cookies_set": lambda s, p: s.cookies_set(
        p.get("name", ""), p.get("value", ""),
        domain=p.get("domain"), path=p.get("path", "/")),
    "session_clear": lambda s, p: s.session_clear(
        what=p.get("what", "all"), domain=p.get("domain")),
    "find": lambda s, p: s.find(p.get("text", ""),
                      next=bool(p.get("next", False))),
    "press": lambda s, p: s.press(p.get("key", "Enter"),
                        selector=p.get("selector")),
    "longpress": lambda s, p: s.longpress(p.get("selector", "")),
    "wait_idle": lambda s, p: s.wait_idle(
        timeout=int(p.get("timeout", 15))),
    "click_through": lambda s, p: s.click_through(
        p.get("url", ""), p.get("click_selector", ""),
        p.get("wait_selector", ""),
        timeout=int(p.get("timeout", 25)),
        confirm=bool(p.get("confirm", False))),
    "har_start": lambda s, p: s.har_start(),
    "har_stop": lambda s, p: s.har_stop(),
    "har_clear": lambda s, p: s.har_clear(),
    "download_pause": lambda s, p: s.download_pause(int(p.get("id", 0))),
    "download_resume": lambda s, p: s.download_resume(int(p.get("id", 0))),
    "bookmark_open": lambda s, p: s.bookmark_open(p.get("id", "")),
    "bookmark_delete": lambda s, p: s.bookmark_delete(p.get("id", "")),
    "a11y_list": lambda s, p: s.a11y_list(),
}
