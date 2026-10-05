# Changelog — Kancil

## Unreleased

### Fixed
- **Zombie tab** (APK agent 1.26, temuan live): satu tab WebView yang
  masuk loading permanen / renderer mati bikin seluruh evaluate JS
  balik null — termasuk tab yang sehat. Recovery sebelumnya manual
  (tutup tab, buka baru). Perbaikan:
  - `onRenderProcessGone` sekarang di-handle (sebelumnya tidak ada):
    WebView yang renderer-nya mati langsung di-revive — diganti
    WebView baru di tab yang sama (id, netlog, UA, URL terakhir
    dipertahankan), bukan dibiarkan jadi zombie.
  - **Zombie watchdog**: tiap tab dicatat `loadingSinceMs` di
    `onPageStarted` (dibersihkan di `onPageFinished` /
    `onReceivedError` main frame); watchdog tiap 15 detik memanggil
    `stopLoading()` pada tab yang loading > 60 detik.
  - **Eval per-tab**: `/js`, `/dom`, `/text`, `/reader` terima param
    `tab=` (body/query) — evaluasi langsung ke WebView tab itu tanpa
    activate dulu (tanpa race); tab tersangka bisa di-probe tanpa
    dijadikan aktif.
  - **evalJs hardening**: referensi WebView di-capture saat dipanggil
    (bukan saat runnable UI jalan) — menutup race activate-then-eval;
    WebView yang hancur gagal cepat dengan pesan jelas, bukan hang 30s.
  - **`/dom` jujur**: kalau tidak ada document, balas error 500
    ("no document (tab #N: still loading or renderer gone)") bukan
    null diam-diam. Python null-safe (`r.get("html") or ""`), jadi
    fallback-nya tetap jalan.
  - `/tabs` sekarang expose `loading`, `loading_ms`, `render_dead`,
    `unstuck` per tab — agent bisa melihat zombie dan menutupnya
    sendiri.
- **NetLog HTTP status** (APK agent 1.25): `NetLog.finish()` tidak pernah
  dipanggil — status di `network()` webview selalu null. Mitigasi:
  `latestFor(url)` + stamp 200 di `onPageFinished`, status asli di
  `onReceivedHttpError`, `fail()` di `onReceivedError`. Subresource tetap
  limitasi platform WebView (tidak expose status), tapi main frame dan
  error kini tercatat; `network(status=)` filter jadi bisa match.
- **Screenshot 1x1 palsu** (APK agent 1.25): `drawWebView()` menutupi
  WebView 0px dengan `Math.max(1, ...)` → PNG 1x1 97-byte yang "sukses".
  Sekarang: tunggu layout bounded (±2 dtk) sebelum capture, dan
  `drawWebView()` melempar error jelas ("webview not laid out") daripada
  gambar palsu.
- **Element screenshot "Can't compress a recycled bitmap"** (APK agent
  1.25): `Bitmap.createBitmap(subset)` bisa share pixel buffer dengan
  source (bahkan objek yang sama) — `bmp.recycle()` sebelum
  `crop.compress()` membunuh pixelnya. Urutan recycle dibetulkan
  (recycle setelah compress, guard identitas objek).
- **dry_run bolong** (Python): guard `dry_run`/`confirm` sekarang juga di
  `touch()`, `upload()`, `download()` — sebelumnya cuma `click()` dan
  `submit_form()`. `confirm` di-thread lewat API, CLI (`--confirm`), dan
  solver Aliyun.
- **pw_engine latent crash** (Python): `_sync_page()` pakai
  `html.encode("utf-8", "html")` — handler error `"html"` tidak valid,
  `LookupError` saat konten halaman punya lone surrogate. Ganti
  `errors="replace"` + regression test.
- **Stealth layering contract** (docs): didokumentasikan dua sumber
  snippet — baseline minimal MainActivity vs full 10-vektor
  `apply_stealth()` (menang saat overlap, idempotent) — plus flavor
  note: snippet desktop-Chrome (Win32) paling pas dipasangkan profil
  TLS `chrome124`, bukan default `chrome_android`.
