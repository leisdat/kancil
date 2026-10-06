"""Query API publik tanpa API key — 100% stdlib, tanpa dependensi.

Sumber data (semua gratis, tanpa registrasi):
- USGS Earthquake feed — public domain
- Open-Meteo — CC-BY 4.0; output cuaca WAJIB mencantumkan
  "Weather data by Open-Meteo.com"
- adsb.lol — ADS-B; parameter `dist` satuannya nautical miles
- Photon (Komoot) — geocoding; data (c) OpenStreetMap contributors
- Launch Library 2 (the Space Devs) — batas 15 request/jam
  tanpa auth (hanya didokumentasikan, bukan di-rate-limit di kode)
- CelesTrak — TLE satelit

Setiap fungsi mengembalikan envelope {"success": bool, ...},
tidak pernah melempar exception jaringan ke pemanggil.
"""
import datetime
import json
import math
import urllib.parse
import urllib.request

_UA = "Kancil/3.15.0 (+https://github.com/leisdat/kancil)"
_TIMEOUT = 18  # detik; API publik di atas umumnya < 3 detik

_USGS_FEED = ("https://earthquake.usgs.gov/earthquakes/feed/v1.0/"
              "summary/4.5_day.geojson")
_OPENMETEO = ("https://api.open-meteo.com/v1/forecast"
              "?latitude={lat}&longitude={lon}"
              "&current=temperature_2m,relative_humidity_2m,"
              "weather_code,wind_speed_10m&timezone=auto")
_ADSB = "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}"
_PHOTON = "https://photon.komoot.io/api/?q={q}&limit=1"
_LL2 = "https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit={n}"
_CELESTRAK = "https://celestrak.org/NORAD/elements/gp.php?{q}&FORMAT=tle"

# 1 nautical mile = 1.852 km
_NM_PER_KM = 1.0 / 1.852

# Kode cuaca WMO -> deskripsi singkat Bahasa Indonesia
_WMO = {
    0: "cerah", 1: "cerah berawan", 2: "berawan sebagian", 3: "mendung",
    45: "berkabut", 48: "kabut + embun beku",
    51: "gerimis ringan", 53: "gerimis", 55: "gerimis lebat",
    56: "gerimis beku ringan", 57: "gerimis beku lebat",
    61: "hujan ringan", 63: "hujan", 65: "hujan lebat",
    66: "hujan beku ringan", 67: "hujan beku lebat",
    71: "salju ringan", 73: "salju", 75: "salju lebat",
    77: "butir salju",
    80: "hujan lokal ringan", 81: "hujan lokal", 82: "hujan lokal lebat",
    85: "hujan salju ringan", 86: "hujan salju lebat",
    95: "badai petir", 96: "badai petir + hujan es ringan",
    99: "badai petir + hujan es lebat",
}


