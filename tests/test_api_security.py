"""Host and Origin protection (C2): localhost only, an explicit allow-list, no CORS, no write methods."""

import unittest

from fastapi.testclient import TestClient

try:
    from api_support import API, ApiTestCase
except ImportError as error:
    if "requirements-api.txt" in str(error):
        raise
    from tests.api_support import API, ApiTestCase

from aether_api import BIND_HOST, DEFAULT_PORT, DEV_PORT
from aether_api.app import create_app
from aether_api.security import SecuritySettings

HEALTH = f"{API}/health"


class HostValidationTests(ApiTestCase):
    def test_the_allowed_hosts_are_localhost_and_127_0_0_1_on_the_api_port(self):
        for host in ("localhost:8765", "127.0.0.1:8765", "LOCALHOST:8765"):
            with self.subTest(host=host):
                self.assertEqual(self.client.get(HEALTH, headers={"Host": host}).status_code, 200)

    def test_other_hosts_are_refused_with_the_structured_error(self):
        for host in ("evil.example", "evil.example:8765", "localhost", "localhost:80", "localhost:8000",
                     "localhost:5173", "127.0.0.1:5173", "localhost:8765.evil.example", "0.0.0.0:8765",
                     "[::1]:8765", "localhost:8765, evil.example", ""):
            with self.subTest(host=host):
                response = self.client.get(HEALTH, headers={"Host": host})
                self.assert_error(response, 403, "host_not_allowed")

    def test_testserver_the_default_test_host_is_refused_too(self):
        stranger = TestClient(self.app)  # Host: testserver
        self.assert_error(stranger.get(HEALTH), 403, "host_not_allowed")

    def test_the_host_check_comes_before_routing_so_unknown_paths_are_refused_too(self):
        response = self.client.get(f"{API}/no-such-path", headers={"Host": "evil.example"})
        self.assert_error(response, 403, "host_not_allowed")

    def test_the_host_check_covers_every_endpoint(self):
        for path in ("/health", "/config", "/history", "/history/x", "/runs/abc", "/runs/abc/files",
                     "/runs/abc/files/content?path=a.py", "/runs/abc/plan.md", "/runs/abc/checkpoint",
                     "/planners/ollama/status", "/openapi.json"):
            with self.subTest(path=path):
                self.assert_error(self.get(path, headers={"Host": "evil.example"}), 403, "host_not_allowed")

    def test_the_api_binds_only_to_the_loopback_address(self):
        self.assertEqual(BIND_HOST, "127.0.0.1")
        self.assertEqual(DEFAULT_PORT, 8765)
        # There is no command-line way to choose another host: the option does not exist.
        import contextlib
        import io

        from aether_api.__main__ import main
        with contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as exit_info:
            main(["--host", "0.0.0.0", "--dump-openapi"])
        self.assertEqual(exit_info.exception.code, 2)
        self.assertIn("unrecognized arguments", errors.getvalue())


