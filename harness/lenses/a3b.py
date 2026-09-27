"""A3b — convergence-aware two-axis verifier (contribution): the ground-truth
verdict for RQ1 PLUS the evidential
emission procedure that completes the attribution decision.

Two public entrypoints:

    verdict(recording) -> {"verdict_per_term": {...}, "violation_mode_per_term": {...}}
    emit(recording, verdict_result=None) -> {
        "emitted_attribution": "agent"|"service"|"both"|"neither",     # recording-level rollup
        "attribution_per_term": {tid: "agent"|"service"|"both"|"neither"},
        "confidence_per_term": {tid: "high"|"inferred"|None},
        "corroboration_per_term": {tid: [<fired classes>]},
        "data_quality_flag_per_term": {tid: bool},
        "agent_competence": "pass"|"fail",
    }

`emit()` is a pure function of VERIFIER OBSERVABLES ONLY — `agent.*`,
`state_timeline.*` (incl. `transfer_status_samples`) — and MUST NOT read
`fault.*` (the injection record is
RQ2's answer key). See `test_lenses.py`'s explicit property-style
assertion that swapping a recording's `fault` block leaves `emit()`'s
output unchanged.

NOTE (resolved spec-vs-real-data ambiguity — see also competence.py's
module docstring): `converged_sample` below implements the
helper literally (`s.provider ∈ expected.per_term[tid].target_providers`,
exact membership). Two early real recordings record
`target_providers` as a human label (e.g. "de") while `state_timeline.
samples[].provider` carries the provider's raw ID hash. This check compares
exactly rather than applying an unspecified heuristic mapping, so such
samples do not count as a match. Normalizing the two fields to one
identifier namespace would address this, parallel
to the `provider_health` schema's own revision.

Stdlib-only. No top-level side effects on import.
"""
from __future__ import annotations

from typing import Optional

from . import competence as competence_mod

PHYSICAL_SIZE_SLI = "physicalSize"
HEALTH_SLI_NAME = "provider_health"
TRANSFER_STATUS_SLI = "get_transfer.replicationStatus"
REQUIRED_ACTION_NAME = "schedule_file_replication"
TARGET_ARG_KEY = "target_provider_id"
NON_TERMINAL_TRANSFER_STATUSES = {"scheduled", "active"}

E_PROBE = "E-probe"
E_STUCK = "E-stuck"
E_HEALTH = "E-health"
E_STATUS_SHORTFALL = "E-status-shortfall"


# --- shared helper --------------------------------------------------------

def converged_sample(recording: dict, term_id: str) -> Optional[dict]:
    """The FIRST sample (by t_rel_s, no time bound applied here)
    that is `probe_ok` on a target provider for `term_id`, `sli_name ==
    "physicalSize"`, with `value == expected_size`. Returns None if no such
    sample exists anywhere in the recorded timeline.
    """
    per_term = (recording.get("expected", {}) or {}).get("per_term", {}) or {}
    entry = per_term.get(term_id, {}) or {}
    target_providers = set(entry.get("target_providers") or [])
    expected_size = entry.get("expected_size")
    samples = (recording.get("state_timeline", {}) or {}).get("samples") or []

    if expected_size is None:
        return None

    candidates = [
        s
        for s in samples
        if s.get("probe_ok") is True
        and s.get("provider") in target_providers
        and s.get("term_id") == term_id
        and s.get("sli_name") == PHYSICAL_SIZE_SLI
        and s.get("value") == expected_size
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda s: s["t_rel_s"])


def _observable_in_window(recording: dict, term_id: str, t_max: float) -> bool:
    """>=1 probe_ok physicalSize sample ON A TARGET PROVIDER for this term
    within [0, t_max]. Scoped to expected.per_term[term_id].target_providers
    (matching converged_sample / _latest_physical / a3a's filter) so an
    unobserved contracted TARGET reads NotDetermined, not Violated — the
    epistemically-honest ND/Violated line.
    """
    per_term = (recording.get("expected", {}) or {}).get("per_term", {}) or {}
    entry = per_term.get(term_id, {}) or {}
    target_providers = set(entry.get("target_providers") or [])
    samples = (recording.get("state_timeline", {}) or {}).get("samples") or []
    return any(
        s.get("term_id") == term_id
        and s.get("sli_name") == PHYSICAL_SIZE_SLI
        and s.get("provider") in target_providers
        and s.get("probe_ok") is True
        and 0 <= s.get("t_rel_s", -1) <= t_max
        for s in samples
    )


