"""RQ2 attribution ground-truth labeler + confusion matrix.

Two public entrypoints:

    truth(recording) -> {term_id: "agent"|"service"|"both"|"neither"}
        The ground-truth attribution label per contract term. A pure,
        deterministic function of ONE recording, reading `fault.*` (the
        injection record) as its RQ2 answer key.

    confusion_matrix(recordings, *, strict_target_shortfall=True) -> {
        "matrix": {truth_label: {emit_label: count}},   # 4x4 over the domain
        "accuracy_ground_truthable": float | None,        # rows {agent,service,both} only
        "wild_row": {emit_label: count},                  # truth={neither} row, reported separately
    }
        Scores `a3b.emit()` (the evidential estimator, which MUST NOT read
        `fault.*`) against `truth()` (the oracle, which MAY and does).
        `strict_target_shortfall` is threaded straight through to
        `a3b.emit()` — see that function's
        docstring for the dual-tally toggle it controls.

**Granularity note:** `a3b.emit()` returns BOTH a recording-
level rollup (`emitted_attribution`) AND a per-term breakdown
(`attribution_per_term`). Since `truth()` is per-term, `confusion_matrix`
compares `truth(recording)[tid]` against `emit(recording)["attribution_per_
term"][tid]` for each term — matching granularity — rather than pairing
every term's truth against one recording-level emit label. For the current
E1 Form B scenario (exactly one term per recording) this is numerically
identical to using the rollup, but it is the architecturally correct choice
for any future multi-term contract.

Stdlib-only. No top-level side effects on import.
"""
from __future__ import annotations

from typing import Optional

from . import schema
from .lenses import a3b, competence

ATTRIBUTION_LABELS = ("agent", "service", "both", "neither")
GROUND_TRUTHABLE_LABELS = ("agent", "service", "both")


# --- F: injected-fault overlap -----------------------------------------

def _w_start(recording: dict) -> float:
    """`w_start`: the required placement action's `ts_rel_s`
    (the first `schedule_file_replication` tool call), fallback `0`.

    Deliberately re-implemented here (not imported as `a3b._w_start`) rather
    than reaching into another module's underscore-prefixed internal —
    matches this codebase's convention of each module keeping its own
    private helpers self-contained (schema.py / competence.py / a3b.py each
    do the same rather than cross-import private helpers).
    """
    tool_calls = (recording.get("agent", {}) or {}).get("tool_calls") or []
    calls = [tc for tc in tool_calls if tc.get("name") == a3b.REQUIRED_ACTION_NAME]
    if not calls:
        return 0.0
    return min(tc.get("ts_rel_s", 0.0) for tc in calls)


def _fault_overlaps_term(recording: dict, term_id: str, t_max: Optional[float]) -> bool:
    """`F` = True iff ALL hold:
      - `fault.condition != "none"`, and
      - `fault.target_provider` is on this term's path (in
        `expected.per_term[tid].target_providers`), and
      - `[fault.injected_at_rel_s, fault.cleared_at_rel_s]` intersects the
        convergence window `[w_start, t_max_s]`.

    `cleared_at_rel_s is None` means the fault is still active — treated as
    `+inf` for the interval-intersection check. Two closed intervals
    `[a, b]` and `[c, d]` intersect iff `a <= d and c <= b`; here
    `a=injected_at`, `b=cleared_at`, `c=w_start`, `d=t_max`.
    """
    fault = recording.get("fault", {}) or {}
    if fault.get("condition", "none") == "none":
        return False

    per_term = (recording.get("expected", {}) or {}).get("per_term", {}) or {}
    entry = per_term.get(term_id, {}) or {}
    target_providers = set(entry.get("target_providers") or [])
    if fault.get("target_provider") not in target_providers:
        return False

    if t_max is None:
        return False

    injected_at = fault.get("injected_at_rel_s")
    if injected_at is None:
        # Malformed/incomplete fault record for a non-"none" condition —
        # can't confirm overlap without a start time; conservative False
        # (schema.py requires this field whenever condition != "none", so
        # this path is defensive, not an expected input).
        return False

    cleared_at = fault.get("cleared_at_rel_s")
    if cleared_at is None:
        cleared_at = float("inf")

    w_start = _w_start(recording)
    return injected_at <= t_max and w_start <= cleared_at


# --- the ground-truth decision table (collapsed rules) -------------------