def _get_text(url, timeout=_TIMEOUT):
    """Ambil URL sebagai teks. Gagal -> envelope error, bukan exception."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                                   "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return {"success": False, "status": r.status,
                        "error": "HTTP %s dari %s" % (r.status, url)}
            return {"success": True,
                    "text": r.read().decode("utf-8", "replace")}
    except Exception as e:
        return {"success": False,
                "error": "network: %s: %s" % (type(e).__name__,
                                              str(e)[:200])}


def _get_json(url, timeout=_TIMEOUT):
    """Ambil URL sebagai JSON. Gagal -> envelope error, bukan exception."""
    r = _get_text(url, timeout)
    if not r["success"]:
        return r
    try:
        return {"success": True, "data": json.loads(r["text"])}
    except ValueError as e:
        return {"success": False,
                "error": "respons bukan JSON valid: %s" % e}


def _haversine_km(lat1, lon1, lat2, lon2):
    """Jarak great-circle dalam km (rumus haversine)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def _ms_iso(ms):
    """Epoch milidetik USGS -> ISO 8601 UTC."""
    try:
        return datetime.datetime.fromtimestamp(
            ms / 1000, tz=datetime.timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def quake_near(lat, lon, radius_km=500, min_mag=4.5):
    """Gempa M4.5+ dalam 24 jam terakhir di sekitar titik.

    Feed USGS hanya menyediakan ringkasan global M4.5+/hari, jadi
    filter jarak (haversine) dan magnitudo dilakukan manual di sini.
    Data USGS public domain.
    """
    r = _get_json(_USGS_FEED)
    if not r["success"]:
        return r
    out = []
    for f in r["data"].get("features") or []:
        props = f.get("properties") or {}
        mag = props.get("mag")
        if mag is None or mag < min_mag:
            continue
        coords = (f.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        dist = _haversine_km(lat, lon, coords[1], coords[0])
        if dist > radius_km:
            continue
        ts = props.get("time")
        out.append({
            "mag": mag,
            "place": props.get("place"),
            "time_utc": _ms_iso(ts) if ts else None,
            "depth_km": coords[2] if len(coords) > 2 else None,
            "lat": coords[1],
            "lon": coords[0],
            "dist_km": round(dist, 1),
            "tsunami": bool(props.get("tsunami")),
            "url": props.get("url"),
        })
    out.sort(key=lambda q: q["time_utc"] or "", reverse=True)
    return {"success": True, "count": len(out), "quakes": out,
            "source": "USGS Earthquake feed (public domain)"}


def weather_now(lat, lon):
    """Cuaca saat ini dari Open-Meteo (tanpa API key).

    Syarat lisensi CC-BY 4.0: output WAJIB mencantumkan atribusi
    "Weather data by Open-Meteo.com" (ada di field `attribution`).
    """
    r = _get_json(_OPENMETEO.format(lat="%g" % lat, lon="%g" % lon))
    if not r["success"]:
        return r
    d = r["data"]
    cur = d.get("current") or {}
    code = cur.get("weather_code")
    return {"success": True,
            "lat": d.get("latitude"), "lon": d.get("longitude"),
            "timezone": d.get("timezone"),
            "time": cur.get("time"),
            "temperature_c": cur.get("temperature_2m"),
            "humidity_pct": cur.get("relative_humidity_2m"),
            "wind_kmh": cur.get("wind_speed_10m"),
            "weather_code": code,
            "weather": _WMO.get(code, "kode %s" % code),
            "attribution": "Weather data by Open-Meteo.com"}


def flights_near(lat, lon, radius_km=100):
    """Pesawat ADS-B di sekitar titik (adsb.lol, tanpa API key).

    SATUAN JARAK: parameter `dist` di API adsb.lol satuannya
    NAUTICAL MILES, bukan km. Terverifikasi 2026-10-06 dari respons
    live: field `dst` pesawat BTK6820 = 90.443 sedangkan jarak
    haversine dari titik query = ~167.7 km = ~90.5 NM — cocok.
    Fungsi ini menerima radius_km lalu mengonversi:
    dist_nm = radius_km / 1.852. Field `dist_km` di output juga
    dikonversi balik dari `dst` (NM) ke km.
    """
    dist_nm = radius_km * _NM_PER_KM
    url = _ADSB.format(lat="%g" % lat, lon="%g" % lon,
                       dist="%g" % dist_nm)
    r = _get_json(url)
    if not r["success"]:
        return r
    out = []
    for a in r["data"].get("ac") or []:
        dst = a.get("dst")  # nautical miles, dari titik query
        out.append({
            "hex": a.get("hex"),
            "flight": (a.get("flight") or "").strip() or None,
            "reg": a.get("r"),
            "type": a.get("t"),
            "lat": a.get("lat"),
            "lon": a.get("lon"),
            "alt_baro_ft": a.get("alt_baro"),
            "gs_kt": a.get("gs"),
            "track_deg": a.get("track"),
            "dist_km": (round(dst / _NM_PER_KM, 1)
                        if dst is not None else None),
        })
    return {"success": True, "count": len(out),
            "radius_km": radius_km, "aircraft": out,
            "source": "adsb.lol (ADS-B)"}


def geocode(q):
    """Nama tempat -> lat/lon + bbox via Photon (Komoot).

    Data (c) OpenStreetMap contributors.
    """
    r = _get_json(_PHOTON.format(q=urllib.parse.quote(q, safe="")))
    if not r["success"]:
        return r
    feats = r["data"].get("features") or []
    if not feats:
        return {"success": False,
                "error": "tidak ada hasil geocode untuk %r" % q}
    f = feats[0]
    props = f.get("properties") or {}
    coords = (f.get("geometry") or {}).get("coordinates") or [None, None]
    extent = props.get("extent")  # [min_lon, min_lat, max_lon, max_lat]
    return {"success": True,
            "name": props.get("name"),
            "lat": coords[1], "lon": coords[0],
            "country": props.get("country"),
            "state": props.get("state"),
            "bbox": extent,
            "source": "Photon (data (c) OpenStreetMap contributors)"}


def launches_upcoming(limit=5):
    """Jadwal peluncuran roket terdekat (Launch Library 2).

    CATATAN rate limit: endpoint unauthenticated dibatasi ~15
    request/jam oleh the Space Devs. Tidak ada rate limiter di
    kode ini — pemanggil yang mengatur frekuensi panggilannya.
    """
    try:
        n = max(1, int(limit))
    except (TypeError, ValueError):
        n = 5
    r = _get_json(_LL2.format(n=n))
    if not r["success"]:
        return r
    out = []
    for it in r["data"].get("results") or []:
        pad = it.get("pad") or {}
        loc = pad.get("location") or {}
        out.append({
            "name": it.get("name"),
            "net": it.get("net"),  # no-earlier-than, ISO
            "status": (it.get("status") or {}).get("name"),
            "agency": (it.get("launch_service_provider") or {}).get("name"),
            "pad": pad.get("name"),
            "location": loc.get("name"),
            "mission": (it.get("mission") or {}).get("description"),
            "url": it.get("url"),
        })
    return {"success": True, "count": len(out), "launches": out,
            "source": "Launch Library 2 (thespacedevs.com)"}


def sat_tle(norad_id=None, group="stations"):
    """TLE satelit dari CelesTrak (teks biasa, bukan JSON).

    norad_id (CATNR, mis. 25544 = ISS) -> satu TLE.
    Tanpa norad_id -> seluruh grup, mis. "stations", "visual",
    "weather", "goes", "gps-ops".
    """
    if norad_id is not None:
        q = "CATNR=%d" % int(norad_id)
    else:
        q = "GROUP=%s" % urllib.parse.quote(str(group), safe="")
    r = _get_text(_CELESTRAK.format(q=q))
    if not r["success"]:
        return r
    lines = [ln.rstrip() for ln in r["text"].splitlines() if ln.strip()]
    sats = []
    i = 0
    while i + 2 < len(lines):
        name, l1, l2 = lines[i], lines[i + 1], lines[i + 2]
        if l1.startswith("1 ") and l2.startswith("2 "):
            sats.append({"name": name.strip(), "line1": l1, "line2": l2})
            i += 3
        else:
            i += 1
    if norad_id is not None and not sats:
        return {"success": False,
                "error": "tidak ada TLE untuk CATNR %s" % norad_id}
    return {"success": True, "count": len(sats), "tle": sats,
            "source": "CelesTrak"}