# --- A3b verdict ------------------------------------------------------------

def verdict(recording: dict) -> dict:
    """The ground-truth verdict (== RQ1's ground truth by construction).

    Fulfilled     iff converged_sample(tid) exists at t_rel_s <= t_max_s.
    Violated      iff no such sample at t_rel_s <= t_max_s WHILE OBSERVABLE
                      (>=1 probe_ok sample in [0, t_max]); violation_mode =
                      "late_convergence" if a converged sample exists at
                      t_rel_s > t_max_s, else "never_converged".
    NotDetermined iff no probe_ok sample in the window (epistemic only).
    """
    terms = recording.get("contract", {}).get("terms") or []
    verdict_per_term: dict = {}
    violation_mode_per_term: dict = {}

    for term in terms:
        tid = term.get("term_id")
        t_max = term.get("t_max_s")

        cs = converged_sample(recording, tid)
        if cs is not None and t_max is not None and cs["t_rel_s"] <= t_max:
            verdict_per_term[tid] = "Fulfilled"
            violation_mode_per_term[tid] = None
            continue

        if t_max is None or not _observable_in_window(recording, tid, t_max):
            verdict_per_term[tid] = "NotDetermined"
            violation_mode_per_term[tid] = None
            continue

        verdict_per_term[tid] = "Violated"
        violation_mode_per_term[tid] = "late_convergence" if cs is not None else "never_converged"

    return {"verdict_per_term": verdict_per_term, "violation_mode_per_term": violation_mode_per_term}


# --- evidential-emission procedure ------------------------------------------

def _w_start(recording: dict) -> float:
    """The placement action's ts_rel_s, fallback 0."""
    tool_calls = (recording.get("agent", {}) or {}).get("tool_calls") or []
    calls = [tc for tc in tool_calls if tc.get("name") == REQUIRED_ACTION_NAME]
    if not calls:
        return 0.0
    return min(tc.get("ts_rel_s", 0.0) for tc in calls)


def _latest_physical(samples: list, term_id: str, target_providers: set):
    """The most recent (by t_rel_s) probe_ok physicalSize sample on a target
    provider for this term, or None if no such sample exists. Shared by the
    E-stuck and E-status-shortfall branches — both need "the
    latest observed physicalSize" to decide whether the target is still
    short of `expected_size`.
    """
    physical_samples_sorted = sorted(
        (
            s
            for s in samples
            if s.get("term_id") == term_id
            and s.get("sli_name") == PHYSICAL_SIZE_SLI
            and s.get("provider") in target_providers
            and s.get("probe_ok") is True
        ),
        key=lambda s: s["t_rel_s"],
    )
    return physical_samples_sorted[-1]["value"] if physical_samples_sorted else None


def _scheduled_target_provider(recording: dict) -> Optional[str]:
    """The `target_provider_id` the agent's FIRST ok `schedule_file_replication`
    tool_call was issued against, or `None` if no such call exists. Mirrors
    `runner._extract_transfer_id`'s selection (iterate `agent.tool_calls[]` in
    recorded order, take the first entry with `name == REQUIRED_ACTION_NAME`
    and `ok is True`) so the "which op is being scored" question is answered
    identically to how the runner picked the transfer to poll — a second,
    later schedule call (e.g. a retry) is deliberately NOT considered here,
    same as the runner never polls it either.
    """
    tool_calls = (recording.get("agent", {}) or {}).get("tool_calls") or []
    for tc in tool_calls:
        if tc.get("name") == REQUIRED_ACTION_NAME and tc.get("ok"):
            return (tc.get("args") or {}).get(TARGET_ARG_KEY)
    return None


