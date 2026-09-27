"""Offline tests for harness/sweep_driver.py — the RQ1/RQ2 sweep composition
layer. Stdlib `unittest` only, same import-shim as test_sweep.py /
test_sweep_run.py. Everything mocked: `run_trial_fn` is an async stub that
returns a synthetic schema-valid recording, `injector` is a fake that logs
inject/clear calls, `fixture_mgr`/`verifier` are opaque sentinels passed
through unchanged. NO federation, NO real legs, NO real asyncio-into-SDK.
"""
from __future__ import annotations

import os
import sys
import unittest

if __package__ in (None, ""):
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    try:
        from harness import schema, sweep, sweep_driver
    except ImportError:
        import schema
        import sweep
        import sweep_driver
else:
    from . import schema, sweep, sweep_driver


DE = "7cfe30bef82a2c904c15624f752c58ddchd932"
PL = "ec4761ab47ecda487c46edd328179824chc3bd"


def _make_recording(*, scenario_id, model_leg, target_provider, fault_dict,
                     arm_live="A0", self_reported="done", trial_id=None):
    """A minimal schema-VALID recording, parameterized so tests can assert
    per-cell identity (model_leg, fault, scenario_id) flowed through."""
    tid = "T1"
    return {
        "trial_id": trial_id or f"trial-{model_leg}-{scenario_id}-{fault_dict.get('condition', 'none')}",
        "sweep_id": "sw-test",
        "recorded_at": "2026-07-06T00:00:00Z",
        "harness_version": "test",
        "trial_start_utc": "2026-07-06T00:00:00Z",
        "scenario_id": scenario_id,
        "arm_live": arm_live,
        "model_leg": model_leg,
        "trial_k": 1,
        "source_provider": "src",
        "agent_mcp_host": "host",
        "fault": fault_dict,
        "contract": {"obligated_party": "agent", "terms": [
            {"term_id": tid, "guarantee": "g", "slis": ["physicalSize"],
             "slo": {"predicate": "physicalSize == expected_size"}, "t_max_s": 30.0}
        ]},
        "expected": {"fixture_space_id": "fx", "poll_until_rel_s": 60.0,
                     "per_term": {tid: {"expected_size": 4096,
                                        "target_providers": [target_provider]}}},
        "agent": {"prompt_verbatim": "p", "tool_calls": [], "messages": [],
                  "final_answer": self_reported, "self_reported": self_reported,
                  "reported_done_at_rel_s": 3.0, "raw_trace": "absent-by-design"},
        "state_timeline": {"poll_interval_s": 1.0, "poll_until_rel_s": 60.0,
                           "polled_past_t_max": True, "samples": []},
        "cost": {"verifier": {}, "model": {}, "retry": {}},
        "otel": {"harness_trace_id": "x", "poll_interval_s": 1.0, "correlated": False},
        "leg_scaffold": "sdk_loop",
    }


class FakeInjector:
    """Logs `inject`/`clear` calls (in order) onto a shared list, so tests
    can assert ordering relative to when `run_trial_fn` was invoked."""

    def __init__(self, log):
        self._log = log

    def inject(self, fault_spec):
        self._log.append(("inject", fault_spec))
        return {"handle_for": fault_spec}

    def clear(self, handle):
        self._log.append(("clear", handle))


def _scenario_of_cell(cell):
    return {"scenario_id": cell.scenario_id, "target_provider_id": cell.target_provider}


class TestMakeExecuteCellNoFault(unittest.TestCase):
    def test_no_fault_cell_never_touches_injector(self):
        log = []
        injector = FakeInjector(log)

        async def run_trial_fn(scenario, leg, fixture_mgr, verifier, fault=None, **kwargs):
            log.append(("run_trial", fault))
            return _make_recording(
                scenario_id=scenario["scenario_id"], model_leg=leg["name"],
                target_provider=scenario.get("target_provider_id"), fault_dict=fault,
                arm_live=kwargs.get("arm_live"),
            )

        execute_cell = sweep_driver.make_execute_cell(
            fixture_mgr="FIXTURE_MGR", verifier="VERIFIER",
            legs_by_name={"a": {"name": "a"}}, injector=injector,
            scenario_of_cell=_scenario_of_cell, run_trial_fn=run_trial_fn,
            sweep_id="sw1", poll_until_rel_s=60.0, poll_interval_s=3.0,
            content_size=4096, recordings_dir="/tmp/recs",
        )

        cell = sweep.Cell(
            scenario_id="E1", model_leg="a", fault=sweep.FaultSpec(), arm_live="A0",
            trial_k=1, target_provider=DE, source_provider=PL,
        )
        recording = execute_cell(cell)

        self.assertEqual(log, [("run_trial", {"condition": "none"})])
        self.assertEqual(recording["scenario_id"], "E1")
        self.assertEqual(recording["model_leg"], "a")
        self.assertEqual(recording["fault"], {"condition": "none"})


