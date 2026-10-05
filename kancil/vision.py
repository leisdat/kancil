"""Visual grounding for text-only agents ("mata" buat Hermes).

Pola: screenshot -> locate target -> koordinat -> touch. Modul ini murni
mekanik: "melihat"-nya didelegasikan ke backend yang pluggable, jadi model
yang nyetir (Hermes) tidak harus support vision — otaknya tetap text-only,
matanya bisa diganti-ganti.

Backend ("eyes"):
  - "template": offline, OpenCV template matching. Cocok buat ikon yang
    bentuknya tetap (tombol close, logo). Butuh cv2+numpy (opsional);
    kalau tidak ada, backend ini lapor unavailable — bukan crash.
  - "api":      vision model lewat OpenAI-compatible chat API
    (env KANCIL_VISION_ENDPOINT / KANCIL_VISION_KEY / KANCIL_VISION_MODEL,
    opsional KANCIL_VISION_PROVIDER=openai|anthropic). Bisa GPT-4o,
    Gemini, GLM-4V, Qwen-VL — apa pun yang ngerti gambar.
  - "callback": fungsi milik pemakai: fn(png_bytes, description) -> (x, y)
    dalam koordinat 0-1000. Buat Hermes yang nanti punya vision sendiri.

Koordinat selalu 0-1000 ternormalisasi antar backend; to_css() memetakan
ke CSS px (yang dimakan /touch) via window.innerWidth/innerHeight.

Fungsi level-tinggi:
  - see_tap(engine, description, ...): lihat -> tap, dengan verify + retry.
  - see_type(engine, description, text, ...): lihat field -> tap -> ketik
    via elementFromPoint (tidak butuh selector sama sekali).
  - see_drag(engine, from_desc, to_desc, ...): drag antar dua target visual.
  - describe_screen(engine, question=None): deskripsi natural tampilan.

Semua fungsi mengembalikan plain dict {"success": bool, ...} (bukan
api.ok/fail — modul ini tidak import api supaya tidak circular; api.py
yang membungkusnya).

Batas jujur:
  - Template matching rapuh kalau UI berubah tema/ukuran; threshold
    default 0.8, turunkan kalau ikonnya di-render ulang tiap versi.
  - Backend "api" butuh network + key; latensi 1-5 detik per locate.
  - Mapping 0-1000 -> CSS px mengasumsikan screenshot = viewport WebView
    persis (benar untuk screenshot Kancil agent 1.25+). Kalau meleset,
    jalankan calibrate() sekali untuk mengunci faktor skala.
"""

import base64
import io
import json
import os
import tempfile
import time
import urllib.request

# ---------------------------------------------------------------------------
# backend registry
# ---------------------------------------------------------------------------

TEMPLATE_THRESHOLD = 0.8


def available_backends():
    """{"template": bool, "api": bool, "callback": True} + alasan."""
    out = {}
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        out["template"] = True
    except Exception as e:
        out["template"] = "unavailable: %s (pip install opencv-python numpy)" % (
            type(e).__name__)
    if os.environ.get("KANCIL_VISION_ENDPOINT") and os.environ.get(
            "KANCIL_VISION_KEY"):
        out["api"] = True
    else:
        out["api"] = ("unavailable: set KANCIL_VISION_ENDPOINT + "
                      "KANCIL_VISION_KEY (+KANCIL_VISION_MODEL)")
    out["callback"] = True
    return out


def _norm(points, w, h):
    """pixel (x, y) -> 0-1000."""
    return [{"x": round(x / w * 1000, 1), "y": round(y / h * 1000, 1),
             "score": round(s, 3)}
            for x, y, s in points]


# ---------------------------------------------------------------------------
# backend: template matching (offline)
# ---------------------------------------------------------------------------

