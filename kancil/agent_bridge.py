"""Agent bridge: drive a REAL browser (the user's Chrome) from Kancil.

The viewer injects agent.js into served pages. That script runs in the real
rendered DOM (JS executed, video playable) and:
  - registers the tab,
  - long-polls for commands (click, type, scroll, eval, snapshot, text),
  - posts results back.

Hermes (or any agent) drives it via: agent_tabs / agent_cmd / agent_snapshot
(CLI: `kancil agent ...`, tool actions of the same names).
"""

import itertools
import json
import threading
import time

# command actions the injected agent.js understands
ACTIONS = ("click", "type", "scroll", "eval", "snapshot", "text")


class AgentBridge:
    def __init__(self, stale_after=120):
        self._lock = threading.Lock()
        self._tabs = {}          # tab_id -> dict
        self._ids = itertools.count(1)
        self.stale_after = stale_after

    # ---------- tab registry ----------
    def register(self, tab_id, info):
        with self._lock:
            t = self._tabs.get(tab_id)
            if t is None:
                t = {"tab_id": tab_id, "seq": 0, "queue": [],
                     "results": {}, "waiters": {},
                     "first_seen": time.time()}
                self._tabs[tab_id] = t
            t.update({"url": info.get("url", ""), "title": info.get("title", ""),
                      "ua": info.get("ua", "")[:120],
                      "last_seen": time.time()})
            return {"tab_id": tab_id}

    def unregister(self, tab_id):
        with self._lock:
            self._tabs.pop(tab_id, None)

    def tabs(self):
        now = time.time()
        with self._lock:
            out = []
            for tid, t in self._tabs.items():
                out.append({"tab_id": tid, "url": t.get("url", ""),
                            "title": t.get("title", ""),
                            "live": now - t.get("last_seen", 0) < self.stale_after,
                            "pending": len(t["queue"])})
            return out

    def _get(self, tab_id):
        t = self._tabs.get(tab_id)
        if t is None:
            raise KeyError("no such agent tab: %s" % tab_id)
        return t

    # ---------- command flow ----------
    def send(self, tab_id, action, args=None, timeout=30):
        """Queue a command, wait for the injected agent to execute it."""
        if action not in ACTIONS:
            return {"success": False, "errors": ["unknown action %r" % action]}
        with self._lock:
            try:
                t = self._get(tab_id)
            except KeyError as e:
                return {"success": False, "errors": [str(e)]}
            cid = next(self._ids)
            t["seq"] += 1
            cmd = {"id": cid, "seq": t["seq"], "action": action,
                   "args": args or {}}
            t["queue"].append(cmd)
            ev = threading.Event()
            t["waiters"][cid] = ev
        ok = ev.wait(timeout)
        with self._lock:
            t["waiters"].pop(cid, None)
            res = t["results"].pop(cid, None)
        if not ok or res is None:
            return {"success": False,
                    "errors": ["agent timeout after %ds (tab closed?)" % timeout]}
        return {"success": True, "tab_id": tab_id, "action": action,
                "result": res}

    def poll(self, tab_id, seq, timeout=25):
        """Long-poll: return commands with seq greater than the given one."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                t = self._tabs.get(tab_id)
                if t is None:
                    return []
                cmds = [c for c in t["queue"] if c["seq"] > seq]
                if cmds:
                    # drop delivered commands from the queue
                    t["queue"] = [c for c in t["queue"] if c["seq"] <= seq]
                    return cmds
            time.sleep(0.25)
        return []

    def submit_result(self, tab_id, cmd_id, out):
        with self._lock:
            t = self._tabs.get(tab_id)
            if t is None:
                return False
            t["results"][cmd_id] = out
            t["last_seen"] = time.time()
            ev = t["waiters"].pop(cmd_id, None)
            if ev is not None:
                ev.set()
            return True

    def heartbeat(self, tab_id):
        with self._lock:
            t = self._tabs.get(tab_id)
            if t is not None:
                t["last_seen"] = time.time()


# one bridge per process; the viewer wires it to the Kancil instance
_bridge = None
_bridge_lock = threading.Lock()


def get_bridge():
    global _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = AgentBridge()
        return _bridge


# ---------------- shared HTTP routing for agent endpoints ----------------
# Both the viewer and the proxy server expose these; the bridge is
# process-global so `kancil agent` sees tabs from either one.

def _agent_js_bytes():
    import os
    js_path = os.path.join(os.path.dirname(__file__), "agent.js")
    with open(js_path, "rb") as f:
        return f.read()


def route_agent(path, query, body):
    """Handle /__kancil__/agent* paths.

    path: e.g. "/__kancil__/agent/poll"; query: dict; body: parsed JSON (or {}).
    Returns (status_code, content_type, body_bytes) or None if not an agent path.
    """
    GW = "/__kancil__"
    if not path.startswith(GW + "/"):
        return None
    action = path[len(GW) + 1:]
    br = get_bridge()

    def _json(obj, code=200):
        return (code, "application/json",
                json.dumps(obj).encode("utf-8"))

    if action == "agent.js":
        try:
            return (200, "application/javascript", _agent_js_bytes())
        except OSError:
            return (404, "text/plain", b"agent.js missing")
    if action == "agent/register":
        tab = body.get("tab") or ("tab-%d" % int(time.time() * 1000))
        br.register(tab, body)
        return _json({"success": True, "tab": tab})
    if action == "agent/poll":
        try:
            seq = int(query.get("seq", "0"))
        except Exception:
            seq = 0
        return _json(br.poll(query.get("tab", ""), seq))
    if action == "agent/result":
        ok_ = br.submit_result(body.get("tab", ""), body.get("id"),
                               body.get("out"))
        return _json({"success": bool(ok_)})
    return None
