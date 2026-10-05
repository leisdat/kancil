<p align="center">
  <img src="assets/logo.png" width="160" alt="Kancil logo">
</p>

<h1 align="center">Kancil</h1>

<p align="center">
  <b>The pocket-sized agent browser.</b><br>
  Small but clever — like the mousedeer of Indonesian folklore.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-3.14.0-brightgreen" alt="version">
  <img src="https://img.shields.io/badge/tests-240%20unit-brightgreen" alt="tests">
  <img src="https://img.shields.io/badge/size-%7E100%20KB-blue" alt="size">
  <img src="https://img.shields.io/badge/dependencies-0%20required-blue" alt="zero required deps">
  <img src="https://img.shields.io/badge/python-3.8%2B-blue" alt="python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="license">
  <a href="README.id.md"><img src="https://img.shields.io/badge/README-Bahasa%20Indonesia-red" alt="indonesian"></a>
</p>

> A **browser built for AI agents** — navigation, DOM, DevTools, scraping,
> and a real-browser driving layer, all through a JSON-in/JSON-out interface.
> **~100 KB, zero required dependencies.** Runs on Termux (Android ARM64), Linux, macOS.

✨ **New:** loop & state round — `kancil wait-for` (server-side JS polling,
agent 1.27+) · `kancil session-export/import` (full session: HttpOnly cookies
+ localStorage, instant login recovery) · `kancil form-submit` (fill→submit→
assert) · `kancil cookies-export` (Netscape format, curl/playwright interop) ·
`kancil markdown` (JS-rendered page → clean Markdown, zero-dep) ·
`back`/`fwd --verify` · rate-limit-aware retry (Retry-After, static engine) ·
TLS fingerprint impersonation (`kancil stealth impersonate`,
6 browser profiles) · WebView anti-detect JS injection · `kancil session clear`
with per-domain cookie wipe · APK agent 1.26 (zombie-tab fix: watchdog
loading + onRenderProcessGone revive + eval per-tab, NetLog HTTP status fix,
screenshot 1x1 + recycled-bitmap fix, dry_run guard buat touch/upload/download,
Aliyun PIL fallback anti-CORS; `/touch` tap/swipe/pinch + human-like swipe +
down/move/up primitives, base64 upload, auto-launch via `am start`,
`/blocklist` CLI, XHR/fetch response-body capture, Aliyun closed-loop
slider solver) · vision "mata" buat agent text-only (`kancil see-tap
"tombol Login"`, `see-type`, `see-drag` — backend template/API pluggable,
tanpa selector) · validator pattern (`click`/`type`/`open
--verify`) · stealth status in `kancil doctor`.

```bash
pip install -e .
kancil open https://example.com --quiet
kancil tool '{"action":"click","selector":"@button_1"}'
kancil scrape https://news.ycombinator.com --auto --max-items 10
```

## Why Kancil?

Regular browsers are built for humans. Automation frameworks bolt tooling on
top with heavy drivers. Kancil inverts that: **the agent is the first-class
user**.

| | Kancil | Playwright / Selenium scripts |
|---|---|---|
| Install size | **~100 KB, 0 deps** | 150 MB+ browser download |
| Runs on Termux Android | ✅ out of the box | ❌ needs proot hacks |
| Agent interface | Native: **74 JSON actions** | You write the wrapper |
| Honest capability reporting | ✅ `devtools --json` | — |
| Drives your real phone browser | ✅ proxy + injected agent | ❌ |
| Sees JS-rendered pages | ✅ via real browser / engine | ✅ |

## Superpowers

**🔍 Scraper that reads like a human** — `scrape --auto` splits articles
into title / links / image / meta (no more
`"1Overgeared NewFantasi · 74,276 viewsChapter 341"` mush).
`structured` grabs JSON-LD + OpenGraph straight from the page.
`sitemap` discovers URLs from sitemap.xml; `--workers N` fetches them in
parallel. Pagination, dedup, robots.txt — built in.

