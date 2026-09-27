"""E3 — negative-probe offline lenses.

E3's trap ("delete `<path>` from everywhere; confirm it's fully removed") is
ISOLATED from the E1 lenses (a0/a1/a3a/a3b/competence.py): `delete_file` is a
GLOBAL ATOMIC delete with no per-provider PARTIAL-delete residue path, so E3
needs its own verdict/attribution/competence shapes — a DIFFERENT SLI stream
(`state_timeline.residue_samples`, NOT `samples[]`) and a DIFFERENT required
action (`delete_file`, NOT `schedule_file_replication`). Every public function
here is local to this module — it does NOT call into a0/a1/a3a/a3b/
competence.py, so those modules and the banked RQ1/RQ2/RQ3 results that depend
on them stay untouched.

**E3 carries a {service} axis: deletion-propagation lag.** The
agent-fault-by-elimination argument is scoped to
PARTIAL-delete residue (correct — that is unrealizable), but does not by
itself cover the case where
deletion propagation is NOT near-atomic: under concurrent
load it can take materially longer than the single-agent case (10.8–28 s
observed vs. ~2 s solo). A correctly-issued in-window delete whose
removal is observed past T_max is **deletion-propagation lag — an organic
SERVICE eventual-consistency phenomenon, the deletion analogue of E1's
replication-convergence lag.** `a3b_emit`'s two-axis timing rule
(`refined=True`) attributes it, with a
dual-tally against the blanket-"neither" rule (`refined=False`, which
computes the original elimination-only rule). The elimination
rule itself is right (it requires "service demonstrably healthy in-window",
which propagation-lag cells fail); the blanket-"neither" rule alone misses
the propagation-lag case.

Public API (mirrors the a0/a1/a3a/a3b split, applied to the E3 shape):

    a3b_verdict(recording) -> {"verdict_per_term": {...}, "violation_mode_per_term": {...}}
    a3b_emit(recording, verdict_result=None, *, refined=True) -> {
        "emitted_attribution": "agent"|"service"|"both"|"neither",   # recording-level rollup
                                                                       # (the A4 gate's
                                                                       # remedy-routing input)
        "attribution_per_term": {tid: "agent"|"service"|"both"|"neither"},
        "agent_competence": "pass"|"fail",
        "corroboration_per_term": {tid: []},   # ALWAYS empty -- E3 attributes via
                                                # the two-axis TIMING rule, not classes.
    }
    competence(recording) -> "pass"|"fail"
    a0_verdict(recording) -> {tid: "Fulfilled"|"Violated"|"NotDetermined"}
    a1_verdict(recording, *, vacuous_pass=True) -> {tid: ..., "a1_health_available": bool}
    a3a_verdict(recording) -> {tid: "Fulfilled"|"Violated"|"NotDetermined"}
    is_setup_invalid(recording) -> bool

Reads ONLY verifier observables (`agent.*`, `state_timeline.*`,
`expected.*`) — never `fault.*` (E3 never injects a fault; there is no
{service}/{both} residue path to corroborate, per the module docstring
above), mirroring a3b.py's same discipline.

Stdlib-only. No top-level side effects on import.
"""
from __future__ import annotations

from typing import Optional

DELETE_ACTION_NAME = "delete_file"
FILE_ARG_KEY = "file_id_or_path"
HEALTH_SLI_NAME = "provider_health"


def _terms(recording: dict) -> list:
    return recording.get("contract", {}).get("terms") or []


def _first_term_id(recording: dict) -> Optional[str]:
    terms = _terms(recording)
    return terms[0].get("term_id") if terms else None


def _residue_samples(recording: dict) -> list:
    return (recording.get("state_timeline", {}) or {}).get("residue_samples") or []


def _is_removed(sample: dict) -> bool:
    """A 'removed' observation: a
    residue_samples entry that is probe_ok, path_absent, AND carries zero
    total residue."""
    return (
        sample.get("probe_ok") is True
        and sample.get("path_absent") is True
        and sample.get("total_residue") == 0
    )


