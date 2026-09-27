"""A4 — the live enforcement-loop gate, running a fixed
remedy policy. A4 is the ONLY live verification arm (A0-A3b are all
offline lenses over one already-recorded execution) — this module drives
the attempt/verify/remedy loop itself, at trial time.

Public surface:

    rollup_verdict(verdict_per_term) -> "Fulfilled"|"Violated"|"NotDetermined"
        The recording-level rollup of a per-term verdict_per_term dict.

    build_feedback(verify_result) -> str
        The machine-checked feedback template handed to the agent on
        `reinvoke_agent` / `reinvoke_then_wait`. This is the PLACEMENT
        (E1 Form B) template.

    build_e3_feedback(verify_result) -> str
        The E3 (negative-probe / deletion) analogue of `build_feedback` —
        a fixed template, implemented verbatim — same discipline, different
        guarantee shape (path absence + zero residue across all providers,
        not a single target's physicalSize).

    run_gate(attempt_fn, verify_fn, *, wait_fn=None, escalate_fn=None,
              n_max=N_MAX_DEFAULT, wall_clock_budget_s=WALL_CLOCK_BUDGET_S_DEFAULT,
              clock_fn=time.monotonic, remedy_by_attribution=None,
              feedback_fn=None) -> dict
        The gate loop itself: attempt -> verify (via A3b) -> accept-if-
        Fulfilled / else attribution-informed remedy -> re-verify ->
        stopping rule (N_max OR wall-clock budget) -> terminate-as-
        unverified. All side-effecting steps are INJECTED async callables so
        this loop is fully unit-testable offline (harness/test_a4_gate.py) —
        no federation contact, no model calls happen in this module itself.
        `feedback_fn` (the one surgical change to this function) is the
        module-level `build_feedback`
        by default (E1 behavior byte-identical when omitted); a caller running
        the E3 scenario passes `feedback_fn=build_e3_feedback` so the
        reinvoke-path feedback matches E3's own guarantee shape.

    make_live_attempt_fn(...) / make_live_verify_fn(...)
        Thin live-glue factories wiring `run_gate`'s injected callables to a
        real model leg + a real `Verifier` poll (the E1 Form B / placement
        shape). Duck-typed (no `panel` import) so this module stays
        importable with only stdlib + `a3b` + (optionally) `verifier` — the
        heavy model-leg SDK deps stay isolated to `panel.py`. NOT unit-tested
        here; the live path is verified live.

    make_live_e3_verify_fn(...)
        The E3 analogue of `make_live_verify_fn`, wired to
        `Verifier.poll_residue_timeline` + `harness.lenses.e3`'s
        `a3b_verdict`/`a3b_emit` instead of `poll_state_timeline` + `a3b`.
        E3 reuses `make_live_attempt_fn` UNCHANGED for its attempt side (the
        agent-invoke / re-anchor mechanism is scenario-agnostic) — only the
        verify side + feedback template are E3-specific.

**Per-cycle deadline window.** Without it, `Fulfilled` would be structurally
unreachable on any retried cycle, making RQ3 `attainment_uplift` ≡ 0 by
construction. The two live factories share one MUTABLE `window` dict —
`{"anchor_epoch": float, "deadline_epoch": float}`, both ABSOLUTE
wall-clock epochs — supplied by the caller (one per trial) and passed to
BOTH factories. The failure mode this avoids: closing over a FIXED
`(trial_start_epoch, poll_until_rel_s)` pair would make every gate cycle
re-poll the SAME `[0, poll_until_rel_s]` window relative to the ORIGINAL
trial start; a cycle invoked after that horizon had already elapsed (any
retried cycle, in practice) would observe only a single, already-late
sample and could never again see a sample inside `[0, T_max]`.

The fix:

- **Init (per trial, by the caller):** `anchor_epoch = trial_start_epoch`,
  `deadline_epoch = trial_start_epoch + T_MAX_S`.
- **Agent re-invoke** (`make_live_attempt_fn`'s `attempt_fn(feedback)` with
  `feedback is not None` — a NEW claim): RE-ANCHOR — `anchor_epoch = now`,
  `deadline_epoch = now + T_MAX_S` computed from a fresh `now`. A fresh,
  full `T_max` opportunity for the new claim.
- **Service wait** (the caller's `wait_fn`, e.g. `a4_sweep_driver.py`'s):
  extend `deadline_epoch` to an ABSOLUTE `time.time() + units * WAIT_UNIT_S`
  — NEVER an offset accumulated relative to the ORIGINAL anchor. An
  accumulated-relative-to-anchor design was tried and rejected: because
  polling itself consumes real wall-clock seconds, a deadline computed as
  "original anchor + T_max + N*unit" is chasing the elapsed clock and can
  never catch up (by the time cycle N's poll starts, the real clock has
  usually already passed that computed deadline). An ABSOLUTE, never-in-the-
  past `time.time() + ...` horizon avoids this entirely.
- **`verify_fn`** (`make_live_verify_fn`) derives `effective_t_max =
  deadline_epoch - anchor_epoch` and polls `Verifier.poll_state_timeline`
  from `t0_epoch=anchor_epoch` forward to `effective_t_max +
  OBSERVATION_MARGIN_S` — the poll's own forward-blocking loop IS the wait
  (no separate sleep is needed on a `wait_repoll` cycle). A3b's `verdict()`
  is then scored against `effective_t_max` (via a scoring-ONLY contract-term
  override — see `_contract_with_effective_t_max`), never the module's fixed
  `T_MAX_S`, so a genuine (service-side) convergence anywhere in `[anchor,
  deadline]` is recognized. The agent-facing feedback template and the
  persisted `contract` still report the contract's ORIGINAL `T_max_s` —
  only the gate's OWN internal acceptance window is extended.
- `window=None` (the default) preserves the single-cycle, no-retry shape
  (`anchor=trial_start_epoch`, `effective_t_max=t_max_s` — numerically
  identical to a first-cycle window).

Stdlib-only at the core (`run_gate`, `rollup_verdict`, `build_feedback`); the
live-glue factories add nothing beyond stdlib `asyncio`/`time` + `a3b`. No
top-level side effects on import.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

from .lenses import a3b
from .lenses import e3

# --- fixed protocol constants ------------------------------------------

N_MAX_DEFAULT = 3
T_MAX_S = 30.0
WALL_CLOCK_BUDGET_S_DEFAULT = 120.0  # ~4 x T_max

# Per-cycle deadline-window constants — see the module docstring's
# "Per-cycle deadline window" section for the full mechanism. WAIT_UNIT_S is
# the grace horizon a single `wait_repoll` remedy grants, expressed as one
# T_max (the SLA's own convergence horizon is the natural timescale the
# remedy table's "extending the observation window" language is measured
# against). OBSERVATION_MARGIN_S is the extra slack a `verify_fn` poll runs
# PAST its scoring deadline so the recorded timeline keeps the late-vs-never
# diagnostic (cycle-1's poll horizon is T_MAX_S + OBSERVATION_MARGIN_S =
# 45s).
WAIT_UNIT_S = T_MAX_S
OBSERVATION_MARGIN_S = 15.0

REMEDY_REINVOKE = "reinvoke_agent"
REMEDY_WAIT = "wait_repoll"
REMEDY_REINVOKE_THEN_WAIT = "reinvoke_then_wait"
REMEDY_ESCALATE = "escalate"

REMEDY_BY_ATTRIBUTION = {
    "agent": REMEDY_REINVOKE,
    "service": REMEDY_WAIT,
    "both": REMEDY_REINVOKE_THEN_WAIT,
    "neither": REMEDY_ESCALATE,
}


# --- rollup + feedback -------------------------------------------------------

def rollup_verdict(verdict_per_term: dict) -> str:
    """The recording-level rollup of a per-term verdict dict: "Fulfilled"
    iff `verdict_per_term` is non-empty AND every value is "Fulfilled";
    else "Violated" if any value is "Violated"; else "NotDetermined"
    (including the empty-dict case).
    """
    if verdict_per_term and all(v == "Fulfilled" for v in verdict_per_term.values()):
        return "Fulfilled"
    if any(v == "Violated" for v in verdict_per_term.values()):
        return "Violated"
    return "NotDetermined"


def build_feedback(verify_result: dict) -> str:
    """The machine-checked feedback template handed to the agent on
    re-invoke. Reads `expected_size`, `target_provider_label`,
    `target_provider_id`, `t_max_s`, `latest_physical` (may be None ->
    "no successful probe") off the `verify_fn` result dict.

    Fixed as a template used uniformly across all A4 trial runs: the target provider is
    named as the canonical **providerId WITH the human label alongside** —
    `provider 'de' (<id>)`. The
    feedback is the verifier's evidence, and the verifier speaks the
    CONTRACT's namespace (the providerId), so the gate supplies the
    GROUNDING a gap-1-failing leg lacked (it passed the label `'de'` or the
    source ID where the target providerId was required). The contract
    carrying that grounding IS the enforcement mechanism (codified
    verification logic corrects the weak agent's mis-grounding).
    Because the template is fixed rather than tuned per trial, RQ3 uplift
    is reported DECOMPOSED by the RQ1
    gap axis — gap-1 recovery = the gate supplied grounding (this ID),
    gap-2 recovery = the gate supplied patience (wait/re-poll).
    `target_provider_id` absent (older/mock callers) -> label-only,
    preserving back-compat.
    """
    expected_size = verify_result.get("expected_size")
    target_provider_label = verify_result.get("target_provider_label")
    target_provider_id = verify_result.get("target_provider_id")
    t_max_s = verify_result.get("t_max_s")
    latest_physical = verify_result.get("latest_physical")
    observed = latest_physical if latest_physical is not None else "no successful probe"
    if target_provider_id:
        provider_ref = f"provider '{target_provider_label}' ({target_provider_id})"
    else:
        provider_ref = f"provider {target_provider_label}"
    return (
        f"The file did not reach {expected_size} bytes on {provider_ref} "
        f"within the {t_max_s}s deadline "
        f"(observed: {observed} bytes). The guarantee is not met. "
        "Retry the replication."
    )


def build_e3_feedback(verify_result: dict) -> str:
    """The E3 (negative-probe / deletion) analogue of `build_feedback`.

    **Fixed template — implement verbatim, do not reword:**

        The file at <full_path> is not fully removed within the <t_max>s
        deadline: <path still resolves | path absent> and residue remains
        on <provider 'label' (id): N bytes; ...>. The required end state
        is: the path absent AND zero bytes on every provider (<all
        provider ids>). The guarantee is not met. Delete it from all
        sites.

    Reads `full_path`, `t_max_s`, `path_present` (bool), `residue_by_provider`
    (a list of `{label, id, bytes}` dicts, one per provider, as read off the
    CHRONOLOGICALLY-LAST probe_ok residue sample — see `make_live_e3_verify_fn`),
    and `all_provider_ids` off the `verify_fn` result dict. No fault/
    attribution detail is surfaced, mirroring `build_feedback`'s own
    discipline (the corrective context is exactly the observable gap plus
    the contract's own grounding, nothing more).

    **Conditional clause selection:**
      - `path_present` is True AND >=1 provider carries residue > 0: BOTH
        halves are stated ("path still resolves and residue remains on
        ...").
      - Residue is zero on every provider (no provider has bytes > 0, and
        none is a never-probed unknown): state ONLY the path half.
      - `path_present` is False (the path itself is already gone): state
        ONLY the residue half — the "path absent" alternative named in the
        template's bracket notation is therefore never itself rendered as
        prose; instead the path half is dropped entirely and only the
        outstanding residue is named, per the instruction to state only
        the residue half when the path is absent.
      - A provider that was never successfully probed (`bytes is None`)
        renders as "no successful probe" for that provider, mirroring
        `build_feedback`'s own `latest_physical is None -> "no successful
        probe"` discipline — never a guess.
      - If NEITHER half has anything to report (defensive only — this is
        UNREACHABLE via the live gate, since `build_e3_feedback` is only
        ever invoked on a `Violated` verdict, which always carries either an
        in-window residue observation or a late-removal signal), the whole
        clause falls back to "no successful probe".
    """
    full_path = verify_result.get("full_path")
    t_max_s = verify_result.get("t_max_s")
    path_present = verify_result.get("path_present")
    residue_by_provider = verify_result.get("residue_by_provider") or []
    all_provider_ids = verify_result.get("all_provider_ids") or []

    if path_present is True:
        path_clause: Optional[str] = "path still resolves"
    elif path_present is False:
        # when path absent, state only the residue half --
        # the path half is dropped entirely, not rendered as "path absent".
        path_clause = None
    else:
        # path_present is None -- the path itself was never successfully
        # probed (mirrors build_feedback's "no successful probe" discipline).
        path_clause = "no successful probe"

    residue_parts = []
    for entry in residue_by_provider:
        label = entry.get("label")
        pid = entry.get("id")
        residue_bytes = entry.get("bytes")
        if residue_bytes is None:
            residue_parts.append(f"provider '{label}' ({pid}): no successful probe")
        elif residue_bytes > 0:
            residue_parts.append(f"provider '{label}' ({pid}): {residue_bytes} bytes")
    residue_clause = "; ".join(residue_parts) if residue_parts else None

    if path_clause is not None and residue_clause is not None:
        state_clause = f"{path_clause} and residue remains on {residue_clause}"
    elif path_clause is not None:
        state_clause = path_clause
    elif residue_clause is not None:
        state_clause = f"residue remains on {residue_clause}"
    else:
        state_clause = "no successful probe"

    return (
        f"The file at {full_path} is not fully removed within the {t_max_s}s "
        f"deadline: {state_clause}. The required end state is: the path "
        f"absent AND zero bytes on every provider "
        f"({', '.join(all_provider_ids)}). The guarantee is not met. "
        "Delete it from all sites."
    )


# --- the gate loop -----------------------------------------------------------

async def run_gate(
    attempt_fn: Callable[[Optional[str]], Awaitable[Any]],
    verify_fn: Callable[[], Awaitable[dict]],
    *,
    wait_fn: Optional[Callable[[int], Awaitable[None]]] = None,
    escalate_fn: Optional[Callable[[], Awaitable[None]]] = None,
    n_max: int = N_MAX_DEFAULT,
    wall_clock_budget_s: float = WALL_CLOCK_BUDGET_S_DEFAULT,
    clock_fn: Callable[[], float] = time.monotonic,
    remedy_by_attribution: Optional[dict] = None,
    feedback_fn: Optional[Callable[[dict], str]] = None,
) -> dict:
    """The A4 attempt -> verify -> remedy -> re-verify loop. All
    side-effecting steps are injected so this is fully offline-testable —
    see harness/test_a4_gate.py.

    `attempt_fn(feedback)` runs one agent attempt (feedback=None on the
    first attempt). `verify_fn()` polls live state + runs A3b, returning at
    least `verdict_per_term`, `emitted_attribution`, `n_probes`, and the
    fields `feedback_fn` needs. `wait_fn(units)` / `escalate_fn()` are
    no-ops when not supplied. `clock_fn` is a SYNC injectable monotonic
    clock (default `time.monotonic`).

    `feedback_fn` (the one surgical, additive parameter this function
    takes): the callable used to build the reinvoke-path feedback string
    from a rejected `verify_fn()` result.
    Defaults to the module-level `build_feedback` (the E1 Form B / placement
    template) when omitted or `None`, so E1 callers' behavior is
    BYTE-IDENTICAL to before this parameter existed. An E3 caller passes
    `feedback_fn=build_e3_feedback` so the reinvoke-path feedback matches
    E3's own (deletion / residue) guarantee shape instead.
    """
    if remedy_by_attribution is None:
        remedy_by_attribution = REMEDY_BY_ATTRIBUTION
    if feedback_fn is None:
        feedback_fn = build_feedback

    start = clock_fn()
    attempts: list = []
    remedy_trail: list = []
    gate_cycles = 0
    verifier_probes = 0
    agent_invocations = 0
    first_attempt_verdict: Optional[str] = None

    # initial attempt
    a = await attempt_fn(None)
    attempts.append({"cycle": 0, "feedback": None, "result": a})
    agent_invocations += 1

    final_verdict: Optional[str] = None
    final_attribution: Optional[str] = None
    terminated = False

    while True:
        gate_cycles += 1
        vr = await verify_fn()
        verifier_probes += int(vr.get("n_probes", 0) or 0)
        verdict = rollup_verdict(vr.get("verdict_per_term", {}))
        attribution = vr.get("emitted_attribution")
        if first_attempt_verdict is None:
            first_attempt_verdict = verdict
        final_verdict = verdict
        final_attribution = attribution

        if verdict == "Fulfilled":
            terminated = False
            break

        # rejected — stopping rule BEFORE remedy
        if gate_cycles >= n_max or (clock_fn() - start) >= wall_clock_budget_s:
            terminated = True
            break

        remedy = remedy_by_attribution.get(attribution, REMEDY_ESCALATE)
        step = {
            "cycle": gate_cycles,
            "rejected_verdict": verdict,
            "attribution": attribution,
            "remedy": remedy,
        }

        if remedy in (REMEDY_REINVOKE, REMEDY_REINVOKE_THEN_WAIT):
            fb = feedback_fn(vr)
            step["feedback"] = fb
            a = await attempt_fn(fb)
            attempts.append({"cycle": gate_cycles, "feedback": fb, "result": a})
            agent_invocations += 1

        if remedy in (REMEDY_WAIT, REMEDY_REINVOKE_THEN_WAIT):
            if wait_fn is not None:
                await wait_fn(1)

        if remedy == REMEDY_ESCALATE:
            if escalate_fn is not None:
                await escalate_fn()
            # {neither} = escalate + wait one unit, then
            # re-verify (the stopping rule bounds this).
            if wait_fn is not None:
                await wait_fn(1)

        remedy_trail.append(step)

    return {
        "final_verdict": final_verdict,
        "final_attribution": final_attribution,
        "first_attempt_verdict": first_attempt_verdict,
        "gate_cycles": gate_cycles,
        "attempts": attempts,
        "remedy_trail": remedy_trail,
        "terminated": terminated,
        "cost_per_bucket": {
            "verifier_probes": verifier_probes,
            "agent_invocations": agent_invocations,
            "gate_retries": len(remedy_trail),
        },
    }


# --- thin live-glue factories (NOT unit-tested here; live wiring only) -----

def _contract_with_effective_t_max(contract: dict, term_id: str, effective_t_max: float) -> dict:
    """A SCORING-ONLY shallow copy of `contract` with `terms[].t_max_s`
    overridden to `effective_t_max` for `term_id` -- never mutates the
    caller's own `contract` (used for the persisted record + the
    agent-facing feedback, which must keep reporting the ORIGINAL
    contract T_max, not the gate's internal grace horizon)."""
    terms = []
    for term in contract.get("terms") or []:
        if term.get("term_id") == term_id:
            term = dict(term)
            term["t_max_s"] = effective_t_max
        terms.append(term)
    out = dict(contract)
    out["terms"] = terms
    return out


def make_live_attempt_fn(
    leg, scenario, *, allowed_tools, system_prompt, user_prompt, trace_sink, window=None,
):
    """Wraps one model-leg call as an async `attempt_fn(feedback)` per
    `run_gate`'s contract. Duck-typed against `panel.ModelLeg.run(prompt,
    allowed_tools, system_prompt=None) -> LegResult` — this module does NOT
    import `panel` (keeps the heavy model-leg SDK deps isolated there).
    `scenario` is accepted for call-site symmetry with the other live
    constructors; not read here (the prompt is fully assembled by the
    caller into `user_prompt`).

    `window`, when given, is the MUTABLE `{"anchor_epoch", "deadline_epoch"}`
    dict SHARED with the paired `make_live_verify_fn` call for this trial
    (see the module docstring's "Per-cycle deadline window" section). On a
    RE-INVOKE (`feedback is not None` — this is NOT the initial attempt),
    the agent is making a NEW claim, so the window RE-ANCHORS to a fresh,
    full `T_max` horizon starting now. The INITIAL attempt (`feedback is
    None`) never re-anchors — the window already starts at the caller's own
    `trial_start_epoch` init.
    """
    async def attempt_fn(feedback: Optional[str]) -> dict:
        if feedback is not None and window is not None:
            now = time.time()
            window["anchor_epoch"] = now
            window["deadline_epoch"] = now + T_MAX_S
        prompt = user_prompt if feedback is None else f"{user_prompt}\n\n{feedback}"
        result = await leg.run(prompt, allowed_tools=allowed_tools, system_prompt=system_prompt)
        trace_sink.append(result)
        return {"feedback_given": feedback is not None}

    return attempt_fn


def make_live_verify_fn(
    verifier,
    *,
    file_id,
    expected_size,
    target_provider_id,
    target_provider_label,
    term_id,
    t_max_s,
    poll_interval_s,
    trial_start_epoch,
    trace_sink,
    contract,
    expected,
    window=None,
):
    """Wraps one `Verifier.poll_state_timeline` call + A3b scoring as an
    async `verify_fn()` per `run_gate`'s contract. Assembles a MINIMAL
    recording (`contract`/`expected`/flattened `agent.tool_calls` from
    `trace_sink`/the fresh `state_timeline`) so A3b's pure functions can run
    unmodified. Correctness of the live path is verified live, not here.

    `window`, when given, is the MUTABLE `{"anchor_epoch", "deadline_epoch"}`
    dict SHARED with the paired `make_live_attempt_fn` call (see the module
    docstring's "Per-cycle deadline window" section — without it, every gate
    cycle would re-poll the same window relative to the original trial
    start, making `Fulfilled` unreachable on any retried cycle). Each call
    derives `effective_t_max =
    deadline_epoch - anchor_epoch` and polls from `t0_epoch=anchor_epoch`
    forward to `effective_t_max + OBSERVATION_MARGIN_S` — the poll's own
    forward-blocking loop supplies the real wait time for a `wait_repoll`
    remedy cycle (no separate sleep needed). A3b's `verdict()` is scored
    against `effective_t_max`, never the module's fixed `T_MAX_S`, via a
    SCORING-ONLY contract-term override (`_contract_with_effective_t_max`);
    the returned `t_max_s` field (read by `build_feedback`) still reports
    the ORIGINAL, caller-supplied `t_max_s` — only the gate's internal
    acceptance window is extended, never the agent-facing contract. Absent
    `window` (`None`, the default), this reduces to the single-cycle shape:
    `anchor=trial_start_epoch`, `effective_t_max=t_max_s`.
    """
    async def verify_fn() -> dict:
        if window is not None:
            anchor = window["anchor_epoch"]
            deadline = window["deadline_epoch"]
        else:
            anchor = trial_start_epoch
            deadline = trial_start_epoch + t_max_s
        effective_t_max = deadline - anchor

        poll_result = await asyncio.to_thread(
            verifier.poll_state_timeline,
            file_id,
            expected_size,
            target_provider_id,
            term_id,
            poll_interval_s,
            effective_t_max + OBSERVATION_MARGIN_S,
            anchor,
            effective_t_max,
            health_provider_ids=[target_provider_id],
        )

        # Per-cycle recording models the CURRENT attempt's claim: use the
        # LATEST LegResult's own tool_calls + reported_done_at_rel_s (one
        # frame — the leg's attempt-relative clock), NOT a cross-attempt
        # merge. competence.agent_competence needs reported_done_at_rel_s
        # (was omitted → competence always "fail" → agent_evidence always
        # True → a3b.emit could never render pure {service}, biasing the
        # gate's remedy to {both} on gap-2 service-faults and confounding
        # RQ3's gap-1/gap-2 (grounding/patience) decomposition). Merging
        # across attempts would break competence's within-frame
        # reported_done >= qualifying_call_ts comparison; the current
        # attempt's placement is also the correct corroboration w_start.
        latest = trace_sink[-1] if trace_sink else None
        latest_tool_calls = list(getattr(latest, "tool_calls", None) or [])
        latest_reported_done = getattr(latest, "reported_done_at_rel_s", None)

        scoring_contract = _contract_with_effective_t_max(contract, term_id, effective_t_max)
        recording = {
            "contract": scoring_contract,
            "expected": expected,
            "agent": {
                "tool_calls": latest_tool_calls,
                "reported_done_at_rel_s": latest_reported_done,
            },
            "state_timeline": poll_result,
        }
        vr_ = a3b.verdict(recording)
        er_ = a3b.emit(recording, vr_)

        samples = poll_result.get("samples") or []
        target_physical_samples = [
            s
            for s in samples
            if s.get("term_id") == term_id
            and s.get("sli_name") == a3b.PHYSICAL_SIZE_SLI
            and s.get("provider") == target_provider_id
            and s.get("probe_ok") is True
        ]
        latest_physical = (
            sorted(target_physical_samples, key=lambda s: s["t_rel_s"])[-1]["value"]
            if target_physical_samples
            else None
        )
        n_probes = sum(
            1
            for s in samples
            if s.get("sli_name") == a3b.PHYSICAL_SIZE_SLI and s.get("probe_ok") is True
        )

        return {
            "verdict_per_term": vr_["verdict_per_term"],
            "emitted_attribution": er_["emitted_attribution"],
            "n_probes": n_probes,
            "expected_size": expected_size,
            "target_provider_label": target_provider_label,
            "target_provider_id": target_provider_id,  # build_feedback quotes canonical ID + label
            "t_max_s": t_max_s,
            "latest_physical": latest_physical,
            "verdict_result": vr_,
            "emit_result": er_,
        }

    return verify_fn


def make_live_e3_verify_fn(
    verifier,
    *,
    file_id,
    full_path,
    all_provider_ids,
    all_provider_labels,
    expected_paths,
    term_id,
    t_max_s,
    poll_interval_s,
    trial_start_epoch,
    trace_sink,
    contract,
    expected,
    window=None,
):
    """The E3 (negative-probe / deletion) analogue of `make_live_verify_fn`.
    Wraps one `Verifier.poll_residue_timeline` call + `harness.lenses.e3`'s
    `a3b_verdict`/`a3b_emit` scoring as an async `verify_fn()` per
    `run_gate`'s contract -- E3's counterpart to `make_live_verify_fn`'s
    `poll_state_timeline` + `a3b` wiring. `make_live_attempt_fn` (the
    attempt/re-anchor side) is REUSED UNCHANGED for E3 -- only the verify
    side + feedback template differ from placement.

    `expected_paths` is accepted for call-site symmetry with a real E3
    trial driver's on-hand values (mirrors `make_live_attempt_fn`'s
    `scenario` parameter, "accepted... not read here") -- the assembled
    per-cycle recording's `expected` is the caller-supplied `expected` dict
    wholesale (already carrying `per_term[term_id].expected_paths`, which
    `harness.lenses.e3.competence` reads), so this function does not read
    `expected_paths` itself.

    `window`, when given, is the SAME MUTABLE `{"anchor_epoch",
    "deadline_epoch"}` dict shared with the paired `make_live_attempt_fn`
    call for this trial (module docstring's "Per-cycle deadline window" —
    the mechanism is IDENTICAL for E3; only the poll target + scoring lens
    differ). Each call derives `effective_t_max = deadline_epoch -
    anchor_epoch` and polls `poll_residue_timeline` from
    `t0_epoch=anchor_epoch` forward to `effective_t_max +
    OBSERVATION_MARGIN_S` -- the poll's own forward-blocking loop supplies
    the real wait time for a `wait_repoll` remedy cycle, same as placement.
    `_contract_with_effective_t_max` (REUSED unchanged) scores against the
    per-cycle `effective_t_max`; the returned `t_max_s` field (read by
    `build_e3_feedback`) still reports the ORIGINAL, caller-supplied
    `t_max_s` -- only the gate's internal acceptance window is extended,
    never the agent-facing contract, mirroring placement's own discipline.

    Per-cycle recording models the CURRENT attempt's claim only (the
    LATEST `LegResult`'s own `tool_calls` + `reported_done_at_rel_s`), same
    "one frame, not a cross-attempt merge" rule `make_live_verify_fn` uses
    -- `harness.lenses.e3.a3b_emit`'s refined report-before-removal /
    competence checks need this attempt's own `reported_done_at_rel_s`.

    Feedback fields are computed from the CHRONOLOGICALLY-LAST (append-
    order -- the poll's own samples list is naturally chronological within
    one call) `probe_ok` residue sample: `path_present = not
    last["path_absent"]`; `residue_by_provider` is built from
    `last["per_provider"]`, one entry per provider in `all_provider_ids`
    (id -> label via the two parallel `all_provider_ids`/
    `all_provider_labels` lists). If no `probe_ok` sample exists at all
    (defensive only -- `build_e3_feedback` is only ever invoked on a
    Violated verdict, which per `e3.a3b_verdict` always carries >=1
    `probe_ok` sample somewhere), `path_present` is `None` and
    `residue_by_provider` is empty, which `build_e3_feedback` renders as
    "no successful probe" (never a guess).
    """
    async def verify_fn() -> dict:
        if window is not None:
            anchor = window["anchor_epoch"]
            deadline = window["deadline_epoch"]
        else:
            anchor = trial_start_epoch
            deadline = trial_start_epoch + t_max_s
        effective_t_max = deadline - anchor

        poll_result = await asyncio.to_thread(
            verifier.poll_residue_timeline,
            file_id,
            full_path,
            all_provider_ids,
            poll_interval_s,
            effective_t_max + OBSERVATION_MARGIN_S,
            anchor,
        )

        # Per-cycle recording models the CURRENT attempt's claim only -- see
        # make_live_verify_fn's own docstring/comment for the full rationale
        # (competence/report-before-removal need THIS attempt's own frame).
        latest = trace_sink[-1] if trace_sink else None
        latest_tool_calls = list(getattr(latest, "tool_calls", None) or [])
        latest_reported_done = getattr(latest, "reported_done_at_rel_s", None)

        scoring_contract = _contract_with_effective_t_max(contract, term_id, effective_t_max)
        recording = {
            "contract": scoring_contract,
            "expected": expected,
            "agent": {
                "tool_calls": latest_tool_calls,
                "reported_done_at_rel_s": latest_reported_done,
            },
            "state_timeline": {"residue_samples": poll_result["residue_samples"]},
        }
        vr_ = e3.a3b_verdict(recording)
        er_ = e3.a3b_emit(recording, vr_)

        residue_samples = poll_result.get("residue_samples") or []
        probe_ok_samples = [s for s in residue_samples if s.get("probe_ok") is True]
        # CHRONOLOGICALLY-LAST -- append order (the poll's own loop already
        # appends in time order within one call), never max(t_rel_s).
        last = probe_ok_samples[-1] if probe_ok_samples else None
        n_probes = len(probe_ok_samples)

        path_present = (not last["path_absent"]) if last is not None else None
        residue_by_provider = []
        if last is not None:
            per_provider = last.get("per_provider") or {}
            for idx, pid in enumerate(all_provider_ids):
                label = all_provider_labels[idx] if idx < len(all_provider_labels) else pid
                residue_by_provider.append({"label": label, "id": pid, "bytes": per_provider.get(pid)})

        return {
            "verdict_per_term": vr_["verdict_per_term"],
            "emitted_attribution": er_["emitted_attribution"],
            "n_probes": n_probes,
            "full_path": full_path,
            "t_max_s": t_max_s,
            "path_present": path_present,
            "residue_by_provider": residue_by_provider,
            "all_provider_ids": all_provider_ids,
            "verdict_result": vr_,
            "emit_result": er_,
        }

    return verify_fn