class OriginValidationTests(ApiTestCase):
    def test_a_get_without_an_origin_is_normal_and_allowed(self):
        self.assertEqual(self.client.get(HEALTH).status_code, 200)  # curl, the browser address bar, same-origin fetch

    def test_a_get_from_one_of_our_own_origins_is_allowed(self):
        for origin in ("http://localhost:8765", "http://127.0.0.1:8765"):
            with self.subTest(origin=origin):
                self.assertEqual(self.client.get(HEALTH, headers={"Origin": origin}).status_code, 200)

    def test_a_get_from_any_other_origin_is_refused(self):
        for origin in ("http://evil.example", "https://localhost:8765", "http://localhost:5173",
                       "http://localhost", "null", "http://localhost:8765.evil.example", "file://"):
            with self.subTest(origin=origin):
                self.assert_error(self.client.get(HEALTH, headers={"Origin": origin}), 403, "origin_not_allowed")

    def test_every_method_but_get_needs_an_allowed_origin_even_without_a_body(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                response = self.client.request(method, f"{API}/history")  # bodyless, no Origin
                self.assert_error(response, 403, "origin_required")
        self.assertEqual(self.client.head(f"{API}/history").status_code, 403)  # a HEAD has no body to inspect

    def test_other_methods_from_a_foreign_origin_are_refused_as_such(self):
        for method in ("POST", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                response = self.client.request(method, f"{API}/history", headers={"Origin": "http://evil.example"})
                self.assert_error(response, 403, "origin_not_allowed")

    def test_other_methods_from_an_allowed_origin_get_405_because_this_phase_is_read_only(self):
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                response = self.client.request(method, f"{API}/history", headers={"Origin": "http://localhost:8765"})
                self.assert_error(response, 405, "method_not_allowed")
                self.assertEqual(response.headers["allow"], "GET")

    def test_the_planned_write_endpoints_do_not_exist_yet(self):
        origin = {"Origin": "http://localhost:8765"}
        for method, path in (("POST", "/runs"), ("POST", "/runs/abc/decision"), ("DELETE", "/history")):
            with self.subTest(path=path):
                self.assert_error(self.client.request(method, API + path, headers=origin), 405, "method_not_allowed")

    def test_no_cors_headers_are_ever_sent(self):
        for headers in ({}, {"Origin": "http://localhost:8765"}):
            response = self.client.get(HEALTH, headers=headers)
            self.assertFalse([h for h in response.headers if h.lower().startswith("access-control-")])
        refused = self.client.get(HEALTH, headers={"Origin": "http://evil.example"})
        self.assertFalse([h for h in refused.headers if h.lower().startswith("access-control-")])
        preflight = self.client.options(HEALTH, headers={"Origin": "http://evil.example",
                                                         "Access-Control-Request-Method": "POST"})
        self.assertEqual(preflight.status_code, 403)
        self.assertFalse([h for h in preflight.headers if h.lower().startswith("access-control-")])


class DevelopmentModeTests(ApiTestCase):
    """--dev accepts the Vite development server (localhost:5173) and nothing else new."""

    settings = SecuritySettings(port=8765, dev=True)

    def test_the_vite_host_and_origin_are_allowed_in_development_only(self):
        self.assertEqual(self.client.get(HEALTH, headers={"Host": f"localhost:{DEV_PORT}"}).status_code, 200)
        self.assertEqual(self.client.get(HEALTH, headers={"Host": f"127.0.0.1:{DEV_PORT}"}).status_code, 200)
        self.assertEqual(self.client.get(HEALTH, headers={"Origin": f"http://localhost:{DEV_PORT}"}).status_code, 200)

    def test_other_ports_and_hosts_are_still_refused_in_development(self):
        for host in ("localhost:5174", "localhost:3000", "localhost:8000", "evil.example:5173"):
            with self.subTest(host=host):
                self.assert_error(self.client.get(HEALTH, headers={"Host": host}), 403, "host_not_allowed")
        for origin in ("http://localhost:5174", "https://localhost:5173", "http://evil.example:5173"):
            with self.subTest(origin=origin):
                self.assert_error(self.client.get(HEALTH, headers={"Origin": origin}), 403, "origin_not_allowed")

    def test_development_does_not_make_writes_possible(self):
        response = self.client.post(f"{API}/runs", headers={"Origin": f"http://localhost:{DEV_PORT}"})
        self.assert_error(response, 405, "method_not_allowed")

    def test_the_vite_host_is_refused_when_not_in_development(self):
        production = TestClient(create_app(SecuritySettings(dev=False)), base_url="http://localhost:8765")
        self.assert_error(production.get(HEALTH, headers={"Host": "localhost:5173"}), 403, "host_not_allowed")
        self.assert_error(production.get(HEALTH, headers={"Origin": "http://localhost:5173"}), 403,
                          "origin_not_allowed")


class CustomPortTests(unittest.TestCase):
    def test_the_allow_list_follows_the_chosen_port(self):
        client = TestClient(create_app(SecuritySettings(port=9100)), base_url="http://localhost:9100")
        self.assertEqual(client.get(HEALTH).status_code, 200)
        self.assertEqual(client.get(HEALTH, headers={"Host": "127.0.0.1:9100"}).status_code, 200)
        self.assertEqual(client.get(HEALTH, headers={"Host": "localhost:8765"}).status_code, 403)

    def test_the_settings_list_exactly_the_expected_hosts_and_origins(self):
        settings = SecuritySettings(port=8765, dev=True)
        self.assertEqual(settings.allowed_hosts, {"localhost:8765", "127.0.0.1:8765", "localhost:5173",
                                                  "127.0.0.1:5173"})
        self.assertEqual(settings.allowed_origins, {f"http://{h}" for h in settings.allowed_hosts})
        self.assertEqual(SecuritySettings().allowed_hosts, {"localhost:8765", "127.0.0.1:8765"})


if __name__ == "__main__":
    unittest.main()
