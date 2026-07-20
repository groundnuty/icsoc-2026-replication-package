"""RQ3 — A4 enforcement-arm attainment/cost/safety metrics
(docs/phase1-a4-enforcement.md §5).

One public entrypoint:

    rq3_metrics(recordings) -> {
        "per_leg": {model_leg: {...metrics...}},
        "overall": {...metrics...},
    }

Reads `a4_result` from recordings (RECORDED, never recomputed — same
discipline as `aggregate.a2_recorded_verdict` reading the persisted
`a2_result`) plus an EPISODE-END final-state truth check for the
post-gate-violation validity check (§6.5 analog: A4's own gate-loop
shouldn't accept a claim the FULL recorded timeline disagrees with at
episode end). Only recordings carrying an `a4_result` are considered —
recordings without one are SKIPPED, never synthesized a value for.

**Post-gate-violation check is FINAL-STATE, not `a3b.verdict()`-rollup
(science #53 review, 2026-07-06 fix).** The original check re-ran
`a3b.verdict()` over the FULL merged `state_timeline` under the
recording's ORIGINAL trial-start anchor and flagged a disagreement with
`Violated`. That FALSE-FLAGS a trial the gate legitimately accepted via a
remedied cycle's per-cycle deadline window (`harness/a4_gate.py`'s
re-anchor/extend mechanism, same fix) — a4_gate's own scoring for a
retried cycle checks convergence against an EXTENDED `effective_t_max`,
which the recording's ORIGINAL, un-extended `contract.terms[].t_max_s`
does not reflect, so re-running `a3b.verdict()` at the ORIGINAL anchor
would flag a legitimately-accepted late convergence as a "violation" that
isn't one. The check here instead asks the anchor-independent question
the safety property (§1) actually cares about: at EPISODE END, does the
recorded state agree with the "Fulfilled" claim? See
`_final_state_violation` below.

Mirrors `rq2.py`'s structure: pure, offline, per-leg + overall grouping.

Stdlib-only. No top-level side effects on import.
"""
from __future__ import annotations

from .lenses import a3b

COST_BUCKET_KEYS = ("verifier_probes", "agent_invocations", "gate_retries")


def _final_state_violation(recording: dict) -> bool:
    """True iff the recorded FINAL state at episode end disagrees with a
    reported `a4_result.final_verdict == "Fulfilled"` claim — the §1
    post-gate-violation safety check, re-framed ANCHOR-INDEPENDENTLY
    (science #53 review, 2026-07-06): rather than re-running
    `a3b.verdict()` over the FULL merged `state_timeline` under the
    recording's ORIGINAL trial-start anchor (which false-flags a
    legitimately-accepted LATE convergence a remedied gate cycle
    re-anchored/extended its own verify window to accept — see
    `harness/a4_gate.py`'s per-cycle deadline-window mechanism), this
    checks the EPISODE-END ground truth directly: for each contract term,
    find the CHRONOLOGICALLY-LAST `probe_ok` `physicalSize` sample recorded on
    the term's target provider(s) in the merged timeline — the last in ARRAY
    (append) order, which is the true chronology; NOT `max(t_rel_s)`, since
    `t_rel_s` resets per re-anchored gate cycle and is non-monotonic across the
    merge — and compare it to `expected_size`. A term whose last observed value
    != `expected_size` (or which was never successfully probed at all) is a
    violation.

    POSITIVE-PROBE ONLY: this checks the placement/convergence guarantee
    (target `physicalSize` == `expected_size`) that E1 Form B — the only
    scenario A4 currently runs against — carries. It does NOT check a
    NEGATIVE/residue-style guarantee (E3's "no provider retains blocks"
    SLI, docs/phase1-scenario-contracts.md §E3) — E1 Form B has no such
    term, so this is a COMPLETE final-state check for A4's current scope.
    A future A4 run over a negative-probe scenario (E3-style) would need
    this check extended to ALSO assert absence/residue at episode end for
    terms that carry that SLI.
    """
    terms = recording.get("contract", {}).get("terms") or []
    per_term = (recording.get("expected", {}) or {}).get("per_term", {}) or {}
    samples = (recording.get("state_timeline", {}) or {}).get("samples") or []

    for term in terms:
        tid = term.get("term_id")
        entry = per_term.get(tid, {}) or {}
        target_providers = set(entry.get("target_providers") or [])
        expected_size = entry.get("expected_size")

        candidates = [
            s
            for s in samples
            if s.get("term_id") == tid
            and s.get("sli_name") == a3b.PHYSICAL_SIZE_SLI
            and s.get("provider") in target_providers
            and s.get("probe_ok") is True
        ]
        if not candidates:
            return True  # never successfully probed -- can't confirm the claim
        # CHRONOLOGICAL last, i.e. array order -- NOT max(t_rel_s). The merged
        # `state_timeline.samples` are appended cycle-by-cycle, so their array
        # order is the true episode chronology, but `t_rel_s` RESETS to 0 at
        # every re-anchored gate cycle (a4_gate per-cycle deadline window), so
        # it is NON-MONOTONIC across the merge. `max(t_rel_s)` therefore picks
        # whichever cycle ran the longest window -- typically an EARLIER
        # REJECTED cycle whose high-t_rel_s tail is still 0 bytes -- and
        # false-flags a legitimately-accepted late-convergence trial (verified
        # 2026-07-06 on a4-control-v3: cycle-1 tail t=47.7/val=0 vs the accepted
        # cycle-3 final t=47.3/val=4096). The accepted cycle's samples are
        # appended LAST, so `candidates[-1]` is the correct episode-end value.
        last = candidates[-1]
        if last.get("value") != expected_size:
            return True

    return False


