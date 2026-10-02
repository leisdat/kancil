"""Kancil engines.

StaticEngine  — pure Python (urllib), works everywhere incl. Termux.
                No JS, no screenshots, no real localStorage/IndexedDB.
PlaywrightEngine (pw_engine.py) — real Chromium when available.

Both expose the same interface; check .capabilities before using
engine-specific features.
"""
import hashlib
import http.cookiejar
import json
import os
import re
import socket
import threading
import time
import urllib.parse
import urllib.request

from .dom import (build_dom, extract_title, render_text, select, select_one,
                  smart_resolve, xpath, a11y_items, inspect_element,
                  generate_css, Node)

UA_DEFAULT = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36")

UA_LIST = [
    UA_DEFAULT,
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
     "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"),
    ("Mozilla/5.0 (Linux; Android 14; SM-G998B) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/125.0 Mobile Safari/537.36"),
    ("Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0"),
]


class EngineError(Exception):
    pass


class _PooledResponse:
    """http.client.HTTPResponse wrapper that returns the connection to the
    pool on close() when the body was fully consumed and the server allows
    keep-alive. Otherwise the connection is closed. All other attributes
    delegate to the wrapped response."""

    def __init__(self, r, conn, key, handler):
        object.__setattr__(self, "_r", r)
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_key", key)
        object.__setattr__(self, "_handler", handler)
        object.__setattr__(self, "_eof", False)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_r"), name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_r"), name, value)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def read(self, amt=None):
        r = object.__getattribute__(self, "_r")
        data = r.read() if amt is None else r.read(amt)
        # read() with no arg, or fewer bytes than asked: hit EOF -> the
        # connection is positioned for the next response and reusable.
        if amt is None or len(data) < amt:
            object.__setattr__(self, "_eof", True)
        return data

    def close(self):
        r = object.__getattribute__(self, "_r")
        conn = object.__getattribute__(self, "_conn")
        try:
            reusable = (object.__getattribute__(self, "_eof")
                        and not r.will_close
                        and getattr(conn, "sock", None) is not None)
        except Exception:
            reusable = False
        try:
            r.close()
        finally:
            if reusable:
                object.__getattribute__(self, "_handler")._pool_put(
                    object.__getattribute__(self, "_key"), conn)
            else:
                try:
                    conn.close()
                except Exception:
                    pass


class _KeepAliveMixin:
    """urllib handler mixin: per-origin keep-alive connection pooling.

    Mirrors AbstractHTTPHandler.do_open, except it does NOT force
    "Connection: close" and does NOT kill the socket after the response.
    A stale pooled connection is dropped and the request retried once on a
    fresh connection. Only installed when no proxy is configured (proxy
    tunneling has its own connection semantics).
    """
    _MAX_POOL = 16

    def __init__(self, debuglevel=None):
        self._pool = {}
        self._pool_lock = threading.Lock()
        super().__init__(debuglevel)

    def _pool_get(self, http_class, host, timeout, http_conn_args):
        key = (http_class.__name__, host)
        with self._pool_lock:
            h = self._pool.pop(key, None)
        if h is not None and getattr(h, "sock", None) is None:
            try:
                h.close()
            except Exception:
                pass
            h = None
        fresh = h is None
        if fresh:
            h = http_class(host, timeout=timeout, **http_conn_args)
        return h, key, fresh

    def _pool_put(self, key, h):
        with self._pool_lock:
            if len(self._pool) >= self._MAX_POOL:
                try:
                    h.close()
                except Exception:
                    pass
            else:
                self._pool[key] = h

    def close_idle(self):
        """Close all pooled keep-alive connections."""
        with self._pool_lock:
            pool, self._pool = self._pool, {}
        for h in pool.values():
            try:
                h.close()
            except Exception:
                pass

    def do_open(self, http_class, req, **http_conn_args):
        from urllib.error import URLError
        host = req.host
        if not host:
            raise URLError('no host given')
        if getattr(req, "_tunnel_host", None):
            # tunneled (proxy CONNECT) connection: never pool
            return super().do_open(http_class, req, **http_conn_args)
        h, key, fresh = self._pool_get(http_class, host, req.timeout,
                                       http_conn_args)
        h.set_debuglevel(self._debuglevel)
        headers = dict(req.unredirected_hdrs)
        headers.update({k: v for k, v in req.headers.items()
                        if k not in headers})
        # NB: stdlib forces "Connection: close" here; we keep HTTP/1.1
        # keep-alive so the socket survives for the next request.
        headers.setdefault("Connection", "keep-alive")
        headers = {name.title(): val for name, val in headers.items()}
        for _ in range(2):
            try:
                h.request(req.get_method(), req.selector, req.data, headers,
                          encode_chunked=req.has_header('Transfer-encoding'))
            except OSError as err:
                # probably a stale pooled connection: retry once fresh
                try:
                    h.close()
                except Exception:
                    pass
                if not fresh:
                    # stale pooled connection: retry once on a fresh one
                    h = http_class(host, timeout=req.timeout, **http_conn_args)
                    fresh = True
                    continue
                raise URLError(err)
            break
        try:
            r = h.getresponse()
        except Exception:
            try:
                h.close()
            except Exception:
                pass
            raise
        r.msg = r.reason
        r.url = req.get_full_url()
        return _PooledResponse(r, h, key, self)


class _KeepAliveHTTPHandler(_KeepAliveMixin, urllib.request.HTTPHandler):
    pass


class _KeepAliveHTTPSHandler(_KeepAliveMixin, urllib.request.HTTPSHandler):
    pass


