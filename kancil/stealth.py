"""Stealth module for Kancil: browser impersonation.

Two layers, both optional and dependency-light:

1. **Static engine** -- TLS/JA3 fingerprint impersonation via ``curl_cffi``.
   ``ImpersonatedOpener`` exposes the same interface ``StaticEngine.fetch()``
   already uses (``opener.open(req, timeout=...)`` -> response with
   ``read()/headers/status/geturl()``), so netlog, cache, cookies and retry
   logic keep working unchanged. When ``curl_cffi`` is not installed the
   engine silently falls back to the default urllib transport.

2. **WebView engine** -- ``STEALTH_JS`` anti-detect snippet injected with
   ``apply_stealth(engine)`` through the existing ``evaluate()`` API.
   Injection-based spoofing covers the common fingerprinting vectors but
   cannot match engine-level patching (Camoufox-style): a sophisticated
   checker can still detect it. Re-apply after every navigation, because a
   page load wipes injected JS.

Nothing here phones home or changes default behaviour: stealth is opt-in.
"""

import io
import json
import urllib.error

try:
    from curl_cffi import requests as _curl_requests
    HAVE_CURL_CFFI = True
except ImportError:  # pragma: no cover - optional dependency
    _curl_requests = None
    HAVE_CURL_CFFI = False

# ---------------------------------------------------------------------------
# Browser profiles: TLS impersonation target + consistent UA/headers.
# ---------------------------------------------------------------------------

BROWSER_PROFILES = {
    "chrome": {
        "impersonate": "chrome124",
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "headers": {
            "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        },
    },
    "chrome_android": {
        "impersonate": "chrome131_android",
        "ua": ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"),
        "headers": {
            "Sec-Ch-Ua": '"Chromium";v="131", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?1",
            "Sec-Ch-Ua-Platform": '"Android"',
        },
    },
    "firefox": {
        "impersonate": "firefox133",
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) "
               "Gecko/20100101 Firefox/133.0"),
        "headers": {},
    },
    "safari": {
        "impersonate": "safari180",
        "ua": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
               "Safari/605.1.15"),
        "headers": {},
    },
    "edge": {
        "impersonate": "edge101",
        "ua": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/101.0.4951.54 Safari/537.36 "
               "Edg/101.0.1210.39"),
        "headers": {
            "Sec-Ch-Ua": '"Chromium";v="101", "Microsoft Edge";v="101", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        },
    },
    "tor": {
        "impersonate": "tor145",
        "ua": ("Mozilla/5.0 (Windows NT 10.0; rv:145.0) Gecko/20100101 "
               "Firefox/145.0"),
        "headers": {},
        "note": "pair with a Tor SOCKS proxy, e.g. socks5://127.0.0.1:9050",
    },
}

DEFAULT_PROFILE = "chrome_android"


def list_profiles():
    """Return available impersonation profile names."""
    return sorted(BROWSER_PROFILES)


def stealth_session(profile=DEFAULT_PROFILE, proxy=None):
    """Standalone curl_cffi session with the given browser profile.

    Useful for one-off impersonated requests outside the engine.
    Raises RuntimeError when curl_cffi is not installed.
    """
    if not HAVE_CURL_CFFI:
        raise RuntimeError("curl_cffi is not installed (pip install curl_cffi)")
    if profile not in BROWSER_PROFILES:
        raise ValueError("unknown profile %r (choose from %s)"
                         % (profile, ", ".join(list_profiles())))
    p = BROWSER_PROFILES[profile]
    s = _curl_requests.Session()
    s.headers.update({"User-Agent": p["ua"]})
    s.headers.update(p.get("headers", {}))
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    # impersonate is per-request in curl_cffi; wrapping request() so every
    # call on this session applies the profile's TLS fingerprint.
    _target = p["impersonate"]
    _orig = s.request

    def _impersonated_request(method, url, **kw):
        kw.setdefault("impersonate", _target)
        return _orig(method, url, **kw)

    s.request = _impersonated_request  # type: ignore[method-assign]
    return s


# ---------------------------------------------------------------------------
# urllib-compatible adapter over curl_cffi (static engine transport)
# ---------------------------------------------------------------------------

