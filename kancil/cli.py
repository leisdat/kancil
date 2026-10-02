"""Kancil CLI.

Usage: python -m browser <command> [args] [--json] [--engine static|playwright|webview]

State persists in ~/.kancil/state.json across invocations.
"""
import argparse
import json
import os
import sys

from . import session as session_mod
from .api import Kancil


def get_browser(args):
    st = session_mod.load_state()
    engine = args.engine or st.get("engine", "static")
    cookie_file = session_mod.BASE + "/cookies.lwp"
    proxy = getattr(args, "proxy", None) or st.get("proxy")
    ua = getattr(args, "ua", None) or st.get("ua")
    pw_session = getattr(args, "session", None)
    pw_browser = (getattr(args, "pw_browser", None)
                  or os.environ.get("KANCIL_PW_BROWSER", "chromium"))
    pw_executable_path = (getattr(args, "executable_path", None)
                          or os.environ.get("KANCIL_BROWSER_PATH"))
    b = Kancil(engine=engine, timeout=st.get("timeout", 25),
               retries=st.get("retries", 2),
               cookie_file=cookie_file, profile=st.get("profile", {}),
               proxy=proxy, ua=ua, pw_session=pw_session,
               pw_browser=pw_browser, pw_executable_path=pw_executable_path,
               cache=not getattr(args, "no_cache", False))
    b.import_state(st)
    return b, st


def put_browser(b, st, args):
    try:
        new_st = b.export_state()
        new_st["engine"] = b._engine_name
        new_st["session"] = st.get("session", "default")
        new_st["timeout"] = st.get("timeout", 25)
        new_st["retries"] = st.get("retries", 2)
        session_mod.save_state(new_st)
        # --session NAME means a *persistent* session: save playwright's
        # storage_state (cookies, localStorage) so the next command —
        # even a fresh browser process — resumes where this one left off
        # (e.g. a solved JS challenge stays solved).
        if b._engine_name == "playwright" and getattr(b, "pw_session", None):
            try:
                b.session_save(b.pw_session)
            except Exception:
                pass
    finally:
        b.close()


def emit(result, args):
    if args.json:
        print(json.dumps(result, indent=1, ensure_ascii=False, default=str))
    elif args.raw:
        d = result.get("data", result)
        print(json.dumps(d, ensure_ascii=False, default=str) if isinstance(d, dict) else d)
    elif args.quiet:
        print("ok" if result.get("success") else "fail: %s" % "; ".join(result.get("errors", [])))
    else:
        _pretty(result)


def _pretty(r):
    if not r.get("success"):
        print("FAIL: %s" % "; ".join(r.get("errors", ["unknown"])))
        if r.get("hint"):
            print("hint: %s" % r["hint"])
        return
    for k, v in r.items():
        if k == "success":
            continue
        if isinstance(v, list) and v and isinstance(v[0], dict):
            print("%s (%d):" % (k, len(v)))
            for item in v[:30]:
                print("  " + " | ".join("%s=%s" % (kk, str(vv)[:60]) for kk, vv in list(item.items())[:5]))
            if len(v) > 30:
                print("  ... %d more" % (len(v) - 30))
        elif isinstance(v, dict):
            print("%s:" % k)
            print(json.dumps(v, indent=2, ensure_ascii=False, default=str)[:2000])
        else:
            print("%s: %s" % (k, str(v)[:300]))


def _common_flags(ap, suppress=False):
    d = argparse.SUPPRESS if suppress else False
    if not suppress:
        from . import __version__ as _v
        ap.add_argument("--version", action="version",
                        version="kancil %s" % _v)
    ap.add_argument("--json", action="store_true", default=d, help="JSON output (agent-friendly)")
    ap.add_argument("--quiet", action="store_true", default=d, help="one-line ok/fail")
    ap.add_argument("--raw", action="store_true", default=d, help="raw data only")
    ap.add_argument("--engine", choices=["static", "playwright", "webview"],
                    default=argparse.SUPPRESS if suppress else None)
    ap.add_argument("--pw-browser", choices=["chromium", "firefox", "webkit"],
                    default=argparse.SUPPRESS if suppress else None,
                    help="playwright browser type (default chromium)")
    ap.add_argument("--executable-path",
                    default=argparse.SUPPRESS if suppress else None,
                    help="custom browser binary (e.g. Camoufox firefox). "
                         "Env KANCIL_BROWSER_PATH works too.")
    ap.add_argument("--timeout", type=int, default=argparse.SUPPRESS if suppress else None)
    ap.add_argument("--retries", type=int, default=argparse.SUPPRESS if suppress else None)
    ap.add_argument("--proxy", default=argparse.SUPPRESS if suppress else None,
                    help="proxy URL, e.g. http://127.0.0.1:8080 (persists in session)")
    ap.add_argument("--ua", default=argparse.SUPPRESS if suppress else None,
                    help="User-Agent override (persists in session)")
    ap.add_argument("--session", default=argparse.SUPPRESS if suppress else None,
                    help="named persistent session: playwright storage_state "
                         "is loaded at start and saved at exit")
    ap.add_argument("--no-cache", action="store_true",
                    default=argparse.SUPPRESS if suppress else False,
                    help="disable the static engine's HTTP cache "
                         "(ETag/Last-Modified revalidation)")
    ap.add_argument("--local", action="store_true",
                    default=argparse.SUPPRESS if suppress else False,
                    help="bypass a running kancil daemon; run in this process")
    return ap