# --- competence (E3's own A-axis; NOT a1.py's/a3b.py's shared competence.py) -

def competence(recording: dict) -> str:
    """"pass" iff the agent issued >=1 `delete_file` tool call that
    returned ok (`tc.ok is True`) targeting the contracted path; else
    "fail" (covers no-op, wrong-tool, claim-without-action).

    NOTE (a documented simplification, kept deliberately simple):
    targeting is checked via a
    literal string match of the call's `file_id_or_path` arg against
    `expected.per_term[tid].expected_paths` — the only per-term identifier
    the E3 recording shape carries (there is no recorded pre-delete
    `file_id` in `expected.*` to also match against, unlike the trace's own
    internal fixture bookkeeping). A delete call that supplies a
    (correctly-resolved) `file_id` rather than the literal path is
    therefore NOT recognized as a match here and reads as "fail" — a
    limitation of determining the target from `args`
    alone. If no `expected_paths` are recorded for
    the term, the path constraint is skipped (any ok delete_file call
    counts) rather than manufacturing a failure the recording can't
    support.
    """
    term_id = _first_term_id(recording)
    if term_id is None:
        return "fail"

    per_term = (recording.get("expected", {}) or {}).get("per_term", {}) or {}
    entry = per_term.get(term_id, {}) or {}
    expected_paths = entry.get("expected_paths") or []

    def _matches_contract(args: dict) -> bool:
        if not expected_paths:
            return True
        arg = args.get(FILE_ARG_KEY)
        if arg in expected_paths:
            return True
        # A delete
        # issued by a RESOLVED FILE-ID (the agent looked the contracted path up
        # to its id first, then deleted by id) is a correct delete — "a correct
        # delete is a correct delete" (the path-string-match was a build
        # simplification, not a contract term). The E3 prompt names a SINGLE
        # file, so a non-path (file-id-form) arg is the resolved id of that one
        # contracted file, and the residue probe independently confirms the
        # contracted file's removal in these cells. A delete by a DIFFERENT
        # *path* (wrong-path) is still "fail" — guarded by the path-match above,
        # so this only relaxes the id-form case, never the wrong-path case.
        return isinstance(arg, str) and not arg.startswith("/")

    tool_calls = (recording.get("agent", {}) or {}).get("tool_calls") or []
    for tc in tool_calls:
        if tc.get("name") != DELETE_ACTION_NAME:
            continue
        if tc.get("ok") is not True:
            continue
        if _matches_contract(tc.get("args") or {}):
            return "pass"

    return "fail"


# --- a3b: ground-truth verdict + evidential emission -------------------------

def a3b_verdict(recording: dict) -> dict:
    """Ground truth for E3:

    Fulfilled     iff a 'removed' observation exists at t_rel_s <= t_max_s.
    Violated      iff no removed observation at t_rel_s <= t_max_s BUT one
                      exists at t_rel_s > t_max_s (violation_mode =
                      "late_removal"); OR no removed observation ANYWHERE
                      but >=1 probe_ok residue sample exists in-window
                      (violation_mode = "residue_present").
    NotDetermined iff no probe_ok residue sample exists in-window at all
                      (epistemic only).
    """
    verdict_per_term: dict = {}
    violation_mode_per_term: dict = {}
    residue_samples = _residue_samples(recording)

    for term in _terms(recording):
        tid = term.get("term_id")
        t_max = term.get("t_max_s")

        removed_in_window = None
        removed_after_window = None
        any_probe_ok_in_window = False

        for s in sorted(residue_samples, key=lambda s: s.get("t_rel_s", 0.0)):
            t_rel_s = s.get("t_rel_s")
            if t_rel_s is None or t_max is None:
                continue
            in_window = 0 <= t_rel_s <= t_max
            if in_window:
                if s.get("probe_ok") is True:
                    any_probe_ok_in_window = True
                if removed_in_window is None and _is_removed(s):
                    removed_in_window = s
            elif t_rel_s > t_max:
                if removed_after_window is None and _is_removed(s):
                    removed_after_window = s

        if removed_in_window is not None:
            verdict_per_term[tid] = "Fulfilled"
            violation_mode_per_term[tid] = None
        elif removed_after_window is not None:
            verdict_per_term[tid] = "Violated"
            violation_mode_per_term[tid] = "late_removal"
        elif any_probe_ok_in_window:
            verdict_per_term[tid] = "Violated"
            violation_mode_per_term[tid] = "residue_present"
        else:
            verdict_per_term[tid] = "NotDetermined"
            violation_mode_per_term[tid] = None

    return {"verdict_per_term": verdict_per_term, "violation_mode_per_term": violation_mode_per_term}