class TestMakeExecuteCellFault(unittest.TestCase):
    def test_fault_cell_injects_before_and_clears_after_with_correct_fault_dict(self):
        log = []
        injector = FakeInjector(log)

        async def run_trial_fn(scenario, leg, fixture_mgr, verifier, fault=None, **kwargs):
            log.append(("run_trial", fault))
            return _make_recording(
                scenario_id=scenario["scenario_id"], model_leg=leg["name"],
                target_provider=scenario.get("target_provider_id"), fault_dict=fault,
                arm_live=kwargs.get("arm_live"),
            )

        execute_cell = sweep_driver.make_execute_cell(
            fixture_mgr="FIXTURE_MGR", verifier="VERIFIER",
            legs_by_name={"a": {"name": "a"}}, injector=injector,
            scenario_of_cell=_scenario_of_cell, run_trial_fn=run_trial_fn,
            sweep_id="sw1", poll_until_rel_s=60.0, poll_interval_s=3.0,
            content_size=4096, recordings_dir="/tmp/recs",
        )

        fault_spec = sweep.FaultSpec(condition="netem_lag", target_provider=DE, netem_delay_ms=25000.0)
        cell = sweep.Cell(
            scenario_id="E1", model_leg="a", fault=fault_spec, arm_live="A0",
            trial_k=1, target_provider=DE, source_provider=PL,
        )
        recording = execute_cell(cell)

        # order: inject BEFORE run_trial, clear AFTER
        self.assertEqual([entry[0] for entry in log], ["inject", "run_trial", "clear"])
        self.assertIs(log[0][1], fault_spec)  # injector.inject was passed cell.fault
        self.assertEqual(log[1][1], {
            "condition": "netem_lag",
            "target_provider": DE,
            "injected_at_rel_s": 0.0,
            "cleared_at_rel_s": 60.0,
            "netem_delay_ms": 25000.0,
        })
        self.assertEqual(recording["fault"]["condition"], "netem_lag")

    def test_inject_verified_and_qdisc_snapshot_threaded_from_handle(self):
        """When the injector's handle carries a real
        `exec_result` (the shape `sweep.LiveFaultInjector.inject()` produces
        from `netem.NetemInjector.exec_fn`'s return dict), `inject_verified`
        + `qdisc_snapshot` must land in the fault_dict passed to
        `run_trial_fn` -- so RQ2 truth is evidence-backed end-to-end."""
        log = []

        class VerifyingFakeInjector(FakeInjector):
            def inject(self, fault_spec):
                self._log.append(("inject", fault_spec))
                return {
                    "fault_spec": fault_spec,
                    "exec_result": {
                        "action": "inject", "rc": 0, "stdout": "", "stderr": "",
                        "inject_verified": True,
                        "qdisc_snapshot": "qdisc netem 8001: root refcnt 2 limit 1000 delay 25.0s\n",
                    },
                }

        injector = VerifyingFakeInjector(log)

        async def run_trial_fn(scenario, leg, fixture_mgr, verifier, fault=None, **kwargs):
            log.append(("run_trial", fault))
            return _make_recording(
                scenario_id=scenario["scenario_id"], model_leg=leg["name"],
                target_provider=scenario.get("target_provider_id"), fault_dict=fault,
            )

        execute_cell = sweep_driver.make_execute_cell(
            fixture_mgr="FM", verifier="V", legs_by_name={"a": {"name": "a"}},
            injector=injector, scenario_of_cell=_scenario_of_cell, run_trial_fn=run_trial_fn,
            sweep_id="sw1", poll_until_rel_s=60.0, recordings_dir="/tmp/recs",
        )
        fault_spec = sweep.FaultSpec(condition="netem_lag", target_provider=DE, netem_delay_ms=25000.0)
        cell = sweep.Cell(scenario_id="E1", model_leg="a", fault=fault_spec, target_provider=DE, source_provider=PL)
        execute_cell(cell)

        self.assertEqual(log[1][1], {
            "condition": "netem_lag",
            "target_provider": DE,
            "injected_at_rel_s": 0.0,
            "cleared_at_rel_s": 60.0,
            "netem_delay_ms": 25000.0,
            "inject_verified": True,
            "qdisc_snapshot": "qdisc netem 8001: root refcnt 2 limit 1000 delay 25.0s\n",
        })

    def test_dry_run_style_handle_without_exec_result_leaves_fault_dict_unchanged(self):
        """A dry-run / test-double injector (no `exec_result` key on the
        handle, mirroring `sweep.DryRunFaultInjector`) must NOT stamp
        `inject_verified`/`qdisc_snapshot` at all -- an unverifiable cell is
        a data-quality gap for rq2.py's `fault_unverified` bucket to flag,
        never a fabricated key on the fault record."""
        log = []
        injector = FakeInjector(log)  # handle = {"handle_for": fault_spec}, no exec_result

        async def run_trial_fn(scenario, leg, fixture_mgr, verifier, fault=None, **kwargs):
            log.append(("run_trial", fault))
            return _make_recording(
                scenario_id=scenario["scenario_id"], model_leg=leg["name"],
                target_provider=scenario.get("target_provider_id"), fault_dict=fault,
            )

        execute_cell = sweep_driver.make_execute_cell(
            fixture_mgr="FM", verifier="V", legs_by_name={"a": {"name": "a"}},
            injector=injector, scenario_of_cell=_scenario_of_cell, run_trial_fn=run_trial_fn,
            sweep_id="sw1", poll_until_rel_s=60.0, recordings_dir="/tmp/recs",
        )
        fault_spec = sweep.FaultSpec(condition="netem_lag", target_provider=DE, netem_delay_ms=25000.0)
        cell = sweep.Cell(scenario_id="E1", model_leg="a", fault=fault_spec, target_provider=DE, source_provider=PL)
        execute_cell(cell)

        self.assertNotIn("inject_verified", log[1][1])
        self.assertNotIn("qdisc_snapshot", log[1][1])

    def test_outage_fault_dict_has_none_netem_delay(self):
        log = []
        injector = FakeInjector(log)

        async def run_trial_fn(scenario, leg, fixture_mgr, verifier, fault=None, **kwargs):
            log.append(("run_trial", fault))
            return _make_recording(
                scenario_id=scenario["scenario_id"], model_leg=leg["name"],
                target_provider=scenario.get("target_provider_id"), fault_dict=fault,
            )

        execute_cell = sweep_driver.make_execute_cell(
            fixture_mgr="FM", verifier="V", legs_by_name={"a": {"name": "a"}},
            injector=injector, scenario_of_cell=_scenario_of_cell, run_trial_fn=run_trial_fn,
            sweep_id="sw1", poll_until_rel_s=45.0, recordings_dir="/tmp/recs",
        )
        fault_spec = sweep.FaultSpec(condition="outage", target_provider=DE)
        cell = sweep.Cell(scenario_id="E1", model_leg="a", fault=fault_spec, target_provider=DE, source_provider=PL)
        execute_cell(cell)

        self.assertEqual(log[1][1], {
            "condition": "outage",
            "target_provider": DE,
            "injected_at_rel_s": 0.0,
            "cleared_at_rel_s": 45.0,
            "netem_delay_ms": None,
        })

    def test_clear_still_called_when_run_trial_raises_and_exception_propagates(self):
        log = []
        injector = FakeInjector(log)

        async def raising_run_trial_fn(scenario, leg, fixture_mgr, verifier, fault=None, **kwargs):
            log.append(("run_trial_raise", fault))
            raise RuntimeError("boom during trial")

        execute_cell = sweep_driver.make_execute_cell(
            fixture_mgr="FM", verifier="V", legs_by_name={"a": {"name": "a"}},
            injector=injector, scenario_of_cell=_scenario_of_cell,
            run_trial_fn=raising_run_trial_fn, sweep_id="sw1",
            poll_until_rel_s=60.0, recordings_dir="/tmp/recs",
        )
        fault_spec = sweep.FaultSpec(condition="outage", target_provider=DE)
        cell = sweep.Cell(scenario_id="E1", model_leg="a", fault=fault_spec, target_provider=DE, source_provider=PL)

        with self.assertRaisesRegex(RuntimeError, "boom during trial"):
            execute_cell(cell)

        self.assertEqual([entry[0] for entry in log], ["inject", "run_trial_raise", "clear"])


