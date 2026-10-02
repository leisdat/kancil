# Changelog — Kancil

## 3.5.0 (2026-10-02)

Moat update: memperkuat yang Hermes tidak punya, menetralkan keunggulan
render tanpa bundel browser.

### Added
- `network_curl` tool action: replay request dari netlog jadi perintah curl
  siap paste (`kancil tool '{"action":"network_curl","id":3}'`). Aman
  shell-quoting, termasuk POST body.
- `batch` tool action: banyak aksi dalam 1 call (`actions` + opsional
  `stop_on_error`), hemat roundtrip LLM. Maks 25 aksi.
- `serve-proxy --mitm`: kalau MITM gagal aktif, tampilkan langkah perbaikan
  (install openssl → proxy-ca → install cert → re-run); kalau aktif,
  tampilkan fingerprint CA untuk verifikasi.
- `agent.js`: console capture (hook console.log/warn/error, buffer 200,
  diambil via `kancil agent cmd <tab> console`) + auto-reconnect
  (backoff eksponensial, re-register setelah 5 gagal — tahan kalau
  bridge restart).
- Playwright engine: auto-detect binary Camoufox di cache Termux
  (`/data/data/com.termux/cache/camoufox/...`) dan `~/.cache/camoufox`
  saat `--pw-browser firefox` tanpa `--executable-path`.

## 3.4.1 (2026-10-02)

Termux-friendly: playwright engine sekarang dukung browser custom & aman
dari LD_PRELOAD.

### Added
- `--pw-browser {chromium,firefox,webkit}` + `--executable-path PATH`
  (atau env `KANCIL_BROWSER_PATH`): pakai binary browser sendiri, mis.
  Camoufox yang ke-cache di Termux:
  `kancil --engine playwright --pw-browser firefox --executable-path
  /data/data/com.termux/cache/camoufox/browsers/.../firefox`
- Playwright engine otomatis menghapus `LD_PRELOAD` dari environment
  browser child (memperbaiki crash `CANNOT LINK EXECUTABLE:
  libtermux-exec.so` di Termux).

### Fixed
- `kancil proxy-ca` tanpa openssl sekarang kasih pesan jelas:
  `pkg install openssl` (Termux) / `apt install openssl` (Debian/Ubuntu).

## 3.4.0 (2026-10-02)

Browser beneran, kancil tetap kecil: Chrome HP jadi mesin render, kancil
jadi lapisan agent di atasnya.

### Added (Level 1 — injected JS agent)
- Viewer menyuntik `agent.js` ke halaman yang diserve: Chrome HP me-render
  penuh (JS jalan), kancil bisa snapshot DOM asli, klik, ketik, scroll,
  eval JS. Command: `kancil agent tabs|cmd|snap`; tool actions
  `agent_tabs`, `agent_cmd`, `agent_snapshot`.
- Terbukti live: Chromium asli buka example.com via viewer → agent snapshot
  → klik link → navigasi ke iana.org → eval di live DOM.

### Added (Level 2 — local proxy)
- `kancil serve-proxy [--mitm]`: proxy HTTP + CONNECT. Chrome HP set proxy
  WiFi ke sini → semua traffic lewat kancil, agent.js disuntik otomatis.
- `kancil proxy-ca`: generate CA (openssl) untuk MITM HTTPS; install sekali
  di HP. Tanpa --mitm: CONNECT di-tunnel buta (host tercatat).
- Forwarder pakai urllib (hormat HTTP(S)_PROXY env) — tanpa dependency baru.
- Terbukti live: HTTP proxy + MITM HTTPS ke example.com (injeksi +
  logging), blind CONNECT tunnel.

### Fixed
- Rewrite `<a href>`: tangani href tanpa quotes (contoh: example.com).
- Semua URL gateway di viewer kini absolut (relatif rusak karena `<base>`).
- `agent.js`: BASE dihitung dari `document.currentScript.src`; lapor
  URL asli via `data-kancil-url`.
- `serve-proxy`: perbaiki call-site `_handle_http` (argumen `fp` nyasar).

### Tests
- 102 unit tests + 5 live tests (tests/test_live.py, URL publik + Chromium
  asli) — semua hijau.

## 3.3.0 (2026-10-02)

### Added
- **`kancil yt-play <url|id|query>`**: mini YouTube player. Query langsung
  → hasil pertama diputar. Halaman player pakai embed resmi YouTube
  (audio + video + fullscreen, browser HP yang memutar). Tanpa dependency
  baru (YouTube tidak lagi menyediakan stream progresif muxed — hanya DASH
  terpisah). Tool action: `yt_play`.
