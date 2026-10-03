"""Kancil — DOM, CSS selectors, XPath, inspector.

Pure Python, no dependencies. Works on Termux/Android (ARM64).
"""
import re
from html.parser import HTMLParser

VERSION = "3.0.0"

VOID = {"br", "img", "input", "hr", "meta", "link", "source", "wbr", "embed",
        "track", "param", "area", "base", "col", "circle"}
SKIP_CONTENT = {"script", "style", "noscript", "template"}


class Node:
    __slots__ = ("tag", "attrs", "parent", "children")

    def __init__(self, tag, attrs, parent=None):
        self.tag = tag.lower()
        self.attrs = {k.lower(): (v if v is not None else "") for k, v in attrs}
        self.parent = parent
        self.children = []

    def get(self, name, default=""):
        return self.attrs.get(name.lower(), default)

    def text_content(self):
        parts = []
        for ch in self.children:
            parts.append(ch.text_content() if isinstance(ch, Node) else ch)
        return re.sub(r"\s+", " ", "".join(parts)).strip()

    def inner_text(self):
        return self.text_content()

    def __repr__(self):
        return "<%s %s>" % (self.tag, " ".join("%s=%r" % (k, v) for k, v in list(self.attrs.items())[:3]))


def walk(node):
    if isinstance(node, Node):
        yield node
        for ch in node.children:
            yield from walk(ch)


def walk_elements(node):
    """Yield element nodes only (skip the document root pseudo-node)."""
    for n in walk(node):
        if n.tag != "document":
            yield n


class DOMBuilder(HTMLParser):
    # Simplified HTML5 auto-closing: a start tag implicitly closes an open
    # element of the mapped tags (handles mis-nested tag soup without a
    # full spec parser or extra dependencies).
    P_CLOSERS = {"address", "article", "aside", "blockquote", "details",
                 "dialog", "div", "dl", "fieldset", "figcaption", "figure",
                 "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
                 "header", "hgroup", "hr", "main", "menu", "nav", "ol", "p",
                 "pre", "section", "table", "ul"}
    AUTO_CLOSE = {
        "li": {"li"},
        "dd": {"dd", "dt"}, "dt": {"dd", "dt"},
        "tr": {"tr"}, "td": {"td", "th"}, "th": {"td", "th"},
        "option": {"option"}, "optgroup": {"optgroup"},
        "a": {"a"}, "button": {"button"},
        "nobr": {"nobr"},
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("document", [])
        self.stack = [self.root]
        self._skip = 0

    def _auto_close(self, tag):
        targets = set(self.AUTO_CLOSE.get(tag, ()))
        if tag in self.P_CLOSERS:
            targets.add("p")
        if not targets:
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag in targets:
                del self.stack[i:]
                break
            # don't cross scope boundaries
            if self.stack[i].tag in ("table", "template", "html"):
                break

    def handle_starttag(self, tag, attrs):
        self._auto_close(tag)
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag in SKIP_CONTENT:
            self._skip += 1
        if tag not in VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if tag in SKIP_CONTENT and self._skip:
            self._skip -= 1
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if self._skip or not data.strip():
            return
        self.stack[-1].children.append(data)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_comment(self, data):
        pass


def build_dom(html_text):
    b = DOMBuilder()
    b.feed(html_text or "")
    return b.root


# ---------------- CSS selectors ----------------
def parse_simple(sel):
    """Parse one compound selector. Returns dict or None if unsupported.

    Supports: tag, #id, .class, [attr], [attr=val], [attr^=/$=/*=/~=val],
    :nth-of-type(N), :nth-child(N), :first-child, :last-child,
    :not(simple), :disabled, :enabled.
    Dynamic pseudo-classes (:hover, :visited, ...) return None — they can't
    be evaluated against a static DOM.
    """
    s = {"tag": None, "id": None, "classes": [], "attrs": [],
         "nth_of_type": None, "nth_child": None, "first_child": False,
         "last_child": False, "not": [], "disabled": False, "enabled": False}
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9_-]*)", sel)
    rest = sel
    if m:
        s["tag"] = m.group(1).lower()
        rest = sel[m.end():]
    elif rest[:1] not in ("#", ".", "[", ":"):
        return None
    while rest:
        if rest[0] == ":":
            m = re.match(r":([a-zA-Z-]+)(\(([^()]*)\))?", rest)
            if not m:
                return None
            pseudo, arg = m.group(1), m.group(3)
            rest = rest[m.end():]
            if pseudo == "not":
                inner = parse_simple((arg or "").strip())
                if inner is None:
                    return None
                s["not"].append(inner)
            elif pseudo == "nth-of-type":
                if not arg or not arg.strip().isdigit():
                    return None
                s["nth_of_type"] = int(arg)
            elif pseudo == "nth-child":
                if not arg or not arg.strip().isdigit():
                    return None
                s["nth_child"] = int(arg)
            elif pseudo == "first-child":
                s["first_child"] = True
            elif pseudo == "last-child":
                s["last_child"] = True
            elif pseudo == "disabled":
                s["disabled"] = True
            elif pseudo == "enabled":
                s["enabled"] = True
            else:
                # :hover, :visited, :focus-visible, ... need a live renderer
                return None
        elif rest[0] == "#":
            m = re.match(r"#([a-zA-Z0-9_-]+)", rest)
            if not m:
                return None
            s["id"] = m.group(1)
            rest = rest[m.end():]
        elif rest[0] == ".":
            m = re.match(r"\.([a-zA-Z0-9_-]+)", rest)
            if not m:
                return None
            s["classes"].append(m.group(1))
            rest = rest[m.end():]
        elif rest[0] == "[":
            m = re.match(r"\[([a-zA-Z0-9_-]+)"
                         r"(?:\s*([~^$*]?=)\s*"
                         r"(?:\"([^\"]*)\"|'([^']*)'|([^\]\"']+)))?\]", rest)
            if not m:
                return None
            val = m.group(3) if m.group(3) is not None else (
                m.group(4) if m.group(4) is not None else m.group(5))
            s["attrs"].append((m.group(1).lower(), m.group(2) or "=", val))
            rest = rest[m.end():]
        else:
            return None
    return s


