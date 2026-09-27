"""Sweep orchestration — cell enumeration + health-gate + concurrency policy.

This module builds the RQ2 fault-injection matrix's *plan* and gates its
*execution*. It is deliberately split so the parts that touch NOTHING live
(cell enumeration, serialization policy) are pure + offline-testable, while the
one part that perturbs the SHARED federation (fault injection via pod-kill /
tc-netem on an op-worker) is behind an abstraction whose live path REFUSES to
run without the `operator_sanctioned` flag explicitly set.

**`operator_sanctioned` boundary.** Fault injection is not a
self-serve op scoped to spaces this harness creates — a pod-kill or netem qdisc on a provider's
op-worker perturbs EVERY space on that provider (the ~24 pre-existing spaces +
other users), not just the ones created for this sweep. `LiveFaultInjector` raises unless
constructed with `operator_sanctioned=True`, and the default injector is
`DryRunFaultInjector` (records the intended fault, executes nothing). The sweep
loop is fully exercisable end-to-end in dry-run; the live perturbation is a
separate, explicitly-confirmed step.

Verdicts still cut on `physicalSize == expected_size` ONLY — the sweep changes
WHAT trials run, never the predicate.

Stdlib-only. No top-level side effects on import.
"""
from __future__ import annotations

import dataclasses
from typing import Callable, Optional


# --- fault specification -----------------------------------------------------
# The fault conditions. `none` is the RQ1/baseline cell (no
# injection, may run concurrently). `outage` = pod-kill on the target's
# op-worker (target physicalSize stalls; target probes may fail). `netem_lag` =
# a tc-netem delay on the target's op-worker interface; the magnitude is chosen
# relative to T_max so at least one cell sits MARGINALLY near the deadline.
FAULT_NONE = "none"
FAULT_OUTAGE = "outage"
FAULT_NETEM_LAG = "netem_lag"
FAULT_CONDITIONS = (FAULT_NONE, FAULT_OUTAGE, FAULT_NETEM_LAG)


@dataclasses.dataclass(frozen=True)
class FaultSpec:
    """One fault condition to inject on ONE provider for a cell.

    `condition == "none"` → `target_provider` is None and the cell is a
    no-fault (concurrent-eligible) cell. Otherwise `target_provider` is the
    op-worker the fault perturbs, and the cell must serialize against other
    fault cells on the SAME provider (see `serialization_plan`).
    `netem_delay_ms` is set only for `netem_lag`.
    """
    condition: str = FAULT_NONE
    target_provider: Optional[str] = None
    netem_delay_ms: Optional[float] = None

    def __post_init__(self):
        if self.condition not in FAULT_CONDITIONS:
            raise ValueError(f"unknown fault condition {self.condition!r}")
        if self.condition == FAULT_NONE:
            if self.target_provider is not None:
                raise ValueError("fault condition 'none' must not name a target_provider")
        else:
            if not self.target_provider:
                raise ValueError(f"fault condition {self.condition!r} requires a target_provider")
        if self.condition == FAULT_NETEM_LAG and self.netem_delay_ms is None:
            raise ValueError("netem_lag requires netem_delay_ms")
        if self.condition != FAULT_NETEM_LAG and self.netem_delay_ms is not None:
            raise ValueError("netem_delay_ms is only valid for the netem_lag condition")

    @property
    def is_fault(self) -> bool:
        return self.condition != FAULT_NONE

    def to_record(self) -> dict:
        """Render to the schema `fault` block shape (condition + optional
        target/params). injected/cleared times are stamped at execution, not
        here (this is the PLAN, not the runtime record)."""
        rec: dict = {"condition": self.condition}
        if self.target_provider is not None:
            rec["target_provider"] = self.target_provider
        if self.netem_delay_ms is not None:
            rec["netem_delay_ms"] = self.netem_delay_ms
        return rec


# --- cells -------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class Cell:
    """One sweep cell = one (scenario, leg, fault, arm_live) config, replicated
    `trial_k` times. The unit the confusion matrix is built over."""
    scenario_id: str
    model_leg: str
    fault: FaultSpec
    arm_live: str = "A0"          # data-gathering is under A0; A4 is the RQ3 enforcement sweep
    trial_k: int = 1              # replicate count for this cell
    target_provider: Optional[str] = None   # the placement target (for health-gate + labelling)
    source_provider: Optional[str] = None

    @property
    def perturbs_provider(self) -> Optional[str]:
        """The provider whose op-worker this cell's fault touches (None for a
        no-fault cell) — the key the serialization policy groups on."""
        return self.fault.target_provider


def enumerate_cells(
    *,
    scenarios: list,
    legs: list,
    fault_specs: list,
    trial_k: int = 1,
    arm_live: str = "A0",
) -> list:
    """Cross-product (scenario × leg × fault_spec) → one `Cell` each.

    `scenarios` is a list of dicts (make_*_scenario() output — read for
    scenario_id + target/source provider ids). `legs` is a list of leg
    identifiers (model_leg strings). `fault_specs` is a list of `FaultSpec`.
    Every cell carries `trial_k` (replicate count) + `arm_live` uniformly.
    Deterministic order: scenario-major, then leg, then fault — so a sweep is
    reproducible + a resumed sweep re-enumerates identically.
    """
    cells = []
    for scenario in scenarios:
        sid = scenario.get("scenario_id")
        target = scenario.get("target_provider_id")
        # source id may not be on the scenario dict (it carries a source_host +
        # source_provider_label); leave None if absent — it's label-only here.
        source = scenario.get("source_provider_id")
        for leg in legs:
            for fs in fault_specs:
                cells.append(
                    Cell(
                        scenario_id=sid,
                        model_leg=leg,
                        fault=fs,
                        arm_live=arm_live,
                        trial_k=trial_k,
                        target_provider=target,
                        source_provider=source,
                    )
                )
    return cells


