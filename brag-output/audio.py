#!/usr/bin/env python3
"""Synth soundtrack for the Kancil launch video: 20s, 100 BPM, Am-F-C-G."""
import numpy as np
import wave, struct

SR, DUR = 44100, 20.0
N = int(SR * DUR)
t = np.arange(N) / SR
mix = np.zeros(N)

BPM = 100.0
BEAT = 60.0 / BPM  # 0.6s

# chords: (bass_freq, [arp freqs])
CHORDS = [
    (55.00, [220.00, 261.63, 329.63, 440.00]),   # Am
    (43.65, [174.61, 220.00, 261.63, 349.23]),   # F
    (65.41, [261.63, 329.63, 392.00, 523.25]),   # C
    (49.00, [196.00, 246.94, 293.66, 392.00]),   # G
]
BARS = 8
BAR = 4 * BEAT  # 2.4s

def add(sig, at, gain=1.0):
    i0 = int(at * SR)
    i1 = min(N, i0 + len(sig))
    if i1 > i0:
        mix[i0:i1] += sig[:i1 - i0] * gain

def pluck(freq, dur, bright=1.0):
    n = int(dur * SR)
    tt = np.arange(n) / SR
    env = np.exp(-tt * 9.0)
    s = np.sin(2 * np.pi * freq * tt) + 0.35 * bright * np.sin(4 * np.pi * freq * tt) * np.exp(-tt * 14)
    return s * env

def bass_note(freq, dur):
    n = int(dur * SR)
    tt = np.arange(n) / SR
    env = np.minimum(1.0, np.exp(-tt * 3.5)) * np.exp(-tt * 1.2)
    s = np.sin(2 * np.pi * freq * tt) + 0.25 * np.sin(6 * np.pi * freq * tt)
    return np.tanh(s * 1.4) * env

def kick():
    n = int(0.22 * SR)
    tt = np.arange(n) / SR
    f = 130 * np.exp(-tt * 22) + 42
    ph = 2 * np.pi * np.cumsum(f) / SR
    return np.sin(ph) * np.exp(-tt * 16)

def hat():
    n = int(0.05 * SR)
    nz = np.random.default_rng(7).standard_normal(n)
    return nz * np.exp(-np.arange(n) / SR * 220) * 0.5

def blip(f0=880.0, f1=1320.0):
    n = int(0.09 * SR)
    tt = np.arange(n) / SR
    f = f0 + (f1 - f0) * tt / 0.09
    ph = 2 * np.pi * np.cumsum(f) / SR
    return np.sin(ph) * np.exp(-tt * 30)

for bar in range(BARS):
    bass_f, arp = CHORDS[bar % 4]
    bt = bar * BAR
    # bass: eighth-note pulse
    for e in range(8):
        add(bass_note(bass_f, 0.28), bt + e * BEAT / 2, 0.5)
    # arp: 16ths, up-down cycle
    seq = arp + arp[::-1]
    for s in range(16):
        add(pluck(seq[s % len(seq)], 0.22), bt + s * BEAT / 4, 0.22)
    # kick on beats, hat offbeat
    for b in range(4):
        add(kick(), bt + b * BEAT, 0.55)
        add(hat(), bt + b * BEAT + BEAT / 2, 0.10)

# scene-change blips
for at, f0 in [(2.5, 990), (5.5, 880), (10.0, 990), (14.0, 880), (17.0, 1174)]:
    add(blip(f0, f0 * 1.5), at, 0.25)

# master: soft clip + normalize
mix = np.tanh(mix * 0.9)
mix = mix / (np.max(np.abs(mix)) + 1e-9) * 0.89
pcm = (mix * 32767).astype(np.int16)

with wave.open("work/audio.wav", "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(SR)
    w.writeframes(pcm.tobytes())
print("audio done", flush=True)
