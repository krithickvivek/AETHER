"""GET /api/v1/jobs/{job_id} (task 014.2): the job status endpoint, the app's registry, lifespan shutdown, restarts.

Jobs here are SYNTHETIC (submitted straight to the registry): no endpoint creates a job yet. Everything uses
temporary stores, a blocked network, and gates so each test decides when a job is in flight."""

import logging
import threading
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

try:
    from api_support import API, BASE_URL, ApiTestCase, snapshot_tree
except ImportError as error:
    if "requirements-api.txt" in str(error):
        raise
    from tests.api_support import API, BASE_URL, ApiTestCase, snapshot_tree

from aether_api.app import create_app
from aether_api.jobs import JobFailure, JobOutcome, JobRegistry, LastEvent, ProgressStep

JOB_STATES = {"running", "succeeded", "failed", "interrupted"}


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class JobsApiTestCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        logger = logging.getLogger("aether_api.jobs")  # expected warnings (interruptions) stay out of the test output
        handler = logging.NullHandler()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.registry = JobRegistry(shutdown_grace=0.3)
        self.app = create_app(self.settings, job_registry=self.registry)
        self.client = TestClient(self.app, base_url=BASE_URL)
        self.addCleanup(self.registry.shutdown, 3.0)

    def gated_job(self, kind="plan", **kwargs):
        release = threading.Event()
        started = threading.Event()
        self.addCleanup(release.set)

        def work(ctx):
            ctx.set_run_id("abc123def456")
            ctx.set_progress("planning", steps=[ProgressStep("Requirement", "done", "3 features"),
                                                ProgressStep("Plan", "current"), ProgressStep("Approval"),
                                                ProgressStep("Generated files")],
                             event_count=1, last_event=LastEvent("Manager", "Understand requirement", "done"))
            started.set()
            release.wait(10)
            return JobOutcome(run_status="awaiting_approval", ok=True, fallback_reason="model unavailable")

        job = self.registry.submit(kind, work, **kwargs).job
        self.assertTrue(started.wait(5))
        return job, release

    def get_job(self, job_id: str):
        return self.get(f"/jobs/{job_id}")