class _CurlHeaders:
    """Expose the subset of the email.message.Message API that
    StaticEngine.fetch() uses: get / get_all / get_content_type / items."""

    def __init__(self, headers):
        self._h = headers

    def get(self, key, default=None):
        # curl_cffi already decodes the body, so never claim an encoding:
        # fetch() would otherwise try to decompress plain bytes.
        if key.lower() == "content-encoding":
            return default
        return self._h.get(key, default)

    def get_all(self, key):
        try:
            vals = self._h.getlist(key)
        except AttributeError:
            v = self._h.get(key)
            vals = [v] if v is not None else []
        return list(vals)

    def get_content_type(self):
        ct = self.get("Content-Type", "") or ""
        return ct.split(";")[0].strip()

    def items(self):
        return [(k, v) for k, v in self._h.items()
                if k.lower() != "content-encoding"]


class _CurlResponse:
    """Minimal urllib-response interface over a curl_cffi response."""

    def __init__(self, resp):
        self._r = resp
        self._headers = _CurlHeaders(resp.headers)

    def read(self, n=-1):
        data = self._r.content or b""
        return data if n is None or n < 0 else data[:n]

    @property
    def status(self):
        return self._r.status_code

    def geturl(self):
        return str(self._r.url)

    @property
    def headers(self):
        return self._headers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ImpersonatedOpener:
    """Drop-in replacement for the urllib opener used by StaticEngine.

    Only implements ``open(req, timeout=...)``. Cookies are synced both
    ways with the engine's http.cookiejar so cookie files keep working.
    HTTP error statuses are raised as urllib.error.HTTPError so fetch()'s
    existing error/cache branches behave identically.
    """

    def __init__(self, jar, profile=DEFAULT_PROFILE, proxy=None, timeout=25):
        if not HAVE_CURL_CFFI:
            raise RuntimeError("curl_cffi is not installed "
                               "(pip install curl_cffi)")
        if profile not in BROWSER_PROFILES:
            raise ValueError("unknown profile %r (choose from %s)"
                             % (profile, ", ".join(list_profiles())))
        self.jar = jar
        self.profile_name = profile
        self.profile = BROWSER_PROFILES[profile]
        self.proxy = proxy
        self.timeout = timeout
        self.session = _curl_requests.Session()

    # -- cookie sync: engine jar <-> curl session -------------------------
    def _cookies_to_session(self):
        for ck in self.jar:
            try:
                self.session.cookies.set(
                    ck.name, ck.value or "",
                    domain=ck.domain or "", path=ck.path or "/",
                    secure=bool(ck.secure))
            except Exception:
                pass

    def _cookies_from_session(self):
        jar = getattr(self.session.cookies, "jar", None)
        if jar is None:
            return
        for ck in jar:
            try:
                self.jar.set_cookie(ck)
            except Exception:
                pass

    # -- transport ---------------------------------------------------------
    def open(self, req, timeout=None):
        self._cookies_to_session()
        headers = dict(req.header_items())
        # consistent fingerprint: profile UA wins over the engine default
        headers["User-Agent"] = self.profile["ua"]
        headers.update(self.profile.get("headers", {}))
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        try:
            resp = self.session.request(
                req.get_method(), req.full_url,
                data=req.data, headers=headers,
                impersonate=self.profile["impersonate"],
                timeout=timeout or self.timeout,
                proxies=proxies)
        except Exception:
            # connection-level failures propagate as generic exceptions so
            # fetch()'s retry/backoff branch handles them (same as URLError)
            raise
        self._cookies_from_session()
        if resp.status_code == 304 or resp.status_code >= 400:
            # behave exactly like urllib: fetch() has dedicated branches
            raise urllib.error.HTTPError(
                req.full_url, resp.status_code,
                "HTTP %d" % resp.status_code,
                _CurlHeaders(resp.headers), io.BytesIO(resp.content or b""))
        return _CurlResponse(resp)


# ---------------------------------------------------------------------------
# WebView anti-detect JS
# ---------------------------------------------------------------------------