def _final_state_violation_e3(recording: dict) -> bool:
    """The E3 (negative-probe / deletion) analogue of `_final_state_violation`
    (docs/_e3_a4_build_spec.md Deliverable 4) — the same episode-end,
    ANCHOR-INDEPENDENT safety check applied to E3's residue-removal
    guarantee instead of E1 Form B's placement guarantee.

    For each contract term, take the CHRONOLOGICALLY-LAST (append/array
    order — NEVER `max(t_rel_s)`, for the identical reason
    `_final_state_violation` documents: `t_rel_s` resets per re-anchored
    gate cycle, so array order is the true episode chronology while
    `t_rel_s` is non-monotonic across a merged multi-cycle timeline)
    `probe_ok` entry in `state_timeline.residue_samples` (E3's residue
    stream is NOT per-term-tagged — see `harness.lenses.e3`'s own
    `_residue_samples`/`a3b_verdict`, which read the same single stream for
    every term — so this uses the one stream directly, mirroring that
    precedent).

    A term is a violation iff, at that last `probe_ok` sample:
      - `total_residue > 0` (residue remains on some provider), OR
      - `path_absent is False` (the path itself still resolves),
    OR if NO `probe_ok` residue sample exists at all (removal was never
    successfully confirmed by an independent probe).

    NEGATIVE-PROBE ONLY: this checks E3's residue-removal guarantee; it
    does not (and need not) check E1 Form B's positive placement guarantee
    — `_final_state_violation` above is unchanged and remains the check for
    that scenario. `rq3._leg_metrics` dispatches on `recording["scenario_id"]`
    to pick the right check per recording.
    """
    terms = recording.get("contract", {}).get("terms") or []
    residue_samples = (recording.get("state_timeline", {}) or {}).get("residue_samples") or []
    probe_ok_samples = [s for s in residue_samples if s.get("probe_ok") is True]

    per_term = (recording.get("expected", {}) or {}).get("per_term", {}) or {}
    for term in terms:
        if not probe_ok_samples:
            return True  # never successfully confirmed removed
        # CHRONOLOGICAL last, i.e. array order -- NOT max(t_rel_s). See
        # `_final_state_violation`'s docstring for the full rationale (a
        # merged multi-cycle timeline's t_rel_s resets per re-anchored
        # cycle, so array order is the only reliable episode chronology).
        last = probe_ok_samples[-1]
        total_residue = last.get("total_residue") or 0
        path_absent = last.get("path_absent")
        if total_residue > 0 or path_absent is False:
            return True
        # Rider 1 (science #76 review, 2026-07-15) — fail-loud on
        # NEVER-PROBED coverage, symmetric to the placement check's
        # per-provider treatment: a Fulfilled claim must be backed by an
        # independent probe that (a) actually resolved the path
        # (`path_absent` is a real bool, not None) AND (b) covered EVERY
        # contract provider. A last probe_ok sample whose `path_absent` is
        # None (path never successfully resolved) or whose `per_provider`
        # map is missing any contract provider (that provider was never
        # covered by a successful residue probe) is a pgv-class failure, not
        # a silent pass. In the live path `probe_residue` always populates
        # both when probe_ok is True, so this is defense-in-depth against a
        # malformed/partial recording rather than an expected live shape.
        if path_absent is None:
            return True
        entry = per_term.get(term.get("term_id"), {}) or {}
        contract_providers = set(entry.get("target_providers") or [])
        covered = set((last.get("per_provider") or {}).keys())
        if contract_providers and not contract_providers.issubset(covered):
            return True

    return False


