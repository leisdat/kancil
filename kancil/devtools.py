"""Kancil devtools: scraper, reader/extract, helpers."""
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request

from .dom import select, select_one


def parse_field_spec(spec):
    """'a@href' -> ('a', 'href'); '.title' -> ('.title', None[text])."""
    if "@" in spec:
        sel, attr = spec.rsplit("@", 1)
        return sel.strip() or "*", attr.strip()
    return spec.strip(), None


def scrape_items(root, base_url, item_selector, fields):
    """fields: {name: 'selector' | 'selector@attr'} -> list of dicts."""
    out = []
    for item in select(root, item_selector):
        row = {}
        for name, spec in fields.items():
            sel, attr = parse_field_spec(spec)
            el = select_one(item, sel) if sel != "*" else item
            if not el:
                row[name] = ""
                continue
            if attr:
                v = el.get(attr, "")
                if attr in ("href", "src") and v:
                    v = urllib.parse.urljoin(base_url, v)
                row[name] = v
            else:
                row[name] = el.text_content()[:500]
        out.append(row)
    return out


def _text_len(node):
    return len(node.text_content())


def _article_item(a, base_url):
    """Split an <article> into title / url / links / image / meta.

    Generic DOM patterns (no per-site rules): the title comes from the
    heading link (or the longest link text), other links are listed
    separately, and leftover text becomes meta. This avoids the classic
    auto-scrape mush like "1Overgeared NewFantasi · 74,276 viewsChapter 341".
    """
    h = select_one(a, "h1, h2, h3, h4")
    h_link = select_one(h, "a[href]") if h is not None else None
    links = []
    seen_u = set()
    for l in select(a, "a[href]"):
        href = (l.get("href") or "").strip()
        if not href or href.lower().startswith(("javascript:", "#")):
            continue
        u = urllib.parse.urljoin(base_url, href)
        t = re.sub(r"\s+", " ", l.text_content() or "").strip()
        if u not in seen_u:
            seen_u.add(u)
            links.append((t, u))
    img = select_one(a, "img")
    image = ""
    if img is not None:
        image = urllib.parse.urljoin(
            base_url, img.get("data-src") or img.get("src") or "")
    if h_link is not None and (h_link.text_content() or "").strip():
        title = re.sub(r"\s+", " ", h_link.text_content()).strip()
        url = urllib.parse.urljoin(base_url, h_link.get("href"))
    elif h is not None and (h.text_content() or "").strip():
        title = re.sub(r"\s+", " ", h.text_content()).strip()
        url = links[0][1] if links else ""
    elif links:
        title, url = max(links, key=lambda x: len(x[0]))
    else:
        title, url = "", ""
    # meta: item text minus title and link texts
    meta = a.text_content() or ""
    for chunk in [title] + [t for t, _ in links]:
        if chunk:
            meta = meta.replace(chunk, " ", 1)
    meta = re.sub(r"\s+", " ", meta).strip()
    meta = re.sub(r"^\d{1,4}\s+", "", meta)  # leading rank/badge number
    return {"title": title[:160], "url": url,
            "links": [{"text": t[:80], "url": u} for t, u in links[:10]
                      if u != url and t],
            "image": image, "meta": meta[:300],
            "text": re.sub(r"\s+", " ", a.text_content() or "").strip()[:600]}


