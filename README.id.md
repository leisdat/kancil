# Kancil — Agent Browser + DevTools untuk Termux

Browser CLI yang bisa dipakai agent sebagai **browser automation + DevTools +
scraper + inspection tool** secara native, terstruktur, dan agent-friendly.

## Install

**Opsi 1 — pip (disarankan):**
```bash
cd ~/workspace/kancil        # atau di mana pun kamu unzip
pip install -e .             # di Termux: pip install -e . --break-system-packages
kancil --version              # kancil 3.1.0
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

> Nama *Kancil* dari cerita rakyat: kecil tapi cerdik — browser 384 KB
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
  tests/                       # 88 unit+integration test (stdlib unittest)
```

## Dua engine, satu API

| Kemampuan            | `static` (default) | `playwright` |
|----------------------|--------------------|--------------|
| Buka URL, tab, history | ✅ | ✅ |
| DOM, CSS, XPath      | ✅ | ✅ |
| Klik link / submit form (+upload file) | ✅ | ✅ |
| Smart resolve `click "Login"` | ✅ | ✅ |
| Cookies              | ✅ | ✅ |
| Network log + header, HAR export | ✅ | ✅ (+ interception) |
| Scraper, extract, perf | ✅ | ✅ |
| Proxy                | ✅ | ✅ (saat start) |
| Request blocking     | ❌ | ✅ |
| PDF export           | ❌ | ✅ |
| **JavaScript**       | ❌ jujur ditolak | ✅ |
| **Screenshot**       | ❌ jujur ditolak | ✅ |
| **localStorage beneran / IndexedDB** | ❌ (simulasi KV) | ✅ |
| **Console JS / bounding box** | ❌ | ✅ |

Cek kapan saja: `kancil devtools --json` → `capabilities`.

## Pakai sebagai CLI

State (tab, cookies, storage, netlog, bookmark, proxy, UA) persist di
`~/.kancil/state.json` antar pemanggilan. Tab dipulihkan **lazy** — cuma
tab aktif yang di-fetch, sisanya pas di-switch.

```bash
cd ~/workspace/kancil
python3 -m kancil open https://example.com --quiet
python3 -m kancil view --port 8901      # buka http://127.0.0.1:8901 di browser HP
python3 -m kancil dom inspect "#login" --json
python3 -m kancil click "Login"
python3 -m kancil network filter api --json
python3 -m kancil scrape --selector ".card" \
    --fields "title:.title,url:a@href,image:img@src" --json
```

Perintah:

```
open <url> | back | fwd | reload | tabs | new-tab [url] | switch <id> | close-tab [id]
dom [tree|inspect|find|xpath] <target>
click <selector> | type <sel> <text> | clear <sel> | select <sel> <value>
check|uncheck <sel> | hover <sel> | scroll [top|bottom|<selector>]
js <expr> | wait <selector> | wait --ms 2000 | wait --text "..."
console | errors
screenshot [--full] [--element <sel>] [--out f.png]
pdf [--out f.pdf]
network [list|clear|filter <re>|request <id>|response <id>]
        [--type xhr|fetch|document|image] [--status 2xx|404]
        [--method GET|POST] [--url substr] [--har out.har]
storage [list|get|set|delete|cookies]
scrape [url] --selector S --fields "n:css,n2:css@attr" [--auto] [--format json|csv]
       [--pages N --max-pages N --max-items N --crawl-timeout 180
        --same-content-limit 3 --scroll-pages N --next <sel> --delay 1.0
        --no-robots]
extract [--mode auto|article|links|images|tables]
a11y [tree|list|find] [query] [--role R] | perf | forms | form <fill|submit|inspect> <id> [--auto] [--set k=v]
download <url> | downloads | dlpause|dlresume <id>
crawl <url> [--depth 2 --max 30 --delay 1 --pattern re --external]
session <save|load|list|delete|info> [name]
tool '<json>' | snapshot [--full|--dom|--no-a11y|--no-links|--no-forms]
observe | warnings
har <start|stop|export|clear|stats> [path] [--no-redact]
bookmark <add|list|open|delete> [url|id] [--title T]
profile <set|get|list|delete> [key] [value]
proxy <set|clear|show> [url]
ua <show|set|rotate|list> [value]
block <add|list|clear> [pattern]
search <query> | shell | devtools | yt-search <q> | yt-video <url> | page-json
view [--port P]  (local viewer: lihat + ambil alih di browser kamu)
agent {tabs|cmd|snap}  (setir halaman yang ke-render beneran)
proxy-ca | serve-proxy [--mitm]  (level 2: traffic browser HP lewat kancil)
```

Help per area: `kancil dom -h`, `kancil network -h`, `kancil storage -h`,
`kancil scrape -h`, dst.