def _prev_elem(node):
    par = node.parent
    if not isinstance(par, Node):
        return None
    prev = None
    for c in par.children:
        if c is node:
            return prev
        if isinstance(c, Node):
            prev = c
    return None


def _next_elem(node):
    par = node.parent
    if not isinstance(par, Node):
        return None
    seen = False
    for c in par.children:
        if seen and isinstance(c, Node):
            return c
        if c is node:
            seen = True
    return None


def match_simple(node, s):
    if not isinstance(node, Node):
        return False
    if s["tag"] and node.tag != s["tag"]:
        return False
    if s["id"] and node.get("id") != s["id"]:
        return False
    classes = node.get("class").split()
    for c in s["classes"]:
        if c not in classes:
            return False
    for name, op, val in s["attrs"]:
        if name not in node.attrs:
            return False
        if val is None:
            continue
        av = node.attrs[name]
        if op == "=" and av != val:
            return False
        elif op == "~=" and val not in av.split():
            return False
        elif op == "^=" and not av.startswith(val):
            return False
        elif op == "$=" and not av.endswith(val):
            return False
        elif op == "*=" and val not in av:
            return False
    for n in s["not"]:
        if match_simple(node, n):
            return False
    if s.get("nth_of_type"):
        par = node.parent
        if not isinstance(par, Node):
            return False
        sibs = [c for c in par.children
                if isinstance(c, Node) and c.tag == node.tag]
        try:
            if sibs.index(node) + 1 != s["nth_of_type"]:
                return False
        except ValueError:
            return False
    if s.get("nth_child"):
        par = node.parent
        if not isinstance(par, Node):
            return False
        els = [c for c in par.children if isinstance(c, Node)]
        try:
            if els.index(node) + 1 != s["nth_child"]:
                return False
        except ValueError:
            return False
    if s.get("first_child") and _prev_elem(node) is not None:
        return False
    if s.get("last_child") and _next_elem(node) is not None:
        return False
    if s.get("disabled") and "disabled" not in node.attrs:
        return False
    if s.get("enabled") and "disabled" in node.attrs:
        return False
    return True


