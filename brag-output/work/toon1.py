#!/usr/bin/env python3
"""Kancil stickman cartoon EP1: 'The Slider' — a real animation attempt.
Stickman agent vs slider captcha. 1280x720 @30fps, 15s."""
import math
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1280, 720, 30
DUR = 15.0
BG = (14, 14, 16)
GREEN = (17, 182, 120)
WHITE = (240, 240, 240)
DIM = (140, 140, 140)
RED = (235, 90, 90)
TRACK = (45, 45, 52)
GY = 600

FD = "/usr/share/fonts/truetype/dejavu"
def F(name, s):
    from PIL import ImageFont as IF
    return IF.truetype(f"{FD}/{name}", s)

# ---------- easing ----------
def clamp01(x): return max(0.0, min(1.0, x))
def eo(x):  # ease out cubic
    x = clamp01(x); return 1 - (1 - x) ** 3
def ei(x):  # ease in quad
    x = clamp01(x); return x * x
def eio(x):
    x = clamp01(x); return 4*x**3 if x < .5 else 1 - (-2*x + 2)**3 / 2
def eob(x):  # ease out back (overshoot)
    x = clamp01(x); c = 1.70158
    return 1 + (c + 1) * (x - 1) ** 3 + c * (x - 1) ** 2
def eel(x):  # ease out elastic
    x = clamp01(x)
    if x == 0 or x == 1: return x
    return 2 ** (-10 * x) * math.sin((x * 10 - 0.75) * (2 * math.pi / 3)) + 1

def seg(t, a, b):
    """normalized 0..1 of t in [a,b]"""
    return clamp01((t - a) / (b - a))

# ---------- stickman rig ----------
# P: dict(hx,hy,lean, a_sL,a_eL,a_sR,a_eR, a_hL,a_kL,a_hR,a_kR, squash, face)
def limb_pt(x, y, ang, ln):
    return (x + math.sin(ang) * ln, y + math.cos(ang) * ln)

def draw_stick(d, P):
    sq = P.get("squash", 1.0)
    sx = 1.0 / math.sqrt(max(0.3, sq))
    def X(v): return v[0]
    hx, hy = P["hx"], P["hy"]
    lean = P.get("lean", 0)
    # squash around feet: scale offsets from ground anchor
    ax, ay = hx, GY  # anchor at feet
    def S(pt):
        px, py = pt
        return (ax + (px - ax) * sx, ay + (py - ay) * sq)
    lw = 11
    # torso
    shx = hx + math.sin(lean) * 105
    shy = hy - math.cos(lean) * 105
    d.line([S((hx, hy)), S((shx, shy))], fill=WHITE, width=lw)
    # head
    hr = 30
    hcx = shx + math.sin(lean) * (hr + 10)
    hcy = shy - math.cos(lean) * (hr + 10)
    hx0, hy0 = S((hcx, hcy))
    d.ellipse([hx0 - hr*sx, hy0 - hr*sq, hx0 + hr*sx, hy0 + hr*sq], outline=WHITE, width=lw)
    # face
    face = P.get("face", "normal")
    ex, ey = hx0, hy0
    if face == "normal":
        d.ellipse([ex - 16, ey - 8, ex - 6, ey + 2], fill=WHITE)
        d.ellipse([ex + 6, ey - 8, ex + 16, ey + 2], fill=WHITE)
    elif face == "strain":
        d.ellipse([ex - 16, ey - 8, ex - 6, ey + 2], fill=WHITE)
        d.ellipse([ex + 6, ey - 8, ex + 16, ey + 2], fill=WHITE)
        d.arc([ex - 12, ey + 6, ex + 12, ey + 22], 10, 170, fill=WHITE, width=5)
    elif face == "dizzy":
        for sxx in (-11, 11):
            d.line([ex + sxx - 7, ey - 7, ex + sxx + 7, ey + 7], fill=WHITE, width=5)
            d.line([ex + sxx - 7, ey + 7, ex + sxx + 7, ey - 7], fill=WHITE, width=5)
    elif face == "happy":
        d.arc([ex - 14, ey - 12, ex - 2, ey + 0], 200, 340, fill=WHITE, width=5)
        d.arc([ex + 2, ey - 12, ex + 14, ey + 0], 200, 340, fill=WHITE, width=5)
        d.arc([ex - 14, ey + 2, ex + 14, ey + 20], 15, 165, fill=WHITE, width=5)
    elif face == "ooh":
        d.ellipse([ex - 16, ey - 10, ex - 4, ey + 2], fill=WHITE)
        d.ellipse([ex + 4, ey - 10, ex + 16, ey + 2], fill=WHITE)
        d.ellipse([ex - 8, ey + 8, ex + 8, ey + 22], outline=WHITE, width=5)
    # arms
    for side, a_s, a_e in (("L", P["a_sL"], P["a_eL"]), ("R", P["a_sR"], P["a_eR"])):
        exx, eyy = limb_pt(shx, shy, a_s, 62)
        hxx, hyy = limb_pt(exx, eyy, a_s + a_e, 58)
        d.line([S((shx, shy)), S((exx, eyy))], fill=WHITE, width=lw)
        d.line([S((exx, eyy)), S((hxx, hyy))], fill=WHITE, width=lw)
    # legs
    for a_h, a_k in ((P["a_hL"], P["a_kL"]), (P["a_hR"], P["a_kR"])):
        kx, ky = limb_pt(hx, hy, a_h, 76)
        fx, fy = limb_pt(kx, ky, a_h + a_k, 76)
        d.line([S((hx, hy)), S((kx, ky))], fill=WHITE, width=lw + 1)
        d.line([S((kx, ky)), S((fx, fy))], fill=WHITE, width=lw + 1)
    # hand pos of right arm (for grabbing)
    exx, eyy = limb_pt(shx, shy, P["a_sR"], 62)
    hxx, hyy = limb_pt(exx, eyy, P["a_sR"] + P["a_eR"], 58)
    return S((hxx, hyy))

