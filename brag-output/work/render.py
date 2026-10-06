#!/usr/bin/env python3
"""Kancil /brag-slim launch video renderer. Every frame is a pure function of time.
Optimized: logo resize cache + single RGBA overlay composite per frame."""
import math, os
from PIL import Image, ImageDraw, ImageFont

W, H, FPS, DUR = 1920, 1080, 30, 20
N = FPS * DUR
BG = (12, 12, 12)
GREEN = (17, 182, 120)
WHITE = (235, 235, 235)
DIM = (130, 130, 130)
FAINT = (70, 70, 70)

FD = "/usr/share/fonts/truetype/dejavu"
def font(name, size):
    return ImageFont.truetype(os.path.join(FD, name), size)

F_MONO = lambda s: font("DejaVuSansMono.ttf", s)
F_MONO_B = lambda s: font("DejaVuSansMono-Bold.ttf", s)
F_SANS = lambda s: font("DejaVuSans.ttf", s)
F_SANS_B = lambda s: font("DejaVuSans-Bold.ttf", s)

_LOGO_SRC = Image.open("/home/hatch/workspace/kancil/assets/logo.png").convert("RGB")
_logo_cache = {}
def logo(size):
    if size not in _logo_cache:
        _logo_cache[size] = _LOGO_SRC.resize((size, size), Image.LANCZOS)
    return _logo_cache[size]

def ease_out_cubic(x):
    x = max(0.0, min(1.0, x))
    return 1 - (1 - x) ** 3

def clamp01(x):
    return max(0.0, min(1.0, x))

def typewriter(text, elapsed, cps=28):
    n = int(elapsed * cps)
    return text[:max(0, min(len(text), n))]

def dip_alpha(t, start, end, dip=0.25):
    a = clamp01((t - start) / dip) * clamp01((end - t) / dip)
    return a

def apply_dip(img, a):
    if a >= 1.0:
        return img
    return Image.blend(Image.new("RGB", img.size, (0, 0, 0)), img, a)

def finish(img, ov, t, start, end):
    """Single composite of the overlay, then dip through black."""
    img = Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB")
    return apply_dip(img, dip_alpha(t, start, end))

def otext(od, cx, y, text, fnt, rgb, alpha=1.0):
    """Centered text onto the overlay."""
    bb = od.textbbox((0, 0), text, font=fnt)
    tw = bb[2] - bb[0]
    od.text((cx - tw / 2, y), text, font=fnt, fill=rgb + (int(255 * clamp01(alpha)),))

# ---------------- Scene 1: hook (0 - 2.5s) ----------------
S1_END = 2.5
def scene1(t):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    f = F_MONO(54)
    l1 = "$ browsers were built for humans."
    l2 = "$ agents needed one too."
    v1 = typewriter(l1, t - 0.3)
    v2 = typewriter(l2, t - 1.5)
    d.text((300, 440), v1, font=f, fill=WHITE)
    d.text((300, 530), v2, font=f, fill=GREEN)
    if int(t * 2.4) % 2 == 0:
        cur2 = len(v2) < len(l2) or (len(v1) == len(l1) and t > 1.5)
        vis = v2 if cur2 else v1
        cx = 300 + d.textlength(vis, font=f)
        cy = 530 if cur2 else 440
        d.rectangle([cx + 8, cy + 6, cx + 34, cy + 62], fill=GREEN)
    return finish(img, ov, t, 0, S1_END)

