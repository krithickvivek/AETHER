"""Error responses: one shape, the right status codes, and nothing private or internal in them."""

import unittest
from unittest import mock

from fastapi.testclient import TestClient

try:
    from api_support import API, BASE_URL, CALCULATOR, ApiTestCase
except ImportError as error:
    if "requirements-api.txt" in str(error):
        raise
    from tests.api_support import API, BASE_URL, CALCULATOR, ApiTestCase


class ErrorShapeTests(ApiTestCase):
    def test_every_kind_of_error_has_the_same_shape(self):
        context, entry = self.make_run(CALCULATOR)
        cases = [
            (self.get("/no-such-endpoint"), 404, "not_found"),
            (self.get("/runs"), 404, "not_found"),  # no list endpoint for runs; the list is /history
            (self.get("/runs/abcdef123456"), 404, "run_not_found"),
            (self.get("/runs/bad.id"), 404, "run_not_found"),
            (self.get("/history/nothing"), 404, "history_entry_not_found"),
            (self.get(f"/runs/{context.run_id}/files/content"), 422, "invalid_request"),
            (self.get(f"/runs/{context.run_id}/files/content?path=../x.py"), 404, "file_not_found"),  # no files yet
            (self.get(f"/runs/{context.run_id}/checkpoint?override=nonsense"), 422, "invalid_override"),
            (self.client.post(f"{API}/runs", headers={"Origin": "http://localhost:8765"}), 405, "method_not_allowed"),
            (self.client.get(f"{API}/health", headers={"Host": "evil.example"}), 403, "host_not_allowed"),
            (self.client.get(f"{API}/health", headers={"Origin": "http://evil.example"}), 403, "origin_not_allowed"),
            (self.client.delete(f"{API}/history"), 403, "origin_required"),
        ]
        for response, status, code in cases:
            with self.subTest(code=code, url=str(response.request.url)):
                self.assertTrue(response.headers["content-type"].startswith("application/json"))
                error = self.assert_error(response, status, code)
                self.assertIsInstance(error["message"], str)

    def test_a_validation_error_lists_where_the_problem_is_and_never_echoes_the_input(self):
        response = self.get("/runs/" + "a" * 65 + "/files/content", params={"path": "x" * 501})
        error = self.assert_error(response, 422, "invalid_request")
        self.assertEqual({d["location"] for d in error["details"]}, {"path.run_id", "query.path"})
        for detail in error["details"]:
            self.assertEqual(set(detail), {"location", "message"})
        self.assertNotIn("x" * 50, response.text)
        self.assertNotIn("a" * 50, response.text)

    def test_domain_errors_have_no_details_and_validation_errors_do(self):
        self.assertIsNone(self.assert_error(self.get("/runs/abcdef123456"), 404, "run_not_found")["details"])
        self.assertIsInstance(self.assert_error(self.get("/runs/a/files/content"), 422, "invalid_request")["details"], list)

    def test_a_run_that_failed_is_state_not_an_error(self):
        """Domain failures (a failed or rejected run) are 200 responses carrying their status."""
        failed, _ = self.make_run("   ")
        rejected, _ = self.make_run(CALCULATOR)
        self.reject(rejected)
        for run in (failed, rejected):
            self.assertEqual(self.get(f"/runs/{run.run_id}").status_code, 200)
            self.assertEqual(self.get(f"/runs/{run.run_id}/checkpoint").status_code, 200)
        self.assertEqual(self.get(f"/runs/{failed.run_id}").json()["status"], "failed")
        self.assertEqual(self.get(f"/runs/{rejected.run_id}").json()["status"], "cancelled")

    def test_the_documented_error_model_matches_what_is_returned(self):
        schema = self.get("/openapi.json").json()["components"]["schemas"]
        self.assertEqual(set(schema["ErrorBody"]["properties"]), {"code", "message", "details"})
        self.assertEqual(set(schema["ErrorResponse"]["properties"]), {"error"})


class UnexpectedFailureTests(ApiTestCase):
    def test_an_unexpected_exception_is_a_generic_500_with_nothing_internal_in_it(self):
        quiet = TestClient(self.app, base_url=BASE_URL, raise_server_exceptions=False)
        secret = "C:\\Users\\someone\\secret\\key.txt password=hunter2"
        with mock.patch.object(self.app.state.repository, "load_run", side_effect=RuntimeError(secret)):
            response = quiet.get(f"{API}/runs/abcdef123456")
        error = self.assert_error(response, 500, "internal_error")
        self.assertNotIn("hunter2", response.text)
        self.assertNotIn("secret", response.text)
        self.assertEqual(error["message"], "Something went wrong. Nothing was changed.")

    def test_an_unexpected_exception_in_a_presenter_is_also_generic(self):
        context, _ = self.make_run(CALCULATOR)
        quiet = TestClient(self.app, base_url=BASE_URL, raise_server_exceptions=False)
        with mock.patch("aether_api.routers.runs.run_view", side_effect=KeyError("C:\\private")):
            response = quiet.get(f"{API}/runs/{context.run_id}")
        self.assert_error(response, 500, "internal_error")
        self.assertNotIn("private", response.text)


if __name__ == "__main__":
    unittest.main()