def base_pose(hx, hy):
    return dict(hx=hx, hy=hy, lean=0, a_sL=0.15, a_eL=0.25, a_sR=-0.15, a_eR=-0.25,
                a_hL=0, a_kL=0.06, a_hR=0, a_kR=0.06, squash=1.0, face="normal")

def ik_reach(P, tx, ty):
    """2-bone IK: set right-arm angles so the hand reaches (tx,ty)."""
    hx, hy, lean = P["hx"], P["hy"], P.get("lean", 0)
    sx = hx + math.sin(lean) * 105
    sy = hy - math.cos(lean) * 105
    l1, l2 = 62, 58
    dx, dy = tx - sx, ty - sy
    d = max(30, min(l1 + l2 - 3, math.hypot(dx, dy)))
    at = math.atan2(dx, dy)
    c1 = max(-1, min(1, (l1 * l1 + d * d - l2 * l2) / (2 * l1 * d)))
    ce = max(-1, min(1, (l1 * l1 + l2 * l2 - d * d) / (2 * l1 * l2)))
    P["a_sR"] = at - math.acos(c1) * 0.9
    P["a_eR"] = (math.pi - math.acos(ce)) * 0.9

# ---------- captcha widget ----------
CAP = dict(x0=640, x1=1060, y=400, hs=62)
def draw_captcha(d, t, handle_x, fill_frac, success, shake=0):
    x0, x1, y, hs = CAP["x0"], CAP["x1"], CAP["y"], CAP["hs"]
    shx = shake * 8 * math.sin(t * 60)
    # track
    d.rounded_rectangle([x0 + shx, y, x1 + shx, y + hs], radius=14, fill=TRACK)
    # fill
    fw = (x1 - x0) * fill_frac
    if fw > 4:
        col = GREEN if success else (90, 140, 200)
        d.rounded_rectangle([x0 + shx, y, x0 + shx + fw, y + hs], radius=14, fill=col)
    # label
    if not success:
        f = F("DejaVuSans.ttf", 26)
        txt = "drag to verify"
        bb = d.textbbox((0, 0), txt, font=f)
        d.text((x0 + 110 + shx, y + (hs - (bb[3] - bb[1])) / 2 - bb[1]), txt, font=f, fill=DIM)
    # handle
    if handle_x is not None:
        hx0 = handle_x - hs / 2 + shx
        d.rounded_rectangle([hx0, y - 4, hx0 + hs, y + hs + 4], radius=12, fill=(70, 70, 80),
                            outline=WHITE, width=3)
        f2 = F("DejaVuSans-Bold.ttf", 30)
        d.text((hx0 + 14, y + 10), ">>>", font=f2, fill=WHITE)
    if success:
        # checkmark pop is drawn by caller
        pass

