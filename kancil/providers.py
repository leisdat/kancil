"""Deteksi provider proteksi anti-bot dari signature.

Signature di-port dari lingxudr/specter (MIT) — specter/providers/*_adapter.py:
header keys, cookie names, dan body markers untuk 8 provider.
Dipakai stuck detector agar hint spesifik per provider, bukan sekadar
"captcha" generik.

Contoh: {"provider": "cloudflare", "confidence": 0.9,
         "evidence": {"cf_ray": "...", "body_markers": ["cf-turnstile"]}}
"""

# (header_keys, cookie_names, body_markers, solvable, hint)
# solvable: "turnstile" = checkbox bisa di-klik otomatis (seperti manusia);
#           False = butuh manusia / tidak bisa.
PROVIDERS = {
    "cloudflare": {
        "headers": {"server": "cloudflare", "cf-ray": True,
                    "cf-cache-status": True},
        "cookies": ("cf_clearance", "__cf_bm", "cf_bm"),
        "body": ("cf-challenge", "cf-spinner",
                 "attention required! | cloudflare",
                 "cf_chl_opt", "__cf_chl_jschl_tk__",
                 "cf-turnstile", "cf-chl-bypass",
                 "challenges.cloudflare.com"),
        "solvable": "turnstile",
        "hint": "Cloudflare: checkbox Turnstile biasanya cukup diklik "
                "(resolve_stuck otomatis mencoba); Managed Challenge "
                "butuh tunggu / manusia.",
    },
    "hcaptcha": {
        "headers": {},
        "cookies": (),
        "body": ("hcaptcha.com/1/api.js", "hcaptcha.com/captcha",
                 "h-captcha", "hcaptcha.render", "data-hcaptcha-sitekey",
                 "newassets.hcaptcha.com"),
        "solvable": False,
        "hint": "hCaptcha: butuh manusia (atau solver berbayar).",
    },
    "recaptcha": {
        "headers": {},
        "cookies": (),
        "body": ("google.com/recaptcha", "grecaptcha", "recaptcha/api2",
                 "recaptcha/api.js", "grecaptcha.render",
                 "recaptcha/api2/anchor"),
        "solvable": False,
        "hint": "reCAPTCHA: v2 checkbox kadang lolos sekali klik; "
                "v3/invisible butuh manusia.",
    },
    "aws_waf": {
        "headers": {"x-amzn-waf-action": True, "x-amz-cf-id": True,
                    "x-amz-waf-id": True},
        "cookies": ("aws-waf-token",),
        "body": ("awswaf", "aws-wfb-token", "aws-waf-token",
                 "challenge.js"),
        "solvable": False,
        "hint": "AWS WAF: token challenge.js — selesaikan sekali di "
                "browser asli lalu import cookie.",
    },
    "akamai": {
        "headers": {"x-akamai-transformed": True, "x-acg-cache-status": True,
                    "akamai-origin-hop": True, "x-akamai-request-id": True},
        "cookies": ("ak_bmsc", "bm_sz", "bm_sv", "_abck"),
        "body": ("akam-test-cookie.js", "akamai", "_bmr.js",
                 "sensor_data"),
        "solvable": False,
        "hint": "Akamai Bot Manager: butuh sensor_data valid — "
                "hampir selalu butuh browser/IP residential asli.",
    },
    "datadome": {
        "headers": {"x-datadome-clientid": True,
                    "x-datadome-campaignid": True},
        "cookies": ("datadome",),
        "body": ("datadome.com/captcha", "datadome.js", "dd.captcha.js",
                 "datadome.com/tag.js"),
        "solvable": False,
        "hint": "DataDome: proteksi keras — butuh sesi browser asli "
                "yang sudah lolos.",
    },
    "imperva": {
        "headers": {"x-iinfo": True, "x-incap-session": True},
        "cookies": ("incap_ses_", "visid_incap_", "reese84"),
        "body": ("incapsula", "imperva", "_incapsula_resource",
                 "reese84"),
        "solvable": False,
        "hint": "Imperva/Incapsula: cookie incap_ses_* — selesaikan "
                "sekali di browser asli.",
    },
    "arkose": {
        "headers": {},
        "cookies": (),
        "body": ("arkoselabs.com/v2/", "client-api.arkoselabs.com",
                 "funcaptcha.com/fc", "arkose.enforcement"),
        "solvable": False,
        "hint": "Arkose/FunCaptcha: puzzle interaktif — butuh manusia.",
    },
}


def detect_provider(headers=None, body="", cookies=""):
    """Identifikasi provider proteksi.

    headers: dict (boleh None), body: HTML/teks halaman,
    cookies: string gabungan nama cookie (boleh "").
    Returns {"provider", "confidence", "evidence"} — provider None
    bila tidak ada signature cocok.
    """
    headers = {(k.lower() if k else ""): (str(v).lower() if v else "")
               for k, v in (headers or {}).items()}
    body_low = (body or "").lower()
    cookies_low = (cookies or "").lower()
    blob = cookies_low + " " + body_low

    best = {"provider": None, "confidence": 0.0, "evidence": {}}
    for name, spec in PROVIDERS.items():
        ev = {}
        conf = 0.0
        for hk, want in spec["headers"].items():
            if hk in headers:
                if want is True or want in headers[hk]:
                    ev["header:" + hk] = headers[hk][:60]
                    conf = max(conf, 0.9)
        found_ck = [c for c in spec["cookies"] if c in blob]
        if found_ck:
            ev["cookies"] = found_ck
            conf = max(conf, 0.85 if conf == 0 else min(0.99, conf + 0.1))
        found_m = [m for m in spec["body"] if m in body_low]
        if found_m:
            ev["body_markers"] = found_m[:4]
            conf = max(conf, 0.85 if conf == 0 else min(0.99, conf + 0.05))
        if conf > best["confidence"]:
            best = {"provider": name, "confidence": round(conf, 2),
                    "evidence": ev, "solvable": spec["solvable"],
                    "hint": spec["hint"]}
    return best


def provider_hint(provider):
    """Hint singkat per provider (untuk stuck detector)."""
    spec = PROVIDERS.get(provider or "")
    return spec["hint"] if spec else ""