def _split_groups(selector):
    """Split on commas, but not inside [...] or quotes."""
    groups, buf = [], ""
    depth, quote = 0, None
    for ch in selector:
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf += ch
        elif ch == "[":
            depth += 1
            buf += ch
        elif ch == "]":
            depth = max(0, depth - 1)
            buf += ch
        elif ch == "," and depth == 0:
            groups.append(buf)
            buf = ""
        else:
            buf += ch
    groups.append(buf)
    return groups


def _parse_combinators(group):
    """Split a selector group into [(combinator, simple)].

    combinator is ' ' (descendant), '>' (child), '+' (adjacent sibling)
    or '~' (general sibling).
    """
    parts, buf = [], ""
    depth, quote = 0, None
    for ch in group.strip():
        if quote:
            buf += ch
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf += ch
        elif ch == "[":
            depth += 1
            buf += ch
        elif ch == "]":
            depth = max(0, depth - 1)
            buf += ch
        elif depth == 0 and ch in (">", "+", "~"):
            if buf.strip():
                parts.append(buf.strip())
                buf = ""
            parts.append(ch)
        elif depth == 0 and ch.isspace():
            if buf.strip():
                parts.append(buf.strip())
                buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())
    seq = []
    comb = " "
    for p in parts:
        if p in (">", "+", "~"):
            comb = p
            continue
        seq.append((comb, p))
        comb = " "
    return seq


def check_selector(selector):
    """Validate a CSS selector. Returns None if valid, else a reason string."""
    if not selector or not selector.strip():
        return "empty selector"
    for group in _split_groups(selector):
        seq = _parse_combinators(group)
        if not seq:
            return "empty group in %r" % group
        for _comb, s in seq:
            if parse_simple(s) is None:
                m = re.search(r":([a-zA-Z-]+)", s)
                if m and m.group(1) not in (
                        "not", "nth-of-type", "nth-child", "first-child",
                        "last-child", "disabled", "enabled"):
                    return ("unsupported pseudo-class :%s in %r — a static DOM "
                            "can't evaluate dynamic pseudos like :hover or "
                            ":visited" % (m.group(1), s))
                return "can't parse %r (check brackets/quotes/operators)" % s
    return None


def select(root, selector):
    """CSS select: tag, #id, .class, [attr], [attr=val], [attr^=/$=/*=/~=val],
    :nth-of-type/:nth-child/:first-child/:last-child/:not()/:disabled,
    descendant, child (>), adjacent (+) and general (~) siblings, comma groups.
    """
    found = []
    for group in _split_groups(selector):
        seq = _parse_combinators(group)
        simples = []
        valid = bool(seq)
        for comb, s in seq:
            ps = parse_simple(s)
            if ps is None:
                valid = False
                break
            simples.append((comb, ps))
        if not valid:
            continue
        for node in walk_elements(root):
            if not match_simple(node, simples[-1][1]):
                continue
            cur, ok = node, True
            for i in range(len(simples) - 1, 0, -1):
                comb, prev = simples[i][0], simples[i - 1][1]
                if comb == ">":
                    cur = cur.parent if isinstance(cur.parent, Node) else None
                    if not (cur is not None and match_simple(cur, prev)):
                        ok = False
                        break
                elif comb == "+":
                    cur = _prev_elem(cur)
                    if not (cur is not None and match_simple(cur, prev)):
                        ok = False
                        break
                elif comb == "~":
                    sib, hit = _prev_elem(cur), None
                    while sib is not None:
                        if match_simple(sib, prev):
                            hit = sib
                            break
                        sib = _prev_elem(sib)
                    if hit is None:
                        ok = False
                        break
                    cur = hit
                else:  # descendant
                    anc = cur.parent if isinstance(cur.parent, Node) else None
                    while anc is not None and not match_simple(anc, prev):
                        anc = anc.parent if isinstance(anc.parent, Node) else None
                    if anc is None:
                        ok = False
                        break
                    cur = anc
            if ok:
                found.append(node)
    seen, out = set(), []
    for n in found:
        if id(n) not in seen:
            seen.add(id(n))
            out.append(n)
    return out