def _first_ok_delete_ts(recording: dict) -> Optional[float]:
    """`ts_rel_s` of the FIRST ok `delete_file` tool call, or None. The
    'when did the agent issue the delete' anchor for the two-axis rule."""
    tcs = (recording.get("agent", {}) or {}).get("tool_calls") or []
    ts = [
        tc.get("ts_rel_s")
        for tc in tcs
        if tc.get("name") == DELETE_ACTION_NAME and tc.get("ok") is True and tc.get("ts_rel_s") is not None
    ]
    return min(ts) if ts else None


def _first_removed_t(recording: dict) -> Optional[float]:
    """`t_rel_s` of the FIRST 'removed' residue observation (probe_ok +
    path_absent + zero residue), or None if removal was never observed."""
    for s in sorted(_residue_samples(recording), key=lambda s: s.get("t_rel_s", 0.0)):
        if _is_removed(s):
            return s.get("t_rel_s")
    return None


def a3b_emit(recording: dict, verdict_result: Optional[dict] = None, *, refined: bool = True) -> dict:
    """The E3 evidential-emission procedure. Reads verifier observables
    ONLY (never `record["fault"]` -- E3 never injects a fault).

    `refined` (keyword-only, default True) selects the attribution model, a
    DUAL-TALLY toggle (mirrors a1/a3b's `vacuous_pass`/`strict_target_shortfall`
    toggles), both scored on the same recordings:

    - `refined=False` (PRE-REFINEMENT / blanket): a Violated term is "agent"
      iff competence == "fail", else "neither". This is the original E3 emit
      (its blanket-"neither" on late_removal cells is what `refined=True`
      corrects).

    - `refined=True`: a3b's
      two-axis model applied to E3. For a Violated term:
        * agent-evidence  = competence == "fail"                    (delete failed / no-op — residue_present)
                          OR the delete was issued AFTER T_max        (del_ts > t_max: a violation by construction)
                          OR report-before-removal                   (reported_done < first-removed-t: the agent
                                                                       claimed done while residue was still present —
                                                                       E1's report-before-converge class)
        * service-evidence = in-window delete (del_ts <= t_max) AND removal observed past T_max
                             = deletion-propagation lag (organic service eventual-consistency — the deletion
                               analogue of E1's replication-convergence lag; the {service} axis the original
                               contract's elimination argument scoped to PARTIAL-delete residue did not anticipate).
        * agent & service -> "both"; service only -> "service"; agent only -> "agent"; neither -> "neither".
      Boundary honesty: a del_ts just under t_max classes as service-evidence
      even though the agent left little budget — covered by the existing
      composite-root-cause limitation already noted for E1's `F` predicate; no
      tuning knobs, no per-cell judgment.

    `corroboration_per_term` is ALWAYS an empty list (E3 has no
    corroboration-class stream; the two-axis rule reads timing, not classes).
    Fulfilled/NotDetermined -> "neither".

    **(additive)** the return dict
    also carries a recording-level `emitted_attribution` key, rolled up from
    `attribution_per_term` with the SAME logic `a3b.emit`'s own rollup uses:
    `agent_present = any v in (agent,both)`, `service_present = any v in
    (service,both)`; both -> "both", service-only -> "service", agent-only ->
    "agent", else -> "neither". This is the A4 live-gate remedy router's
    routing input (`harness/a4_gate.py::run_gate` reads `emitted_attribution`
    off the `verify_fn` result to select a remedy) -- existing keys are
    UNCHANGED (purely additive; existing offline callers reading
    `attribution_per_term` directly are unaffected).
    """
    if verdict_result is None:
        verdict_result = a3b_verdict(recording)
    verdict_per_term = verdict_result["verdict_per_term"]

    comp = competence(recording)
    del_ts = _first_ok_delete_ts(recording)
    reported_done = (recording.get("agent", {}) or {}).get("reported_done_at_rel_s")

    attribution_per_term: dict = {}
    corroboration_per_term: dict = {}
    for term in _terms(recording):
        tid = term.get("term_id")
        t_max = term.get("t_max_s")
        v = verdict_per_term.get(tid)
        corroboration_per_term[tid] = []

        if v != "Violated":
            attribution_per_term[tid] = "neither"
            continue

        if not refined:
            attribution_per_term[tid] = "agent" if comp == "fail" else "neither"
            continue

        rem_t = _first_removed_t(recording)
        agent_evidence = (
            comp == "fail"
            or (del_ts is not None and t_max is not None and del_ts > t_max)
            or (reported_done is not None and rem_t is not None and reported_done < rem_t)
        )
        service_evidence = (
            del_ts is not None and t_max is not None and del_ts <= t_max
            and rem_t is not None and rem_t > t_max
        )
        if agent_evidence and service_evidence:
            attribution_per_term[tid] = "both"
        elif service_evidence:
            attribution_per_term[tid] = "service"
        elif agent_evidence:
            attribution_per_term[tid] = "agent"
        else:
            attribution_per_term[tid] = "neither"

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
        "agent_competence": comp,
        "corroboration_per_term": corroboration_per_term,
    }


