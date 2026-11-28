"""The in-memory job registry (task 014.2): states, admission, limits under simultaneous requests, idempotency,
retention, shutdown, worker failure, mutex discipline and instance identity.

Pure standard library (the registry imports no FastAPI). Operations are controlled with Gate objects, so every test
decides exactly when a job is "in flight". Concurrency tests use start barriers so all threads fire together."""

import ast
import logging
import re
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from aether_api import jobs as J
from aether_api.jobs import (
    FAILED, INTERRUPTED, RUNNING, SUCCEEDED, IdempotencyKeyReused, JobFailure, JobOutcome, JobRegistry,
    JobsShuttingDown, JobStartFailed, LastEvent, PlannerBusy, ProgressStep, RunBusy, TooManyJobs,
)

JOBS_SOURCE = Path(J.__file__)
HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX12 = re.compile(r"^[0-9a-f]{12}$")


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.002) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class Gate:
    """An operation the test controls: it blocks until released, and records how many ran at the same time."""

    def __init__(self, outcome: JobOutcome | None = None):
        self.release = threading.Event()
        self.outcome = outcome or JobOutcome(run_status="awaiting_approval", ok=True)
        self._lock = threading.Lock()
        self.entered_count = 0
        self.current = 0
        self.maximum = 0
        self.calls = 0
        self.entered = threading.Semaphore(0)

    def work(self, ctx):
        with self._lock:
            self.calls += 1
            self.entered_count += 1
            self.current += 1
            self.maximum = max(self.maximum, self.current)
        self.entered.release()
        try:
            self.release.wait(20)
        finally:
            with self._lock:
                self.current -= 1
        return self.outcome

    def wait_entered(self, count: int = 1, timeout: float = 5.0) -> bool:
        return wait_until(lambda: self.entered_count >= count, timeout)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float):
        self.now += seconds


def quick(outcome=None):
    outcome = outcome or JobOutcome(run_status="completed", ok=True)
    return lambda ctx: outcome


class RegistryTestCase(unittest.TestCase):
    def setUp(self):
        # The registry logs warnings and errors on purpose (interruptions, failures). Keep them out of the test output;
        # assertLogs still sees them.
        logger = logging.getLogger("aether_api.jobs")
        handler = logging.NullHandler()
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)

    def registry(self, **kwargs) -> JobRegistry:
        kwargs.setdefault("shutdown_grace", 0.5)
        registry = JobRegistry(**kwargs)
        self.addCleanup(registry.shutdown, 3.0)  # cleanups run last-in-first-out: gates are released before this runs
        return registry

    def gate(self, outcome=None) -> Gate:
        gate = Gate(outcome)
        self.addCleanup(gate.release.set)
        return gate

    def finish(self, registry: JobRegistry, job_id: str, state: str = SUCCEEDED, timeout: float = 5.0):
        self.assertTrue(wait_until(lambda: registry.get(job_id).state == state, timeout),
                        f"job did not reach {state}: {registry.get(job_id)}")
        return registry.get(job_id)


