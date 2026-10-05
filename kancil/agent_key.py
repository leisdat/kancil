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
import secrets
import stat

KEY_NAME = "agent.key"


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