def locate_template(png_bytes, template, threshold=TEMPLATE_THRESHOLD,
                    multiple=False, max_results=5):
    """Cari `template` (path file atau bytes PNG) di dalam screenshot.

    Returns {"success", "candidates": [{x, y, score}]} — x/y 0-1000,
    score = koefisien korelasi 0..1.
    """
    try:
        import cv2
        import numpy as np
    except Exception as e:
        return {"success": False, "backend": "template",
                "error": "opencv/numpy tidak tersedia: %s" % type(e).__name__,
                "hint": "pip install opencv-python numpy, atau pakai "
                        "backend 'api'"}
    try:
        if isinstance(template, (bytes, bytearray)):
            tbuf = template
        else:
            with open(template, "rb") as f:
                tbuf = f.read()
        screen = cv2.imdecode(
            np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_COLOR)
        tpl = cv2.imdecode(np.frombuffer(tbuf, np.uint8), cv2.IMREAD_COLOR)
        if screen is None or tpl is None:
            return {"success": False, "backend": "template",
                    "error": "gagal decode PNG (screenshot/template)"}
        sh, sw = screen.shape[:2]
        th, tw = tpl.shape[:2]
        if th > sh or tw > sw:
            # template lebih besar dari layar: downscale template
            scale = min(sh / th, sw / tw) * 0.95
            tpl = cv2.resize(tpl, (int(tw * scale), int(th * scale)))
            th, tw = tpl.shape[:2]
        res = cv2.matchTemplate(screen, tpl, cv2.TM_CCOEFF_NORMED)
        cands = []
        if multiple:
            ys, xs = (res >= threshold).nonzero()
            for x, y in zip(xs.tolist()[:max_results * 4],
                            ys.tolist()[:max_results * 4]):
                # non-max suppression sederhana: jarak min setengah template
                cx, cy = x + tw / 2, y + th / 2
                if all(abs(cx - c[0]) > tw / 2 or abs(cy - c[1]) > th / 2
                       for c in cands):
                    cands.append((cx, cy, float(res[y, x])))
                    if len(cands) >= max_results:
                        break
        else:
            _, mx, _, ml = cv2.minMaxLoc(res)
            if mx >= threshold:
                cands.append((ml[0] + tw / 2, ml[1] + th / 2, float(mx)))
        if not cands:
            return {"success": False, "backend": "template",
                    "error": "tidak ketemu (threshold %.2f)" % threshold,
                    "hint": "turunkan threshold atau pastikan template "
                            "di-crop dari screenshot yang sama"}
        return {"success": True, "backend": "template",
                "candidates": _norm(cands, sw, sh)}
    except Exception as e:
        return {"success": False, "backend": "template",
                "error": "template matching gagal: %s" % str(e)[:200]}


# ---------------------------------------------------------------------------
# backend: vision API (OpenAI-compatible / Anthropic)
# ---------------------------------------------------------------------------

def _api_config(over):
    return {
        "endpoint": over.get("endpoint") or
        os.environ.get("KANCIL_VISION_ENDPOINT", ""),
        "api_key": over.get("api_key") or
        os.environ.get("KANCIL_VISION_KEY", ""),
        "model": over.get("model") or
        os.environ.get("KANCIL_VISION_MODEL", "gpt-4o-mini"),
        "provider": (over.get("provider") or
                     os.environ.get("KANCIL_VISION_PROVIDER", "openai")
                     ).lower(),
        "timeout": int(over.get("timeout") or
                       os.environ.get("KANCIL_VISION_TIMEOUT", "60")),
    }


_LOCATE_PROMPT = (
    "You are a UI element locator. Look at this screenshot and find: {desc}\n"
    "Respond with ONLY a JSON object, no other text: "
    '{{"x": <number 0-1000>, "y": <number 0-1000>}}\n'
    "x,y = center of the element in 0-1000 normalized coordinates "
    "(0,0 = top-left). If the target is not visible, respond "
    '\'{{"x": -1, "y": -1}}\'.'
)
_LOCATE_PROMPT_MULTI = (
    "You are a UI element locator. Look at this screenshot and find ALL "
    "instances of: {desc}\n"
    "Respond with ONLY a JSON array, no other text: "
    "'[{{\"x\": <0-1000>, \"y\": <0-1000>}}, ...]' "
    "(empty array [] if none visible)."
)
_DESCRIBE_PROMPT = (
    "Describe this screenshot briefly for a blind operator: layout, key "
    "interactive elements (buttons, fields, links) and their approximate "
    "positions, plus any visible text that matters. {question}"
)