def serialization_plan(cells: list) -> dict:
    """Group cells into a concurrency plan.

    Returns `{"concurrent": [no-fault cells...], "serial_groups": {provider:
    [fault cells...]}}`. Policy:
      - No-fault cells (`perturbs_provider is None`) can ALL run concurrently
        (they only read; recorded with a `concurrency` count).
      - Fault cells that perturb the SAME provider MUST serialize (two outages
        on `de` at once would confound each other). Different providers'
        fault-groups run concurrently WITH EACH OTHER (de-outage ∥ uibk-outage
        is fine; the op-workers are distinct).
    The caller runs each `serial_groups[p]` sequentially, the groups + the
    concurrent bucket in parallel.
    """
    concurrent = []
    serial_groups: dict = {}
    for cell in cells:
        p = cell.perturbs_provider
        if p is None:
            concurrent.append(cell)
        else:
            serial_groups.setdefault(p, []).append(cell)
    return {"concurrent": concurrent, "serial_groups": serial_groups}


# --- health gate -------------------------------------------------------------

def health_gate(get_provider_online: Callable, provider_ids: list) -> dict:
    """Pre-flight liveness screen. `get_provider_online` is
    `Verifier.get_provider_online` (or any `pid -> bool|None`); `provider_ids`
    the distinct providers a sweep will touch. Returns `{pid: online_bool_or_None}`.
    A cell whose target (or source) is not `True` here should be SKIPPED (logged,
    not silently dropped) — a sweep must not run trials against a provider
    onezone reports down, or the recording's `Violated` would be an environment
    artefact, not a finding. None (dead probe) is treated as NOT-green (fail
    closed) — a provider that cannot be confirmed up receives no trials.
    """
    return {pid: get_provider_online(pid) for pid in dict.fromkeys(provider_ids)}


def gate_pass(health: dict, provider_ids: list) -> bool:
    """True iff EVERY listed provider is confirmed online (`is True`) in the
    `health` map. Fail-closed on None/False/missing."""
    return all(health.get(pid) is True for pid in provider_ids)


# --- fault injection (the sanction boundary) ---------------------------------

class FaultInjectionError(RuntimeError):
    """Raised when a live fault injection is attempted without sanction, or fails."""


class FaultInjector:
    """Abstract fault-injection interface. `inject(fault_spec) -> handle` starts
    the fault; `clear(handle)` ends it. Implementations decide whether that
    touches anything live."""

    def inject(self, fault_spec: FaultSpec) -> dict:
        raise NotImplementedError

    def clear(self, handle: dict) -> None:
        raise NotImplementedError


class DryRunFaultInjector(FaultInjector):
    """Default injector — records the INTENDED fault, executes nothing. Lets the
    whole sweep loop run end-to-end (enumeration → per-cell record → confusion
    matrix) with no live perturbation. A recording made under dry-run carries
    `fault.condition` as PLANNED but with no real injection, so it is a
    no-fault trial for verdict purposes; use it to validate orchestration, not
    to produce RQ2 ground-truth."""

    def __init__(self):
        self.injected: list = []
        self.cleared: list = []

    def inject(self, fault_spec: FaultSpec) -> dict:
        handle = {"fault_spec": fault_spec, "dry_run": True}
        self.injected.append(fault_spec)
        return handle

    def clear(self, handle: dict) -> None:
        self.cleared.append(handle.get("fault_spec"))


class LiveFaultInjector(FaultInjector):
    """Live injector — pod-kill / tc-netem on a provider's op-worker.

    Refuses to construct without `operator_sanctioned=True`. A live fault
    perturbs a SHARED federation component (every space on the target provider,
    not just the ones created for this sweep), so it falls outside the self-serve
    envelope for spaces this harness creates, and requires the `operator_sanctioned=True`
    flag to be passed explicitly. Even
    sanctioned, the actual `kubectl` / `tc` invocations are provided by the
    caller as `exec_fn` (so the mechanism is auditable + testable and this
    module never hard-codes a destructive command). This class is the gate, not
    the mechanism.
    """

    def __init__(self, exec_fn: Callable, *, operator_sanctioned: bool = False):
        if not operator_sanctioned:
            raise FaultInjectionError(
                "LiveFaultInjector requires operator_sanctioned=True — fault injection "
                "(pod-kill / tc-netem) perturbs a shared provider op-worker (all its spaces) "
                "and must be enabled explicitly; use DryRunFaultInjector otherwise."
            )
        self._exec = exec_fn
        self.operator_sanctioned = True

    def inject(self, fault_spec: FaultSpec) -> dict:
        if not fault_spec.is_fault:
            return {"fault_spec": fault_spec, "noop": True}
        result = self._exec("inject", fault_spec)
        return {"fault_spec": fault_spec, "exec_result": result}

    def clear(self, handle: dict) -> None:
        fault_spec = handle.get("fault_spec")
        if fault_spec is None or not fault_spec.is_fault or handle.get("noop"):
            return
        self._exec("clear", fault_spec)
