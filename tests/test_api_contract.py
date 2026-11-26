"""The API's contract: its dependencies are present (no skipping), its surface is exactly the approved read-only set,
and the OpenAPI document describes every endpoint and error."""

import re
import subprocess
import sys
import unittest
from importlib import metadata
from pathlib import Path

try:  # works under `discover -s tests` and under `python -m unittest tests.test_api_contract`
    from api_support import API, ApiTestCase
except ImportError as error:
    if "requirements-api.txt" in str(error):  # the missing-dependency guard: never hide it
        raise
    from tests.api_support import API, ApiTestCase

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_ENDPOINTS = {
    ("GET", f"{API}/health"),
    ("GET", f"{API}/config"),
    ("GET", f"{API}/planners/ollama/status"),
    ("GET", f"{API}/history"),
    ("GET", f"{API}/history/{{entry_id}}"),
    ("GET", f"{API}/runs/{{run_id}}"),
    ("GET", f"{API}/runs/{{run_id}}/checkpoint"),
    ("GET", f"{API}/runs/{{run_id}}/files"),
    ("GET", f"{API}/runs/{{run_id}}/files/content"),
    ("GET", f"{API}/runs/{{run_id}}/plan.md"),
}


def version_tuple(text: str) -> tuple:
    return tuple(int(part) for part in re.findall(r"\d+", text)[:3])


class DependencyGuardTests(unittest.TestCase):
    """Missing API packages must make the suite FAIL, not skip."""

    def test_requirements_file_lists_the_api_packages_with_upper_bounds(self):
        lines = [line.strip() for line in (ROOT / "requirements-api.txt").read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
        names = {re.split(r"[<>=!~]", line)[0].lower(): line for line in lines}
        self.assertEqual(set(names), {"fastapi", "uvicorn", "httpx"})
        for name, line in names.items():
            self.assertIn(">=", line, name)
            self.assertIn("<", line, f"{name} must have an upper bound (pinned to a release series)")

    def test_installed_versions_satisfy_requirements_api_txt(self):
        for line in (ROOT / "requirements-api.txt").read_text(encoding="utf-8").splitlines():
            match = re.match(r"^(\w+)>=([\d.]+),<([\d.]+)", line.strip())
            if not match:
                continue
            name, low, high = match.groups()
            installed = version_tuple(metadata.version(name))
            self.assertGreaterEqual(installed, version_tuple(low), name)
            self.assertLess(installed, version_tuple(high), name)

    def test_missing_packages_raise_an_import_error_instead_of_skipping(self):
        """Run api_support in a child process where FastAPI cannot be imported: it must raise, naming the file."""
        code = (
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT)!r}); sys.path.insert(0, {str(ROOT / 'tests')!r})\n"
            "sys.modules['fastapi'] = None\n"  # makes `import fastapi` raise ImportError
            "try:\n"
            "    import api_support\n"
            "except ImportError as error:\n"
            "    print('IMPORT-ERROR', error)\n"
            "else:\n"
            "    print('IMPORTED')\n"
        )
        done = subprocess.run([sys.executable, "-I", "-B", "-c", code], capture_output=True, text=True, timeout=60)
        self.assertIn("IMPORT-ERROR", done.stdout, done.stderr)
        self.assertIn("requirements-api.txt", done.stdout)
        self.assertNotIn("IMPORTED", done.stdout)


class ApiSurfaceTests(ApiTestCase):
    def openapi(self) -> dict:
        response = self.get("/openapi.json")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_the_api_exposes_exactly_the_approved_read_only_endpoints(self):
        found = {(method.upper(), path) for path, methods in self.openapi()["paths"].items() for method in methods}
        self.assertEqual(found, EXPECTED_ENDPOINTS)
        self.assertTrue(all(method == "GET" for method, _ in found), "Phase 2 has no write endpoint")

    def test_no_route_is_registered_for_any_method_but_get(self):
        for route in self.app.routes:
            methods = getattr(route, "methods", None)
            if methods and route.path.startswith(API):
                self.assertEqual(set(methods) - {"HEAD"}, {"GET"}, route.path)

    def test_every_endpoint_has_an_operation_id_and_documents_its_errors(self):
        operation_ids = []
        for path, methods in self.openapi()["paths"].items():
            for method, operation in methods.items():
                operation_ids.append(operation["operationId"])
                for status in ("404", "422", "503", "403"):
                    self.assertIn(status, operation["responses"], f"{method} {path} does not document {status}")
        self.assertEqual(len(operation_ids), len(set(operation_ids)), "operation ids must be unique")

    def test_the_contract_names_its_models(self):
        schemas = set(self.openapi()["components"]["schemas"])
        for name in ("ErrorResponse", "RunView", "HistoryResponse", "CheckpointPreview", "ConfigResponse",
                     "FileContentResponse", "FileListResponse", "HealthResponse", "OllamaStatusResponse", "Actions"):
            self.assertIn(name, schemas)

    def test_the_bundled_documentation_pages_are_off_so_the_api_never_loads_scripts_from_a_cdn(self):
        for path in ("/docs", "/redoc", "/docs/oauth2-redirect"):
            self.assert_error(self.client.get(path), 404, "not_found")


if __name__ == "__main__":
    unittest.main()