**⚡ Fast by default** — HTTP keep-alive pooling, ETag/Last-Modified cache
(`kancil http-cache`), 5-minute DNS cache, browser-like headers. Static
pages fly; repeat visits are nearly free.

**🕵️ Real-browser mode, zero bloat** — Kancil stays tiny, your phone's
Chrome does the rendering:
- **Level 1** — `kancil view` injects `agent.js`: snapshot the real DOM,
  click, type, eval JS, capture console. Auto-reconnects.
- **Level 2** — `kancil serve-proxy --mitm`: route your phone browser's
  traffic through Kancil. Login-walled sites work because the requests
  come from *your real browser*. MITM certs are generated in **pure
  Python** — no openssl binary needed.

**🤖 Agent Tool Interface** — 74 actions, JSON-in/JSON-out, structured
errors (`ELEMENT_NOT_FOUND`, `TIMEOUT`, …). `batch` runs 25 actions per
call; `network_curl` replays any logged request as curl.

**🎬 YouTube kit** — `yt-search` (via `ytInitialData`, no JS),
`yt-video` metadata, `yt-play` mini player (opens the video and triggers
play; sustained playback for guests can be gated/paused by YouTube —
full playback realistically needs a logged-in session).

**🧩 Playwright engine (optional)** — real Chromium for full JS,
screenshots, PDF. Same API. Auto-detects Camoufox on Termux.

**📱 WebView engine (new)** — `--engine webview` drives the
**Kancil Browser** Android app (`android/`, ~96 KB APK) on the same phone:
real Chromium via System WebView. Log in / solve captchas once in the app,
the Termux agent reuses that live session. Element queries pierce shadow DOM
+ same-origin iframes (cross-origin frames are unreachable by same-origin
policy); `click` reports the landing URL/title, `open --idle` settles the
page like a human. Includes JS console capture,
pattern-based request blocking (`kancil block add <pattern>`), popup-ad
killer (gesture-less auto-popups blocked outright; tap-triggered popups
closed if they navigate to an ad host), fullscreen video support
(`onShowCustomView`, back exits fullscreen), manual **DevTools**
(Console/JavaScript/Network per tab, no agent needed), night mode that
pierces shadow DOM (YouTube thumbnails stay normal), native
full-page + element screenshots, video listing, form fill, file upload,
download manager — all agent-controlled, zero new dependencies. A foreground
service ("Jaga agent tetap hidup") keeps the app alive against MIUI/EMUI
task killers. If the app does crash, an `UncaughtExceptionHandler`
auto-restarts it (max 3× per 5 min, loop-guarded) and the stacktrace stays
readable via `kancil --engine webview crashes`. **Stealth mode** (default ON) hides the small WebView tells:
strips `Version/4.0` from the UA, locks `navigator.webdriver`, stubs
`window.chrome` — the phone's genuine hardware does the rest. The app itself
wears an **Emerald & Gold** theme: warm ivory toolbar + deep emerald icons in
light mode, noir + champagne gold in dark mode, gold progress bar, gold-ringed
tab cards, rounded dialogs.

> **WebView = session-stateful: drive it from ONE process.** Every
> `kancil ...` CLI call is a new process that re-reads `/status`; if Android
> recreates the app in the background, tab IDs can shift between commands.
> For any multi-step flow use `kancil --engine webview shell` (one live
> session) or `kancil daemon start` — never per-command CLI for flows.
> New patterns pushed via `block add` scrub the HTTP cache and force
> `LOAD_NO_CACHE` on every tab while any pattern is active (block = block,
> nothing leaks through the cache).

## Quickstart

