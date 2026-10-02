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
  <img src="https://img.shields.io/badge/tests-150%20unit%20%2B%205%20live-brightgreen" alt="tests">
  <img src="https://img.shields.io/badge/size-%7E100%20KB-blue" alt="size">
  <img src="https://img.shields.io/badge/dependencies-0-blue" alt="zero deps">
  <img src="https://img.shields.io/badge/python-3.8%2B-blue" alt="python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="license">
  <a href="README.id.md"><img src="https://img.shields.io/badge/README-Bahasa%20Indonesia-red" alt="indonesian"></a>
</p>

> A **browser built for AI agents** — navigation, DOM, DevTools, scraping,
> and a real-browser driving layer, all through a JSON-in/JSON-out interface.
> **~100 KB, zero dependencies.** Runs on Termux (Android ARM64), Linux, macOS.

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
`yt-video` metadata, `yt-play` mini player.

**🧩 Playwright engine (optional)** — real Chromium for full JS,
screenshots, PDF. Same API. Auto-detects Camoufox on Termux.

**📱 WebView engine (new)** — `--engine webview` drives the
**Kancil Browser** Android app (`android/`, ~24 KB APK) on the same phone:
real Chromium via System WebView. Log in / solve captchas once in the app,
the Termux agent reuses that live session.

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

# watch YouTube, mini style
kancil yt-play "termux tutorial" --port 8901
```

## Tests

```
150 unit tests (stdlib unittest) + 5 live tests (public URLs, real Chromium)
$ python3 -m unittest tests.test_browser
$ python3 -m unittest tests.test_live      # needs network + chromium
```

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

## License

MIT — see [LICENSE](LICENSE).
