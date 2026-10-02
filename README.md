<p align="center">
  <img src="assets/logo.png" width="160" alt="Kancil logo">
</p>

<h1 align="center">Kancil</h1>

<p align="center">
  <b>The tiny agent browser.</b><br>
  Small but clever — like the mousedeer of Indonesian folklore.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-3.7.0-brightgreen" alt="version">
  <img src="https://img.shields.io/badge/tests-120%20unit%20%2B%205%20live-brightgreen" alt="tests">
  <img src="https://img.shields.io/badge/size-106%20KB-blue" alt="size">
  <img src="https://img.shields.io/badge/dependencies-0-blue" alt="zero deps">
  <img src="https://img.shields.io/badge/python-3.8%2B-blue" alt="python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="license">
  <a href="README.id.md"><img src="https://img.shields.io/badge/README-Bahasa%20Indonesia-red" alt="indonesian"></a>
</p>

Kancil is a **browser built for AI agents** — navigation, DOM, DevTools,
scraping, and a real-browser driving layer, all through a JSON-in/JSON-out
interface. **106 KB, zero dependencies**, runs on Termux (Android ARM64),
Linux, and macOS.

```bash
pip install -e .
kancil open https://example.com --quiet
kancil tool '{"action":"click","selector":"@button_1"}'
kancil yt-search "termux tutorial" --max 5
```

## Why Kancil?

Regular browsers are built for humans. Agent frameworks bolt automation on
top with heavy drivers. Kancil inverts that: **the agent is the first-class
user**.

| | Kancil | Selenium / Playwright scripts |
|---|---|---|
| Install size | **106 KB, 0 deps** | 150 MB+ browser download |
| Runs on Termux Android | ✅ | ❌ (needs proot hacks) |
| Agent interface | Native: 68 JSON actions | You write the wrapper |
| Sees JS-rendered pages | ✅ via injected agent | ✅ |
| Drives your real phone browser | ✅ proxy + agent.js | ❌ |
| Honest capability reporting | ✅ `devtools --json` | — |

## Features

**Core browsing** — tabs, history, cookies, smart `click "Login"` resolution,
stable a11y refs (`@button_1`) that survive CLI restarts.

**Network DevTools** — request log with timing/sizes, HAR export with secret
redaction on by default, header inspection.

**Scraper** — pagination (numbered/next/infinite-scroll), dedup, robots.txt,
`extract` (article/links/images/tables).

**Agent Tool Interface** — 70 actions, JSON-in/JSON-out, structured error
codes (`ELEMENT_NOT_FOUND`, `TIMEOUT`, …). Built for LLM agents.
`batch` runs many actions in one call; `network_curl` replays any logged
request as a copy-pasteable curl command.

**YouTube kit** — `yt-search` (via `ytInitialData`, no JS needed), `yt-video`
metadata, `yt-play` mini player, `page-json` embedded-JSON extraction
(`__NEXT_DATA__`, `ld+json`, …).

**Real-browser mode** — two levels, Kancil stays tiny, your phone's Chrome
does the rendering:
- **Level 1**: `kancil view` injects `agent.js` — snapshot the real DOM,
  click, type, eval JS, capture console logs. Auto-reconnects with
  exponential backoff. `kancil agent tabs|cmd|snap`.
- **Level 2**: `kancil serve-proxy [--mitm]` — route your phone browser's
  traffic through Kancil. Works on login-walled sites because the requests
  come from your real browser. `--mitm` prints fix steps when the CA isn't
  ready, and the CA fingerprint when it is.

**Playwright engine** (optional) — real Chromium when you need full JS,
screenshots, PDF, request blocking. Same API. `--pw-browser firefox`
auto-detects a cached Camoufox binary on Termux; `LD_PRELOAD` is stripped
automatically so Termux's libtermux-exec doesn't crash the browser.

## Quickstart

```bash
# Termux / Linux / macOS — zero dependencies
git clone https://github.com/leisdat/kancil && cd kancil
pip install -e .                      # or: python3 -m kancil (from repo root)

kancil open https://news.ycombinator.com --quiet
kancil scrape --selector ".athing" --fields "title:.titlelink" --max 10 --json

# drive it like an agent
kancil tool '{"action":"snapshot"}'
kancil tool '{"action":"type","selector":"@textbox_1","text":"hello"}'

# watch YouTube, mini style
kancil yt-play "termux tutorial" --port 8901
# open the printed URL on your phone

# drive the real rendered page (Level 1)
kancil view --port 8901               # open http://127.0.0.1:8901 on your phone
kancil agent tabs
kancil agent cmd <tab> click '{"selector":"a.login"}'

# proxy your phone browser through Kancil (Level 2)
kancil proxy-ca                       # install ca.crt on the phone once
kancil serve-proxy --mitm             # set WiFi proxy -> 127.0.0.1:8080
```

## Tests

```
102 unit tests (stdlib unittest) + 5 live tests (public URLs, real Chromium)
$ python3 -m unittest tests.test_browser
$ python3 -m unittest tests.test_live      # needs network + chromium
```

## Project layout

```
kancil/                 # pip project root
  kancil/               # package: api, cli, dom, engines, devtools,
                        #          viewer, agent_bridge, proxy_server, ...
  tests/                # test_browser.py (unit) + test_live.py (live)
  assets/logo.png
  README.md / README.id.md / CHANGELOG.md
```

## License

MIT — see [LICENSE](LICENSE).