def draw_check(d, cx, cy, scale):
    r = int(44 * scale)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=GREEN)
    d.line([cx - r*0.45, cy, cx - r*0.1, cy + r*0.35], fill=WHITE, width=int(10*scale))
    d.line([cx - r*0.1, cy + r*0.35, cx + r*0.5, cy - r*0.3], fill=WHITE, width=int(10*scale))

def draw_bang(d, x, y, scale):
    s = scale
    d.rounded_rectangle([x - 10*s, y - 44*s, x + 10*s, y - 6*s], radius=8*s, fill=RED)
    d.ellipse([x - 10*s, y + 4*s, x + 10*s, y + 24*s], fill=RED)

def draw_sweat(d, drops):
    for (x, y, s) in drops:
        d.ellipse([x - 7*s, y - 10*s, x + 7*s, y + 6*s], fill=(150, 200, 255))
        d.polygon([x - 7*s, y - 4*s, x + 7*s, y - 4*s, x, y - 18*s], fill=(150, 200, 255))

# ---------- timeline ----------
def pose_at(t):
    """Returns (P, fx) where fx carries extra flags."""
    fx = {}
    P = base_pose(430, GY - 152)
    # ---- P1 walk in 0-2.2 ----
    if t < 2.2:
        p = seg(t, 0, 2.0)
        P["hx"] = -80 + 510 * eo(p)
        ph = p * 14 * math.pi
        sw = 0.55 * (1 - 0.4 * p)
        P["a_hL"] = sw * math.sin(ph)
        P["a_hR"] = sw * math.sin(ph + math.pi)
        P["a_kL"] = 0.12 + 0.8 * max(0, math.sin(ph + 2.4)) * (1 - 0.3 * p)
        P["a_kR"] = 0.12 + 0.8 * max(0, math.sin(ph + math.pi + 2.4)) * (1 - 0.3 * p)
        P["a_sL"] = -0.42 * math.sin(ph) * (1 - 0.4 * p)
        P["a_sR"] = -0.42 * math.sin(ph + math.pi) * (1 - 0.4 * p)
        P["a_eL"] = P["a_eR"] = 0.35
        P["hy"] = GY - 152 - 9 * abs(math.cos(ph)) * (1 - p * 0.5)
        P["lean"] = 0.14 * (1 - p * 0.6)
        if t > 2.0:  # settle
            q = seg(t, 2.0, 2.2)
            for k in ("a_hL", "a_hR", "a_kL", "a_kR", "a_sL", "a_sR"):
                P[k] *= (1 - q)
            P["a_eL"] = P["a_eR"] = 0.35 - 0.1 * q
            P["hy"] = GY - 152
    # ---- P2 notice 2.2-3.0 ----
    elif t < 3.0:
        P["hx"] = 430
        P["lean"] = -0.10 * eo(seg(t, 2.2, 2.5))
        P["face"] = "ooh" if t > 2.45 else "normal"
        P["a_sL"] = 0.15 + 0.5 * eo(seg(t, 2.4, 2.8))
        P["a_sR"] = -0.15 - 0.5 * eo(seg(t, 2.4, 2.8))
        fx["bang"] = eob(seg(t, 2.55, 2.9))
    # ---- P3 grab 3.0-3.8 ----
    elif t < 3.8:
        q = eio(seg(t, 3.0, 3.8))
        P["hx"] = 430 + 130 * q
        P["lean"] = 0.35 * q
        P["face"] = "normal"
        if q > 0.55:
            ik_reach(P, 671, 431)
        else:
            P["a_sR"] = -0.15 + 1.30 * q
            P["a_eR"] = 0.25 - 0.10 * q
        fx["grabbed"] = t > 3.5
        if fx["grabbed"]:
            fx["handle_x"] = 671.0
    # ---- P4 drag1 3.8-5.6 ----
    elif t < 5.6:
        if t < 5.0:
            q = eio(seg(t, 3.8, 4.6))
            P["hx"] = 560 + 40 * q
            P["lean"] = 0.35 + 0.20 * q
            P["face"] = "strain" if t > 4.4 else "normal"
            if t > 4.5:  # struggle shake
                P["hx"] += 7 * math.sin(t * 42)
                fx["sweat"] = seg(t, 4.5, 4.8)
            hx_handle = 671.0 + 300.0 * q
            fx["handle_x"] = hx_handle
            ik_reach(P, hx_handle, 431)
        else:
            # release + stumble back
            q = seg(t, 5.0, 5.6)
            P["hx"] = 600 - 55 * eo(q)
            P["lean"] = 0.55 - 0.95 * eo(q)
            P["a_sR"] = 1.30 - 1.10 * eo(q)
            P["a_sL"] = 0.15 + 2.2 * math.sin(q * 14) * (1 - q)
            P["a_eL"] = 0.35 + 0.5 * math.sin(q * 14)
            P["face"] = "ooh"
            fx["snapback"] = q
    # ---- P5 drag2 + fall 5.6-8.3 ----
    elif t < 8.3:
        if t < 6.7:
            q = eio(seg(t, 5.6, 6.2))
            P["hx"] = 545 + 55 * q
            P["lean"] = -0.35 + 0.90 * q
            P["face"] = "strain" if t > 6.1 else "normal"
            if t > 6.1:
                P["hx"] += 6 * math.sin(t * 46)
                fx["sweat"] = seg(t, 6.1, 6.4)
            yq = ei(seg(t, 6.2, 6.7)) if t > 6.2 else 0.0
            P["hx"] = 545 + 55 * q + 45 * yq
            P["lean"] = -0.35 + 0.90 * q + 0.20 * yq
            hx_handle = 671.0 + 300.0 * q + 190.0 * yq
            fx["handle_x"] = hx_handle
            ik_reach(P, hx_handle, 431)
        else:
            # handle detached; stickman topples backward onto butt
            q = seg(t, 6.7, 7.5)
            P["hx"] = 645 - 95 * eio(q)
            P["hy"] = (GY - 152) + 82 * eio(q)   # butt near ground
            P["lean"] = 0.75 - 1.20 * eio(q)     # torso tips back
            P["a_hL"] = 1.50 * q
            P["a_kL"] = -1.30 * q
            P["a_hR"] = 1.25 * q
            P["a_kR"] = -1.30 * q
            P["a_sL"] = 0.3 - 1.20 * q           # hands plant behind
            P["a_eL"] = 0.3 - 0.5 * q
            P["a_sR"] = 1.35 - 2.25 * q
            P["a_eR"] = 0.15
            # squash on landing ~7.35
            land = math.exp(-((t - 7.35) ** 2) / 0.004)
            P["squash"] = 1.0 - 0.28 * land
            P["face"] = "dizzy" if t > 7.2 else "ooh"
            if t > 7.5:
                fx["spiral"] = seg(t, 7.5, 7.8)
        fx["handle_fly"] = t > 6.7
    # ---- P6 recover 8.3-9.3 ----
    elif t < 9.3:
        q = eio(seg(t, 8.3, 8.9))
        P["hx"] = 550 - 30 * q
        P["hy"] = (GY - 70) - 82 * q
        P["lean"] = -0.20 + 0.35 * q
        P["a_hL"] = 1.35 * (1 - q)
        P["a_hR"] = 1.15 * (1 - q)
        P["a_kL"] = -1.10 * (1 - q) + 0.06 * q
        P["a_kR"] = -1.00 * (1 - q) + 0.06 * q
        P["a_sL"] = -0.8 * (1 - q) + 0.15 * q
        P["a_sR"] = -0.75 * (1 - q) + 0.10 * q
        P["face"] = "normal" if t > 8.8 else "dizzy"
        if t > 8.8:  # scratch head
            sq = eo(seg(t, 8.8, 9.2))
            P["a_sR"] = 0.1 + 2.6 * sq
            P["a_eR"] = 0.3 + 0.9 * sq + 0.25 * math.sin(t * 22) * sq
            P["lean"] = 0.15 + 0.08 * math.sin(t * 22) * sq
    else:
        # placeholder for P7+ (chunk 3 overrides)
        P["hx"] = 520
    return P, fx