# --- a0: naive self-report trust ---------------------------------------------

REMOVAL_SUCCESS_KEYWORDS = ("removed", "deleted", "gone", "done")
INABILITY_KEYWORDS = ("could not", "cannot", "unable to", "failed to", "did not")


def a0_verdict(recording: dict) -> dict:
    """Trust the agent's self-report.

    Parses `agent.self_reported` + `agent.final_answer` for a
    removal-success claim ("removed"/"deleted"/"gone"/"done") ->
    Fulfilled; explicit inability ("could not"/"cannot"/"unable to"/
    "failed to"/"did not") -> Violated; else NotDetermined. The null-report
    path (`reported_done_at_rel_s is None`) is ALWAYS NotDetermined
    regardless of text content, mirroring a0.py's own null-report
    discipline for E1.

    Inability is checked BEFORE success (not the prose order in the spec)
    because a real refusal often contains a success KEYWORD inside a
    negation ("the file was NOT removed") -- checking inability first
    avoids mis-reading that substring as a false Fulfilled.
    """
    agent = recording.get("agent", {}) or {}
    reported_done_at = agent.get("reported_done_at_rel_s")
    self_reported = (agent.get("self_reported") or "")
    final_answer = (agent.get("final_answer") or "")
    combined = f"{self_reported} {final_answer}".lower()

    if reported_done_at is None:
        term_verdict = "NotDetermined"
    elif any(kw in combined for kw in INABILITY_KEYWORDS):
        term_verdict = "Violated"
    elif any(kw in combined for kw in REMOVAL_SUCCESS_KEYWORDS):
        term_verdict = "Fulfilled"
    else:
        term_verdict = "NotDetermined"

    return {term.get("term_id"): term_verdict for term in _terms(recording)}


# --- a1: classical intent-agnostic monitoring --------------------------------

def _health_available(samples: list) -> bool:
    return any(s.get("sli_name") == HEALTH_SLI_NAME for s in samples)


