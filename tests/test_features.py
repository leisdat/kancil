"""Test 8 saran fitur audit: MCP, JSON Schema, snapshot compact/diff,
safety gate, record/replay, vault, network bodies."""
import json
import os
import tempfile
import unittest


class JsonSchemaTest(unittest.TestCase):
    def test_single_action_schema(self):
        from kancil.api import Kancil
        k = Kancil(engine="static")
        r = k.action_json_schema("click")
        self.assertTrue(r["success"])
        s = r["schema"]
        self.assertEqual(s["type"], "object")
        self.assertIn("selector", s["properties"])
        self.assertIn("$schema", s)

    def test_all_schemas_count(self):
        from kancil.api import Kancil
        k = Kancil(engine="static")
        r = k.action_json_schema()
        self.assertTrue(r["success"])
        self.assertEqual(len(r["schemas"]), len(Kancil._TOOL_ACTIONS))

    def test_unknown_action(self):
        from kancil.api import Kancil
        r = Kancil(engine="static").action_json_schema("nope")
        self.assertFalse(r["success"])

    def test_validate_ok_and_bad(self):
        from kancil.api import Kancil
        k = Kancil(engine="static")
        self.assertTrue(
            k.validate_action("click", {"action": "click",
                                       "selector": "#x"})["success"])
        bad = k.validate_action("click", {"action": "click",
                                          "selector": 123})
        self.assertFalse(bad["success"])
        self.assertTrue(any("selector" in e for e in bad["errors"]))

    def test_destructive_has_confirm_param(self):
        from kancil.api import Kancil
        k = Kancil(engine="static")
        s = k.action_json_schema("session_clear")["schema"]
        self.assertIn("confirm", s["properties"])
        self.assertEqual(s["properties"]["confirm"]["type"], "boolean")


class SafetyGateTest(unittest.TestCase):
    def test_destructive_blocked_without_confirm(self):
        from kancil.api import Kancil
        k = Kancil(engine="static")
        r = k.tool({"action": "session_clear"})
        self.assertFalse(r["success"])
        self.assertEqual(r["error"]["code"], "CONFIRM_REQUIRED")

    def test_destructive_passes_with_confirm(self):
        from kancil.api import Kancil
        k = Kancil(engine="static")
        # dengan confirm -> lolos gate, dieksekusi normal
        r = k.tool({"action": "session_clear", "confirm": True})
        self.assertNotEqual(r.get("error", {}).get("code"),
                            "CONFIRM_REQUIRED")

    def test_opt_out_flag(self):
        from kancil.api import Kancil
        k = Kancil(engine="static", confirm_destructive=False)
        r = k.tool({"action": "session_clear"})
        self.assertNotEqual(r.get("error", {}).get("code"),
                            "CONFIRM_REQUIRED")

    def test_non_destructive_unaffected(self):
        from kancil.api import Kancil
        k = Kancil(engine="static")
        r = k.tool({"action": "snapshot", "compact": True})
        # boleh gagal karena no page, tapi bukan karena confirm
        self.assertNotEqual(r.get("error", {}).get("code"),
                            "CONFIRM_REQUIRED")

    def test_mark_untrusted(self):
        from kancil import safety
        m = safety.mark("klik di sini")
        self.assertTrue(m.startswith("[untrusted]"))
        self.assertTrue(m.endswith("[/untrusted]"))
        self.assertIn("klik di sini", m)
        # idempoten
        self.assertEqual(safety.mark(m), m)


class SnapshotDiffTest(unittest.TestCase):
    def _snap(self, url, title, links, buttons=()):
        return {"success": True, "url": url, "title": title,
                "page": {"headings": [],
                         "links": [{"text": t, "url": u} for t, u in links],
                         "buttons": [{"name": b} for b in buttons],
                         "inputs": []}}

    def test_diff_detects_changes(self):
        from kancil.api import Kancil
        k = Kancil(engine="static")
        a = self._snap("https://x.com/", "T", [("a", "https://x.com/a")])
        b = self._snap("https://x.com/", "T2",
                       [("a", "https://x.com/a"), ("b", "https://x.com/b")],
                       buttons=["Go"])
        r = k.snapshot_diff(a, b)
        self.assertTrue(r["success"])
        self.assertTrue(r["title_changed"])
        self.assertFalse(r["url_changed"])
        self.assertIn("https://x.com/b", r["added"]["links"])
        self.assertIn("Go", r["added"]["buttons"])
        self.assertIn("2 added", r["summary"])

    def test_diff_needs_two(self):
        from kancil.api import Kancil
        r = Kancil(engine="static").snapshot_diff()
        self.assertFalse(r["success"])


class RecordReplayTest(unittest.TestCase):
    def test_record_and_replay(self):
        from kancil.api import Kancil
        k = Kancil(engine="static")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "rec.jsonl")
            self.assertTrue(k.session_record_start(p)["success"])
            k.tool({"action": "snapshot", "compact": True})
            # aksi ditolak (tanpa confirm) tidak ikut terekam
            k.tool({"action": "session_clear"})
            self.assertTrue(k.session_record_stop()["success"])
            lines = [l for l in open(p).read().splitlines() if l.strip()]
            self.assertEqual(len(lines), 1)
            step = json.loads(lines[0])
            self.assertEqual(step["action"], "snapshot")
            # dry-run validasi
            r = k.session_replay(p, dry_run=True)
            self.assertTrue(r["success"])
            self.assertEqual(r["ran"], 1)

    def test_replay_missing_file(self):
        from kancil.api import Kancil
        r = Kancil(engine="static").session_replay("/tmp/tidak-ada-xyz.jsonl")
        self.assertFalse(r["success"])