def truth(recording: dict) -> dict:
    """The ground-truth attribution label per contract term
    (collapsed rules):

        Fulfilled     -> "neither", always (rows 1-4; a `data_quality_flag`
                          is raised on rows 3-4 when A=fail, but that flag is
                          NOT part of this function's return value — it is
                          not an attribution).
        NotDetermined -> "agent" if A=="fail" else "neither" (tie-break T3:
                          `F` is recorded elsewhere but never promotes
                          NotDetermined to service/both).
        Violated      -> agent_fault=(A=="fail"), service_fault=F:
                          (T,T)=both (tie-break T1) · (F,T)=service ·
                          (T,F)=agent · (F,F)=neither (wild-caught).

    Pure function of one recording: MAY and DOES read `fault.*` —
    this is the RQ2 oracle side, never used by `a3b.emit()` (which
    withholds `fault.*` from the evidential emitter by design).
    """
    terms = recording.get("contract", {}).get("terms") or []
    a_fail = competence.agent_competence(recording) == "fail"
    verdict_per_term = a3b.verdict(recording)["verdict_per_term"]

    labels: dict = {}
    for term in terms:
        tid = term.get("term_id")
        t_max = term.get("t_max_s")
        v = verdict_per_term.get(tid)

        if v == "Fulfilled":
            labels[tid] = "neither"
        elif v == "NotDetermined":
            labels[tid] = "agent" if a_fail else "neither"
        elif v == "Violated":
            f = _fault_overlaps_term(recording, tid, t_max)
            if a_fail and f:
                labels[tid] = "both"
            elif f:
                labels[tid] = "service"
            elif a_fail:
                labels[tid] = "agent"
            else:
                labels[tid] = "neither"
        else:
            # Defensive only: a3b.verdict()'s vocabulary is closed to
            # {Fulfilled, Violated, NotDetermined} — this branch should be
            # unreachable, but never crash the labeler on an unexpected value.
            labels[tid] = "neither"

    return labels


# --- RQ2 scoring: confusion(emit, truth) -------------------------------------

def confusion_matrix(recordings: list, *, strict_target_shortfall: bool = True) -> dict:
    """Build the RQ2 confusion matrix over `{agent, service, both, neither}`,
    rows = `truth` label, cols = `a3b.emit` label.

    - `matrix`: nested `{truth_label: {emit_label: count}}`, all 16 cells
      always present (zero-filled).
    - `accuracy_ground_truthable`: accuracy over the ground-truthable rows
      ONLY — `{agent}`, `{service}`, `{both}` — never `{neither}`. `None`
      when those rows have zero terms (avoids a spurious 0/0).
    - `wild_row`: the truth={neither} row (its emit distribution), reported
      SEPARATELY — never folded into the accuracy
      denominator, and never relabeled.
    - `leg_errors`: count of recordings SKIPPED because they carry a
      leg-level failure (`schema.is_leg_error`) — a Forge/adapter API error
      is NO-DATA for attribution scoring, never a "neither"/genuine verdict
      (no-silent-caps discipline).
    - `fault_unverified`: count of recordings SKIPPED because they carry a
      fault (`fault.condition != "none"`) whose `inject_verified` is not
      `True` (missing or False) — mirrors `leg_errors`' no-silent-caps
      discipline. A fault cell
      enters service-fault truth only if inject_verified.

    `strict_target_shortfall` (keyword-only, default True): threaded
    straight through to `a3b.emit()` — see its docstring. `truth()` is
    UNAFFECTED (it reads `fault.*`, never `emit()`'s corroboration), so
    only the `emit`-side column of the matrix moves under this flag; the
    row totals (truth distribution) are identical either way. This is the
    dual-tally toggle so RQ2 can be re-scored under
    both the original and the refined lens on the same recordings.
    """
    matrix = {t: {e: 0 for e in ATTRIBUTION_LABELS} for t in ATTRIBUTION_LABELS}
    leg_errors = 0
    fault_unverified = 0

    for recording in recordings:
        if schema.is_leg_error(recording):
            leg_errors += 1
            continue
        fault = recording.get("fault", {}) or {}
        if fault.get("condition", "none") != "none" and fault.get("inject_verified") is not True:
            # A fault cell without a verified injection never enters the
            # service-fault truth population — visible + tallied, NEVER a
            # silent "neither" (same discipline as leg_errors above).
            fault_unverified += 1
            continue
        truth_per_term = truth(recording)
        emit_per_term = a3b.emit(recording, strict_target_shortfall=strict_target_shortfall)[
            "attribution_per_term"
        ]
        for tid, truth_label in truth_per_term.items():
            emit_label = emit_per_term.get(tid)
            if emit_label not in ATTRIBUTION_LABELS:
                continue  # defensive; a3b.emit()'s vocabulary is closed
            matrix[truth_label][emit_label] += 1

    correct = sum(matrix[t][t] for t in GROUND_TRUTHABLE_LABELS)
    total = sum(matrix[t][e] for t in GROUND_TRUTHABLE_LABELS for e in ATTRIBUTION_LABELS)
    accuracy = (correct / total) if total else None

    return {
        "matrix": matrix,
        "accuracy_ground_truthable": accuracy,
        "wild_row": dict(matrix["neither"]),
        "leg_errors": leg_errors,
        "fault_unverified": fault_unverified,
    }
