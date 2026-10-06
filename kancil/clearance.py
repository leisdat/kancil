"""Cache cf_clearance per host + TTL.

Ide dari lingxudr/specter (cf_persistent.py): setelah challenge lolos
sekali (klik Turnstile / manual), cookie clearance disimpan; kunjungan
berikutnya ke host yang sama dalam masa TTL langsung inject cookie —
tanpa challenge ulang.

Store: ~/.kancil/clearance.json (0600):
  {host: {"cookies": {name: value}, "expires": ts, "saved_at": ...}}
"""

import json
import os
import time

CLEARANCE_COOKIES = ("cf_clearance", "__cf_bm", "cf_bm")
DEFAULT_TTL = 1800


def _store_path():
    d = os.path.expanduser("~/.kancil")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return os.path.join(d, "clearance.json")


def _read():
    p = _store_path()
    try:
        with open(p) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    p = _store_path()
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def host_of(url):
    """example.com dari URL."""
    u = (url or "").strip()
    if "://" in u:
        u = u.split("://", 1)[1]
    return u.split("/")[0].split(":")[0].lower().lstrip(".")


def save(host, cookies, ttl_s=DEFAULT_TTL):
    """Simpan cookie clearance untuk host. cookies: {name: value}."""
    host = (host or "").lower()
    if not host or not cookies:
        return False
    data = _read()
    data[host] = {"cookies": dict(cookies),
                  "expires": time.time() + max(60, int(ttl_s)),
                  "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    # buang yang kedaluwarsa sekalian
    now = time.time()
    data = {h: v for h, v in data.items()
            if v.get("expires", 0) > now}
    _write(data)
    return True


def load(host):
    """Ambil cookie clearance yang masih fresh; None bila tidak ada."""
    host = (host or "").lower()
    e = _read().get(host)
    if not e or e.get("expires", 0) <= time.time():
        return None
    return dict(e.get("cookies", {}))


def clear(host=None):
    """Hapus cache satu host (atau semua bila host None)."""
    data = _read()
    if host:
        data.pop((host or "").lower(), None)
    else:
        data = {}
    _write(data)
    return True


def save_from_kancil(kancil, host=None):
    """Baca cookie engine, simpan yang clearance untuk host."""
    if host is None:
        try:
            tabs = kancil.tabs().get("tabs", [])
            cur = [t for t in tabs if t.get("current")]
            url = cur[0].get("url", "") if cur else ""
        except Exception:
            url = ""
        host = host_of(url)
    if not host:
        return {"success": False, "errors": ["host tidak diketahui"]}
    try:
        all_cookies = kancil.engine.cookies() or []
    except Exception as e:
        return {"success": False, "errors": [str(e)[:150]]}
    jar = {}
    for c in all_cookies:
        n = (c.get("name") or "")
        if n in CLEARANCE_COOKIES and c.get("value"):
            jar[n] = c["value"]
    if not jar:
        return {"success": False, "errors": [
            "tidak ada cookie clearance (cf_clearance/__cf_bm)"]}
    save(host, jar)
    return {"success": True, "host": host,
            "cookies": sorted(jar), "ttl_s": DEFAULT_TTL}


def inject_to_kancil(kancil, host=None, url=None):
    """Inject cookie clearance cache ke engine (webview)."""
    if host is None:
        host = host_of(url or "")
    if not host:
        return {"success": False, "errors": ["host tidak diketahui"]}
    jar = load(host)
    if not jar:
        return {"success": False, "errors": ["tidak ada cache fresh"],
                "host": host}
    if kancil._engine_name != "webview":
        return {"success": False, "errors": [
            "inject butuh engine webview (cookies_set)"]}
    injected, failed = [], []
    for name, value in jar.items():
        try:
            r = kancil.cookies_set(name, value, domain=host)
            (injected if r.get("success") else failed).append(name)
        except Exception:
            failed.append(name)
    return {"success": not failed, "host": host,
            "injected": injected, "failed": failed,
            "errors": ["gagal inject: %s" % ",".join(failed)] if failed
                      else []}