class TestRunSweepHappyPath(unittest.TestCase):
    @staticmethod
    def _execute_cell(cell):
        return _make_recording(
            scenario_id=cell.scenario_id, model_leg=cell.model_leg,
            target_provider=cell.target_provider, fault_dict=cell.fault.to_record(),
            arm_live=cell.arm_live,
        )

    def test_collects_recordings_and_builds_aggregate_and_confusion(self):
        scenarios = [{"scenario_id": "E1", "target_provider_id": DE, "source_provider_id": PL}]
        legs = ["sdk", "forge"]
        fault_specs = [sweep.FaultSpec()]

        stamped = []

        def apply_a2_fn(recording):
            stamped.append(recording["trial_id"])
            return {
                "verdict_per_term": {"T1": "Fulfilled"},
                "raw_response": "ok", "model_id": "claude-sonnet-5",
                "prompt_sha": "abc123abc123", "parse_error": False,
            }

        out = sweep_driver.run_sweep(
            scenarios=scenarios, legs=legs, fault_specs=fault_specs,
            execute_cell=self._execute_cell, trial_k=1, arm_live="A0",
            max_concurrency=4, apply_a2_fn=apply_a2_fn,
        )

        self.assertEqual(out["n_cells"], 2)
        self.assertEqual(out["errors"], [])
        self.assertEqual(len(out["recordings"]), 2)
        self.assertEqual(len(stamped), 2)

        for rec in out["recordings"]:
            self.assertEqual(rec["a2_result"]["verdict_per_term"], {"T1": "Fulfilled"})
            schema.validate(rec)  # every recording stays schema-valid after stamping

        self.assertIn("cells", out["aggregate"])
        self.assertIsInstance(out["aggregate"]["cells"], list)
        self.assertIn("matrix", out["confusion"])
        self.assertIn("accuracy_ground_truthable", out["confusion"])

    def test_without_apply_a2_fn_recordings_are_not_stamped(self):
        scenarios = [{"scenario_id": "E1", "target_provider_id": DE, "source_provider_id": PL}]
        out = sweep_driver.run_sweep(
            scenarios=scenarios, legs=["sdk"], fault_specs=[sweep.FaultSpec()],
            execute_cell=self._execute_cell,
        )
        self.assertEqual(len(out["recordings"]), 1)
        self.assertNotIn("a2_result", out["recordings"][0])


