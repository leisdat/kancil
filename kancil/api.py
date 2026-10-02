"""Kancil Agent API — structured, JSON-friendly browser automation.

Every method returns a dict: {"success": bool, ...data..., "errors": []}
Designed so Hermes (or any agent) never parses terminal text.
"""
import json
import os
import re
import time

from . import engines
from . import session as session_mod
from .devtools import scrape_items, scrape_auto, extract_reader
from .dom import inspect_element, select, xpath, smart_resolve, generate_css, generate_xpath


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
                 pw_executable_path=None, cache=True):
        self.profile = profile or {}
        self.proxy = proxy
        self.ua = ua
        self.cache = cache
        self.pw_session = pw_session
        self.pw_browser = pw_browser
        self.pw_executable_path = pw_executable_path
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
        return engines.StaticEngine(timeout=timeout, retries=retries,
                                    cookie_file=cookie_file,
                                    proxy=self.proxy,
                                    user_agent=self.ua or engines.UA_DEFAULT,
                                    cache=self.cache)

    @property
    def capabilities(self):
        caps = dict(self.engine.capabilities)
        caps["engine"] = self._engine_name
        return caps

    # ---------- navigation ----------
    def open(self, url):
        r = self.engine.open(url)
        return self._wrap(r)

    def back(self):
        return self._wrap(self.engine.back())

    def forward(self):
        return self._wrap(self.engine.forward())

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

    def click(self, selector):
        return self._wrap(self.engine.click(selector))

    def type(self, selector, text):
        return self._wrap(self.engine.type(selector, text))

    def clear(self, selector):
        return self._wrap(self.engine.clear(selector))

    def select(self, selector, value):
        return self._wrap(self.engine.select_option(selector, value))

    def check(self, selector):
        return self._wrap(self.engine.check(selector))

    def uncheck(self, selector):
        return self._wrap(self.engine.uncheck(selector))

    def hover(self, selector):
        return self._wrap(self.engine.hover(selector))

    def focus(self, selector):
        return self._wrap(self.engine.focus(selector))

    def scroll(self, target="bottom"):
        return self._wrap(self.engine.scroll(target))

    # ---------- js / wait / console ----------
    def evaluate(self, js):
        return self._wrap(self.engine.evaluate(js))

    def wait(self, selector=None, ms=None, text=None):
        return self._wrap(self.engine.wait(selector=selector, ms=ms, text=text))

    def console(self):
        return self._wrap(self.engine.console())

    def errors(self):
        errs = self.engine.errors
        return ok(errors=list(errs() if callable(errs) else errs)[-50:])

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

    def storage(self, kind="local"):
        data = self.engine.storage(kind)
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
                                          "text": data["text"][:20000]})
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

    def form_fill(self, form_id, values=None, auto=False):
        return self._wrap(self.engine.fill_form(form_id - 1, values, auto, self.profile))

    def form_submit(self, form_id):
        return self._wrap(self.engine.submit_form(form_id - 1))

    # ---------- downloads ----------
    def download(self, url, path=None):
        return self._wrap(self.engine.download(url, path))

    def downloads(self):
        return ok(downloads=self.engine.download_list())

    def download_pause(self, did):
        return self._wrap(self.engine.download_pause(did))

    def download_resume(self, did):
        return self._wrap(self.engine.download_resume(did))

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

    # ---------- request blocking (playwright) ----------
    def block_add(self, pattern):
        fn = getattr(self.engine, "block_add", None)
        if not fn:
            return fail("request blocking needs the playwright engine",
                        supported=False)
        return self._wrap(fn(pattern))

    def block_list(self):
        fn = getattr(self.engine, "block_list", None)
        if not fn:
            return fail("request blocking needs the playwright engine",
                        supported=False)
        return ok(patterns=fn())

    def block_clear(self):
        fn = getattr(self.engine, "block_clear", None)
        if not fn:
            return fail("request blocking needs the playwright engine",
                        supported=False)
        return self._wrap(fn())

    # ---------- HAR export ----------
    # ---------- HAR ----------
    # Recording reuses the network log; har start = clear + mark, export
    # serializes to valid HAR 1.2 with safe-by-default redaction.
    def har_start(self):
        self.engine.network_clear()
        self._har_recording = True
        self._har_started = time.time()
        return ok(recording=True)

    def har_stop(self):
        self._har_recording = False
        return ok(recording=False, entries=len(self.engine.netlog))

    def har_clear(self):
        self.engine.network_clear()
        self._har_recording = False
        return ok(cleared=True)

    def har_stats(self):
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
        import datetime
        import urllib.parse as up
        if not redact:
            redact_cookie = redact_authorization = redact_token = False
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
            entries.append({
                "startedDateTime": started,
                "time": e.get("ms", 0),
                "request": {
                    "method": e.get("method"), "url": e.get("url"),
                    "httpVersion": "HTTP/1.1", "cookies": [],
                    "headers": [{"name": k, "value": str(v)} for k, v in req_hdrs.items()],
                    "queryString": qs, "headersSize": -1,
                    "bodySize": e.get("request_size", 0),
                    **({"postData": post} if post else {}),
                },
                "response": {
                    "status": e.get("status") if isinstance(e.get("status"), int) else 0,
                    "statusText": "", "httpVersion": "HTTP/1.1", "cookies": [],
                    "headers": [{"name": k, "value": str(v)} for k, v in res_hdrs.items()],
                    "content": {"size": e.get("response_size", e.get("size", 0)),
                                "mimeType": e.get("ctype", "")},
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
        if action.startswith("scrape"):
            return "SCRAPE_FAILED"
        if action.startswith("download"):
            return "DOWNLOAD_FAILED"
        if ("connection" in m or "dns" in m or "network" in m
                or "name resolution" in m):
            return "NETWORK_ERROR"
        return "UNKNOWN_ERROR"

    _TOOL_ACTIONS = {}

    def tool(self, payload):
        """Structured agent tool call. payload: {"action": ..., ...params}."""
        if not isinstance(payload, dict):
            return {"success": False, "error": {
                "code": "INVALID_INPUT", "message": "payload must be a JSON object"}}
        action = payload.get("action")
        handler = self._TOOL_ACTIONS.get(action)
        if not handler:
            return {"success": False, "error": {
                "code": "UNKNOWN_ACTION",
                "message": "unknown action %r" % (action,),
                "actions": sorted(self._TOOL_ACTIONS)}}
        try:
            result = handler(self, payload)
        except Exception as e:
            return {"success": False, "error": {
                "code": "INTERNAL_ERROR",
                "message": "%s: %s" % (type(e).__name__, str(e)[:200]),
                "action": action}}
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
        err = {"code": code, "message": errs[0], "action": action}
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
def _p(payload, *keys, default=None):
    return {k: payload[k] for k in keys if k in payload}


Kancil._TOOL_ACTIONS = {
    # navigation
    "open": lambda s, p: s.open(p.get("url", "")),
    "back": lambda s, p: s.back(),
    "forward": lambda s, p: s.forward(),
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
    "click": lambda s, p: s.click(p.get("selector", "")),
    "type": lambda s, p: s.type(p.get("selector", ""), p.get("text", "")),
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
}