# ---------------- Scene 2: reveal (2.5 - 5.5s) ----------------
S2_END = 5.5
def scene2(t):
    lt = t - 2.5
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    pop = ease_out_cubic(lt / 0.7)
    ls = int(300 * (0.6 + 0.4 * pop))
    img.paste(logo(ls), ((W - ls) // 2, int(120 + 30 * (1 - pop))))
    ta = clamp01((lt - 0.5) / 0.5)
    if ta > 0:
        y = 430 + int(40 * (1 - ease_out_cubic((lt - 0.5) / 0.6)))
        otext(od, W / 2, y, "KANCIL", F_SANS_B(150), WHITE, ta)
    if lt > 1.2:
        otext(od, W / 2, 640, "the pocket-sized agent browser", F_SANS(52), GREEN,
              clamp01((lt - 1.2) / 0.5))
    chips = ["~108 KB", "0 deps", "MIT"]
    f3 = F_MONO_B(44)
    for i, ctext in enumerate(chips):
        ct = lt - (1.7 + i * 0.22)
        if ct <= 0:
            continue
        s = ease_out_cubic(ct / 0.35)
        a = clamp01(ct / 0.25)
        cx, cy = W / 2 + (i - 1) * 300, 800
        bb = od.textbbox((0, 0), ctext, font=f3)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        k = 0.7 + 0.3 * s
        pw, ph = (tw + 56) * k, (th + 34) * k
        ai = int(255 * a)
        od.rounded_rectangle([cx - pw / 2, cy - ph / 2, cx + pw / 2, cy + ph / 2],
                            radius=16, outline=GREEN + (ai,), width=3)
        od.text((cx - tw / 2, cy - th / 2 - bb[1]), ctext, font=f3, fill=WHITE + (ai,))
    return finish(img, ov, t, 2.5, S2_END)

# ---------------- Scene 3: drive (5.5 - 10s) ----------------
S3_END = 10.0
def scene3(t):
    lt = t - 5.5
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    otext(od, W / 2, 90, "122 tool actions \u2014 JSON in, JSON out.", F_SANS_B(54), WHITE,
          clamp01(lt / 0.4))
    tx0, ty0, tx1, ty1 = 260, 220, 1660, 940
    d.rounded_rectangle([tx0, ty0, tx1, ty1], radius=18, outline=FAINT, width=2)
    d.rounded_rectangle([tx0, ty0, tx1, ty0 + 64], radius=18, fill=(24, 24, 24))
    d.pieslice([tx0, ty0, tx0 + 36, ty0 + 64], 90, 270, fill=(24, 24, 24))
    d.pieslice([tx1 - 36, ty0, tx1, ty0 + 64], 270, 90, fill=(24, 24, 24))
    d.rectangle([tx0 + 18, ty0 + 64, tx1 - 18, ty0 + 66], fill=(24, 24, 24))
    for i, col in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([tx0 + 34 + i * 40, ty0 + 22, tx0 + 58 + i * 40, ty0 + 46], fill=col)
    d.text((tx0 + 170, ty0 + 16), "agent \u2014 kancil", font=F_MONO(28), fill=DIM)
    fm = F_MONO(38)
    lines = [
        ("$ kancil open https://shop.example --engine webview", WHITE, 0.4),
        ("  \u2713 200 \u00b7 dom ready \u00b7 1.2s", GREEN, 1.6),
        ("$ kancil tool '{\"action\":\"click\",\"selector\":\"@buy\"}'", WHITE, 2.2),
        ("  {\"ok\": true, \"delta\": {\"url\": \"/checkout\"}}", GREEN, 3.4),
    ]
    y = ty0 + 110
    cursor_done = False
    for text, col, start in lines:
        el = lt - start
        if el <= 0:
            continue
        vis = typewriter(text, el, cps=42)
        d.text((tx0 + 50, y), vis, font=fm, fill=col)
        if not cursor_done and len(vis) < len(text) and int(t * 2.4) % 2 == 0:
            cx = tx0 + 50 + d.textlength(vis, font=fm)
            d.rectangle([cx + 6, y + 4, cx + 30, y + 46], fill=GREEN)
            cursor_done = True
        y += 78
    return finish(img, ov, t, 5.5, S3_END)

# ---------------- Scene 4: features (10 - 14s) ----------------
S4_END = 14.0
def scene4(t):
    lt = t - 10.0
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    otext(od, W / 2, 90, "loops \u00b7 sessions \u00b7 stealth \u2014 built for agents.", F_SANS_B(54),
          WHITE, clamp01(lt / 0.4))
    feats = ["wait_for", "session-export", "stealth TLS", "markdown", "aliyun-solver", "agent-key auth"]
    f3 = F_MONO_B(46)
    for i, feat in enumerate(feats):
        ct = lt - (0.5 + i * 0.28)
        if ct <= 0:
            continue
        s = ease_out_cubic(ct / 0.4)
        al = clamp01(ct / 0.3)
        col, row = i % 3, i // 3
        cw, chh, gap = 440, 150, 40
        gx0 = (W - (3 * cw + 2 * gap)) / 2
        x0 = gx0 + col * (cw + gap)
        y0 = 300 + row * (chh + gap) + int(60 * (1 - s))
        ai = int(255 * al)
        od.rounded_rectangle([x0, y0, x0 + cw, y0 + chh], radius=18,
                            outline=GREEN + (ai,), width=3)
        bb = od.textbbox((0, 0), feat, font=f3)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        od.text((x0 + (cw - tw) / 2, y0 + (chh - th) / 2 - bb[1]), feat, font=f3,
                fill=WHITE + (ai,))
    return finish(img, ov, t, 10.0, S4_END)

# ---------------- Scene 5: everywhere (14 - 17s) ----------------
S5_END = 17.0
def scene5(t):
    lt = t - 14.0
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    otext(od, W / 2, 330, "runs where agents live.", F_SANS_B(64), WHITE, clamp01(lt / 0.4))
    plats = ["Termux", "Android", "Linux", "macOS"]
    f3 = F_MONO_B(52)
    for i, p in enumerate(plats):
        ct = lt - (0.7 + i * 0.25)
        if ct <= 0:
            continue
        s = ease_out_cubic(ct / 0.35)
        al = clamp01(ct / 0.25)
        cx, cy = W / 2 + (i - 1.5) * 330, 620
        bb = od.textbbox((0, 0), p, font=f3)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        k = 0.7 + 0.3 * s
        pw, ph = (tw + 64) * k, (th + 40) * k
        ai = int(255 * al)
        od.rounded_rectangle([cx - pw / 2, cy - ph / 2, cx + pw / 2, cy + ph / 2],
                            radius=18, outline=GREEN + (ai,), width=3)
        od.text((cx - tw / 2, cy - th / 2 - bb[1]), p, font=f3, fill=WHITE + (ai,))
    return finish(img, ov, t, 14.0, S5_END)

# ---------------- Scene 6: outro (17 - 20s) ----------------
S6_END = 20.0
def scene6(t):
    lt = t - 17.0
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    pop = ease_out_cubic(lt / 0.6)
    ls = int(220 * (0.6 + 0.4 * pop))
    img.paste(logo(ls), ((W - ls) // 2, int(150 + 24 * (1 - pop))))
    y = 450
    for text, fnt, col, start in [
        ("small but clever.", F_SANS(58), GREEN, 0.5),
        ("github.com/leisdat/kancil", F_MONO_B(54), WHITE, 1.1),
        ("pip install kancil", F_MONO(46), DIM, 1.6),
    ]:
        el = lt - start
        if el > 0:
            otext(od, W / 2, y, text, fnt, col, clamp01(el / 0.4))
        y += 110
    return finish(img, ov, t, 17.0, S6_END)

def frame_at(t):
    if t < S1_END:
        return scene1(t)
    if t < S2_END:
        return scene2(t)
    if t < S3_END:
        return scene3(t)
    if t < S4_END:
        return scene4(t)
    if t < S5_END:
        return scene5(t)
    return scene6(t)

if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "work"
    if out == "-":
        # stream raw RGB to stdout for piping into ffmpeg
        buf = sys.stdout.buffer
        poster_first = os.environ.get("BRAG_POSTER_FIRST") == "1"
        pimg = None
        if poster_first:
            pimg = Image.open(os.path.join("work", "poster.png")).convert("RGB")
        for i in range(N):
            fr = pimg if (poster_first and i == 0) else frame_at(i / FPS)
            buf.write(fr.tobytes())
        sys.stderr.write("stream done\n")
    else:
        os.makedirs(out, exist_ok=True)
        poster_n = int(4.6 * FPS)
        for i in range(N):
            t = i / FPS
            fr = frame_at(t)
            fr.save(os.path.join(out, "f%04d.png" % i))
            if i == poster_n:
                fr.save(os.path.join(out, "poster.png"))
            if i % 60 == 0:
                print("frame %d/%d" % (i, N), flush=True)
        print("done", flush=True)