def select_one(root, selector):
    r = select(root, selector)
    return r[0] if r else None


# ---------------- XPath (useful subset) ----------------
# Supports: //tag, //tag[@a='v'], //tag[@a], //tag[N], //tag[text()='v'],
#            //tag[contains(text(),'v')], //tag[contains(@a,'v')], //*,
#            /html/body/div, relative paths, | union (via multiple calls).
_XPATH_STEP = re.compile(
    r"^(?P<tag>\*|[a-zA-Z][a-zA-Z0-9_-]*)"
    r"(?P<preds>(\[([^\]]+)\])*)$"
)
_XPATH_PRED = re.compile(
    r"@(?P<attr>[a-zA-Z0-9_-]+)\s*=\s*['\"](?P<val>[^'\"]*)['\"]"   # [@a='v']
    r"|@(?P<attr2>[a-zA-Z0-9_-]+)"                                 # [@a]
    r"|(?P<pos>\d+)"                                               # [3]
    r"|text\(\)\s*=\s*['\"](?P<text>[^'\"]*)['\"]"                 # [text()='v']
    r"|contains\(text\(\),\s*['\"](?P<ctext>[^'\"]*)['\"]\)"       # [contains(text(),'v')]
    r"|contains\(@(?P<cattr>[a-zA-Z0-9_-]+),\s*['\"](?P<cval>[^'\"]*)['\"]\)"  # [contains(@a,'v')]
)