Flag global (boleh sebelum ATAU sesudah subcommand):
`--json` (output JSON, untuk agent), `--quiet` (satu baris ok/fail),
`--raw` (data saja), `--engine static|playwright`,
`--proxy http://host:port`, `--ua "..."`, `--timeout N`, `--retries N`.

### Proxy

```bash
kancil proxy set http://127.0.0.1:8080   # persisten di session
kancil open https://example.com           # lewat proxy
kancil proxy clear
```

Format `http://user:pass@host:port` didukung. Untuk engine Playwright,
proxy/UA dibaca saat start: `kancil --engine playwright --proxy ...`.

### Smart element resolution

`click "Login"` dicoba berurutan: CSS selector → `#id` → `[name=]` →
`[aria-label=]` → `[placeholder=]` → text persis di button/link →
text mengandung. Method yang kepakai dilaporkan di hasil.

### File upload

```bash
kancil form fill 1 --set avatar=@/sdcard/pic.jpg --set name=budi
kancil form submit 1
```

Engine static mengirim `multipart/form-data` beneran; Playwright pakai
`set_input_files`.

## Pakai sebagai Agent API (Python)

```python
from kancil import Kancil

b = Kancil(engine="static")              # atau "playwright"
r = b.open("https://example.com")       # {"success": True, "url": ..., "title": ...}
r = b.dom_find(".card .title")
r = b.inspect("#login")                 # tag/attrs/text/html/css/xpath/parent/children
r = b.click("Login")                    # smart resolve
r = b.scrape(selector=".card",
             fields={"title": ".title", "url": "a@href"})
r = b.network(pattern="api")
r = b.har("out.har")
r = b.bookmark_add()
r = b.pdf()                             # playwright saja
print(b.to_json(r))
```

Semua method mengembalikan `{"success": bool, ...data..., "errors": [...]}`.

## Engine Playwright (JS + screenshot beneran)

Di Termux, lewat proot-distro Ubuntu (ARM64):

```bash
proot-distro login ubuntu
pip install playwright
playwright install chromium --only-shell   # hemat tempat (~100-150 MB)
playwright install-deps chromium
```

```bash
python3 -m kancil --engine playwright open https://example.com
python3 -m kancil --engine playwright js "document.title"
python3 -m kancil --engine playwright screenshot --full
python3 -m kancil --engine playwright block add "*doubleclick*"
```

**Fallback yang jujur:** kalau Playwright/Chromium belum kepasang, engine
playwright gagal start dengan pesan jelas. Command JS/screenshot/PDF/block
di engine static mengembalikan `{"success": false, "supported": false}`.

## Viewer: lihat + ambil alih di browser kamu (`kancil view`)

```bash
python3 -m kancil open https://example.com --quiet
python3 -m kancil view --port 8901
# buka http://127.0.0.1:8901 di browser HP — Ctrl+C untuk berhenti
```

Mini YouTube player (nonton beneran, ada suara):

```bash
python3 -m kancil yt-play "termux tutorial" --port 8901
# buka URL player yang dicetak di browser HP
```

- **Engine static** (jalan di Termux, nol dependency): halaman aktif diserve
  lewat gateway lokal. Browser HP me-render beneran; klik link, address bar,
  dan form GET dinavigasikan *lewat Kancil* — cookies/session ikut.
- **Engine playwright**: screenshot live yang auto-refresh; **klik di gambar
  = klik beneran di Chromium**, plus kotak ketik untuk kirim teks.

Catatan: viewer hidup selama proses `kancil view` jalan, bind ke
`127.0.0.1` saja. Jangan expose ke jaringan tanpa auth.

## Agent: setir halaman yang ke-render beneran (Level 1)

Viewer menyuntikkan `agent.js` ke halaman — Chrome HP me-render penuh
(JS jalan), dan kancil bisa menyetirnya:

```bash
python3 -m kancil view --port 8901          # buka di Chrome HP
python3 -m kancil agent tabs                # lihat tab live
python3 -m kancil agent snap <tab-id>       # snapshot DOM asli (post-JS)
python3 -m kancil agent cmd <tab-id> click '{"selector":"a.login"}'
python3 -m kancil agent cmd <tab-id> type '{"selector":"input[name=q]","text":"termux"}'
```

## Proxy lokal: traffic browser HP lewat kancil (Level 2)

```bash
python3 -m kancil proxy-ca                 # bikin CA sekali
# install ca.crt di HP: Settings > Security > Install CA certificate
python3 -m kancil serve-proxy --mitm       # jalan di foreground
# set proxy WiFi HP -> 127.0.0.1:8080
```

Semua request Chrome HP (termasuk HTTPS, setelah CA terinstall) lewat
kancil: agent.js disuntik otomatis, traffic tercatat. Tanpa `--mitm`,
CONNECT di-tunnel buta (host tercatat, isi tidak). Ini yang bikin situs
login-wall kayak Facebook bisa dikerjakan agent — yang request Chrome asli
pakai akun asli kamu, kancil tinggal menyetir + mencatat.