def scrape_auto(root, base_url):
    """Detect common structures: articles, products/cards, tables, images, links."""
    found = {"articles": [], "products": [], "tables": [], "images": [], "links": []}
    # articles
    for a in select(root, "article"):
        found["articles"].append(_article_item(a, base_url))
    # product/card-like: repeated divs with img + price-ish text
    for sel in (".product", ".card", ".item", "[class*=card]", "[class*=product]"):
        try:
            cards = select(root, sel)
        except Exception:
            continue
        if len(cards) >= 2:
            for c in cards[:50]:
                img = select_one(c, "img")
                link = select_one(c, "a[href]")
                price = ""
                m = re.search(r"([$€£¥Rp]\s?[\d.,]+|[\d.,]+\s?(USD|EUR|IDR))", c.text_content())
                if m:
                    price = m.group(0)
                found["products"].append({
                    "title": (select_one(c, "h1, h2, h3, h4") or c).text_content()[:120],
                    "url": urllib.parse.urljoin(base_url, link.get("href")) if link else "",
                    "image": urllib.parse.urljoin(base_url, img.get("src")) if img and img.get("src") else "",
                    "price": price,
                })
            break
    # tables
    for t in select(root, "table"):
        rows = []
        for tr in select(t, "tr"):
            rows.append([c.text_content()[:100] for c in select(tr, "th, td")])
        if rows and any(any(cells) for cells in rows):
            found["tables"].append({"rows": rows[:50], "n_rows": len(rows)})
    # images
    for img in select(root, "img"):
        src = img.get("src")
        if src:
            found["images"].append({
                "src": urllib.parse.urljoin(base_url, src),
                "alt": img.get("alt")[:80]})
    # links
    seen = set()
    for a in select(root, "a[href]"):
        href = a.get("href")
        if href.startswith(("javascript:", "#")):
            continue
        u = urllib.parse.urljoin(base_url, href)
        if u not in seen:
            seen.add(u)
            found["links"].append({"text": a.text_content()[:80], "url": u})
    # drop empties
    return {k: v for k, v in found.items() if v}


def extract_reader(page):
    """Token-efficient reader output for LLMs."""
    dom = page.dom
    headings = [{"level": h.tag, "text": h.text_content()[:120]}
                for h in select(dom, "h1, h2, h3")]
    best, best_score = None, 0
    for cand in select(dom, "article, main, [role=main], div, section"):
        score = sum(len(p.text_content()) for p in select(cand, "p"))
        if score > best_score:
            best, best_score = cand, score
    article = best.text_content() if best is not None and best_score > 200 else page.text
    tables = []
    for t in select(dom, "table"):
        rows = []
        for tr in select(t, "tr"):
            rows.append([c.text_content()[:100] for c in select(tr, "th, td")])
        if rows:
            tables.append(rows[:30])
    meta = {}
    for m in select(dom, "meta"):
        k = m.get("name") or m.get("property")
        if k and m.get("content"):
            meta[k] = m.get("content")[:200]
    images = []
    for img in select(dom, "img"):
        src = img.get("src")
        if src:
            images.append({"src": urllib.parse.urljoin(page.url, src),
                           "alt": img.get("alt")[:80]})
    return {
        "title": page.title,
        "url": page.url,
        "headings": headings[:30],
        "article": article[:8000],
        "links": [{"text": t, "url": u} for t, u in page.links[:100]],
        "images": images[:50],
        "tables": tables[:5],
        "meta": meta,
    }


# ---------------- pagination ----------------
_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                    "utm_content", "gclid", "fbclid", "msclkid", "_ga"}


def canonical_url(url):
    """Normalize for duplicate detection: lowercase host, sorted query,
    strip fragment + tracking params."""
    try:
        u = urllib.parse.urlparse(url)
        q = sorted((k, v) for k, v in urllib.parse.parse_qsl(u.query)
                   if k.lower() not in _TRACKING_PARAMS)
        return urllib.parse.urlunparse((
            u.scheme.lower(), u.netloc.lower(), u.path or "/",
            "", urllib.parse.urlencode(q), ""))
    except Exception:
        return url


def _item_key(item):
    return hashlib.md5(json.dumps(item, sort_keys=True, default=str).encode()
                       ).hexdigest()[:16]


_robots_cache = {}


def robots_allowed(url, ua_token="kancil"):
    """Minimal robots.txt check. Fail-open on fetch/parse errors."""
    try:
        u = urllib.parse.urlparse(url)
        host = u.netloc.lower()
        if host not in _robots_cache:
            _robots_cache[host] = _fetch_robots(u.scheme, host)
        disallows = _robots_cache[host]
        path = u.path or "/"
        return not any(path.startswith(d) for d in disallows if d)
    except Exception:
        return True


def _fetch_robots(scheme, host):
    disallows = []
    try:
        req = urllib.request.Request(
            "%s://%s/robots.txt" % (scheme, host),
            headers={"User-Agent": "kancil"})
        with urllib.request.urlopen(req, timeout=10) as r:
            if r.status != 200:
                return []
            txt = r.read(100000).decode("utf-8", errors="ignore")
        in_group, relevant = False, False
        for line in txt.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            k, v = [x.strip() for x in line.split(":", 1)]
            kl = k.lower()
            if kl == "user-agent":
                in_group = True
                relevant = v in ("*", "kancil")
            elif kl == "disallow" and in_group and relevant and v:
                disallows.append(v)
            elif kl not in ("disallow", "allow", "crawl-delay"):
                in_group, relevant = False, False
    except Exception:
        pass
    return disallows


