"""Kancil daemon mode: one persistent process, zero per-command startup cost.

The daemon holds a single Kancil engine (warm DNS cache, keep-alive
connections, cookies, tabs, HTTP cache). CLI commands are forwarded to it
over a unix socket as newline-delimited JSON:

    {"op": "_dispatch", "args": {...}}   -> run cli.dispatch on the daemon
    {"op": "_ping"}                      -> {"ok": true, "pong": true}
    {"op": "_stop"}                      -> save state, shut down

Stdlib only. No new dependencies.
"""

import json
import os
import socket
import sys
import threading
import time

from . import session as session_mod

def sock_path():
    return os.path.join(session_mod.BASE, "daemon.sock")


def pid_path():
    return os.path.join(session_mod.BASE, "daemon.pid")


def log_path():
    return os.path.join(session_mod.BASE, "daemon.log")
CALL_TIMEOUT = 600


def _send_msg(sock, obj):
    data = (json.dumps(obj, default=str) + "\n").encode("utf-8")
    sock.sendall(data)


def _recv_msg(f):
    line = f.readline()
    if not line:
        raise ConnectionError("daemon closed connection")
    if isinstance(line, bytes):
        line = line.decode("utf-8")
    return json.loads(line)


def call(msg, timeout=CALL_TIMEOUT):
    """Send one message to the daemon, return its reply dict."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(sock_path())
        f = sock.makefile("r")
        _send_msg(sock, msg)
        return _recv_msg(f)
    finally:
        try:
            sock.close()
        except Exception:
            pass


def alive():
    try:
        r = call({"op": "_ping"}, timeout=5)
        return bool(r.get("pong"))
    except Exception:
        return False


def _write_pid():
    with open(pid_path(), "w") as f:
        f.write(str(os.getpid()))


def _read_pid():
    try:
        with open(pid_path()) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _pid_running(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def start(engine="static", proxy=None, ua=None, timeout=25, retries=2):
    if alive():
        return {"success": True, "status": "already running",
                "pid": _read_pid()}
    try:
        os.remove(sock_path())
    except OSError:
        pass
    os.makedirs(session_mod.BASE, exist_ok=True)
    log = open(log_path(), "a")
    # the kancil package lives next to this file; the daemon subprocess
    # must start there (kancil is not pip-installed).
    repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    proc = None
    try:
        import subprocess
        proc = subprocess.Popen(
            [sys.executable, "-m", "kancil.daemon", "_serve",
             "--engine", engine,
             "--timeout", str(timeout), "--retries", str(retries)] +
            (["--proxy", proxy] if proxy else []) +
            (["--ua", ua] if ua else []),
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, cwd=repo_dir)
    finally:
        # parent keeps no handle; the daemon is fully detached
        pass
    for _ in range(50):  # wait up to ~5s for the socket
        time.sleep(0.1)
        if alive():
            return {"success": True, "status": "started",
                    "pid": _read_pid(), "sock": sock_path()}
        if proc is not None and proc.poll() is not None:
            break
    tail = ""
    try:
        with open(log_path()) as f:
            tail = "".join(f.readlines()[-5:])
    except Exception:
        pass
    return {"success": False, "errors": ["daemon failed to start", tail[-300:]]}


def stop():
    if not alive():
        _cleanup_files()
        return {"success": True, "status": "not running"}
    try:
        call({"op": "_stop"}, timeout=15)
    except Exception:
        pass
    pid = _read_pid()
    for _ in range(30):
        time.sleep(0.1)
        if not _pid_running(pid) and not alive():
            break
    else:
        try:
            if pid:
                os.kill(pid, 9)
        except Exception:
            pass
    _cleanup_files()
    return {"success": True, "status": "stopped"}


def _cleanup_files():
    for p in (sock_path(), pid_path()):
        try:
            os.remove(p)
        except OSError:
            pass


# ---------------- server ----------------

class _Server:
    def __init__(self, engine, proxy, ua, timeout, retries):
        from argparse import Namespace
        from .api import Kancil
        st = session_mod.load_state()
        cookie_file = os.path.join(session_mod.BASE, "cookies.lwp")
        self._b = Kancil(engine=engine or st.get("engine", "static"),
                         timeout=timeout, retries=retries,
                         cookie_file=cookie_file,
                         profile=st.get("profile", {}),
                         proxy=proxy or st.get("proxy"),
                         ua=ua or st.get("ua"))
        self._b.import_state(st)
        self._st = st
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._sock = None

    def _save(self):
        try:
            new_st = self._b.export_state()
            new_st["engine"] = self._b._engine_name
            new_st["session"] = self._st.get("session", "default")
            new_st["timeout"] = self._st.get("timeout", 25)
            new_st["retries"] = self._st.get("retries", 2)
            session_mod.save_state(new_st)
        except Exception:
            pass

    def _handle(self, conn):
        try:
            f = conn.makefile("r")
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    _send_msg(conn, {"ok": False,
                                     "error": "invalid json"})
                    continue
                op = msg.get("op")
                if op == "_ping":
                    _send_msg(conn, {"ok": True, "pong": True})
                    continue
                if op == "_stop":
                    _send_msg(conn, {"ok": True, "stopping": True})
                    self._shutdown.set()
                    return
                if op == "_dispatch":
                    from argparse import Namespace
                    from . import cli as cli_mod
                    args = Namespace(**msg.get("args", {}))
                    cwd = msg.get("cwd")
                    prev_cwd = os.getcwd()
                    with self._lock:
                        try:
                            if cwd:
                                os.chdir(cwd)
                            try:
                                result = cli_mod.dispatch(self._b, args)
                            except Exception as e:
                                result = {"success": False, "errors": [
                                    "%s: %s" % (type(e).__name__, str(e)[:200])]}
                        finally:
                            if cwd:
                                try:
                                    os.chdir(prev_cwd)
                                except Exception:
                                    pass
                    _send_msg(conn, {"ok": True, "result": result})
                    continue
                _send_msg(conn, {"ok": False, "error": "unknown op"})
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def serve(self):
        _write_pid()
        try:
            os.remove(sock_path())
        except OSError:
            pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(sock_path())
        self._sock.listen(16)
        self._sock.settimeout(0.5)
        try:
            while not self._shutdown.is_set():
                try:
                    conn, _ = self._sock.accept()
                except socket.timeout:
                    continue
                t = threading.Thread(target=self._handle, args=(conn,),
                                     daemon=True)
                t.start()
        finally:
            self._save()
            try:
                self._b.close()
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            _cleanup_files()


def _serve_main(argv):
    import argparse
    if argv[:1] == ["_serve"]:
        argv = argv[1:]
    p = argparse.ArgumentParser()
    p.add_argument("--engine", default="static")
    p.add_argument("--proxy", default=None)
    p.add_argument("--ua", default=None)
    p.add_argument("--timeout", type=int, default=25)
    p.add_argument("--retries", type=int, default=2)
    a = p.parse_args(argv)
    os.makedirs(session_mod.BASE, exist_ok=True)
    srv = _Server(a.engine, a.proxy, a.ua, a.timeout, a.retries)
    srv.serve()


if __name__ == "__main__":
    _serve_main(sys.argv[1:])