class Page:
    def __init__(self, url, status, raw, ctype):
        self.url = url
        self.status = status
        self.raw = raw
        self.ctype = ctype
        self.dom = None
        self.title = url
        self.text = ""
        self.links = []
        self.forms = []
        self.elements = []   # actionable a11y items with refs
        self.values = {}     # ref -> value
        if raw is not None and "html" in ctype:
            self._build_html(raw, url)
        elif raw is not None and (ctype.startswith("text/") or "json" in ctype):
            # readable text responses (e.g. form POST echo) become viewable text
            txt = _decode(raw)
            if len(txt) < 100000:
                self.text = txt
        # content sniffing (like real browsers): missing/wrong content-type
        # but body looks like HTML -> parse it as HTML anyway
        if self.dom is None and raw is not None and raw.lstrip()[:1] == b"<":
            try:
                self._build_html(raw, url)
            except Exception:
                pass

    def _build_html(self, raw, url):
        self.dom = build_dom(_decode(raw))
        self.title = extract_title(self.dom) or url
        self.text, self.links = render_text(self.dom, url)
        self.forms = _extract_forms(self.dom, url)
        n = 0
        for it in a11y_items(self.dom):
            if it["role"] in ("link", "button", "textbox", "checkbox", "radio", "combobox"):
                n += 1
                it["ref"] = "@e%d" % n
                self.elements.append(it)

    def el(self, ref):
        for e in self.elements:
            if e["ref"] == ref:
                return e
        return None