- **Aliyun CORS fallback** (Python): kalau analisis pixel in-page gagal
  (fetch kena CORS di `static-captcha.aliyuncs.com`), solver otomatis
  fallback ke analisis Python/PIL — download native tanpa CORS,
  algoritma gray-veil yang sama. Butuh `pillow` (opsional; tanpa itu
  fallback dilewati dengan pesan jelas). Juga dipakai
  `aliyun-analyze`.

### Added
- **Agent UX overhaul (Hermes feedback, Python-only)**:
  - `Kancil.tool_schema()` + CLI `kancil agent-info [--action]` —
    manifest self-describing: 74 aksi (sekarang 115) dengan params
    (tipe/required/default/contoh), return shape, dan engine support,
    di-generate runtime dari `_TOOL_ACTIONS` (nggak pernah drift).
  - **State delta otomatis**: 16 aksi mutating (`open`/`click`/`type`/
    `touch`/`back`/...) melampirkan `delta{url,title,text_chars,
    media,shell}` — 0 RT tambahan di static, 1 probe `/js` di webview.
  - **Error envelope**: semua path error bawa
    `error{code,message,retriable,hint}`; `errors[]` tetap ada.
    Kode baru: `DRY_RUN_BLOCKED`.
  - **Cascade selectors** (Hermes #4): `click`/`type`/`clear`/`select`/
    `check`/`uncheck`/`hover`/`focus` terima string atau list — list
    dicoba berurutan; cuma `ELEMENT_NOT_FOUND` yang lanjut.
    `matched_selector`/`tried_selectors` di hasil.
  - **Verify `found[]`** (Hermes #5): verify gagal melampirkan
    `verify_found` — elemen interaktif yang ADA di halaman.
  - **tool() parity** (Hermes #6): 35 engine op masuk `_TOOL_ACTIONS`
    (74 → 109): `touch`, `upload`, `network_bodies`, `stealth_*`,
    `block_*`, `ua_*`, `proxy_*`, `cookies_set`, `session_clear`,
    `find`/`press`/`longpress`, `wait_idle`, `click_through`, `har_*`,
    `download_pause/resume`, `bookmark_*`, `a11y_list`, aliyun.
- **GitHub Actions CI** (`.github/workflows/test.yml`): tiap push/PR
  ke main jalanin `pytest tests/ --ignore=tests/test_live.py` +
  compile check. `test_live.py` sengaja di-exclude (butuh network
  publik + Chromium + playwright, opt-in manual).
- **Vision: "mata" buat agent text-only** (`kancil/vision.py`,
  Python-only, tanpa rebuild APK): screenshot → locate → tap, buat
  Hermes/model yang nggak support vision. Backend pluggable:
  `template` (OpenCV template matching, offline, opsional),
  `api` (vision model via OpenAI-compatible chat API —
  `KANCIL_VISION_ENDPOINT`/`KANCIL_VISION_KEY`/`KANCIL_VISION_MODEL`,
  provider `openai`/`anthropic`), `callback` (fungsi sendiri).
  Koordinat 0-1000 ternormalisasi → CSS px via viewport. API:
  `vision_locate()` / `vision_describe()` / `vision_calibrate()` /
  `see_tap()` (verify + retry antar kandidat) / `see_type()`
  (ketik via `elementFromPoint`, tanpa selector) / `see_drag()`.
  CLI: `kancil see-locate|see-tap|see-type|see-drag|see-describe`.
  `dry_run`/`confirm` di-respect; error envelope + state delta ikut.
  Manifest: 109 → **115 tool actions**. Batas jujur: template rapuh
  kalau UI ganti tema; backend `api` butuh network + key.
- **Aliyun closed-loop slider solver** (APK agent 1.24): port pendekatan
  0xgetz/aliyun-puzzle-solver (MIT) — deteksi gap via analisis pixel
  in-page (gray veil: saturasi rendah + brightness mid/high), lalu drag
  closed-loop: `/touch down` → loop `move` kecil (3–14px, ~32ms) sambil
  baca `style.left` tiap step, berhenti di target → `up`. Primitif touch
  baru `down`/`move`/`up` (satu downTime per gesture, diingat per tab).
  Python `kancil/aliyun.py` + API `Kancil.solve_aliyun_puzzle()` + CLI
  `kancil aliyun-solve`. Full fitur (Python-only): auto-detect selector,
  refresh-on-fail, fallback traceless, `aliyun-analyze` dry-run, timing
  adaptif, verifikasi mask hilang / teks sukses. Tanpa garansi — arms
  race; Descope lockout 5x/180 dtk (max_tries default 4).
- **Response-body capture** (APK agent 1.23): `/network` tidak cuma metadata
  lagi — semua response XHR/fetch direkam (64KB cap, 60 terakhir per tab)
  via patch `fetch`/XHR di JS. Jalur aman: observe-only, tanpa
  `shouldInterceptRequest`, jadi loading native tidak disentuh. Endpoint
  `GET /network/bodies[?clear=1]`; Python `network_bodies()` /
  `network_response(rid)` (bentuk hasil sama kayak static engine, JSON
  di-pretty); CLI `kancil network bodies [clear]` + `kancil network
  response <id>`. Limit jujur: cuma XHR/fetch, bukan dokumen/gambar.
- **Human-like swipe** (APK agent 1.22): `/touch` terima flag `human` —
  swipe pakai trajektori bezier melengkung, ease-in-out, jitter, micro-pause
  ala jari manusia. Python `touch(..., human=True)`, CLI `--human`. Untuk
  slider yang ada behavior analysis (mis. Aliyun CAPTCHA); tanpa garansi
  lolos — arms race.
- **`/touch` endpoint** (APK agent 1.21): tap / swipe / longpress /
  pinch sintetis via `MotionEvent` → `dispatchTouchEvent()` — koordinat CSS
  px (app konversi pakai skala WebView). Python `touch(action, x, y, x2,
  y2, selector, duration_ms, distance_start, distance_end)` dengan targeting
  standar engine (CSS, XPath, `@ref` a11y, teks visible — semudah `click()`),
  plus `pinch_in`/`pinch_out` buat zoom peta. API `Kancil.touch()`, CLI
  `kancil touch`. Untuk elemen yang `click()` JS tidak bisa drive (canvas,
  map, custom gesture).
- **Upload base64** (APK agent 1.19): `POST /upload` terima `{filename, data}`
  base64 selain `{path}` — file dari direktori privat Termux bisa di-upload
  langsung; Python `upload()` selalu kirim base64 (fallback path-staging
  untuk agent lama). Batas 5MB.
- **CLI `blocklist`** (`status|add|clear [patterns]`): `block_add/block_list/
  block_clear` akhirnya terekspos ke CLI (sebelumnya cuma engine+API).
- **Modul stealth** (`kancil/stealth.py`): impersonasi fingerprint TLS untuk
  static engine via `curl_cffi` (opsional — fallback otomatis ke urllib kalau
  tidak ter-install). 6 profil: `chrome`, `chrome_android` (default),
  `firefox`, `safari`, `edge`, `tor`. Adapter kompatibel-urllib: netlog,
  cache, cookies, dan retry tidak berubah.
- **Snippet anti-detect JS** untuk engine webview
  (`stealth.apply_stealth(engine)`): spoof `navigator.webdriver`, plugins,
  noise canvas, vendor/renderer WebGL, `userAgentData`, `window.chrome`,
  permissions, battery, dan audio fingerprint. Injeksi ulang tiap navigasi.
- API: `Kancil(engine="static", impersonate=...)`,
  `stealth_status()` / `stealth_impersonate()` / `stealth_apply()`;
  CLI: `kancil stealth <status|impersonate|apply>` + flag global
  `--impersonate` (tersimpan di sesi).
- `kancil doctor` sekarang melaporkan status stealth (ketersediaan
  curl_cffi + daftar profil).
- `click(..., verify=...)`: pola validator ala Artemis — klik bisa
  memverifikasi selector yang harus muncul sesudahnya.
- **`kancil session clear`** (`--what all|cookies,tabs,netlog,cache`,
  `--domain`): wipe state sesi berjalan. Static engine: cookies (termasuk
  jar sesi impersonasi), tab, netlog, cache; `--domain example.com` untuk
  surgical cookie wipe satu situs. Webview: tab + netlog + cache; cookies
  hanya bila diminta eksplisit (`all` tidak menyentuh cookies demi
  melindungi login).
- **APK agent 1.17**: endpoint baru `/cookies/clear` (semua atau per-domain)
  dan `/cache/clear` — dipakai `WebViewEngine.cookies_clear()` /
  `cache_clear()`.
- **APK agent 1.18 — full-power agent**: `/cookies/set` (injeksi sesi:
  name/value/domain/path/maxAge), `/ua/set` + `/ua/reset` (override UA per
  tab), `/find` (find-in-page: highlight + jumlah match, `next` untuk lompat).
  Python: `WebViewEngine.cookies_set/set_user_agent/reset_user_agent/find`,
  `StaticEngine.cookies_set/find` (find static: hitung + snippet dari teks
  halaman), API `cookies_set/ua_reset/find`, CLI `kancil cookies
  <list|set|clear>`, `kancil find <text> [--next]`, `kancil ua reset`
  (`ua set` kini juga jalan di webview).
- **Pola validator Artemis diperluas**: `type --verify` dan `open --verify`
  (engine webview; CLI, API, dan tool-action ikut mendukung). Aksi hanya
  dihitung tuntas kalau efeknya benar-benar muncul di halaman.

### Notes
- 230 unit tests hijau (23 baru: modul stealth + wiring API/CLI).
- Batasan jujur: spoofing injeksi-JS tidak sedalam patch engine-level
  ala Camoufox; Kancil sengaja tetap kecil.

## 3.14.0 (2026-10-03)

Kancil Browser: aplikasi Android pendamping + engine `webview`.

### Added
- **Kancil Browser APK** (`android/`, ~24 KB, signed): browser berbasis
  System WebView (Chromium beneran) dengan agent HTTP server di
  `127.0.0.1:8080` — navigate/dom/js/click/type/network/cookies/screenshot,
  network log ala DevTools, pill status + toast aktivitas agent.
  Build tanpa Gradle: `./build.sh` (aapt2 + d8 langsung).
- **Engine `webview`** (`kancil/webview_engine.py`): `Kancil(engine="webview")`
  / `kancil open --engine webview …` — nyetir aplikasi di HP yang sama
  lewat HTTP API-nya. Login/captcha cukup sekali di app, agent di Termux
  pakai session-nya terus (JS, video, screenshot, localStorage beneran).
  Gagal cepat dengan pesan jelas kalau app belum dibuka.
- `Kancil(..., webview_host=, webview_port=)` — override alamat agent.
- **Multi-tab** di app + engine: tiap tab WebView + network log sendiri,
  counter tab + dialog switcher di toolbar, agent API
  `/tabs /tabs/new /tabs/activate /tabs/close`.
- **Pilihan search engine** (Google/DuckDuckGo/Brave/Bing) di pengaturan —
  kalau SafeSearch Google dikunci jaringan, pindah engine solusinya.
- **Adblock ringan** (20 pola host iklan/tracker, di-log sebagai
  `blocked:adblock`), **reader mode** (tombol + `POST /reader`),
  **download manager**, **hemat data** (blokir gambar), **situs desktop**
  (toggle UA), **mode malam** (CSS filter), **cari di halaman**.

### Notes
- 156 unit tests hijau (6 baru: mock agent server — open/page/actions/
  screenshot/cookies/network, fail-fast, routing API).

## 3.13.0 (2026-10-03)

Daemon mode: satu proses persistent, nol biaya startup per command.

### Added
- `kancil daemon {start|stop|restart|status}` — background engine
  (Unix socket `~/.kancil/daemon.sock`, protokol JSON, stdlib only).
- Auto-route: kalau daemon jalan, semua command (`open`, `tool`, `dom`,
  `network`, …) otomatis lewat daemon — engine tetap hangat (DNS cache,
  keep-alive, cookies, tabs persist antar command). `--local` untuk
  paksa jalan di proses sendiri.
- Bonus: tabs/cookies sekarang persist antar invocasi CLI saat daemon aktif.
- `fetch(..., method=...)` — PUT/PATCH/DELETE untuk kerja API.

### Notes
- 150 unit tests hijau (4 baru: daemon start/ping/dispatch/stop).

## 3.12.0 (2026-10-02)

Network log ala DevTools: query params + cookies per request.
(Jujur: ini request log + header, tanpa response status/body — bukan
DevTools penuh.)

### Added
- Setiap entri netlog (static + playwright) sekarang bawa `query`
  (dict hasil parse), `cookies_sent` (dari header Cookie yang dikirim),
  dan `cookies_set` (parse `Set-Cookie`: name/value/domain/path/
  expires/secure/httponly/samesite).
- `har_export` ngisi `request.cookies` / `response.cookies` (nilai
  tetap di-redact default).
- `kancil cookies` tetap untuk isi jar; cookies_import tetap untuk
  warisi sesi browser asli.

### Notes
- 146 unit tests hijau (3 baru).

## 3.11.0 (2026-10-02)

Tiga optimasi sekaligus.

### Added
- Auto-scrape `articles` dirombak: judul diambil dari heading link
  (h1-h4), link lain dipisah (teks + URL), thumbnail dari
  `data-src`/`src`, sisa teks jadi `meta`, nomor rank di-strip.
  `"1Overgeared NewFantasi · 74,276 viewsChapter 341"` ->
  `title="Overgeared New"`, `meta="Fantasi · 74,276 views"`,
  `links=[Chapter 341]`. Field `text` lama tetap ada (kompatibel).
- HTTP cache (`kancil/httpcache.py`): ETag / Last-Modified revalidation
  untuk static engine. 304 -> body dari cache (`~/.kancil/cache/`,
  cap 50 MB). Hormati `Cache-Control: no-store`. `--no-cache` untuk
  bypass; `kancil http-cache [stats|clear]` + tool action `http_cache`.
- DNS cache: `socket.getaddrinfo` di-wrap dengan TTL 5 menit
  (lock-guarded, max 2000 entri). Otomatis aktif di static engine.

### Notes
- 143 unit tests hijau (7 baru). Cache tidak pernah stale: entri tanpa
  validator tidak disimpan, jadi tiap hit selalu revalidasi (304 murah).

## 3.10.0 (2026-10-02)

Level 2 tanpa openssl, Level 1 dirampingkan.

### Added
- `kancil/x509.py`: RSA-2048 + X.509 murni stdlib (Miller-Rabin,
  PKCS#1 v1.5, DER). `proxy-ca` dan `serve-proxy --mitm` tidak butuh
  binary openssl lagi — `pkg install openssl` hilang dari syarat.
  openssl CLI tetap jadi fallback kalau tersedia.
- Fingerprint CA dihitung murni Python (format `SHA256 Fingerprint=..`).

### Changed
- Level 1 (viewer/agent_bridge): hapus docstring ganda, hoist import ke
  modul, hilangkan double-parse URL di routing agent. Tetap 0 dependency.

### Notes
- 136 unit tests hijau (4 baru: x509 roundtrip, verifikasi signature,
  ensure_ca, host_cert + `openssl verify` silang kalau tersedia).

## 3.9.0 (2026-10-02)

Scraper naik kelas: structured data, sitemap crawl, concurrent fetch.
Lahir dari test scrape komiku.org.

### Added
- `structured` (tool action + `kancil structured`): ekstrak JSON-LD
  (@graph di-flatten), OpenGraph, Twitter Card, dan meta
  description/keywords/author. Data mesin-baca yang memang disediakan
  situs — sering lebih bersih dari scraping teks visual.
- `sitemap` (tool action + `kancil sitemap [url]`): daftar URL dari
  sitemap.xml — via baris `Sitemap:` di robots.txt, fallback
  `/sitemap.xml`. Mendukung sitemapindex (rekursi, depth<=2) dan .xml.gz.
- `scrape --sitemap`: crawl pakai URL dari sitemap, bukan nebak
  pagination (`?halaman=N`). Coverage penuh, halaman gagal (404) di-skip
  tanpa membunuh crawl.
- `scrape --sitemap --workers N`: fetch N halaman paralel (static engine,
  max 8, tiap worker engine sendiri — thread-safe by isolation).
  Contoh: `kancil scrape https://komiku.org/ --sitemap --selector h1
  --fields title:h1 --pages 8 --workers 4`.

### Notes
- workers butuh sitemap (URL list diketahui di awal); pagination biasa
  tetap sekuensial karena tiap halaman menemukan halaman berikutnya.
- 132 unit tests hijau (9 baru).

## 3.8.0 (2026-10-02)

Menutup temuan sesi: `--session` sekarang persisten beneran + `open`
bisa nunggu hydrate.

### Added
- `--session NAME` sekarang load storage_state di awal DAN save otomatis
  saat command selesai (sebelumnya cuma load — cookie hasil challenge
  mati bareng browser). Alur Reddit:
  `kancil --engine playwright --pw-browser firefox --session reddit
  open https://www.reddit.com/ --wait-ms 8000`
  lalu command berikutnya dengan `--session reddit` mewarisi cookie.
- `open --wait-ms N`: tunggu N ms setelah load (playwright: kasih waktu
  JS hydrate sebelum query).
- Playwright auto `--no-sandbox` saat jalan sebagai root (docker/proot).

### Notes
- `kancil repl --engine playwright` tetap cara terbaik untuk sesi
  interaktif panjang: satu browser, banyak command.

## 3.7.0 (2026-10-02)

Anti-bot hygiene (jujur): mengurangi sinyal bot untuk deteksi naif +
jembatan trust dari browser asli. Tetap 0 dependency.

### Added
- Browser-like default headers di static engine: `Accept`,
  `Accept-Language`, `Accept-Encoding: gzip, deflate`,
  `Upgrade-Insecure-Requests`, `Sec-Fetch-*` (bisa override via
  extra_headers). Request "cuma User-Agent" adalah sinyal bot.
- Dekompresi gzip/deflate otomatis (karena sekarang di-advertise).
- `kancil cookies-import <file>` + tool action `cookies_import`:
  import Netscape-format cookies.txt (hasil export dari browser asli).
  Alur praktis: selesaikan challenge sekali di browser HP → export
  cookie → static engine mewarisi sesi terpercaya.
- `kancil agent cmd <tab> console` masuk ke pilihan CLI (sebelumnya
  cuma bisa via tool JSON).

### Notes
- Reddit (`www.reddit.com`) masih menyajikan JS challenge page ke
  static engine — itu batas arsitektur (challenge butuh eksekusi JS),
  bukan bug. Jalur yang benar: Level 2 proxy (browser HP asli
  menyelesaikan challenge secara native) atau cookies-import.

## 3.6.0 (2026-10-02)

Optimasi: HTTP keep-alive pooling + batas memori netlog. Tetap 0 dependency.

### Added
- Keep-alive connection pooling di static engine: koneksi TCP/TLS dipakai
  ulang per origin (sebelumnya tiap request buka koneksi baru — urllib
  sengaja mematikan keep-alive). Stale connection otomatis di-drop dan
  request di-retry sekali. Tidak aktif saat proxy dipakai (tunneling
  punya semantik koneksi sendiri). `StaticEngine.close()` menutup
  koneksi pool.
- Netlog dibatasi 1000 entri terakhir (sebelumnya tak terbatas — sesi
  scraping panjang bisa bengkak memori).

### Fixed
- `kancil.__version__` di-bump ke 3.6.0 (sebelumnya tertinggal di 3.4.0
  walau CHANGELOG sudah 3.5.0).

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

## Unreleased (webview fixes, dari live test)
- APK: SEMUA akses metadata tab/WebView dipindah ke UI thread (`uiGet`) — fix bug live `tabs` kosong semua (worker thread baca `getUrl()` = null).
- Engine: XPath beneran via `document.evaluate` (`//...`, `xpath=...`) di resolve/click/type/focus/dll.
- Engine: smart text resolve — `click "Login"` (teks polos) fallback ke XPath text match kalau CSS nggak kena.
- Engine: `type`/`clear` pakai native value setter (kompatibel React/Vue/Angular, bukan `execCommand`).
- Engine: `hover` kirim mouseover/mouseenter/mousemove events (bukan cuma focus).
- Engine: `wait(text=...)` diimplementasikan (polling `innerText`).
- Engine: `scroll` dukung selector (scrollIntoView).
- Engine: `screenshot(full/selector)` gagal eksplisit, bukan diam-diam diabaikan.
- Engine: `network()` filter `type_`/`status` beneran dipakai.
- Capabilities jujur: `indexeddb`/`computed_style`/`forms`/`console_capture` = False.
- 13 unit tests WebView (6 baru: xpath, smart text, wait text, hover/scroll, screenshot flags, capabilities).

## Unreleased (UI review P0+P1)
- APK UI: status agent ramah ("● Agent aktif" / "○ Agent terputus", dot warna, contentDescription) — port cuma di dialog Agent.
- APK UI: error page beneran (pesan + URL + [Coba lagi] [Kembali]) ganti toast.
- APK UI: vector drawable sendiri (back/forward/menu/close), tombol tab "▣ N" + label aksesibilitas.
- APK UI: dark mode native (Theme.Kancil.Dark, recreate saat toggle) — toolbar/dialog ikut gelap.
- APK UI: tab switcher kartu (judul+URL+●/○, tombol ✕ per tab, + tab baru).
- APK UI: dialog "Agent API" (status, tab aktif, contoh command + salin, 10 log terakhir).
- APK UI: toast agent berwarna (info/ok/warn/error) + log persisten 50 entri.
- APK UI: navigate() kenali localhost/IP:port sebagai URL (http://).
- APK UI: WindowInsets — toolbar aman dari notch/status bar.
- APK: 73 KB (naik 6 KB dari 67 KB).

## Unreleased (bugfix dari live test Hermes)
- API: `extract(mode="article")` KeyError 'text' → pakai `data["article"]` (bug di semua engine, bukan cuma webview).
- Engine webview: `storage()` normalisasi hasil `"null"`/None jadi `{}` (fix TypeError di `storage list`); api.storage juga defensif.
- Engine webview: method `forms()` ditambahkan (sebelumnya AttributeError).
- Engine webview: `switch_tab` verifikasi via /status setelah activate — ID basi (app di-recreate OS) kini gagal jujur, bukan "sukses" palsu.
- APK: `restoreTabs()` pertahankan ID tab asli (sebelumnya tiap recreate kasih ID baru → ID agent basi). `Tab.id` tidak final lagi.
- 17 unit tests WebView (4 baru: forms, storage null, switch verify, extract article).

## Unreleased (audit menyeluruh)
- Audit: semua `engine.X()` di api.py ter-cover di 3 engine (static: 4 guarded, webview: lengkap, playwright: inherit static).
- Engine webview: `_el_err()` — JS error (`ERR:`) dan selector invalid kini gagal jujur di click/type/clear/select/hover/focus/scroll (sebelumnya sukses palsu).
- Engine webview: `wait`/`resolve` tahan `int("null")`; `storage_get` balikin None (bukan string "null") untuk key kosong.
- APK: `findTab` di `/tabs/activate` pindah ke UI thread (hindari ConcurrentModificationException).
- Bersih-bersih: 11 unused import dihapus (api, cli, engines, devtools, pw_engine, proxy_server).
- 169 tests hijau (19 webview).
