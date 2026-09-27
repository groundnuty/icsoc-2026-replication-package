"""Offline tests for harness/a4_gate.py -- the A4 enforcement-loop. Fully
OFFLINE: `run_gate` is driven with injected async mock `attempt_fn` /
`verify_fn` / `wait_fn` / `escalate_fn` + a fake sync `clock_fn` -- no
federation contact, no model calls, no real sleeps.

Section 1b (`TestLiveGlueDeadlineWindow`) additionally drives the REAL
`make_live_attempt_fn` / `make_live_verify_fn` live-glue factories (not
mocked out) against fake leg/verifier doubles, to prove the per-cycle
deadline-window mechanism: `Fulfilled` is reachable on a retried gate
cycle via BOTH remedy paths (agent re-invoke re-anchors; service wait
extends the deadline). No real sleeps there either -- the "advancing clock"
is a test-owned fake, never `time.sleep`.

Same import-shim convention as harness/test_rq2.py.

Runs either as:
    python3 -m unittest harness.test_a4_gate -v      # from repo root
    python3 harness/test_a4_gate.py                  # directly as a script
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

if __package__ in (None, ""):
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    try:
        from harness import a4_gate
    except ImportError:
        import a4_gate
else:
    from . import a4_gate


# --- injected async mocks ----------------------------------------------------

class ScriptedVerify:
    """An async verify_fn stub replaying a fixed script of results, one per
    call -- the LAST scripted result repeats for any call past the end (so
    "always Violated" style tests need only one scripted entry).
    """

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    async def __call__(self):
        idx = min(self.calls, len(self.results) - 1)
        self.calls += 1
        return self.results[idx]


class RecordingAttempt:
    """An async attempt_fn stub recording every feedback it was called with."""

    def __init__(self):
        self.calls = []

    async def __call__(self, feedback):
        self.calls.append(feedback)
        return {"attempt_index": len(self.calls) - 1, "feedback": feedback}


class RecordingWait:
    """An async wait_fn stub recording every `units` it was called with."""

    def __init__(self):
        self.calls = []

    async def __call__(self, units):
        self.calls.append(units)


class RecordingEscalate:
    """An async escalate_fn stub counting invocations."""

    def __init__(self):
        self.calls = 0

    async def __call__(self):
        self.calls += 1


class FakeClock:
    """A SYNC clock_fn stub replaying a fixed sequence of monotonic values
    -- the LAST value repeats for any call past the end.
    """

    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.calls = 0

    def __call__(self):
        idx = min(self.calls, len(self.sequence) - 1)
        self.calls += 1
        return self.sequence[idx]


def _violated(attribution, **extra):
    base = {"verdict_per_term": {"T1": "Violated"}, "emitted_attribution": attribution, "n_probes": 1}
    base.update(extra)
    return base


def _fulfilled(**extra):
    base = {"verdict_per_term": {"T1": "Fulfilled"}, "emitted_attribution": "neither", "n_probes": 1}
    base.update(extra)
    return base


def _not_determined(**extra):
    base = {"verdict_per_term": {"T1": "NotDetermined"}, "emitted_attribution": "neither", "n_probes": 0}
    base.update(extra)
    return base


# =============================================================================
# 1. run_gate loop shapes
# =============================================================================

class TestRunGate(unittest.TestCase):
    def test_fulfilled_first_cycle(self):
        attempt = RecordingAttempt()
        verify = ScriptedVerify([_fulfilled()])

        result = asyncio.run(a4_gate.run_gate(attempt, verify))

        self.assertEqual(result["gate_cycles"], 1)
        self.assertFalse(result["terminated"])
        self.assertEqual(result["final_verdict"], "Fulfilled")
        self.assertEqual(result["first_attempt_verdict"], "Fulfilled")
        self.assertEqual(result["cost_per_bucket"]["agent_invocations"], 1)
        self.assertEqual(result["remedy_trail"], [])

    def test_agent_remedy_then_fulfilled(self):
        attempt = RecordingAttempt()
        verify = ScriptedVerify([
            _violated("agent", expected_size=4096, target_provider_label="de",
                      t_max_s=30.0, latest_physical=0),
            _fulfilled(),
        ])

        result = asyncio.run(a4_gate.run_gate(attempt, verify))

        self.assertEqual(result["gate_cycles"], 2)
        self.assertFalse(result["terminated"])
        self.assertEqual(result["cost_per_bucket"]["agent_invocations"], 2)
        self.assertEqual(len(result["remedy_trail"]), 1)
        self.assertEqual(result["remedy_trail"][0]["remedy"], "reinvoke_agent")
        self.assertTrue(result["remedy_trail"][0]["feedback"])
        self.assertIsInstance(result["remedy_trail"][0]["feedback"], str)
        # first attempt got no feedback, the re-invoke got a non-empty string
        self.assertIsNone(attempt.calls[0])
        self.assertIsNotNone(attempt.calls[1])

    def test_service_remedy_then_fulfilled(self):
        attempt = RecordingAttempt()
        wait = RecordingWait()
        verify = ScriptedVerify([_violated("service"), _fulfilled()])

        result = asyncio.run(a4_gate.run_gate(attempt, verify, wait_fn=wait))

        self.assertEqual(result["gate_cycles"], 2)
        self.assertEqual(len(wait.calls), 1)
        self.assertEqual(wait.calls[0], 1)
        # agent was NOT re-invoked -- only the initial attempt
        self.assertEqual(result["cost_per_bucket"]["agent_invocations"], 1)
        self.assertEqual(result["remedy_trail"][0]["remedy"], "wait_repoll")

    def test_both_remedy_then_fulfilled(self):
        attempt = RecordingAttempt()
        wait = RecordingWait()
        verify = ScriptedVerify([
            _violated("both", expected_size=4096, target_provider_label="de",
                      t_max_s=30.0, latest_physical=None),
            _fulfilled(),
        ])

        result = asyncio.run(a4_gate.run_gate(attempt, verify, wait_fn=wait))

        self.assertEqual(result["gate_cycles"], 2)
        self.assertEqual(result["cost_per_bucket"]["agent_invocations"], 2)
        self.assertEqual(len(wait.calls), 1)
        self.assertEqual(result["remedy_trail"][0]["remedy"], "reinvoke_then_wait")

    def test_n_max_exhaustion(self):
        attempt = RecordingAttempt()
        wait = RecordingWait()
        verify = ScriptedVerify([_violated("service")])  # always Violated/service

        result = asyncio.run(a4_gate.run_gate(attempt, verify, wait_fn=wait, n_max=3))

        self.assertEqual(result["gate_cycles"], 3)
        self.assertTrue(result["terminated"])
        self.assertEqual(result["final_verdict"], "Violated")

    def test_wall_clock_exhaustion(self):
        attempt = RecordingAttempt()
        wait = RecordingWait()
        verify = ScriptedVerify([_violated("service")])
        clock = FakeClock([0.0, 1000.0])  # start=0.0, then far past the budget

        result = asyncio.run(a4_gate.run_gate(
            attempt, verify, wait_fn=wait, n_max=99, wall_clock_budget_s=120.0, clock_fn=clock,
        ))

        self.assertTrue(result["terminated"])
        self.assertLess(result["gate_cycles"], 99)
        self.assertEqual(result["gate_cycles"], 1)

    def test_neither_escalates(self):
        attempt = RecordingAttempt()
        wait = RecordingWait()
        escalate = RecordingEscalate()
        verify = ScriptedVerify([_violated("neither"), _fulfilled()])

        result = asyncio.run(a4_gate.run_gate(attempt, verify, wait_fn=wait, escalate_fn=escalate))

        self.assertEqual(escalate.calls, 1)
        self.assertEqual(len(wait.calls), 1)
        self.assertEqual(result["remedy_trail"][0]["remedy"], "escalate")
        self.assertEqual(result["final_verdict"], "Fulfilled")

    def test_not_determined_treated_non_fulfilled(self):
        attempt = RecordingAttempt()
        wait = RecordingWait()
        escalate = RecordingEscalate()
        verify = ScriptedVerify([_not_determined()])  # always NotDetermined/neither

        result = asyncio.run(a4_gate.run_gate(
            attempt, verify, wait_fn=wait, escalate_fn=escalate, n_max=2,
        ))

        self.assertTrue(result["terminated"])
        self.assertEqual(result["final_verdict"], "NotDetermined")
        self.assertEqual(result["gate_cycles"], 2)
        # {neither} attribution routes ND through escalate, once (before the
        # 2nd, budget-exhausting verify call).
        self.assertEqual(escalate.calls, 1)

    def test_seeded_validity_gate_not_accepted_before_convergence(self):
        """Spec's seeded validity check: a Fulfilled claim reached only AFTER
        remedy must show the pre-remedy (Violated) attempt did NOT
        short-circuit acceptance -- i.e. the claim was gated, not accepted
        on the first, unconverged attempt.
        """
        attempt = RecordingAttempt()
        verify = ScriptedVerify([
            _violated("agent", expected_size=4096, target_provider_label="de",
                      t_max_s=30.0, latest_physical=0),
            _fulfilled(),
        ])

        result = asyncio.run(a4_gate.run_gate(attempt, verify))

        self.assertEqual(result["first_attempt_verdict"], "Violated")
        self.assertEqual(result["final_verdict"], "Fulfilled")
        self.assertFalse(result["terminated"])
        self.assertGreater(result["gate_cycles"], 1)


# =============================================================================
# 1b. Live-glue per-cycle deadline window
# =============================================================================

class _StubLeg:
    """A minimal async leg double: `.run(...)` returns a `SimpleNamespace`
    carrying `.tool_calls` (matching `LegResult`'s duck-typed shape enough
    for `make_live_verify_fn`'s `getattr(leg_result, "tool_calls", None)`
    read) -- optionally advancing a shared fake clock to model agent
    think-time.
    """

    name = "stub-leg"

    def __init__(self, fake_now=None, advance_s: float = 0.0):
        self.fake_now = fake_now
        self.advance_s = advance_s
        self.calls = 0

    async def run(self, prompt, allowed_tools=None, system_prompt=None):
        self.calls += 1
        if self.fake_now is not None:
            self.fake_now[0] += self.advance_s
        return SimpleNamespace(tool_calls=[])


class _ReanchorKeyedVerifier:
    """Fake verifier whose `poll_state_timeline` returns a CONVERGED sample
    (within window) iff called with a `t0_epoch` DIFFERENT from the
    trial's ORIGINAL anchor -- i.e. iff `make_live_attempt_fn`'s re-anchor
    branch already fired before this poll call. Proves reachability is
    conditioned on the re-anchor actually happening, not just on retries /
    elapsed time in general.
    """

    def __init__(self, original_anchor: float):
        self.original_anchor = original_anchor
        self.calls: list = []

    def poll_state_timeline(self, file_id, expected_size, target_provider_id,
                             term_id, poll_interval_s, poll_until_rel_s,
                             t0_epoch, t_max_s, health_provider_ids=None):
        self.calls.append(t0_epoch)
        reanchored = t0_epoch != self.original_anchor
        value = expected_size if reanchored else 0
        sample = {
            "t_rel_s": 1.0, "term_id": term_id, "sli_name": "physicalSize",
            "value": value, "provider": target_provider_id, "probe_ok": True,
        }
        return {
            "poll_interval_s": poll_interval_s, "poll_until_rel_s": poll_until_rel_s,
            "polled_past_t_max": poll_until_rel_s >= t_max_s,
            "samples": [sample],
        }


class _ClockAdvancingVerifier:
    """Fake verifier whose `poll_state_timeline` simulates the REAL
    implementation's forward-blocking behaviour (one sample per
    `poll_interval_s` tick from `t_rel=0` to `>= poll_until_rel_s`) against
    a shared, test-owned fake clock (`fake_now`) that THIS CALL ITSELF
    advances to `t0_epoch + <the last sampled t_rel>` -- modelling the real
    wall-clock time a live poll call actually consumes. `convergence_visible_from`
    is the ABSOLUTE epoch at/after which a probe observes the target
    converged (`>=` that epoch -> `value=expected_size`; before -> `value=0`).

    A STATIONARY clock (one that never advances) would trivially "pass" even
    the REJECTED cumulative-`extra_budget_s` design -- the advancing clock
    is essential to what this test proves, not incidental.
    """

    def __init__(self, fake_now: list, convergence_visible_from: float):
        self.fake_now = fake_now
        self.convergence_visible_from = convergence_visible_from
        self.calls: list = []

    def poll_state_timeline(self, file_id, expected_size, target_provider_id,
                             term_id, poll_interval_s, poll_until_rel_s,
                             t0_epoch, t_max_s, health_provider_ids=None):
        self.calls.append({
            "t0_epoch": t0_epoch, "poll_until_rel_s": poll_until_rel_s, "t_max_s": t_max_s,
        })
        samples = []
        t_rel = 0.0
        while True:
            abs_epoch = t0_epoch + t_rel
            converged = abs_epoch >= self.convergence_visible_from
            samples.append({
                "t_rel_s": round(t_rel, 3), "term_id": term_id, "sli_name": "physicalSize",
                "value": expected_size if converged else 0,
                "provider": target_provider_id, "probe_ok": True,
            })
            if t_rel >= poll_until_rel_s:
                break
            t_rel += poll_interval_s
        end_epoch = t0_epoch + t_rel
        if end_epoch > self.fake_now[0]:
            self.fake_now[0] = end_epoch
        return {
            "poll_interval_s": poll_interval_s, "poll_until_rel_s": poll_until_rel_s,
            "polled_past_t_max": poll_until_rel_s >= t_max_s,
            "samples": samples,
        }


class TestLiveGlueDeadlineWindow(unittest.TestCase):
    """`Fulfilled` must be reachable on a retried gate cycle via BOTH
    remedy paths, driven through the REAL live-glue factories
    (`make_live_attempt_fn` / `make_live_verify_fn`), not just `run_gate`
    with a fully-mocked `verify_fn`.
    """

    def _contract_and_expected(self, term_id: str, t_max_s: float, expected_size: int,
                                target_provider_id: str) -> tuple:
        contract = {"terms": [{"term_id": term_id, "t_max_s": t_max_s}]}
        expected = {"per_term": {term_id: {
            "expected_size": expected_size, "target_providers": [target_provider_id],
        }}}
        return contract, expected

    def test_agent_reanchor_makes_cycle2_fulfilled_reachable(self):
        """Cycle-2 `Fulfilled` REACHABLE via a re-anchored agent-retry: the
        fake verifier only reports convergence once `make_live_attempt_fn`'s
        re-invoke branch has actually re-anchored the shared window to a
        FRESH `t0_epoch` -- proving reachability is conditioned on the
        re-anchor firing, not merely on a second cycle happening.
        """
        trial_start_epoch = 1_000_000.0
        term_id, expected_size, target_provider_id = "T1", 4096, "prov-de"
        contract, expected = self._contract_and_expected(
            term_id, a4_gate.T_MAX_S, expected_size, target_provider_id,
        )

        verifier = _ReanchorKeyedVerifier(original_anchor=trial_start_epoch)
        leg = _StubLeg()
        trace_sink: list = []
        window = {"anchor_epoch": trial_start_epoch, "deadline_epoch": trial_start_epoch + a4_gate.T_MAX_S}

        attempt_fn = a4_gate.make_live_attempt_fn(
            leg, {}, allowed_tools=[], system_prompt="sys", user_prompt="do it",
            trace_sink=trace_sink, window=window,
        )
        verify_fn = a4_gate.make_live_verify_fn(
            verifier, file_id="f1", expected_size=expected_size,
            target_provider_id=target_provider_id, target_provider_label="de",
            term_id=term_id, t_max_s=a4_gate.T_MAX_S, poll_interval_s=3.0,
            trial_start_epoch=trial_start_epoch, trace_sink=trace_sink,
            contract=contract, expected=expected, window=window,
        )

        result = asyncio.run(a4_gate.run_gate(attempt_fn, verify_fn, n_max=3, wall_clock_budget_s=120.0))

        self.assertEqual(result["final_verdict"], "Fulfilled")
        self.assertGreaterEqual(result["gate_cycles"], 2)
        self.assertFalse(result["terminated"])
        # cycle 1 polled at the ORIGINAL anchor; cycle 2 polled at a DIFFERENT
        # (re-anchored) epoch -- the re-anchor is what made convergence visible.
        self.assertEqual(verifier.calls[0], trial_start_epoch)
        self.assertNotEqual(verifier.calls[1], trial_start_epoch)
        self.assertEqual(leg.calls, 2)  # initial attempt + one re-invoke

    def test_service_wait_extends_deadline_reaches_fulfilled_with_advancing_clock(self):
        """Extended-window service-wait reachability (absolute-deadline
        design): a WAIT-ONLY remedy (no agent reinvoke) must reach
        `Fulfilled` once real elapsed time genuinely advances past `T_max`,
        by extending `window["deadline_epoch"]` to `time.time() +
        WAIT_UNIT_S` (an ABSOLUTE, never-in-the-past horizon) -- NOT the
        REJECTED `extra_budget_s`-relative-to-original-anchor design, which
        chases elapsed real time and never catches up (by cycle 2 the real
        clock has already passed a cumulative-relative deadline, since
        polling itself consumes real wall-clock seconds).

        This test drives the REAL `make_live_attempt_fn` (cycle-0 attempt
        only -- feedback stays `None` throughout, so NO re-anchor fires) +
        the REAL `make_live_verify_fn` across two direct calls, applying the
        driver's `wait_fn` FORMULA between them
        (`window["deadline_epoch"] = time.time() + units * WAIT_UNIT_S`)
        rather than driving through `run_gate`'s attribution-based remedy
        routing. Why: `make_live_verify_fn`'s own per-cycle recording never
        populates `agent.reported_done_at_rel_s` (only `tool_calls`), which
        makes `harness.lenses.competence.agent_competence` return `"fail"`
        unconditionally in the LIVE glue's internal scoring -- collapsing
        `a3b.emit`'s attribution to always-`agent_evidence=True` and making
        a PURE `"service"` attribution unreachable via the live gate's own
        natural routing (a separate, pre-existing gap, out of THIS fix's
        scope). Applying the wait_fn
        formula directly is exactly what `run_gate` does on a
        `wait_repoll` cycle regardless of how the remedy was selected, so
        this proves the DEADLINE-EXTENSION MECHANISM itself (this fix's
        subject) reaches `Fulfilled` on a wait-only retry.

        The mock verifier's `poll_state_timeline` ADVANCES a shared fake
        clock to model the real wall-clock time a live forward-blocking
        poll consumes -- a stationary-clock mock would trivially pass even
        the rejected cumulative design, so the advancing clock is
        essential to this test's claim.
        """
        trial_start_epoch = 1_000_000.0
        term_id, expected_size, target_provider_id = "T1", 4096, "prov-de"
        contract, expected = self._contract_and_expected(
            term_id, a4_gate.T_MAX_S, expected_size, target_provider_id,
        )
        fake_now = [trial_start_epoch]

        # Convergence becomes visible at T0+50s -- AFTER cycle 1's full poll
        # horizon (T0 + T_MAX_S + OBSERVATION_MARGIN_S = T0+45) ends, so
        # cycle 1 must NOT see it; BEFORE cycle 2's extended horizon
        # (T0 + 75 + 15 = T0+90) ends, so cycle 2 MUST see it.
        convergence_visible_from = trial_start_epoch + 50.0
        verifier = _ClockAdvancingVerifier(fake_now, convergence_visible_from)
        leg = _StubLeg(fake_now=fake_now, advance_s=2.0)  # trivial agent think-time
        trace_sink: list = []
        window = {"anchor_epoch": trial_start_epoch, "deadline_epoch": trial_start_epoch + a4_gate.T_MAX_S}

        attempt_fn = a4_gate.make_live_attempt_fn(
            leg, {}, allowed_tools=[], system_prompt="sys", user_prompt="do it",
            trace_sink=trace_sink, window=window,
        )
        verify_fn = a4_gate.make_live_verify_fn(
            verifier, file_id="f1", expected_size=expected_size,
            target_provider_id=target_provider_id, target_provider_label="de",
            term_id=term_id, t_max_s=a4_gate.T_MAX_S, poll_interval_s=3.0,
            trial_start_epoch=trial_start_epoch, trace_sink=trace_sink,
            contract=contract, expected=expected, window=window,
        )

        asyncio.run(attempt_fn(None))  # cycle 0 -- the only attempt

        vr1 = asyncio.run(verify_fn())  # cycle 1
        self.assertEqual(a4_gate.rollup_verdict(vr1["verdict_per_term"]), "Violated")
        self.assertEqual(verifier.calls[0]["t0_epoch"], trial_start_epoch)
        self.assertAlmostEqual(
            verifier.calls[0]["poll_until_rel_s"], a4_gate.T_MAX_S + a4_gate.OBSERVATION_MARGIN_S,
        )

        # --- the WAIT remedy, applied exactly as a4_sweep_driver.wait_fn
        # does: an ABSOLUTE horizon from "now" (the advanced fake clock),
        # anchor UNCHANGED (pure wait, never re-anchors).
        window["deadline_epoch"] = fake_now[0] + 1 * a4_gate.WAIT_UNIT_S

        vr2 = asyncio.run(verify_fn())  # cycle 2 -- NO re-invoke happened
        self.assertEqual(a4_gate.rollup_verdict(vr2["verdict_per_term"]), "Fulfilled")
        # anchor never moved -- Fulfilled was reached WITHOUT a re-anchor.
        self.assertEqual(verifier.calls[1]["t0_epoch"], trial_start_epoch)
        self.assertGreater(verifier.calls[1]["poll_until_rel_s"], verifier.calls[0]["poll_until_rel_s"])
        self.assertEqual(len(trace_sink), 1)  # attempt_fn was called exactly once

    def test_competence_populated_makes_pure_service_reachable(self):
        """Competence-gap fix: make_live_verify_fn's per-cycle recording now
        carries agent.reported_done_at_rel_s (from the LATEST LegResult), so a
        COMPETENT current attempt on a service-stalled fault reads PURE
        {service} (competence pass + service evidence). Previously the recording
        omitted reported_done_at_rel_s → competence always "fail" →
        agent_evidence always True → pure {service} unreachable → the gate would
        mis-remedy a gap-2 service-fault as {both}, confounding RQ3's
        gap-1/gap-2 (grounding/patience) decomposition. This proves the fix
        via the live glue's own natural a3b.emit routing.
        """
        term_id, expected_size, dep = "T1", 4096, "prov-de"
        contract, expected = self._contract_and_expected(
            term_id, a4_gate.T_MAX_S, expected_size, dep,
        )
        trial_start_epoch = 1_000_000.0

        class _CompetentLeg:  # valid replication op + honest done-report
            name = "competent-leg"

            async def run(self, prompt, allowed_tools=None, system_prompt=None):
                return SimpleNamespace(
                    tool_calls=[{
                        "name": "schedule_file_replication",
                        "args": {"target_provider_id": dep, "file_id_or_path": "/f.bin"},
                        "ok": True, "ts_rel_s": 1.0,
                    }],
                    reported_done_at_rel_s=2.0,
                )

        class _StalledVerifier:  # target probed but STALLED (service fault); health up
            def poll_state_timeline(self, file_id, expected_size, target_provider_id,
                                     term_id, poll_interval_s, poll_until_rel_s,
                                     t0_epoch, t_max_s, health_provider_ids=None):
                samples = [
                    {"t_rel_s": 5.0, "term_id": term_id, "sli_name": "physicalSize",
                     "value": 0, "provider": target_provider_id, "probe_ok": True},
                    {"t_rel_s": 5.0, "term_id": term_id, "sli_name": "provider_health",
                     "value": True, "provider": target_provider_id, "probe_ok": True},
                ]
                return {"poll_interval_s": poll_interval_s, "poll_until_rel_s": poll_until_rel_s,
                        "polled_past_t_max": True, "samples": samples}

        trace_sink: list = []
        window = {"anchor_epoch": trial_start_epoch, "deadline_epoch": trial_start_epoch + a4_gate.T_MAX_S}
        attempt_fn = a4_gate.make_live_attempt_fn(
            _CompetentLeg(), {}, allowed_tools=[], system_prompt="",
            user_prompt="do it", trace_sink=trace_sink,
        )
        verify_fn = a4_gate.make_live_verify_fn(
            _StalledVerifier(), file_id="/f.bin", expected_size=expected_size,
            target_provider_id=dep, target_provider_label="de", term_id=term_id,
            t_max_s=a4_gate.T_MAX_S, poll_interval_s=1.0, trial_start_epoch=trial_start_epoch,
            trace_sink=trace_sink, contract=contract, expected=expected, window=window,
        )
        asyncio.run(attempt_fn(None))   # populate trace_sink with the competent attempt
        vr = asyncio.run(verify_fn())
        self.assertEqual(vr["verdict_per_term"][term_id], "Violated")  # stalled = service Violated
        self.assertEqual(vr["emitted_attribution"], "service")  # competence pass → PURE service


# =============================================================================
# 1c. feedback_fn injection -- the one surgical, additive change to run_gate
# =============================================================================

class TestFeedbackFnInjection(unittest.TestCase):
    def test_default_feedback_fn_is_module_build_feedback(self):
        attempt = RecordingAttempt()
        verify = ScriptedVerify([
            _violated("agent", expected_size=4096, target_provider_label="de",
                      t_max_s=30.0, latest_physical=0),
            _fulfilled(),
        ])

        result = asyncio.run(a4_gate.run_gate(attempt, verify))

        # omitting feedback_fn preserves EXACT prior behavior: the
        # feedback string is whatever the module-level build_feedback emits.
        expected_feedback = a4_gate.build_feedback(
            {"expected_size": 4096, "target_provider_label": "de", "t_max_s": 30.0, "latest_physical": 0}
        )
        self.assertEqual(result["remedy_trail"][0]["feedback"], expected_feedback)
        self.assertEqual(attempt.calls[1], expected_feedback)

    def test_custom_feedback_fn_is_used_instead(self):
        attempt = RecordingAttempt()
        verify = ScriptedVerify([_violated("agent"), _fulfilled()])
        sentinel = "CUSTOM FEEDBACK STRING"

        def custom_feedback_fn(vr):
            return sentinel

        result = asyncio.run(a4_gate.run_gate(attempt, verify, feedback_fn=custom_feedback_fn))

        self.assertEqual(result["remedy_trail"][0]["feedback"], sentinel)
        self.assertEqual(attempt.calls[1], sentinel)

    def test_custom_feedback_fn_used_on_reinvoke_then_wait_too(self):
        attempt = RecordingAttempt()
        wait = RecordingWait()
        verify = ScriptedVerify([_violated("both"), _fulfilled()])
        sentinel = "BOTH-PATH CUSTOM FEEDBACK"

        result = asyncio.run(a4_gate.run_gate(
            attempt, verify, wait_fn=wait, feedback_fn=lambda vr: sentinel,
        ))

        self.assertEqual(result["remedy_trail"][0]["remedy"], "reinvoke_then_wait")
        self.assertEqual(result["remedy_trail"][0]["feedback"], sentinel)
        self.assertEqual(attempt.calls[1], sentinel)


# =============================================================================
# 2. rollup_verdict
# =============================================================================

class TestRollupVerdict(unittest.TestCase):
    def test_all_fulfilled(self):
        self.assertEqual(
            a4_gate.rollup_verdict({"T1": "Fulfilled", "T2": "Fulfilled"}), "Fulfilled",
        )

    def test_any_violated(self):
        self.assertEqual(
            a4_gate.rollup_verdict({"T1": "Fulfilled", "T2": "Violated"}), "Violated",
        )

    def test_not_determined_only(self):
        self.assertEqual(a4_gate.rollup_verdict({"T1": "NotDetermined"}), "NotDetermined")

    def test_empty_dict(self):
        self.assertEqual(a4_gate.rollup_verdict({}), "NotDetermined")

    def test_mixed_fulfilled_and_violated_is_violated(self):
        self.assertEqual(
            a4_gate.rollup_verdict({"T1": "Fulfilled", "T2": "Violated", "T3": "Fulfilled"}),
            "Violated",
        )

    def test_mixed_fulfilled_and_not_determined_is_not_determined(self):
        self.assertEqual(
            a4_gate.rollup_verdict({"T1": "Fulfilled", "T2": "NotDetermined"}),
            "NotDetermined",
        )


# =============================================================================
# 3. build_feedback
# =============================================================================

class TestBuildFeedback(unittest.TestCase):
    def test_includes_expected_fields(self):
        text = a4_gate.build_feedback({
            "expected_size": 4096, "target_provider_label": "de", "t_max_s": 30.0,
            "latest_physical": 512,
        })
        self.assertIn("4096", text)
        self.assertIn("de", text)
        self.assertIn("30.0", text)
        self.assertIn("512", text)
        self.assertIn("Retry the replication", text)

    def test_none_latest_physical_reads_no_successful_probe(self):
        text = a4_gate.build_feedback({
            "expected_size": 4096, "target_provider_label": "de", "t_max_s": 30.0,
            "latest_physical": None,
        })
        self.assertIn("no successful probe", text)

    def test_freeze_quotes_canonical_provider_id_with_label(self):
        # When target_provider_id is present, the feedback
        # names it canonically WITH the label alongside — the gate supplies
        # the grounding a gap-1-failing leg lacked.
        pid = "7cfe30bef82a2c904c15624f752c58ddchd932"
        text = a4_gate.build_feedback({
            "expected_size": 4096, "target_provider_label": "de",
            "target_provider_id": pid, "t_max_s": 30.0, "latest_physical": 0,
        })
        self.assertIn(pid, text)          # canonical providerId present
        self.assertIn("de", text)          # label alongside
        self.assertIn(f"'de' ({pid})", text)

    def test_missing_provider_id_falls_back_to_label_only(self):
        # Back-compat: mock/older callers without target_provider_id → label-only.
        text = a4_gate.build_feedback({
            "expected_size": 4096, "target_provider_label": "de", "t_max_s": 30.0,
            "latest_physical": 0,
        })
        self.assertIn("provider de", text)
        self.assertNotIn("(", text.split("deadline")[0])  # no ID-paren before the deadline clause


if __name__ == "__main__":
    unittest.main()
