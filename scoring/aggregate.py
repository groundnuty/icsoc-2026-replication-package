"""RQ1 aggregation — docs/phase1-lens-spec.md §5.

Per cell `(arm x leg_scaffold x scenario_id)`, compares each prior-practice
arm's (A0/A1) and A3a's per-term verdict against the GROUND-TRUTH verdict
(= A3b's verdict, by construction — lens-spec §0/§5):

    false-pass: arm says Fulfilled, ground-truth says Violated
                (A0/A1's signature failure — the headline).
    false-fail: arm says Violated, ground-truth says Fulfilled
                (A3a's signature failure on late-convergence).
    agreement:  arm's verdict matches ground-truth exactly.
    other_mismatch: any other combination that isn't false-pass/false-fail/
                agreement (e.g. either side is NotDetermined while the
                other isn't) — tracked so the three headline rates never
                silently have to sum to 1 when they don't.

`arm_class in {prior_practice, contribution}` is carried per row so tables
read "prior-practice false-passes," never "our-variant-A1" (lens-spec §5).
A3b is included as its own row with agreement_rate == 1.0 by construction
— stated explicitly, not hidden (§5's own instruction).

**Cross-leg discipline** (§5, science #12 rider): cells are keyed by
`(arm, leg_scaffold, scenario_id)` — `leg_scaffold` is part of the key, so
`harness_react` and `sdk_loop` trials are NEVER silently pooled into one
row. `otel.correlated` is recorded per cell (and flagged `_mixed` if a cell
somehow spans both truth values, which shouldn't happen given
`leg_scaffold` already determines `otel.correlated` 1:1 per
`runner.py::_otel_correlated_for`).

Also provides `violation_mode_distribution` and `not_determined_rate` —
the two of the three places §5 says A3b's OWN quality is characterized
(the third, the T_max-sensitivity lens, is `§12.1` future work, out of
scope for this increment).

Stdlib-only. No top-level side effects on import.
"""
from __future__ import annotations

from typing import Optional

from . import schema
from .lenses import a0, a1, a3a, a3b

def a2_recorded_verdict(recording: dict) -> dict:
    """A2's per-term verdict, read from the RECORDED `a2_result` field —
    NEVER recomputed. A2 is the one stochastic, non-re-derivable lens (a live
    LLM call, lenses/a2.py module docstring); its verdict is persisted at
    record-time and this function reads that persisted state. Returns `{}`
    (NOT NotDetermined-per-term) when `a2_result` is absent — an unscored
    recording contributes no terms to the A2 cell rather than a synthetic ND,
    so `not_determined_rate` isn't inflated by recordings A2 never saw.
    """
    return (recording.get("a2_result") or {}).get("verdict_per_term", {})


ARM_VERDICT_FNS = {
    "A0": a0.verdict,
    "A1": a1.verdict,
    "A2": a2_recorded_verdict,
    "A3a": a3a.verdict,
    "A3b": lambda recording: a3b.verdict(recording)["verdict_per_term"],
}

ARM_CLASS = {
    "A0": "prior_practice",
    "A1": "prior_practice",
    "A2": "prior_practice",
    "A3a": "contribution",
    "A3b": "contribution",
}

DEFAULT_ARMS = ("A0", "A1", "A3a", "A3b")


def validated_ingest(recordings: list) -> dict:
    """Split `recordings` into schema-valid vs excluded vs leg-error, so RQ1
    aggregation never scores a malformed recording NOR a leg-level failure
    (adapter/API error — e.g. a hosted model that is temporarily unavailable) as
    if it were an agent-competence signal.

    Returns `{"valid": [...], "excluded": [{"trial_id", "reason"}...],
    "leg_errors": [{"trial_id", "model_leg", "error"}...]}`. `"excluded"` is
    schema-INVALID only; a schema-valid recording carrying `agent.error`
    (see `schema.is_leg_error`) is routed to `"leg_errors"` instead, NOT
    into `"valid"` — so `aggregate(validated_ingest(recs)["valid"])`
    transparently skips leg-error trials with no caller changes needed. The
    canonical caller for RQ1 is `aggregate(validated_ingest(recs)["valid"])`,
    and it MUST surface both `len(excluded)` and `len(leg_errors)`
    (no-silent-caps discipline, silent-fallback-hazards Pattern) — the stale
    label-namespace smoke recordings the cross-namespace guard now rejects
    (schema.py / science #15) are excluded HERE with their SchemaError
    message rather than silently dropped or crashing the run. Never raises
    on a bad record.
    """
    valid, excluded, leg_errors = [], [], []
    for rec in recordings:
        try:
            schema.validate(rec)
        except schema.SchemaError as e:
            excluded.append({"trial_id": (rec or {}).get("trial_id"), "reason": str(e)})
            continue
        if schema.is_leg_error(rec):
            leg_errors.append({
                "trial_id": rec.get("trial_id"),
                "model_leg": rec.get("model_leg"),
                "error": (rec.get("agent") or {}).get("error"),
            })
            continue
        valid.append(rec)
    return {"valid": valid, "excluded": excluded, "leg_errors": leg_errors}