def race(count: int, call):
    """Run `call(i)` on `count` threads released together by a barrier. Returns [(result_or_exception), ...]."""
    barrier = threading.Barrier(count)
    results = [None] * count

    def runner(index):
        barrier.wait(10)
        try:
            results[index] = call(index)
        except Exception as error:  # the callers want to see which exception type each thread got
            results[index] = error

    threads = [threading.Thread(target=runner, args=(i,)) for i in range(count)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    return results


# ====================================================================== basics

class BasicsTests(RegistryTestCase):
    def test_a_new_job_is_running_and_identified(self):
        registry, gate = self.registry(), None
        gate = self.gate()
        admission = registry.submit("plan", gate.work)
        job = admission.job
        self.assertFalse(admission.replayed)
        self.assertEqual((job.kind, job.state, job.run_id), ("plan", RUNNING, None))
        self.assertRegex(job.job_id, HEX32)
        self.assertEqual(job.instance_id, registry.instance_id)
        self.assertRegex(job.created_at, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertIsNone(job.finished_at)
        self.assertIsNone(job.result)
        self.assertIsNone(job.error)

    def test_the_initial_progress_depends_on_the_kind(self):
        registry, gate = self.registry(), self.gate()
        plan = registry.submit("plan", gate.work).job.progress
        approve = registry.submit("approve", gate.work, run_id="r1").job.progress
        self.assertEqual(plan.stage, "understanding")
        self.assertEqual(approve.stage, "generating")
        for progress in (plan, approve):
            self.assertEqual([s.title for s in progress.steps], ["Requirement", "Plan", "Approval", "Generated files"])
            self.assertEqual({s.status for s in progress.steps}, {"pending"})
            self.assertEqual((progress.event_count, progress.last_event), (0, None))

    def test_job_ids_are_random_128_bit_hex_and_never_repeat(self):
        registry = self.registry(max_jobs=1000, finished_ttl=3600, max_finished=1000)
        ids = [registry.submit("plan", quick()).job.job_id for _ in range(400)]
        self.assertEqual(len(set(ids)), 400)
        self.assertTrue(all(HEX32.match(i) for i in ids))

    def test_bad_arguments_are_rejected(self):
        registry = self.registry()
        with self.assertRaises(ValueError):
            registry.submit("explode", quick())
        with self.assertRaises(ValueError):
            registry.submit("plan", quick(), idempotency_key="k")  # a key needs a request hash
        for bad in ({"max_jobs": 0}, {"max_ollama_jobs": 0}, {"max_finished": -1}, {"finished_ttl": -1}):
            with self.assertRaises(ValueError):
                JobRegistry(**bad)

    def test_the_defaults_are_the_approved_limits(self):
        registry = JobRegistry()
        self.assertEqual((registry.max_jobs, registry.max_ollama_jobs, registry.finished_ttl, registry.max_finished,
                          registry.shutdown_grace), (2, 1, 3600.0, 200, 5.0))


# ====================================================================== lifecycle and states

class LifecycleTests(RegistryTestCase):
    def test_a_successful_job(self):
        registry = self.registry()
        outcome = JobOutcome(run_status="awaiting_approval", ok=True, ai_generated=False, files_count=None,
                             warnings=("history_not_saved",))
        job = self.finish(registry, registry.submit("plan", quick(outcome)).job.job_id)
        self.assertEqual(job.result, outcome)
        self.assertIsNone(job.error)
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.finished_at)
        self.assertIsInstance(job.duration_ms, float)
        self.assertGreaterEqual(job.duration_ms, 0)
        self.assertEqual(job.progress.stage, "finished")
        registry.check_invariants()

    def test_an_operation_that_returns_nothing_still_succeeds_with_an_empty_result(self):
        registry = self.registry()
        job = self.finish(registry, registry.submit("plan", lambda ctx: None).job.job_id)
        self.assertEqual(job.result, JobOutcome())

    def test_a_job_failure_carries_its_code_and_message_and_no_result(self):
        registry = self.registry()

        def work(ctx):
            raise JobFailure("storage_unavailable", "The data could not be read from disk.")

        job = self.finish(registry, registry.submit("approve", work, run_id="r1").job.job_id, FAILED)
        self.assertEqual((job.error.code, job.error.message), ("storage_unavailable", "The data could not be read from disk."))
        self.assertIsNone(job.result)
        self.assertEqual(job.progress.stage, "finished")

    def test_an_unexpected_exception_fails_the_job_with_a_generic_message_and_logs_the_traceback(self):
        registry = self.registry()

        def work(ctx):
            raise RuntimeError("secret C:\\Users\\someone\\key.txt password=hunter2")

        with self.assertLogs("aether_api.jobs", level="ERROR") as logs:
            job = self.finish(registry, registry.submit("plan", work).job.job_id, FAILED)
        self.assertEqual((job.error.code, job.error.message), ("internal_error", J.INTERNAL_ERROR_MESSAGE))
        self.assertNotIn("hunter2", repr(job))
        self.assertNotIn("someone", repr(job))
        self.assertIn("hunter2", "\n".join(logs.output), "the traceback goes to the server log only")
        self.assertIsNone(job.result)

    def test_even_SystemExit_and_KeyboardInterrupt_inside_a_worker_end_the_job(self):
        registry = self.registry()
        for exception in (SystemExit(3), KeyboardInterrupt()):
            def work(ctx, exception=exception):
                raise exception

            with self.assertLogs("aether_api.jobs", level="ERROR"):
                job = self.finish(registry, registry.submit("plan", work).job.job_id, FAILED)
            self.assertEqual(job.error.code, "internal_error")
        self.assertEqual(registry.stats()["active"], 0)
        registry.check_invariants()

    def test_the_run_id_and_progress_are_visible_while_the_job_runs(self):
        registry, ready, proceed = self.registry(), threading.Event(), threading.Event()
        self.addCleanup(proceed.set)

        def work(ctx):
            ctx.set_run_id("abc123def456")
            ctx.set_progress("planning", steps=[ProgressStep("Requirement", "done", "3 features"),
                                                ProgressStep("Plan", "current"), ProgressStep("Approval"),
                                                ProgressStep("Generated files")],
                             event_count=1, last_event=LastEvent("Manager", "Understand requirement", "done"))
            ready.set()
            proceed.wait(10)
            return JobOutcome(run_status="awaiting_approval", ok=True)

        job_id = registry.submit("plan", work).job.job_id
        self.assertTrue(ready.wait(5))
        live = registry.get(job_id)
        self.assertEqual((live.state, live.run_id, live.progress.stage), (RUNNING, "abc123def456", "planning"))
        self.assertEqual([(s.title, s.status) for s in live.progress.steps][:2], [("Requirement", "done"), ("Plan", "current")])
        self.assertEqual((live.progress.event_count, live.progress.last_event.action), (1, "Understand requirement"))
        proceed.set()
        done = self.finish(registry, job_id)
        self.assertEqual(done.run_id, "abc123def456")
        self.assertEqual(done.progress.stage, "finished")
        self.assertEqual(done.progress.steps[0].detail, "3 features", "the last reported steps are kept")

    def test_partial_progress_updates_keep_the_other_fields(self):
        registry, ready, proceed = self.registry(), threading.Event(), threading.Event()
        self.addCleanup(proceed.set)

        def work(ctx):
            ctx.set_progress("planning", event_count=2)
            ctx.set_progress("awaiting_approval")
            ready.set()
            proceed.wait(10)

        job_id = registry.submit("plan", work).job.job_id
        self.assertTrue(ready.wait(5))
        live = registry.get(job_id)
        self.assertEqual((live.progress.stage, live.progress.event_count), ("awaiting_approval", 2))

    def test_invalid_progress_and_a_changing_run_id_are_programming_errors_that_fail_the_job(self):
        registry = self.registry()

        def bad_stage(ctx):
            ctx.set_progress("dancing")

        def changing_run(ctx):
            ctx.set_run_id("r1")
            ctx.set_run_id("r2")

        for work in (bad_stage, changing_run):
            with self.assertLogs("aether_api.jobs", level="ERROR"):
                job = self.finish(registry, registry.submit("plan", work).job.job_id, FAILED)
            self.assertEqual(job.error.code, "internal_error")

    def test_started_at_is_set_when_the_worker_begins(self):
        registry, gate = self.registry(), self.gate()
        job_id = registry.submit("plan", gate.work).job.job_id
        self.assertTrue(gate.wait_entered())
        self.assertTrue(wait_until(lambda: registry.get(job_id).started_at is not None))

    def test_the_queued_state_is_reserved_and_never_produced(self):
        self.assertEqual(J.QUEUED, "queued")
        registry = self.registry()
        job = self.finish(registry, registry.submit("plan", quick()).job.job_id)
        self.assertNotEqual(job.state, J.QUEUED)
        self.assertEqual(J.TERMINAL_STATES, {SUCCEEDED, FAILED, INTERRUPTED})


# ====================================================================== admission (sequential rules)

class AdmissionTests(RegistryTestCase):
    def test_the_error_codes_match_the_documented_api_codes(self):
        self.assertEqual({e.code for e in (JobsShuttingDown, TooManyJobs, PlannerBusy, RunBusy, IdempotencyKeyReused,
                                           JobStartFailed)},
                         {"shutting_down", "too_many_jobs", "planner_busy", "run_busy", "idempotency_key_reused",
                          "job_start_failed"})

    def test_the_third_job_is_refused_at_capacity_and_a_slot_comes_back_when_a_job_ends(self):
        registry, gate = self.registry(max_jobs=2), self.gate()
        first = registry.submit("plan", gate.work).job.job_id
        registry.submit("plan", gate.work)
        with self.assertRaises(TooManyJobs):
            registry.submit("plan", gate.work)
        self.assertEqual(registry.stats()["active"], 2)
        gate.release.set()
        self.finish(registry, first)
        self.assertTrue(wait_until(lambda: registry.stats()["active"] == 0))
        registry.submit("plan", quick())  # accepted again
        registry.check_invariants()

    def test_only_one_ai_planner_job_runs_at_a_time_but_others_are_not_blocked(self):
        registry, gate = self.registry(max_jobs=3, max_ollama_jobs=1), self.gate()
        ai = registry.submit("plan", gate.work, uses_ollama=True).job.job_id
        with self.assertRaises(PlannerBusy):
            registry.submit("plan", gate.work, uses_ollama=True)
        registry.submit("plan", gate.work)  # the rule-based planner is fine
        gate.release.set()
        self.finish(registry, ai)
        self.assertTrue(wait_until(lambda: registry.stats()["active_ollama"] == 0))
        registry.submit("plan", quick(), uses_ollama=True)

    def test_one_active_job_per_run(self):
        registry, gate = self.registry(max_jobs=5), self.gate()
        first = registry.submit("approve", gate.work, run_id="run1").job.job_id
        with self.assertRaises(RunBusy) as caught:
            registry.submit("approve", gate.work, run_id="run1")
        self.assertEqual(caught.exception.details, {"job_id": first})
        registry.submit("approve", gate.work, run_id="run2")  # another run is fine
        gate.release.set()
        self.finish(registry, first)
        self.assertTrue(wait_until(lambda: registry.stats()["runs_claimed"] == 0))
        registry.submit("approve", quick(), run_id="run1")

    def test_a_planning_job_claims_its_run_as_soon_as_it_knows_the_id(self):
        registry, ready, proceed = self.registry(max_jobs=5), threading.Event(), threading.Event()
        self.addCleanup(proceed.set)

        def planning(ctx):
            ctx.set_run_id("freshrun1234")
            ready.set()
            proceed.wait(10)

        registry.submit("plan", planning)
        self.assertTrue(ready.wait(5))
        with self.assertRaises(RunBusy):
            registry.submit("approve", quick(), run_id="freshrun1234")
        proceed.set()
        self.assertTrue(wait_until(lambda: registry.stats()["runs_claimed"] == 0))

    def test_the_order_of_the_checks(self):
        gate = self.gate()
        # shutting down beats everything
        registry = self.registry(max_jobs=1)
        registry.shutdown(0)
        with self.assertRaises(JobsShuttingDown):
            registry.submit("plan", gate.work, uses_ollama=True)
        # run busy beats too many jobs; too many jobs beats planner busy
        registry = self.registry(max_jobs=1, max_ollama_jobs=1)
        registry.submit("approve", gate.work, run_id="r1", uses_ollama=True)
        with self.assertRaises(RunBusy):
            registry.submit("approve", gate.work, run_id="r1")
        with self.assertRaises(TooManyJobs):
            registry.submit("plan", gate.work, uses_ollama=True)

    def test_a_thread_that_cannot_start_leaves_nothing_behind(self):
        registry, gate = self.registry(max_jobs=2, max_ollama_jobs=1), self.gate()
        for round_number in range(30):  # many rounds: a leak of any counter would show up as refusals later
            with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("can't start new thread")):
                with self.assertRaises(JobStartFailed) as caught:
                    registry.submit("approve", gate.work, run_id="run1", uses_ollama=True, idempotency_key="k",
                                    request_hash="h")
            self.assertIsInstance(caught.exception.__cause__, RuntimeError)
            stats = registry.stats()
            self.assertEqual((stats["active"], stats["active_ollama"], stats["jobs"], stats["runs_claimed"],
                              stats["idempotency_keys"]), (0, 0, 0, 0, 0), round_number)
        registry.check_invariants()
        admission = registry.submit("approve", gate.work, run_id="run1", uses_ollama=True, idempotency_key="k",
                                    request_hash="h")  # the same run, key and AI slot are all free again
        self.assertFalse(admission.replayed)


