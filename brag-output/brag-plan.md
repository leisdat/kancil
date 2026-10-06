# /brag-slim — Kancil launch video

## What it is (one sentence)
Kancil — the pocket-sized agent browser: a ~108 KB, zero-dependency browser built to be *driven by AI agents* (JSON in/out, 122 tool actions), on Termux/Android, Linux, macOS.

## Who / what / edge
- **Who:** developers building AI agents that need to browse, scrape, log in, and act on the real web.
- **What it does:** gives the agent a real browser (WebView on Android, static engine elsewhere) with navigation, DOM, DevTools, stealth, sessions — through a CLI + Python API + 122 JSON tool actions.
- **Sets it apart:** not a human browser with AI bolted on — an *agent-first* browser. 108 KB APK agent, zero required deps, runs on a phone in Termux.
- **Funniest/impressive claim:** "small but clever — like the mousedeer of Indonesian folklore." The logo literally winks at you.
- **Visual hook:** emerald-green geometric mousedeer on near-black; terminal typing.
- **Real UI shown:** actual `kancil` CLI session (open → tool call → JSON delta), real command names.
- **Tone:** default (punchy, playful, clean) with deadpan-dev energy.
- **Share caption:** "Browsers were built for humans. Kancil is the 108 KB browser built for agents — 122 tool actions, zero deps, runs on your phone. github.com/leisdat/kancil"

## Storyboard (20s, 1920×1080, 30fps)

| # | Time | Scene | Visual | Audio |
|---|------|-------|--------|-------|
| 1 | 0–2.5s | **Hook** | Black. Terminal types `$ browsers were built for humans.` → `$ agents needed one too.` Blinking cursor. | Music starts, low pulse |
| 2 | 2.5–5.5s | **Reveal** | Logo fades/scales in, winks. `KANCIL` huge, `the pocket-sized agent browser`. Chips pop: `~108 KB` `0 deps` `MIT` | Blip per chip |
| 3 | 5.5–10s | **Drive** | Terminal window, typed live: `kancil open …` → `✓ dom ready`; `kancil tool '{"action":"click",…}'` → `{"ok":true,"delta":{…}}`. Caption: "122 tool actions — JSON in, JSON out." | Typing ticks |
| 4 | 10–14s | **Features** | Cards cascade in: `wait_for` · `session-export` · `stealth TLS` · `markdown` · `aliyun-solver` · `agent-key auth`. Caption: "loops · sessions · stealth — built for agents." | Blip per card |
| 5 | 14–17s | **Everywhere** | "runs where agents live" + chips: `Termux` `Android` `Linux` `macOS` | — |
| 6 | 17–20s | **Outro** | Logo + `small but clever.` + `github.com/leisdat/kancil` + `pip install kancil` | Resolve chord |

Transitions: dip through black (stagger, never crossfade busy→busy).