def hand_of(P):
    """Pure right-hand position (no squash)."""
    hx, hy, lean = P["hx"], P["hy"], P.get("lean", 0)
    shx = hx + math.sin(lean) * 105
    shy = hy - math.cos(lean) * 105
    ex, ey = limb_pt(shx, shy, P["a_sR"], 62)
    return limb_pt(ex, ey, P["a_sR"] + P["a_eR"], 58)

def captcha_state(t, P, fx):
    """Returns dict(visible, handle_xy or None, fill, success, shake)."""
    from toon1 import CAP
    rest = CAP["x0"] + CAP["hs"] / 2
    st = dict(visible=t > 2.2, handle=None, fill=0.0, success=False, shake=0)
    if not st["visible"]:
        return st
    if t < 3.7:
        st["handle"] = (rest, CAP["y"] + CAP["hs"] / 2)
    elif fx.get("handle_x") is not None and t < 6.7:
        hx = min(fx["handle_x"], CAP["x1"] + 60)
        st["handle"] = (hx, CAP["y"] + CAP["hs"] / 2)
    elif 5.0 <= t < 5.6:
        q = t - 5.0
        grab = min(CAP["x0"] + 300, CAP["x1"] - 40)
        st["handle"] = (grab + (rest - grab) * eel(q / 0.6), CAP["y"] + CAP["hs"] / 2)
        st["shake"] = (1 - q / 0.6) * 0.5
    elif 6.7 <= t < 10.2:
        # flying handle physics
        dt = t - 6.7
        x = CAP["x1"] + 40 + 520 * dt
        y = CAP["y"] + 1500 * dt * dt / 2 - 380 * dt
        if x < W + 100:
            st["handle"] = (x, y)
    elif t >= 10.2:
        q = seg(t, 10.2, 10.5)
        st["handle"] = (rest, CAP["y"] + CAP["hs"] / 2)
        if t >= 10.4:
            st["fill"] = eio(seg(t, 10.4, 11.6))
        if t >= 11.6:
            st["success"] = True
    return st