def _shortfall_op_targeted_contract(recording: dict, entry: dict) -> bool:
    """E-status-shortfall's terminal-transfer signal is only SERVICE-corroboration
    if the terminal transfer actually targeted a CONTRACT target for this
    term — membership in `target_providers` (canonical provider-ID namespace)
    OR `target_provider_labels` (the human-label namespace some recordings
    still carry alongside it; see the module docstring's namespace note).

    Without this guard, a WRONG-target replication (e.g. the agent schedules
    to the SOURCE provider instead of the contracted target) completes
    trivially — the file is already there — producing a terminal status
    while the CONTRACT target's physicalSize stays short of expected. That
    terminal-status-with-shortfall pattern is exactly what E-status-shortfall
    was built to detect, but here it is the agent's own wrong op echoed back
    by the verifier, not evidence the SERVICE failed to converge the
    correctly-targeted op. A wrong-target completion counts as
    agent evidence, not service corroboration.

    Returns `True` (i.e. "don't block") when there is NO ok schedule call at
    all (the shadow case) — E-status-shortfall's terminal-transfer-sample
    precondition is independent of the agent's tool-call trace, and the
    "no schedule op" case is not the wrong-target class this guard targets,
    so the original (unguarded) behavior is preserved there.
    """
    scheduled_target = _scheduled_target_provider(recording)
    if scheduled_target is None:
        return True
    contract_targets = set(entry.get("target_providers") or []) | set(
        entry.get("target_provider_labels") or []
    )
    return scheduled_target in contract_targets


def _corroboration_classes(
    recording: dict, term_id: str, t_max: float, w_start: float, strict_target_shortfall: bool = True
) -> set:
    """The four observable corroborating classes, evaluated
    on the contracted target within [w_start, t_max].

    `strict_target_shortfall` (default True, the current behavior)
    gates E-status-shortfall on `_shortfall_op_targeted_contract` per the
    guard above. `False` disables the guard (E-status-shortfall fires on any
    terminal status + shortfall, regardless of which provider the op
    targeted) — an explicit toggle (mirrors `a1.verdict`'s
    `vacuous_pass`) so RQ2 can be re-scored under both lens versions on the
    same recordings.
    """
    fired: set = set()

    per_term = (recording.get("expected", {}) or {}).get("per_term", {}) or {}
    entry = per_term.get(term_id, {}) or {}
    target_providers = set(entry.get("target_providers") or [])
    expected_size = entry.get("expected_size")
    samples = (recording.get("state_timeline", {}) or {}).get("samples") or []

    # E-probe: >=1 probe_ok==false sample on a target provider in-window.
    for s in samples:
        if (
            s.get("term_id") == term_id
            and s.get("sli_name") == PHYSICAL_SIZE_SLI
            and s.get("provider") in target_providers
            and s.get("probe_ok") is False
            and w_start <= s.get("t_rel_s", -1) <= t_max
        ):
            fired.add(E_PROBE)
            break

    # E-stuck: transfer_status_samples non-terminal at/after t_max while the
    # latest physicalSize < expected.
    transfer_samples = (recording.get("state_timeline", {}) or {}).get("transfer_status_samples") or []
    stuck_at_or_after_tmax = any(
        s.get("term_id") == term_id
        and s.get("t_rel_s", -1) >= t_max
        and s.get("value") in NON_TERMINAL_TRANSFER_STATUSES
        for s in transfer_samples
    )
    if stuck_at_or_after_tmax and expected_size is not None:
        latest_physical = _latest_physical(samples, term_id, target_providers)
        if latest_physical is None or latest_physical < expected_size:
            fired.add(E_STUCK)

    # E-status-shortfall: transfer_status_samples shows a TERMINAL status
    # (any non-empty value NOT in NON_TERMINAL_TRANSFER_STATUSES, e.g.
    # "completed") while the latest physicalSize < expected -- the
    # "completed-with-0-bytes" divergence class. Sibling to
    # E-stuck: E-stuck is the mid-flight non-terminal stall, this is the
    # terminal-but-data-absent case E-stuck's non-terminal check cannot see.
    #
    # Gated on `strict_target_shortfall` --
    # a terminal status only counts as SERVICE-corroboration if the op that
    # went terminal actually targeted a CONTRACT target (see
    # `_shortfall_op_targeted_contract`). A wrong-target completion (e.g.
    # scheduled to the SOURCE, which converges trivially) is agent-evidence
    # echoed back, not proof the correctly-targeted transfer stalled.
    terminal_status_present = any(
        s.get("term_id") == term_id
        and isinstance(s.get("value"), str)
        and s.get("value")
        and s.get("value") not in NON_TERMINAL_TRANSFER_STATUSES
        for s in transfer_samples
    )
    if terminal_status_present and expected_size is not None:
        latest_physical = _latest_physical(samples, term_id, target_providers)
        if latest_physical is None or latest_physical < expected_size:
            if not strict_target_shortfall or _shortfall_op_targeted_contract(recording, entry):
                fired.add(E_STATUS_SHORTFALL)

    # E-health: a provider_health==false sample on the contracted target
    # in-window.
    for s in samples:
        if (
            s.get("sli_name") == HEALTH_SLI_NAME
            and s.get("term_id") == term_id
            and s.get("provider") in target_providers
            and s.get("value") is False
            and w_start <= s.get("t_rel_s", -1) <= t_max
        ):
            fired.add(E_HEALTH)
            break

    return fired


