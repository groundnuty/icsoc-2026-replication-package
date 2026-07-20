"""A3a — contract-aware, convergence-naive (contribution). docs/phase1-lens-spec.md §3 A3a.

Reads the actual target `physicalSize` but ONLY at the report instant:

    s* = the sample with the smallest t_rel_s >= reported_done_at_rel_s
         that is probe_ok on a target provider for the term.
    Fulfilled     iff s* exists and s*.value == expected_size.
    Violated      iff s* exists and s*.value != expected_size
                      (spec states "< expected_size"; physicalSize is
                      monotonically non-decreasing under the fixture
                      discipline so a real recording never exceeds
                      expected_size — any non-equal value is treated as
                      not-yet-converged, i.e. Violated, which is exhaustive
                      given the spec's own two-outcome partition once s*
                      exists).
    NotDetermined iff no probe_ok sample at/after the report — INCLUDING
                      the null-report path (reported_done_at_rel_s is
                      null -> no report instant exists -> NotDetermined).

Catches "reported before writing" but false-fails late-convergence (an
agent that reports at an instant not-yet-converged, which WOULD converge
before T_max) — the A3a-false-fail that A3b (§3 A3b) fixes.

Stdlib-only. No top-level side effects on import.
"""
from __future__ import annotations

PHYSICAL_SIZE_SLI = "physicalSize"


def verdict(recording: dict) -> dict:
    """Returns a bare `{term_id: "Fulfilled"|"Violated"|"NotDetermined"}` dict."""
    agent = recording.get("agent", {}) or {}
    reported_done_at = agent.get("reported_done_at_rel_s")
    samples = (recording.get("state_timeline", {}) or {}).get("samples") or []
    per_term = (recording.get("expected", {}) or {}).get("per_term", {}) or {}
    terms = recording.get("contract", {}).get("terms") or []

    result: dict = {}
    for term in terms:
        tid = term.get("term_id")

        if reported_done_at is None:
            result[tid] = "NotDetermined"
            continue

        entry = per_term.get(tid, {}) or {}
        target_providers = set(entry.get("target_providers") or [])
        expected_size = entry.get("expected_size")

        candidates = [
            s
            for s in samples
            if s.get("term_id") == tid
            and s.get("sli_name") == PHYSICAL_SIZE_SLI
            and s.get("probe_ok") is True
            and s.get("provider") in target_providers
            and s.get("t_rel_s", -1) >= reported_done_at
        ]

        if not candidates:
            result[tid] = "NotDetermined"
            continue

        s_star = min(candidates, key=lambda s: s["t_rel_s"])
        if expected_size is not None and s_star.get("value") == expected_size:
            result[tid] = "Fulfilled"
        else:
            result[tid] = "Violated"

    return result