def _parse_xpath(expr):
    expr = expr.strip()
    if not expr:
        raise ValueError("empty xpath")
    steps = []
    i = 0
    first_axis = "child"
    if expr.startswith("//"):
        first_axis = "descendant"
        i = 2
    elif expr.startswith("/"):
        i = 1
    buf, depth = "", 0
    raw_steps = []
    while i < len(expr):
        ch = expr[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "/" and depth == 0:
            raw_steps.append(buf)
            buf = ""
            if expr[i:i + 2] == "//":
                raw_steps.append("//")
                i += 2
                continue
            i += 1
            continue
        buf += ch
        i += 1
    if buf:
        raw_steps.append(buf)
    axis = first_axis
    for rs in raw_steps:
        if rs == "//":
            axis = "descendant"
            continue
        m = _XPATH_STEP.match(rs)
        if not m:
            raise ValueError("unsupported xpath step: %r" % rs)
        preds = []
        for pm in re.finditer(r"\[([^\]]+)\]", m.group("preds") or ""):
            p = pm.group(1).strip()
            xm = _XPATH_PRED.fullmatch(p)
            if not xm:
                raise ValueError("unsupported xpath predicate: [%s]" % p)
            preds.append(xm.groupdict())
        steps.append({"axis": axis, "tag": m.group("tag"), "preds": preds})
        axis = "child"
    return steps


def _match_pred(node, pr):
    if pr["attr"]:
        return node.attrs.get(pr["attr"]) == pr["val"]
    if pr["attr2"]:
        return pr["attr2"] in node.attrs
    if pr["pos"]:
        return True  # handled positionally below
    if pr["text"] is not None:
        return node.text_content() == pr["text"]
    if pr["ctext"] is not None:
        return pr["ctext"] in node.text_content()
    if pr["cattr"]:
        return pr["cval"] in node.attrs.get(pr["cattr"], "")
    return True


def xpath(root, expr):
    """Evaluate a useful XPath subset. Returns list of Nodes."""
    steps = _parse_xpath(expr)
    current = [root]
    for si, st in enumerate(steps):
        nxt = []
        for ctx in current:
            if st["axis"] == "descendant":
                cands = [n for n in walk_elements(ctx)]
            else:
                cands = [ch for ch in ctx.children if isinstance(ch, Node)]
            if st["tag"] != "*":
                cands = [n for n in cands if n.tag == st["tag"]]
            # positional predicate applies per parent group for child axis,
            # and document-order for descendant axis (simplified: global order)
            pos_wanted = None
            other = []
            for pr in st["preds"]:
                if pr["pos"]:
                    pos_wanted = int(pr["pos"])
                else:
                    other.append(pr)
            cands = [n for n in cands if all(_match_pred(n, pr) for pr in other)]
            if pos_wanted is not None:
                cands = [cands[pos_wanted - 1]] if 0 < pos_wanted <= len(cands) else []
            nxt.extend(cands)
        # dedupe
        seen, current = set(), []
        for n in nxt:
            if id(n) not in seen:
                seen.add(id(n))
                current.append(n)
    return current


# ---------------- inspector ----------------
def outer_html(node, limit=1200):
    parts = []

    def rec(n):
        if isinstance(n, str):
            parts.append(n)
            return
        attrs = "".join(' %s="%s"' % (k, v) for k, v in n.attrs.items())
        if n.tag in VOID:
            parts.append("<%s%s>" % (n.tag, attrs))
            return
        parts.append("<%s%s>" % (n.tag, attrs))
        for ch in n.children:
            rec(ch)
            if sum(len(p) for p in parts) > limit:
                break
        parts.append("</%s>" % n.tag)

    rec(node)
    s = "".join(parts)
    return s if len(s) <= limit else s[:limit] + "…"


def generate_css(node):
    if node.get("id"):
        return "#%s" % node.get("id")
    if node.get("class"):
        return "%s.%s" % (node.tag, ".".join(node.get("class").split()))
    # nth-of-type among siblings
    parent = node.parent
    if isinstance(parent, Node):
        same = [c for c in parent.children if isinstance(c, Node) and c.tag == node.tag]
        if len(same) > 1:
            idx = same.index(node) + 1
            base = generate_css(parent) if parent.tag != "document" else ""
            return ("%s > " % base if base else "") + "%s:nth-of-type(%d)" % (node.tag, idx)
    if isinstance(parent, Node) and parent.tag != "document":
        return generate_css(parent) + " > " + node.tag
    return node.tag


def generate_xpath(node):
    parts = []
    n = node
    while isinstance(n, Node) and n.tag != "document":
        parent = n.parent
        if isinstance(parent, Node):
            same = [c for c in parent.children if isinstance(c, Node) and c.tag == n.tag]
            idx = same.index(n) + 1 if len(same) > 1 else None
        else:
            idx = None
        parts.append("/%s%s" % (n.tag, "[%d]" % idx if idx else ""))
        n = parent
    return "".join(reversed(parts)) or "/"


def node_summary(node, max_text=60):
    d = {"tag": node.tag, "attributes": dict(node.attrs)}
    t = node.text_content()
    if t:
        d["text"] = t[:max_text]
    return d


def inspect_element(node, root):
    """DevTools-style inspector output (dict, JSON-serializable)."""
    parent = node.parent if isinstance(node.parent, Node) and node.parent.tag != "document" else None
    children = [node_summary(c) for c in node.children if isinstance(c, Node)][:20]
    sibs = []
    if isinstance(node.parent, Node):
        sibs = [c for c in node.parent.children if isinstance(c, Node)]
    return {
        "tag": node.tag,
        "attributes": dict(node.attrs),
        "text": node.text_content()[:500],
        "html": outer_html(node),
        "css": generate_css(node),
        "xpath": generate_xpath(node),
        "parent": node_summary(parent) if parent else None,
        "children": children,
        "child_count": len([c for c in node.children if isinstance(c, Node)]),
        "sibling_index": sibs.index(node) + 1 if node in sibs else None,
        "sibling_count": len(sibs),
    }


# ---------------- smart element resolution ----------------
def smart_resolve(root, query):
    """Resolve a human query to an element. Returns (node, method)."""
    q = query.strip()
    if not q:
        return None, None
    # 1. direct CSS selector
    try:
        nodes = select(root, q)
        if nodes:
            return nodes[0], "css"
    except Exception:
        pass
    # 2. xpath
    if q.startswith("/") or q.startswith("("):
        try:
            nodes = xpath(root, q)
            if nodes:
                return nodes[0], "xpath"
        except Exception:
            pass
    ql = q.lower()
    # 3. #id / [name=] / [aria-label=]
    for sel, method in (("#" + re.sub(r"\s+", "-", ql), "id"),
                        ("[name='%s']" % q, "name"),
                        ("[aria-label='%s']" % q, "aria-label"),
                        ("[placeholder='%s']" % q, "placeholder")):
        try:
            nodes = select(root, sel)
            if nodes:
                return nodes[0], method
        except Exception:
            pass
    # 4. text match on buttons/links (exact then contains)
    for tag in ("button", "a", "input"):
        for n in select(root, tag):
            name = (n.get("value") or n.text_content() or n.get("aria-label", "")).strip()
            if name.lower() == ql:
                return n, "text-exact"
    for tag in ("button", "a"):
        for n in select(root, tag):
            name = (n.text_content() or n.get("aria-label", "") or n.get("value", "")).strip()
            if ql in name.lower() and name:
                return n, "text-contains"
    # 5. role= query like 'role=button "Login"' — not supported statically
    return None, None


# ---------------- accessibility tree ----------------
def acc_name(node, root):
    if node.get("aria-label"):
        return node.get("aria-label").strip()[:80]
    nid = node.get("id")
    if nid:
        for lab in select(root, "label"):
            if lab.get("for") == nid:
                txt = lab.text_content()
                if txt:
                    return txt[:80]
    # <label> wrapping the input
    anc = node.parent
    if isinstance(anc, Node) and anc.tag == "label":
        txt = anc.text_content().strip()
        if txt:
            return txt[:80]
    for key in ("placeholder", "title", "alt", "name"):
        v = node.get(key).strip()
        if v:
            return v[:80]
    if node.tag in ("button", "a") or node.tag.startswith("h"):
        t = node.text_content()
        if t:
            return t[:80]
    if node.tag == "input" and node.get("type").lower() in ("submit", "button"):
        v = node.get("value").strip()
        if v:
            return v[:80]
    return ""


def a11y_items(root):
    items = []
    for node in walk_elements(root):
        role = None
        tag = node.tag
        if tag == "a" and node.get("href"):
            role = "link"
        elif tag == "button":
            role = "button"
        elif tag == "input":
            t = node.get("type", "text").lower()
            role = {"submit": "button", "button": "button", "reset": "button",
                    "checkbox": "checkbox", "radio": "radio"}.get(t, "textbox")
        elif tag == "select":
            role = "combobox"
        elif tag == "textarea":
            role = "textbox"
        elif tag in ("h1", "h2", "h3"):
            role = "heading"
        if role:
            items.append({"role": role, "name": acc_name(node, root), "node": node})
    return items


def a11y_tree(root):
    """Nested accessibility tree. Returns (tree_dict_with_nodes, flat_list)."""
    items = a11y_items(root)
    id2tn = {}
    tnodes = []
    for it in items:
        tn = {"role": it["role"], "name": it["name"], "node": it["node"],
              "children": []}
        id2tn[id(it["node"])] = tn
        tnodes.append(tn)
    roots = []
    for tn in tnodes:
        anc = tn["node"].parent
        parent_tn = None
        while isinstance(anc, Node) and anc.tag != "document":
            if id(anc) in id2tn:
                parent_tn = id2tn[id(anc)]
                break
            anc = anc.parent
        if parent_tn is not None:
            parent_tn["children"].append(tn)
        else:
            roots.append(tn)
    doc = {"role": "document", "name": "", "node": None, "children": roots}
    return doc, tnodes


def strip_a11y_nodes(tn):
    """JSON-safe tree (drops live Node references)."""
    return {"role": tn["role"], "name": tn["name"],
            **({"ref": tn["ref"]} if "ref" in tn else {}),
            "children": [strip_a11y_nodes(c) for c in tn["children"]]}


def assign_a11y_refs(tnodes):
    """Stable per-page refs like button_1, textbox_2. Returns {ref: node}."""
    refs, counters = {}, {}
    for tn in tnodes:
        role = tn["role"]
        counters[role] = counters.get(role, 0) + 1
        ref = "%s_%d" % (role, counters[role])
        tn["ref"] = ref
        refs[ref] = tn["node"]
    return refs


def a11y_find(root, query, role=None):
    """Semantic search over accessible names. Returns list of items."""
    q = (query or "").lower()
    out = []
    for it in a11y_items(root):
        if role and it["role"] != role:
            continue
        if q in (it["name"] or "").lower():
            out.append(it)
    return out


def dom_sig(root):
    """Structural fingerprint for reference invalidation."""
    import hashlib
    h = hashlib.md5()
    for n in walk_elements(root):
        h.update(("%s|%s|%s" % (n.tag, n.get("id"), n.get("class"))).encode())
    return h.hexdigest()[:16]


def render_a11y_tree(tn, prefix="", last=True, lines=None):
    """Render tree as text: document ├── heading "Welcome" ..."""
    if lines is None:
        lines = []
    if tn["role"] == "document" and not prefix:
        lines.append("document")
    else:
        branch = "└── " if last else "├── "
        name = ' "%s"' % tn["name"] if tn["name"] else ""
        ref = " @%s" % tn["ref"] if tn.get("ref") else ""
        lines.append("%s%s%s%s%s" % (prefix, branch, tn["role"], name, ref))
    kids = tn["children"]
    for i, ch in enumerate(kids):
        is_last = i == len(kids) - 1
        ext = "    " if last else "│   "
        render_a11y_tree(ch, prefix + ext, is_last, lines)
    return "\n".join(lines)

# ---------------- text rendering ----------------
BLOCK = {"p", "div", "section", "article", "main", "header", "footer", "tr",
         "li", "ul", "ol", "table", "blockquote", "pre", "form", "nav",
         "aside", "h1", "h2", "h3", "h4", "h5", "h6", "figure", "dl", "dd", "dt"}


def render_text(root, base_url=""):
    out, links = [], []

    def emit(s):
        out.append(s)

    def rec(node):
        if isinstance(node, str):
            emit(node)
            return
        tag = node.tag
        href = node.get("href")
        if tag == "a" and href and not href.lower().startswith("javascript:"):
            import urllib.parse as up
            absu = up.urljoin(base_url, href)
            start = len(out)
            for ch in node.children:
                rec(ch)
            text = "".join(out[start:]).strip()
            del out[start:]
            if text:
                n = len(links) + 1
                links.append((text[:80], absu))
                emit("%s[%d]" % (text, n))
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            emit("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in BLOCK:
            emit("\n\n" if tag != "li" else "\n  * ")
        elif tag == "br":
            emit("\n")
        elif tag == "img":
            alt = node.get("alt").strip()
            emit("[img%s]" % (": " + alt[:40] if alt else ""))
            return
        elif tag == "hr":
            emit("\n" + "-" * 40 + "\n")
            return
        elif tag in ("td", "th"):
            emit(" | ")
        for ch in node.children:
            rec(ch)

    for ch in root.children:
        rec(ch)
    text = "".join(out)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), links


def extract_title(root):
    t = select_one(root, "title")
    if t and t.text_content():
        return t.text_content()
    h1 = select_one(root, "h1")
    return h1.text_content()[:120] if h1 else ""