def _page_number(url):
    """Extract current page number from ?page=N / &p=N / /page/N. None if absent."""
    try:
        u = urllib.parse.urlparse(url)
        for k, v in urllib.parse.parse_qsl(u.query):
            if k.lower() in ("page", "p", "pg") and v.isdigit():
                return int(v)
        m = re.search(r"/page/(\d+)", u.path)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def fetch_sitemap_urls(start_url, max_urls=5000, respect_robots=True):
    """Discover page URLs from sitemap.xml.

    Sources: robots.txt "Sitemap:" lines, then /sitemap.xml fallback.
    Handles sitemapindex recursion (depth<=2) and .xml.gz. Pure sitemap
    protocol parsing, no per-site rules.

    Returns {"sitemaps": [...visited], "urls": [...], "truncated": bool}.
    """
    import gzip
    import xml.etree.ElementTree as ET
    u = urllib.parse.urlparse(start_url)
    origin = "%s://%s" % (u.scheme or "https", u.netloc)
    candidates = []
    try:
        req = urllib.request.Request(
            origin + "/robots.txt", headers={"User-Agent": "kancil"})
        with urllib.request.urlopen(req, timeout=10) as r:
            txt = r.read(200000).decode("utf-8", errors="ignore")
        for line in txt.splitlines():
            line = line.split("#", 1)[0].strip()
            if line.lower().startswith("sitemap:"):
                sm = line.split(":", 1)[1].strip()
                if sm and sm not in candidates:
                    candidates.append(sm)
    except Exception:
        pass
    if origin + "/sitemap.xml" not in candidates:
        candidates.append(origin + "/sitemap.xml")

    seen_sm, sitemaps, urls = set(), [], []
    truncated = False

    def fetch_xml(sm_url, depth):
        nonlocal truncated
        if depth > 2 or sm_url in seen_sm:
            return
        if len(sitemaps) >= 50 or len(urls) >= max_urls:
            truncated = True
            return
        seen_sm.add(sm_url)
        if respect_robots and not robots_allowed(sm_url):
            return
        try:
            req = urllib.request.Request(
                sm_url, headers={"User-Agent": "kancil"})
            with urllib.request.urlopen(req, timeout=15) as r:
                if r.status != 200:
                    return
                data = r.read(3000000)
            if sm_url.endswith(".gz"):
                data = gzip.decompress(data)
            root = ET.fromstring(data)
        except Exception:
            return
        sitemaps.append(sm_url)
        children = list(root)
        locs = []
        for c in children:
            for loc in c.iter():
                if loc.tag.endswith("loc") and (loc.text or "").strip():
                    locs.append(loc.text.strip())
                    break
        is_index = any(c.tag.endswith("sitemap") for c in children)
        if is_index:
            for loc in locs:
                fetch_xml(loc, depth + 1)
        else:
            for loc in locs:
                if len(urls) >= max_urls:
                    truncated = True
                    break
                urls.append(loc)

    for cand in candidates:
        fetch_xml(cand, 0)
        if urls:
            break
    return {"sitemaps": sitemaps, "urls": urls, "truncated": truncated}


def find_numbered_next(dom, base_url, current_url):
    """Find link to the next numbered page (?page=N+1 etc)."""
    cur = _page_number(current_url) or 1
    for a in select(dom, "a[href]"):
        href = a.get("href", "")
        if not href or href.startswith(("#", "javascript:")):
            continue
        absu = urllib.parse.urljoin(base_url, href)
        n = _page_number(absu)
        if n == cur + 1:
            return absu
    # fallback: "next"/"»" text link
    for a in select(dom, "a[href]"):
        if (a.text_content() or "").strip().lower() in ("next", "»", "›", ">"):
            href = urllib.parse.urljoin(base_url, a.get("href", ""))
            if href and href != current_url:
                return href
    return None