def _post_json(url, payload, api_key, timeout, provider):
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if provider == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    else:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _openai_payload(png_bytes, prompt, model, multiple):
    b64 = base64.b64encode(png_bytes).decode()
    return {
        "model": model,
        "max_tokens": 300,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + b64}},
            ],
        }],
    }


def _anthropic_payload(png_bytes, prompt, model):
    b64 = base64.b64encode(png_bytes).decode()
    return {
        "model": model,
        "max_tokens": 300,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png",
                            "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    }


def _extract_text(resp, provider):
    try:
        if provider == "anthropic":
            return resp["content"][0]["text"]
        return resp["choices"][0]["message"]["content"]
    except Exception:
        return ""


def _parse_points(text, multiple):
    """Ambil JSON dari jawaban model -> list (x, y)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    start = t.find("{") if not multiple else t.find("[")
    end = t.rfind("}") + 1 if not multiple else t.rfind("]") + 1
    if start < 0 or end <= 0:
        return None
    try:
        obj = json.loads(t[start:end])
    except Exception:
        return None
    pts = obj if multiple else [obj]
    out = []
    for p in pts:
        try:
            x, y = float(p["x"]), float(p["y"])
        except Exception:
            continue
        if 0 <= x <= 1000 and 0 <= y <= 1000:
            out.append((x, y, 1.0))
    return out


def locate_api(png_bytes, description, multiple=False, **over):
    """Locate via vision chat API. over: endpoint/api_key/model/provider/timeout."""
    cfg = _api_config(over)
    if not cfg["endpoint"] or not cfg["api_key"]:
        return {"success": False, "backend": "api",
                "error": "KANCIL_VISION_ENDPOINT / KANCIL_VISION_KEY belum di-set",
                "hint": "export KANCIL_VISION_ENDPOINT=https://api.openai.com/v1 "
                        "KANCIL_VISION_KEY=sk-... "
                        "KANCIL_VISION_MODEL=gpt-4o-mini"}
    prompt = (_LOCATE_PROMPT_MULTI if multiple else _LOCATE_PROMPT).format(
        desc=description)
    try:
        if cfg["provider"] == "anthropic":
            url = cfg["endpoint"].rstrip("/") + "/messages"
            payload = _anthropic_payload(png_bytes, prompt, cfg["model"])
        else:
            url = cfg["endpoint"].rstrip("/") + "/chat/completions"
            payload = _openai_payload(png_bytes, prompt, cfg["model"],
                                      multiple)
        resp = _post_json(url, payload, cfg["api_key"], cfg["timeout"],
                          cfg["provider"])
        text = _extract_text(resp, cfg["provider"])
        pts = _parse_points(text, multiple)
        if pts is None:
            return {"success": False, "backend": "api",
                    "error": "model tidak mengembalikan JSON koordinat",
                    "raw": text[:300]}
        if not pts:
            return {"success": False, "backend": "api",
                    "error": "target tidak terlihat di screenshot",
                    "description": description}
        return {"success": True, "backend": "api", "model": cfg["model"],
                "candidates": [{"x": x, "y": y, "score": s}
                               for x, y, s in pts]}
    except Exception as e:
        return {"success": False, "backend": "api",
                "error": "vision API gagal: %s" % str(e)[:200]}


def describe_screen(png_bytes, question="", **over):
    """Deskripsi natural tampilan layar via vision API."""
    cfg = _api_config(over)
    if not cfg["endpoint"] or not cfg["api_key"]:
        return {"success": False,
                "error": "KANCIL_VISION_ENDPOINT / KANCIL_VISION_KEY belum di-set"}
    prompt = _DESCRIBE_PROMPT.format(
        question=("Extra question: " + question) if question else "")
    try:
        if cfg["provider"] == "anthropic":
            url = cfg["endpoint"].rstrip("/") + "/messages"
            payload = _anthropic_payload(png_bytes, prompt, cfg["model"])
        else:
            url = cfg["endpoint"].rstrip("/") + "/chat/completions"
            payload = _openai_payload(png_bytes, prompt, cfg["model"], False)
            payload["max_tokens"] = 600
        resp = _post_json(url, payload, cfg["api_key"], cfg["timeout"],
                          cfg["provider"])
        return {"success": True, "backend": "api", "model": cfg["model"],
                "description": _extract_text(resp, cfg["provider"]).strip()}
    except Exception as e:
        return {"success": False,
                "error": "describe gagal: %s" % str(e)[:200]}


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def locate(png_bytes, description, backend=None, template=None,
           multiple=False, callback=None, **over):
    """Dispatch ke backend mata yang dipilih.

    backend: "template" | "api" | "callback" | None (auto: template kalau
    `template` dikasih, else api kalau env lengkap, else error jelas).
    """
    if backend is None:
        backend = ("template" if template else
                   ("api" if available_backends()["api"] is True else None))
    if backend == "template":
        if not template:
            return {"success": False, "backend": "template",
                    "error": "backend template butuh `template` (path/bytes ikon)"}
        return locate_template(png_bytes, template, multiple=multiple,
                               **{k: v for k, v in over.items()
                                  if k in ("threshold", "max_results")})
    if backend == "api":
        return locate_api(png_bytes, description, multiple=multiple, **over)
    if backend == "callback":
        if not callable(callback):
            return {"success": False, "backend": "callback",
                    "error": "backend callback butuh fungsi "
                             "fn(png_bytes, description) -> (x, y)"}
        try:
            pt = callback(png_bytes, description)
            x, y = float(pt[0]), float(pt[1])
            if not (0 <= x <= 1000 and 0 <= y <= 1000):
                raise ValueError("koordinat di luar 0-1000")
            return {"success": True, "backend": "callback",
                    "candidates": [{"x": x, "y": y, "score": 1.0}]}
        except Exception as e:
            return {"success": False, "backend": "callback",
                    "error": "callback gagal: %s" % str(e)[:200]}
    return {"success": False,
            "error": "backend '%s' tidak dikenal / tidak tersedia" % backend,
            "available": available_backends(),
            "hint": "pilih backend='template' (+template=...), "
                    "backend='api' (+env KANCIL_VISION_*), atau "
                    "backend='callback' (+callback=fn)"}


# ---------------------------------------------------------------------------
# engine plumbing: screenshot bytes, viewport, 0-1000 -> CSS px
# ---------------------------------------------------------------------------

def grab_png(engine, full=False):
    """Screenshot engine -> PNG bytes (via file sementara)."""
    shot = getattr(engine, "screenshot", None)
    if not shot:
        return {"success": False,
                "error": "engine tidak punya screenshot()"}
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    try:
        r = shot(path=tmp.name, full=full)
        if isinstance(r, dict) and not r.get("success", True):
            return {"success": False,
                    "error": "screenshot gagal: %s" % r.get("errors", r)}
        path = r.get("path", tmp.name) if isinstance(r, dict) else tmp.name
        with open(path, "rb") as f:
            raw = f.read()
        if raw[:8] != b"\x89PNG\r\n\x1a\n":
            return {"success": False, "error": "bukan PNG valid"}
        return {"success": True, "png": raw, "path": path}
    except Exception as e:
        return {"success": False, "error": "screenshot gagal: %s" % str(e)[:200]}
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def viewport_css(engine):
    """(lebar, tinggi) viewport dalam CSS px via JS."""
    ev = getattr(engine, "evaluate", None)
    if not ev:
        return {"success": False,
                "error": "engine tidak punya evaluate()"}
    try:
        r = ev("({w: window.innerWidth, h: window.innerHeight})")
        if isinstance(r, dict):
            r = r.get("result", r.get("value", r))
        if isinstance(r, str):
            r = json.loads(r)
        w, h = float(r["w"]), float(r["h"])
        if w <= 0 or h <= 0:
            raise ValueError("viewport 0")
        return {"success": True, "w": w, "h": h}
    except Exception as e:
        return {"success": False,
                "error": "baca viewport gagal: %s" % str(e)[:200]}


def to_css(vw, vh, x1000, y1000):
    """0-1000 -> CSS px. Murni matematika (gampang di-test)."""
    return (round(x1000 / 1000 * vw, 1), round(y1000 / 1000 * vh, 1))


def calibrate(engine, full=False):
    """Kunci faktor skala: screenshot + viewport, kembalikan info mapping.

    Dipakai sekali kalau tap meleset; hasilnya bisa di-cache pemakai.
    """
    g = grab_png(engine, full=full)
    if not g["success"]:
        return g
    v = viewport_css(engine)
    if not v["success"]:
        return v
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(g["png"]))
        iw, ih = img.size
    except Exception:
        iw, ih = None, None
    return {"success": True, "viewport_css": [v["w"], v["h"]],
            "screenshot_px": [iw, ih],
            "mapping": "css_x = x1000/1000 * %.1f ; css_y = y1000/1000 * %.1f"
                       % (v["w"], v["h"]),
            "note": "kalau tap meleset vertikal, kemungkinan screenshot "
                    "mencakup area di luar WebView — laporkan iw/ih vs "
                    "viewport untuk koreksi offset"}


# ---------------------------------------------------------------------------
# high-level: see_tap / see_type / see_drag
# ---------------------------------------------------------------------------

_TYPE_AT_JS = """(()=>{const X=%s,Y=%s,TEXT=%s;
const el=document.elementFromPoint(X,Y);
const inp=(el&&(el.closest('input,textarea')||
  (el.isContentEditable?el:null)))||el;
if(!inp)return 'no-element';
inp.focus();
if(inp.isContentEditable){inp.textContent=TEXT;}
else{
  const proto=inp.tagName==='TEXTAREA'?
    HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
  const desc=Object.getOwnPropertyDescriptor(proto,'value');
  if(desc&&desc.set){desc.set.call(inp,TEXT);}else{inp.value=TEXT;}
  inp.dispatchEvent(new Event('input',{bubbles:true}));
}
inp.dispatchEvent(new Event('change',{bubbles:true}));
return 'typed:'+((inp.value||inp.textContent||'').length);})()"""


def _verify_ok(engine, verify):
    """verify: selector CSS yang harus ada setelah aksi (atau None)."""
    if not verify:
        return True, "skipped"
    ev = getattr(engine, "evaluate", None)
    if not ev:
        return True, "no-evaluate"
    try:
        r = ev("!!document.querySelector(%s)" % json.dumps(verify))
        if isinstance(r, dict):
            r = r.get("result", r.get("value", False))
        found = r is True or r == "true"
        return found, "found" if found else "missing"
    except Exception as e:
        return False, "verify-error: %s" % str(e)[:100]


def see_tap(engine, description, backend=None, template=None,
            verify=None, max_tries=3, dry_run=False, confirm=False,
            human=False, callback=None, **over):
    """Lihat -> tap target. Full loop: locate, konversi, tap, verify, retry.

    verify: selector CSS yang harus muncul setelah tap (pola validator).
    Tiap retry pakai kandidat berikutnya.
    """
    if dry_run and not confirm:
        return {"success": False, "code": "DRY_RUN_BLOCKED",
                "error": "dry-run: tap diblokir (pakai confirm=True untuk "
                         "eksekusi)",
                "planned": {"action": "tap", "description": description,
                            "backend": backend or "auto"}}
    g = grab_png(engine)
    if not g["success"]:
        return dict(g, stage="screenshot")
    loc = locate(g["png"], description, backend=backend, template=template,
                 multiple=True, callback=callback, **over)
    if not loc["success"]:
        return dict(loc, stage="locate")
    v = viewport_css(engine)
    if not v["success"]:
        return dict(v, stage="viewport")
    touch = getattr(engine, "touch", None)
    if not touch:
        return {"success": False, "stage": "touch",
                "error": "engine tidak punya touch() (butuh webview agent 1.22+)"}
    cands = loc["candidates"][:max_tries]
    tried = []
    for i, c in enumerate(cands):
        cx, cy = to_css(v["w"], v["h"], c["x"], c["y"])
        try:
            tr = touch(action="tap", x=cx, y=cy, human=human)
            tok = not (isinstance(tr, dict) and tr.get("success") is False)
        except Exception as e:
            tr, tok = {"success": False, "error": str(e)[:150]}, False
        vok, vinfo = _verify_ok(engine, verify)
        tried.append({"x1000": [c["x"], c["y"]], "css": [cx, cy],
                      "tap_ok": tok, "verify": vinfo})
        if tok and vok:
            return {"success": True, "backend": loc["backend"],
                    "description": description, "x": cx, "y": cy,
                    "x1000": [c["x"], c["y"]], "tries": i + 1,
                    "tried": tried}
        time.sleep(0.5)
    return {"success": False, "backend": loc["backend"],
            "error": "tap %d kandidat, verify '%s' tak kunjung muncul"
                     % (len(cands), verify),
            "tried": tried,
            "hint": "coba verify=None, atau deskripsi lebih spesifik"}


def see_type(engine, description, text, backend=None, template=None,
             dry_run=False, confirm=False, callback=None, **over):
    """Lihat field -> tap (fokus) -> ketik via elementFromPoint. Tanpa selector."""
    if dry_run and not confirm:
        return {"success": False, "code": "DRY_RUN_BLOCKED",
                "error": "dry-run: type diblokir",
                "planned": {"action": "type", "description": description,
                            "text_len": len(text)}}
    g = grab_png(engine)
    if not g["success"]:
        return dict(g, stage="screenshot")
    loc = locate(g["png"], description, backend=backend, template=template,
                 callback=callback, **over)
    if not loc["success"]:
        return dict(loc, stage="locate")
    v = viewport_css(engine)
    if not v["success"]:
        return dict(v, stage="viewport")
    c = loc["candidates"][0]
    cx, cy = to_css(v["w"], v["h"], c["x"], c["y"])
    ev = getattr(engine, "evaluate", None)
    if not ev:
        return {"success": False, "stage": "type",
                "error": "engine tidak punya evaluate()"}
    touch = getattr(engine, "touch", None)
    if touch:
        try:
            touch(action="tap", x=cx, y=cy)
            time.sleep(0.4)
        except Exception:
            pass
    try:
        r = ev(_TYPE_AT_JS % (cx, cy, json.dumps(text)))
        if isinstance(r, dict):
            r = r.get("result", r.get("value", r))
        ok_ = isinstance(r, str) and r.startswith("typed:")
        return {"success": ok_, "x": cx, "y": cy,
                "x1000": [c["x"], c["y"]], "backend": loc["backend"],
                "typed_chars": r.split(":")[1] if ok_ else 0,
                **({} if ok_ else {"error": "elementFromPoint: %s" % r})}
    except Exception as e:
        return {"success": False, "stage": "type",
                "error": "type gagal: %s" % str(e)[:200]}


def see_drag(engine, from_desc, to_desc, backend=None, template=None,
             dry_run=False, confirm=False, human=True,
             callback=None, **over):
    """Drag dari satu target visual ke target visual lain (mis. slider)."""
    if dry_run and not confirm:
        return {"success": False, "code": "DRY_RUN_BLOCKED",
                "error": "dry-run: drag diblokir",
                "planned": {"action": "drag", "from": from_desc, "to": to_desc}}
    g = grab_png(engine)
    if not g["success"]:
        return dict(g, stage="screenshot")
    got = {}
    for key, desc in (("from", from_desc), ("to", to_desc)):
        loc = locate(g["png"], desc, backend=backend, template=template,
                     callback=callback, **over)
        if not loc["success"]:
            return dict(loc, stage="locate-" + key, description=desc)
        got[key] = loc["candidates"][0]
    v = viewport_css(engine)
    if not v["success"]:
        return dict(v, stage="viewport")
    touch = getattr(engine, "touch", None)
    if not touch:
        return {"success": False, "stage": "drag",
                "error": "engine tidak punya touch()"}
    x1, y1 = to_css(v["w"], v["h"], got["from"]["x"], got["from"]["y"])
    x2, y2 = to_css(v["w"], v["h"], got["to"]["x"], got["to"]["y"])
    try:
        tr = touch(action="swipe", x=x1, y=y1, x2=x2, y2=y2,
                   human=human)
        tok = not (isinstance(tr, dict) and tr.get("success") is False)
    except Exception as e:
        tr, tok = {"success": False, "error": str(e)[:150]}, False
    return {"success": tok, "from_css": [x1, y1], "to_css": [x2, y2],
            "backend": backend or "auto",
            **({} if tok else {"touch_result": tr})}
