"""Aliyun FeiLin slide/puzzle CAPTCHA solver.

Ported approach from 0xgetz/aliyun-puzzle-solver (MIT License) —
"Human-like, closed-loop auto-solver for Aliyun FeiLin slide/puzzle
CAPTCHA, driven over CDP". Adapted to Kancil: pixel analysis runs
in-page via /js, the drag is closed-loop via /touch down/move/up
primitives (agent 1.24+), one shared downTime per gesture.

The three key techniques (from the original's verified findings):
1. Gap detection: the hole is a gray veil — low saturation
   (max-min < 40) + mid/high brightness (avg > 140) inside the
   piece's vertical band. Widest gray column range = gap.
2. CLOSED-LOOP drag: read the strip's style.left after EVERY small
   step (3-14px, ~32ms apart) and stop at target. Fast/overshooting/
   single-jump drags are rejected even when geometrically perfect.
3. Human entry: press, hold ~200ms, then move with micro tremor.

Extra vs the original (Kancil full-feature set):
- Auto-detect handle/puzzle selectors across candidates.
- Refresh-on-fail: tap the puzzle panel's refresh icon for a new
  puzzle instead of giving up.
- Traceless fallback: if no slider handle, tap the click-to-verify
  widget (Aliyun "traceless" mode).
- analyze_aliyun(): dry-run gap detection for tuning selectors.
- Adaptive timing: each retry varies the step pause.

Honest limits: this is an arms race; Aliyun's risk engine also
weighs IP/behavior history. Descope locks out after 5 failures
(180s), so max_tries defaults to 4 and failures never auto-retry
beyond that.
"""

import json
import time

HANDLE_CANDIDATES = [".slider-move", ".nc_icon", ".slider",
                     "[class*='slider-move']", "[class*='nc-icon']"]
PUZZLE_CANDIDATES = ["img.puzzle", ".puzzle img", "img[class*='puzzle']",
                     ".aliyun-captcha-widget img"]

# img search that also pierces same-origin iframes via the agent's
# deep-query helpers (falls back to plain document).
_IMGS = ("(window.__kancilQA"
         "?[...window.__kancilQA('img')]"
         ":[...document.querySelectorAll('img')])")

_STRIP_FIND = (
    _IMGS + ".find(e=>e.classList.length===0"
    "&&Math.round(e.naturalWidth)>=40&&Math.round(e.naturalWidth)<=60"
    "&&Math.round(e.naturalHeight)>120)"
)


def _strip_left_js():
    return ("(()=>{const s=%s;"
            "return s?(s.style.left||'0'):'-1';})()" % _STRIP_FIND)


def _read_state_js(handle_sel, puzzle_sel):
    return ("""(()=>{const h=document.querySelector(%s);
const p=document.querySelector(%s);
const s=%s;
function ctr(e){const r=e.getBoundingClientRect();
return [r.x+r.width/2,r.y+r.height/2];}
return JSON.stringify({handle:!!h,
handleX:h?ctr(h)[0]:null,handleY:h?ctr(h)[1]:null,
puzzle:!!p,puzzleW:p?Math.round(p.naturalWidth):0,
left:s?parseFloat(s.style.left||'0'):-1});})()"""
            % (json.dumps(handle_sel), json.dumps(puzzle_sel),
               _STRIP_FIND))