STEALTH_JS = """(() => {
'use strict';
/* Kancil stealth v1 - injection-based anti-detect. Covers common
   fingerprinting vectors; NOT engine-level patching. Re-apply after
   every navigation. */
if (window.__kancil_stealth__) return 'already-applied';
window.__kancil_stealth__ = 1;

const mask = (fn, name) => {
  try {
    Object.defineProperty(fn, 'toString', {
      value: function () { return 'function ' + (name || fn.name || '') + '() { [native code] }'; },
      configurable: true, writable: true
    });
  } catch (e) {}
};
const def = (obj, prop, value) => {
  try { Object.defineProperty(obj, prop, { get: () => value, configurable: true }); } catch (e) {}
};

/* 1. automation flag */
def(navigator, 'webdriver', false);

/* 2. plugins / mimeTypes */
const _mkMime = (t, s, d) => ({ type: t, suffixes: s, description: d });
const _mkPlugin = (name, desc, file, mimes) => {
  const p = { name, description: desc, filename: file, length: mimes.length };
  mimes.forEach((m, i) => { p[i] = m; });
  p.item = function (i) { return this[i] || null; };
  p.namedItem = function (n) {
    for (let i = 0; i < this.length; i++) if (this[i].type === n) return this[i];
    return null;
  };
  return p;
};
const _plugins = [
  _mkPlugin('Chrome PDF Plugin', 'Portable Document Format', 'internal-pdf-viewer',
    [_mkMime('application/pdf', 'pdf', 'Portable Document Format')]),
  _mkPlugin('Chrome PDF Viewer', '', 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
    [_mkMime('application/pdf', 'pdf', '')]),
  _mkPlugin('Native Client', '', 'internal-nacl-plugin',
    [_mkMime('application/x-nacl', '', 'Native Client Executable'),
     _mkMime('application/x-pnacl', '', 'Portable Native Client Executable')])
];
def(navigator, 'plugins', _plugins);
def(navigator, 'mimeTypes', (() => {
  const m = [];
  _plugins.forEach(p => { for (let i = 0; i < p.length; i++) m.push(p[i]); });
  m.item = function (i) { return this[i] || null; };
  m.namedItem = function (n) {
    for (let i = 0; i < this.length; i++) if (this[i].type === n) return this[i];
    return null;
  };
  return m;
})());

/* 3. locale / hardware */
def(navigator, 'language', 'en-US');
def(navigator, 'languages', ['en-US', 'en']);
def(navigator, 'platform', 'Win32');
def(navigator, 'hardwareConcurrency', 8);
def(navigator, 'deviceMemory', 8);
def(navigator, 'maxTouchPoints', 0);

/* 4. Client Hints */
try {
  const brands = [
    { brand: 'Chromium', version: '124' },
    { brand: 'Google Chrome', version: '124' },
    { brand: 'Not-A.Brand', version: '99' }
  ];
  const uad = {
    brands, mobile: false, platform: 'Windows',
    getHighEntropyValues: (hints) => Promise.resolve({
      brands, mobile: false, platform: 'Windows', architecture: 'x86',
      bitness: '64', model: '', platformVersion: '15.0.0',
      uaFullVersion: '124.0.0.0', fullVersionList: brands
    })
  };
  mask(uad.getHighEntropyValues, 'getHighEntropyValues');
  def(navigator, 'userAgentData', uad);
} catch (e) {}

/* 5. window.chrome */
def(window, 'chrome', {
  runtime: {},
  loadTimes: function () {}, csi: function () {},
  app: { isInstalled: false }
});
mask(window.chrome.loadTimes, 'loadTimes');
mask(window.chrome.csi, 'csi');

/* 6. canvas fingerprint noise */
try {
  const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
  const _toBlob = HTMLCanvasElement.prototype.toBlob;
  const _getImageData = CanvasRenderingContext2D.prototype.getImageData;
  const _noise = (canvas) => {
    try {
      const ctx = canvas.getContext('2d');
      if (!ctx || canvas.width === 0 || canvas.height === 0) return;
      const d = _getImageData.call(ctx, 0, 0, canvas.width, canvas.height);
      const px = d.data;
      for (let i = 0; i < px.length; i += 401) px[i] ^= 1;  // 1-bit flip, invisible
      ctx.putImageData(d, 0, 0);
    } catch (e) { /* tainted canvas: leave alone */ }
  };
  HTMLCanvasElement.prototype.toDataURL = function (...a) { _noise(this); return _toDataURL.apply(this, a); };
  HTMLCanvasElement.prototype.toBlob = function (...a) { _noise(this); return _toBlob.apply(this, a); };
  mask(HTMLCanvasElement.prototype.toDataURL, 'toDataURL');
  mask(HTMLCanvasElement.prototype.toBlob, 'toBlob');
} catch (e) {}

/* 7. WebGL vendor/renderer */
try {
  const _getParam = WebGLRenderingContext.prototype.getParameter;
  const _spoofGL = function (p) {
    if (p === 37445) return 'Intel Inc.';
    if (p === 37446) return 'Intel Iris OpenGL Engine';
    return _getParam.call(this, p);
  };
  WebGLRenderingContext.prototype.getParameter = _spoofGL;
  mask(WebGLRenderingContext.prototype.getParameter, 'getParameter');
  if (window.WebGL2RenderingContext) {
    WebGL2RenderingContext.prototype.getParameter = _spoofGL;
    mask(WebGL2RenderingContext.prototype.getParameter, 'getParameter');
  }
} catch (e) {}

/* 8. audio fingerprint noise */
try {
  const _gcd = AudioBuffer.prototype.getChannelData;
  AudioBuffer.prototype.getChannelData = function (...a) {
    const d = _gcd.apply(this, a);
    for (let i = 0; i < d.length; i += 97) d[i] += (Math.random() - 0.5) * 1e-7;
    return d;
  };
  mask(AudioBuffer.prototype.getChannelData, 'getChannelData');
} catch (e) {}

/* 9. permissions */
try {
  const _query = Permissions.prototype.query;
  Permissions.prototype.query = function (p) {
    if (p && p.name === 'notifications')
      return Promise.resolve({ state: 'prompt', onchange: null });
    return _query.call(this, p);
  };
  mask(Permissions.prototype.query, 'query');
} catch (e) {}

/* 10. battery */
try {
  navigator.getBattery = () => Promise.resolve({
    charging: true, chargingTime: 0, dischargingTime: Infinity, level: 1,
    onchargingchange: null, onchargingtimechange: null,
    ondischargingtimechange: null, onlevelchange: null,
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false
  });
  mask(navigator.getBattery, 'getBattery');
} catch (e) {}

return 'stealth-applied';
})()"""