def next_page_by_template(current_url):
    """Try incrementing ?page=N directly when no next link found."""
    n = _page_number(current_url)
    if n is None:
        return None
    u = urllib.parse.urlparse(current_url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query)]
    changed = False
    nq = []
    for k, v in q:
        if k.lower() in ("page", "p", "pg") and v == str(n):
            nq.append((k, str(n + 1)))
            changed = True
        else:
            nq.append((k, v))
    if changed:
        return urllib.parse.urlunparse(
            (u.scheme, u.netloc, u.path, u.params, urllib.parse.urlencode(nq), ""))
    m = re.search(r"/page/(\d+)", u.path)
    if m:
        path = u.path[:m.start()] + "/page/%d" % (n + 1) + u.path[m.end():]
        return urllib.parse.urlunparse(
            (u.scheme, u.netloc, path, u.params, u.query, ""))
    return None


def paginate_scrape(engine, start_url, selector=None, fields=None, auto=False,
                    max_pages=10, max_items=1000, next_selector=None,
                    delay=1.0, timeout=180, same_content_limit=3,
                    scroll_pages=0, respect_robots=True, url_list=None):
    """Paginated scrape with duplicate detection and hard limits.

    url_list: when given, iterate these URLs directly instead of following
    next-page links (e.g. URLs discovered from a sitemap). max_pages caps
    how many are fetched.

    Returns {"pages_crawled", "items", "duplicates", "failed_pages",
             "stopped_reason", "data"}.
    """
    deadline = time.time() + timeout
    seen_urls, seen_items = set(), set()
    data, duplicates, failed = [], 0, 0
    pages_crawled, same_content = 0, 0
    stopped = "done"
    url_iter = iter(url_list) if url_list else None
    if url_iter is not None:
        url, via = next(url_iter, None), "list"  # list mode: only the list
    else:
        url, via = start_url, "start"  # how the current url was discovered

    def page_items(p):
        if not p.dom:
            return []
        if auto:
            d = scrape_auto(p.dom, p.url)
            items = []
            for cat, rows in d.items():
                if isinstance(rows, list):
                    for r in rows:
                        items.append({"_category": cat, **r} if isinstance(r, dict)
                                     else {"_category": cat, "value": r})
            return items
        if selector and fields:
            return scrape_items(p.dom, p.url, selector, fields)
        return []

    while url:
        if time.time() > deadline:
            stopped = "timeout"
            break
        if pages_crawled >= max_pages:
            stopped = "max-pages"
            break
        if len(data) >= max_items:
            stopped = "max-items"
            break
        canon = canonical_url(url)
        if canon in seen_urls:
            stopped = "duplicate-url"
            break
        if respect_robots and not robots_allowed(url):
            stopped = "robots-disallowed"
            break
        seen_urls.add(canon)

        # infinite scroll (playwright only): load more before extracting
        if scroll_pages and getattr(engine, "capabilities", {}).get("javascript"):
            for _ in range(scroll_pages):
                try:
                    engine.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    engine.wait(ms=1200)
                except Exception:
                    break

        r = engine.open(url)
        if not r.get("success"):
            if via == "template":
                # guessed page doesn't exist -> end of pagination, not an error
                stopped = "no-more-pages"
                break
            failed += 1
            if url_iter is not None:
                url, via = next(url_iter, None), "list"  # advance before continue
                continue  # list mode: skip bad URL, keep going
            stopped = "open-failed"
            break
        p = engine.page
        pages_crawled += 1

        new_this_page = 0
        for item in page_items(p):
            key = _item_key(item)
            if key in seen_items:
                duplicates += 1
                continue
            seen_items.add(key)
            data.append(item)
            new_this_page += 1
            if len(data) >= max_items:
                break

        if new_this_page == 0:
            same_content += 1
            if same_content >= same_content_limit:
                stopped = "same-content"
                break
        else:
            same_content = 0

        # next page?
        if url_iter is not None:
            url, via = next(url_iter, None), "list"
        else:
            nxt, via = None, "none"
            if next_selector and p.dom:
                els = select(p.dom, next_selector)
                if els and els[0].get("href"):
                    nxt = urllib.parse.urljoin(p.url, els[0].get("href"))
                    via = "selector"
            if not nxt and p.dom:
                nxt = find_numbered_next(p.dom, p.url, p.url)
                if nxt:
                    via = "numbered"
            if not nxt:
                nxt = next_page_by_template(p.url if hasattr(p, "url") else url)
                if nxt:
                    via = "template"
            url = nxt
        if url and delay:
            time.sleep(delay)

    return {"pages_crawled": pages_crawled, "items": len(data),
            "duplicates": duplicates, "failed_pages": failed,
            "stopped_reason": stopped, "data": data}