def _decode(raw):
    for enc in ("utf-8", "iso-8859-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _encode_multipart(fields, files):
    """fields: [(name, value)], files: [(name, path)] -> (body_bytes, content_type)."""
    import uuid
    import mimetypes
    boundary = uuid.uuid4().hex
    buf = bytearray()

    def w(s):
        buf.extend(s if isinstance(s, bytes) else s.encode("utf-8"))

    for k, v in fields:
        w("--%s\r\n" % boundary)
        w('Content-Disposition: form-data; name="%s"\r\n\r\n' % k)
        w(str(v) + "\r\n")
    for k, path in files:
        fname = os.path.basename(path)
        ctype = mimetypes.guess_type(fname)[0] or "application/octet-stream"
        w("--%s\r\n" % boundary)
        w('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (k, fname))
        w("Content-Type: %s\r\n\r\n" % ctype)
        with open(path, "rb") as f:
            buf.extend(f.read())
        w("\r\n")
    w("--%s--\r\n" % boundary)
    return bytes(buf), "multipart/form-data; boundary=%s" % boundary


def _extract_forms(dom, base):
    forms = []
    for fnode in select(dom, "form"):
        inputs = []
        for el in _walk(fnode):
            if not isinstance(el, Node) or el.tag not in ("input", "select", "textarea", "button"):
                continue
            if el.tag == "select":
                opts = [{"value": o.get("value", o.text_content()), "text": o.text_content()[:60]}
                        for o in select(el, "option")]
                inputs.append({"tag": "select", "node": el, "name": el.get("name"),
                               "type": "select", "value": "", "options": opts})
            else:
                inputs.append({"tag": el.tag, "node": el, "name": el.get("name"),
                               "type": el.get("type", "text").lower() if el.tag == "input" else el.tag,
                               "value": el.get("value", "")})
        forms.append({"node": fnode,
                      "action": urllib.parse.urljoin(base, fnode.get("action", "")),
                      "method": fnode.get("method", "get").lower(),
                      "inputs": inputs})
    return forms


def _walk(node):
    if isinstance(node, Node):
        yield node
        for ch in node.children:
            yield from _walk(ch)


FIELD_PATTERNS = {
    "email": ["email", "e-mail", "mail"],
    "username": ["username", "user_name", "login", "userid"],
    "password": ["password", "passwd", "pwd", "sandi"],
    "name": ["fullname", "full_name", "nama_lengkap"],
    "phone": ["phone", "tel", "telepon", "mobile", "hp"],
    "address": ["address", "alamat", "street"],
    "city": ["city", "kota", "town"],
    "search": ["search", "query", "cari", "keyword"],
}


def guess_field(name, label, ftype):
    hay = ("%s %s" % (name, label)).lower()
    if ftype == "email" or "email" in hay:
        return "email"
    if ftype == "password":
        return "password"
    if ftype == "tel":
        return "phone"
    for key, pats in FIELD_PATTERNS.items():
        if any(p in hay for p in pats):
            return key
    return None


class StaticEngine:
    """Pure-Python engine. Honest capabilities: no JS/screenshot/real-storage."""

    name = "static"
    capabilities = {
        "javascript": False,
        "screenshot": False,
        "real_localstorage": False,
        "indexeddb": False,
        "network_interception": False,
        "computed_style": False,
        "bounding_box": False,
        "console_capture": False,
        "video": False,
        "cookies": True,
        "forms": True,
        "downloads": True,
        "xpath": True,
        "css_selectors": True,
    }

    def __init__(self, cookie_file=None, user_agent=UA_DEFAULT,
                 timeout=25, retries=2, proxy=None):
        self.timeout = timeout
        self.retries = retries
        self.ua = user_agent or UA_DEFAULT
        self.proxy = proxy
        self.jar = http.cookiejar.LWPCookieJar(cookie_file) if cookie_file else http.cookiejar.CookieJar()
        if cookie_file:
            try:
                self.jar.load(ignore_discard=True)
            except OSError:
                pass
        self._build_opener()
        self.tabs = [{"history": [], "pos": -1}]
        self.cur = 0
        self.netlog = []   # {id, t, method, url, status, ctype, size, ms, req_headers, res_headers}
        self._req_id = 0
        self._page_req_mark = 0
        self.errors = []   # console-ish error log
        self.downloads = {}
        self._dl_id = 0
        # a11y refs (populated by a11y()/a11y_find(), restored lazily)
        self._a11y_refs = {}
        self._a11y_sig = None
        self._a11y_css = {}
        self._a11y_pending = ({}, None)
        # simulated per-origin storage (real localStorage needs JS engine)
        self._ls = {}
        self._ss = {}

    def _build_opener(self):
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler(
                {"http": self.proxy, "https": self.proxy}))
        else:
            # no proxy: pool keep-alive connections per origin
            handlers.append(_KeepAliveHTTPHandler())
            handlers.append(_KeepAliveHTTPSHandler())
        self.opener = urllib.request.build_opener(*handlers)

    def set_proxy(self, url):
        """url like http://host:port or http://user:pass@host:port; None clears."""
        self.proxy = url or None
        self._build_opener()
        return {"success": True, "proxy": self.proxy}

    def set_user_agent(self, ua):
        self.ua = ua or UA_DEFAULT
        return {"success": True, "ua": self.ua[:60]}

    # ---------- tabs (lazy: history items may be URL strings, fetched on demand) ----------
    def _tab(self):
        return self.tabs[self.cur]

    def _materialize(self, t, idx):
        """Turn a lazy URL string at history[idx] into a Page (fetch now)."""
        url = t["history"][idx]
        try:
            final, status, ctype, raw, _ = self.fetch(url)
        except EngineError:
            return None
        pg = Page(final, status, raw, ctype)
        t["history"][idx] = pg
        return pg

    @property
    def page(self):
        t = self._tab()
        if not (0 <= t["pos"] < len(t["history"])):
            return None
        item = t["history"][t["pos"]]
        if isinstance(item, str):
            return self._materialize(t, t["pos"])
        return item

    def new_tab(self, url=None):
        self.tabs.append({"history": [], "pos": -1})
        self.cur = len(self.tabs) - 1
        if url:
            return self.open(url)
        return {"success": True, "tab": self.cur}

    def switch_tab(self, idx):
        if 0 <= idx < len(self.tabs):
            self.cur = idx
            p = self.page  # materialize a lazy-restored tab now
            t = self._tab()
            if p is None and t["history"]:
                return {"success": False, "tab": idx,
                        "errors": ["failed to load tab page (offline?)"]}
            return {"success": True, "tab": idx}
        return {"success": False, "errors": ["no such tab %d" % idx]}

    def close_tab(self, idx=None):
        idx = self.cur if idx is None else idx
        if len(self.tabs) <= 1:
            return {"success": False, "errors": ["cannot close last tab"]}
        if not (0 <= idx < len(self.tabs)):
            return {"success": False, "errors": ["no such tab"]}
        del self.tabs[idx]
        self.cur = min(self.cur, len(self.tabs) - 1)
        return {"success": True, "tab": self.cur}

    def list_tabs(self):
        out = []
        for i, t in enumerate(self.tabs):
            item = t["history"][t["pos"]] \
                if 0 <= t["pos"] < len(t["history"]) else None
            if isinstance(item, str):
                # lazy-restored tab: show URL without fetching (P0-4)
                out.append({"id": i, "active": i == self.cur,
                            "title": item[:80], "url": item, "lazy": True})
                continue
            p = item
            out.append({"id": i, "active": i == self.cur,
                        "title": p.title[:80] if p else "(empty)",
                        "url": p.url if p else ""})
        return out

    # ---------- fetch ----------
    def _log(self, entry):
        self._req_id += 1
        entry["id"] = self._req_id
        self.netlog.append(entry)
        # cap memory on long sessions (oldest dropped first)
        if len(self.netlog) > 1000:
            del self.netlog[:len(self.netlog) - 1000]
        return entry

    @staticmethod
    def _body_preview(raw, limit=32768):
        """Capped text preview of a response body (never unbounded)."""
        if not raw:
            return "", False
        trunc = len(raw) > limit
        try:
            return raw[:limit].decode("utf-8", errors="replace"), trunc
        except Exception:
            return "", trunc

    # Browser-like request headers (anti-bot hygiene: a bare "User-Agent
    # only" request is itself a bot signal). Callers can override via
    # extra_headers.
    BROWSER_HEADERS = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }

    @staticmethod
    def _decode_body(raw, encoding):
        """Decompress gzip/deflate bodies (we advertise Accept-Encoding)."""
        if not raw or not encoding:
            return raw
        enc = encoding.lower()
        try:
            if "gzip" in enc:
                import gzip as _gz
                return _gz.decompress(raw)
            if "deflate" in enc:
                import zlib as _zl
                return _zl.decompress(raw)
        except Exception:
            pass
        return raw

    def fetch(self, url, data=None, timeout=None, extra_headers=None):
        import datetime
        timeout = timeout or self.timeout
        last_err = None
        for attempt in range(self.retries + 1):
            t0 = time.time()
            headers = {"User-Agent": self.ua}
            headers.update(self.BROWSER_HEADERS)
            headers.update(extra_headers or {})
            req = urllib.request.Request(url, data=data, headers=headers)
            entry = {"t": time.strftime("%H:%M:%S"),
                     "started": datetime.datetime.now().isoformat(timespec="seconds"),
                     "method": "POST" if data else "GET",
                     "url": url, "status": "ERR", "ctype": "-", "size": 0,
                     "ms": 0, "req_headers": dict(req.header_items()), "res_headers": {},
                     "resource_type": "document",
                     "request_size": len(data) if data else 0,
                     "response_size": 0,
                     "attempt": attempt + 1, "attempts": self.retries + 1,
                     "timing": {"start": datetime.datetime.now().isoformat(timespec="seconds"),
                                "duration": 0}}
            if data:
                try:
                    entry["post_data"] = data[:4096].decode("utf-8", errors="replace")
                    entry["post_truncated"] = len(data) > 4096
                except Exception:
                    pass
            try:
                with self.opener.open(req, timeout=timeout) as r:
                    raw = r.read(8_000_000)
                    enc = r.headers.get("Content-Encoding", "")
                    raw = self._decode_body(raw, enc)
                    body, trunc = self._body_preview(raw)
                    entry.update({"url": r.geturl(), "status": r.status,
                                  "ctype": r.headers.get_content_type(), "size": len(raw),
                                  "response_size": len(raw),
                                  "ms": int((time.time() - t0) * 1000),
                                  "res_headers": dict(r.headers.items()),
                                  "res_body": body, "res_truncated": trunc})
                    entry["timing"]["duration"] = entry["ms"]
                    self._log(entry)
                    return r.geturl(), r.status, r.headers.get_content_type(), raw, dict(r.headers.items())
            except urllib.error.HTTPError as e:
                entry.update({"status": e.code, "ms": int((time.time() - t0) * 1000)})
                entry["timing"]["duration"] = entry["ms"]
                try:
                    entry["res_headers"] = dict(e.headers.items())
                    eb = e.read(32768)
                    eb = self._decode_body(
                        eb, e.headers.get("Content-Encoding", ""))
                    entry["res_body"], entry["res_truncated"] = self._body_preview(eb)
                except Exception:
                    pass
                self._log(entry)
                self.errors.append({"type": "http", "url": url, "status": e.code,
                                    "t": entry["t"]})
                raise EngineError("HTTP %d for %s" % (e.code, url))
            except Exception as e:
                last_err = e
                entry.update({"ms": int((time.time() - t0) * 1000),
                              "error": "%s: %s" % (type(e).__name__, str(e)[:120])})
                self._log(entry)
                if attempt < self.retries:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                self.errors.append({"type": "network", "url": url,
                                    "error": "%s: %s" % (type(e).__name__, str(e)[:120]),
                                    "t": entry["t"]})
                raise EngineError("%s: %s" % (type(e).__name__, str(e)[:160]))
        raise EngineError(str(last_err))

    # ---------- navigation ----------
    def open(self, url, data=None):
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
            url = "https://" + url
        # mark netlog position so perf() counts this page's requests only
        self._page_req_mark = self._req_id
        try:
            final, status, ctype, raw, _ = self.fetch(url, data)
        except EngineError as e:
            return {"success": False, "url": url, "errors": [str(e)]}
        page = Page(final, status, raw, ctype)
        t = self._tab()
        t["history"] = t["history"][:t["pos"] + 1] + [page]
        t["pos"] = len(t["history"]) - 1
        return {"success": True, "url": final, "title": page.title, "status": status,
                "tab": self.cur}

    def back(self):
        t = self._tab()
        if t["pos"] > 0:
            t["pos"] -= 1
            p = self.page
            if not p:
                return {"success": False, "errors": ["failed to restore page (offline?)"]}
            url = p.url if hasattr(p, "url") else p
            title = p.title[:80] if hasattr(p, "title") else url
            return {"success": True, "url": url, "title": title}
        return {"success": False, "errors": ["no back history"]}

    def forward(self):
        t = self._tab()
        if t["pos"] < len(t["history"]) - 1:
            t["pos"] += 1
            p = self.page
            if not p:
                return {"success": False, "errors": ["failed to restore page (offline?)"]}
            url = p.url if hasattr(p, "url") else p
            title = p.title[:80] if hasattr(p, "title") else url
            return {"success": True, "url": url, "title": title}
        return {"success": False, "errors": ["no forward history"]}

    def reload(self):
        t = self._tab()
        if not (0 <= t["pos"] < len(t["history"])):
            return {"success": False, "errors": ["no page"]}
        item = t["history"][t["pos"]]
        url = item.url if hasattr(item, "url") else item
        t["history"].pop()
        t["pos"] -= 1
        return self.open(url)

    def history(self):
        t = self._tab()
        out = []
        for i, h in enumerate(t["history"]):
            url = h.url if hasattr(h, "url") else h
            title = (h.title[:80] if hasattr(h, "title") else url) if not isinstance(h, str) else h
            out.append({"pos": i, "current": i == t["pos"], "title": title, "url": url,
                        "loaded": not isinstance(h, str)})
        return out

    # ---------- a11y references ----------
    A11Y_REF_RE = re.compile(r"^@?((?:button|textbox|checkbox|radio|combobox|link|heading)_(\d+))$")

    def resolve_a11y_ref(self, ref):
        """Returns (node_or_None, error_or_None).

        Restores refs lazily from the persisted css map on first use, so
        read-only commands never pay for a page fetch.
        """
        refs = getattr(self, "_a11y_refs", None)
        if not refs:
            self._restore_pending_refs()
            refs = getattr(self, "_a11y_refs", None)
        if not refs or ref not in refs:
            return None, None
        if not self._a11y_sig_valid():
            return None, "reference @%s invalidated (page changed); re-run a11y" % ref
        return refs[ref], None

    def _restore_pending_refs(self):
        css_map, sig = getattr(self, "_a11y_pending", ({}, None))
        self._a11y_pending = ({}, None)
        if not css_map or not sig:
            return
        try:
            p = self.page  # materializes on first access
            if not p or not p.dom:
                return
            from .dom import dom_sig, select as _sel
            if dom_sig(p.dom) != sig:
                return  # page changed -> refs stay invalidated
            refs = {}
            for r, css in css_map.items():
                try:
                    nodes = _sel(p.dom, css)
                except Exception:
                    nodes = []
                if nodes:
                    refs[r] = nodes[0]
            self._a11y_refs = refs
            self._a11y_sig = sig
            self._a11y_css = dict(css_map)
        except Exception:
            pass

    def _a11y_sig_valid(self):
        p = self.page
        if not p or not p.dom:
            return False
        from .dom import dom_sig
        return dom_sig(p.dom) == getattr(self, "_a11y_sig", None)

    # ---------- element resolution & actions ----------
    def resolve(self, query):
        p = self.page
        if not p:
            return {"success": False, "errors": ["no page loaded"]}
        m = self.A11Y_REF_RE.match(query or "")
        if m and p.dom:
            node, err = self.resolve_a11y_ref(m.group(1))
            if err:
                return {"success": False, "errors": [err]}
            if node is None:
                return {"success": False,
                        "errors": ["no such reference @%s; re-run a11y" % m.group(1)]}
            return {"success": True, "method": "a11y-ref", "node": node}
        if not p.dom:
            return {"success": False,
                    "errors": ["page has no DOM (content-type: %s)" % p.ctype],
                    "hint": "the page is not HTML; try extract() or download()"}
        node, method = smart_resolve(p.dom, query)
        if not node:
            return {"success": False, "errors": ["no element matches %r" % query]}
        return {"success": True, "method": method, "node": node}

    def _el_by_ref(self, ref):
        p = self.page
        if p:
            return p, p.el(ref)
        return None, None

    def _form_of(self, node):
        p = self.page
        anc = node.parent
        while anc is not None:
            if isinstance(anc, Node) and anc.tag == "form":
                for i, f in enumerate(p.forms):
                    if f["node"] is anc:
                        return i
                return None
            anc = anc.parent if isinstance(anc, Node) else None
        return None

    def click(self, query):
        """query: @e-ref, CSS selector, or smart text like 'Login'."""
        p = self.page
        if not p:
            return {"success": False, "errors": ["no page"]}
        el = None
        if query.startswith("@e"):
            el = p.el(query)
            if not el:
                return {"success": False, "errors": ["no such element %s" % query]}
        else:
            r = self.resolve(query)
            if not r["success"]:
                return r
            node = r["node"]
            # map node -> element entry if actionable
            for e in p.elements:
                if e["node"] is node:
                    el = e
                    break
            if not el:
                return {"success": False, "errors": ["element is not actionable"]}
        node, role = el["node"], el["role"]
        if role == "link":
            return self.open(urllib.parse.urljoin(p.url, node.get("href")))
        if role == "button":
            fidx = self._form_of(node)
            if fidx is not None:
                return self.submit_form(fidx)
            if node.get("formaction"):
                return self.open(urllib.parse.urljoin(p.url, node.get("formaction")))
            return {"success": False, "errors": ["button needs JavaScript (static engine)"],
                    "hint": "use playwright engine for JS-driven buttons"}
        if role == "checkbox":
            cur_v = p.values.get(el["ref"])
            p.values[el["ref"]] = "" if cur_v else (node.get("value") or "on")
            return {"success": True, "ref": el["ref"], "checked": bool(p.values[el["ref"]])}
        if role == "radio":
            for e2 in p.elements:
                if e2["role"] == "radio" and e2["node"].get("name") == node.get("name"):
                    p.values.pop(e2["ref"], None)
            p.values[el["ref"]] = node.get("value") or "on"
            return {"success": True, "ref": el["ref"], "selected": True}
        return {"success": False, "errors": ["cannot click %s; use type()" % role]}

    def type(self, query, text):
        p, el = self._resolve_el(query)
        if not el:
            return {"success": False, "errors": ["no such textbox %s" % query]}
        if el["role"] != "textbox":
            return {"success": False, "errors": ["%s is %s, not a textbox" % (query, el["role"])]}
        p.values[el["ref"]] = text
        return {"success": True, "ref": el["ref"]}

    def clear(self, query):
        p, el = self._resolve_el(query)
        if not el:
            return {"success": False, "errors": ["no such element %s" % query]}
        p.values[el["ref"]] = ""
        return {"success": True, "ref": el["ref"]}

    def _resolve_el(self, query):
        p = self.page
        if not p:
            return None, None
        if query.startswith("@e"):
            return p, p.el(query)
        r = self.resolve(query)
        if not r["success"]:
            return p, None
        node = r["node"]
        for e in p.elements:
            if e["node"] is node:
                return p, e
        return p, None

    def select_option(self, query, choice):
        p, el = self._resolve_el(query)
        if not el or el["role"] != "combobox":
            return {"success": False, "errors": ["no such dropdown %s" % query]}
        for o in select(el["node"], "option"):
            ot, ov = o.text_content(), o.get("value", o.text_content())
            if choice.lower() in ot.lower() or choice == ov:
                p.values[el["ref"]] = ov
                return {"success": True, "ref": el["ref"], "value": ov}
        return {"success": False, "errors": ["no such option %r" % choice]}

    def check(self, query):
        return self._set_check(query, True)

    def uncheck(self, query):
        return self._set_check(query, False)

    def _set_check(self, query, want):
        p, el = self._resolve_el(query)
        if not el or el["role"] not in ("checkbox", "radio"):
            return {"success": False, "errors": ["no such checkbox/radio %s" % query]}
        node = el["node"]
        if el["role"] == "radio" and want:
            for e2 in p.elements:
                if e2["role"] == "radio" and e2["node"].get("name") == node.get("name"):
                    p.values.pop(e2["ref"], None)
        p.values[el["ref"]] = (node.get("value") or "on") if want else ""
        return {"success": True, "ref": el["ref"], "checked": want}

    def hover(self, query):
        return {"success": False, "errors": ["hover needs a JS engine"],
                "hint": "use playwright engine"}

    def focus(self, query):
        p, el = self._resolve_el(query)
        if not el:
            return {"success": False, "errors": ["no such element %s" % query]}
        return {"success": True, "ref": el["ref"], "note": "static engine: focus is a no-op"}

    def scroll(self, target="bottom"):
        return {"success": False, "errors": ["scroll needs a JS engine for effect"],
                "note": "static engine renders full page text already"}

    # ---------- forms ----------
    def forms(self):
        p = self.page
        if not p:
            return []
        out = []
        for i, fm in enumerate(p.forms):
            out.append({"id": i + 1, "action": fm["action"], "method": fm["method"],
                        "fields": [{"name": x["name"], "type": x["type"],
                                    "label": x.get("label", "")[:40]} for x in fm["inputs"] if x["name"]]})
        return out

    def _collect_fields(self, p, fidx):
        fm = p.forms[fidx]
        fields, files = [], []
        for inp in fm["inputs"]:
            if not inp["name"] or inp["type"] in ("submit", "button"):
                continue
            ref = next((e["ref"] for e in p.elements if e["node"] is inp["node"]), None)
            if inp["type"] == "hidden":
                fields.append((inp["name"], inp["value"]))
            elif ref and ref in p.values:
                v = p.values[ref]
                if isinstance(v, tuple) and v[0] == "@file":
                    if os.path.exists(v[1]):
                        files.append((inp["name"], v[1]))
                    continue
                if inp["type"] == "checkbox":
                    if v:
                        fields.append((inp["name"], v if isinstance(v, str) else "on"))
                elif inp["type"] == "radio":
                    if v:
                        fields.append((inp["name"], v))
                else:
                    fields.append((inp["name"], v))
            elif inp["type"] == "radio":
                if not any(k == inp["name"] for k, _ in fields):
                    fields.append((inp["name"], inp["value"] or "on"))
            elif inp["type"] != "checkbox":
                fields.append((inp["name"], inp["value"]))
        for inp in fm["inputs"]:
            if inp["type"] == "submit" and inp["name"]:
                fields.append((inp["name"], inp["value"]))
        return fm, fields, files

    def submit_form(self, fidx):
        p = self.page
        if not p or not (0 <= fidx < len(p.forms)):
            return {"success": False, "errors": ["no such form"]}
        fm, fields, files = self._collect_fields(p, fidx)
        action = fm["action"] or p.url
        if files:
            # file upload forces POST multipart (per HTML spec)
            body, ctype = _encode_multipart(fields, files)
            try:
                final, status, rctype, raw, _ = self.fetch(
                    action, data=body, timeout=self.timeout,
                    extra_headers={"Content-Type": ctype})
            except EngineError as e:
                return {"success": False, "errors": [str(e)]}
            page = Page(final, status, raw, rctype)
            t = self._tab()
            t["history"] = t["history"][:t["pos"] + 1] + [page]
            t["pos"] = len(t["history"]) - 1
            return {"success": True, "url": final, "title": page.title,
                    "status": status, "tab": self.cur,
                    "note": "multipart upload (%d file(s))" % len(files)}
        if fm["method"] == "post":
            return self.open(action, urllib.parse.urlencode(fields).encode())
        qs = urllib.parse.urlencode(fields)
        sep = "&" if urllib.parse.urlparse(action).query else "?"
        return self.open(action + sep + qs)

    def fill_form(self, fidx, values=None, auto=False, profile=None):
        """values: {field_name: value}. '@' prefix + existing path = file upload."""
        p = self.page
        if not p or not (0 <= fidx < len(p.forms)):
            return {"success": False, "errors": ["no such form"]}
        fm = p.forms[fidx]
        values = values or {}
        filled = []
        for inp in fm["inputs"]:
            if not inp["name"] or inp["type"] in ("submit", "button", "hidden"):
                continue
            ref = next((e["ref"] for e in p.elements if e["node"] is inp["node"]), None)
            if not ref:
                continue
            if inp["name"] in values:
                v = values[inp["name"]]
                if isinstance(v, str) and v.startswith("@") and os.path.exists(v[1:]):
                    p.values[ref] = ("@file", v[1:])
                    filled.append("%s<=file %s" % (inp["name"], v[1:]))
                else:
                    p.values[ref] = v
                    filled.append(inp["name"])
            elif auto and profile:
                key = guess_field(inp["name"], inp.get("label", ""), inp["type"])
                if key and key in profile:
                    p.values[ref] = profile[key]
                    filled.append("%s<-%s" % (inp["name"], key))
        return {"success": True, "filled": filled, "form": fidx + 1}

    # ---------- javascript (not supported) ----------
    def evaluate(self, js):
        return {"success": False, "supported": False,
                "errors": ["JavaScript needs the playwright engine"],
                "hint": "use engine='playwright' or run: browser js --engine playwright"}

    def wait(self, selector=None, ms=None, text=None):
        if ms:
            time.sleep(ms / 1000)
            return {"success": True, "waited_ms": ms}
        return {"success": True, "note": "static engine: DOM is complete after load; no waiting needed"}

    def console(self):
        return {"success": True, "logs": [],
                "note": "no JS console on static engine; see errors() for network errors"}

    # ---------- screenshot (not supported) ----------
    def screenshot(self, path=None, full=False, selector=None):
        return {"success": False, "supported": False,
                "errors": ["screenshots need the playwright engine"]}

    # ---------- storage ----------
    def _origin(self):
        p = self.page
        return urllib.parse.urlparse(p.url).netloc if p else "global"

    def cookies(self):
        out = []
        for c in self.jar:
            out.append({"name": c.name, "value": c.value[:60], "domain": c.domain,
                        "path": c.path, "secure": c.secure,
                        "expires": c.expires})
        return out

    def storage(self, kind="local"):
        store = self._ls if kind == "local" else self._ss
        return dict(store.get(self._origin(), {}))

    def storage_get(self, key, kind="local"):
        return (self._ls if kind == "local" else self._ss).get(self._origin(), {}).get(key)

    def storage_set(self, key, value, kind="local"):
        store = self._ls if kind == "local" else self._ss
        store.setdefault(self._origin(), {})[key] = value
        return {"success": True, "key": key}

    def storage_delete(self, key, kind="local"):
        store = self._ls if kind == "local" else self._ss
        store.get(self._origin(), {}).pop(key, None)
        return {"success": True, "key": key}

    # ---------- network ----------
    def network(self, pattern=None, limit=50, type_=None, status=None, method=None):
        out = self.netlog[-limit:]

        def _status_match(e):
            if not status:
                return True
            s = str(status).lower()
            code = e.get("status")
            if not isinstance(code, int):
                return False
            if s.endswith("xx") and len(s) == 3 and s[0].isdigit():
                return code // 100 == int(s[0])
            try:
                return code == int(s)
            except ValueError:
                return False

        if type_:
            out = [e for e in out if (e.get("resource_type") or "").lower() == type_.lower()]
        if status:
            out = [e for e in out if _status_match(e)]
        if method:
            out = [e for e in out if (e.get("method") or "").upper() == method.upper()]
        if pattern:
            rx = re.compile(pattern, re.I)
            out = [e for e in out if rx.search(e["url"]) or rx.search(e.get("ctype", ""))]
        return out

    def network_clear(self):
        self.netlog = []
        return {"success": True}

    def request(self, rid):
        for e in self.netlog:
            if e["id"] == rid:
                return e
        return None

    def network_response(self, rid, max_bytes=65536):
        """Response inspection: pretty JSON when applicable, raw capped otherwise."""
        e = self.request(rid)
        if not e:
            return {"success": False, "errors": ["no such request id %s" % rid]}
        body = e.get("res_body", "")
        ctype = e.get("ctype", "")
        out = {"success": True, "id": rid, "url": e["url"], "status": e["status"],
               "ctype": ctype, "size": e.get("response_size", e.get("size", 0)),
               "truncated": e.get("res_truncated", False)}
        if "res_body" not in e:
            # entry restored from state.json (bodies aren't persisted there)
            out["note"] = ("response body not persisted across restarts; "
                           "re-open the URL to inspect it")
            out["format"] = "unavailable"
            return out
        if "json" in ctype and body:
            try:
                out["json"] = json.loads(body)
                out["format"] = "json"
            except Exception:
                out["format"] = "text"
                out["body"] = body[:max_bytes]
        else:
            out["format"] = "text"
            out["body"] = body[:max_bytes]
        return out

    def network_request_detail(self, rid):
        e = self.request(rid)
        if not e:
            return {"success": False, "errors": ["no such request id %s" % rid]}
        return {"success": True, "id": rid, "method": e.get("method"),
                "url": e.get("url"), "resource_type": e.get("resource_type"),
                "request_size": e.get("request_size", 0),
                "headers": e.get("req_headers", {}),
                "post_data": e.get("post_data"),
                "post_truncated": e.get("post_truncated", False)}

    # ---------- perf ----------
    def perf(self, url=None):
        """Timing breakdown. Direct socket measurement when possible;
        honest urllib fallback when behind a proxy (DNS/connect not separable)."""
        url = url or (self.page.url if self.page else None)
        if not url:
            return {"success": False, "errors": ["no url"]}
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
            url = "https://" + url
        proxied = any(os.environ.get(k) for k in
                      ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"))
        if not proxied:
            r = self._perf_direct(url)
            if r["success"]:
                return r
        return self._perf_urllib(url, proxied)

    def _perf_direct(self, url):
        import http.client
        u = urllib.parse.urlparse(url)
        host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        t = {}
        t0 = time.time()
        try:
            t1 = time.time()
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            t["dns_ms"] = int((time.time() - t1) * 1000)
            fam, st, proto, _, addr = infos[0]
            t2 = time.time()
            s = socket.socket(fam, st, proto)
            s.settimeout(self.timeout)
            s.connect(addr)
            t["connect_ms"] = int((time.time() - t2) * 1000)
            if u.scheme == "https":
                import ssl
                t3 = time.time()
                s = ssl.create_default_context().wrap_socket(s, server_hostname=host)
                t["tls_ms"] = int((time.time() - t3) * 1000)
            t4 = time.time()
            s.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\nConnection: close\r\n\r\n"
                       % (path, host, self.ua)).encode())
            first = s.recv(1)
            t["ttfb_ms"] = int((time.time() - t4) * 1000)
            if not first:
                raise EngineError("empty response")
            t5 = time.time()
            size = 1
            while True:
                d = s.recv(65536)
                if not d:
                    break
                size += len(d)
            t["download_ms"] = int((time.time() - t5) * 1000)
            s.close()
            t["total_ms"] = int((time.time() - t0) * 1000)
            t["bytes"] = size
            t["requests"] = self._page_requests()
            t["requests_note"] = "requests since this page was opened"
            return {"success": True, "method": "direct-socket", **t, "url": url}
        except Exception as e:
            return {"success": False,
                    "errors": ["%s: %s" % (type(e).__name__, str(e)[:120])]}

    def _page_requests(self):
        """Requests logged since the current page was opened (not cumulative)."""
        mark = getattr(self, "_page_req_mark", 0)
        return sum(1 for e in self.netlog if e.get("id", 0) > mark)

    def _perf_urllib(self, url, proxied):
        t0 = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": self.ua})
            with self.opener.open(req, timeout=self.timeout) as r:
                t1 = time.time()
                first = r.read(1)
                ttfb = int((time.time() - t1) * 1000)
                size = len(first)
                t2 = time.time()
                while True:
                    d = r.read(65536)
                    if not d:
                        break
                    size += len(d)
                download = int((time.time() - t2) * 1000)
            return {"success": True, "method": "urllib-fallback",
                    "note": "behind proxy: dns/connect not separable; ttfb includes proxy",
                    "dns_ms": None, "connect_ms": None, "tls_ms": None,
                    "ttfb_ms": ttfb, "download_ms": download,
                    "total_ms": int((time.time() - t0) * 1000),
                    "bytes": size, "url": url,
                    "requests": self._page_requests(),
                    "requests_note": "requests since this page was opened "}
        except Exception as e:
            return {"success": False,
                    "errors": ["%s: %s" % (type(e).__name__, str(e)[:120])]}

    # ---------- downloads ----------
    def download(self, url, path=None):
        self._dl_id += 1
        did = self._dl_id
        name = os.path.basename(urllib.parse.urlparse(url).path.split("?")[0]) or ("file-%d.bin" % did)
        entry = {"id": did, "url": url,
                 "path": path or os.path.join(_dl_dir(), name),
                 "total": None, "done": 0, "status": "queued",
                 "sha256": None, "stop": False, "ctype": None}
        self.downloads[did] = entry
        threading.Thread(target=self._dl_worker, args=(entry,), daemon=True).start()
        return {"success": True, "id": did, "path": entry["path"]}

    def _dl_worker(self, e):
        e["status"] = "running"
        try:
            start = os.path.getsize(e["path"]) if os.path.exists(e["path"]) else 0
            req = urllib.request.Request(e["url"], headers={"User-Agent": self.ua})
            if start:
                req.add_header("Range", "bytes=%d-" % start)
            with self.opener.open(req, timeout=120) as r:
                cr, total = r.headers.get("Content-Range"), r.headers.get("Content-Length")
                if start and r.status == 206 and cr:
                    e["total"] = int(cr.split("/")[-1])
                    e["done"] = start
                    mode = "ab"
                else:
                    if start:
                        e.setdefault("notes", []).append("server ignored resume; restarting")
                    start, e["done"] = 0, 0
                    e["total"] = int(total) if total else None
                    mode = "wb"
                e["ctype"] = r.headers.get_content_type()
                h = hashlib.sha256()
                if mode == "ab":
                    with open(e["path"], "rb") as f:
                        for ch in iter(lambda: f.read(65536), b""):
                            h.update(ch)
                with open(e["path"], mode) as f:
                    while True:
                        if e["stop"]:
                            e["status"] = "paused"
                            return
                        ch = r.read(65536)
                        if not ch:
                            break
                        f.write(ch)
                        h.update(ch)
                        e["done"] += len(ch)
            e["sha256"] = h.hexdigest()
            e["status"] = "done"
        except Exception as ex:
            e["status"] = "error: %s" % str(ex)[:120]

    def download_pause(self, did):
        e = self.downloads.get(did)
        if e:
            e["stop"] = True
            return {"success": True, "id": did}
        return {"success": False, "errors": ["no such download"]}

    def download_resume(self, did):
        e = self.downloads.get(did)
        if e and e["status"] != "running":
            e["stop"] = False
            threading.Thread(target=self._dl_worker, args=(e,), daemon=True).start()
            return {"success": True, "id": did}
        return {"success": False, "errors": ["no such download or already running"]}

    def download_list(self):
        out = []
        for did in sorted(self.downloads):
            e = self.downloads[did]
            out.append({"id": did, "file": os.path.basename(e["path"]), "path": e["path"],
                        "done": e["done"], "total": e["total"], "status": e["status"],
                        "sha256": e["sha256"], "ctype": e.get("ctype")})
        return out

    def errors(self):
        return self.errors[-50:]

    def close(self):
        try:
            if hasattr(self.jar, "filename") and self.jar.filename:
                self.jar.save(ignore_discard=True)
        except OSError:
            pass
        # drop pooled keep-alive connections
        try:
            for h in self.opener.handlers:
                close_idle = getattr(h, "close_idle", None)
                if close_idle:
                    close_idle()
        except Exception:
            pass


def _dl_dir():
    d = os.path.join(os.path.expanduser("~"), "browser", "downloads")
    os.makedirs(d, exist_ok=True)
    return d
