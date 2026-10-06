#!/usr/bin/env python3
"""Stickman PoC: walk-in, wave, wink. Proves the cartoon pipeline."""
import math, os
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1280, 720, 30
BG = (12, 12, 12)
GREEN = (17, 182, 120)
WHITE = (235, 235, 235)
GY = 600  # ground y

def draw_stickman(d, x, gy, phase, mode="walk", t=0):
    """phase: walk cycle 0..2pi. mode: walk | wave | idle"""
    head_r, lw = 34, 10
    hip_y = gy - 150
    sh_y = gy - 260
    head_cy = gy - 330
    # head
    d.ellipse([x - head_r, head_cy - head_r, x + head_r, head_cy + head_r],
              outline=WHITE, width=lw)
    # body
    d.line([x, sh_y, x, hip_y], fill=WHITE, width=lw)
    if mode == "walk":
        s = math.sin(phase)
        # legs: two-segment swing
        for side in (1, -1):
            a = s * side
            kx = x + a * 55
            ky = gy - 75
            fx = x + a * 95
            d.line([x, hip_y, kx, ky], fill=WHITE, width=lw)
            d.line([kx, ky, fx, gy], fill=WHITE, width=lw)
            # arms opposite
            ax = x - a * 45
            ay = sh_y + 70
            hx = x - a * 80
            d.line([x, sh_y, ax, ay], fill=WHITE, width=lw)
            d.line([ax, ay, hx, ay + 35], fill=WHITE, width=lw)
    elif mode == "wave":
        # legs straight
        for side in (1, -1):
            d.line([x, hip_y, x + side * 28, gy - 75], fill=WHITE, width=lw)
            d.line([x + side * 28, gy - 75, x + side * 30, gy], fill=WHITE, width=lw)
        # left arm down
        d.line([x, sh_y, x - 40, sh_y + 75], fill=WHITE, width=lw)
        d.line([x - 40, sh_y + 75, x - 45, sh_y + 120], fill=WHITE, width=lw)
        # right arm waving
        wv = math.sin(t * 10) * 25
        d.line([x, sh_y, x + 55, sh_y - 40], fill=WHITE, width=lw)
        d.line([x + 55, sh_y - 40, x + 95 + wv, sh_y - 95], fill=WHITE, width=lw)
    # wink: right eye (viewer right) becomes a curve after t_wink
    return head_cy

def frame(i):
    t = i / FPS
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.line([0, GY, W, GY], fill=(40, 40, 40), width=3)
    if t < 2.6:
        # walk in from left
        p = t / 2.6
        x = -100 + (W / 2 + 100) * (1 - (1 - p) ** 2)
        draw_stickman(d, x, GY, phase=t * 9, mode="walk")
    else:
        x = W / 2
        hc = draw_stickman(d, x, GY, phase=0, mode="wave", t=t)
        # wink after 3.4s: draw winking eye
        if t > 3.4:
            d.arc([x + 8, hc - 14, x + 34, hc + 6], start=200, end=340, fill=GREEN, width=6)
            d.ellipse([x - 30, hc - 12, x - 14, hc + 4], fill=WHITE)
        else:
            d.ellipse([x - 30, hc - 12, x - 14, hc + 4], fill=WHITE)
            d.ellipse([x + 12, hc - 12, x + 28, hc + 4], fill=WHITE)
        if t > 4.4:
            f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72)
            txt = "kancil ;)"
            bb = d.textbbox((0, 0), txt, font=f)
            d.text(((W - (bb[2] - bb[0])) / 2, 120), txt, font=f, fill=GREEN)
    return img

if __name__ == "__main__":
    os.makedirs("work", exist_ok=True)
    import subprocess
    cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-framerate", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "22", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "stickman_poc.mp4"]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    n = int(6 * FPS)
    for i in range(n):
        p.stdin.write(frame(i).tobytes())
    p.stdin.close()
    p.wait()
    print("done stickman_poc.mp4")