def build_parser():
    p = argparse.ArgumentParser(prog="kancil", description="Kancil — agent browser + DevTools for Termux")
    _common_flags(p)
    sub = p.add_subparsers(dest="cmd")

    def SP(name, **kw):
        # subparser that also accepts the global flags anywhere.
        # SUPPRESS defaults so flags given BEFORE the subcommand survive.
        c = argparse.ArgumentParser(add_help=False)
        _common_flags(c, suppress=True)
        return sub.add_parser(name, parents=[c], **kw)

    SP("shell", help="interactive REPL")
    sp = SP("open", help="open URL")
    sp.add_argument("url")
    sp.add_argument("--wait-ms", type=int, default=0,
                    help="wait N ms after load (playwright: lets JS hydrate)")
    SP("back", help="go back")
    SP("fwd", help="go forward")
    SP("reload", help="reload page")
    SP("tabs", help="list tabs")
    sp = SP("new-tab", help="new tab")
    sp.add_argument("url", nargs="?")
    sp = SP("switch", help="switch tab")
    sp.add_argument("id", type=int)
    sp = SP("close-tab", help="close tab")
    sp.add_argument("id", type=int, nargs="?")

    dom = SP("dom", help="DOM inspector")
    dom.add_argument("action", nargs="?", choices=["tree", "inspect", "find", "xpath"],
                     default="tree")
    dom.add_argument("target", nargs="?")
    dom.add_argument("--max", type=int, default=200)

    el = SP("click", help="click element")
    el.add_argument("selector")
    el = SP("type", help="type into element")
    el.add_argument("selector")
    el.add_argument("text")
    el = SP("clear", help="clear element")
    el.add_argument("selector")
    el = SP("select", help="choose dropdown option")
    el.add_argument("selector")
    el.add_argument("value")
    el = SP("check", help="check checkbox/radio")
    el.add_argument("selector")
    el = SP("uncheck", help="uncheck")
    el.add_argument("selector")
    el = SP("hover", help="hover element")
    el.add_argument("selector")
    el = SP("scroll", help="scroll page/element")
    el.add_argument("target", nargs="?", default="bottom")

    js = SP("js", help="evaluate JavaScript (playwright engine)")
    js.add_argument("expr", nargs=argparse.REMAINDER)
    w = SP("wait", help="wait for selector/ms/text")
    w.add_argument("target", nargs="?")
    w.add_argument("--ms", type=int, default=None)
    w.add_argument("--text", default=None)
    SP("console", help="JS console logs")
    SP("errors", help="error console")

    sh = SP("screenshot", help="take screenshot (playwright engine)")
    sh.add_argument("--full", action="store_true")
    sh.add_argument("--element", default=None)
    sh.add_argument("--out", default=None)

    net = SP("network", help="network log")
    net.add_argument("action", nargs="?", default="list")
    net.add_argument("target", nargs="?")
    net.add_argument("--limit", type=int, default=50)
    net.add_argument("--har", default=None, help="export HAR to this path")
    net.add_argument("--type", default=None, help="filter resource type: xhr, fetch, document, image")
    net.add_argument("--status", default=None, help="filter status: 200, 404, 2xx, 4xx")
    net.add_argument("--method", default=None, help="filter method: GET, POST")
    net.add_argument("--url", default=None, help="filter URL substring")
    net.add_argument("--no-redact", action="store_true", help="disable HAR redaction")

    sto = SP("storage", help="storage devtools")
    sto.add_argument("action", nargs="?", choices=["list", "get", "set", "delete", "cookies"],
                     default="list")
    sto.add_argument("key", nargs="?")
    sto.add_argument("value", nargs="?")
    sto.add_argument("--kind", choices=["local", "session"], default="local")

    sc = SP("scrape", help="scrape structured data")
    sc.add_argument("url", nargs="?")
    sc.add_argument("--selector", default=None)
    sc.add_argument("--fields", default=None, help="name:css,name2:css@attr,...")
    sc.add_argument("--auto", action="store_true")
    sc.add_argument("--format", choices=["json", "csv"], default="json")
    sc.add_argument("--pages", type=int, default=1, help="max pages (alias --max-pages)")
    sc.add_argument("--max-pages", type=int, default=None)
    sc.add_argument("--max-items", type=int, default=1000)
    sc.add_argument("--crawl-timeout", type=int, default=180,
                    dest="crawl_timeout", help="total crawl seconds")
    sc.add_argument("--same-content-limit", type=int, default=3)
    sc.add_argument("--scroll-pages", type=int, default=0,
                    help="infinite scroll rounds (playwright only)")
    sc.add_argument("--next", default=None, help="next-page selector")
    sc.add_argument("--delay", type=float, default=1.0)
    sc.add_argument("--no-robots", action="store_true",
                    help="ignore robots.txt (not recommended)")
    sc.add_argument("--sitemap", action="store_true",
                    help="discover pages from sitemap.xml instead of pagination")
    sc.add_argument("--workers", type=int, default=1,
                    help="concurrent fetch workers (needs --sitemap, static only, max 8)")

    ex = SP("extract", help="reader mode")
    ex.add_argument("--mode", choices=["auto", "article", "links", "images", "tables"],
                    default="auto")
    ay = SP("a11y", help="accessibility tree")
    ay.add_argument("action", nargs="?", choices=["tree", "list", "find"], default="tree")
    ay.add_argument("query", nargs="?")
    ay.add_argument("--role", default=None)
    sp = SP("perf", help="performance timing")

    fm = SP("forms", help="list forms")
    fi = SP("form", help="form fill/submit")
    fi.add_argument("action", choices=["fill", "submit", "inspect"])
    fi.add_argument("id", type=int)
    fi.add_argument("--auto", action="store_true")
    fi.add_argument("--set", action="append", default=[], help="field=value")

    dl = SP("download", help="download URL")
    dl.add_argument("url")
    dl.add_argument("--out", default=None)
    SP("downloads", help="download manager")
    dp = SP("dlpause", help="pause download")
    dp.add_argument("id", type=int)
    dr = SP("dlresume", help="resume download")
    dr.add_argument("id", type=int)

    cr = SP("crawl", help="crawl site")
    cr.add_argument("url")
    cr.add_argument("--depth", type=int, default=2)
    cr.add_argument("--max", type=int, default=30)
    cr.add_argument("--delay", type=float, default=1.0)
    cr.add_argument("--pattern", default=None)
    cr.add_argument("--external", action="store_true")

    se = SP("session", help="sessions")
    se.add_argument("action", choices=["save", "load", "list", "delete", "info"])
    se.add_argument("name", nargs="?")

    ha = SP("har", help="HAR recording session")
    ha.add_argument("action", choices=["start", "stop", "export", "clear", "stats"])
    ha.add_argument("path", nargs="?", default="network.har")
    ha.add_argument("--no-redact", action="store_true")
    ha.add_argument("--redact-cookie", action="store_true")
    ha.add_argument("--redact-authorization", action="store_true")
    ha.add_argument("--redact-token", action="store_true")

    bm = SP("bookmark", help="bookmarks")
    bm.add_argument("action", choices=["add", "list", "open", "delete"])
    bm.add_argument("target", nargs="?", help="url (add) or id (open/delete)")
    bm.add_argument("--title", default=None)

    pf = SP("profile", help="form-fill profile")
    pf.add_argument("action", choices=["set", "get", "list", "delete"])
    pf.add_argument("key", nargs="?")
    pf.add_argument("value", nargs="?")

    px = SP("proxy", help="proxy settings")
    px.add_argument("action", choices=["set", "clear", "show"])
    px.add_argument("url", nargs="?")

    ua = SP("ua", help="user-agent")
    ua.add_argument("action", choices=["show", "set", "rotate", "list"])
    ua.add_argument("value", nargs="?")

    bl = SP("block", help="request blocking (playwright engine)")
    bl.add_argument("action", choices=["add", "list", "clear"])
    bl.add_argument("pattern", nargs="?")

    pdf = SP("pdf", help="export page as PDF (playwright engine)")
    pdf.add_argument("--out", default=None)

    SP("devtools", help="capabilities & engine info")
    SP("search", help="web search").add_argument("query", nargs=argparse.REMAINDER)

    to = SP("tool", help="structured agent tool call (JSON in, JSON out)")
    to.add_argument("payload", nargs="?",
                    help='JSON like \'{"action":"click","selector":"#login"}\' (or stdin)')

    sn = SP("snapshot", help="agent context snapshot")
    sn.add_argument("--full", action="store_true")
    sn.add_argument("--dom", action="store_true")
    sn.add_argument("--no-a11y", action="store_true")
    sn.add_argument("--no-links", action="store_true")
    sn.add_argument("--no-forms", action="store_true")

    SP("observe", help="observability: console/network/page errors + perf")
    SP("warnings", help="console warnings")

    yt = SP("yt-search", help="YouTube search via ytInitialData (no JS)")
    yt.add_argument("query", nargs="+")
    yt.add_argument("--max", type=int, default=20)

    yv = SP("yt-video", help="YouTube video metadata via og: tags")
    yv.add_argument("url")

    SP("page-json", help="extract JSON blobs embedded in the page HTML")
    SP("structured", help="extract JSON-LD + OpenGraph/Twitter meta tags")
    sm = SP("sitemap", help="list page URLs from sitemap.xml")
    sm.add_argument("url", nargs="?")
    sm.add_argument("--max-urls", type=int, default=5000, dest="max_urls")
    hc = SP("http-cache", help="inspect/clear the HTTP cache")
    hc.add_argument("action", nargs="?", choices=["stats", "clear"],
                    default="stats")

    vw = SP("view", help="local viewer: see the page in your browser, "
                         "take over navigation (static) or live screen "
                         "(playwright)")
    vw.add_argument("--port", type=int, default=0)
    vw.add_argument("--host", default="127.0.0.1")

    yp = SP("yt-play", help="mini YouTube player: URL/ID or search query -> "
                            "player page (official embed, audio+video)")
    yp.add_argument("target", nargs="+")
    yp.add_argument("--port", type=int, default=0)
    yp.add_argument("--host", default="127.0.0.1")

    ag = SP("agent", help="drive the real rendered page (injected agent.js)")
    ag_sub = ag.add_subparsers(dest="agent_cmd")
    ag_sub.add_parser("tabs", help="list live agent tabs")
    ag_c = ag_sub.add_parser("cmd", help="send command to a live tab")
    ag_c.add_argument("tab")
    ag_c.add_argument("action",
                      choices=["click", "type", "scroll", "eval",
                               "snapshot", "text", "console"])
    ag_c.add_argument("args", nargs="?", default="{}",
                      help='JSON args, e.g. \'{"selector":"a.x"}\'')
    ag_c.add_argument("--timeout", type=int, default=30)
    ag_s = ag_sub.add_parser("snap", help="DOM snapshot of a live tab")
    ag_s.add_argument("tab")
    ag_s.add_argument("--timeout", type=int, default=30)

    SP("proxy-ca", help="generate MITM CA for serve-proxy "
                        "(install ca.crt on the phone once)")
    dm = SP("daemon", help="persistent background engine: zero per-command "
                           "startup cost (warm DNS, keep-alive, cookies, tabs)")
    dm.add_argument("action", nargs="?", default="status",
                    choices=["start", "stop", "restart", "status"],
                    help="default: status")
    ci = SP("cookies-import", help="import Netscape-format cookies.txt "
                                   "(e.g. exported from your real browser) "
                                   "into the static engine's jar")
    ci.add_argument("file")
    px = SP("serve-proxy", help="level 2: local HTTP(S) proxy — your real "
                                "browser's traffic goes through Kancil")
    px.add_argument("--port", type=int, default=8080)
    px.add_argument("--host", default="127.0.0.1")
    px.add_argument("--mitm", action="store_true",
                    help="decrypt HTTPS with the CA (needs proxy-ca + "
                         "cert installed on the phone)")
    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0
    if args.cmd == "shell":
        from . import repl
        repl.run(engine=args.engine)
        return 0

    # Daemon fast-path: a running daemon means one warm engine
    # (DNS, keep-alive, cookies, tabs) — no per-command startup cost.
    # Everything except long-lived/interactive commands routes through it.
    if args.cmd not in ("daemon", "shell", "agent", "serve-proxy", "proxy-ca") \
            and not getattr(args, "local", False):
        from . import daemon as _dm
        if _dm.alive():
            if args.cmd == "tool" and not getattr(args, "payload", None) \
                    and not sys.stdin.isatty():
                args.payload = sys.stdin.read()
            try:
                rep = _dm.call({"op": "_dispatch", "args": vars(args),
                                "cwd": os.getcwd()})
            except Exception as e:
                sys.stderr.write("daemon unreachable (%s); running locally\n"
                                 % str(e)[:100])
                rep = None
            if rep is not None:
                if rep.get("ok"):
                    result = rep.get("result") or {
                        "success": False, "errors": ["empty daemon reply"]}
                else:
                    result = {"success": False, "errors": [
                        "daemon: %s" % rep.get("error", "unknown")]}
                if result.get("_direct"):
                    return 0
                emit(result, args)
                return 0 if result.get("success") else 1

    # The CLI is one-process-per-command: hold the state lock across the
    # whole load -> dispatch -> save cycle so parallel invocations can't
    # last-write-wins each other's tabs/netlog.
    with session_mod.StateLock():
        try:
            b, st = get_browser(args)
        except Exception as e:
            emit({"success": False,
                  "errors": ["engine start failed: %s" % str(e)[:200]]}, args)
            return 1
        if args.timeout:
            st["timeout"] = args.timeout
        if args.retries is not None:
            st["retries"] = args.retries
        try:
            result = dispatch(b, args)
        except Exception as e:
            result = {"success": False, "errors": ["%s: %s" % (type(e).__name__, str(e)[:200])]}
        put_browser(b, st, args)
    if result.get("_direct"):
        return 0
    emit(result, args)
    return 0 if result.get("success") else 1