```bash
# Termux / Linux / macOS — zero dependencies
git clone https://github.com/leisdat/kancil && cd kancil
pip install -e .                      # or: python3 -m kancil (from repo root)

kancil open https://news.ycombinator.com --quiet
kancil scrape --selector ".athing" --fields "title:.titlelink" --max-items 10

# drive it like an agent
kancil tool '{"action":"snapshot"}'
kancil tool '{"action":"type","selector":"@textbox_1","text":"hello"}'

# cheat-code scraping: structured data, no selectors needed
kancil open https://komiku.org --quiet && kancil structured
kancil sitemap https://example.com --max-urls 100

# drive the real rendered page (Level 1)
kancil view --port 8901               # open http://127.0.0.1:8901 on your phone
kancil agent tabs
kancil agent cmd <tab> click '{"selector":"a.login"}'

# proxy your phone browser through Kancil (Level 2)
kancil proxy-ca                       # install ca.crt on the phone once
kancil serve-proxy --mitm             # set WiFi proxy -> 127.0.0.1:8080

# daemon mode: one warm engine, zero per-command startup (great on slow phones)
kancil daemon start                   # start once
kancil open https://example.com       # auto-routed through the daemon
kancil daemon stop                    # stop it (state is saved)

# drive the phone's real browser app (install android/kancil-browser.apk first)
kancil open --engine webview https://example.com
kancil --engine webview shell    # stable session: one process, no stale tabs
kancil --engine webview videos   # list <video> elements + direct src URLs
kancil --engine webview console  # real JS console capture
kancil --engine webview block add ads.example   # pattern request blocking
kancil --engine webview download https://example.com/f.zip
kancil --engine webview upload /sdcard/pic.jpg  # next file-chooser is fed
kancil --engine webview crashes  # last app crash report, if the APK died
# surf like a human: open + wait for settle, press keys, long-press
kancil --engine webview open https://example.com --idle  # readyState + network quiet
kancil --engine webview wait-idle   # same, as a separate step
kancil --engine webview press Enter            # real KeyboardEvent on focused element
kancil --engine webview press Escape --selector "#modal"
kancil --engine webview longpress ".tweet"     # mobile long-press (context menu)
kancil --engine webview scroll 600 --settle-ms 1200  # scroll + wait render
kancil --engine webview scroll 600 --verify ".new-post"  # wait for content
# validator pattern (Artemis): action only counts as done if the effect shows up
kancil --engine webview click "#btn" --verify "#done"
kancil --engine webview type "#q" "hello" --verify ".suggest"
kancil --engine webview open https://example.com --verify "#main"
# agent self-heal: if the app is dead, kancil auto-launches it via `am start`
kancil --engine webview launch        # manual (re)launch, waits for agent
kancil --engine webview open <url> --no-auto-launch  # opt out
kancil --engine webview upload ./photo.jpg   # file upload (base64, agent 1.19+)
kancil --engine webview blocklist add ads.com tracker.io  # block requests
kancil --engine webview console               # JS console logs
kancil --engine webview touch tap --selector "#play"  # synthesized tap
kancil --engine webview touch tap "Putar"     # visible text also works
kancil --engine webview touch swipe --x 100 --y 800 --x2 100 --y2 200
kancil --engine webview touch pinch_out --x 540 --y 900  # map zoom
kancil --engine webview touch swipe --x 100 --y 500 --x2 400 --y 500 --human
# ^ human-like swipe (bezier, ease-in-out) for behavior-checked sliders
kancil --engine webview network bodies          # captured XHR/fetch bodies
kancil --engine webview network response 12     # body of netlog entry (JSON pretty)
kancil --engine webview aliyun-solve            # closed-loop Aliyun slider solver
kancil --engine webview aliyun-analyze          # dry-run gap detection (tuning)
kancil --engine webview press Enter  # synthetic Enter; auto-submit form kalau key nggak ngapa-ngapain
kancil --engine webview press Enter --no-submit-fallback  # tanpa auto-submit
kancil --engine webview find "kata kunci"  # find-in-page: highlight + match count
# cookie session injection (agent 1.18+), UA override per tab
kancil cookies set sess abc123 --domain example.com
kancil --engine webview ua set "CustomUA/1.0"  # kancil ua reset untuk balik
kancil --engine webview composer-open  # m.facebook composer via warm nav
kancil --engine webview har export yt.har  # HAR dari netlog APK (auto-sync)
kancil --engine webview downloads  # dl_status + bytes_done/bytes_total per file
kancil doctor  # health check: engines, agent server, session, env
# dry-run: verify forms/composer freely, clicks+submits are blocked
# unless you pass --confirm (anti accidental publish)
kancil --engine webview --dry-run form fill 1 --set "isi=Halo"
kancil --engine webview --dry-run click "#post-btn" --confirm

# SPA warm navigation in one process: open -> click -> wait.
# m.facebook's composer only renders via click from the feed, NOT via
# direct URL (SPA needs the warm feed state) — same for many mobile SPAs:
kancil --engine webview click-through https://m.facebook.com \
  '[aria-label="Posting status baru"]' '[contenteditable="true"]'

# watch YouTube, mini style
kancil yt-play "termux tutorial" --port 8901
```