# ====================================================================== simultaneous requests

class SimultaneousRequestTests(RegistryTestCase):
    ROUNDS = 12

    def test_simultaneous_requests_never_exceed_max_jobs(self):
        for _ in range(self.ROUNDS):
            registry, gate = self.registry(max_jobs=2), self.gate()
            results = race(40, lambda i: registry.submit("plan", gate.work))
            accepted = [r for r in results if not isinstance(r, Exception)]
            refused = [r for r in results if isinstance(r, Exception)]
            self.assertEqual(len(accepted), 2, results)
            self.assertTrue(all(isinstance(r, TooManyJobs) for r in refused), refused)
            self.assertTrue(gate.wait_entered(2))
            self.assertLessEqual(gate.maximum, 2, "more operations ran at once than the limit")
            self.assertEqual(registry.stats()["active"], 2)
            gate.release.set()
            self.assertTrue(wait_until(lambda: registry.stats()["active"] == 0))
            registry.check_invariants()
            registry.submit("plan", quick())  # nothing leaked: a new job is accepted again (it may already be done)

    def test_simultaneous_ai_planner_requests_admit_exactly_one(self):
        for _ in range(self.ROUNDS):
            registry, gate = self.registry(max_jobs=8, max_ollama_jobs=1), self.gate()
            results = race(30, lambda i: registry.submit("plan", gate.work, uses_ollama=True))
            self.assertEqual(sum(not isinstance(r, Exception) for r in results), 1)
            self.assertTrue(all(isinstance(r, PlannerBusy) for r in results if isinstance(r, Exception)))
            gate.release.set()
            self.assertTrue(wait_until(lambda: registry.stats()["active"] == 0))
            registry.check_invariants()

    def test_mixed_simultaneous_requests_respect_both_limits(self):
        for _ in range(self.ROUNDS):
            registry, gate = self.registry(max_jobs=3, max_ollama_jobs=1), self.gate()
            results = race(36, lambda i: registry.submit("plan", gate.work, uses_ollama=(i % 3 == 0)))
            accepted = [r for r in results if not isinstance(r, Exception)]
            self.assertEqual(len(accepted), 3)
            self.assertLessEqual(sum(1 for r in accepted if r.job.job_id and registry._jobs[r.job.job_id].uses_ollama), 1)
            self.assertTrue(gate.wait_entered(3))
            self.assertLessEqual(gate.maximum, 3)
            gate.release.set()
            self.assertTrue(wait_until(lambda: registry.stats()["active"] == 0))
            registry.check_invariants()

    def test_simultaneous_operations_on_one_run_admit_exactly_one(self):
        for _ in range(self.ROUNDS):
            registry, gate = self.registry(max_jobs=20), self.gate()
            results = race(20, lambda i: registry.submit("approve", gate.work, run_id="the-run"))
            self.assertEqual(sum(not isinstance(r, Exception) for r in results), 1)
            self.assertTrue(all(isinstance(r, RunBusy) for r in results if isinstance(r, Exception)))
            winners = [r.job.job_id for r in results if not isinstance(r, Exception)]
            self.assertTrue(all(r.details == {"job_id": winners[0]} for r in results if isinstance(r, RunBusy)))
            gate.release.set()
            self.assertTrue(wait_until(lambda: registry.stats()["active"] == 0))

    def test_simultaneous_identical_idempotent_requests_start_exactly_one_job(self):
        for _ in range(self.ROUNDS):
            registry, gate = self.registry(max_jobs=2), self.gate()
            results = race(30, lambda i: registry.submit("plan", gate.work, idempotency_key="key-1", request_hash="h"))
            self.assertTrue(all(not isinstance(r, Exception) for r in results), results)
            self.assertEqual(len({r.job.job_id for r in results}), 1, "every caller must get the same job")
            self.assertEqual(sum(not r.replayed for r in results), 1)
            self.assertTrue(gate.wait_entered(1))
            self.assertEqual(gate.calls, 1, "the operation ran more than once")
            gate.release.set()
            self.assertTrue(wait_until(lambda: registry.stats()["active"] == 0))

    def test_simultaneous_requests_with_one_key_but_different_bodies_admit_one_and_refuse_the_rest(self):
        registry, gate = self.registry(max_jobs=5), self.gate()
        results = race(20, lambda i: registry.submit("plan", gate.work, idempotency_key="k", request_hash=f"body-{i % 2}"))
        winners = [r for r in results if not isinstance(r, Exception) and not r.replayed]
        self.assertEqual(len(winners), 1)
        self.assertEqual(gate.calls, 1) if gate.wait_entered(1) else self.fail("no job started")
        won_hash = f"body-{results.index(winners[0]) % 2}"
        for index, result in enumerate(results):
            if f"body-{index % 2}" == won_hash:
                self.assertFalse(isinstance(result, Exception), result)
            else:
                self.assertIsInstance(result, IdempotencyKeyReused)

    def test_churn_never_exceeds_the_limit_and_leaks_nothing(self):
        registry = self.registry(max_jobs=2, max_finished=1000)
        gate = Gate()
        gate.release.set()  # operations finish at once; the threads hammer submit/get while jobs come and go
        seen_states = set()

        def hammer(index):
            accepted = 0
            for _ in range(60):
                try:
                    job = registry.submit("plan", gate.work).job
                    accepted += 1
                    seen_states.add(registry.get(job.job_id).state)
                except TooManyJobs:
                    time.sleep(0.001)
            return accepted

        results = race(8, hammer)
        self.assertTrue(all(isinstance(r, int) for r in results), results)
        self.assertGreater(sum(results), 8)
        self.assertLessEqual(gate.maximum, 2)
        self.assertTrue(wait_until(lambda: registry.stats()["active"] == 0))
        registry.check_invariants()
        self.assertTrue(seen_states <= {RUNNING, SUCCEEDED})

    def test_random_operations_from_many_threads_keep_the_invariants(self):
        registry = self.registry(max_jobs=3, max_ollama_jobs=2, max_finished=10)
        gate = Gate()
        gate.release.set()
        ids = []
        ids_lock = threading.Lock()

        def operate(index):
            for step in range(80):
                action = (index + step) % 4
                try:
                    if action == 0:
                        job = registry.submit("approve", gate.work, run_id=f"run{step % 5}").job
                    elif action == 1:
                        job = registry.submit("plan", gate.work, uses_ollama=bool(step % 2),
                                              idempotency_key=f"k{step % 6}", request_hash="h").job
                    else:
                        with ids_lock:
                            known = list(ids[-5:])
                        for job_id in known:
                            registry.get(job_id)
                        registry.stats()
                        continue
                    with ids_lock:
                        ids.append(job.job_id)
                except J.AdmissionError:
                    pass

        results = race(8, operate)
        self.assertTrue(all(r is None for r in results), results)
        self.assertTrue(wait_until(lambda: registry.stats()["active"] == 0))
        registry.check_invariants()
        self.assertLessEqual(registry.stats()["finished"], 10)