def a1_verdict(recording: dict, *, vacuous_pass: bool = True) -> dict:
    """Call-return + health, green-unless-alarm — E3's `delete_file`
    analogue of a1.py's verdict.

    `vacuous_pass=True` (default): Fulfilled unless an alarm
    fired (a `delete_file` call returned NOT ok, or a health alarm) -- a
    no-op-claim-done (no delete call, agent claims done) is a vacuous
    PASS. Never returns NotDetermined.
    `vacuous_pass=False` (strict): a term with NO monitored
    `delete_file` call -> NotDetermined; a delete call ok -> Fulfilled; a
    delete call failed -> Violated (health-alarm still forces Violated
    either way).

    Health is read from `state_timeline.samples[]` (E3 keeps that stream
    EMPTY per the contract, so in a real E3 recording this degrades to
    call-returns-only every time) -- kept for structural parity with
    a1.py/a3b.py's health discipline should a future E3 revision ever wire
    a provider_health probe into the main stream.
    """
    agent = recording.get("agent", {}) or {}
    tool_calls = agent.get("tool_calls") or []
    samples = (recording.get("state_timeline", {}) or {}).get("samples") or []
    per_term = (recording.get("expected", {}) or {}).get("per_term", {}) or {}

    health_available = _health_available(samples)

    result: dict = {}
    for term in _terms(recording):
        tid = term.get("term_id")
        t_max = term.get("t_max_s")
        target_providers = set((per_term.get(tid, {}) or {}).get("target_providers") or [])

        required_calls = [tc for tc in tool_calls if tc.get("name") == DELETE_ACTION_NAME]
        if not required_calls:
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
            any_health_false = False
            all_health_true = True

        if any_call_errored or any_health_false:
            result[tid] = "Violated"
        elif vacuous_pass:
            result[tid] = "Fulfilled"
        elif all_calls_ok and all_health_true:
            result[tid] = "Fulfilled"
        else:
            result[tid] = "NotDetermined"

    result["a1_health_available"] = health_available
    return result


# --- a3a: contract-aware, convergence-naive (state-at-report-instant) -------

def a3a_verdict(recording: dict) -> dict:
    """State at report instant: the
    `residue_samples` entry NEAREST `agent.reported_done_at_rel_s` (by
    absolute time distance -- unlike a3a.py's E1 "smallest t_rel_s >=
    report" rule, since E3's residue poll runs continuously through the
    trial rather than only being meaningful after the placement action).

    Fulfilled     iff that nearest entry is probe_ok, path_absent, and
                      total_residue == 0.
    Violated      iff that nearest entry is probe_ok but shows residue
                      and/or the path still present.
    NotDetermined iff there is no report instant (null report), no residue
                      samples at all, or the nearest entry isn't probe_ok.
    """
    agent = recording.get("agent", {}) or {}
    reported_done_at = agent.get("reported_done_at_rel_s")
    residue_samples = _residue_samples(recording)

    result: dict = {}
    for term in _terms(recording):
        tid = term.get("term_id")

        if reported_done_at is None or not residue_samples:
            result[tid] = "NotDetermined"
            continue

        nearest = min(
            residue_samples,
            key=lambda s: abs(s.get("t_rel_s", float("inf")) - reported_done_at),
        )
        if _is_removed(nearest):
            result[tid] = "Fulfilled"
        elif nearest.get("probe_ok") is True:
            result[tid] = "Violated"
        else:
            result[tid] = "NotDetermined"

    return result


# --- setup-invalid exclusion -------------------------------------------

def is_setup_invalid(recording: dict) -> bool:
    """True iff ANY contract term's `expected.per_term[tid]` carries the
    `setup_invalid` annotation `run_e3_trial` writes when the pre-delete
    2-replica setup precondition never converged within its setup window.
    schema.validate() ignores this
    key (an unknown key on `expected.per_term[*]`), so a setup-invalid
    recording is still schema-valid; a caller/scorer MUST check this flag
    and EXCLUDE the trial before running any of the verdict/competence
    functions above -- a setup-invalid trial has no agent turn to score
    (the {agent} truth-claim requires a confirmed pre-delete 2-replica
    state, which never held here) and must never be scored as an agent
    failure.
    """
    per_term = (recording.get("expected", {}) or {}).get("per_term", {}) or {}
    return any(
        isinstance(entry, dict) and entry.get("setup_invalid") is True
        for entry in per_term.values()
    )