class VaultCryptoTest(unittest.TestCase):
    def test_aes128_nist_vector(self):
        from kancil.vault import _enc_block, _dec_block
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        pt = bytes.fromhex("3243f6a8885a308d313198a2e0370734")
        ct_exp = bytes.fromhex("3925841d02dc09fbdc118597196a0b32")
        ct = _enc_block(key, pt)
        self.assertEqual(ct, ct_exp)
        self.assertEqual(_dec_block(key, ct), pt)

    def test_encrypt_decrypt_roundtrip(self):
        from kancil.vault import encrypt, decrypt
        blob = encrypt("pw-123", b"data rahasia" * 100)
        self.assertEqual(decrypt("pw-123", blob), b"data rahasia" * 100)

    def test_wrong_password_rejected(self):
        from kancil.vault import encrypt, decrypt
        blob = encrypt("benar", b"x")
        with self.assertRaises(ValueError):
            decrypt("salah", blob)

    def test_tamper_rejected(self):
        from kancil.vault import encrypt, decrypt
        blob = bytearray(encrypt("pw", b"y" * 64))
        blob[40] ^= 1
        with self.assertRaises(ValueError):
            decrypt("pw", bytes(blob))
        blob2 = bytearray(encrypt("pw", b"y" * 64))
        blob2[-1] ^= 1  # MAC
        with self.assertRaises(ValueError):
            decrypt("pw", bytes(blob2))

    def test_vault_api_cycle(self):
        from kancil.api import Kancil
        k = Kancil(engine="static")
        # pakai HOME sementara agar tidak menyentuh ~/.kancil asli
        with tempfile.TemporaryDirectory() as home:
            old = os.environ.get("HOME")
            os.environ["HOME"] = home
            os.environ["KANCIL_VAULT_PASSWORD"] = "t3st"
            try:
                r = k.tool({"action": "vault_save", "name": "s1",
                            "data": {"a": 1}})
                self.assertTrue(r["success"], r)
                r = k.vault_list()
                self.assertIn("s1", r["entries"])
                r = k.tool({"action": "vault_load", "name": "s1"})
                self.assertTrue(r["success"], r)
                self.assertEqual(r["data"], {"a": 1})
                # file harus 0600
                import stat
                p = os.path.join(home, ".kancil", "vault", "s1.kv")
                self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
                self.assertTrue(k.vault_delete("s1")["success"])
                self.assertEqual(k.vault_list()["entries"], [])
            finally:
                if old is None:
                    del os.environ["HOME"]
                else:
                    os.environ["HOME"] = old
                del os.environ["KANCIL_VAULT_PASSWORD"]


class MCPServerTest(unittest.TestCase):
    def test_tools_list_and_call(self):
        import subprocess
        import sys
        p = subprocess.Popen(
            [sys.executable, "-m", "kancil", "mcp"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)

        def rpc(method, params=None, rid=1):
            msg = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                msg["params"] = params
            p.stdin.write(json.dumps(msg) + "\n")
            p.stdin.flush()
            return json.loads(p.stdout.readline())

        try:
            r = rpc("initialize", {"protocolVersion": "2024-11-05"}, 1)
            self.assertEqual(r["result"]["protocolVersion"], "2024-11-05")
            self.assertEqual(r["result"]["serverInfo"]["name"], "kancil")
            r = rpc("tools/list", None, 2)
            tools = r["result"]["tools"]
            self.assertGreater(len(tools), 120)
            names = {t["name"] for t in tools}
            self.assertIn("snapshot", names)
            self.assertIn("vault_save", names)
            t = next(t for t in tools if t["name"] == "click")
            self.assertEqual(t["inputSchema"]["type"], "object")
            # tools/call: unknown -> JSON-RPC error
            r = rpc("tools/call", {"name": "tidak_ada",
                                   "arguments": {}}, 3)
            self.assertIn("error", r)
            # tools/call: valid action -> content block
            r = rpc("tools/call", {"name": "snapshot",
                                   "arguments": {"compact": True}}, 4)
            c = r["result"]
            self.assertIn("content", c)
            self.assertEqual(c["content"][0]["type"], "text")
            body = json.loads(c["content"][0]["text"])
            # gagal jujur (no page) tapi bentuknya benar
            self.assertIn("success", body)
            r = rpc("ping", None, 5)
            self.assertEqual(r["result"], {})
        finally:
            p.stdin.close()
            p.wait(timeout=15)


class NetworkBodiesTest(unittest.TestCase):
    def test_with_bodies_static(self):
        from kancil.api import Kancil
        k = Kancil(engine="static")
        # simulasi entri netlog dengan res_body
        k.engine.netlog = [
            {"id": 1, "url": "https://x.com/api", "method": "GET",
             "status": 200, "res_body": '{"ok": true}'},
            {"id": 2, "url": "https://x.com/img.png", "method": "GET",
             "status": 200},
        ]
        r = k.network(with_bodies=True)
        reqs = r["requests"]
        self.assertTrue(reqs[0]["has_body"])
        self.assertIn("ok", reqs[0]["body_preview"])
        self.assertFalse(reqs[1]["has_body"])
        self.assertNotIn("body_preview", reqs[1])


if __name__ == "__main__":
    unittest.main()
