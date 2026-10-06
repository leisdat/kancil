# Security Policy — Kancil

## Supported versions

| Version | Supported |
| ------- | --------- |
| 3.15.x  | ✅        |
| < 3.15  | ❌        |

Gunakan rilis terbaru dari
[GitHub releases](https://github.com/leisdat/kancil/releases).

## Melaporkan kerentanan

**Jangan** buka issue publik untuk kerentanan keamanan.

Kirim laporan privat ke maintainer via:

- GitHub: [private vulnerability report](https://github.com/leisdat/kancil/security/advisories/new)
  (Security → Advisories → Report a vulnerability)

Sertakan: versi Kancil, langkah reproduksi minimal, dampak yang
diperkirakan. Kami targetkan respons awal ≤ 72 jam dan patch untuk
isu High/Critical di rilis berikutnya.

## Model ancaman

Kancil adalah **agent browser lokal** — server HTTP-nya
(`kancil view`, `serve-proxy`, agent server APK) hanya mengikat
`127.0.0.1` dan **tidak untuk di-expose ke internet**.

Pertahanan yang sudah ada:

- **Agent key auth** (`X-Kancil-Key` / `?key=`): wajib di semua rute
  aksi server lokal — satu key `~/.kancil/agent.key` untuk semua
  server. Mencegah app localhost lain menyetir browser.
- **Host loopback check** + penolakan `Origin`/`Referer` asing:
  mitigasi DNS rebinding & CSRF lokal (temuan audit 2026-10-06).
- **Konfirmasi aksi destruktif**: `session_clear`, `session_delete`,
  `storage_delete`, `bookmark_delete`, `close_tab` butuh
  `"confirm": true` (atau `Kancil(confirm_destructive=False)` untuk
  otomasi tepercaya).
- **Penanda konten tidak tepercaya**: output `snapshot()`/`text()`
  menandai konten web sebagai data pihak ketiga (anti prompt
  injection).
- **Session vault terenkripsi**: AES-256-CBC + HMAC-SHA256,
  PBKDF2-HMAC-SHA256 (200k iterasi) untuk cookie/sesi tersimpan.
- **CA key proxy** ditulis atomik `0600`; direktori `ca/` `0700`;
  socket daemon `0600`.

Di luar cakupan: serangan yang butuh akses root/ADB ke perangkat,
rekayasa sosial agar user menjalankan perintah berbahaya, dan
kerentanan di situs web yang dikunjungi (itu ranah bug bounty
masing-masing situs).

## Praktik rilis

- Tag versi `vX.Y.Z` → sdist + wheel dibangun dari tag yang sama
  (`python -m build`), hash diverifikasi sebelum upload PyPI.
- APK rilis ditandatangani debug key repo; verifikasi versi via
  `kancil agent-info` / dex.
