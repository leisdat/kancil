"""Stuck detector: kenali halaman yang menghentikan autopilot.

Jenis:
- captcha      -> butuh manusia (pause + lapor). Jangan ditebak.
- login_wall   -> butuh login manual / vault restore.
- consent      -> BISA di-auto-klik (tombol Setuju/Accept).
- paywall      -> laporkan, tidak bisa dilewati.
- challenge    -> Cloudflare "just a moment" — tunggu lalu cek ulang.

Dipakai manual via check_stuck()/resolve_stuck(), dan otomatis oleh
autopilot sebelum tiap step.
"""

PATTERNS = {
    "captcha": [
        "i'm not a robot", "i am not a robot", "recaptcha",
        "verify you are human", "verify you're human",
        "select all images", "pilih semua gambar",
        "slide to verify", "geser untuk verifikasi",
        "drag the slider", "nocaptcha", "cf-turnstile", "turnstile",
        "arkose", "funcaptcha", "hcap", "verifikasi keamanan",
        "complete the puzzle", "selesaikan puzzle",
    ],
    "login_wall": [
        "log in to continue", "login to continue",
        "sign in to continue", "masuk untuk melanjutkan",
        "login dulu", "silakan masuk", "please log in",
        "daftar atau masuk",
    ],
    "consent": [
        "accept cookies", "accept all cookies", "accept all",
        "terima semua", "setuju", "we value your privacy",
        "we use cookies", "cookie consent", "agree and continue",
        "setuju dan lanjutkan", "izinkan semua",
    ],
    "paywall": [
        "subscribe to continue", "berlangganan untuk",
        "this article is for subscribers", "konten premium",
        "paywall", "lanjutkan membaca dengan berlangganan",
    ],
    "challenge": [
        "checking your browser", "just a moment",
        "verifying you are human", "memeriksa browser anda",
        "one more step",
    ],
}

# prioritas: yang paling blocking duluan
PRIORITY = ["captcha", "challenge", "login_wall", "paywall", "consent"]

# tombol consent yang aman di-klik (teks visible / selector umum)
CONSENT_BUTTONS = [
    "Accept all", "Terima semua", "Setuju", "Agree", "OK", "Got it",
    "Mengerti", "Setuju dan lanjutkan", "Izinkan semua",
    "[id*='accept'][id*='cookie']", "[class*='accept'][class*='cookie']",
    "#accept-cookies", ".accept-cookies",
]


def detect(text, url=""):
    """text: teks halaman (lower di dalam). -> {stuck, kinds[], evidence}."""
    t = (text or "").lower()
    kinds = []
    evidence = {}
    for kind in PRIORITY:
        hits = [p for p in PATTERNS[kind] if p in t]
        if hits:
            kinds.append(kind)
            evidence[kind] = hits[:3]
    return {"stuck": bool(kinds), "kinds": kinds, "evidence": evidence,
            "primary": kinds[0] if kinds else None}


def page_text(kancil, limit=6000):
    """Ambil teks halaman lintas engine."""
    try:
        if kancil._engine_name == "static":
            pg = kancil.engine.page
            return (getattr(pg, "text", "") or "")[:limit]
        r = kancil.evaluate(
            "(function(){try{return (document.body?document.body.innerText"
            ":\"\").slice(0,%d)}catch(e){return \"\"}})()" % limit)
        if r.get("success") and r.get("result"):
            return str(r["result"])
    except Exception:
        pass
    return ""


def page_html(kancil, limit=30000):
    """Ambil HTML halaman (untuk signature provider) lintas engine."""
    try:
        if kancil._engine_name == "static":
            pg = kancil.engine.page
            dom = getattr(pg, "dom", None)
            if dom is not None:
                from .dom import outer_html as _oh
                return _oh(dom, limit=limit)
            return ""
        r = kancil.evaluate(
            "(function(){try{return document.documentElement.outerHTML"
            ".slice(0,%d)}catch(e){return \"\"}})()" % limit)
        if r.get("success") and r.get("result"):
            return str(r["result"])
    except Exception:
        pass
    return ""


def check(kancil):
    """Cek apakah halaman stuck. -> detect() + provider + url."""
    from . import providers as _pv
    url = ""
    try:
        tabs = kancil.tabs().get("tabs", [])
        cur = [t for t in tabs if t.get("current")]
        if cur:
            url = cur[0].get("url", "")
    except Exception:
        pass
    if not url:
        try:
            url = getattr(kancil.engine.page, "url", "") or ""
        except Exception:
            url = ""
    d = detect(page_text(kancil), url)
    d["url"] = url
    d["success"] = True
    if d.get("stuck"):
        html = page_html(kancil)
        prov = _pv.detect_provider(body=html)
        d["provider"] = prov["provider"]
        d["provider_confidence"] = prov["confidence"]
        if prov["provider"]:
            d["provider_hint"] = prov["hint"]
            d["provider_solvable"] = prov["solvable"]
    return d