# ====================================================================== idempotency (sequential rules)

class IdempotencyTests(RegistryTestCase):
    def test_the_same_key_and_body_returns_the_same_job_without_starting_another(self):
        registry, gate = self.registry(), self.gate()
        first = registry.submit("plan", gate.work, idempotency_key="k1", request_hash="h1")
        again = registry.submit("plan", gate.work, idempotency_key="k1", request_hash="h1")
        self.assertEqual((first.replayed, again.replayed), (False, True))
        self.assertEqual(first.job.job_id, again.job.job_id)
        self.assertTrue(gate.wait_entered(1))
        self.assertEqual(gate.calls, 1)

    def test_a_replay_after_the_job_finished_returns_the_finished_job(self):
        registry = self.registry()
        first = registry.submit("plan", quick(), idempotency_key="k1", request_hash="h1")
        self.finish(registry, first.job.job_id)
        again = registry.submit("plan", quick(), idempotency_key="k1", request_hash="h1")
        self.assertTrue(again.replayed)
        self.assertEqual(again.job.state, SUCCEEDED)

    def test_the_same_key_with_a_different_body_is_refused(self):
        registry = self.registry()
        registry.submit("plan", quick(), idempotency_key="k1", request_hash="h1")
        with self.assertRaises(IdempotencyKeyReused):
            registry.submit("plan", quick(), idempotency_key="k1", request_hash="OTHER")

    def test_a_replay_uses_no_slot_so_it_works_at_capacity_and_on_a_busy_run(self):
        registry, gate = self.registry(max_jobs=1), self.gate()
        first = registry.submit("approve", gate.work, run_id="r1", idempotency_key="k", request_hash="h")
        with self.assertRaises(TooManyJobs):
            registry.submit("plan", gate.work)
        replay = registry.submit("approve", gate.work, run_id="r1", idempotency_key="k", request_hash="h")
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.job.job_id, first.job.job_id)

    def test_keys_are_scoped_by_kind_run_and_key(self):
        registry, gate = self.registry(max_jobs=10), self.gate()
        a = registry.submit("plan", gate.work, idempotency_key="k", request_hash="h").job.job_id
        b = registry.submit("approve", gate.work, run_id="r1", idempotency_key="k", request_hash="h").job.job_id
        c = registry.submit("approve", gate.work, run_id="r2", idempotency_key="k", request_hash="h").job.job_id
        d = registry.submit("plan", gate.work, idempotency_key="other", request_hash="h").job.job_id
        self.assertEqual(len({a, b, c, d}), 4)

    def test_a_shutdown_registry_refuses_even_a_replay(self):
        registry = self.registry()
        registry.submit("plan", quick(), idempotency_key="k", request_hash="h")
        registry.shutdown(0)
        with self.assertRaises(JobsShuttingDown):
            registry.submit("plan", quick(), idempotency_key="k", request_hash="h")