class JobStatusTests(JobsApiTestCase):
    def test_a_running_job(self):
        job, _ = self.gated_job()
        response = self.get_job(job.job_id)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("application/json"))
        self.assertEqual(response.headers["cache-control"], "no-store")
        body = response.json()
        self.assertEqual((body["job_id"], body["kind"], body["state"]), (job.job_id, "plan", "running"))
        self.assertEqual(body["instance_id"], self.registry.instance_id)
        self.assertEqual(body["run_id"], "abc123def456")
        self.assertRegex(body["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertEqual((body["finished_at"], body["duration_ms"], body["result"], body["error"]), (None, None, None, None))
        self.assertEqual(body["progress"]["stage"], "planning")
        self.assertEqual([(s["title"], s["status"], s["detail"]) for s in body["progress"]["steps"]],
                         [("Requirement", "done", "3 features"), ("Plan", "current", ""), ("Approval", "pending", ""),
                          ("Generated files", "pending", "")])
        self.assertEqual(body["progress"]["event_count"], 1)
        self.assertEqual(body["progress"]["last_event"], {"agent": "Manager", "action": "Understand requirement",
                                                         "status": "done"})
        self.assert_no_private_text(response)

    def test_the_response_has_exactly_the_documented_fields(self):
        job, _ = self.gated_job()
        body = self.get_job(job.job_id).json()
        self.assertEqual(set(body), {"job_id", "kind", "state", "run_id", "instance_id", "created_at", "started_at",
                                     "finished_at", "duration_ms", "progress", "result", "error"})
        self.assertEqual(set(body["progress"]), {"stage", "steps", "event_count", "last_event"})

    def test_a_succeeded_job_reports_its_result(self):
        job, release = self.gated_job("approve", run_id="abc123def456")
        release.set()
        self.assertTrue(wait_until(lambda: self.get_job(job.job_id).json()["state"] == "succeeded"))
        body = self.get_job(job.job_id).json()
        self.assertEqual(body["result"], {"run_status": "awaiting_approval", "ok": True, "ai_generated": False,
                                          "fallback_reason": "model unavailable", "files_count": None, "warnings": []})
        self.assertIsNone(body["error"])
        self.assertIsNotNone(body["finished_at"])
        self.assertIsInstance(body["duration_ms"], float)
        self.assertEqual(body["progress"]["stage"], "finished")

    def test_a_failed_job_reports_a_safe_error_and_no_result(self):
        job = self.registry.submit("plan", lambda ctx: (_ for _ in ()).throw(
            JobFailure("storage_unavailable", "The data could not be read from disk."))).job
        self.assertTrue(wait_until(lambda: self.get_job(job.job_id).json()["state"] == "failed"))
        body = self.get_job(job.job_id).json()
        self.assertEqual(body["error"], {"code": "storage_unavailable", "message": "The data could not be read from disk."})
        self.assertIsNone(body["result"])

    def test_an_unexpected_exception_never_reaches_the_response(self):
        def work(ctx):
            raise RuntimeError(f"secret {Path.home()}\\key.txt password=hunter2")

        with self.assertLogs("aether_api.jobs", level="ERROR"):
            job = self.registry.submit("plan", work).job
            self.assertTrue(wait_until(lambda: self.get_job(job.job_id).json()["state"] == "failed"))
        response = self.get_job(job.job_id)
        self.assertEqual(response.json()["error"]["code"], "internal_error")
        self.assertNotIn("hunter2", response.text)
        self.assertNotIn("secret", response.text)
        self.assert_no_private_text(response)

    def test_a_message_from_an_operation_is_scrubbed_of_the_home_folder(self):
        def work(ctx):
            raise JobFailure("storage_unavailable", f"Could not read {Path.home()}\\AETHER-projects\\x")

        job = self.registry.submit("plan", work).job
        self.assertTrue(wait_until(lambda: self.get_job(job.job_id).json()["state"] == "failed"))
        response = self.get_job(job.job_id)
        self.assertIn("~", response.json()["error"]["message"])
        self.assert_no_private_text(response)

    def test_an_interrupted_job_is_never_reported_successful(self):
        job, release = self.gated_job()
        with self.assertLogs("aether_api.jobs", level="WARNING"):
            self.registry.shutdown(0)
        release.set()  # the operation finishes later; it must not change what the API reports
        time.sleep(0.1)
        body = self.get_job(job.job_id).json()
        self.assertEqual(body["state"], "interrupted")
        self.assertIsNone(body["result"])
        self.assertEqual(body["error"]["code"], "server_shutdown")
        self.assertIsNotNone(body["finished_at"])

    def test_every_state_value_is_a_documented_one(self):
        job, release = self.gated_job()
        states = {self.get_job(job.job_id).json()["state"]}
        release.set()
        wait_until(lambda: self.get_job(job.job_id).json()["state"] != "running")
        states.add(self.get_job(job.job_id).json()["state"])
        self.assertTrue(states <= JOB_STATES, states)

    def test_polling_while_a_job_changes_always_gives_a_valid_view(self):
        job, release = self.gated_job()
        problems, seen = [], set()

        def poll():
            client = TestClient(self.app, base_url=BASE_URL)
            for _ in range(60):
                response = client.get(f"{API}/jobs/{job.job_id}")
                if response.status_code != 200 or response.json()["state"] not in JOB_STATES:
                    problems.append((response.status_code, response.text[:100]))
                else:
                    seen.add(response.json()["state"])

        threads = [threading.Thread(target=poll) for _ in range(4)]
        [t.start() for t in threads]
        time.sleep(0.05)
        release.set()
        [t.join(30) for t in threads]
        self.assertEqual(problems, [])
        self.assertIn("succeeded", seen | {self.get_job(job.job_id).json()["state"]})


class JobNotFoundTests(JobsApiTestCase):
    def test_an_unknown_job_is_a_404_that_names_the_current_instance(self):
        error = self.assert_error(self.get_job("0" * 32), 404, "job_not_found")
        self.assertEqual(error["message"], "There is no job with this id.")
        self.assertEqual(error["details"]["instance_id"], self.registry.instance_id)
        self.assertIn("GET /runs/{run_id}", error["details"]["hint"])

    def test_odd_ids_are_a_404_and_never_a_crash(self):
        for job_id in ("not-a-job", "a%20b", "caf%C3%A9", "%2e%2e", "DROP%20TABLE", "0" * 33, "x"):
            with self.subTest(job_id=job_id):
                self.assert_error(self.get_job(job_id), 404, "job_not_found")

    def test_an_id_that_is_too_long_is_an_invalid_request(self):
        error = self.assert_error(self.get_job("a" * 65), 422, "invalid_request")
        self.assertEqual(error["details"][0]["location"], "path.job_id")

    def test_a_purged_job_is_a_404(self):
        registry = JobRegistry(max_finished=0)
        self.addCleanup(registry.shutdown, 3.0)
        client = TestClient(create_app(self.settings, job_registry=registry), base_url=BASE_URL)
        job = registry.submit("plan", lambda ctx: None).job
        self.assertTrue(wait_until(lambda: client.get(f"{API}/jobs/{job.job_id}").status_code == 404))

    def test_a_job_in_flight_is_never_purged(self):
        registry = JobRegistry(max_finished=0, finished_ttl=0)
        self.addCleanup(registry.shutdown, 3.0)
        client = TestClient(create_app(self.settings, job_registry=registry), base_url=BASE_URL)
        release = threading.Event()
        self.addCleanup(release.set)
        job = registry.submit("plan", lambda ctx: release.wait(10)).job
        for _ in range(20):
            self.assertEqual(client.get(f"{API}/jobs/{job.job_id}").json()["state"], "running")


class InstanceIdentityApiTests(JobsApiTestCase):
    def test_a_job_id_from_an_earlier_server_instance_is_not_found_by_the_new_one(self):
        old_job = self.registry.submit("plan", lambda ctx: JobOutcome(ok=True)).job
        self.assertTrue(wait_until(lambda: self.get_job(old_job.job_id).json()["state"] == "succeeded"))
        old_instance = self.registry.instance_id

        restarted_registry = JobRegistry()  # a "restart": a fresh app with a fresh registry
        self.addCleanup(restarted_registry.shutdown, 3.0)
        restarted = TestClient(create_app(self.settings, job_registry=restarted_registry), base_url=BASE_URL)
        new_job = restarted_registry.submit("plan", lambda ctx: JobOutcome(ok=True)).job

        gone = restarted.get(f"{API}/jobs/{old_job.job_id}")
        self.assertEqual(gone.status_code, 404)
        self.assertEqual(gone.json()["error"]["code"], "job_not_found")
        self.assertNotEqual(gone.json()["error"]["details"]["instance_id"], old_instance)
        self.assertEqual(gone.json()["error"]["details"]["instance_id"], restarted_registry.instance_id)
        found = restarted.get(f"{API}/jobs/{new_job.job_id}")
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()["instance_id"], restarted_registry.instance_id)
        self.assertNotEqual(found.json()["instance_id"], old_instance)
        self.assertNotEqual(old_job.job_id, new_job.job_id)

    def test_two_apps_never_share_jobs(self):
        other_registry = JobRegistry()
        self.addCleanup(other_registry.shutdown, 3.0)
        other = TestClient(create_app(self.settings, job_registry=other_registry), base_url=BASE_URL)
        mine = self.registry.submit("plan", lambda ctx: JobOutcome(ok=True)).job
        theirs = other_registry.submit("plan", lambda ctx: JobOutcome(ok=True)).job
        self.assertEqual(self.get_job(mine.job_id).status_code, 200)
        self.assertEqual(other.get(f"{API}/jobs/{mine.job_id}").status_code, 404)
        self.assertEqual(other.get(f"{API}/jobs/{theirs.job_id}").status_code, 200)
        self.assertEqual(self.get_job(theirs.job_id).status_code, 404)

    def test_a_default_app_gets_its_own_registry_with_a_fresh_instance_id(self):
        first, second = create_app(self.settings), create_app(self.settings)
        for app in (first, second):
            self.addCleanup(app.state.jobs.shutdown, 3.0)
        self.assertNotEqual(first.state.jobs.instance_id, second.state.jobs.instance_id)
        self.assertEqual((first.state.jobs.max_jobs, first.state.jobs.max_ollama_jobs), (2, 1))


class LifespanShutdownTests(JobsApiTestCase):
    def test_leaving_the_app_stops_admission_and_interrupts_jobs_that_outlive_the_grace_period(self):
        job, release = self.gated_job()
        with self.assertLogs("aether_api.jobs", level="WARNING"):
            with TestClient(self.app, base_url=BASE_URL) as live:  # entering runs startup, leaving runs shutdown
                self.assertEqual(live.get(f"{API}/jobs/{job.job_id}").json()["state"], "running")
                self.assertFalse(self.registry.shutting_down)
        self.assertTrue(self.registry.shutting_down)
        release.set()
        body = self.get_job(job.job_id).json()
        self.assertEqual((body["state"], body["result"], body["error"]["code"]), ("interrupted", None, "server_shutdown"))

    def test_a_job_that_finishes_inside_the_grace_period_is_not_interrupted(self):
        registry = JobRegistry(shutdown_grace=5)
        app = create_app(self.settings, job_registry=registry)
        release = threading.Event()
        self.addCleanup(release.set)
        job = registry.submit("plan", lambda ctx: (release.wait(10), JobOutcome(ok=True, run_status="awaiting_approval"))[1]).job
        threading.Timer(0.2, release.set).start()
        with TestClient(app, base_url=BASE_URL):
            pass
        snapshot = registry.get(job.job_id)
        self.assertEqual(snapshot.state, "succeeded")

    def test_reads_still_work_after_shutdown_began(self):
        self.registry.shutdown(0)
        for path in ("/health", "/config", "/history"):
            self.assertEqual(self.get(path).status_code, 200, path)
        self.assert_error(self.get_job("0" * 32), 404, "job_not_found")


class JobEndpointContractTests(JobsApiTestCase):
    def test_the_endpoint_is_in_the_openapi_contract(self):
        document = self.get("/openapi.json").json()
        operation = document["paths"][f"{API}/jobs/{{job_id}}"]["get"]
        self.assertEqual(operation["operationId"], "getJob")
        for status in ("200", "403", "404", "422", "503"):
            self.assertIn(status, operation["responses"])
        schemas = document["components"]["schemas"]
        for name in ("JobView", "JobProgress", "JobResult", "JobError", "LastEvent"):
            self.assertIn(name, schemas)
        self.assertEqual(set(schemas["JobView"]["properties"]),
                         {"job_id", "kind", "state", "run_id", "instance_id", "created_at", "started_at", "finished_at",
                          "duration_ms", "progress", "result", "error"})

    def test_the_state_kind_and_stage_values_are_closed_sets(self):
        schemas = self.get("/openapi.json").json()["components"]["schemas"]
        self.assertEqual(set(schemas["JobView"]["properties"]["state"]["enum"]),
                         {"queued", "running", "succeeded", "failed", "interrupted"})
        self.assertEqual(set(schemas["JobView"]["properties"]["kind"]["enum"]), {"plan", "approve"})
        self.assertEqual(set(schemas["JobProgress"]["properties"]["stage"]["enum"]),
                         {"understanding", "planning", "awaiting_approval", "generating", "finished"})

    def test_no_write_method_exists_on_the_jobs_path(self):
        origin = {"Origin": "http://localhost:8765"}
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                self.assert_error(self.client.request(method, f"{API}/jobs/{'0' * 32}", headers=origin), 405,
                                  "method_not_allowed")

    def test_the_host_and_origin_checks_cover_the_new_endpoint(self):
        job, _ = self.gated_job()
        self.assert_error(self.client.get(f"{API}/jobs/{job.job_id}", headers={"Host": "evil.example"}), 403,
                          "host_not_allowed")
        self.assert_error(self.client.get(f"{API}/jobs/{job.job_id}", headers={"Origin": "http://evil.example"}), 403,
                          "origin_not_allowed")
        self.assertEqual(self.client.get(f"{API}/jobs/{job.job_id}", headers={"Origin": "http://localhost:8765"})
                         .status_code, 200)

    def test_reading_a_job_changes_nothing_on_disk(self):
        job, release = self.gated_job()
        self.make_run()
        before = snapshot_tree(self.base)
        for _ in range(5):
            self.get_job(job.job_id)
            self.get_job("0" * 32)
        release.set()
        self.assertEqual(snapshot_tree(self.base), before)
        self.assertFalse((self.runs / ".locks").exists(), "no lock folder: jobs do not touch the run store")

    def test_existing_endpoints_are_unchanged_by_the_registry(self):
        self.make_run()
        for path in ("/health", "/config", "/history", "/planners/ollama/status"):
            self.assertEqual(self.get(path).status_code, 200, path)
        self.assertEqual(self.get("/health").json(), {"status": "ok", "api_version": "v1", "schema_version": 1,
                                                      "read_only": True})


if __name__ == "__main__":
    unittest.main()