def _leg_metrics(recordings: list) -> dict:
    """The full RQ3 metrics block (docs/phase1-a4-enforcement.md §5) over
    ONE group of recordings (already filtered to those carrying `a4_result`).
    """
    n_trials = len(recordings)
    final_fulfilled = 0
    first_attempt_fulfilled = 0
    attempts_to_success: list = []
    terminated = 0
    post_gate_violations = 0
    total_cost_buckets = {k: 0 for k in COST_BUCKET_KEYS}
    total_cost_all_trials = 0

    for recording in recordings:
        a4 = recording["a4_result"]
        cost = a4.get("cost_per_bucket") or {}
        trial_cost = sum(int(cost.get(k, 0) or 0) for k in COST_BUCKET_KEYS)
        total_cost_all_trials += trial_cost
        for k in COST_BUCKET_KEYS:
            total_cost_buckets[k] += int(cost.get(k, 0) or 0)

        if a4.get("final_verdict") == "Fulfilled":
            final_fulfilled += 1
            attempts_to_success.append(a4.get("gate_cycles"))
            # §6.5-analog validity check: a "Fulfilled" A4 claim must never
            # disagree with the recorded FINAL state at episode end (see
            # `_final_state_violation`'s docstring for why this is
            # anchor-independent, not `a3b.verdict()`-rollup) -- a gate bug,
            # ~=0 by construction. Dispatch on scenario_id
            # (docs/_e3_a4_build_spec.md Deliverable 4): E3's residue-removal
            # guarantee needs its own final-state check
            # (`_final_state_violation_e3`); every other (E1 Form B /
            # placement) scenario is UNCHANGED.
            if recording.get("scenario_id") == "E3":
                violation = _final_state_violation_e3(recording)
            else:
                violation = _final_state_violation(recording)
            if violation:
                post_gate_violations += 1

        if a4.get("first_attempt_verdict") == "Fulfilled":
            first_attempt_fulfilled += 1

        if a4.get("terminated") is True:
            terminated += 1

    attainment_uplift = (
        (final_fulfilled - first_attempt_fulfilled) / n_trials if n_trials else None
    )
    termination_rate = (terminated / n_trials) if n_trials else None
    cost_per_verified_success = (
        (total_cost_all_trials / final_fulfilled) if final_fulfilled else None
    )
    post_gate_violation_rate = (
        (post_gate_violations / final_fulfilled) if final_fulfilled else None
    )

    return {
        "n_trials": n_trials,
        "final_fulfilled": final_fulfilled,
        "first_attempt_fulfilled": first_attempt_fulfilled,
        "attainment_uplift": attainment_uplift,
        "attempts_to_success": sorted(attempts_to_success),
        "terminated": terminated,
        "termination_rate": termination_rate,
        "total_cost_buckets": total_cost_buckets,
        "cost_per_verified_success": cost_per_verified_success,
        "post_gate_violations": post_gate_violations,
        "post_gate_violation_rate": post_gate_violation_rate,
    }


def rq3_metrics(recordings: list) -> dict:
    """`recordings` -> `{"per_leg": {model_leg: {...}}, "overall": {...}}`.

    Only recordings carrying an `a4_result` contribute (skipped, not
    synthesized, otherwise -- same discipline as
    `aggregate.a2_recorded_verdict`). Grouped by `recording["model_leg"]`.
    """
    scored = [r for r in recordings if r.get("a4_result")]

    by_leg: dict = {}
    for recording in scored:
        by_leg.setdefault(recording.get("model_leg"), []).append(recording)

    per_leg = {leg: _leg_metrics(recs) for leg, recs in by_leg.items()}
    overall = _leg_metrics(scored)

    return {"per_leg": per_leg, "overall": overall}