def emit(
    recording: dict,
    verdict_result: Optional[dict] = None,
    *,
    strict_target_shortfall: bool = True,
) -> dict:
    """The A3b evidential-emission procedure. Reads verifier
    observables ONLY — never `record["fault"]`.

    `strict_target_shortfall` (keyword-only, default True): gates the
    E-status-shortfall corroboration class on the terminal transfer having
    actually targeted a CONTRACT target.
    Set `False` to disable the guard (E-status-shortfall fires regardless
    of which provider the terminal transfer targeted) — a toggle mirroring `a1.verdict`'s
    `vacuous_pass`, so both lens versions can be scored on the same
    recordings. Does NOT affect `verdict()` — only this function's emit
    (attribution/corroboration/confidence), never the Fulfilled/Violated/
    NotDetermined verdict itself.
    """
    if verdict_result is None:
        verdict_result = verdict(recording)
    verdict_per_term = verdict_result["verdict_per_term"]

    agent_competence = competence_mod.agent_competence(recording)
    agent_evidence = agent_competence == "fail"

    terms = recording.get("contract", {}).get("terms") or []

    attribution_per_term: dict = {}
    confidence_per_term: dict = {}
    corroboration_per_term: dict = {}
    data_quality_flag_per_term: dict = {}

    for term in terms:
        tid = term.get("term_id")
        t_max = term.get("t_max_s")
        v = verdict_per_term.get(tid)

        if v != "Violated":
            # Fulfilled/NotDetermined -> neither (deliberately
            # NOT the ground-truth-labeler's row-10 NotDetermined+fail=agent
            # asymmetry — that rule belongs to `truth()`, not `emit()`).
            attribution_per_term[tid] = "neither"
            confidence_per_term[tid] = None
            corroboration_per_term[tid] = []
            data_quality_flag_per_term[tid] = v == "Fulfilled" and agent_evidence
            continue

        w_start = _w_start(recording)
        fired = (
            _corroboration_classes(recording, tid, t_max, w_start, strict_target_shortfall)
            if t_max is not None
            else set()
        )
        service_evidence = (agent_competence == "pass") or bool(fired)

        if agent_evidence and service_evidence:
            attribution_per_term[tid] = "both"
        elif service_evidence:
            attribution_per_term[tid] = "service"
        elif agent_evidence:
            attribution_per_term[tid] = "agent"
        else:
            attribution_per_term[tid] = "neither"

        confidence_per_term[tid] = ("high" if fired else "inferred") if service_evidence else None
        corroboration_per_term[tid] = sorted(fired)
        data_quality_flag_per_term[tid] = False

    agent_present = any(v in ("agent", "both") for v in attribution_per_term.values())
    service_present = any(v in ("service", "both") for v in attribution_per_term.values())
    if agent_present and service_present:
        emitted_attribution = "both"
    elif service_present:
        emitted_attribution = "service"
    elif agent_present:
        emitted_attribution = "agent"
    else:
        emitted_attribution = "neither"

    return {
        "emitted_attribution": emitted_attribution,
        "attribution_per_term": attribution_per_term,
        "confidence_per_term": confidence_per_term,
        "corroboration_per_term": corroboration_per_term,
        "data_quality_flag_per_term": data_quality_flag_per_term,
        "agent_competence": agent_competence,
    }