def ground_truth_verdict(recording: dict) -> dict:
    """`ground_truth_verdict(recording) = a3b.verdict(recording)` (lens-spec §5,
    literal) — returns the FULL a3b.verdict() envelope
    (`{"verdict_per_term": ..., "violation_mode_per_term": ...}`), not a
    bare per-term dict; callers that just need the per-term verdicts read
    `ground_truth_verdict(recording)["verdict_per_term"]`.
    """
    return a3b.verdict(recording)


def classify_term(arm_verdict: Optional[str], gt_verdict: Optional[str]) -> str:
    """One of "false_pass" | "false_fail" | "agreement" | "other_mismatch"."""
    if arm_verdict == gt_verdict:
        return "agreement"
    if arm_verdict == "Fulfilled" and gt_verdict == "Violated":
        return "false_pass"
    if arm_verdict == "Violated" and gt_verdict == "Fulfilled":
        return "false_fail"
    return "other_mismatch"


def compare_recording_arm(recording: dict, arm: str) -> dict:
    """`{term_id: "false_pass"|"false_fail"|"agreement"|"other_mismatch"}`
    for one recording under one arm, vs. the recording's own ground truth.
    """
    fn = ARM_VERDICT_FNS.get(arm)
    if fn is None:
        raise ValueError(f"compare_recording_arm: unknown arm {arm!r}")

    arm_verdict_per_term = fn(recording)
    gt_per_term = ground_truth_verdict(recording)["verdict_per_term"]
    terms = recording.get("contract", {}).get("terms") or []

    result = {}
    for term in terms:
        tid = term.get("term_id")
        result[tid] = classify_term(arm_verdict_per_term.get(tid), gt_per_term.get(tid))
    return result


def _cell_key(recording: dict, arm: str) -> tuple:
    return (arm, recording.get("leg_scaffold"), recording.get("scenario_id"))


def aggregate(recordings: list, arms=DEFAULT_ARMS) -> dict:
    """`recordings` -> `{"cells": [ {...one row per (arm, leg_scaffold,
    scenario_id)...} ]}`. Each row carries counts + rates + `arm_class` +
    `otel_correlated` (None/mixed-flagged if the cell isn't otel-uniform).
    """
    cells: dict = {}

    for recording in recordings:
        for arm in arms:
            key = _cell_key(recording, arm)
            cell = cells.setdefault(
                key,
                {
                    "arm": arm,
                    "leg_scaffold": recording.get("leg_scaffold"),
                    "scenario_id": recording.get("scenario_id"),
                    "arm_class": ARM_CLASS.get(arm),
                    "n_terms": 0,
                    "false_pass": 0,
                    "false_fail": 0,
                    "agreement": 0,
                    "other_mismatch": 0,
                    "not_determined": 0,
                    "_otel_correlated_values": set(),
                },
            )
            cell["_otel_correlated_values"].add((recording.get("otel", {}) or {}).get("correlated"))

            classifications = compare_recording_arm(recording, arm)
            # `not_determined` is a SEPARATE dimension from the four
            # classification buckets above — it overlaps them (an ND arm
            # verdict still lands in agreement/other_mismatch per
            # classify_term), it is not a 5th mutually-exclusive bucket. Needs
            # the arm's RAW per-term verdict (classify_term only returns the
            # comparison outcome), so call the arm fn directly here too.
            arm_verdict_per_term = ARM_VERDICT_FNS[arm](recording)
            for tid, cls in classifications.items():
                cell["n_terms"] += 1
                cell[cls] += 1
                if arm_verdict_per_term.get(tid) == "NotDetermined":
                    cell["not_determined"] += 1

    rows = []
    for cell in cells.values():
        n = cell["n_terms"]
        otel_values = cell.pop("_otel_correlated_values")
        cell["otel_correlated"] = next(iter(otel_values)) if len(otel_values) == 1 else None
        cell["otel_correlated_mixed"] = len(otel_values) > 1
        cell["false_pass_rate"] = (cell["false_pass"] / n) if n else None
        cell["false_fail_rate"] = (cell["false_fail"] / n) if n else None
        cell["agreement_rate"] = (cell["agreement"] / n) if n else None
        cell["other_mismatch_rate"] = (cell["other_mismatch"] / n) if n else None
        cell["not_determined_rate"] = (cell["not_determined"] / n) if n else None
        rows.append(cell)

    return {"cells": rows}


