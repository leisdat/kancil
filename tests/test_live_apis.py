"""Tests untuk kancil/live.py + method live_* di Kancil.

SEMUA test memakai mock — tidak ada yang menyentuh network.
Verifikasi live endpoint dilakukan manual via curl (lihat laporan).
"""
import io
import json
import unittest
from unittest import mock

from kancil import live as lv
from kancil.api import Kancil

_LIVE_ACTIONS = ["live_quake", "live_weather", "live_flights",
                 "live_geocode", "live_launches", "live_tle"]


def _resp(status=200, body=b"{}"):
    """Mock context manager untuk urllib.request.urlopen."""
    m = mock.MagicMock()
    m.status = status
    m.read.return_value = body
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    return m


class GetJsonTest(unittest.TestCase):
    def test_ok(self):
        payload = {"a": 1}
        with mock.patch("urllib.request.urlopen",
                        return_value=_resp(
                            200, json.dumps(payload).encode())):
            r = lv._get_json("https://x.test/")
        self.assertTrue(r["success"])
        self.assertEqual(r["data"], payload)

    def test_http_error(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=_resp(503, b"nope")):
            r = lv._get_json("https://x.test/")
        self.assertFalse(r["success"])
        self.assertIn("503", r["error"])

    def test_network_exception(self):
        import urllib.error
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError("boom")):
            r = lv._get_json("https://x.test/")
        self.assertFalse(r["success"])
        self.assertIn("network", r["error"])

    def test_bad_json(self):
        with mock.patch("urllib.request.urlopen",
                        return_value=_resp(200, b"bukan json")):
            r = lv._get_json("https://x.test/")
        self.assertFalse(r["success"])
        self.assertIn("JSON", r["error"])


# --- fixture ---

def _usgs():
    # Jakarta = -6.2, 106.8. "dekat" ~150 km, "jauh" ~2000 km,
    # "kecil" dekat tapi mag di bawah min.
    return {"type": "FeatureCollection", "features": [
        _usgs_feat(5.1, "dekat", 107.5, -6.5, 10.0, 1000),
        _usgs_feat(6.0, "jauh", 130.0, -6.0, 20.0, 2000),
        _usgs_feat(4.0, "kecil", 106.9, -6.3, 5.0, 3000),
    ]}


def _usgs_feat(mag, place, lon, lat, depth, ms):
    return {"type": "Feature",
            "properties": {"mag": mag, "place": place,
                           "time": ms, "tsunami": 0,
                           "url": "https://x/%s" % place},
            "geometry": {"type": "Point",
                         "coordinates": [lon, lat, depth]}}


class QuakeTest(unittest.TestCase):
    def test_filter_radius_dan_mag(self):
        with mock.patch.object(lv, "_get_json",
                               return_value={"success": True,
                                             "data": _usgs()}):
            r = lv.quake_near(-6.2, 106.8, radius_km=500, min_mag=4.5)
        self.assertTrue(r["success"])
        # hanya "dekat" lolos: "jauh" di luar radius, "kecil" di bawah mag
        self.assertEqual(r["count"], 1)
        q = r["quakes"][0]
        self.assertEqual(q["place"], "dekat")
        self.assertEqual(q["mag"], 5.1)
        self.assertLess(q["dist_km"], 500)
        self.assertGreater(q["dist_km"], 50)  # ~150 km, bukan 0
        self.assertEqual(q["depth_km"], 10.0)
        self.assertTrue(q["time_utc"].endswith("+00:00"))

    def test_network_fail_jadi_envelope(self):
        with mock.patch.object(lv, "_get_json",
                               return_value={"success": False,
                                             "error": "network: x"}):
            r = lv.quake_near(-6.2, 106.8)
        self.assertFalse(r["success"])


class WeatherTest(unittest.TestCase):
    def test_parse_dan_atribusi(self):
        data = {"latitude": -6.2, "longitude": 106.85,
                "timezone": "Asia/Jakarta",
                "current": {"time": "2026-10-06T13:00",
                            "temperature_2m": 31.5,
                            "relative_humidity_2m": 70,
                            "weather_code": 3,
                            "wind_speed_10m": 12.4}}
        with mock.patch.object(lv, "_get_json",
                               return_value={"success": True, "data": data}):
            r = lv.weather_now(-6.2, 106.85)
        self.assertTrue(r["success"])
        self.assertEqual(r["temperature_c"], 31.5)
        self.assertEqual(r["humidity_pct"], 70)
        self.assertEqual(r["wind_kmh"], 12.4)
        self.assertEqual(r["weather_code"], 3)
        self.assertEqual(r["weather"], "mendung")
        # syarat CC-BY 4.0: atribusi wajib ada di output
        self.assertEqual(r["attribution"], "Weather data by Open-Meteo.com")

    def test_wmo_tak_dikenal(self):
        data = {"current": {"weather_code": 999}}
        with mock.patch.object(lv, "_get_json",
                               return_value={"success": True, "data": data}):
            r = lv.weather_now(0, 0)
        self.assertIn("999", r["weather"])


