<p align="center">
  <img src="assets/logo.png" width="160" alt="Logo Kancil">
</p>

<h1 align="center">Kancil</h1>

<p align="center">
  <b>Agent browser seukuran saku.</b><br>
  Kecil tapi cerdik — seperti kancil di cerita rakyat.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-3.14.0-brightgreen" alt="version">
  <img src="https://img.shields.io/badge/tests-240%20unit-brightgreen" alt="tests">
  <img src="https://img.shields.io/badge/size-%7E100%20KB-blue" alt="size">
  <img src="https://img.shields.io/badge/dependencies-0%20required-blue" alt="zero required deps">
  <img src="https://img.shields.io/badge/python-3.8%2B-blue" alt="python">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="license">
  <a href="README.md"><img src="https://img.shields.io/badge/README-English-blue" alt="english"></a>
</p>

> Browser CLI yang bisa dipakai agent sebagai **browser automation + DevTools +
> scraper + inspection tool** secara native, terstruktur, dan agent-friendly.
> **~100 KB, nol dependency wajib.** Jalan di Termux Android (ARM64), Linux, macOS.

✨ **Baru:** impersonasi fingerprint TLS (`kancil stealth impersonate`,
6 profil browser) · injeksi JS anti-detect buat WebView · `kancil session clear`
dengan wipe cookie per-domain · APK agent 1.17 (`/cookies/clear`, `/cache/clear`)
· `click(..., verify=...)` pola validator · status stealth di `kancil doctor`.

```bash
pip install -e .
kancil open https://example.com --quiet
kancil scrape https://news.ycombinator.com --auto --max-items 10
```

## Install

**Opsi 1 — pip (disarankan):**
```bash
cd ~/workspace/kancil        # atau di mana pun kamu unzip
pip install -e .             # di Termux: pip install -e . --break-system-packages
kancil --version              # kancil 3.11.0
kancil open https://example.com --quiet
```

**Opsi 2 — tanpa install (jalan langsung):**
```bash
cd ~/workspace/kancil         # project root (sejajar dengan folder kancil/)
python3 -m kancil open https://example.com --quiet
```
> Jangan jalankan dari *dalam* folder `kancil/` (tempat `api.py` berada) —
> `python3 -m kancil` butuh project root di `sys.path`. Setelah `pip install`,
> perintah `kancil` bisa dipakai dari direktori mana pun.

**Nol dependency** untuk engine default (pure Python stdlib).
Jalan di Termux Android (ARM64), Linux, macOS.

> Nama *Kancil* dari cerita rakyat: kecil tapi cerdik — browser ~100 KB
> yang ngalahin browser ratusan MB untuk kerjaan agent.

```
kancil/                        # project root
  pyproject.toml               # pip install -e .  -> perintah `kancil`
  README.md  CHANGELOG.md
  kancil/                      # package (from kancil import Kancil)
    __init__.py  __main__.py   # python -m kancil ...
    api.py                     # Kancil — semua method return dict JSON
    cli.py                     # CLI: kancil <command> [--json|--quiet|--raw]
    dom.py                     # DOM, CSS selector, XPath, inspector
    engines.py                 # StaticEngine (pure Python, selalu bisa)
    pw_engine.py               # PlaywrightEngine (Chromium asli, opsional)
    devtools.py                # scraper, reader/extract, network/perf helpers
    session.py                 # persistent state (~/.kancil/)
    repl.py                    # kancil shell  (REPL interaktif)
  tests/                       # 201 unit test + 5 live test (stdlib unittest)
```

## Dua engine, satu API

