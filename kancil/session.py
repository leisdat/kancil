"""Persistent session state for the Kancil CLI.

State file: ~/.kancil/state.json
Each CLI invocation loads state, acts, saves state — so
`kancil open X` then `kancil click ...` works across processes.
"""
import json
import os
import shutil
import time

BASE = os.path.join(os.path.expanduser("~"), ".kancil")
STATE_FILE = os.path.join(BASE, "state.json")
SESSIONS_DIR = os.path.join(BASE, "sessions")
SCREEN_DIR = os.path.join(BASE, "screenshots")
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(SCREEN_DIR, exist_ok=True)


def default_state():
    return {
        "engine": "static",
        "session": "default",
        "tabs": [{"history": [], "pos": -1}],
        "cur": 0,
        "profile": {},
        "bookmarks": [],
        "bm_id": 0,
        "proxy": None,
        "ua": None,
        "block": [],
        "netlog": [],
        "timeout": 25,
        "retries": 2,
        "updated": time.time(),
    }


def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
        # minimal validation
        if not isinstance(st.get("tabs"), list) or not st["tabs"]:
            raise ValueError("bad tabs")
        return st
    except (OSError, ValueError):
        return default_state()


def save_state(st):
    st["updated"] = time.time()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


class StateLock:
    """Best-effort exclusive lock around load-modify-save.

    The CLI is multi-process by design (one process per command), so two
    parallel `kancil` invocations would otherwise last-write-wins each
    other's tabs/netlog. Unix-only (fcntl); silently no-ops elsewhere.
    """
    def __init__(self):
        self._f = None

    def __enter__(self):
        try:
            import fcntl
            os.makedirs(BASE, exist_ok=True)
            self._f = open(os.path.join(BASE, "state.lock"), "w")
            fcntl.flock(self._f.fileno(), fcntl.LOCK_EX)
        except Exception:
            self._f = None
        return self

    def __exit__(self, *exc):
        try:
            if self._f is not None:
                import fcntl
                fcntl.flock(self._f.fileno(), fcntl.LOCK_UN)
                self._f.close()
        except Exception:
            pass
        self._f = None
        return False


def save_named(name, st):
    d = os.path.join(SESSIONS_DIR, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "state.json"), "w") as f:
        json.dump(st, f)
    # copy cookies alongside
    cf = os.path.join(BASE, "cookies.lwp")
    if os.path.exists(cf):
        shutil.copy(cf, os.path.join(d, "cookies.lwp"))
    # metadata (safe only)
    mp = os.path.join(d, "meta.json")
    meta = {}
    if os.path.exists(mp):
        try:
            with open(mp) as f:
                meta = json.load(f)
        except Exception:
            pass
    tabs = st.get("tabs", [])
    cur = st.get("cur", 0)
    url = ""
    try:
        h = tabs[cur]["history"]
        pos = tabs[cur]["pos"]
        u = h[pos]
        url = u.get("url", u) if isinstance(u, dict) else getattr(u, "url", u)
    except Exception:
        pass
    meta.update({"name": name, "engine": st.get("engine", "static"),
                 "created": meta.get("created") or time.strftime("%Y-%m-%d %H:%M:%S"),
                 "last_used": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "url": url, "tabs_count": len(tabs),
                 "user_agent": st.get("ua")})
    with open(mp, "w") as f:
        json.dump(meta, f, indent=1)
    _secure(mp)


def load_named(name):
    p = os.path.join(SESSIONS_DIR, name, "state.json")
    with open(p) as f:
        st = json.load(f)
    cf = os.path.join(SESSIONS_DIR, name, "cookies.lwp")
    if os.path.exists(cf):
        shutil.copy(cf, os.path.join(BASE, "cookies.lwp"))
    return st


def list_named():
    return sorted(n for n in os.listdir(SESSIONS_DIR)
                  if os.path.isdir(os.path.join(SESSIONS_DIR, n)))


def _secure(path):
    """Restrict to owner-only (session data may contain secrets)."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def delete_named(name):
    import shutil
    d = os.path.join(SESSIONS_DIR, name)
    if not os.path.isdir(d):
        return False
    shutil.rmtree(d)
    return True


def pw_state_path(name):
    return os.path.join(SESSIONS_DIR, name, "storage_state.json")


def meta_path(name):
    return os.path.join(SESSIONS_DIR, name, "meta.json")


def save_pw_session(name, storage_state, meta):
    """Persist Playwright storage_state (cookies + localStorage). Owner-only."""
    d = os.path.join(SESSIONS_DIR, name)
    os.makedirs(d, exist_ok=True)
    sp = pw_state_path(name)
    with open(sp, "w") as f:
        json.dump(storage_state, f)
    _secure(sp)
    mp = meta_path(name)
    meta = dict(meta or {})
    meta.setdefault("name", name)
    meta.setdefault("engine", "playwright")
    meta["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(mp, "w") as f:
        json.dump(meta, f, indent=1)
    _secure(mp)
    return sp


def load_pw_session(name):
    """Returns (storage_state_dict_or_None, meta_dict)."""
    sp, mp = pw_state_path(name), meta_path(name)
    state = None
    if os.path.exists(sp):
        with open(sp) as f:
            state = json.load(f)
    meta = {}
    if os.path.exists(mp):
        with open(mp) as f:
            meta = json.load(f)
    # touch last_used
    if meta:
        meta["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(mp, "w") as f:
            json.dump(meta, f, indent=1)
    return state, meta


def session_info(name):
    """Safe metadata only — never cookie/token values."""
    d = os.path.join(SESSIONS_DIR, name)
    if not os.path.isdir(d):
        return None
    mp = meta_path(name)
    meta = {}
    if os.path.exists(mp):
        with open(mp) as f:
            meta = json.load(f)
    # enrich from storage_state without exposing values
    cookies, storage_keys = 0, 0
    sp = pw_state_path(name)
    if os.path.exists(sp):
        try:
            with open(sp) as f:
                ss = json.load(f)
            cookies = len(ss.get("cookies", []))
            for origin in ss.get("origins", []):
                storage_keys += len(origin.get("localStorage", []))
        except Exception:
            pass
    info = {"name": name, "engine": meta.get("engine", "static"),
            "created": meta.get("created", "-"),
            "last_used": meta.get("last_used", "-"),
            "url": meta.get("url", "-"),
            "tabs": meta.get("tabs_count", meta.get("tabs", "-")),
            "cookies": cookies, "storage_keys": storage_keys,
            "user_agent": (meta.get("user_agent") or "")[:60]}
    return info