- 95 → **99 tests** (video ID extraction, player page, /play route,
  yt_play API).

### Fixed
- `yt-search` / `yt-play`: `nargs=REMAINDER` menelan `--port`/`--json`/`--max`
  menjadi bagian query. Ganti ke `nargs="+"` — flag kini terparse benar.

## 3.2.0 (2026-10-02)

### Added
- **`kancil view` (local viewer)**: lihat halaman di browser sendiri dan
  ambil alih navigasi. Engine static → halaman diserve lewat gateway lokal
  (`/__kancil__/go`, `/page`), link/form di-route lewat Kancil; engine
  playwright → screenshot live + klik/ketik diteruskan ke Chromium.
  Bind 127.0.0.1 saja. Tool actions: `view`, `view_stop`.
- 91 → **95 tests** (rewrite_html, gateway routes static/playwright,
  view API).

## 3.1.0 (2026-10-02)

Audit eksternal oleh Hermes (agent) + temuan live test. Semua P0/P1 diperbaiki
tanpa rewrite arsitektur; API/CLI tetap kompatibel.

### Fixed (P0)
- **README command path**: `python3 -m kancil` harus dari project root, bukan
  dari dalam folder package. README diperbaiki + regression test.
- **Packaging**: tambah `pyproject.toml` (setuptools, dynamic version dari
  `kancil.__version__`), entry point `kancil = kancil.cli:main`,
  `pip install -e .` terverifikasi. Layout jadi `kancil/` project root +
  `kancil/kancil/` package.
- **Race condition state.json**: `StateLock` (fcntl, best-effort) di sekitar
  siklus load → dispatch → save di CLI; per-command lock di REPL.
- **Netlog pollution**: `import_state` tidak lagi me-refetch tab aktif —
  materialisasi lazy murni via properti `page`. Command read-only
  (`tabs`, `network`, ...) tidak menyentuh network sama sekali.
  Entry retry kini bernomor (`attempt`/`attempts`).
- **crawl.txt di CWD**: output crawl sekarang ke
  `~/.kancil/crawl-<timestamp>.txt`.

### Fixed (P1)
- **state.json bloat**: netlog yang dipersist dipangkas ke metadata saja
  (tanpa body/header), max 200 entries. `network response <id>` jujur
  melapor bila body tidak dipersist antar restart.
- **CSS parser**: tambah sibling combinator `+`/`~`, `:not()`,
  `:first-child`, `:last-child`, `:nth-child()`, operator atribut
  `^=`/`$=`/`*=`/`~=`, `:disabled`/`:enabled`. Pseudo dinamis (`:hover`, ...)
  ditolak dengan pesan jelas.
- **Tag soup**: aturan auto-close HTML5 sederhana di parser
  (`<p>`, `<li>`, `<td>`, `<option>`, `<a>`, ...). Tetap nol dependency.
- **Selector errors**: `check_selector()` — bedakan "selector invalid"
  (`INVALID_INPUT` + hint) vs "valid tapi kosong" (`count: 0` + hint).
  Text query ("Learn more") tidak kena validasi CSS.
- **perf**: `requests` kini dihitung sejak halaman dibuka, bukan kumulatif
  session.

### Added
- **Embedded JSON extraction**: `page-json` — ambil JSON blob di HTML mentah
  (`ytInitialData`, `__NEXT_DATA__`, `ld+json`, ...).
- **`yt-search "<query>"`**: hasil pencarian YouTube via ytInitialData
  (tanpa JS). **`yt-video <url>`**: metadata via og: tags.
  Keduanya tersedia sebagai tool action (`yt_search`, `yt_video`, `page_json`).
- **`kancil --version`**.

### Tests
- 74 → **88 tests**, semua hijau. Termasuk: no-fetch-on-import,
  StateLock, CSS extensions, check_selector, auto-close soup,
  embedded JSON/ytInitialData/og:, perf per-page.

## 3.0.0 (2026-10-02)

Upgrade besar: Network DevTools + HAR session + redaction, a11y tree +
stable refs, scraper pagination (numbered/dedup/robots.txt), persistent
Playwright session via `storage_state`, Agent Tool Interface
(`kancil tool`, 59 actions, error codes), snapshot, observability.
74 tests hijau. Rename Hermes Browser → Kancil.