## Agent Tool Interface (untuk Hermes)

Satu pintu JSON-in/JSON-out, tanpa parsing teks terminal:

```bash
kancil tool '{"action":"open","url":"https://example.com"}'
echo '{"action":"click","selector":"@button_1"}' | kancil tool
kancil tool '{"action":"scrape","selector":".card","fields":{"title":".title"}}'
```

Response sukses: `{"success": true, "action": "click", ...}`.
Error terstruktur: `{"success": false, "error": {"code": "ELEMENT_NOT_FOUND",
"message": "...", "action": "click", "selector": "#x"}}`.

Error codes: `INVALID_URL TAB_NOT_FOUND ELEMENT_NOT_FOUND TIMEOUT
NAVIGATION_FAILED JS_ERROR NETWORK_ERROR SESSION_NOT_FOUND SCRAPE_FAILED
DOWNLOAD_FAILED ENGINE_UNAVAILABLE INVALID_INPUT UNKNOWN_ACTION`.

59 actions: open/back/forward/reload/history, tabs/new_tab/switch_tab/close_tab,
inspect/elements/a11y/a11y_find/dom_tree/dom_find/dom_xpath,
click/type/clear/select/check/uncheck/hover/focus/scroll, evaluate/wait,
network/network_request/network_response/har_export, cookies/storage(+get/set/delete),
screenshot/pdf, scrape/extract/download/downloads,
session_save/session_load/session_delete/session_info/session_list,
snapshot/observe/console/warnings/errors/perf/forms/form_fill/form_submit,
bookmark_add/bookmark_list, capabilities.

Dari Python: `Kancil().tool({"action": "click", "selector": "#login"})`.

## Snapshot

Satu response gambaran halaman (hemat token, tanpa HTML mentah):

```bash
kancil snapshot --json
# {url, title, tab_id, page:{headings,links,buttons,inputs}, a11y, errors,
#  network_summary:{requests, failed}}
kancil snapshot --full --dom   # tambah DOM ringkas
```

## Accessibility refs

```bash
kancil a11y                    # tree: document ├── button "Login" @button_1 ...
kancil a11y find "Login" --role button
kancil click @button_1         # atau: kancil tool '{"action":"click","selector":"@button_1"}'
kancil type @textbox_1 "user@x.id"
```

Ref (`button_1`, `textbox_2`, ...) stabil selama halaman tidak berubah dan
**persist antar pemanggilan CLI** via `~/.kancil/state.json`. Kalau DOM
berubah, ref otomatis invalid dengan pesan jelas ("re-run a11y").

## HAR recording

```bash
kancil har start
kancil open https://example.com
kancil har stats
kancil har export ./capture.har   # HAR 1.2 valid, redaction ON by default
kancil har clear
```

Redaction default: nilai `Cookie`/`Set-Cookie`/`Authorization`/`*token*`
diganti `***`. Matikan dengan `--no-redact` (tidak disarankan).

## Paginasi scraper

```bash
kancil scrape URL --selector ".card" --fields "title:.title" --pages 10
kancil scrape URL --next ".pagination .next" --max-items 500
kancil scrape URL --scroll-pages 5 --engine playwright   # infinite scroll
```

Otomatis: deteksi `?page=N`, ikut link next, dedup via canonical URL +
hash konten, hormati robots.txt, batas `--max-pages/--max-items/
--crawl-timeout/--same-content-limit`. Output:
`{pages_crawled, items, duplicates, failed_pages, stopped_reason, data}`.

## Persistent session (Playwright)

```bash
kancil --engine playwright --session muse open https://...
kancil --engine playwright session save muse    # storage_state (cookies+localStorage)
# ...tutup Termux, buka lagi...
kancil --engine playwright --session muse tabs  # pulih
kancil session info muse    # metadata aman saja, tanpa nilai secret
kancil session delete muse
```

File `~/.kancil/sessions/<name>/storage_state.json` permission 0600.
Untuk static engine, `session save/load` tetap pakai replay state seperti dulu.

## Testing

```bash
cd ~/workspace/kancil
python3 -m unittest tests.test_browser -v
```

88 test: navigation, back/forward, tabs, lazy restore, DOM, CSS, XPath,
inspector, smart resolve, klik/type/submit form, upload file, form auto-fill,
cookies, storage, network + HAR, HTTP errors, scraper, extract, perf,
download, bookmark, profile, proxy, UA, bentuk JSON, capabilities,
error handling.

## Batasan yang diketahui

- Static engine: tidak eksekusi JavaScript — situs SPA/React yang render
  konten via JS butuh `--engine playwright`.
- Screenshot/PDF/request-blocking butuh Playwright; tidak ada jalan pintas
  pure-Python, dan Kancil tidak akan ngaku bisa.
- Search memakai Bing RSS (DuckDuckGo men-serve CAPTCHA ke IP datacenter).