# ====================================================================== retention

class RetentionTests(RegistryTestCase):
    def finished_job(self, registry, **kwargs) -> str:
        job_id = registry.submit("plan", quick(), **kwargs).job.job_id
        self.finish(registry, job_id)
        return job_id

    def test_finished_jobs_stay_readable_until_the_ttl_then_are_purged(self):
        clock = FakeClock()
        registry = self.registry(finished_ttl=100, clock=clock)
        job_id = self.finished_job(registry)
        clock.advance(99)
        self.assertEqual(registry.get(job_id).state, SUCCEEDED)
        clock.advance(2)
        self.assertIsNone(registry.get(job_id), "purged once older than the ttl")
        registry.check_invariants()

    def test_the_oldest_finished_jobs_are_dropped_first_beyond_max_finished(self):
        registry = self.registry(max_jobs=1, max_finished=3, finished_ttl=3600)
        ids = [self.finished_job(registry) for _ in range(6)]
        self.assertEqual([registry.get(i) is not None for i in ids], [False, False, False, True, True, True])
        self.assertEqual(registry.stats()["finished"], 3)

    def test_running_jobs_are_never_purged(self):
        clock = FakeClock()
        registry, gate = self.registry(finished_ttl=1, max_finished=0, clock=clock), self.gate()
        job_id = registry.submit("plan", gate.work).job.job_id
        clock.advance(10_000)
        for _ in range(5):
            registry.submit("plan", quick())  # churn, with purges
        self.assertEqual(registry.get(job_id).state, RUNNING)

    def test_purging_also_frees_the_idempotency_key(self):
        clock = FakeClock()
        registry = self.registry(finished_ttl=50, clock=clock)
        first = self.finished_job(registry, idempotency_key="k", request_hash="h")
        self.assertEqual(registry.stats()["idempotency_keys"], 1)
        clock.advance(51)
        again = registry.submit("plan", quick(), idempotency_key="k", request_hash="h")
        self.assertFalse(again.replayed, "the key is gone with its job, so this is a new job")
        self.assertNotEqual(again.job.job_id, first)

    def test_purging_happens_on_read_without_any_new_submission(self):
        clock = FakeClock()
        registry = self.registry(finished_ttl=10, clock=clock)
        job_id = self.finished_job(registry)
        clock.advance(11)
        self.assertEqual(registry.stats()["jobs"], 0)
        self.assertIsNone(registry.get(job_id))

    def test_zero_retention_keeps_nothing(self):
        registry = self.registry(max_finished=0)
        job_id = registry.submit("plan", quick()).job.job_id
        self.assertTrue(wait_until(lambda: registry.get(job_id) is None))
        registry.check_invariants()