| Kemampuan            | `static` (default) | `playwright` | `webview` |
|----------------------|--------------------|--------------|-----------|
| Buka URL, tab, history | ✅ | ✅ | ✅ (multi-tab) |
| DOM, CSS, XPath      | ✅ | ✅ | ✅ |
| Klik link / submit form (+upload file) | ✅ | ✅ | ✅ (klik/ketik/fill) |
| Smart resolve `click "Login"` | ✅ | ✅ | ✅ |
| Cookies              | ✅ | ✅ | ✅ (punya app) |
| Network log + header, HAR export | ✅ | ✅ (+ interception) | ✅ (log saja) |
| Scraper, extract, perf | ✅ | ✅ | ✅ (scraper/extract) |
| Proxy                | ✅ | ✅ (saat start) | ❌ (ikut app) |
| Request blocking     | ❌ | ✅ | ✅ (blocklist pola) |
| PDF export           | ❌ | ✅ | ❌ |
| **JavaScript**       | ❌ jujur ditolak | ✅ | ✅ |
| **Screenshot**       | ❌ jujur ditolak | ✅ | ✅ (window + full-page) |
| **localStorage beneran / IndexedDB** | ❌ (simulasi KV) | ✅ | ✅ |
| **Console JS / bounding box** | ❌ | ✅ | ✅ (console) / ❌ |
| **Login/captcha manual** | ❌ | ⚠️ (sering ke-block) | ✅ (di app, sekali) |
| **Stealth (sembunyikan jejak WebView)** | ❌ | ⚠️ (butuh Camoufox) | ✅ (default ON) |

Engine `webview` nyambung ke aplikasi **Kancil Browser** (folder `android/`,
APK ~90 KB, agent 1.13) yang jalan di HP yang sama — Chromium beneran (System WebView).
Login/captcha cukup sekali di app, agent di Termux pakai session-nya terus:
`kancil open --engine webview https://example.com`. Ada juga **pembunuh popup iklan**:
popup otomatis (tanpa gesture) langsung diblokir, popup dari tap yang larinya ke host iklan
langsung ditutup tab-nya. **DevTools manual** (menu → DevTools): console JS, eksekutor
JavaScript, dan network log per tab — tanpa perlu agent. Tombol fullscreen video
(YouTube dsb) berfungsi; mode malam tidak lagi bikin thumbnail jadi negatif.

> **Webview = session-stateful: drive dari SATU proses.** Tiap `kancil ...`
> = proses baru yang baca ulang `/status`; kalau Android me-recreate app di
> background, ID tab bisa bergeser antar-command. Untuk flow multi-langkah
> pakai `kancil --engine webview shell` (satu sesi hidup) atau
> `kancil daemon start` — jangan CLI per-command buat flow. Pola baru via
> `block add` otomatis scrub HTTP cache + paksa `LOAD_NO_CACHE` di semua tab
> selama ada pola aktif (block = block, nggak ada yang lolos lewat cache).
>
> Perintah lain: `videos` (list video + src), `console`, `block add <pola>`,
> `download <url>`, `upload <path>` (file chooser), `crashes` (laporan crash
> terakhir app).
>
> Selancar kaya manusia: `open <url> --idle` (tunggu readyState + network
> diem, nggak perlu tebak sleep), `wait-idle`, `press Enter`
> (KeyboardEvent beneran ke element fokus), `longpress <selector>`
> (long-press ala mobile). `click` sekarang lapor URL + title tempat mendarat.
>
> Query element tembus shadow DOM + iframe same-origin (rekursif) — berlaku
> buat click/type/press/hover/wait/videos/screenshot element. Iframe
> cross-origin nggak bisa ditembus (same-origin policy, bukan bug).
>
> Aman: `--dry-run` — isi form/composer sepuasnya, click/submit ke-block
> kecuali `--confirm` (anti publish tak sengaja).
>
> Navigasi warm SPA dalam satu proses: `click-through <url> <klik-selector>
> <tunggu-selector>` — buka (settled) → klik → tunggu. Composer m.facebook
> cuma render lewat klik dari feed, BUKAN lewat URL langsung (SPA butuh warm
> state dari feed) — `composer-open` udah bake-in flow benernya. Berlaku juga
> buat banyak SPA mobile lain.
>
> Scroll kaya manusia: `scroll 600` nunggu render 800ms + lapor delta
> (delta 0 = nggak ada konten baru). `open --idle` deteksi "shell kosong"
> (title render tapi konten nol) → kasih warning login gate.
>
> Polish: `press Enter` otomatis submit form beneran kalau synthetic key-nya
> nggak ngapa-ngapain (cek: nggak ada navigasi + request baru); `har export`
> narik netlog dari APK dulu biar nggak kosong; `open()` re-sync tab ID kalau
> app sempat di-restart Android; `/downloads` lapor progress
> (dl_status/bytes_done/bytes_total).
>
> Kalau app crash saat di-drive agent, UncaughtExceptionHandler restart
> otomatis (max 3x per 5 menit, anti loop) dan stacktrace bisa dibaca via
> `kancil --engine webview crashes`.
>
> Tema **Emerald & Gold**: light mode toolbar putih gading + ikon emerald tua,
> dark mode noir + emas champagne, progress bar emas, kartu tab ber-ring emas,
> semua dialog rounded.
>
> `kancil doctor` — cek kesehatan sekali jalan: engine static/playwright/webview,
> server agent, sesi login, disk, environment, status stealth. Kalau ada yang
> rusak, dikasih tau cara benerinnya.

