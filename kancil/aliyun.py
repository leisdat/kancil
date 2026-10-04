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

Honest limits: this is an arms race; Aliyun's risk engine also
weighs IP/behavior history. Descope locks out after 5 failures
(180s), so max_tries defaults to 4 and failures never auto-retry
beyond that.
"""

import json
import time

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


def _analyze_js(handle_sel, puzzle_sel):
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
JSON.stringify({error:String((e&&e.stack)||e)};}
})();'started'"""
            % (json.dumps(puzzle_sel), _STRIP_FIND))


def _mask_gone_js():
    return ("(()=>!document.querySelector("
            "'#aliyunCaptcha-mask,#aliyunCaptcha-window-popup'"
            "))()")


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


def _touch(engine, action, x, y):
    fn = getattr(engine, "touch", None)
    if fn:
        return fn(action, x=x, y=y)
    # Kancil API object: touch lives on .engine
    return engine.engine.touch(action, x=x, y=y)


def solve_aliyun_puzzle(engine, max_tries=4, handle_sel=".slider-move",
                        puzzle_sel="img.puzzle", step_pause=0.032,
                        verbose=True):
    """Solve an Aliyun FeiLin slide/puzzle CAPTCHA in the current tab.

    engine: WebViewEngine or Kancil (needs agent 1.24+ for
    down/move/up touch primitives). Returns a dict:
    {"ok": bool, "tries": n, "reason": str}.
    """
    log = (lambda *a: print("[aliyun]", *a)) if verbose else (lambda *a: None)
    eng = getattr(engine, "engine", engine)  # unwrap Kancil -> engine

    for attempt in range(1, max_tries + 1):
        # wait for handle + puzzle + fresh strip
        st = None
        for _ in range(25):
            raw = _eval(eng, _read_state_js(handle_sel, puzzle_sel))
            try:
                st = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                st = None
            if st and st.get("handle") and st.get("puzzle") \
                    and st.get("puzzleW", 0) > 100 \
                    and (st.get("left") or 0) <= 0.5:
                break
            time.sleep(0.6)
        if not st or not st.get("handle") or st.get("puzzleW", 0) <= 100:
            return {"ok": False, "tries": attempt,
                    "reason": "captcha not ready"}

        _eval(eng, _analyze_js(handle_sel, puzzle_sel))
        raw = _poll_result(eng)
        try:
            info = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            info = None
        if not info or info.get("error") or "targetLeft" not in info:
            log("analyze failed (%s); retrying"
                % str(info)[:80] if info else "no result")
            continue
        target = info["targetLeft"]
        if not (target > 5) or target > info.get("puzzleW", 1e9):
            log("implausible target", round(target, 1), "- retrying")
            continue
        log("attempt %d: targetLeft=%.1f" % (attempt, target))

        hx, hy = st["handleX"], st["handleY"]
        _touch(eng, "down", hx, hy)
        time.sleep(0.2)  # press-and-hold like a real finger

        px, ok, reason = hx, False, "drag incomplete"
        for k in range(90):
            try:
                left = float(_eval(eng, _strip_left_js()) or -1)
            except (TypeError, ValueError):
                left = -1
            if left < 0:
                reason = "strip gone"
                break
            if left >= target - 1.5:
                ok, reason = True, "reached target"
                break
            rem = target - left
            step = min(14, max(3, rem * 0.35))
            px += step
            yy = hy + ((k % 5) - 2) * 0.8  # micro tremor
            _touch(eng, "move", px, yy)
            time.sleep(step_pause)
        else:
            try:
                left = float(_eval(eng, _strip_left_js()) or -1)
            except (TypeError, ValueError):
                left = -1
            if left >= target - 3:
                ok, reason = True, "reached target (final check)"
        log("drag:", reason)
        time.sleep(0.3)
        _touch(eng, "up", px, hy)
        time.sleep(4)

        gone = _eval(eng, _mask_gone_js())
        if gone in (True, "true"):
            log("VERIFIED")
            return {"ok": True, "tries": attempt, "reason": "verified"}
        log("not verified; retries left:",
            max_tries - attempt)
    return {"ok": False, "tries": max_tries,
            "reason": "exhausted attempts"}