# ====================================================================== shutdown

class ShutdownTests(RegistryTestCase):
    def test_shutdown_with_no_jobs_is_immediate_and_stops_admission(self):
        registry = self.registry()
        started = time.monotonic()
        self.assertEqual(registry.shutdown(5), [])
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(registry.shutting_down)
        with self.assertRaises(JobsShuttingDown):
            registry.submit("plan", quick())

    def test_a_job_that_finishes_within_the_grace_period_completes_normally(self):
        registry, gate = self.registry(), self.gate()
        job_id = registry.submit("plan", gate.work).job.job_id
        self.assertTrue(gate.wait_entered())
        threading.Timer(0.1, gate.release.set).start()
        self.assertEqual(registry.shutdown(5), [])
        job = registry.get(job_id)
        self.assertEqual(job.state, SUCCEEDED)
        self.assertIsNotNone(job.result)

    def test_a_job_that_outlives_the_grace_period_is_interrupted_and_can_never_look_successful(self):
        registry, gate = self.registry(), self.gate()
        job_id = registry.submit("plan", gate.work).job.job_id
        self.assertTrue(gate.wait_entered())
        with self.assertLogs("aether_api.jobs", level="WARNING"):
            self.assertEqual(registry.shutdown(0.15), [job_id])
        job = registry.get(job_id)
        self.assertEqual(job.state, INTERRUPTED)
        self.assertIsNone(job.result)
        self.assertEqual((job.error.code, job.error.message), ("server_shutdown", J.SHUTDOWN_MESSAGE))
        self.assertIsNotNone(job.finished_at)
        self.assertEqual(job.progress.stage, "finished")
        self.assertEqual(registry.stats()["active"], 0)
        registry.check_invariants()

    def test_a_thread_that_finishes_after_the_interruption_cannot_change_the_job(self):
        registry, gate = self.registry(), self.gate(JobOutcome(run_status="completed", ok=True))
        job_id = registry.submit("approve", gate.work, run_id="r1").job.job_id
        self.assertTrue(gate.wait_entered())
        with self.assertLogs("aether_api.jobs", level="WARNING") as logs:
            registry.shutdown(0.1)
            gate.release.set()  # the operation now "succeeds", too late
            self.assertTrue(wait_until(lambda: gate.current == 0))
            self.assertTrue(wait_until(lambda: any("late result was ignored" in line for line in logs.output)))
        job = registry.get(job_id)
        self.assertEqual((job.state, job.result, job.error.code), (INTERRUPTED, None, "server_shutdown"))
        self.assertEqual(registry.stats()["active"], 0)
        registry.check_invariants()

    def test_every_job_that_is_still_running_is_interrupted_and_none_is_reported_successful(self):
        registry, slow = self.registry(max_jobs=3), self.gate()
        ids = [registry.submit("approve", slow.work, run_id=f"r{i}").job.job_id for i in range(3)]
        self.assertTrue(slow.wait_entered(3))
        with self.assertLogs("aether_api.jobs", level="WARNING"):
            self.assertEqual(sorted(registry.shutdown(0.1)), sorted(ids))
        slow.release.set()
        self.assertTrue(wait_until(lambda: slow.current == 0))
        for job_id in ids:
            job = registry.get(job_id)
            self.assertEqual((job.state, job.result), (INTERRUPTED, None))

    def test_a_context_cannot_update_a_job_after_it_was_interrupted(self):
        registry, ready, proceed = self.registry(), threading.Event(), threading.Event()
        self.addCleanup(proceed.set)
        holder = {}

        def work(ctx):
            holder["ctx"] = ctx
            ready.set()
            proceed.wait(10)

        job_id = registry.submit("plan", work).job.job_id
        self.assertTrue(ready.wait(5))
        with self.assertLogs("aether_api.jobs", level="WARNING"):
            registry.shutdown(0.05)
        holder["ctx"].set_run_id("late-run")
        holder["ctx"].set_progress("planning", event_count=99)
        job = registry.get(job_id)
        self.assertEqual((job.run_id, job.progress.event_count, job.progress.stage), (None, 0, "finished"))
        proceed.set()

    def test_shutdown_twice_is_harmless(self):
        registry, gate = self.registry(), self.gate()
        registry.submit("plan", gate.work)
        self.assertTrue(gate.wait_entered())
        with self.assertLogs("aether_api.jobs", level="WARNING"):
            first = registry.shutdown(0.05)
        self.assertEqual(len(first), 1)
        self.assertEqual(registry.shutdown(0.05), [])

    def test_zero_grace_interrupts_at_once(self):
        registry, gate = self.registry(), self.gate()
        job_id = registry.submit("plan", gate.work).job.job_id
        self.assertTrue(gate.wait_entered())
        started = time.monotonic()
        with self.assertLogs("aether_api.jobs", level="WARNING"):
            self.assertEqual(registry.shutdown(0), [job_id])
        self.assertLess(time.monotonic() - started, 1.0)

    def test_the_registry_keeps_answering_while_shutdown_waits_for_a_job(self):
        registry, gate = self.registry(), self.gate()
        job_id = registry.submit("plan", gate.work).job.job_id
        self.assertTrue(gate.wait_entered())
        waiter = threading.Thread(target=lambda: registry.shutdown(1.5))
        waiter.start()
        try:
            self.assertTrue(wait_until(lambda: registry.shutting_down))
            for call in (lambda: registry.get(job_id), registry.stats):
                started = time.monotonic()
                call()
                self.assertLess(time.monotonic() - started, 0.5, "shutdown must not hold the mutex while it waits")
            started = time.monotonic()
            with self.assertRaises(JobsShuttingDown):
                registry.submit("plan", quick())
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            gate.release.set()
            waiter.join(10)
        self.assertEqual(registry.get(job_id).state, SUCCEEDED, "it finished inside the grace period")


