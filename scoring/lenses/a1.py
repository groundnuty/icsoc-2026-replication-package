"""A1 — classical intent-agnostic SLA monitoring (prior practice).
docs/phase1-lens-spec.md §3 A1 (+ §1a for the `provider_health` stream;
+ the 2026-07-06 dated variant appended there).

Verdict from generic liveness ONLY — call-returns + component health —
NEVER contract-state-vs-expected (`physicalSize` never enters this lens).

refined PREDICATE (default, `vacuous_pass=True`) — two-valued
"green-unless-alarm", per the 2026-07-06 science ruling (issue #3):

    Violated  iff  a required call errored
                   OR any provider_health sample went false in-window.
    Fulfilled otherwise — INCLUDING vacuously when no required call is
                   present at all, and when calls are ok with no
                   health-false alarm anywhere. A1 NEVER returns
                   NotDetermined under this mode: classical monitoring is
                   two-valued (a dashboard is green or it alarms — there
                   is no operator-facing abstention state), and a green
                   dashboard over a system where nothing ran IS the
                   operator's "success" read — the intent-agnostic
                   failure mode this paper characterizes, not a case A1
                   should sidestep via ND.

baseline PREDICATE (retained, `vacuous_pass=False`) — exactly the
original three-valued §3 predicate, kept computable so the results doc
can DUAL-TALLY A1-false-pass under both predicates (the variant landed
after seeing data, so the baseline number must stay reportable
alongside the refined one):

    Fulfilled iff  the required placement call(s) returned success
                   (tool_calls[].ok == true)
                   AND every provider_health sample for the term's
                   involved (target) providers within [0, t_max] is true.
    Violated  iff  a required call errored
                   OR any provider_health sample went false in-window.
    NotDetermined iff no relevant call is present (also returned when
                   calls are ok but the health stream is present-and-
                   inconclusive for this term's target providers).

On recordings that predate the `provider_health` SLI (lens-spec §1a — the
two increment-2b smoke recordings both predate it), A1 degrades to
call-returns-only and records `a1_health_available: false` so the RQ1
table never silently treats a health-blind A1 as the fair baseline. This
degradation applies identically under both predicates.

Stdlib-only. No top-level side effects on import.
"""
from __future__ import annotations

REQUIRED_ACTION_NAME = "schedule_file_replication"
HEALTH_SLI_NAME = "provider_health"


def _health_available(samples: list) -> bool:
    return any(s.get("sli_name") == HEALTH_SLI_NAME for s in samples)


def verdict(recording: dict, *, vacuous_pass: bool = True) -> dict:
    """Returns `{term_id: "Fulfilled"|"Violated"|"NotDetermined", ...,
    "a1_health_available": bool}` — one meta key alongside the per-term
    verdicts (§1a requires it be recorded "in the result"; a term_id can
    never collide with this literal marker string).

    `vacuous_pass=True` (default) — refined two-valued predicate: NEVER
    returns NotDetermined (see module docstring).
    `vacuous_pass=False` — baseline three-valued predicate, exactly
    as originally spec'd; kept computable for the required dual-tally.
    """
    agent = recording.get("agent", {}) or {}
    tool_calls = agent.get("tool_calls") or []
    samples = (recording.get("state_timeline", {}) or {}).get("samples") or []
    per_term = (recording.get("expected", {}) or {}).get("per_term", {}) or {}
    terms = recording.get("contract", {}).get("terms") or []

    health_available = _health_available(samples)

    result: dict = {}
    for term in terms:
        tid = term.get("term_id")
        t_max = term.get("t_max_s")
        target_providers = set((per_term.get(tid, {}) or {}).get("target_providers") or [])

        required_calls = [tc for tc in tool_calls if tc.get("name") == REQUIRED_ACTION_NAME]
        if not required_calls:
            # refined: vacuous green — nothing ran, dashboard stays green,
            # never ND. baseline: unchanged three-valued ND.
            result[tid] = "Fulfilled" if vacuous_pass else "NotDetermined"
            continue

        any_call_errored = any(not tc.get("ok", False) for tc in required_calls)
        all_calls_ok = all(tc.get("ok", False) for tc in required_calls)

        if health_available:
            health_in_window = [
                s
                for s in samples
                if s.get("sli_name") == HEALTH_SLI_NAME
                and s.get("term_id") == tid
                and s.get("provider") in target_providers
                and t_max is not None
                and 0 <= s.get("t_rel_s", -1) <= t_max
            ]
            any_health_false = any(s.get("value") is False for s in health_in_window)
            all_health_true = bool(health_in_window) and all(
                s.get("value") is True for s in health_in_window
            )
        else:
            # Degrade to call-returns-only (§1a) — vacuously "healthy".
            any_health_false = False
            all_health_true = True

        if any_call_errored or any_health_false:
            result[tid] = "Violated"
        elif vacuous_pass:
            # refined: green-unless-alarm. `all_calls_ok` is guaranteed
            # true here (any_call_errored is False) — no health-false
            # alarm anywhere (whether from zero in-window samples or all-
            # true samples) reads Fulfilled; absence of an alarm IS the
            # green read, never NotDetermined.
            result[tid] = "Fulfilled"
        elif all_calls_ok and all_health_true:
            result[tid] = "Fulfilled"
        else:
            # baseline only: calls ok but health stream present-and-
            # inconclusive for this term (e.g. no in-window samples for
            # its target providers) — conservative fallback rather than
            # manufacturing a verdict.
            result[tid] = "NotDetermined"

    result["a1_health_available"] = health_available
    return result
