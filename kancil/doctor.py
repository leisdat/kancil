"""kancil doctor — one-command health check for every engine.

Each check returns a dict: name, status (ok/warn/fail), detail, hint.
Run from CLI (`kancil doctor`) or API (`Kancil(...).doctor()`).
Stdlib only.
"""

import os
import shutil
import socket
import sys
import time
import urllib.request


def _check_python():
    ok = sys.version_info >= (3, 8)
    return {
        "name": "python",
        "status": "ok" if ok else "fail",
        "detail": "Python %s" % sys.version.split()[0],
        "hint": "" if ok else "butuh Python 3.8+",
    }


def _check_static(timeout=10):
    t0 = time.time()
    try:
        req = urllib.request.Request(
            "https://example.com",
            headers={"User-Agent": "kancil-doctor/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            code = r.status
        ms = int((time.time() - t0) * 1000)
        ok = code == 200
        return {
            "name": "static engine",
            "status": "ok" if ok else "fail",
            "detail": "example.com -> HTTP %d (%d ms)" % (code, ms),
            "hint": "" if ok else "cek koneksi internet",
        }
    except Exception as e:
        return {
            "name": "static engine",
            "status": "fail",
            "detail": "tidak bisa fetch: %s" % str(e)[:80],
            "hint": "cek koneksi internet / DNS",
        }


def _check_playwright():
    try:
        import playwright  # noqa: F401
    except ImportError:
        return {
            "name": "playwright engine",
            "status": "fail",
            "detail": "package playwright tidak ter-install",
            "hint": ("di Termux butuh proot Ubuntu dulu, lalu "
                     "pip install playwright && playwright install chromium"),
        }
    # package ada — cari browser binary
    found = None
    for b in ("chromium", "chromium-browser", "google-chrome", "firefox"):
        p = shutil.which(b)
        if p:
            found = p
            break
    home = os.path.expanduser("~")
    msplay = os.path.join(home, ".cache", "ms-playwright")
    if not found and os.path.isdir(msplay):
        try:
            for d in os.listdir(msplay):
                if d.startswith("chromium"):
                    found = os.path.join(msplay, d)
                    break
        except OSError:
            pass
    if found:
        return {"name": "playwright engine", "status": "ok",
                "detail": "playwright OK, browser: %s" % found, "hint": ""}
    return {"name": "playwright engine", "status": "warn",
            "detail": "playwright ter-install tapi browser binary tidak ketemu",
            "hint": "jalankan: playwright install chromium"}


def _check_webview(timeout=5):
    base = "http://127.0.0.1:8080"
    try:
        req = urllib.request.Request(base + "/status")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            import json
            st = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return [{
            "name": "webview engine",
            "status": "fail",
            "detail": "agent server tidak menjawab di 127.0.0.1:8080: %s"
                     % str(e)[:60],
            "hint": "buka aplikasi Kancil Browser di HP dulu",
        }]
    agent = st.get("agent", "?")
    tabs = st.get("tabs", "?")
    url = st.get("url", "")
    out = [{
        "name": "webview engine",
        "status": "ok",
        "detail": "agent %s, %s tab(s)%s"
                  % (agent, tabs, (" — " + url[:60]) if url else ""),
        "hint": "",
    }]
    # cookie signal: ada sesi login di halaman aktif?
    try:
        req = urllib.request.Request(base + "/cookies")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            import json
            cj = json.loads(r.read().decode("utf-8", "replace"))
        n = len(cj.get("cookies", []))
        out.append({
            "name": "webview session",
            "status": "ok" if n else "warn",
            "detail": ("%d cookie di halaman aktif — kemungkinan sudah login"
                       % n) if n else "0 cookie di halaman aktif",
            "hint": ("" if n else "login manual sekali di app "
                     "(YouTube/Facebook) biar agent pakai sesi itu"),
        })
    except Exception:
        pass
    return out


def _check_disk():
    try:
        total, used, free = shutil.disk_usage(os.path.expanduser("~"))
        mb = free // (1024 * 1024)
        ok = mb > 100
        return {
            "name": "disk",
            "status": "ok" if ok else "warn",
            "detail": "%d MB free" % mb,
            "hint": "" if ok else "ruang sempit — cache kancil bisa membengkak",
        }
    except Exception as e:
        return {"name": "disk", "status": "warn",
                "detail": str(e)[:60], "hint": ""}


def _check_env():
    termux = "com.termux" in os.environ.get("PREFIX", "")
    rooted = os.geteuid() == 0 if hasattr(os, "geteuid") else False
    detail = []
    if termux:
        detail.append("Termux")
    if rooted:
        detail.append("root")
    return {
        "name": "environment",
        "status": "ok",
        "detail": ", ".join(detail) if detail else "Linux biasa",
        "hint": ("playwright butuh proot di Termux" if termux and not rooted
                 else ""),
    }


def _check_dns():
    try:
        socket.gethostbyname("example.com")
        return {"name": "dns", "status": "ok",
                "detail": "resolve OK", "hint": ""}
    except Exception as e:
        return {"name": "dns", "status": "fail",
                "detail": "gagal resolve: %s" % str(e)[:60],
                "hint": "cek DNS / koneksi"}


def run():
    """Run all checks. Returns (checks, all_ok)."""
    checks = [_check_python(), _check_dns(), _check_static(),
              _check_playwright()]
    checks.extend(_check_webview())
    checks.extend([_check_disk(), _check_env()])
    all_ok = all(c["status"] != "fail" for c in checks)
    return checks, all_ok


def format_text(checks):
    icons = {"ok": "✓", "warn": "!", "fail": "✗"}
    lines = []
    for c in checks:
        lines.append("%s %-16s %s" % (icons.get(c["status"], "?"),
                                     c["name"], c["detail"]))
        if c.get("hint"):
            lines.append("  → %s" % c["hint"])
    return "\n".join(lines)