# ====================================================================== worker failure

class WorkerFailureTests(RegistryTestCase):
    def test_a_worker_that_dies_without_finishing_is_failed_as_worker_lost(self):
        registry = self.registry(max_jobs=1)
        with mock.patch.object(JobRegistry, "_run", lambda self, job, work: None):  # the thread ends, job unfinished
            job_id = registry.submit("approve", quick(), run_id="r1").job.job_id
        thread = registry._jobs[job_id].thread
        thread.join(5)
        self.assertFalse(thread.is_alive())
        with self.assertLogs("aether_api.jobs", level="ERROR"):
            job = registry.get(job_id)
        self.assertEqual((job.state, job.result, job.error.code), (FAILED, None, "worker_lost"))
        self.assertEqual(registry.stats()["active"], 0)
        self.assertEqual(registry.stats()["runs_claimed"], 0)
        registry.submit("approve", quick(), run_id="r1")  # the slot and the run are free again
        registry.check_invariants()

    def test_a_running_job_whose_thread_is_alive_is_not_called_lost(self):
        registry, gate = self.registry(), self.gate()
        job_id = registry.submit("plan", gate.work).job.job_id
        self.assertTrue(gate.wait_entered())
        for _ in range(20):
            self.assertEqual(registry.get(job_id).state, RUNNING)

    def test_failures_free_their_slot_claim_and_ai_slot(self):
        registry = self.registry(max_jobs=1, max_ollama_jobs=1)

        def boom(ctx):
            raise JobFailure("storage_unavailable", "x")

        job_id = registry.submit("approve", boom, run_id="r1", uses_ollama=True).job.job_id
        self.finish(registry, job_id, FAILED)
        stats = registry.stats()
        self.assertEqual((stats["active"], stats["active_ollama"], stats["runs_claimed"]), (0, 0, 0))
        registry.submit("approve", quick(), run_id="r1", uses_ollama=True)

    def test_a_late_finish_after_worker_lost_is_ignored(self):
        registry = self.registry()
        with mock.patch.object(JobRegistry, "_run", lambda self, job, work: None):
            job_id = registry.submit("plan", quick()).job.job_id
        registry._jobs[job_id].thread.join(5)
        with self.assertLogs("aether_api.jobs", level="ERROR"):
            registry.get(job_id)
        job = registry._jobs[job_id]
        self.assertFalse(registry._finish(job, SUCCEEDED, JobOutcome(ok=True), None))
        self.assertEqual((registry.get(job_id).state, registry.get(job_id).result), (FAILED, None))


# ====================================================================== the mutex is a leaf

