"""Interactive REPL for Kancil (keeps one engine alive)."""
import json
import shlex

from . import session as session_mod
from .api import Kancil


def run(engine=None):
    st = session_mod.load_state()
    b = Kancil(engine=engine or st.get("engine", "static"),
                      timeout=st.get("timeout", 25),
                      cookie_file=session_mod.BASE + "/cookies.lwp",
                      profile=st.get("profile", {}))
    b.import_state(st)
    print("Kancil shell — engine=%s. 'help' for commands, 'q' to quit."
          % b._engine_name)
    from .cli import dispatch, build_parser
    parser = build_parser()
    while True:
        try:
            raw = input("browser> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            break
        if not raw:
            continue
        if raw in ("q", "quit", "exit"):
            break
        if raw == "help":
            parser.print_help()
            continue
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = raw.split()
        ns = argparse_ns(parser, parts)
        if ns is None:
            continue
        try:
            r = dispatch(b, ns)
        except Exception as e:
            r = {"success": False, "errors": ["%s: %s" % (type(e).__name__, str(e)[:200])]}
        _show(r)
        # persist per command under the state lock so parallel CLI
        # invocations don't last-write-wins the REPL's state
        try:
            with session_mod.StateLock():
                new_st = b.export_state()
                new_st["engine"] = b._engine_name
                session_mod.save_state(new_st)
        except Exception:
            pass
    # persist
    try:
        new_st = b.export_state()
        new_st["engine"] = b._engine_name
        session_mod.save_state(new_st)
    finally:
        b.close()
    print("bye")


def argparse_ns(parser, parts):
    import argparse
    try:
        ns = parser.parse_args(parts)
    except SystemExit:
        return None
    ns.json = False
    ns.quiet = False
    ns.raw = False
    return ns


def _show(r):
    if not r.get("success"):
        print("FAIL: %s" % "; ".join(r.get("errors", ["?"])))
        return
    # compact human rendering
    if "url" in r and "title" in r:
        print("OK %s\n  %s" % (r["url"][:90], r.get("title", "")[:80]))
        return
    print(json.dumps(r, indent=1, ensure_ascii=False, default=str)[:3000])
