"""HTML -> Markdown, stdlib only (html.parser). Zero-dependency.

Dipakai Kancil.markdown(): halaman JS-rendered -> markdown bersih buat
agent, bukan HTML mentah. Bukan full CommonMark — cukup buat dibaca
model: headings, paragraphs, links, images, lists, code, quotes, tables,
bold/italic. Tag non-konten (script/style/nav/header/footer/aside/iframe/
noscript) di-skip.
"""

from html.parser import HTMLParser

SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside",
             "iframe", "noscript", "svg", "form", "head"}


class _MD(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skip = 0
        self.in_pre = False
        self.link = None
        self.li_depth = 0
        self.table = None  # list of rows
        self.row = None
        self.cell = None  # current cell text parts
        # chars after which glued text needs no separating space
        self._nosp = " \n\t([>"

    def _w(self, s):
        self.out.append(s)

    def _nl(self, n=2):
        # collapse trailing whitespace, ensure n newlines max
        while self.out and self.out[-1].strip() == "":
            self.out.pop()
        self.out.append("\n" * n)

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        a = dict(attrs)
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._nl(2)
            self._w("#" * int(tag[1]) + " ")
        elif tag == "p":
            self._nl(2)
        elif tag == "br":
            self._w("\n")
        elif tag == "hr":
            self._nl(2)
            self._w("---")
            self._nl(2)
        elif tag == "a":
            self._w("[")
            self.link = a.get("href", "")
        elif tag == "img":
            alt = a.get("alt", "")
            src = a.get("src", "")
            self._nl(2)
            self._w("![%s](%s)" % (alt, src))
            self._nl(2)
        elif tag in ("ul", "ol"):
            self._nl(1)
            self.li_depth += 1
        elif tag == "li":
            self._w("\n" + "  " * (self.li_depth - 1) + "- ")
        elif tag in ("strong", "b"):
            self._w("**")
        elif tag in ("em", "i"):
            self._w("*")
        elif tag == "code":
            if not self.in_pre:
                self._w("`")
        elif tag == "pre":
            self._nl(2)
            self._w("```\n")
            self.in_pre = True
        elif tag == "blockquote":
            self._nl(1)
            self._w("> ")
        elif tag == "table":
            self.table = []
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p"):
            self._nl(2)
        elif tag == "a":
            if self.link:
                self._w("](%s)" % self.link)
            else:
                self._w("]")
            self.link = None
        elif tag in ("ul", "ol"):
            self.li_depth = max(0, self.li_depth - 1)
            self._nl(1)
        elif tag in ("strong", "b"):
            self._w("**")
        elif tag in ("em", "i"):
            self._w("*")
        elif tag == "code":
            if not self.in_pre:
                self._w("`")
        elif tag == "pre":
            self._w("\n```")
            self._nl(2)
            self.in_pre = False
        elif tag == "table":
            self._render_table()
            self.table = None
            self._nl(2)
        elif tag == "tr":
            if self.table is not None and self.row is not None:
                self.table.append(self.row)
            self.row = None
        elif tag in ("td", "th"):
            if self.row is not None:
                self.row.append(" ".join(self.cell or []))
            self.cell = None

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_pre:
            self._w(data)
            return
        leading = data[:1].isspace()
        trailing = data[-1:].isspace()
        text = " ".join(data.split())
        if not text:
            return
        if self.table is not None and self.cell is not None:
            # inside a table cell: accumulate, don't emit
            self.cell.append(text)
            return
        if leading and self.out and self.out[-1] \
                and self.out[-1][-1] not in self._nosp:
            self._w(" ")
        self._w(text)
        if trailing:
            self._w(" ")

    def _render_table(self):
        if not self.table:
            return
        self._nl(2)
        for i, row in enumerate(self.table[:30]):
            cells = [c.replace("|", "\\|") for c in row]
            self._w("| " + " | ".join(cells) + " |\n")
            if i == 0:
                self._w("| " + " | ".join(["---"] * len(cells)) + " |\n")


def html_to_markdown(html, max_chars=60000):
    """Convert HTML to readable Markdown. Returns (markdown, stats)."""
    p = _MD()
    try:
        p.feed(html or "")
    except Exception:
        pass
    md = "".join(p.out)
    # tidy: max 2 consecutive newlines
    lines = []
    blank = 0
    for ln in md.split("\n"):
        if ln.strip():
            blank = 0
            lines.append(ln.rstrip())
        else:
            blank += 1
            if blank <= 1:
                lines.append("")
    md = "\n".join(lines).strip()
    if len(md) > max_chars:
        md = md[:max_chars] + "\n\n...[truncated]"
    return md, {"chars": len(md), "truncated": len(md) >= max_chars}