# ---------- late phases (appended to pose_at via dispatch) ----------
import toon1 as T

_orig_pose_at = T.pose_at
def pose_at2(t):
    if t < 9.3:
        return _orig_pose_at(t)
    P = T.base_pose(520, T.GY - 152)
    fx = {}
    if t < 10.4:
        # P7: idle, watching logo pop
        P["hx"] = 520
        P["lean"] = 0.18 + 0.05 * math.sin(t * 3)
        P["hy"] = T.GY - 152 - 4 * abs(math.sin(t * 3))
        P["a_sL"], P["a_eL"] = 0.2, 0.3
        P["a_sR"], P["a_eR"] = -0.2, 0.3
        P["face"] = "ooh"
        fx["logo"] = T.eob(T.seg(t, 9.5, 9.9))
        fx["logo_wink"] = t > 10.0
    elif t < 12.0:
        # P8: watching solve, then happy
        P["hx"] = 520
        P["lean"] = 0.12
        P["a_sL"], P["a_eL"] = 0.35, 0.4
        P["a_sR"], P["a_eR"] = -0.35, 0.4
        P["face"] = "happy" if t > 11.6 else "ooh"
        fx["logo"] = 1.0
        fx["logo_wink"] = True
        if t > 11.6:
            fx["check"] = T.eob(T.seg(t, 11.6, 12.0))
    elif t < 13.8:
        # P9: celebration jump
        fx["logo"] = 1.0
        fx["logo_wink"] = True
        if t < 12.25:
            q = T.seg(t, 12.0, 12.25)
            P["squash"] = 1.0 - 0.22 * q
            P["hy"] = T.GY - 152 + 26 * q
            P["a_hL"] = P["a_hR"] = 0.5 * q
            P["a_kL"] = P["a_kR"] = -1.0 * q
            P["a_sL"] = 0.35 - 0.9 * q
            P["a_sR"] = -0.35 + 0.9 * q
            P["face"] = "strain"
        elif t < 12.85:
            q = T.seg(t, 12.25, 12.85)
            P["hy"] = (T.GY - 152) - 190 * math.sin(math.pi * q)
            P["squash"] = 1.0 + 0.06 * math.sin(math.pi * q)
            P["a_sL"] = -0.55 - 1.85 * T.eo(q)   # arms up in V
            P["a_sR"] = 0.55 + 1.85 * T.eo(q)
            P["a_eL"] = P["a_eR"] = 0.25
            P["a_hL"] = 0.5 + 0.55 * T.eo(q)     # knees tuck up
            P["a_hR"] = 0.5 + 0.45 * T.eo(q)
            P["a_kL"] = -1.0 - 0.9 * T.eo(q)
            P["a_kR"] = -1.0 - 0.8 * T.eo(q)
            P["face"] = "happy"
        else:
            q = T.seg(t, 12.85, 13.2)
            land = math.exp(-((t - 12.9) ** 2) / 0.004)
            P["squash"] = 1.0 - 0.30 * land
            P["hy"] = T.GY - 152 + 20 * land
            P["a_sL"] = -2.65 + 2.9 * T.eo(q)
            P["a_sR"] = 2.65 - 2.9 * T.eo(q)
            P["face"] = "happy"
    else:
        # P10: outro
        fx["logo"] = 1.0
        fx["logo_wink"] = True
        P["hx"] = 520
        P["hy"] = T.GY - 152 - 4 * abs(math.sin(t * 3))
        P["lean"] = 0.05
        wv = math.sin(t * 9) * 0.35
        P["a_sR"] = 2.5 + wv
        P["a_eR"] = 0.35 + wv * 0.5
        P["a_sL"], P["a_eL"] = 0.2, 0.3
        P["face"] = "happy"
        fx["outro"] = T.eo(T.seg(t, 13.6, 14.2))
    return P, fx

