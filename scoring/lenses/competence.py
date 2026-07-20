"""Agent competence (A-axis) — docs/phase1-lens-spec.md §2.

`agent_competence` is a property of the RECORDING (the trace), not of any
one lens — computed once, shared by every lens (A0/A1/A3a/A3b all read the
same value; A3b's evidential emitter additionally uses it as its
`agent-evidence` boolean per §4).

**Scope of this increment (per the increment-3 build task): E1 Form B
only.** §2's Required-action set gives the EXACT pinned instantiation for
E1 Form B as just two checks:

    (a) schedule_file_replication(file, target) where
        target == expected.per_term[tid].target_providers[0], and
        file   == the contracted source file; issued at some ts_rel_s.
    (b) reported_done_at_rel_s >= that call's ts_rel_s.
    A=pass iff (a) present with correct target/file AND (b); else fail.

The spec's general 3-point definition (source-artifact-exists / correct
action / no-premature-report) is instantiated by the E1-Form-B paragraph
as exactly (a)+(b) — condition (1) "source artifact exists" is guaranteed
by the harness's own fixture-write-before-agent-run protocol (fixtures.py
writes the file before `leg.run()` ever starts, per runner.py's
`run_trial`) and is therefore NOT independently re-checked here from the
recording (there is no recorded "source exists" boolean to check against;
re-deriving it would require re-contacting the federation, which a pure
offline lens must not do).

Form A (`add_file_qos_requirement` substituting for (a), per the spec's
parenthetical) is NOT implemented here: the increment-3 build task scopes
this module to the E1 Form B check explicitly, no Form-A recording exists
yet to validate against (Form A is still "staged", not live — see
docs/phase1-scenario-contracts.md), and the schema doesn't even persist
which form a trial used. Extending this function when Form A activates is
future work, flagged inline below.

Stdlib-only. No top-level side effects on import.
"""
from __future__ import annotations

from typing import Any, Optional

REQUIRED_ACTION_NAME = "schedule_file_replication"
TARGET_ARG_KEY = "target_provider_id"
FILE_ARG_KEY = "file_id_or_path"


def _first_term_id(recording: dict) -> Optional[str]:
    terms = recording.get("contract", {}).get("terms") or []
    if not terms:
        return None
    return terms[0].get("term_id")


def agent_competence(recording: dict) -> str:
    """`"pass"` iff the E1-Form-B required-action set (§2) holds, else `"fail"`.

    Reads ONLY the trace (`agent.tool_calls[]`, `agent.reported_done_at_rel_s`)
    plus the term's `expected.per_term[tid]` entry — never `state_timeline`
    (competence is an A-axis-only, outcome-independent judgment) and never
    `fault.*` (irrelevant to whether the agent acted correctly).

    Generalizes "term T1" to "the recording's first (only) contract term"
    rather than hardcoding the literal string "T1" — E1 Form B always has
    exactly one term, so this is equivalent to the spec's literal wording
    but doesn't silently mis-key on a differently-named single term.

    NOTE (resolved spec-vs-real-data ambiguity, cited in full in the PR/branch
    report): the two real increment-2b recordings record
    `expected.per_term[tid].target_providers` as a HUMAN LABEL (e.g. "de")
    while `agent.tool_calls[].args.target_provider_id` and
    `state_timeline.samples[].provider` use the provider's raw ID hash. This
    function performs an EXACT string-equality check per the spec's literal
    "target == target_providers[0]" wording — on those two real recordings
    this makes condition (a) unsatisfiable (label != hash) even though the
    agent targeted the right provider. This is a data-hygiene gap in the
    CURRENT recording pipeline (label vs. ID namespace), not a lens bug; see
    the module-level note in `a3b.py` (shared helper `converged_sample`) for
    the same caveat, and the branch report for the recommended follow-up
    (normalize `target_providers` to the same ID namespace `state_timeline`
    already uses, parallel to the pending `provider_health` schema revision
    from lens-spec §1a).
    """
    agent = recording.get("agent", {}) or {}
    tool_calls = agent.get("tool_calls") or []
    reported_done_at = agent.get("reported_done_at_rel_s")

    term_id = _first_term_id(recording)
    if term_id is None:
        return "fail"

    per_term = (recording.get("expected", {}) or {}).get("per_term", {}) or {}
    entry = per_term.get(term_id, {}) or {}
    target_providers = entry.get("target_providers") or []
    expected_paths = entry.get("expected_paths") or []

    if not target_providers:
        # Can't confirm "targeting exactly the term's target_providers[0]"
        # without a recorded target — conservatively fail rather than guess.
        return "fail"
    required_target = target_providers[0]

    def _matches_file(args: dict) -> bool:
        if not expected_paths:
            # No path constraint recorded for this term — don't manufacture
            # a failure the spec didn't ask for.
            return True
        return args.get(FILE_ARG_KEY) in expected_paths

    qualifying_calls = []
    for tc in tool_calls:
        if tc.get("name") != REQUIRED_ACTION_NAME:
            continue
        args = tc.get("args") or {}
        if args.get(TARGET_ARG_KEY) != required_target:
            continue
        if not _matches_file(args):
            continue
        qualifying_calls.append(tc)

    if not qualifying_calls:
        return "fail"

    # (b) no premature self-report, judged against the EARLIEST qualifying
    # (correctly-targeted, correctly-filed) action — "the required action"
    # in the spec is singular; using the earliest correct attempt is the
    # most conservative (hardest-to-pass) reading.
    earliest_call = min(qualifying_calls, key=lambda tc: tc.get("ts_rel_s", float("inf")))

    if reported_done_at is None:
        # schema.py currently requires this field to be numeric on any
        # schema-VALID recording (see test_lenses.py's note on the
        # null-report edge case) — defensively handled here anyway since a
        # lens must not crash on a malformed/relaxed input. Can't
        # affirmatively confirm "no premature report" without a report at
        # all, so this is the conservative "fail" (never a NotDetermined —
        # competence is a strict two-valued judgment, unlike verdicts).
        return "fail"

    if reported_done_at < earliest_call.get("ts_rel_s", float("inf")):
        return "fail"

    return "pass"