TURNSTILE_JS = """(function(){
  var out = [];
  var ifs = document.querySelectorAll('iframe');
  for (var i = 0; i < ifs.length; i++) {
    var f = ifs[i], s = (f.src || '').toLowerCase();
    if (s.indexOf('turnstile') >= 0 ||
        s.indexOf('challenges.cloudflare.com') >= 0) {
      var r = f.getBoundingClientRect();
      if (r.width > 10 && r.height > 10)
        out.push({x: r.x + r.width/2, y: r.y + r.height/2});
    }
  }
  if (!out.length) {
    var els = document.querySelectorAll(
      '.cf-turnstile iframe, div.cf-turnstile');
    for (var j = 0; j < els.length; j++) {
      var b = els[j].getBoundingClientRect();
      if (b.width > 10 && b.height > 10)
        out.push({x: b.x + b.width/2, y: b.y + b.height/2});
    }
  }
  return JSON.stringify(out);
})()"""


def solve_turnstile(kancil, timeout=25):
    """Klik checkbox Cloudflare Turnstile (webview).

    BUKAN bypass — sama seperti klik manusia: Turnstile checkbox pada
    umumnya lolos sekali klik. Gagal jujur bila tidak ada widget /
    challenge tidak hilang.
    """
    import time as _t
    if kancil._engine_name != "webview":
        return {"success": False, "solved": False,
                "errors": ["solve_turnstile butuh engine webview"]}
    r = kancil.evaluate(TURNSTILE_JS)
    boxes = []
    if r.get("success") and r.get("result"):
        try:
            import json as _j
            boxes = _j.loads(str(r["result"])) or []
        except Exception:
            boxes = []
    if not boxes:
        return {"success": False, "solved": False,
                "errors": ["widget turnstile tidak ketemu"]}
    b = boxes[0]
    t = kancil.touch("tap", x=b["x"], y=b["y"], human=True)
    if not t.get("success"):
        return {"success": False, "solved": False,
                "errors": ["tap gagal: %s"
                           % str(t.get("errors"))[:120]]}
    # tunggu challenge hilang
    end = _t.time() + timeout
    while _t.time() < end:
        _t.sleep(2.0)
        c = check(kancil)
        kinds = c.get("kinds") or []
        if "captcha" not in kinds and "challenge" not in kinds:
            return {"success": True, "solved": True,
                    "clicked_at": {"x": b["x"], "y": b["y"]}}
    return {"success": True, "solved": False,
            "note": "challenge masih ada setelah %ds" % timeout}


def resolve(kancil, kind=None):
    """Coba selesaikan stuck. consent -> auto-klik; sisanya -> lapor.

    Returns {resolved: bool, kind, action_taken, needs_human}.
    """
    c = check(kancil)
    kind = kind or c.get("primary")
    if not kind:
        return {"success": True, "resolved": True, "kind": None,
                "note": "tidak stuck"}
    if kind == "consent":
        for btn in CONSENT_BUTTONS:
            try:
                r = kancil.click(btn)
            except Exception:
                continue
            if r.get("success"):
                # verifikasi: pola consent hilang?
                import time as _t
                _t.sleep(1.0)
                c2 = check(kancil)
                gone = "consent" not in (c2.get("kinds") or [])
                return {"success": True, "resolved": gone, "kind": kind,
                        "action_taken": "click %r" % btn,
                        "needs_human": False}
        return {"success": True, "resolved": False, "kind": kind,
                "action_taken": "consent button tidak ketemu",
                "needs_human": True}
    if kind == "challenge":
        # Cloudflare "just a moment" sering selesai sendiri
        import time as _t
        _t.sleep(4.0)
        c2 = check(kancil)
        gone = "challenge" not in (c2.get("kinds") or []) and \
               "captcha" not in (c2.get("kinds") or [])
        return {"success": True, "resolved": gone, "kind": kind,
                "action_taken": "tunggu 4 dtk + cek ulang",
                "needs_human": not gone}
    # captcha / login_wall / paywall: jangan ditebak — kecuali
    # Turnstile Cloudflare yang memang cuma butuh sekali klik.
    if kind == "captcha" and c.get("provider") == "cloudflare" and \
            c.get("provider_solvable") == "turnstile":
        s = solve_turnstile(kancil)
        if s.get("solved"):
            try:
                from . import clearance as _cl
                host = _cl.host_of(c.get("url", ""))
                if host:
                    _cl.save_from_kancil(kancil, host)
            except Exception:
                pass
            return {"success": True, "resolved": True, "kind": kind,
                    "action_taken": "klik checkbox turnstile",
                    "needs_human": False,
                    "provider": "cloudflare"}
        # jatuh ke bawah: lapor manusia
        c["_turnstile_note"] = s.get("note") or str(
            s.get("errors") or [""])[0][:120]
    return {"success": True, "resolved": False, "kind": kind,
            "action_taken": "dilaporkan (tidak di-auto-handle)",
            "needs_human": True,
            "provider": c.get("provider"),
            "provider_hint": c.get("provider_hint", ""),
            "hint": {"captcha": "selesaikan captcha manual di HP, lalu "
                                "lanjutkan autopilot (resume checkpoint)",
                     "login_wall": "login manual sekali di app, atau "
                                   "autopilot --vault <nama>",
                     "paywall": "butuh langganan — lewati step ini"}.get(
                         kind, "")}