T.pose_at = pose_at2

# ---------- logo ----------
from PIL import Image
_logo = Image.open("/home/hatch/workspace/kancil/assets/logo.png").convert("RGB")
def logo_sprite(size):
    im = _logo.resize((size, size), Image.LANCZOS).convert("RGBA")
    px = im.load()
    for y in range(im.height):
        for x in range(im.width):
            r, g, b, a = px[x, y]
            if r < 40 and g < 40 and b < 40:
                px[x, y] = (0, 0, 0, 0)
    return im

LOGO_SPRITE = logo_sprite(190)

def draw_logo(img, scale, wink, t=0):
    s = int(190 * max(0.01, scale))
    sp = LOGO_SPRITE.resize((s, s), Image.LANCZOS)
    img.paste(sp, (850 - s // 2, 130), sp)
    if wink and scale > 0.9 and t < 10.7:
        d = ImageDraw.Draw(img)
        fade = max(0.0, 1.0 - (t - 10.0) / 0.7)
        for i in range(3):
            a = 9.8 + i * 0.12
            x = 850 + 130 + 26 * math.cos(a)
            y = 96 + 26 * math.sin(a)
            d.line([850 + 130, 96, x, y], fill=T.GREEN, width=4)

# ---------- SFX ----------
def synth_sfx():
    import numpy as np
    SR = 44100
    mix = np.zeros(int(SR * T.DUR))
    def add(sig, at, g=1.0):
        i0 = int(at * SR); i1 = min(len(mix), i0 + len(sig))
        if i1 > i0: mix[i0:i1] += sig[:i1 - i0] * g
    def tone(f0, f1, dur, decay=18, kind="sine"):
        n = int(dur * SR); tt = np.arange(n) / SR
        f = f0 + (f1 - f0) * tt / dur
        ph = 2 * np.pi * np.cumsum(f) / SR
        s = np.sin(ph) if kind == "sine" else np.sign(np.sin(ph))
        return s * np.exp(-tt * decay)
    def noise(dur, decay=30):
        n = int(dur * SR)
        return np.random.default_rng(3).standard_normal(n) * np.exp(-np.arange(n) / SR * decay)
    add(tone(500, 950, 0.12), 2.60, 0.5)          # bang pop
    add(noise(0.05, 90) + tone(2100, 1800, 0.05), 3.70, 0.5)  # grab click
    add(tone(420, 70, 0.28, 10), 5.00, 0.7)       # snap twang
    add(tone(110, 90, 0.35, 12, "square"), 5.05, 0.25)  # fail buzz
    add(noise(0.30, 12), 6.70, 0.4)               # whoosh
    add(tone(75, 45, 0.30, 14), 7.32, 0.9)        # thud (fall)
    add(tone(550, 1000, 0.14), 9.50, 0.5)         # logo pop
    s = tone(880, 880, 0.5, 7) + 0.6 * tone(1318, 1318, 0.5, 7)
    add(s, 11.60, 0.55)                            # ding
    add(tone(160, 520, 0.28, 8), 12.25, 0.6)      # boing
    add(tone(70, 42, 0.25, 15), 12.90, 0.9)       # thud (land)
    # playful pluck loop (C major, bouncy)
    bpm, beat = 132, 60 / 132
    notes = [261.6, 329.6, 392.0, 523.3, 392.0, 329.6, 293.7, 329.6]
    for i, fq in enumerate(notes * 5):
        at = i * beat / 2
        if at > T.DUR - 0.4: break
        add(tone(fq, fq, 0.22, 16), at, 0.16)
        if i % 4 == 0:
            add(tone(fq / 4, fq / 4, 0.3, 6), at, 0.20)
    mix = np.tanh(mix * 0.9)
    mix /= (np.max(np.abs(mix)) + 1e-9) / 0.89
    import wave, struct
    pcm = (mix * 32767).astype(np.int16)
    with wave.open("work/toon_audio.wav", "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print("sfx done", flush=True)

# ---------- main ----------
def frame(t):
    from PIL import ImageDraw
    img = Image.new("RGB", (T.W, T.H), T.BG)
    d = ImageDraw.Draw(img)
    d.line([0, T.GY, T.W, T.GY], fill=(42, 42, 48), width=3)
    P, fx = T.pose_at(t)
    cap = T.captcha_state(t, P, fx)
    if cap["visible"]:
        T.draw_captcha(d, t, cap["handle"][0] if cap["handle"] else None,
                       cap["fill"], cap["success"], cap["shake"])
    if fx.get("bang"):
        cx = P["hx"] + 40
        T.draw_bang(d, cx, T.GY - 430, fx["bang"])
    if fx.get("sweat"):
        hx = P["hx"]
        sw = fx["sweat"]
        drops = [(hx - 46 - 60 * sw, T.GY - 400 + 90 * sw * sw, 1.0),
                 (hx + 52 + 70 * sw, T.GY - 410 + 110 * sw * sw, 0.85)]
        T.draw_sweat(d, drops)
    if fx.get("spiral"):
        cx, cy, r = P["hx"], T.GY - 430, 40
        for k in range(3):
            a0 = t * 7 + k * 2.1
            d.arc([cx - r, cy - r, cx + r, cy + r], math.degrees(a0),
                  math.degrees(a0 + 4.2), fill=T.DIM, width=6)
    if 7.3 < t < 8.1:  # landing dust poof
        age = (t - 7.3) / 0.8
        for ox, sp in ((-70, 90), (-30, 130), (40, 110), (80, 80)):
            r = 8 + 24 * age
            x = P["hx"] + ox - sp * age
            d.ellipse([x - r, T.GY - 12 - r, x + r, T.GY - 12 + r], fill=(88, 88, 94))
    if fx.get("logo"):
        draw_logo(img, fx["logo"], fx.get("logo_wink"), t)
        d = ImageDraw.Draw(img)
    T.draw_stick(d, P)
    if fx.get("check"):
        T.draw_check(d, 1000, 431, fx["check"])
    if fx.get("outro"):
        f = T.F("DejaVuSans-Bold.ttf", 64)
        txt = "kancil \u2014 small but clever."
        bb = d.textbbox((0, 0), txt, font=f)
        tw = bb[2] - bb[0]
        a = fx["outro"]
        ov = Image.new("RGBA", (T.W, T.H), (0, 0, 0, 0))
        ImageDraw.Draw(ov).text(((T.W - tw) / 2, 60), txt, font=f,
                                fill=T.GREEN + (int(255 * a),))
        img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    return img

if __name__ == "__main__":
    import sys, subprocess, os
    if len(sys.argv) > 1 and sys.argv[1] == "sfx":
        synth_sfx()
    else:
        os.makedirs("work", exist_ok=True)
        cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
               "-s", f"{T.W}x{T.H}", "-framerate", str(T.FPS), "-i", "-",
               "-i", "work/toon_audio.wav",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
               "-shortest", "-movflags", "+faststart", "toon_ep1.mp4"]
        synth_sfx()
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        n = int(T.DUR * T.FPS)
        for i in range(n):
            p.stdin.write(frame(i / T.FPS).tobytes())
            if i % 90 == 0: print(f"frame {i}/{n}", flush=True)
        p.stdin.close(); p.wait()
        print("done toon_ep1.mp4", flush=True)