class FlightsTest(unittest.TestCase):
    _AC = {"ac": [
        {"hex": "8a091f", "flight": "BTK6820 ",
         "r": "PK-BKJ", "t": "A320", "lat": -5.4, "lon": 105.5,
         "alt_baro": 23625, "gs": 444.1, "track": 293.91,
         "dst": 90.443},
        {"hex": "abcdef", "flight": "", "r": None, "t": "B738",
         "lat": -6.0, "lon": 107.0, "alt_baro": None, "gs": None,
         "track": None},
    ]}

    def test_dist_dalam_nautical_miles(self):
        seen = {}

        def fake_get(url, timeout=18):
            seen["url"] = url
            return {"success": True, "data": self._AC}

        with mock.patch.object(lv, "_get_json", side_effect=fake_get):
            r = lv.flights_near(-6.2, 106.8, radius_km=100)
        self.assertTrue(r["success"])
        # 100 km = 53.9957 NM -> dist di URL harus NM (~54), bukan 100 (km)
        dist = float(seen["url"].rsplit("/dist/", 1)[1])
        self.assertAlmostEqual(dist, 100 / 1.852, places=2)
        self.assertNotIn("/dist/100", seen["url"])

    def test_parse_field(self):
        with mock.patch.object(lv, "_get_json",
                               return_value={"success": True,
                                             "data": self._AC}):
            r = lv.flights_near(-6.2, 106.8, radius_km=100)
        self.assertEqual(r["count"], 2)
        a = r["aircraft"][0]
        self.assertEqual(a["hex"], "8a091f")
        self.assertEqual(a["flight"], "BTK6820")  # trailing space di-trim
        self.assertEqual(a["reg"], "PK-BKJ")
        self.assertEqual(a["type"], "A320")
        self.assertEqual(a["alt_baro_ft"], 23625)
        self.assertEqual(a["gs_kt"], 444.1)
        self.assertEqual(a["track_deg"], 293.91)
        # dst 90.443 NM -> ~167.5 km
        self.assertAlmostEqual(a["dist_km"], 90.443 * 1.852, places=1)
        b = r["aircraft"][1]
        self.assertIsNone(b["flight"])
        self.assertIsNone(b["dist_km"])


class GeocodeTest(unittest.TestCase):
    def test_parse(self):
        data = {"features": [{
            "properties": {"name": "Jakarta", "country": "Indonesia",
                           "state": "Jawa",
                           "extent": [106.31, -6.37, 106.97, -4.99]},
            "geometry": {"type": "Point",
                         "coordinates": [106.827168, -6.1754049]}}]}
        with mock.patch.object(lv, "_get_json",
                               return_value={"success": True, "data": data}):
            r = lv.geocode("Jakarta")
        self.assertTrue(r["success"])
        self.assertEqual(r["name"], "Jakarta")
        self.assertAlmostEqual(r["lat"], -6.1754049)
        self.assertAlmostEqual(r["lon"], 106.827168)
        self.assertEqual(r["bbox"], [106.31, -6.37, 106.97, -4.99])

    def test_kosong(self):
        with mock.patch.object(lv, "_get_json",
                               return_value={"success": True,
                                             "data": {"features": []}}):
            r = lv.geocode("zzzzz-tidak-ada")
        self.assertFalse(r["success"])


class LaunchesTest(unittest.TestCase):
    _LL2 = {"results": [{
        "name": "Nuri | NeonSat",
        "net": "2026-10-07T00:00:00Z",
        "status": {"name": "Go"},
        "launch_service_provider": {"name": "KARI"},
        "pad": {"name": "Pad 1",
                "location": {"name": "Naro Space Center"}},
        "mission": {"description": "uji coba"},
        "url": "https://x/launch"}]}

    def test_parse(self):
        seen = {}

        def fake_get(url, timeout=18):
            seen["url"] = url
            return {"success": True, "data": self._LL2}

        with mock.patch.object(lv, "_get_json", side_effect=fake_get):
            r = lv.launches_upcoming(limit=5)
        self.assertIn("limit=5", seen["url"])
        self.assertTrue(r["success"])
        self.assertEqual(r["count"], 1)
        l = r["launches"][0]
        self.assertEqual(l["name"], "Nuri | NeonSat")
        self.assertEqual(l["status"], "Go")
        self.assertEqual(l["agency"], "KARI")
        self.assertEqual(l["pad"], "Pad 1")
        self.assertEqual(l["location"], "Naro Space Center")

    def test_limit_invalid_jadi_default(self):
        with mock.patch.object(lv, "_get_json",
                               return_value={"success": True,
                                             "data": {"results": []}}):
            r = lv.launches_upcoming(limit="bukan-angka")
        self.assertTrue(r["success"])


