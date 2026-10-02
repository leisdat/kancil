"""Tiny HTTP cache for the static engine.

GET-only, ETag / Last-Modified revalidation. Stored under
~/.kancil/cache/<sha1>.{meta,body}. Respects Cache-Control: no-store.
A 304 response serves the cached body without re-downloading.

No expiry heuristics: entries without validators are never stored, so
every cache hit is revalidated (cheap 304) — stale data is impossible.
"""

import hashlib
import json
import os
import time

MAX_TOTAL = 50 * 1024 * 1024  # 50 MB cap for the whole cache dir


def cache_dir():
    from . import session as session_mod
    d = os.path.join(session_mod.BASE, "cache")
    os.makedirs(d, exist_ok=True)
    return d


def _key(url):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def _paths(url):
    d = cache_dir()
    k = _key(url)
    return (os.path.join(d, k + ".meta"), os.path.join(d, k + ".body"))


def lookup(url):
    """Return (meta, body_bytes) or (None, None)."""
    mp, bp = _paths(url)
    try:
        with open(mp, encoding="utf-8") as f:
            meta = json.load(f)
        with open(bp, "rb") as f:
            body = f.read()
        if meta.get("url") != url:
            return None, None
        return meta, body
    except Exception:
        return None, None


def conditional_headers(url):
    """If-None-Match / If-Modified-Since for a cached URL."""
    meta, _ = lookup(url)
    h = {}
    if meta:
        if meta.get("etag"):
            h["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            h["If-Modified-Since"] = meta["last_modified"]
    return h


def cached_response(url):
    """(final_url, 200, ctype, body, headers) from cache, or None."""
    meta, body = lookup(url)
    if not meta or body is None:
        return None
    return (meta.get("final_url", url), 200, meta.get("ctype", ""),
            body, meta.get("headers", {}))


def _total_size(d):
    total = 0
    try:
        for f in os.listdir(d):
            try:
                total += os.path.getsize(os.path.join(d, f))
            except OSError:
                pass
    except OSError:
        pass
    return total


def _evict_if_needed(d):
    if _total_size(d) < MAX_TOTAL:
        return
    try:
        files = [(os.path.getmtime(os.path.join(d, f)), f)
                 for f in os.listdir(d)]
    except OSError:
        return
    files.sort()
    for _, f in files:
        try:
            os.remove(os.path.join(d, f))
        except OSError:
            pass
        if _total_size(d) < MAX_TOTAL:
            break


def store(url, final_url, status, headers, body):
    """Store a GET response. Only 200s with validators; honors no-store."""
    if status != 200 or not body:
        return
    h = {k.lower(): v for k, v in headers.items()}
    cc = h.get("cache-control", "").lower()
    if "no-store" in cc:
        return
    etag = h.get("etag")
    last_mod = h.get("last-modified")
    if not etag and not last_mod:
        return  # nothing to revalidate with -> don't store
    try:
        d = cache_dir()
        _evict_if_needed(d)
        mp, bp = _paths(url)
        meta = {"url": url, "final_url": final_url,
                "ctype": h.get("content-type", "").split(";")[0].strip(),
                "etag": etag, "last_modified": last_mod,
                "headers": dict(headers), "saved": int(time.time())}
        with open(mp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        with open(bp, "wb") as f:
            f.write(body)
    except Exception:
        pass


def clear():
    """Wipe the cache. Returns number of files removed."""
    n = 0
    try:
        d = cache_dir()
        for f in os.listdir(d):
            try:
                os.remove(os.path.join(d, f))
                n += 1
            except OSError:
                pass
    except OSError:
        pass
    return n


def stats():
    """(file_count, total_bytes)."""
    try:
        d = cache_dir()
        files = os.listdir(d)
        return len(files), _total_size(d)
    except OSError:
        return 0, 0
