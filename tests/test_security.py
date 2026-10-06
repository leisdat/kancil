"""Test keamanan server lokal: auth token + Host check (temuan audit 3.1)."""
import unittest


def _key():
    from kancil import agent_key
    return agent_key.get_or_create()


class CheckRequestTest(unittest.TestCase):
    def test_header_token_ok(self):
        from kancil import agent_key
        ok, _ = agent_key.check_request(
            {"Host": "127.0.0.1:8901", "X-Kancil-Key": _key()}, {})
        self.assertTrue(ok)

    def test_query_key_ok(self):
        from kancil import agent_key
        ok, _ = agent_key.check_request(
            {"host": "localhost:8901"}, {"key": _key()})
        self.assertTrue(ok)

    def test_wrong_token_rejected(self):
        from kancil import agent_key
        ok, why = agent_key.check_request(
            {"Host": "127.0.0.1"}, {"key": "salah"})
        self.assertFalse(ok)
        self.assertIn("token", why)

    def test_missing_token_rejected(self):
        from kancil import agent_key
        ok, _ = agent_key.check_request({"Host": "127.0.0.1"}, {})
        self.assertFalse(ok)

    def test_dns_rebinding_rejected(self):
        # Host: evil.com yang me-resolve ke 127.0.0.1 -> tolak
        from kancil import agent_key
        ok, why = agent_key.check_request(
            {"Host": "evil.com", "X-Kancil-Key": _key()}, {})
        self.assertFalse(ok)
        self.assertIn("loopback", why)

    def test_foreign_origin_rejected(self):
        from kancil import agent_key
        ok, _ = agent_key.check_request(
            {"Host": "127.0.0.1", "Origin": "https://evil.com",
             "X-Kancil-Key": _key()}, {})
        self.assertFalse(ok)

    def test_localhost_origin_ok(self):
        from kancil import agent_key
        ok, _ = agent_key.check_request(
            {"Host": "127.0.0.1",
             "Origin": "http://127.0.0.1:8901",
             "X-Kancil-Key": _key()}, {})
        self.assertTrue(ok)


class RouteAgentAuthTest(unittest.TestCase):
    def test_register_no_token_401(self):
        from kancil.agent_bridge import route_agent
        r = route_agent("/__kancil__/agent/register", {},
                        {"tab": "t"}, headers={"Host": "127.0.0.1"})
        self.assertIsNotNone(r)
        self.assertEqual(r[0], 401)

    def test_register_with_key_200(self):
        from kancil.agent_bridge import route_agent
        r = route_agent("/__kancil__/agent/register", {"key": _key()},
                        {"tab": "t-auth"}, headers={"Host": "127.0.0.1"})
        self.assertEqual(r[0], 200)

    def test_poll_no_token_401(self):
        from kancil.agent_bridge import route_agent
        r = route_agent("/__kancil__/agent/poll", {"tab": "t"},
                        {}, headers={"Host": "127.0.0.1"})
        self.assertEqual(r[0], 401)

    def test_agent_js_public(self):
        # agent.js statis: boleh publik (tidak ada data sensitif)
        from kancil.agent_bridge import route_agent
        r = route_agent("/__kancil__/agent.js", {}, {},
                        headers={"Host": "127.0.0.1"})
        self.assertEqual(r[0], 200)

    def test_rebinding_cannot_poll(self):
        from kancil.agent_bridge import route_agent
        r = route_agent("/__kancil__/agent/poll",
                        {"tab": "t", "key": _key()}, {},
                        headers={"Host": "evil.com"})
        self.assertEqual(r[0], 401)


class GatewayKeyTest(unittest.TestCase):
    def test_rewrite_html_embeds_key(self):
        from kancil.viewer import rewrite_html
        out = rewrite_html('<a href="https://ex.com/x">y</a>',
                           "https://ex.com/", key="K123")
        self.assertIn("/__kancil__/go?u=", out)
        self.assertIn("key=K123", out)

    def test_rewrite_html_no_key(self):
        from kancil.viewer import rewrite_html
        out = rewrite_html('<a href="https://ex.com/x">y</a>',
                           "https://ex.com/")
        self.assertIn("/__kancil__/go?u=", out)
        self.assertNotIn("key=", out)

    def test_inject_agent_carries_key(self):
        from kancil.proxy_server import inject_agent
        out = inject_agent("<html><body></body></html>",
                           "https://ex.com/", "http://127.0.0.1:8080/__kancil__")
        s = out.decode()
        self.assertIn("agent.js?key=", s)
        self.assertIn(_key(), s)


class CaKeyPermTest(unittest.TestCase):
    def test_write_private_0600(self):
        import os, stat, tempfile
        from kancil.proxy_server import _write_private, ca_dir
        d = ca_dir()
        st = os.stat(d)
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o700)
        p = os.path.join(tempfile.gettempdir(), "kancil-test.key")
        try:
            _write_private(p, "x")
            st = os.stat(p)
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        finally:
            try:
                os.remove(p)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
