"""Tests for kancil/vision.py (visual grounding) + api/cli wiring."""
import io
import json
import os
import unittest
from unittest import mock


def make_png(w=400, h=800, square=None):
    """Synthetic PNG via PIL. square=(x,y,sz) draws a black square."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), "white")
    if square:
        x, y, sz = square
        ImageDraw.Draw(img).rectangle([x, y, x + sz, y + sz], fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakeEngine:
    """Minimal engine: screenshot/evaluate/touch."""
    def __init__(self, png=None, vw=400, vh=800):
        self.png = png or make_png(vw, vh)
        self.vw, self.vh = vw, vh
        self.touches = []
        self.evals = []
        self.verify_results = []

    def screenshot(self, path=None, full=False):
        with open(path, "wb") as f:
            f.write(self.png)
        return {"success": True, "path": path, "bytes": len(self.png)}

    def evaluate(self, js):
        self.evals.append(js)
        if "innerWidth" in js:
            return {"w": self.vw, "h": self.vh}
        if "document.querySelector" in js:
            return bool(self.verify_results and self.verify_results.pop(0))
        if "elementFromPoint" in js:
            return "typed:5"
        return None

    def touch(self, action="tap", x=None, y=None, x2=None, y2=None,
              human=False, **kw):
        self.touches.append({"action": action, "x": x, "y": y,
                             "x2": x2, "y2": y2, "human": human})
        return {"success": True}


class VisionMathTest(unittest.TestCase):
    def test_to_css(self):
        from kancil import vision as v
        self.assertEqual(v.to_css(400, 800, 500, 250), (200.0, 200.0))
        self.assertEqual(v.to_css(400, 800, 0, 0), (0.0, 0.0))
        self.assertEqual(v.to_css(400, 800, 1000, 1000), (400.0, 800.0))

    def test_available_backends_shape(self):
        from kancil import vision as v
        a = v.available_backends()
        self.assertIn("template", a)
        self.assertIn("api", a)
        self.assertTrue(a["callback"])


class LocateTest(unittest.TestCase):
    def test_callback_backend(self):
        from kancil import vision as v
        png = make_png()
        r = v.locate(png, "tombol login", backend="callback",
                     callback=lambda p, d: (500, 250))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["candidates"][0]["x"], 500)
        self.assertEqual(r["candidates"][0]["y"], 250)

    def test_callback_out_of_range(self):
        from kancil import vision as v
        r = v.locate(make_png(), "x", backend="callback",
                     callback=lambda p, d: (5000, 10))
        self.assertFalse(r["success"])
        self.assertIn("0-1000", r["error"])

    def test_callback_not_callable(self):
        from kancil import vision as v
        r = v.locate(make_png(), "x", backend="callback")
        self.assertFalse(r["success"])

    def test_unknown_backend(self):
        from kancil import vision as v
        r = v.locate(make_png(), "x", backend="sonar")
        self.assertFalse(r["success"])
        self.assertIn("available", r)

    def test_auto_no_backend_no_env(self):
        from kancil import vision as v
        env = {k: val for k, val in os.environ.items()
               if not k.startswith("KANCIL_VISION_")}
        with mock.patch.dict(os.environ, env, clear=True):
            r = v.locate(make_png(), "x")
        self.assertFalse(r["success"])
        self.assertIn("backend", r["error"])

    def test_api_no_env(self):
        from kancil import vision as v
        env = {k: val for k, val in os.environ.items()
               if not k.startswith("KANCIL_VISION_")}
        with mock.patch.dict(os.environ, env, clear=True):
            r = v.locate_api(make_png(), "tombol")
        self.assertFalse(r["success"])
        self.assertIn("KANCIL_VISION_ENDPOINT", r["error"])

    def _mock_urlopen(self, payload_text):
        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return payload_text.encode()
        return mock.patch("urllib.request.urlopen", return_value=Resp())

    def test_api_locate_parses_coords(self):
        from kancil import vision as v
        body = json.dumps({"choices": [{"message": {"content":
                        '{"x": 500, "y": 250}'}}]})
        with self._mock_urlopen(body):
            r = v.locate_api(make_png(), "tombol", endpoint="https://x/v1",
                             api_key="k", model="m")
        self.assertTrue(r["success"], r)
        self.assertEqual(r["candidates"][0]["x"], 500)

    def test_api_locate_not_visible(self):
        from kancil import vision as v
        body = json.dumps({"choices": [{"message": {"content":
                        '{"x": -1, "y": -1}'}}]})
        with self._mock_urlopen(body):
            r = v.locate_api(make_png(), "tombol", endpoint="https://x/v1",
                             api_key="k", model="m")
        self.assertFalse(r["success"])
        self.assertIn("tidak terlihat", r["error"])

    def test_api_locate_multiple(self):
        from kancil import vision as v
        body = json.dumps({"choices": [{"message": {"content":
                        '[{"x": 100, "y": 100}, {"x": 900, "y": 900}]'}}]})
        with self._mock_urlopen(body):
            r = v.locate_api(make_png(), "tombol", multiple=True,
                             endpoint="https://x/v1", api_key="k", model="m")
        self.assertTrue(r["success"], r)
        self.assertEqual(len(r["candidates"]), 2)

    def test_api_garbage_response(self):
        from kancil import vision as v
        body = json.dumps({"choices": [{"message": {"content":
                        "maaf tidak bisa"}}]})
        with self._mock_urlopen(body):
            r = v.locate_api(make_png(), "tombol", endpoint="https://x/v1",
                             api_key="k", model="m")
        self.assertFalse(r["success"])

    def test_describe_screen(self):
        from kancil import vision as v
        body = json.dumps({"choices": [{"message": {"content":
                        "A login page with a blue button."}}]})
        with self._mock_urlopen(body):
            r = v.describe_screen(make_png(), endpoint="https://x/v1",
                                  api_key="k", model="m")
        self.assertTrue(r["success"], r)
        self.assertIn("login", r["description"])

    def test_template_backend(self):
        from kancil import vision as v
        try:
            import cv2  # noqa: F401
        except Exception:
            r = v.locate_template(make_png(), b"")
            self.assertFalse(r["success"])
            self.assertIn("opencv", r["error"].lower())
            return
        # synthetic: black 40px square at (100,200) in 400x800
        png = make_png(400, 800, square=(100, 200, 40))
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        tpl = img.crop((100, 200, 140, 240))
        buf = io.BytesIO()
        tpl.save(buf, format="PNG")
        r = v.locate_template(png, buf.getvalue(), threshold=0.9)
        self.assertTrue(r["success"], r)
        c = r["candidates"][0]
        # center of square = (120, 220) -> 0-1000 = (300, 275)
        self.assertAlmostEqual(c["x"], 300, delta=15)
        self.assertAlmostEqual(c["y"], 275, delta=15)
        self.assertGreaterEqual(c["score"], 0.9)


class PlumbingTest(unittest.TestCase):
    def test_grab_png(self):
        from kancil import vision as v
        e = FakeEngine()
        g = v.grab_png(e)
        self.assertTrue(g["success"], g)
        self.assertEqual(g["png"][:8], b"\x89PNG\r\n\x1a\n")

    def test_grab_png_no_screenshot(self):
        from kancil import vision as v
        g = v.grab_png(object())
        self.assertFalse(g["success"])

    def test_viewport_css(self):
        from kancil import vision as v
        r = v.viewport_css(FakeEngine(vw=360, vh=740))
        self.assertTrue(r["success"], r)
        self.assertEqual((r["w"], r["h"]), (360, 740))

    def test_calibrate(self):
        from kancil import vision as v
        r = v.calibrate(FakeEngine())
        self.assertTrue(r["success"], r)
        self.assertIn("mapping", r)


class SeeTapTest(unittest.TestCase):
    def test_see_tap_happy_path(self):
        from kancil import vision as v
        e = FakeEngine()
        e.verify_results = [True]
        r = v.see_tap(e, "tombol login", backend="callback",
                      callback=lambda p, d: (500, 250), verify="#ok")
        self.assertTrue(r["success"], r)
        self.assertEqual(len(e.touches), 1)
        t = e.touches[0]
        self.assertEqual(t["action"], "tap")
        self.assertEqual((t["x"], t["y"]), (200.0, 200.0))
        self.assertEqual(r["tries"], 1)

    def test_see_tap_dry_run_blocked(self):
        from kancil import vision as v
        e = FakeEngine()
        r = v.see_tap(e, "tombol", backend="callback",
                      callback=lambda p, d: (1, 1), dry_run=True)
        self.assertFalse(r["success"])
        self.assertEqual(r["code"], "DRY_RUN_BLOCKED")
        self.assertEqual(e.touches, [])
        # confirm=True melewati blokir
        r2 = v.see_tap(e, "tombol", backend="callback",
                       callback=lambda p, d: (1, 1),
                       dry_run=True, confirm=True)
        self.assertTrue(r2["success"], r2)
        self.assertEqual(len(e.touches), 1)

    def test_see_tap_retry_next_candidate(self):
        from kancil import vision as v
        e = FakeEngine()
        e.verify_results = [False, True]  # kandidat 1 gagal verify
        fake_locate = {"success": True, "backend": "api",
                       "candidates": [{"x": 100, "y": 100, "score": 1.0},
                                      {"x": 500, "y": 500, "score": 0.9}]}
        with mock.patch.object(v, "locate", return_value=fake_locate):
            r = v.see_tap(e, "tombol", backend="api",
                          verify="#ok", max_tries=2)
        self.assertTrue(r["success"], r)
        self.assertEqual(r["tries"], 2)
        self.assertEqual(len(e.touches), 2)
        self.assertEqual((e.touches[1]["x"], e.touches[1]["y"]),
                         (200.0, 400.0))

    def test_see_tap_not_found(self):
        from kancil import vision as v
        e = FakeEngine()
        r = v.see_tap(e, "tombol", backend="callback",
                      callback=lambda p, d: (_ for _ in ()).throw(
                          RuntimeError("buta")))
        self.assertFalse(r["success"])
        self.assertEqual(r["stage"], "locate")

    def test_see_type(self):
        from kancil import vision as v
        e = FakeEngine()
        r = v.see_type(e, "kolom nama", "Budi",
                       backend="callback", callback=lambda p, d: (250, 400))
        self.assertTrue(r["success"], r)
        self.assertEqual(r["typed_chars"], "5")
        self.assertEqual((r["x"], r["y"]), (100.0, 320.0))
        self.assertTrue(any("elementFromPoint" in j for j in e.evals))

    def test_see_type_dry_run(self):
        from kancil import vision as v
        e = FakeEngine()
        r = v.see_type(e, "kolom", "x", backend="callback",
                       callback=lambda p, d: (1, 1), dry_run=True)
        self.assertFalse(r["success"])
        self.assertEqual(r["code"], "DRY_RUN_BLOCKED")

    def test_see_drag(self):
        from kancil import vision as v
        e = FakeEngine()
        r = v.see_drag(e, "gagang", "ujung",
                       backend="callback",
                       callback=lambda p, d: (100, 500) if "gagang" in d
                       else (900, 500))
        self.assertTrue(r["success"], r)
        self.assertEqual(len(e.touches), 1)
        t = e.touches[0]
        self.assertEqual(t["action"], "swipe")
        self.assertEqual((t["x"], t["y"]), (40.0, 400.0))
        self.assertEqual((t["x2"], t["y2"]), (360.0, 400.0))


class VisionApiWiringTest(unittest.TestCase):
    def test_engine_gate(self):
        from kancil.api import Kancil
        b = Kancil(engine="static", timeout=5, retries=0)
        try:
            for fn, args in [(b.vision_locate, ("x",)),
                             (b.see_tap, ("x",)),
                             (b.see_type, ("x", "y")),
                             (b.see_drag, ("a", "b")),
                             (b.vision_describe, ())]:
                r = fn(*args)
                self.assertFalse(r["success"])
                self.assertIn("webview", r["errors"][0])
        finally:
            b.close()

    def test_tool_actions_registered(self):
        from kancil.api import Kancil
        for a in ("vision_locate", "vision_describe", "vision_calibrate",
                  "see_tap", "see_type", "see_drag"):
            self.assertIn(a, Kancil._TOOL_ACTIONS)
            m = Kancil._action_meta(a)
            self.assertTrue(m["description"], a)
            self.assertIn("params", m)

    def test_tool_call_vision_locate_no_vision_engine(self):
        from kancil.api import Kancil
        b = Kancil(engine="static", timeout=5, retries=0)
        try:
            r = b.tool({"action": "vision_locate",
                        "description": "tombol"})
            self.assertFalse(r["success"])
            self.assertIn("error", r)
            self.assertIn("code", r["error"])
        finally:
            b.close()


if __name__ == "__main__":
    unittest.main()
