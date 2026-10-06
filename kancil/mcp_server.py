"""MCP server (Model Context Protocol) untuk Kancil — stdio JSON-RPC.

Membungkus seluruh tool actions Kancil (122) sebagai MCP tools, agar
Claude/agent MCP-client lain bisa menyetir Kancil langsung:

    kancil mcp [--engine static]
    # atau di Claude Desktop: {"command": "kancil", "args": ["mcp"]}

Protokol: JSON-RPC 2.0, newline-delimited via stdin/stdout.
Method yang didukung: initialize, notifications/initialized, tools/list,
tools/call, ping. inputSchema per tool = JSON Schema draft 2020-12
dari kancil/jsonschema.py (di-derive dari manifest runtime).

Zero dependency baru. Log ke stderr saja — stdout murni protokol.
"""

import json
import sys
import traceback

PROTOCOL_VERSION = "2024-11-05"


def _log(msg):
    sys.stderr.write("[kancil-mcp] %s\n" % msg)
    sys.stderr.flush()


class MCPServer:
    def __init__(self, engine="static", **kw):
        from .api import Kancil
        self.kancil = Kancil(engine=engine, **kw)
        self._tools_cache = None

    # ---------- tools ----------
    def _tools(self):
        if self._tools_cache is None:
            from . import jsonschema as _js
            manifest = self.kancil._manifest_cache()
            tools = []
            for action in sorted(manifest):
                meta = manifest[action]
                desc = meta.get("description") or action
                tools.append({
                    "name": action,
                    "description": "%s %s" % (
                        desc, "(engines: %s)" % ",".join(
                            meta.get("engines", []))),
                    "inputSchema": _js.for_action(meta),
                })
            self._tools_cache = tools
        return self._tools_cache

    def _call_tool(self, name, arguments):
        payload = {"action": name}
        if isinstance(arguments, dict):
            payload.update(arguments)
        try:
            result = self.kancil.tool(payload)
        except Exception as e:
            result = {"success": False,
                      "error": {"code": "INTERNAL_ERROR",
                                "message": "%s: %s" % (type(e).__name__,
                                                       str(e)[:300])}}
        try:
            text = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            text = str(result)
        return {
            "content": [{"type": "text", "text": text}],
            "isError": not bool(result.get("success")),
        }

    # ---------- JSON-RPC ----------
    def _respond(self, rid, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result if result is not None else {}
        sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    @staticmethod
    def _err(code, message, data=None):
        e = {"code": code, "message": message}
        if data is not None:
            e["data"] = data
        return e

    def handle(self, msg):
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}
        is_notif = rid is None

        def reply(result=None, error=None):
            if not is_notif:
                self._respond(rid, result, error)

        try:
            if method == "initialize":
                from . import __version__
                reply({
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "kancil",
                                   "version": __version__},
                })
            elif method == "notifications/initialized":
                pass  # notifikasi: tanpa respons
            elif method == "ping":
                reply({})
            elif method == "tools/list":
                reply({"tools": self._tools()})
            elif method == "tools/call":
                name = params.get("name", "")
                if name not in self.kancil._TOOL_ACTIONS:
                    reply(error=self._err(
                        -32602, "unknown tool %r" % (name,)))
                else:
                    reply(self._call_tool(name, params.get("arguments")))
            else:
                reply(error=self._err(-32601,
                                      "method not found: %s" % (method,)))
        except Exception as e:
            _log("handler error: %s\n%s" % (e, traceback.format_exc()[:500]))
            reply(error=self._err(-32603, "internal error: %s" % str(e)[:200]))

    def serve_forever(self):
        _log("serving %d tools over stdio"
             % len(self.kancil._TOOL_ACTIONS))
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                self._respond(None, error=self._err(-32700, "parse error"))
                continue
            if isinstance(msg, list):
                for m in msg:
                    if isinstance(m, dict):
                        self.handle(m)
            elif isinstance(m := msg, dict):
                self.handle(m)
        _log("stdin closed, exiting")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="kancil mcp")
    ap.add_argument("--engine", default="static",
                    help="kancil engine (default: static)")
    ap.add_argument("--timeout", type=int, default=25)
    a = ap.parse_args(argv)
    MCPServer(engine=a.engine, timeout=a.timeout).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