def dispatch(b, args):
    c = args.cmd
    if c == "open":
        r = b.open(args.url)
        wait_ms = getattr(args, "wait_ms", 0) or 0
        if wait_ms > 0 and r.get("success"):
            import time as _t
            _t.sleep(wait_ms / 1000.0)
            r["waited_ms"] = wait_ms
        return r
    if c == "back":
        return b.back()
    if c == "fwd":
        return b.forward()
    if c == "reload":
        return b.reload()
    if c == "tabs":
        return b.tabs()
    if c == "new-tab":
        return b.new_tab(args.url)
    if c == "switch":
        return b.switch_tab(args.id)
    if c == "close-tab":
        return b.close_tab(args.id)
    if c == "dom":
        if args.action == "tree":
            return b.dom_tree(max_nodes=args.max)
        if args.action == "inspect":
            return b.inspect(args.target or "body")
        if args.action == "find":
            return b.dom_find(args.target or "")
        if args.action == "xpath":
            return b.dom_xpath(args.target or "")
    if c == "click":
        return b.click(args.selector)
    if c == "type":
        return b.type(args.selector, args.text)
    if c == "clear":
        return b.clear(args.selector)
    if c == "select":
        return b.select(args.selector, args.value)
    if c == "check":
        return b.check(args.selector)
    if c == "uncheck":
        return b.uncheck(args.selector)
    if c == "hover":
        return b.hover(args.selector)
    if c == "scroll":
        return b.scroll(args.target)
    if c == "js":
        return b.evaluate(" ".join(args.expr))
    if c == "wait":
        if args.ms:
            return b.wait(ms=args.ms)
        if args.text:
            return b.wait(text=args.text)
        return b.wait(selector=args.target)
    if c == "console":
        return b.console()
    if c == "errors":
        return b.errors()
    if c == "screenshot":
        return b.screenshot(path=args.out, full=args.full, selector=args.element)
    if c == "network":
        if args.har:
            return b.har_export(args.har, redact=not args.no_redact)
        if args.action == "clear":
            return b.network_clear()
        if args.action == "response":
            try:
                return b.network_response(int(args.target))
            except (ValueError, TypeError):
                return {"success": False, "errors": ["usage: kancil network response <id>"]}
        if args.action == "request":
            try:
                return b.network_request(int(args.target))
            except (ValueError, TypeError):
                return {"success": False, "errors": ["usage: kancil network request <id>"]}
        filt = dict(type_=args.type, status=args.status, method=args.method)
        if args.action == "filter":
            return b.network(pattern=args.target, limit=args.limit, **filt)
        if args.action == "list":
            pat = args.url or None
            return b.network(pattern=pat, limit=args.limit, **filt)
        try:
            return b.request(int(args.action))
        except (ValueError, TypeError):
            return {"success": False, "errors": ["usage: kancil network [list|clear|filter <re>|request <id>|response <id>] [--har out.har]"]}
    if c == "storage":
        if args.action == "cookies":
            return b.cookies()
        if args.action == "list":
            return b.storage(args.kind)
        if args.action == "get":
            return b.storage_get(args.key, args.kind)
        if args.action == "set":
            return b.storage_set(args.key, args.value, args.kind)
        if args.action == "delete":
            return b.storage_delete(args.key, args.kind)
    if c == "scrape":
        fields = {}
        if args.fields:
            for pair in args.fields.split(","):
                if ":" in pair:
                    k, v = pair.split(":", 1)
                    fields[k.strip()] = v.strip()
        max_pages = args.max_pages if args.max_pages else args.pages
        return b.scrape(url=args.url, selector=args.selector, fields=fields,
                        auto=args.auto, fmt=args.format, pages=max_pages,
                        next_selector=args.next, delay=args.delay,
                        max_items=args.max_items, timeout=args.crawl_timeout,
                        same_content_limit=args.same_content_limit,
                        scroll_pages=args.scroll_pages,
                        respect_robots=not args.no_robots,
                        sitemap=args.sitemap, workers=args.workers)
    if c == "structured":
        return b.structured()
    if c == "sitemap":
        return b.sitemap(url=args.url, max_urls=args.max_urls)
    if c == "http-cache":
        return b.http_cache(action=args.action)
    if c == "extract":
        return b.extract(mode=args.mode)
    if c == "a11y":
        if args.action == "list":
            return b.a11y_list()
        if args.action == "find":
            return b.a11y_find(args.query or "", role=args.role)
        r = b.a11y()
        if getattr(args, "json", False) or getattr(args, "raw", False):
            return r
        # compact human tree rendering
        if r.get("success"):
            from .dom import render_a11y_tree, a11y_tree as _at, assign_a11y_refs as _ar
            p = b.engine.page
            doc, tnodes = _at(p.dom)
            _ar(tnodes)
            print(render_a11y_tree(doc))
            return {"success": True, "refs": r["refs"], "_printed": True}
        return r
    if c == "perf":
        return b.perf()
    if c == "forms":
        return b.forms()
    if c == "form":
        if args.action == "inspect":
            f = b.forms()
            forms = f.get("forms", [])
            if 1 <= args.id <= len(forms):
                return {"success": True, "form": forms[args.id - 1]}
            return {"success": False, "errors": ["no such form"]}
        if args.action == "fill":
            values = {}
            for s in args.set:
                if "=" in s:
                    k, v = s.split("=", 1)
                    values[k] = v
            r = b.form_fill(args.id, values or None, auto=args.auto)
            return r
        if args.action == "submit":
            return b.form_submit(args.id)
    if c == "download":
        return b.download(args.url, args.out)
    if c == "downloads":
        return b.downloads()
    if c == "dlpause":
        return b.download_pause(args.id)
    if c == "dlresume":
        return b.download_resume(args.id)
    if c == "crawl":
        from .devtools import scrape_items  # noqa
        return _crawl_cli(b, args)
    if c == "session":
        if args.action == "save":
            return b.session_save(args.name)
        if args.action == "load":
            return b.session_load(args.name)
        if args.action == "delete":
            return b.session_delete(args.name)
        if args.action == "info":
            return b.session_info(args.name)
        return b.session_list()
    if c == "har":
        if args.action == "start":
            return b.har_start()
        if args.action == "stop":
            return b.har_stop()
        if args.action == "clear":
            return b.har_clear()
        if args.action == "stats":
            return b.har_stats()
        # export: redaction ON by default; flags are explicit (also default)
        return b.har_export(args.path, redact=not args.no_redact)
    if c == "bookmark":
        if args.action == "add":
            return b.bookmark_add(url=args.target, title=args.title)
        if args.action == "list":
            return b.bookmark_list()
        if args.action == "open":
            try:
                return b.bookmark_open(int(args.target))
            except (ValueError, TypeError):
                return {"success": False, "errors": ["bookmark open needs an id"]}
        if args.action == "delete":
            try:
                return b.bookmark_delete(int(args.target))
            except (ValueError, TypeError):
                return {"success": False, "errors": ["bookmark delete needs an id"]}
    if c == "profile":
        if args.action == "set":
            if not args.key or args.value is None:
                return {"success": False, "errors": ["usage: kancil profile set <key> <value>"]}
            return b.profile_set(args.key, args.value)
        if args.action == "get":
            return b.profile_get(args.key)
        if args.action == "list":
            return b.profile_list()
        if args.action == "delete":
            return b.profile_delete(args.key)
    if c == "proxy":
        if args.action == "set":
            if not args.url:
                return {"success": False, "errors": ["usage: kancil proxy set <url>"]}
            return b.proxy_set(args.url)
        if args.action == "clear":
            return b.proxy_clear()
        return b.proxy_show()
    if c == "ua":
        if args.action == "set":
            if not args.value:
                return {"success": False, "errors": ["usage: kancil ua set <string>"]}
            return b.ua_set(args.value)
        if args.action == "rotate":
            return b.ua_rotate()
        if args.action == "list":
            return b.ua_list()
        return b.ua_show()
    if c == "block":
        if args.action == "add":
            if not args.pattern:
                return {"success": False, "errors": ["usage: kancil block add <pattern>"]}
            return b.block_add(args.pattern)
        if args.action == "list":
            return b.block_list()
        return b.block_clear()
    if c == "pdf":
        return b.pdf(path=args.out)
    if c == "devtools":
        return {"success": True, "capabilities": b.capabilities,
                "engine": b._engine_name}
    if c == "tool":
        import json as _json
        payload = args.payload
        if not payload and not sys.stdin.isatty():
            payload = sys.stdin.read()
        if not payload:
            return {"success": False, "error": {
                "code": "INVALID_INPUT",
                "message": "usage: kancil tool '{\"action\":\"click\",\"selector\":\"#x\"}'"}}
        try:
            data = _json.loads(payload)
        except Exception as e:
            return {"success": False, "error": {
                "code": "INVALID_INPUT",
                "message": "bad JSON: %s" % str(e)[:120]}}
        # tool() already returns the final structured shape; bypass emit()
        print(b.to_json(b.tool(data)))
        return {"success": True, "_direct": True}
    if c == "snapshot":
        return b.snapshot(full=args.full, dom=args.dom,
                          a11y=not args.no_a11y, links=not args.no_links,
                          forms=not args.no_forms)
    if c == "observe":
        return b.observe()
    if c == "warnings":
        return b.warnings()
    if c == "yt-search":
        return b.yt_search(" ".join(args.query), max_results=args.max)
    if c == "yt-video":
        return b.yt_video(args.url)
    if c == "page-json":
        return b.page_json()
    if c == "view":
        r = b.view(port=args.port, host=args.host)
        if r.get("success"):
            if args.json:
                print(json.dumps({"url": r["url"], "engine": r["engine"],
                                  "mode": r["mode"]}), flush=True)
            else:
                print("kancil view: %s" % r["url"])
                print("engine: %s -- %s" % (r["engine"], r["mode"]))
                print("Open the URL in your browser. Ctrl+C to stop.")
            try:
                import time as _t
                while True:
                    _t.sleep(3600)
            except KeyboardInterrupt:
                pass
            return {"success": True, "stopped": True}
        return r
    if c == "agent":
        sub = args.agent_cmd
        if sub == "tabs":
            return b.agent_tabs()
        if sub == "cmd":
            try:
                a = json.loads(args.args)
            except Exception as e:
                return {"success": False,
                        "errors": ["bad JSON args: %s" % e]}
            return b.agent_cmd(args.tab, args.action, a,
                               timeout=args.timeout)
        if sub == "snap":
            return b.agent_snapshot(args.tab, timeout=args.timeout)
        return {"success": False, "errors": ["usage: kancil agent {tabs|cmd|snap}"]}
    if c == "proxy-ca":
        from .proxy_server import ensure_ca, ca_fingerprint, ca_paths
        try:
            crt, key = ensure_ca()
        except Exception as e:
            return {"success": False, "errors": [str(e)]}
        print("CA cert : %s" % crt)
        print("CA key  : %s (keep secret)" % key)
        print("SHA256  : %s" % ca_fingerprint(crt))
        print()
        print("Install once on Android: Settings > Security > Encryption &")
        print("credentials > Install a certificate > CA certificate,")
        print("pilih file ca.crt di atas.")
        print("Lalu: set proxy WiFi ke 127.0.0.1:8080 dan jalankan")
        print("`kancil serve-proxy --mitm`.")
        return {"success": True, "ca_crt": crt}
    if c == "cookies-import":
        return b.cookies_import(args.file)
    if c == "daemon":
        from . import daemon as _dm
        act = args.action or "status"
        if act == "status":
            run = _dm.alive()
            return {"success": True, "daemon": "running" if run else "stopped",
                    "pid": _dm._read_pid(), "sock": _dm.sock_path()}
        if act == "stop":
            return _dm.stop()
        # start / restart
        if act == "restart":
            _dm.stop()
        return _dm.start(
            engine=getattr(args, "engine", None) or "static",
            proxy=getattr(args, "proxy", None),
            ua=getattr(args, "ua", None),
            timeout=getattr(args, "timeout", None) or 25,
            retries=getattr(args, "retries", None) if getattr(
                args, "retries", None) is not None else 2)
    if c == "serve-proxy":
        from .proxy_server import ProxyServer, ca_fingerprint, ca_paths
        srv = ProxyServer(b, host=args.host, port=args.port,
                          mitm=args.mitm)
        url = srv.start()
        print("kancil serve-proxy: %s  (mitm=%s)" % (url, srv.mitm))
        if args.mitm and not srv.mitm:
            print()
            print("MITM requested but NOT active. Fix:")
            print("  1. Generate the CA : kancil proxy-ca  (pure Python, no")
            print("     openssl needed; openssl CLI is only a fallback)")
            print("  2. Install ca.crt on the phone: Settings > Security >")
            print("     Encryption & credentials > Install a certificate > CA")
            print("  3. Re-run          : kancil serve-proxy --mitm")
        else:
            print("Set phone WiFi proxy -> %s:%d" % (args.host, srv.port))
            if srv.mitm:
                try:
                    crt, _k = ca_paths()
                    print("MITM CA fingerprint: %s" % ca_fingerprint(crt))
                    print("(it must match the CA certificate installed on the phone)")
                except Exception:
                    pass
        print("Ctrl+C to stop.")
        try:
            import time as _t
            while True:
                _t.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            srv.stop()
        return {"success": True, "stopped": True}
    if c == "yt-play":
        target = " ".join(args.target).strip()
        if not target:
            return {"success": False,
                    "errors": ["usage: kancil yt-play <url|id|query>"]}
        r = b.yt_play(target, port=args.port, host=args.host)
        if r.get("success"):
            if args.json:
                print(json.dumps({"play_url": r["play_url"],
                                  "video_id": r["video_id"],
                                  "title": r.get("title", "")}), flush=True)
            else:
                if r.get("title"):
                    print("Playing: %s" % r["title"])
                print("player: %s" % r["play_url"])
                print("Open the URL in your browser. Ctrl+C to stop.")
            try:
                import time as _t
                while True:
                    _t.sleep(3600)
            except KeyboardInterrupt:
                pass
            return {"success": True, "stopped": True}
        return r
    if c == "search":
        return _search_cli(b, args)
    return {"success": False, "errors": ["unknown command %s" % c]}


