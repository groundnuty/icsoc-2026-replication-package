"""Regression tests for harness/sweep_run.py — the sweep concurrency
orchestrator. Stdlib `unittest` only, same import-shim as test_sweep.py.
All pure/offline — `execute_cell` is always a mock; no live federation, no
kubectl, no tc.

Timing-based assertions use generous sleeps (0.05s) and assert
ordering/counts rather than exact timings, to avoid flaky failures from
scheduling jitter.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest

if __package__ in (None, ""):
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    try:
        from harness import sweep, sweep_run
    except ImportError:
        import sweep
        import sweep_run
else:
    from . import sweep, sweep_run


DE = "7cfe30bef82a2c904c15624f752c58ddchd932"
UIBK = "af63236ac5942dbc22bd9159a8154c8ach6020"
PL = "ec4761ab47ecda487c46edd328179824chc3bd"

SLEEP = 0.05  # generous per-cell "work" duration for timing-based assertions


def _scenario(scenario_id="E1", target=DE, source=PL):
    return {
        "scenario_id": scenario_id,
        "target_provider_id": target,
        "source_provider_id": source,
        "target_provider_label": "de",
    }


class ConcurrencyTracker:
    """Thread-safe helper: tracks how many callers are concurrently "inside"
    a critical section, and the max concurrency ever observed."""

    def __init__(self):
        self._lock = threading.Lock()
        self._current = 0
        self.max_seen = 0

    def enter(self):
        with self._lock:
            self._current += 1
            self.max_seen = max(self.max_seen, self._current)

    def exit(self):
        with self._lock:
            self._current -= 1


class RecordingExecutor:
    """Mock `execute_cell`. Records (cell, start, end) for every call via a
    thread-safe list, and tracks per-provider concurrency via a
    ConcurrencyTracker keyed on the cell's `perturbs_provider` (falling back
    to a shared "concurrent-bucket" key for no-fault cells)."""

    def __init__(self, sleep=SLEEP, raise_for=()):
        self._lock = threading.Lock()
        self.calls: list = []
        self.sleep = sleep
        self.raise_for = set(raise_for)  # set of scenario_id+leg tuples to raise for
        self.trackers: dict = {}

    def _tracker_for(self, key):
        with self._lock:
            if key not in self.trackers:
                self.trackers[key] = ConcurrencyTracker()
            return self.trackers[key]

    def __call__(self, cell):
        key = cell.perturbs_provider or "_concurrent_"
        tracker = self._tracker_for(key)
        start = time.monotonic()
        tracker.enter()
        try:
            if (cell.scenario_id, cell.model_leg, cell.fault.condition) in self.raise_for:
                raise RuntimeError(f"boom on {cell.model_leg}")
            time.sleep(self.sleep)
            end = time.monotonic()
            with self._lock:
                self.calls.append((cell, start, end))
            return {"ok": True, "leg": cell.model_leg}
        finally:
            tracker.exit()


class TestRunPlanNoFault(unittest.TestCase):
    def test_all_no_fault_cells_run(self):
        cells = sweep.enumerate_cells(
            scenarios=[_scenario()], legs=["a", "b", "c", "d"], fault_specs=[sweep.FaultSpec()]
        )
        executor = RecordingExecutor()
        out = sweep_run.run_plan(cells, executor, max_concurrency=8)

        self.assertEqual(out["n_cells"], 4)
        self.assertEqual(len(out["results"]), 4)
        self.assertEqual(out["errors"], [])
        legs_seen = {r["result"]["leg"] for r in out["results"]}
        self.assertEqual(legs_seen, {"a", "b", "c", "d"})
        # no-fault cells are independent tasks — should be able to overlap
        self.assertGreaterEqual(executor.trackers["_concurrent_"].max_seen, 2)


class TestRunPlanSerialGroups(unittest.TestCase):
    def test_same_provider_faults_run_serially(self):
        faults = [
            sweep.FaultSpec(condition="outage", target_provider=DE),
            sweep.FaultSpec(condition="netem_lag", target_provider=DE, netem_delay_ms=25000.0),
            sweep.FaultSpec(condition="outage", target_provider=DE),
        ]
        cells = sweep.enumerate_cells(scenarios=[_scenario(target=DE)], legs=["a"], fault_specs=faults)
        self.assertEqual(len(cells), 3)  # sanity: all three fault cells on DE

        executor = RecordingExecutor()
        out = sweep_run.run_plan(cells, executor, max_concurrency=8)

        self.assertEqual(len(out["results"]), 3)
        self.assertEqual(out["errors"], [])
        # max concurrency observed WITHIN the DE group must never exceed 1
        self.assertEqual(executor.trackers[DE].max_seen, 1)

        # non-overlap re-derived directly from recorded windows, independent
        # of the tracker: sort by start time and assert each window ends
        # before the next one starts.
        windows = sorted(((s, e) for _, s, e in executor.calls), key=lambda w: w[0])
        for (_, end_prev), (start_next, _) in zip(windows, windows[1:]):
            self.assertLessEqual(end_prev, start_next)

    def test_different_provider_faults_can_overlap(self):
        faults_de = sweep.FaultSpec(condition="outage", target_provider=DE)
        faults_uibk = sweep.FaultSpec(condition="outage", target_provider=UIBK)
        cells = (
            sweep.enumerate_cells(scenarios=[_scenario(target=DE)], legs=["a"], fault_specs=[faults_de])
            + sweep.enumerate_cells(scenarios=[_scenario(target=UIBK)], legs=["a"], fault_specs=[faults_uibk])
        )
        executor = RecordingExecutor()
        out = sweep_run.run_plan(cells, executor, max_concurrency=8)

        # both groups complete regardless of overlap
        self.assertEqual(len(out["results"]), 2)
        self.assertEqual(out["errors"], [])
        providers_seen = {c.perturbs_provider for c in (r["cell"] for r in out["results"])}
        self.assertEqual(providers_seen, {DE, UIBK})
        # each individual provider group still serializes internally (only
        # one cell per group here, so max_seen == 1 trivially) — the real
        # assertion is that DE's window and UIBK's window are free to overlap,
        # i.e. the orchestrator didn't serialize ACROSS groups. This is checked
        # by confirming the two windows overlap in wall-clock time.
        de_window = next((s, e) for c, s, e in executor.calls if c.perturbs_provider == DE)
        uibk_window = next((s, e) for c, s, e in executor.calls if c.perturbs_provider == UIBK)
        overlap = min(de_window[1], uibk_window[1]) - max(de_window[0], uibk_window[0])
        self.assertGreater(overlap, 0, "different-provider fault groups should be able to run concurrently")

    def test_mixed_concurrent_and_serial_all_present(self):
        faults = [sweep.FaultSpec(), sweep.FaultSpec(condition="outage", target_provider=DE)]
        cells = sweep.enumerate_cells(scenarios=[_scenario(target=DE)], legs=["a", "b"], fault_specs=faults)
        self.assertEqual(len(cells), 4)

        executor = RecordingExecutor()
        out = sweep_run.run_plan(cells, executor, max_concurrency=8)

        self.assertEqual(len(out["results"]), 4)
        self.assertEqual(out["errors"], [])
        self.assertEqual(executor.trackers[DE].max_seen, 1)  # 2 fault cells on DE serialize


class TestRunPlanErrorIsolation(unittest.TestCase):
    def test_one_cell_raising_does_not_crash_sweep(self):
        cells = sweep.enumerate_cells(
            scenarios=[_scenario()], legs=["a", "b", "c"], fault_specs=[sweep.FaultSpec()]
        )
        executor = RecordingExecutor(raise_for={("E1", "b", "none")})
        out = sweep_run.run_plan(cells, executor, max_concurrency=8)

        self.assertEqual(out["n_cells"], 3)
        self.assertEqual(len(out["results"]), 2)
        self.assertEqual(len(out["errors"]), 1)
        errored_leg = out["errors"][0]["cell"].model_leg
        self.assertEqual(errored_leg, "b")
        self.assertIn("boom on b", out["errors"][0]["error"])
        ok_legs = {r["cell"].model_leg for r in out["results"]}
        self.assertEqual(ok_legs, {"a", "c"})

    def test_error_in_serial_group_does_not_strand_siblings(self):
        faults = [
            sweep.FaultSpec(condition="outage", target_provider=DE),
            sweep.FaultSpec(condition="netem_lag", target_provider=DE, netem_delay_ms=25000.0),
        ]
        # both fault cells share leg "a" but different fault condition, so key
        # on (scenario_id, leg, condition) distinguishes them
        cells = sweep.enumerate_cells(scenarios=[_scenario(target=DE)], legs=["a"], fault_specs=faults)
        executor = RecordingExecutor(raise_for={("E1", "a", "outage")})
        out = sweep_run.run_plan(cells, executor, max_concurrency=8)

        self.assertEqual(out["n_cells"], 2)
        self.assertEqual(len(out["errors"]), 1)
        self.assertEqual(len(out["results"]), 1)
        self.assertEqual(out["errors"][0]["cell"].fault.condition, "outage")
        self.assertEqual(out["results"][0]["cell"].fault.condition, "netem_lag")


class TestRunPlanVerifyBetween(unittest.TestCase):
    """`verify_between` — the clear-verification rider: after each cell in a
    serial (fault) group, abort the rest of the group if the qdisc-clean
    state can't be verified."""

    def _four_de_fault_cells(self):
        faults = [
            sweep.FaultSpec(condition="outage", target_provider=DE),
            sweep.FaultSpec(condition="netem_lag", target_provider=DE, netem_delay_ms=10000.0),
            sweep.FaultSpec(condition="outage", target_provider=DE),
            sweep.FaultSpec(condition="netem_lag", target_provider=DE, netem_delay_ms=20000.0),
        ]
        return sweep.enumerate_cells(scenarios=[_scenario(target=DE)], legs=["a"], fault_specs=faults)

    def test_verify_between_always_true_runs_all_cells(self):
        cells = self._four_de_fault_cells()
        executor = RecordingExecutor()
        out = sweep_run.run_plan(cells, executor, max_concurrency=8, verify_between=lambda cell: True)

        self.assertEqual(len(out["results"]), 4)
        self.assertEqual(out["errors"], [])
        self.assertEqual(len(executor.calls), 4)  # execute_cell called for every cell

    def test_verify_between_false_aborts_remaining_group_cells(self):
        cells = self._four_de_fault_cells()
        executor = RecordingExecutor()
        call_count = {"n": 0}

        def verify_between(cell):
            call_count["n"] += 1
            return call_count["n"] < 2  # True after cell 1, False after cell 2

        out = sweep_run.run_plan(cells, executor, max_concurrency=8, verify_between=verify_between)

        self.assertEqual(len(out["results"]), 2)  # first two cells ran + verified clean
        self.assertEqual(len(out["errors"]), 2)   # remaining two aborted, not executed
        self.assertEqual(len(executor.calls), 2)  # execute_cell NOT called for aborted cells
        for err in out["errors"]:
            self.assertIn("group aborted", err["error"])
            self.assertIn("qdisc clean unverifiable", err["error"])

    def test_verify_between_raising_is_treated_as_false_and_aborts(self):
        cells = self._four_de_fault_cells()
        executor = RecordingExecutor()
        call_count = {"n": 0}

        def verify_between(cell):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return True
            raise RuntimeError("cannot reach provider to confirm qdisc state")

        out = sweep_run.run_plan(cells, executor, max_concurrency=8, verify_between=verify_between)

        self.assertEqual(len(out["results"]), 2)
        self.assertEqual(len(out["errors"]), 2)
        self.assertEqual(len(executor.calls), 2)
        for err in out["errors"]:
            self.assertIn("group aborted", err["error"])

    def test_verify_between_not_applied_to_concurrent_no_fault_bucket(self):
        # verify_between is passed, but no-fault cells have no serial group
        # to abort — they should all still run untouched.
        cells = sweep.enumerate_cells(
            scenarios=[_scenario()], legs=["a", "b"], fault_specs=[sweep.FaultSpec()]
        )
        executor = RecordingExecutor()
        out = sweep_run.run_plan(cells, executor, max_concurrency=8, verify_between=lambda cell: False)

        self.assertEqual(len(out["results"]), 2)
        self.assertEqual(out["errors"], [])


class TestRunPlanSummary(unittest.TestCase):
    def test_summary_reports_ok_and_errored_counts(self):
        cells = sweep.enumerate_cells(
            scenarios=[_scenario()], legs=["a", "b", "c"], fault_specs=[sweep.FaultSpec()]
        )
        executor = RecordingExecutor(raise_for={("E1", "c", "none")})
        out = sweep_run.run_plan(cells, executor, max_concurrency=8)

        self.assertEqual(out["summary"], "ran 3 cells: 2 ok, 1 errored")

    def test_summary_all_ok(self):
        cells = sweep.enumerate_cells(
            scenarios=[_scenario()], legs=["a", "b"], fault_specs=[sweep.FaultSpec()]
        )
        executor = RecordingExecutor()
        out = sweep_run.run_plan(cells, executor, max_concurrency=8)

        self.assertEqual(out["summary"], "ran 2 cells: 2 ok, 0 errored")


if __name__ == "__main__":
    unittest.main()