def _analyze_js(puzzle_sel):
    # Async pixel analysis; result lands in window.__aliyunResult
    # because /js does not await promises (poll it from Python).
    return ("""window.__aliyunResult=null;(async()=>{
try{
async function pix(url){const r=await fetch(url);const b=await r.blob();
const bmp=await createImageBitmap(b);
const c=document.createElement('canvas');c.width=bmp.width;
c.height=bmp.height;const x=c.getContext('2d');x.drawImage(bmp,0,0);
return{w:bmp.width,h:bmp.height,
d:x.getImageData(0,0,bmp.width,bmp.height).data};}
const puzzle=document.querySelector(%s);
const strip=%s;
if(!puzzle||!strip){window.__aliyunResult='null';return;}
const pu=await pix(puzzle.src),st=await pix(strip.src);
const w=pu.w,h=pu.h;
let minx=1e9,maxx=-1,miny=1e9,maxy=-1;
for(let y=0;y<st.h;y++)for(let x=0;x<st.w;x++){
const a=st.d[(y*st.w+x)*4+3];
if(a>30){if(x<minx)minx=x;if(x>maxx)maxx=x;
if(y<miny)miny=y;if(y>maxy)maxy=y;}}
if(maxx<0){window.__aliyunResult='null';return;}
const bandH=Math.max(1,maxy-miny);
const cols=new Array(w).fill(0);
for(let y=miny;y<=maxy;y++)for(let x=0;x<w;x++){
const i=(y*w+x)*4,R=pu.d[i],G=pu.d[i+1],B=pu.d[i+2];
const mx=Math.max(R,G,B),mn=Math.min(R,G,B);
if(mx-mn<40&&(R+G+B)/3>140)cols[x]++;}
let ranges=[],inR=false,s0=0;
const hi=Math.max(8,Math.round(bandH*0.25));
const lo=Math.max(4,Math.round(bandH*0.12));
for(let x=0;x<w;x++){
if(cols[x]>=hi&&!inR){inR=true;s0=x;}
else if(inR&&cols[x]<lo){inR=false;ranges.push([s0,x-1]);}}
if(inR)ranges.push([s0,w-1]);
ranges=ranges.filter(r=>r[1]-r[0]>12);
if(!ranges.length){window.__aliyunResult='null';return;}
ranges.sort((a,b)=>(b[1]-b[0])-(a[1]-a[0]));
const gap=ranges[0],gapCenterNatural=(gap[0]+gap[1])/2;
const pieceCx=(minx+maxx)/2;
const scale=puzzle.getBoundingClientRect().width/w;
const targetLeft=gapCenterNatural*scale-pieceCx;
window.__aliyunResult=JSON.stringify({pieceCx:pieceCx,
gapCenterNatural:gapCenterNatural,targetLeft:targetLeft,
scale:scale,puzzleW:w});
}catch(e){window.__aliyunResult=
JSON.stringify({error:String((e&&e.stack)||e)});}
})();'started'"""
            % (json.dumps(puzzle_sel), _STRIP_FIND))



_UA_DL = ("Mozilla/5.0 (Linux; Android 14; POCO M4 Pro) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0 Mobile Safari/537.36")


def _fetch_img(url, timeout=20):
    """Download an image natively (urllib) — no page CORS involved."""
    from PIL import Image
    import io
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": _UA_DL,
                                               "Referer": url})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return Image.open(io.BytesIO(r.read()))


def _img_info_js(puzzle_sel):
    """Puzzle/strip URLs + display width — plain strings, no CORS risk."""
    return ("""(()=>{const p=document.querySelector(%s);
const s=%s;
if(!p||!s)return 'null';
const r=p.getBoundingClientRect();
return JSON.stringify({puzzle:p.src,strip:s.src,
displayW:Math.round(r.width),puzzleNW:p.naturalWidth});})()"""
            % (json.dumps(puzzle_sel), _STRIP_FIND))