## Tests

```
230 unit tests (stdlib unittest) + live tests (public URLs, real Chromium)
$ python3 -m unittest tests.test_browser tests.test_stealth
$ python3 -m unittest tests.test_live      # needs network + chromium
```

## Stealth (browser impersonation)

Two opt-in layers, both dependency-light (`curl_cffi` is optional —
without it the engine silently falls back to urllib):

```bash
kancil stealth status                        # profiles, active transport
kancil stealth impersonate chrome_android    # static engine: TLS fingerprint
                                             # spoofing (chrome, firefox, safari,
                                             # edge, tor, chrome_android)
kancil stealth impersonate off               # back to urllib
kancil --impersonate tor open https://...    # one-shot via CLI flag
kancil stealth apply                         # webview engine: inject anti-detect
                                             # JS (webdriver, canvas, WebGL, ...)
```

```python
from kancil.api import Kancil
b = Kancil(engine="static", impersonate="chrome_android")
b.engine.set_impersonate("firefox")   # or None to disable

from kancil import stealth
stealth.apply_stealth(webview_engine)  # re-apply after each navigation
```

Injection-based spoofing covers the common fingerprinting vectors but can't
match engine-level patching (Camoufox-style) — a sophisticated checker can
still detect it. Kancil stays small on purpose.

## Session clear

```bash
kancil session clear                          # wipe everything
kancil session clear --what cookies           # cookies only
kancil session clear --what cookies --domain example.com  # one site only
```

Static engine wipes cookies (incl. the impersonated session jar), tabs,
netlog and cache. Webview closes tabs, clears netlog + WebView cache;
`all` never touches cookies (they hold your logins) — pass
`--what cookies` explicitly to wipe them (needs APK agent 1.17+).

## Project layout

```
kancil/                 # pip project root
  kancil/               # package: api, cli, dom, engines, devtools,
                        #   viewer, agent_bridge, proxy_server,
                        #   x509 (pure-Python certs), httpcache, ...
  tests/                # test_browser.py (unit) + test_live.py (live)
  assets/logo.png
  README.md / README.id.md / CHANGELOG.md
```

## Troubleshooting (webview engine)

**App frozen / agent timeout (MIUI, screen off).** Enable "Keep agent alive"
in Settings (default ON) — a foreground service + notification keeps task
killers away. If it still times out, wake it manually:
`am start -n com.kancil.browser/.MainActivity`.

**Stale tab IDs.** Every `kancil ...` is a new process that re-reads
`/status`; if Android recreates the app in the background, tab IDs can
shift. For a stable session use `kancil --engine webview shell` (one
process) or `kancil daemon start`.

**Per-app VPN: WebView shows a different IP than Termux.** Per-app VPNs only
tunnel selected apps — add Kancil Browser to the VPN allowlist. Alternative:
`kancil serve-proxy --mitm` on Termux (already VPN-tunneled) and route the
phone's traffic through it.

**Network log is request + headers only** (no response status/body — that
needs full CDP/MITM). HAR export works from the existing log.

## License

MIT — see [LICENSE](LICENSE).