# ---------------- structured data extraction ----------------
# JSON-LD blocks + OpenGraph/Twitter meta tags: the machine-readable data
# sites publish for crawlers. Often cleaner than scraping visible text.


def extract_structured(dom, raw_html=None):
    """Extract JSON-LD, OpenGraph, Twitter Card and basic meta tags.

    JSON-LD comes from raw HTML (the DOM parser skips <script> content);
    meta tags come from the DOM. Pure patterns, no per-site rules.

    Returns {"json_ld": [...], "opengraph": {...}, "twitter": {...},
             "meta": {...}}.
    """
    out = {"json_ld": [], "opengraph": {}, "twitter": {}, "meta": {}}
    if raw_html:
        for b in extract_embedded_json(raw_html):
            if b.get("source") != "ld+json":
                continue
            data = b.get("data")
            if isinstance(data, dict) and isinstance(data.get("@graph"), list):
                out["json_ld"].extend(data["@graph"])
            elif isinstance(data, list):
                out["json_ld"].extend(data)
            elif data is not None:
                out["json_ld"].append(data)
    if dom is None:
        return out
    for el in select(dom, "meta"):
        content = el.get("content")
        if content is None:
            continue
        prop = el.get("property") or ""
        name = el.get("name") or ""
        if prop.startswith("og:"):
            out["opengraph"][prop[3:]] = content
        elif name.startswith("twitter:"):
            out["twitter"][name[8:]] = content
        elif name in ("description", "keywords", "author"):
            out["meta"][name] = content
    return out