def analyze_pil(puzzle_url, strip_url, display_w):
    """Python-side gray-veil gap detection (PIL).

    Same algorithm as the in-page analyzer (ported from
    0xgetz/aliyun-puzzle-solver, MIT): piece vertical band via the
    strip's alpha channel, gray column scan over the background,
    widest gray range = gap. Runs natively so a CORS-blocked
    in-page fetch() is not fatal. Returns the same result shape,
    or None (PIL missing / download failed / no gap found)."""
    try:
        from PIL import Image  # noqa
    except ImportError:
        return None
    try:
        pu = _fetch_img(puzzle_url).convert("RGB")
        st = _fetch_img(strip_url)
        w, h = pu.size
        px = pu.load()
        if "A" in st.getbands():
            sa = st.convert("RGBA")
            a = sa.load()
            minx, maxx, miny, maxy = 1e9, -1, 1e9, -1
            for y in range(sa.height):
                for x in range(sa.width):
                    if a[x, y][3] > 30:
                        if x < minx:
                            minx = x
                        if x > maxx:
                            maxx = x
                        if y < miny:
                            miny = y
                        if y > maxy:
                            maxy = y
            if maxx < 0:
                return None
        else:
            # no alpha (e.g. JPEG strip): degraded — middle 60% band
            minx, maxx = 0, st.width - 1
            miny, maxy = int(st.height * 0.2), int(st.height * 0.8)
        bandH = max(1, maxy - miny)
        cols = [0] * w
        for y in range(miny, maxy + 1):
            yy = min(y, h - 1)
            for x in range(w):
                R, G, B = px[x, yy]
                mx = R if R >= G and R >= B else (G if G >= B else B)
                mn = R if R <= G and R <= B else (G if G <= B else B)
                if mx - mn < 40 and (R + G + B) / 3 > 140:
                    cols[x] += 1
        ranges, inR, s0 = [], False, 0
        hi = max(8, round(bandH * 0.25))
        lo = max(4, round(bandH * 0.12))
        for x in range(w):
            if cols[x] >= hi and not inR:
                inR, s0 = True, x
            elif inR and cols[x] < lo:
                inR = False
                ranges.append((s0, x - 1))
        if inR:
            ranges.append((s0, w - 1))
        ranges = [r for r in ranges if r[1] - r[0] > 12]
        if not ranges:
            return None
        ranges.sort(key=lambda r: r[1] - r[0], reverse=True)
        gap = ranges[0]
        gapCenterNatural = (gap[0] + gap[1]) / 2
        pieceCx = (minx + maxx) / 2
        scale = display_w / w if w else 1.0
        return {"pieceCx": pieceCx,
                "gapCenterNatural": gapCenterNatural,
                "targetLeft": gapCenterNatural * scale - pieceCx,
                "scale": scale, "puzzleW": w, "method": "pil"}
    except Exception:
        return None


def analyze_pil_fallback(eng, puzzle_sel):
    """Fetch img URLs from the page, run analyze_pil natively."""
    meta = _parse(_eval(eng, _img_info_js(puzzle_sel)))
    if not meta or not meta.get("puzzle") or not meta.get("strip"):
        return None
    return analyze_pil(meta["puzzle"], meta["strip"],
                       meta.get("displayW") or meta.get("puzzleNW") or 0)

def _refresh_icon_js(puzzle_sel):
    # small clickable near the puzzle panel's top-right corner
    return ("""(()=>{const p=document.querySelector(%s);
if(!p)return'null';
const r=p.getBoundingClientRect();
const cs=[...document.querySelectorAll('div,button,span,i,svg')]
.filter(e=>{const b=e.getBoundingClientRect();
const cx=b.x+b.width/2,cy=b.y+b.height/2;
return b.width>10&&b.width<64&&b.height>10&&b.height<64
&&cx>r.x+r.width-110&&cx<r.x+r.width+10
&&cy>r.y-10&&cy<r.y+80;});
if(!cs.length)return'null';
const b=cs[cs.length-1].getBoundingClientRect();
return JSON.stringify({x:b.x+b.width/2,y:b.y+b.height/2});})()"""
            % json.dumps(puzzle_sel))


def _widget_center_js():
    return ("""(()=>{const w=document.querySelector(
'#aliyun-captcha-widget');
if(!w)return'null';
const r=w.getBoundingClientRect();
if(r.width<10)return'null';
return JSON.stringify({x:r.x+r.width/2,
y:r.y+Math.min(r.height,120)/2});})()""")


def _verify_js(success_text=None):
    checks = ("if(!document.querySelector('#aliyunCaptcha-mask,"
              "#aliyunCaptcha-window-popup'))return'gone';")
    if success_text:
        checks += ("const t=document.body.innerText.toLowerCase();"
                   "if(t.includes(%s))return'text';" % json.dumps(
                       success_text.lower()))
    return "(()=>{%sreturn'present';})()" % checks


def _eval(engine, js):
    r = engine.evaluate(js)
    if not isinstance(r, dict) or not r.get("success"):
        return None
    v = r.get("result")
    if isinstance(v, str) and len(v) >= 2 and v.startswith('"') \
            and v.endswith('"'):
        try:
            v = json.loads(v)
        except Exception:
            pass
    return v


