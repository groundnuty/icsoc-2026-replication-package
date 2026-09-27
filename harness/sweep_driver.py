"""RQ1/RQ2 sweep driver.

Composes the already-built pieces into the sweep the RQ1/RQ2 experiment
actually runs:

    sweep.py       -> cell enumeration + fault-injection abstraction (the PLAN)
    sweep_run.py   -> concurrency orchestrator (RUNS the plan against an
                      injected `execute_cell`)
    runner.py      -> `run_trial` (the per-cell record loop; injected here as
                      `run_trial_fn`, never imported directly)
    aggregate.py   -> RQ1 cell rollup
    rq2.py         -> RQ2 confusion matrix

This module itself imports NOTHING that pulls in the model-leg dependencies
(claude-agent-sdk / openai / mcp) — `run_trial_fn` and the fixture/verifier/
injector objects are all supplied by the caller, so `make_execute_cell` and
`run_sweep` run under stdlib python with mocks (see `test_sweep_driver.py`).
The live caller is expected to pass `harness.runner.run_trial` as
`run_trial_fn` — this module does not import `harness.runner` itself so it
stays import-safe without the model-leg venv.

Stdlib-only. No top-level side effects on import.
"""
from __future__ import annotations

import asyncio
import os
import sys
from typing import Callable, Optional

if __package__ in (None, ""):
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    try:
        from harness import aggregate, rq2, sweep, sweep_run
    except ImportError:
        import aggregate
        import rq2
        import sweep
        import sweep_run
else:
    from . import aggregate, rq2, sweep, sweep_run


def make_execute_cell(
    *,
    fixture_mgr,
    verifier,
    legs_by_name: dict,
    injector,
    scenario_of_cell: Callable,
    run_trial_fn: Callable,
    sweep_id: str,
    poll_until_rel_s: float = 60.0,
    poll_interval_s: float = 3.0,
    content_size: int = 4096,
    recordings_dir: str,
) -> Callable:
    """Build a sync `execute_cell(cell) -> recording` closure over the
    injected live-or-mock dependencies, suitable as `sweep_run.run_plan`'s
    executor.

    `legs_by_name` maps `cell.model_leg` -> a leg object; `scenario_of_cell`
    builds the per-cell scenario dict (the live caller wires this to
    `runner.make_e1_form_b_scenario`); `run_trial_fn` is an ASYNC callable
    with `runner.run_trial`'s signature (the live caller passes
    `runner.run_trial` itself; tests inject an async stub).

    Fault handling: a fault cell's `injector.inject(cell.fault)` runs BEFORE
    the trial and `injector.clear(handle)` runs in a `finally` — so the
    fault is always cleared, even if the trial raises. A no-fault cell never
    touches the injector. netem is treated as active for the WHOLE trial
    window (inject-before, clear-after), so the fault record stamps
    `injected_at_rel_s=0.0` / `cleared_at_rel_s=poll_until_rel_s` — the
    honest record of when the perturbation was actually in effect relative
    to the trial's own epoch.

    `asyncio.run(...)` is correct here (not `await`): `sweep_run.run_plan`
    calls `execute_cell` from a plain `ThreadPoolExecutor` thread, never from
    inside an event loop, so each cell gets its own fresh loop.
    """

    def execute_cell(cell) -> dict:
        leg = legs_by_name[cell.model_leg]
        scenario = scenario_of_cell(cell)

        if cell.fault.is_fault:
            fault_dict = {
                "condition": cell.fault.condition,
                "target_provider": cell.fault.target_provider,
                "injected_at_rel_s": 0.0,
                "cleared_at_rel_s": poll_until_rel_s,
                "netem_delay_ms": cell.fault.netem_delay_ms,
            }
            handle = injector.inject(cell.fault)
            # Thread the injector's per-cell inject-
            # verification evidence into the fault record so RQ2 truth is
            # evidence-backed (schema.py / rq2.py's fault_unverified gate).
            # `LiveFaultInjector.inject()` nests the real exec_fn result
            # under handle["exec_result"] (netem.NetemInjector.exec_fn's
            # return dict carries inject_verified + qdisc_snapshot); a
            # dry-run / test-double injector has no such key, in which case
            # the keys are left ABSENT (not stamped as False) — an
            # unverifiable cell is a data-quality gap for rq2.py to flag,
            # never a fabricated verdict.
            exec_result = handle.get("exec_result") if isinstance(handle, dict) else None
            if isinstance(exec_result, dict):
                if "inject_verified" in exec_result:
                    fault_dict["inject_verified"] = exec_result["inject_verified"]
                if "qdisc_snapshot" in exec_result:
                    fault_dict["qdisc_snapshot"] = exec_result["qdisc_snapshot"]
            try:
                return asyncio.run(
                    run_trial_fn(
                        scenario,
                        leg,
                        fixture_mgr,
                        verifier,
                        fault=fault_dict,
                        sweep_id=sweep_id,
                        arm_live=cell.arm_live,
                        poll_until_rel_s=poll_until_rel_s,
                        poll_interval_s=poll_interval_s,
                        content_size=content_size,
                        recordings_dir=recordings_dir,
                    )
                )
            finally:
                injector.clear(handle)

        fault_dict = {"condition": "none"}
        return asyncio.run(
            run_trial_fn(
                scenario,
                leg,
                fixture_mgr,
                verifier,
                fault=fault_dict,
                sweep_id=sweep_id,
                arm_live=cell.arm_live,
                poll_until_rel_s=poll_until_rel_s,
                poll_interval_s=poll_interval_s,
                content_size=content_size,
                recordings_dir=recordings_dir,
            )
        )

    return execute_cell