class TestRunSweepThreadsVerifyBetween(unittest.TestCase):
    """`run_sweep`'s `verify_between` kwarg must reach `sweep_run.run_plan`
    unchanged — the netem clear-verification rider is threaded through this
    composition layer, not consumed or transformed here."""

    @staticmethod
    def _execute_cell(cell):
        return _make_recording(
            scenario_id=cell.scenario_id, model_leg=cell.model_leg,
            target_provider=cell.target_provider, fault_dict=cell.fault.to_record(),
            arm_live=cell.arm_live,
        )

    def test_verify_between_is_forwarded_to_run_plan(self):
        scenarios = [{"scenario_id": "E1", "target_provider_id": DE, "source_provider_id": PL}]
        recorded_kwargs = {}
        real_run_plan = sweep_driver.sweep_run.run_plan

        def spy_run_plan(cells, execute_cell, **kwargs):
            recorded_kwargs.update(kwargs)
            return real_run_plan(cells, execute_cell, **kwargs)

        sentinel = lambda cell: True  # noqa: E731 — throwaway test sentinel
        sweep_driver.sweep_run.run_plan = spy_run_plan
        try:
            sweep_driver.run_sweep(
                scenarios=scenarios, legs=["sdk"], fault_specs=[sweep.FaultSpec()],
                execute_cell=self._execute_cell, verify_between=sentinel,
            )
        finally:
            sweep_driver.sweep_run.run_plan = real_run_plan

        self.assertIs(recorded_kwargs.get("verify_between"), sentinel)

    def test_default_call_forwards_verify_between_none(self):
        scenarios = [{"scenario_id": "E1", "target_provider_id": DE, "source_provider_id": PL}]
        recorded_kwargs = {}
        real_run_plan = sweep_driver.sweep_run.run_plan

        def spy_run_plan(cells, execute_cell, **kwargs):
            recorded_kwargs.update(kwargs)
            return real_run_plan(cells, execute_cell, **kwargs)

        sweep_driver.sweep_run.run_plan = spy_run_plan
        try:
            sweep_driver.run_sweep(
                scenarios=scenarios, legs=["sdk"], fault_specs=[sweep.FaultSpec()],
                execute_cell=self._execute_cell,
            )
        finally:
            sweep_driver.sweep_run.run_plan = real_run_plan

        self.assertIsNone(recorded_kwargs.get("verify_between"))
        self.assertIn("verify_between", recorded_kwargs)  # explicitly passed, not just absent


class TestRunSweepEmptyRecordings(unittest.TestCase):
    def test_all_cells_erroring_returns_empty_recordings_without_crash(self):
        scenarios = [{"scenario_id": "E1", "target_provider_id": DE, "source_provider_id": PL}]

        def always_raises(cell):
            raise RuntimeError("always fails")

        out = sweep_driver.run_sweep(
            scenarios=scenarios, legs=["a"], fault_specs=[sweep.FaultSpec()],
            execute_cell=always_raises,
        )

        self.assertEqual(out["n_cells"], 1)
        self.assertEqual(out["recordings"], [])
        self.assertEqual(len(out["errors"]), 1)
        self.assertEqual(out["aggregate"], {"cells": []})
        self.assertEqual(out["confusion"]["accuracy_ground_truthable"], None)
        self.assertEqual(out["confusion"]["matrix"]["agent"]["agent"], 0)


if __name__ == "__main__":
    unittest.main()