class MutexDisciplineTests(RegistryTestCase):
    def test_the_mutex_is_not_held_while_the_operation_runs(self):
        registry, observed = self.registry(), []
        job_id = registry.submit("plan", lambda ctx: observed.append(registry._lock.locked())).job.job_id
        self.finish(registry, job_id)
        self.assertEqual(observed, [False])

    def test_the_mutex_is_not_held_while_a_thread_starts(self):
        registry, observed = self.registry(), []
        real_start = threading.Thread.start

        def spy(thread):
            observed.append(registry._lock.locked())
            return real_start(thread)

        with mock.patch.object(threading.Thread, "start", spy):
            self.finish(registry, registry.submit("plan", quick()).job.job_id)
        self.assertEqual(observed, [False])

    def test_the_mutex_is_not_held_while_logging(self):
        registry, observed, logged = self.registry(), [], threading.Event()

        class Spy(logging.Handler):
            def emit(self, record):
                observed.append(registry._lock.locked())
                logged.set()

        handler, logger = Spy(level=logging.DEBUG), logging.getLogger("aether_api.jobs")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        previous_level, previous_propagate = logger.level, logger.propagate
        logger.setLevel(logging.DEBUG)  # capture EVERY level: a debug or info line under the mutex would also be a bug
        logger.propagate = False
        self.addCleanup(logger.setLevel, previous_level)
        self.addCleanup(setattr, logger, "propagate", previous_propagate)

        def boom(ctx):
            raise RuntimeError("x")

        job_id = registry.submit("plan", boom).job.job_id
        worker = registry._jobs[job_id].thread
        self.assertTrue(logged.wait(5), "the worker never logged")
        worker.join(5)                      # nothing else touches the registry while the worker can still log
        self.assertFalse(worker.is_alive(), "the worker is still running")
        self.assertNotIn(True, observed, "a log call was made while the registry mutex was held")
        self.assertEqual(len(observed), 1)
        self.assertEqual(registry.get(job_id).state, FAILED)

        gate = self.gate()
        registry.submit("plan", gate.work)
        self.assertTrue(gate.wait_entered())
        registry.shutdown(0.05)                                                             # logs a warning
        self.assertNotIn(True, observed, "a log call was made while the registry mutex was held")
        self.assertEqual(observed, [False, False])

    def test_reads_and_refusals_stay_fast_while_a_long_operation_runs(self):
        registry, gate = self.registry(max_jobs=1), self.gate()
        job_id = registry.submit("plan", gate.work).job.job_id
        self.assertTrue(gate.wait_entered())
        started = time.monotonic()
        for _ in range(50):
            registry.get(job_id)
            registry.stats()
            with self.assertRaises(TooManyJobs):
                registry.submit("plan", quick())
        self.assertLess(time.monotonic() - started, 1.0)

    def test_the_registry_source_never_touches_the_filesystem_or_calls_anything_long_running(self):
        """A static guard: no file, path, process, network, core or web-framework import, and no open() call."""
        tree = ast.parse(JOBS_SOURCE.read_text(encoding="utf-8"))
        forbidden = {"os", "pathlib", "shutil", "tempfile", "io", "subprocess", "socket", "urllib", "http", "requests",
                     "aether", "aether_ui", "fastapi", "pydantic", "starlette", "sqlite3", "asyncio"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [(node.module or "").split(".")[0]]
            else:
                modules = []
            self.assertFalse(set(modules) & forbidden, f"jobs.py imports {modules}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                self.assertNotIn(node.func.id, {"open", "print", "input", "exec", "eval"}, node.func.id)

    def test_the_mutex_is_only_ever_taken_with_a_with_statement_so_it_cannot_leak(self):
        source = JOBS_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("self._lock.acquire", source)
        self.assertNotIn("self._lock.release", source)

    def test_workers_are_daemon_threads_so_exit_is_never_blocked(self):
        registry, gate = self.registry(), self.gate()
        job_id = registry.submit("plan", gate.work).job.job_id
        self.assertTrue(gate.wait_entered())
        thread = registry._jobs[job_id].thread
        self.assertTrue(thread.daemon)
        self.assertTrue(thread.name.startswith("aether-job-"))


# ====================================================================== instance identity

class InstanceIdentityTests(RegistryTestCase):
    def test_the_instance_id_is_12_random_hex_characters_and_differs_between_registries(self):
        ids = {JobRegistry().instance_id for _ in range(300)}
        self.assertEqual(len(ids), 300)
        self.assertTrue(all(HEX12.match(i) for i in ids))

    def test_a_given_instance_id_is_kept_and_stamped_on_every_snapshot(self):
        registry = self.registry(instance_id="abcdef012345")
        self.assertEqual(registry.instance_id, "abcdef012345")
        admission = registry.submit("plan", quick())
        self.assertEqual(admission.job.instance_id, "abcdef012345")
        self.assertEqual(self.finish(registry, admission.job.job_id).instance_id, "abcdef012345")

    def test_a_job_id_from_another_instance_is_never_found(self):
        earlier, current = self.registry(), self.registry()
        old_ids = [earlier.submit("plan", quick()).job.job_id for _ in range(5)]
        new_ids = [current.submit("plan", quick()).job.job_id for _ in range(5)]
        for job_id in old_ids:
            self.assertIsNone(current.get(job_id))
        for job_id in new_ids:
            self.assertIsNone(earlier.get(job_id))
            self.assertIsNotNone(current.get(job_id))

    def test_job_ids_of_two_instances_never_collide(self):
        a = self.registry(max_jobs=1000, max_finished=1000)
        b = self.registry(max_jobs=1000, max_finished=1000)
        ids_a = [a.submit("plan", quick()).job.job_id for _ in range(500)]
        ids_b = [b.submit("plan", quick()).job.job_id for _ in range(500)]
        self.assertEqual(len(set(ids_a) | set(ids_b)), 1000)

    def test_a_restart_means_a_new_instance_that_knows_none_of_the_old_jobs(self):
        before = self.registry()
        job_id = before.submit("plan", quick()).job.job_id
        self.finish(before, job_id)
        before.shutdown(0)
        after = self.registry()  # the "restarted" server
        self.assertNotEqual(after.instance_id, before.instance_id)
        self.assertIsNone(after.get(job_id))
        self.assertEqual(after.stats()["jobs"], 0)

    def test_idempotency_keys_do_not_survive_into_another_instance(self):
        before, after = self.registry(), self.registry()
        first = before.submit("plan", quick(), idempotency_key="k", request_hash="h").job.job_id
        replay = after.submit("plan", quick(), idempotency_key="k", request_hash="h")
        self.assertFalse(replay.replayed)
        self.assertNotEqual(replay.job.job_id, first)


if __name__ == "__main__":
    unittest.main()