def _crawl_cli(b, args):
    import re, time, urllib.parse
    eng = b.engine
    start = args.url if "://" in args.url else "https://" + args.url
    host = urllib.parse.urlparse(start).netloc
    rx = re.compile(args.pattern) if args.pattern else None
    seen, queue, results = set(), [(start, 0)], []
    while queue and len(results) < args.max:
        url, d = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        r = eng.open(url)
        if not r.get("success"):
            results.append({"url": url, "status": "ERR", "title": ""})
            continue
        p = eng.page
        results.append({"url": p.url, "status": "ok", "title": p.title[:80]})
        if d < args.depth and p.dom:
            from .dom import select
            for _, lu in p.links:
                if lu in seen or not lu.startswith("http"):
                    continue
                if not args.external and urllib.parse.urlparse(lu).netloc != host:
                    continue
                if rx and not rx.search(lu):
                    continue
                queue.append((lu, d + 1))
        time.sleep(args.delay)
    import time as _t
    out_path = os.path.join(session_mod.BASE,
                            "crawl-%s.txt" % _t.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(session_mod.BASE, exist_ok=True)
    with open(out_path, "w") as f:
        for r_ in results:
            f.write(r_["url"] + "\n")
    return {"success": True, "pages": len(results), "results": results,
            "saved": out_path}


def _search_cli(b, args):
    import urllib.parse
    q = " ".join(args.query)
    url = "https://www.bing.com/search?format=rss&q=" + urllib.parse.quote_plus(q)
    r = b.open(url)
    if not r.get("success"):
        return r
    import re, html as ih
    p = b.engine.page
    raw = p.raw.decode("utf-8", errors="ignore") if isinstance(p.raw, bytes) else p.raw
    out = []
    for m in re.finditer(r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>", raw, re.S):
        out.append({"title": ih.unescape(re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1)).strip())[:100],
                    "url": ih.unescape(m.group(2).strip())})
        if len(out) >= 15:
            break
    return {"success": True, "query": q, "results": out}


if __name__ == "__main__":
    sys.exit(main())