def _poll_result(engine, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        v = _eval(engine, "window.__aliyunResult")
        if v not in (None, "null", "undefined", ""):
            return v
        time.sleep(0.4)
    return None


def _touch(engine, action, x, y, confirm=False):
    fn = getattr(engine, "touch", None)
    if fn:
        return fn(action, x=x, y=y, confirm=confirm)
    return engine.engine.touch(action, x=x, y=y, confirm=confirm)


def _parse(raw):
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None


def detect_selectors(engine, verbose=False):
    """Try handle/puzzle selector candidates; return the first working
    pair as (handle_sel, puzzle_sel), or (None, None)."""
    for hs in HANDLE_CANDIDATES:
        for ps in PUZZLE_CANDIDATES:
            st = _parse(_eval(engine, _read_state_js(hs, ps)))
            if st and st.get("handle") and st.get("puzzle"):
                if verbose:
                    print("[aliyun] selectors:", hs, ps)
                return hs, ps
    return None, None


def analyze_aliyun(engine, handle_sel=None, puzzle_sel=None,
                   verbose=True):
    """Dry-run: detect selectors + locate the gap, without dragging.
    For tuning before a real solve."""
    log = (lambda *a: print("[aliyun]", *a)) if verbose else (lambda *a: None)
    eng = getattr(engine, "engine", engine)
    if not handle_sel or not puzzle_sel:
        hs, ps = detect_selectors(eng, verbose=verbose)
        if not hs:
            return {"ok": False, "reason": "no handle/puzzle found"}
        handle_sel, puzzle_sel = handle_sel or hs, puzzle_sel or ps
    st = _parse(_eval(eng, _read_state_js(handle_sel, puzzle_sel)))
    if not st or not st.get("handle"):
        return {"ok": False, "reason": "captcha not ready"}
    _eval(eng, _analyze_js(puzzle_sel))
    info = _parse(_poll_result(eng))
    if not (info and "targetLeft" in info):
        info = analyze_pil_fallback(eng, puzzle_sel)
    out = {"ok": bool(info and "targetLeft" in info),
           "handle_sel": handle_sel, "puzzle_sel": puzzle_sel,
           "handle": [st.get("handleX"), st.get("handleY")],
           "puzzleW": st.get("puzzleW"), "strip_left": st.get("left"),
           "analysis": info}
    log("analysis:", json.dumps(info)[:200] if info else None)
    return out


def _refresh(engine, puzzle_sel, verbose, confirm=False):
    raw = _eval(engine, _refresh_icon_js(puzzle_sel))
    c = _parse(raw)
    if not c or "x" not in c:
        if verbose:
            print("[aliyun] no refresh icon found")
        return False
    _touch(engine, "tap", c["x"], c["y"], confirm=confirm)
    time.sleep(2.2)
    if verbose:
        print("[aliyun] tapped refresh")
    return True


def _drag_closed_loop(eng, hx, hy, target, step_pause, log,
                      confirm=False):
    _touch(eng, "down", hx, hy, confirm=confirm)
    time.sleep(0.2)  # press-and-hold like a real finger
    px = hx
    for k in range(90):
        try:
            left = float(_eval(eng, _strip_left_js()) or -1)
        except (TypeError, ValueError):
            left = -1
        if left < 0:
            return px, False, "strip gone"
        if left >= target - 1.5:
            return px, True, "reached target"
        rem = target - left
        step = min(14, max(3, rem * 0.35))
        px += step
        yy = hy + ((k % 5) - 2) * 0.8  # micro tremor
        _touch(eng, "move", px, yy, confirm=confirm)
        time.sleep(step_pause)
    try:
        left = float(_eval(eng, _strip_left_js()) or -1)
    except (TypeError, ValueError):
        left = -1
    ok = left >= target - 3
    return px, ok, "reached target (final check)" if ok else "drag incomplete"


def _traceless(engine, verbose=True, confirm=False):
    """Click-to-verify fallback (Aliyun 'traceless' mode): no slider."""
    log = (lambda *a: print("[aliyun]", *a)) if verbose else (lambda *a: None)
    eng = getattr(engine, "engine", engine)
    c = _parse(_eval(eng, _widget_center_js()))
    if not c:
        return {"ok": False, "reason": "no captcha widget found"}
    log("traceless: tapping widget at",
        round(c["x"], 1), round(c["y"], 1))
    _touch(eng, "tap", c["x"], c["y"], confirm=confirm)
    time.sleep(3)
    v = _eval(eng, _verify_js())
    if v == "gone":
        return {"ok": True, "tries": 1, "reason": "verified"}
    return {"ok": False, "tries": 1, "reason": "traceless not verified"}


def solve_aliyun_puzzle(engine, max_tries=4, handle_sel=None,
                        puzzle_sel=None, step_pause=0.032,
                        success_text=None, enable_refresh=True,
                        enable_traceless=True, verbose=True,
                        confirm=False):
    """Solve an Aliyun FeiLin slide/puzzle CAPTCHA in the current tab.

    engine: WebViewEngine or Kancil (needs agent 1.24+ for
    down/move/up touch primitives). Selectors auto-detect when not
    given. Returns {"ok": bool, "tries": n, "reason": str,
    "mode": "slider"|"traceless"}.
    """
    log = (lambda *a: print("[aliyun]", *a)) if verbose else (lambda *a: None)
    eng = getattr(engine, "engine", engine)  # unwrap Kancil -> engine

    if not handle_sel or not puzzle_sel:
        hs, ps = None, None
        for _ in range(4):  # widget may render late
            hs, ps = detect_selectors(eng, verbose=verbose)
            if hs:
                break
            time.sleep(2)
        handle_sel, puzzle_sel = handle_sel or hs, puzzle_sel or ps

    pauses = [step_pause, step_pause * 1.4, step_pause * 0.8,
              step_pause * 1.15]
    for attempt in range(1, max_tries + 1):
        # wait for handle + puzzle + fresh strip
        st = None
        if handle_sel:
            for _ in range(25):
                st = _parse(_eval(
                    eng, _read_state_js(handle_sel, puzzle_sel)))
                if st and st.get("handle") and st.get("puzzle") \
                        and st.get("puzzleW", 0) > 100 \
                        and (st.get("left") or 0) <= 0.5:
                    break
                time.sleep(0.6)
        if not st or not st.get("handle"):
            if enable_traceless:
                log("no slider handle — trying traceless mode")
                r = _traceless(engine, verbose=verbose, confirm=confirm)
                r["mode"] = "traceless"
                return r
            return {"ok": False, "tries": attempt, "mode": "slider",
                    "reason": "captcha not ready"}

        _eval(eng, _analyze_js(puzzle_sel))
        info = _parse(_poll_result(eng))
        if not info or info.get("error") or "targetLeft" not in info:
            why = (info or {}).get("error", "no result") if isinstance(
                info, dict) else "no result"
            log("in-page analyze failed (%s); PIL fallback" % str(why)[:60])
            info = analyze_pil_fallback(eng, puzzle_sel)
            if info:
                log("PIL fallback: targetLeft=%.1f" % info["targetLeft"])
        if not info or "targetLeft" not in info:
            log("analyze failed; refreshing")
            _refresh(eng, puzzle_sel, verbose, confirm=confirm) if enable_refresh else None
            continue
        target = info["targetLeft"]
        if not (target > 5) or target > info.get("puzzleW", 1e9):
            log("implausible target", round(target, 1), "- refreshing")
            _refresh(eng, puzzle_sel, verbose, confirm=confirm) if enable_refresh else None
            continue
        log("attempt %d: targetLeft=%.1f pause=%.3f"
            % (attempt, target, pauses[(attempt - 1) % len(pauses)]))

        hx, hy = st["handleX"], st["handleY"]
        px, _, reason = _drag_closed_loop(
            eng, hx, hy, target, pauses[(attempt - 1) % len(pauses)], log,
            confirm=confirm)
        log("drag:", reason)
        time.sleep(0.3)
        _touch(eng, "up", px, hy, confirm=confirm)
        time.sleep(4)

        v = _eval(eng, _verify_js(success_text))
        if v in ("gone", "text"):
            log("VERIFIED")
            return {"ok": True, "tries": attempt, "mode": "slider",
                    "reason": "verified"}
        log("not verified; retries left:", max_tries - attempt)
        if enable_refresh:
            _refresh(eng, puzzle_sel, verbose)
    return {"ok": False, "tries": max_tries, "mode": "slider",
            "reason": "exhausted attempts"}