_STEALTH_CHECK_JS = ("JSON.stringify({stealth: !!window.__kancil_stealth__,"
                    " webdriver: navigator.webdriver,"
                    " plugins: (navigator.plugins || []).length,"
                    " ua_data: !!navigator.userAgentData,"
                    " chrome: !!window.chrome})")


def apply_stealth(engine, verify=True):
    """Inject STEALTH_JS into a WebView engine via its evaluate() API.

    Returns {"success": bool, "applied": bool, "verified": {...}|None,
             "errors": [...]}. Safe to call twice (snippet is idempotent).
    Re-apply after every navigation.
    """
    try:
        r = engine.evaluate(STEALTH_JS)
    except Exception as e:
        return {"success": False, "applied": False, "verified": None,
                "errors": [str(e)[:200]]}
    if not isinstance(r, dict) or not r.get("success"):
        errs = r.get("errors", ["evaluate failed"]) if isinstance(r, dict) else ["no response"]
        return {"success": False, "applied": False, "verified": None,
                "errors": errs}
    out = {"success": True, "applied": True, "verified": None, "errors": []}
    if verify:
        try:
            c = engine.evaluate(_STEALTH_CHECK_JS)
            if isinstance(c, dict) and c.get("success"):
                out["verified"] = json.loads(c.get("result") or "{}")
            else:
                out["verified"] = None
        except Exception as e:
            out["verified"] = None
            out["errors"].append("verify failed: %s" % str(e)[:120])
    return out