class TleTest(unittest.TestCase):
    _TLE = ("ISS (ZARYA)\n"
            "1 25544U 98067A   26278.82086582  .00005030  00000+0  10025-3 0  9992\n"
            "2 25544  51.6314 112.5837 0006860 227.0704 132.9710 15.48745232588905\n"
            "POISK\n"
            "1 36086U 09060A   26278.82086582  .00005030  00000+0  10025-3 0  9990\n"
            "2 36086  51.6314 112.5837 0006860 227.0704 132.9710 15.48745232588906\n")

    def test_group_parse(self):
        seen = {}

        def fake_get(url, timeout=18):
            seen["url"] = url
            return {"success": True, "text": self._TLE}

        with mock.patch.object(lv, "_get_text", side_effect=fake_get):
            r = lv.sat_tle(group="stations")
        self.assertIn("GROUP=stations", seen["url"])
        self.assertTrue(r["success"])
        self.assertEqual(r["count"], 2)
        self.assertEqual(r["tle"][0]["name"], "ISS (ZARYA)")
        self.assertTrue(r["tle"][0]["line1"].startswith("1 25544"))
        self.assertTrue(r["tle"][0]["line2"].startswith("2 25544"))

    def test_catnr(self):
        seen = {}

        def fake_get(url, timeout=18):
            seen["url"] = url
            return {"success": True, "text": self._TLE}

        with mock.patch.object(lv, "_get_text", side_effect=fake_get):
            r = lv.sat_tle(norad_id=25544)
        self.assertIn("CATNR=25544", seen["url"])

    def test_catnr_tak_dikenal(self):
        with mock.patch.object(lv, "_get_text",
                               return_value={"success": True, "text": ""}):
            r = lv.sat_tle(norad_id=999999)
        self.assertFalse(r["success"])


class LiveApiMethodTest(unittest.TestCase):
    """Method Kancil.live_*: envelope error, bukan exception mentah."""

    def _k(self):
        k = Kancil.__new__(Kancil)
        k._engine_name = "static"
        return k

    def test_quake_ok_dan_invalid(self):
        k = self._k()
        with mock.patch("kancil.live.quake_near",
                        return_value={"success": True, "count": 1}) as m:
            r = k.live_quake(-6.2, 106.8, radius_km=500, min_mag=4.5)
        self.assertTrue(r["success"])
        m.assert_called_once_with(-6.2, 106.8, 500.0, 4.5)
        r = k.live_quake("bukan", None)
        self.assertFalse(r["success"])
        self.assertIn("errors", r)

    def test_weather_flights_geocode(self):
        k = self._k()
        with mock.patch("kancil.live.weather_now",
                        return_value={"success": True}) as m:
            self.assertTrue(k.live_weather(-6.2, 106.8)["success"])
            m.assert_called_once_with(-6.2, 106.8)
        self.assertFalse(k.live_weather(None, None)["success"])
        with mock.patch("kancil.live.flights_near",
                        return_value={"success": True}) as m:
            self.assertTrue(k.live_flights(-6.2, 106.8)["success"])
            m.assert_called_once_with(-6.2, 106.8, 100.0)
        self.assertFalse(k.live_geocode("")["success"])
        with mock.patch("kancil.live.geocode",
                        return_value={"success": True}):
            self.assertTrue(k.live_geocode("Jakarta")["success"])

    def test_launches_tle(self):
        k = self._k()
        with mock.patch("kancil.live.launches_upcoming",
                        return_value={"success": True}) as m:
            self.assertTrue(k.live_launches(3)["success"])
            m.assert_called_once_with(3)
        self.assertFalse(k.live_launches("x")["success"])
        with mock.patch("kancil.live.sat_tle",
                        return_value={"success": True}) as m:
            self.assertTrue(k.live_tle(norad_id=25544)["success"])
            m.assert_called_once_with(25544, "stations")
            self.assertTrue(k.live_tle()["success"])
            m.assert_called_with(None, "stations")
        self.assertFalse(k.live_tle(norad_id="x")["success"])


class LiveManifestTest(unittest.TestCase):
    def test_enam_action_terdaftar(self):
        for a in _LIVE_ACTIONS:
            self.assertIn(a, Kancil._TOOL_ACTIONS)
            meta = Kancil._action_meta(a)
            self.assertTrue(meta["params"], a)
            self.assertTrue(meta["description"], a)

    def test_param_lat_lon(self):
        meta = Kancil._action_meta("live_quake")
        self.assertEqual(meta["params"]["radius_km"]["default"], 500)
        self.assertEqual(meta["params"]["min_mag"]["default"], 4.5)
        meta = Kancil._action_meta("live_launches")
        self.assertEqual(meta["params"]["limit"]["type"], "integer")
        meta = Kancil._action_meta("live_tle")
        self.assertEqual(meta["params"]["norad_id"]["type"], "integer")

    def test_tool_call_end_to_end(self):
        k = Kancil.__new__(Kancil)
        k._engine_name = "static"
        with mock.patch("kancil.live.geocode",
                        return_value={"success": True, "name": "Jakarta"}):
            r = Kancil._TOOL_ACTIONS["live_geocode"](
                k, {"action": "live_geocode", "q": "Jakarta"})
        self.assertTrue(r["success"])

    def test_count(self):
        self.assertEqual(len(Kancil._TOOL_ACTIONS), 147)


if __name__ == "__main__":
    unittest.main()
