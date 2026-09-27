"""Sweep concurrency orchestrator.

`sweep.py` builds the PLAN (cell enumeration + serialization policy); this
module RUNS it, given an injected per-cell executor. The executor is the only
thing that could touch anything live (fixture write -> fault inject -> run
trial -> fault clear); it is supplied by the caller, so this module never
imports `harness.runner` / `harness.fixtures` / `harness.verifier` and is
fully offline-testable with a mock executor (see `test_sweep_run.py`).

Concurrency policy (mirrors `sweep.py`'s
`serialization_plan` docstring):
  - No-fault cells (`plan["concurrent"]`) each run as their own task.
  - Same-provider fault cells (`plan["serial_groups"][provider]`) run
    SEQUENTIALLY within their group — one task per group, looping its cells
    in order, so a fault must finish + clear before the next fault on the
    same provider starts.
  - Different provider groups, and the no-fault bucket, all run concurrently
    WITH EACH OTHER — every task (one per no-fault cell, one per fault group)
    is submitted to a single shared `ThreadPoolExecutor`, bounded by
    `max_concurrency`.

Threads, not asyncio: the live executor drives its own event loop internally
(it wraps `run_trial`, which is itself async under the hood via
`asyncio.run`), so orchestrating it from a second asyncio loop in this module
would be nested-loop trouble. A plain thread pool lets each task block on its
own synchronous call into the (possibly async-internally) executor.

Error isolation: an executor call that raises is captured per-cell, not
allowed to crash the sweep or take out sibling cells (including siblings
later in the same serial group's queue — the group keeps going after a
failure so one bad fault-cell doesn't strand its neighbours).

Stdlib-only. No top-level side effects on import.
"""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

if __package__ in (None, ""):
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    try:
        from harness import sweep
    except ImportError:
        import sweep
else:
    from . import sweep


def _run_one(cell, execute_cell: Callable) -> tuple:
    """Run a single cell through the injected executor, capturing any
    exception rather than propagating it. Returns `("ok", cell, result)` or
    `("error", cell, error_str)`."""
    try:
        result = execute_cell(cell)
    except Exception as exc:  # noqa: BLE001 — deliberately broad: error isolation
        return ("error", cell, str(exc))
    return ("ok", cell, result)


def _run_serial_group(
    cells: list, execute_cell: Callable, verify_between: Optional[Callable] = None
) -> list:
    """Run a provider's fault cells one after another (same thread, in
    order). A failing cell is captured and does NOT stop the rest of the
    group — each cell's outcome is independent.

    Clear-verification rider: when
    `verify_between` is supplied, it is called as `verify_between(cell) ->
    bool` after EACH cell in the group. A fault cell that ERRORS might leave
    the provider's netem qdisc NOT cleanly removed; if the next cell in this
    serial group then ran under a lingering netem, its data would be
    invalid. So a `False` return from `verify_between` (meaning: the
    provider's qdisc-clean state could not be verified) ABORTS the
    remaining cells in the group — they are recorded as errors rather than
    executed. `verify_between` itself raising is treated as `False`
    (fail-closed abort), not propagated.
    """
    outcomes: list = []
    for i, cell in enumerate(cells):
        outcomes.append(_run_one(cell, execute_cell))
        if verify_between is not None:
            try:
                clean = verify_between(cell)
            except Exception:  # noqa: BLE001 — fail-closed: unverifiable == dirty
                clean = False
            if not clean:
                for remaining_cell in cells[i + 1:]:
                    outcomes.append((
                        "error",
                        remaining_cell,
                        f"group aborted: qdisc clean unverifiable after {cell}",
                    ))
                break
    return outcomes


def run_plan(
    cells: list,
    execute_cell: Callable,
    *,
    max_concurrency: int = 8,
    verify_between: Optional[Callable] = None,
) -> dict:
    """Run `cells` against the injected `execute_cell(cell) -> result`,
    respecting `sweep.serialization_plan`'s concurrency policy.

    `verify_between`, if supplied, is an injected `verify_between(cell) ->
    bool` (e.g. `NetemInjector.verify_clean`) threaded to `_run_serial_group`
    as the clear-verification rider — see its docstring. Only serial (fault)
    groups are verified; the concurrent no-fault bucket is unaffected. This
    module makes NO live calls itself; `verify_between` is entirely the
    caller's responsibility, same as `execute_cell`.

    Returns:
        {
          "results": [{"cell": Cell, "result": <execute_cell's return>}, ...],
          "errors":  [{"cell": Cell, "error": "<str(exception)>"}, ...],
          "n_cells": len(cells),
          "summary": "ran N cells: S ok, E errored",
        }

    `results` + `errors` together cover every cell in `cells` exactly once
    (order not guaranteed — cells complete as their tasks finish); callers
    that need per-cell lookup should key off the `Cell` object (or a field
    of it) in each entry.
    """
    plan = sweep.serialization_plan(cells)

    results: list = []
    errors: list = []

    with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
        futures = []
        for cell in plan["concurrent"]:
            futures.append(pool.submit(_run_one, cell, execute_cell))
        for group_cells in plan["serial_groups"].values():
            futures.append(
                pool.submit(_run_serial_group, group_cells, execute_cell, verify_between)
            )

        for future in as_completed(futures):
            outcome = future.result()
            # _run_one returns a single (status, cell, payload) tuple;
            # _run_serial_group returns a list of them. Normalize to a list
            # so the collection loop below is uniform either way.
            items = outcome if isinstance(outcome, list) else [outcome]
            for status, cell, payload in items:
                if status == "ok":
                    results.append({"cell": cell, "result": payload})
                else:
                    errors.append({"cell": cell, "error": payload})

    summary = f"ran {len(cells)} cells: {len(results)} ok, {len(errors)} errored"
    return {
        "results": results,
        "errors": errors,
        "n_cells": len(cells),
        "summary": summary,
    }