def run_sweep(
    *,
    scenarios: list,
    legs: list,
    fault_specs: list,
    execute_cell: Callable,
    trial_k: int = 1,
    arm_live: str = "A0",
    max_concurrency: int = 8,
    apply_a2_fn: Optional[Callable] = None,
    verify_between: Optional[Callable] = None,
) -> dict:
    """Enumerate the (scenario x leg x fault) cross-product, run it through
    `execute_cell` under `sweep_run.run_plan`'s concurrency policy, and
    aggregate the resulting recordings into the RQ1 cell table + RQ2
    confusion matrix.

    `legs` is a list of leg-name STRINGS (`sweep.enumerate_cells` wants
    identifiers, not leg objects — resolving a name to an actual leg object
    is `make_execute_cell`'s `legs_by_name` concern, upstream of this
    function).

    If `apply_a2_fn` is given, it is called on each recording as
    `apply_a2_fn(recording) -> a2_result_dict` and the result is stamped
    onto `recording["a2_result"]` (the live caller passes
    `a2_judge.apply_a2`). Left `None`, recordings are returned as-is
    (no A2 pass).

    `verify_between`, if given, is threaded straight through to
    `sweep_run.run_plan`'s `verify_between` param — the netem clear-
    verification rider (e.g. `NetemInjector.verify_clean`) that aborts the
    remaining cells in a serial (fault) group when the provider's qdisc-
    clean state can't be verified after a cell. Left `None` (the default),
    behavior is byte-identical to before this param existed — no clear
    verification is performed.

    Never crashes on zero recordings (e.g. every cell errored) —
    `aggregate.aggregate([])` and `rq2.confusion_matrix([])` both already
    handle the empty-list case.
    """
    cells = sweep.enumerate_cells(
        scenarios=scenarios,
        legs=legs,
        fault_specs=fault_specs,
        trial_k=trial_k,
        arm_live=arm_live,
    )

    outcome = sweep_run.run_plan(
        cells, execute_cell, max_concurrency=max_concurrency, verify_between=verify_between
    )

    recordings = [r["result"] for r in outcome["results"] if r["result"] is not None]

    if apply_a2_fn is not None:
        for recording in recordings:
            recording["a2_result"] = apply_a2_fn(recording)

    return {
        "recordings": recordings,
        "errors": outcome["errors"],
        "n_cells": outcome["n_cells"],
        "aggregate": aggregate.aggregate(recordings, arms=("A0", "A1", "A2", "A3a", "A3b")),
        "confusion": rq2.confusion_matrix(recordings),
    }
