"""API key buat Kancil Browser agent server (127.0.0.1:8080).

File-based, Kancil-local — tanpa Android Keystore, tanpa dependency baru.
Key disimpan di ~/.kancil/agent.key (mode 0600). Direktori private Termux
sudah di-sandbox oleh Android (app lain tidak bisa baca tanpa root), jadi
ini cukup untuk threat model "app lain di localhost iseng nyetir browser".

Alur sinkronisasi:
- Python adalah source of truth: get_or_create() bikin key sekali.
- Saat auto-launch (`am start`), key dikirim sebagai intent extra
  `kancil_agent_key` -> app menyimpan di SharedPreferences.
- Kalau server jawab 401 (key tidak cocok, mis. app dibuka manual dengan
  key lama), jalankan `kancil agent-key sync` untuk force-stop + relaunch
  app dengan key yang benar.
"""

import os
import hmac
import secrets
import stat

KEY_NAME = "agent.key"
HEADER = "X-Kancil-Key"

# Host yang boleh: loopback saja. Ini pertahanan lawan DNS rebinding —
# request rebinding datang dengan Host: <domain-penyerang>.
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def base_dir():
    d = os.path.join(os.path.expanduser("~"), ".kancil")
    os.makedirs(d, exist_ok=True)
    return d


def key_path():
    return os.path.join(base_dir(), KEY_NAME)


def read():
    """Baca key yang ada, atau None kalau belum pernah dibuat."""
    p = key_path()
    try:
        with open(p) as f:
            k = f.read().strip()
        return k or None
    except (OSError, IOError):
        return None


def _write(key):
    p = key_path()
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (key + "\n").encode())
    finally:
        os.close(fd)
    # pastikan 0600 walau file sudah ada dengan mode lain
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return key


def get_or_create():
    """Key yang ada, atau generate baru (url-safe, 256 bit) dan simpan."""
    k = read()
    if k:
        return k
    return _write(secrets.token_urlsafe(32))


def regenerate():
    """Buang key lama, bikin baru. Wajib `agent-key sync` setelah ini."""
    return _write(secrets.token_urlsafe(32))


def key_mode_ok():
    """True kalau file key ada dan tidak bisa dibaca grup/other."""
    p = key_path()
    try:
        st = os.stat(p)
    except OSError:
        return False
    return not (st.st_mode & (stat.S_IRGRP | stat.S_IROTH))


def check_request(headers, query):
    """Cek auth untuk server HTTP lokal Kancil (viewer, proxy).

    headers: dict header request (key case-insensitive).
    query: dict query param (nilai pertama).
    Returns (True, "") atau (False, alasan).

    Dua lapis:
    - Host harus loopback (lawan DNS rebinding: request rebinding
      membawa Host: <domain penyerang>, bukan 127.0.0.1).
    - Token via header X-Kancil-Key atau ?key= (lawan CSRF: halaman
      asing tidak bisa menebak token, jadi <img src=...> ke
      127.0.0.1 tidak bisa memicu aksi).
    """
    hd = {str(k).lower(): v for k, v in dict(headers or {}).items()}
    host = hd.get("host", "").split(":")[0].strip().lower().strip("[]")
    if host not in LOOPBACK_HOSTS:
        return False, "host %r bukan loopback" % host
    # Origin/Referer asing juga ditolak (lapis tambahan lawan CSRF)
    for h in ("origin", "referer"):
        o = hd.get(h, "")
        if o:
            try:
                import urllib.parse as _up
                oh = (_up.urlsplit(o).hostname or "").lower()
            except Exception:
                oh = ""
            if oh and oh not in LOOPBACK_HOSTS:
                return False, "%s asing: %s" % (h, oh)
    key = get_or_create()
    given = hd.get(HEADER.lower(), "") or (query or {}).get("key", "")
    if not given or not hmac.compare_digest(str(given), key):
        return False, "token salah/hilang (header %s atau ?key=)" % HEADER
    return True, ""