## Stealth (impersonasi browser)

Dua lapis opt-in, dua-duanya ringan (`curl_cffi` opsional):

```bash
kancil stealth status                        # profil + transport aktif
kancil stealth impersonate chrome_android    # static engine: spoofing
                                             # fingerprint TLS (chrome, firefox,
                                             # safari, edge, tor, chrome_android)
kancil stealth impersonate off               # balik ke urllib
kancil --impersonate tor open https://...    # sekali jalan via flag CLI
kancil stealth apply                         # engine webview: injek JS
                                             # anti-detect (webdriver, canvas,
                                             # WebGL, ...)
```

```python
from kancil.api import Kancil
b = Kancil(engine="static", impersonate="chrome_android")
b.engine.set_impersonate("firefox")   # atau None untuk matikan

from kancil import stealth
stealth.apply_stealth(webview_engine)  # injek ulang tiap navigasi
```

Spoofing injeksi-JS mencakup vektor fingerprinting umum tapi nggak sedalam
patch engine-level ala Camoufox — checker canggih masih bisa detek. Kancil
sengaja tetap kecil.

## Session clear

```bash
kancil session clear                                         # wipe semua
kancil session clear --what cookies                          # cuma cookies
kancil session clear --what cookies --domain example.com     # satu situs aja
```

Static engine: cookies (termasuk jar sesi impersonasi), tab, netlog, cache.
Webview: tutup tab, clear netlog + cache WebView; `all` nggak pernah sentuh
cookies (isinya login lo) — pakai `--what cookies` eksplisit kalau mau wipe
(butuh APK agent 1.17+).

## Troubleshooting (engine webview)

**App di-freeze / agent timeout (MIUI, layar mati).**
Aktifkan "Jaga agent tetap hidup" di Pengaturan (default ON) — foreground
service + notifikasi bikin task killer nggak berani kill. Kalau masih
timeout, bangunkan manual:
`am start -n com.kancil.browser/.MainActivity`.

**Tab ID basi / snapshot baca tab salah.**
`switch_tab` selalu verifikasi via `/status` dan gagal jujur kalau ID basi —
baca ulang `kancil tabs` dulu, atau pakai `shell`/`daemon` (satu proses).

**VPN per-app: IP WebView beda dengan Termux.**
VPN per-app cuma nunnel app yang dipilih — masukkan Kancil Browser ke
allowlist VPN. Alternatif: `kancil serve-proxy --mitm` di Termux (yang sudah
ke-VPN), lalu arahkan traffic HP lewat proxy itu.

**Network log: request + header saja.**
Webview mencatat method/URL/header per request (`kancil network`), tanpa
response status/body — itu butuh CDP/MITM penuh. HAR export tersedia dari
log yang ada.

**`type()` di input framework modern (React/Vue).**
Sudah pakai native value setter + event `input`/`change`. Kalau masih nggak
nempel, pakai `kancil form fill` atau cek `kancil console` untuk error JS.