def scrape_url_list(urls, engine_factory, selector=None, fields=None, auto=False,
                    max_items=1000, workers=4, delay=0.0, timeout=180,
                    respect_robots=True):
    """Scrape a known URL list concurrently.

    Each worker gets its own engine from engine_factory() (thread-safety by
    isolation; each has its own cookie jar/pool). Shared dedup via lock.
    delay is applied per-worker between requests (politeness). workers is
    capped at 8 to avoid hammering small sites.

    Returns like paginate_scrape: {"pages_crawled", "items", "duplicates",
    "failed_pages", "stopped_reason", "data"}.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor
    deadline = time.time() + timeout
    workers = max(1, min(int(workers), 8))
    seen_items, data = set(), []
    duplicates, failed, pages = 0, 0, 0
    lock = threading.Lock()

    def page_items(p):
        if not p.dom:
            return []
        if auto:
            d = scrape_auto(p.dom, p.url)
            items = []
            for cat, rows in d.items():
                if isinstance(rows, list):
                    for r in rows:
                        items.append({"_category": cat, **r}
                                     if isinstance(r, dict)
                                     else {"_category": cat, "value": r})
            return items
        if selector and fields:
            return scrape_items(p.dom, p.url, selector, fields)
        return []

    def one(url):
        nonlocal duplicates, failed, pages
        if time.time() > deadline:
            return
        with lock:
            if len(data) >= max_items:
                return
        if respect_robots and not robots_allowed(url):
            return
        if delay:
            time.sleep(delay)
        eng = None
        try:
            eng = engine_factory()
            r = eng.open(url)
            if not r.get("success"):
                with lock:
                    failed += 1
                return
            items = page_items(eng.page)
            with lock:
                pages += 1
                for item in items:
                    key = _item_key(item)
                    if key in seen_items:
                        duplicates += 1
                        continue
                    seen_items.add(key)
                    if len(data) >= max_items:
                        break
                    data.append(item)
        except Exception:
            with lock:
                failed += 1
        finally:
            try:
                if eng is not None:
                    eng.close()
            except Exception:
                pass

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, list(urls)))
    stopped = "done"
    if time.time() > deadline:
        stopped = "timeout"
    elif len(data) >= max_items:
        stopped = "max-items"
    return {"pages_crawled": pages, "items": len(data), "duplicates": duplicates,
            "failed_pages": failed, "stopped_reason": stopped, "data": data}


# ---------------- embedded JSON extraction ----------------
# Many sites inject data as JSON blobs in raw HTML (YouTube ytInitialData,
# Next.js __NEXT_DATA__, Nuxt, ld+json, ...). The static engine can't run JS,
# but it CAN read these blobs directly.

def _decode_html_text(raw):
    if isinstance(raw, bytes):
        for enc in ("utf-8", "latin-1"):
            try:
                return raw.decode(enc)
            except Exception:
                continue
        return raw.decode("utf-8", errors="replace")
    return raw or ""


def extract_embedded_json(html):
    """Extract JSON blobs embedded in raw HTML.

    Returns [{"source": str, "data": parsed}] — never raises; oversized
    blobs (>2MB) are skipped to bound memory.
    """
    html = _decode_html_text(html)
    out = []

    def _add(source, data):
        if isinstance(data, (dict, list)):
            out.append({"source": source, "data": data})

    # 1. <script type="application/json"> / ld+json / id=__NEXT_DATA__
    for m in re.finditer(
            r'<script\b[^>]*>(.*?)</script\s*>', html,
            re.I | re.S):
        tag = m.group(0)[:400].lower()
        body = m.group(1).strip()
        if len(body) > 2_000_000 or not body:
            continue
        src = None
        if '__next_data__' in tag:
            src = "__NEXT_DATA__"
        elif 'type="application/ld+json"' in tag or "type='application/ld+json'" in tag:
            src = "ld+json"
        elif 'type="application/json"' in tag or "type='application/json'" in tag:
            src = "application/json"
        if not src:
            continue
        try:
            _add(src, json.loads(body))
        except Exception:
            pass

    # 2. JS variable assignments: ytInitialData, __NUXT__, etc.
    for var in ("ytInitialData", "__NUXT__", "__NEXT_DATA__", "window.___r"):
        # 2a. quoted JS string literal: var = '...';  (escapes like \x7b)
        for m in re.finditer(
                r'%s\s*=\s*(\'|\")(.*?)\1\s*;' % re.escape(var),
                html, re.S):
            body = m.group(2)
            if len(body) > 2_000_000:
                continue
            try:
                # JS string literal with escapes (\x7b = {, \")
                data = json.loads(body.encode().decode("unicode_escape"))
                _add(var, data)
            except Exception:
                continue
        # 2b. raw JSON object: var = {...};
        for m in re.finditer(r'%s\s*=\s*\{' % re.escape(var), html):
            start = m.end() - 1  # at the opening brace
            if start > len(html) - 2:
                continue
            depth, instr, esc = 0, None, False
            end = None
            for i in range(start, min(len(html), start + 3_000_000)):
                ch = html[i]
                if instr:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == instr:
                        instr = None
                else:
                    if ch in ("'", '"'):
                        instr = ch
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
            if end is None:
                continue
            try:
                _add(var, json.loads(html[start:end]))
            except Exception:
                continue
    return out


def _walk(obj, pred, out, _depth=0):
    if _depth > 40:
        return
    if isinstance(obj, dict):
        if pred(obj):
            out.append(obj)
        for v in obj.values():
            _walk(v, pred, out, _depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _walk(v, pred, out, _depth + 1)


def _runs_text(node):
    try:
        runs = node.get("runs") or []
        return "".join(r.get("text", "") for r in runs).strip()
    except Exception:
        return ""


def extract_yt_search(html):
    """Video results from a YouTube search page via ytInitialData.

    Returns [{"videoId", "title", "url", "channel"}] deduped by videoId.
    """
    videos, seen = [], set()
    for blob in extract_embedded_json(html):
        if blob["source"] != "ytInitialData":
            continue
        found = []
        _walk(blob["data"],
              lambda d: isinstance(d.get("videoRenderer"), dict), found)
        for d in found:
            vr = d["videoRenderer"]
            vid = vr.get("videoId")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            title = _runs_text(vr.get("title", {}))
            chan = _runs_text(
                (vr.get("ownerText") or vr.get("longBylineText") or {}))
            videos.append({
                "videoId": vid,
                "title": title or "(no title)",
                "url": "https://www.youtube.com/watch?v=" + vid,
                "channel": chan,
            })
    return videos


def extract_yt_video(html):
    """Metadata of a YouTube watch page: og: tags (title/desc/thumb/video)."""
    out = {}
    for m in re.finditer(
            r'<meta\s+(?:property|name)="([^"]+)"\s+content="([^"]*)"',
            html, re.I):
        prop, val = m.group(1).lower(), m.group(2)
        if prop.startswith("og:"):
            out[prop[3:]] = val
    return out
