"""Zombie-tab watchdog: deteksi + recovery otomatis.

Belang yang ditemukan di verifikasi live 2026-10-05: satu tab yang loading
permanen bikin SEMUA evaluate() mengembalikan null — dan recovery-nya masih
manual (tutup tab + buka baru). Modul ini mengotomatisasi itu.

Tanda zombie (webview): probe JS konstan `1+1` mengembalikan null —
ekspresi yang tidak mungkin null secara legitimate. Satu null bisa jadi
kebetulan; streak null = zombie.

Dipakai manual:
    kancil.tool({"action": "health_check"})
    kancil.tool({"action": "recover_zombie"})

Dipakai otomatis: autopilot memanggil ensure() sebelum tiap step.
"""

PROBE_JS = "1+1"
NULL_STREAK_LIMIT = 3


def probe(kancil):
    """Probe responsivitas: {responsive, zombie_suspect, result, note}."""
    if kancil._engine_name == "static":
        return {"responsive": True, "zombie_suspect": False,
                "note": "static engine: halaman complete setelah load; "
                        "konsep zombie tidak berlaku"}
    r = kancil.evaluate(PROBE_JS)
    if not r.get("success"):
        return {"responsive": False, "zombie_suspect": False,
                "note": "evaluate gagal: %s"
                        % str(r.get("errors") or ["?"])[:120]}
    res = r.get("result")
    if res is None:
        return {"responsive": False, "zombie_suspect": True,
                "note": "probe '%s' mengembalikan null" % PROBE_JS}
    ok = str(res).strip() in ("2", "2.0")
    return {"responsive": ok, "zombie_suspect": False, "result": res}


def check(kancil):
    """Health check lengkap: probe + null-streak + info tab."""
    p = probe(kancil)
    streak = getattr(kancil, "_null_streak", 0)
    out = {"success": True, "engine": kancil._engine_name,
           "responsive": p["responsive"],
           "zombie_suspect": p["zombie_suspect"] or
           streak >= NULL_STREAK_LIMIT,
           "null_streak": streak}
    if p.get("note"):
        out["note"] = p["note"]
    try:
        tabs = kancil.tabs().get("tabs", [])
        cur = [t for t in tabs if t.get("current")]
        out["tabs"] = len(tabs)
        if cur:
            out["current_tab"] = {"id": cur[0].get("id"),
                                  "url": cur[0].get("url", "")[:120]}
    except Exception:
        pass
    return out


def recover(kancil, verify=None):
    """Recovery zombie: ingat URL -> tutup tab -> tab baru -> buka URL.

    Returns hasil open() + info recovery. Gagal jujur kalau tidak ada
    URL yang bisa dipulihkan.
    """
    url = ""
    tab_id = None
    try:
        tabs = kancil.tabs().get("tabs", [])
        cur = [t for t in tabs if t.get("current")]
        if cur:
            tab_id = cur[0].get("id")
            url = cur[0].get("url", "")
    except Exception:
        pass
    if not url:
        # fallback: url dari engine page
        try:
            pg = kancil.engine.page
            url = getattr(pg, "url", "") or ""
        except Exception:
            url = ""
    if not url or url in ("about:blank", ""):
        return {"success": False, "recovered": False,
                "errors": ["tidak ada URL untuk dipulihkan"]}
    # tutup tab zombie (abaikan error — tab-nya memang rusak)
    try:
        kancil.close_tab(tab_id)
    except Exception:
        pass
    try:
        kancil.new_tab()
    except Exception:
        pass
    kancil._null_streak = 0
    r = kancil.open(url, settle=True,
                    verify=verify) if verify else kancil.open(url, settle=True)
    out = {"success": bool(r.get("success")), "recovered": True,
           "url": url, "open_result": r}
    if not r.get("success"):
        out["errors"] = r.get("errors") or ["open gagal setelah recovery"]
    return out


def ensure(kancil, verify=None):
    """Dipanggil autopilot sebelum tiap step: cek + recovery bila perlu.

    Returns {"ok": True, "recovered": bool}.
    """
    c = check(kancil)
    if not c["zombie_suspect"]:
        return {"ok": True, "recovered": False}
    r = recover(kancil, verify=verify)
    return {"ok": bool(r.get("success")), "recovered": True,
            "detail": r}