def violation_mode_distribution(recordings: list) -> dict:
    """Over all Violated terms (per A3b) across `recordings`: counts of
    `late_convergence` vs `never_converged` (lens-spec §5 — "where A3b's
    OWN quality is characterized", surface 1 of 3).
    """
    counts = {"late_convergence": 0, "never_converged": 0}
    for recording in recordings:
        result = a3b.verdict(recording)
        for tid, v in result["verdict_per_term"].items():
            if v != "Violated":
                continue
            mode = result["violation_mode_per_term"].get(tid)
            if mode in counts:
                counts[mode] += 1
    return counts


def _has_terminal_transfer_status(recording: dict, term_id: str) -> bool:
    """True if the verifier's INDEPENDENT `transfer_status_samples` recorded a
    TERMINAL transfer status for `term_id` — i.e. the transfer self-reported the
    job finished (any non-empty status value NOT in
    `a3b.NON_TERMINAL_TRANSFER_STATUSES` = {"scheduled","active"}, e.g.
    "completed"). Mirrors a3b's E-status-shortfall terminal check so the two
    stay definitionally in lockstep."""
    samples = (recording.get("state_timeline", {}) or {}).get("transfer_status_samples") or []
    return any(
        s.get("term_id") == term_id
        and isinstance(s.get("value"), str)
        and s.get("value")
        and s.get("value") not in a3b.NON_TERMINAL_TRANSFER_STATUSES
        for s in samples
    )


def status_vs_data_divergence(recordings: list) -> dict:
    """Separate a transfer that merely REPORTS a terminal status ("the job
    completed") from one that completed WITH DATA (bytes actually landed on the
    target). Keeps the OneData "completed-with-0-bytes" case — a service that
    reports success while transferring nothing — a FIRST-CLASS, distinctly
    counted category instead of lumping it into generic non-convergence.

    Motivation (operator request, 2026-07-06): when a transfer self-reports
    `completed` but zero bytes arrive, that is a platform (OneData) status lie,
    NOT the agent violating the SLA. Surfacing it distinctly lets the results
    choose, at analysis time, whether to treat such a cell as a service failure
    or merely note "the job self-reported done" — a choice we can only make
    because we recorded BOTH the reported status and the ground-truth bytes.
    This is the paper's own thesis turned on our substrate: status-signal
    (what A0/A1 trust) diverging from observed converged state (what A3b grounds
    compliance in).

    Uses the verifier's INDEPENDENT signals only: `transfer_status_samples` for
    the reported status, and a3b for the ground truth (`emit`'s E-status-shortfall
    corroboration class = terminal-status-observed AND bytes never reached
    expected). Per (recording, term), counts:
      - completed_with_data      : terminal status reported AND data present
                                   (true success; includes late-but-arrived)
      - completed_but_data_absent: terminal status reported BUT data absent
                                   (== a3b E-status-shortfall; the status-lied class)
      - no_terminal_status       : no terminal status ever observed (still
                                   scheduled/active, never dispatched, or no
                                   transfer scheduled at all)
    `terminal_status_reported` = completed_with_data + completed_but_data_absent
    is included as a convenience total ("the job completed, at least").
    """
    counts = {
        "completed_with_data": 0,
        "completed_but_data_absent": 0,
        "no_terminal_status": 0,
        "terminal_status_reported": 0,
    }
    for recording in recordings:
        verdict_per_term = a3b.verdict(recording)["verdict_per_term"]
        emit_result = a3b.emit(recording)
        corroboration = emit_result.get("corroboration_per_term", {}) or {}
        for tid in verdict_per_term:
            if not _has_terminal_transfer_status(recording, tid):
                counts["no_terminal_status"] += 1
                continue
            counts["terminal_status_reported"] += 1
            if a3b.E_STATUS_SHORTFALL in (corroboration.get(tid) or []):
                counts["completed_but_data_absent"] += 1
            else:
                counts["completed_with_data"] += 1
    return counts


def not_determined_rate(recordings: list, arm: str = "A3b") -> Optional[float]:
    """Fraction of (recording, term) pairs where `arm`'s verdict is
    NotDetermined (lens-spec §5 — surface 3 of 3, "how often the state
    genuinely couldn't be established"). Defaults to A3b per the spec's own
    framing, but works for any arm in `ARM_VERDICT_FNS`.
    """
    fn = ARM_VERDICT_FNS.get(arm)
    if fn is None:
        raise ValueError(f"not_determined_rate: unknown arm {arm!r}")

    total = 0
    not_determined = 0
    for recording in recordings:
        verdict_per_term = fn(recording)
        terms = recording.get("contract", {}).get("terms") or []
        for term in terms:
            tid = term.get("term_id")
            total += 1
            if verdict_per_term.get(tid) == "NotDetermined":
                not_determined += 1
    return (not_determined / total) if total else None
