"""Regression tests for harness/rq3.py.

Stdlib `unittest` only, same import-shim + local-fixture-factory convention
as harness/test_rq2.py (test modules don't import each other; each carries
its own small `_recording(...)` factory). Fully OFFLINE: every recording is
a hand-built synthetic dict, no federation contact, no model calls.

Covers:
    1. attainment_uplift computation (first_attempt_fulfilled < final_fulfilled).
    2. termination_rate.
    3. cost_per_verified_success math (3-bucket sum over ALL trials / final_fulfilled).
    4. post_gate_violation detection (a4_result claims Fulfilled but the
       recorded state_timeline's own a3b.verdict says Violated).
    5. per_leg grouping across two legs + overall aggregation.
    6. recordings without an a4_result are SKIPPED, never synthesized.

Runs either as:
    python3 -m unittest harness.test_rq3 -v      # from repo root
    python3 harness/test_rq3.py                  # directly as a script
"""
from __future__ import annotations

import os
import sys
import unittest

if __package__ in (None, ""):
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    try:
        from harness import rq3
    except ImportError:
        import rq3
else:
    from . import rq3


TERM = "T1"
T_MAX = 30.0
EXPECTED_SIZE = 4096
TARGET = "prov-de"
SOURCE = "prov-cloud-pl"
FILE_PATH = "/space-1/harness/synthetic/trial.bin"


# --- fixture builders --------------------------------------------------------

def _physical_sample(t_rel_s, value, provider=TARGET, term_id=TERM, probe_ok=True):
    return {
        "t_rel_s": t_rel_s,
        "term_id": term_id,
        "sli_name": "physicalSize",
        "value": value if probe_ok else None,
        "provider": provider,
        "probe_ok": probe_ok,
    }


def _converged_samples(step_at=5.0, until=60.0, interval=2.0, provider=TARGET,
                        term_id=TERM, expected_size=EXPECTED_SIZE):
    """physicalSize steps up to `expected_size` at `step_at` -- converges
    within T_max=30 -> a3b.verdict() == Fulfilled.
    """
    samples = []
    t = 0.0
    while t <= until + 1e-9:
        value = expected_size if t >= step_at else 0
        samples.append(_physical_sample(round(t, 3), value, provider=provider, term_id=term_id))
        t += interval
    return samples


def _never_converged_samples(until=60.0, interval=2.0, provider=TARGET, term_id=TERM):
    """physicalSize stays at 0 the whole timeline, all probe_ok -- observable
    but never converges -> a3b.verdict() == Violated.
    """
    samples = []
    t = 0.0
    while t <= until + 1e-9:
        samples.append(_physical_sample(round(t, 3), 0, provider=provider, term_id=term_id))
        t += interval
    return samples


def _late_convergence_accepted_by_remedy_samples(step_at=50.0, until=90.0, interval=3.0,
                                                  provider=TARGET, term_id=TERM,
                                                  expected_size=EXPECTED_SIZE):
    """physicalSize stays at 0 through the ORIGINAL T_max=30 and beyond,
    converging only at t=50s (well past T_max) and staying converged through
    the LAST sample -- exactly what a `wait_repoll`-remedied gate cycle's
    EXTENDED per-cycle deadline window would legitimately accept as
    Fulfilled (harness/a4_gate.py's deadline-window fix), but
    what an anchor-fixed `a3b.verdict()`-rollup post-gate check would
    mis-flag as "Violated" (late_convergence), since it never
    accounts for the extended window a remedied cycle actually used.
    """
    samples = []
    t = 0.0
    while t <= until + 1e-9:
        value = expected_size if t >= step_at else 0
        samples.append(_physical_sample(round(t, 3), value, provider=provider, term_id=term_id))
        t += interval
    return samples


def _final_sample_regresses_below_expected_samples(until=60.0, interval=3.0, provider=TARGET,
                                                     term_id=TERM, expected_size=EXPECTED_SIZE):
    """physicalSize reads `expected_size` on every sample EXCEPT the very
    LAST one, which regresses to 0 -- demonstrates the final-state check
    looks at the LAST recorded sample specifically ("what does the episode
    end on"), not "was `expected_size` ever observed anywhere in the
    timeline."
    """
    samples = []
    t = 0.0
    while t <= until + 1e-9:
        is_last = t >= until - 1e-9
        value = 0 if is_last else expected_size
        samples.append(_physical_sample(round(t, 3), value, provider=provider, term_id=term_id))
        t += interval
    return samples


def _reanchored_multicycle_accepted_samples(provider=TARGET, term_id=TERM,
                                            expected_size=EXPECTED_SIZE):
    """Merged, RE-ANCHORED multi-cycle timeline (a4_gate per-cycle deadline
    window): a REJECTED cycle 1 (t_rel_s 0->48, all 0 bytes, LONG window)
    followed by the ACCEPTED cycle 2 (t_rel_s 0->30, converging to
    `expected_size`, SHORTER window), appended in chronological (array) order.
    t_rel_s is NON-MONOTONIC across the merge and cycle 1's max t_rel_s (48)
    EXCEEDS cycle 2's (30) -- so a naive `max(t_rel_s)` rule lands on cycle 1's
    0-byte tail (6 spurious post-gate violations) while
    the chronological last sample is cycle 2's `expected_size` final. Unlike
    `_late_convergence_accepted_by_remedy_samples` (a single MONOTONIC
    timeline, where max(t_rel_s) coincides with the last sample and this
    failure mode is invisible), this reproduces the re-anchored merge that
    triggers it.
    """
    cycle1, t = [], 0.0
    while t <= 48.0 + 1e-9:
        cycle1.append(_physical_sample(round(t, 3), 0, provider=provider, term_id=term_id))
        t += 3.0
    cycle2, t = [], 0.0
    while t <= 30.0 + 1e-9:
        value = expected_size if t >= 5.0 else 0
        cycle2.append(_physical_sample(round(t, 3), value, provider=provider, term_id=term_id))
        t += 3.0
    return cycle1 + cycle2


def _a4_result(*, final_verdict, first_attempt_verdict, gate_cycles, terminated,
               verifier_probes=0, agent_invocations=1, gate_retries=0,
               final_attribution=None):
    return {
        "final_verdict": final_verdict,
        "final_attribution": final_attribution,
        "first_attempt_verdict": first_attempt_verdict,
        "gate_cycles": gate_cycles,
        "attempts": [{"cycle": 0, "feedback": None, "result": {}}],
        "remedy_trail": [],
        "terminated": terminated,
        "cost_per_bucket": {
            "verifier_probes": verifier_probes,
            "agent_invocations": agent_invocations,
            "gate_retries": gate_retries,
        },
    }


def _recording(*, model_leg, a4_result=None, samples=None, term_id=TERM, t_max_s=T_MAX,
               expected_size=EXPECTED_SIZE, target_providers=None, poll_until_rel_s=60.0):
    target_providers = target_providers if target_providers is not None else [TARGET]
    rec = {
        "trial_id": f"trial-{model_leg}-{id(samples)}",
        "sweep_id": "sweep-synthetic",
        "recorded_at": "2026-07-06T00:00:00Z",
        "harness_version": "test",
        "trial_start_utc": "2026-07-06T00:00:00Z",
        "scenario_id": "E1",
        "arm_live": "A4",
        "model_leg": model_leg,
        "trial_k": 1,
        "source_provider": SOURCE,
        "agent_mcp_host": "https://example.invalid",
        "fault": {
            "condition": "none", "target_provider": None,
            "injected_at_rel_s": None, "cleared_at_rel_s": None, "netem_delay_ms": None,
        },
        "contract": {
            "obligated_party": "agent",
            "terms": [{
                "term_id": term_id,
                "guarantee": "data physically present on >=2 distinct providers",
                "slis": ["get_file_distribution.physicalSize"],
                "slo": {
                    "predicate": "physicalSize == expected_size",
                    "min_distinct_providers": 2,
                    "target_providers": target_providers,
                },
                "t_max_s": t_max_s,
                "remedy": "re-issue / escalate / abstain (A4)",
            }],
        },
        "expected": {
            "fixture_space_id": "space-1",
            "poll_until_rel_s": poll_until_rel_s,
            "per_term": {
                term_id: {
                    "expected_size": expected_size,
                    "target_providers": target_providers,
                    "expected_paths": [FILE_PATH],
                }
            },
        },
        "agent": {
            "prompt_verbatim": "synthetic prompt",
            "tool_calls": [],
            "messages": [],
            "final_answer": "done",
            "self_reported": "done",
            "reported_done_at_rel_s": 0.0,
            "raw_trace": "absent-by-design",
        },
        "state_timeline": {
            "poll_interval_s": 2.0,
            "poll_until_rel_s": poll_until_rel_s,
            "polled_past_t_max": poll_until_rel_s >= t_max_s,
            "samples": samples or [],
            "transfer_status_samples": [],
        },
        "cost": {"verifier": {}, "model": {}, "retry": {}},
        "otel": {"harness_trace_id": "trace-synthetic", "poll_interval_s": 2.0, "correlated": False},
        "leg_scaffold": "sdk_loop",
    }
    if a4_result is not None:
        rec["a4_result"] = a4_result
    return rec


# =============================================================================
# 1. attainment_uplift
# =============================================================================

class TestAttainmentUplift(unittest.TestCase):
    def test_uplift_and_attempts_to_success(self):
        recordings = [
            _recording(
                model_leg="leg-A", samples=_converged_samples(),
                a4_result=_a4_result(final_verdict="Fulfilled", first_attempt_verdict="Fulfilled",
                                      gate_cycles=1, terminated=False),
            ),
            _recording(
                model_leg="leg-A", samples=_converged_samples(),
                a4_result=_a4_result(final_verdict="Fulfilled", first_attempt_verdict="Fulfilled",
                                      gate_cycles=1, terminated=False),
            ),
            _recording(
                model_leg="leg-A", samples=_converged_samples(),
                a4_result=_a4_result(final_verdict="Fulfilled", first_attempt_verdict="Violated",
                                      gate_cycles=2, terminated=False),
            ),
            _recording(
                model_leg="leg-A", samples=_converged_samples(),
                a4_result=_a4_result(final_verdict="Fulfilled", first_attempt_verdict="Violated",
                                      gate_cycles=3, terminated=False),
            ),
        ]

        result = rq3.rq3_metrics(recordings)
        leg = result["per_leg"]["leg-A"]

        self.assertEqual(leg["n_trials"], 4)
        self.assertEqual(leg["final_fulfilled"], 4)
        self.assertEqual(leg["first_attempt_fulfilled"], 2)
        self.assertEqual(leg["attainment_uplift"], 0.5)
        self.assertEqual(leg["attempts_to_success"], [1, 1, 2, 3])
        # every trial's own recorded timeline actually converges -> no
        # gate bugs here.
        self.assertEqual(leg["post_gate_violations"], 0)
        self.assertEqual(leg["post_gate_violation_rate"], 0.0)

        overall = result["overall"]
        self.assertEqual(overall["n_trials"], 4)
        self.assertEqual(overall["attainment_uplift"], 0.5)

    def test_zero_trials_uplift_is_none(self):
        result = rq3.rq3_metrics([])
        self.assertEqual(result["overall"]["n_trials"], 0)
        self.assertIsNone(result["overall"]["attainment_uplift"])
        self.assertIsNone(result["overall"]["termination_rate"])
        self.assertIsNone(result["overall"]["cost_per_verified_success"])
        self.assertIsNone(result["overall"]["post_gate_violation_rate"])


# =============================================================================
# 2. termination_rate
# =============================================================================

class TestTerminationRate(unittest.TestCase):
    def test_termination_rate(self):
        recordings = [
            _recording(
                model_leg="leg-B", samples=_never_converged_samples(),
                a4_result=_a4_result(final_verdict="Violated", first_attempt_verdict="Violated",
                                      gate_cycles=3, terminated=True),
            ),
            _recording(
                model_leg="leg-B", samples=_converged_samples(),
                a4_result=_a4_result(final_verdict="Fulfilled", first_attempt_verdict="Fulfilled",
                                      gate_cycles=1, terminated=False),
            ),
        ]

        result = rq3.rq3_metrics(recordings)
        leg = result["per_leg"]["leg-B"]

        self.assertEqual(leg["terminated"], 1)
        self.assertEqual(leg["termination_rate"], 0.5)


# =============================================================================
# 3. cost_per_verified_success
# =============================================================================

class TestCostPerVerifiedSuccess(unittest.TestCase):
    def test_cost_math(self):
        recordings = [
            _recording(
                model_leg="leg-cost", samples=_converged_samples(),
                a4_result=_a4_result(final_verdict="Fulfilled", first_attempt_verdict="Fulfilled",
                                      gate_cycles=1, terminated=False,
                                      verifier_probes=5, agent_invocations=1, gate_retries=0),
            ),
            _recording(
                model_leg="leg-cost", samples=_converged_samples(),
                a4_result=_a4_result(final_verdict="Fulfilled", first_attempt_verdict="Violated",
                                      gate_cycles=2, terminated=False,
                                      verifier_probes=3, agent_invocations=2, gate_retries=1),
            ),
            _recording(
                model_leg="leg-cost", samples=_never_converged_samples(),
                a4_result=_a4_result(final_verdict="Violated", first_attempt_verdict="Violated",
                                      gate_cycles=3, terminated=True,
                                      verifier_probes=8, agent_invocations=1, gate_retries=1),
            ),
        ]

        result = rq3.rq3_metrics(recordings)
        leg = result["per_leg"]["leg-cost"]

        self.assertEqual(leg["final_fulfilled"], 2)
        self.assertEqual(
            leg["total_cost_buckets"],
            {"verifier_probes": 16, "agent_invocations": 4, "gate_retries": 2},
        )
        # sum over ALL 3 trials (16+4+2=22), divided by final_fulfilled (2)
        # -- the un-converted (Violated/terminated) trial's cost still
        # counts toward the numerator.
        self.assertEqual(leg["cost_per_verified_success"], 11.0)
        self.assertAlmostEqual(leg["termination_rate"], 1 / 3)


# =============================================================================
# 4. post_gate_violation detection
# =============================================================================

class TestPostGateViolation(unittest.TestCase):
    def test_claimed_fulfilled_but_timeline_never_converges(self):
        recordings = [
            _recording(
                model_leg="leg-bug", samples=_never_converged_samples(),
                a4_result=_a4_result(final_verdict="Fulfilled", first_attempt_verdict="Fulfilled",
                                      gate_cycles=1, terminated=False),
            ),
        ]

        result = rq3.rq3_metrics(recordings)
        leg = result["per_leg"]["leg-bug"]

        self.assertEqual(leg["final_fulfilled"], 1)
        self.assertEqual(leg["post_gate_violations"], 1)
        self.assertEqual(leg["post_gate_violation_rate"], 1.0)


# =============================================================================
# 4b. final-state (anchor-independent) post-gate consistency
# =============================================================================

class TestFinalStateConsistencyCheck(unittest.TestCase):
    """The post-gate-violation check is FINAL-STATE (the
    LAST recorded probe_ok sample vs `expected_size`), anchor-independent --
    NOT a re-run of `a3b.verdict()` over the full merged timeline under the
    recording's ORIGINAL trial-start anchor. An anchor-fixed check
    would false-flag a legitimately-accepted late convergence (a remedied
    gate cycle's EXTENDED per-cycle window, harness/a4_gate.py's deadline-
    window fix) as a "violation" that isn't one.

    NOTE: E1 Form B -- the only scenario
    A4 currently runs -- carries a POSITIVE placement guarantee only (target
    `physicalSize == expected_size`), no NEGATIVE/residue-style SLI (E3's
    "no provider retains blocks").
    These tests therefore only exercise the positive-probe path; see
    `rq3._final_state_violation`'s docstring for the explicit scope note.
    """

    def test_seeded_late_convergence_accepted_by_remedy_is_not_flagged(self):
        """A recording whose merged timeline would score "Violated" under
        the ORIGINAL, un-extended anchor (samples are all 0 through T_max=30
        and well beyond) BUT whose LAST probe_ok target sample ==
        expected_size (the remedied gate cycle's extended window legitimately
        observed convergence) -> post_gate_violations == 0.
        """
        recordings = [
            _recording(
                model_leg="leg-remedied",
                samples=_late_convergence_accepted_by_remedy_samples(),
                a4_result=_a4_result(final_verdict="Fulfilled", first_attempt_verdict="Violated",
                                      gate_cycles=2, terminated=False),
            ),
        ]

        result = rq3.rq3_metrics(recordings)
        leg = result["per_leg"]["leg-remedied"]

        self.assertEqual(leg["final_fulfilled"], 1)
        self.assertEqual(leg["post_gate_violations"], 0)
        self.assertEqual(leg["post_gate_violation_rate"], 0.0)

    def test_final_sample_below_expected_is_a_violation(self):
        """The inverse: `final_verdict == "Fulfilled"` but the LAST recorded
        target sample reads below `expected_size` (a regression, or the gate
        accepted a claim the final state doesn't actually support) ->
        post_gate_violations == 1. Proves the check reads the LAST sample
        specifically, not "was expected_size ever observed."
        """
        recordings = [
            _recording(
                model_leg="leg-regressed",
                samples=_final_sample_regresses_below_expected_samples(),
                a4_result=_a4_result(final_verdict="Fulfilled", first_attempt_verdict="Fulfilled",
                                      gate_cycles=1, terminated=False),
            ),
        ]

        result = rq3.rq3_metrics(recordings)
        leg = result["per_leg"]["leg-regressed"]

        self.assertEqual(leg["final_fulfilled"], 1)
        self.assertEqual(leg["post_gate_violations"], 1)
        self.assertEqual(leg["post_gate_violation_rate"], 1.0)

    def test_reanchored_multicycle_late_accept_not_flagged(self):
        """MERGED re-anchored multi-cycle timeline: a REJECTED earlier cycle's
        high-t_rel_s tail is 0 bytes but the ACCEPTED later cycle ends at
        expected_size. The chronological last sample (array order) ==
        expected_size, so post_gate_violations == 0. A naive
        max(t_rel_s) rule would false-flag this as a violation (the rejected
        cycle's t=48 0-byte sample outranks the accepted cycle's t=30 final) --
        e.g. 6 spurious violations, 0.25/0.125
        post_gate_violation_rate vs the expected ~0."""
        recordings = [
            _recording(
                model_leg="leg-reanchored",
                samples=_reanchored_multicycle_accepted_samples(),
                a4_result=_a4_result(final_verdict="Fulfilled", first_attempt_verdict="Violated",
                                      gate_cycles=2, terminated=False),
            ),
        ]

        result = rq3.rq3_metrics(recordings)
        leg = result["per_leg"]["leg-reanchored"]

        self.assertEqual(leg["final_fulfilled"], 1)
        self.assertEqual(leg["post_gate_violations"], 0)
        self.assertEqual(leg["post_gate_violation_rate"], 0.0)


# =============================================================================
# 5. per_leg grouping + overall
# =============================================================================

class TestPerLegGrouping(unittest.TestCase):
    def test_two_legs_grouped_and_overall_combines(self):
        leg_a = [
            _recording(
                model_leg="leg-A", samples=_converged_samples(),
                a4_result=_a4_result(final_verdict="Fulfilled", first_attempt_verdict="Fulfilled",
                                      gate_cycles=1, terminated=False),
            ),
            _recording(
                model_leg="leg-A", samples=_converged_samples(),
                a4_result=_a4_result(final_verdict="Fulfilled", first_attempt_verdict="Fulfilled",
                                      gate_cycles=1, terminated=False),
            ),
        ]
        leg_b = [
            _recording(
                model_leg="leg-B", samples=_never_converged_samples(),
                a4_result=_a4_result(final_verdict="Violated", first_attempt_verdict="Violated",
                                      gate_cycles=3, terminated=True),
            ),
        ]

        result = rq3.rq3_metrics(leg_a + leg_b)

        self.assertEqual(set(result["per_leg"].keys()), {"leg-A", "leg-B"})
        self.assertEqual(result["per_leg"]["leg-A"]["n_trials"], 2)
        self.assertEqual(result["per_leg"]["leg-A"]["final_fulfilled"], 2)
        self.assertEqual(result["per_leg"]["leg-B"]["n_trials"], 1)
        self.assertEqual(result["per_leg"]["leg-B"]["final_fulfilled"], 0)

        overall = result["overall"]
        self.assertEqual(overall["n_trials"], 3)
        self.assertEqual(overall["final_fulfilled"], 2)
        self.assertEqual(overall["terminated"], 1)
        self.assertAlmostEqual(overall["termination_rate"], 1 / 3)


# =============================================================================
# 6. recordings without a4_result are skipped, not synthesized
# =============================================================================

class TestSkipsRecordingsWithoutA4Result(unittest.TestCase):
    def test_missing_a4_result_is_excluded(self):
        recordings = [
            _recording(model_leg="leg-none"),  # no a4_result key at all
            _recording(
                model_leg="leg-with", samples=_converged_samples(),
                a4_result=_a4_result(final_verdict="Fulfilled", first_attempt_verdict="Fulfilled",
                                      gate_cycles=1, terminated=False),
            ),
        ]

        result = rq3.rq3_metrics(recordings)

        self.assertNotIn("leg-none", result["per_leg"])
        self.assertIn("leg-with", result["per_leg"])
        self.assertEqual(result["overall"]["n_trials"], 1)


# =============================================================================
# 7. E3 (negative-probe) episode-end final-state check + scenario_id dispatch
# =============================================================================

E3_TERM = "T1"
E3_T_MAX = 30.0
E3_PROV_A = "prov-cloud-pl"
E3_PROV_B = "prov-de"
E3_FULL_PATH = "/space-1/harness/e3/trial-synthetic.bin"


def _e3_residue_sample(t_rel_s, *, probe_ok=True, path_absent=None, total_residue=None, per_provider=None):
    if not probe_ok:
        return {
            "t_rel_s": t_rel_s, "probe_ok": False,
            "path_absent": None, "total_residue": None, "per_provider": None,
        }
    return {
        "t_rel_s": t_rel_s,
        "probe_ok": True,
        "path_absent": path_absent,
        "total_residue": total_residue,
        "per_provider": per_provider if per_provider is not None else {E3_PROV_A: total_residue, E3_PROV_B: 0},
    }


def _e3_removed_sample(t_rel_s):
    return _e3_residue_sample(
        t_rel_s, path_absent=True, total_residue=0, per_provider={E3_PROV_A: 0, E3_PROV_B: 0}
    )


def _e3_present_sample(t_rel_s, residue=4096):
    return _e3_residue_sample(
        t_rel_s, path_absent=False, total_residue=residue, per_provider={E3_PROV_A: residue, E3_PROV_B: 0}
    )


def _e3_a4_result(*, final_verdict, gate_cycles=1, terminated=False, first_attempt_verdict=None):
    return {
        "final_verdict": final_verdict,
        "final_attribution": None,
        "first_attempt_verdict": first_attempt_verdict or final_verdict,
        "gate_cycles": gate_cycles,
        "attempts": [{"cycle": 0, "feedback": None, "result": {}}],
        "remedy_trail": [],
        "terminated": terminated,
        "cost_per_bucket": {"verifier_probes": 0, "agent_invocations": 1, "gate_retries": 0},
    }


def _e3_recording(*, model_leg, a4_result, residue_samples):
    return {
        "trial_id": f"trial-e3-{model_leg}-{id(residue_samples)}",
        "sweep_id": "sweep-synthetic",
        "recorded_at": "2026-07-15T00:00:00Z",
        "harness_version": "test",
        "trial_start_utc": "2026-07-15T00:00:00Z",
        "scenario_id": "E3",
        "arm_live": "A4",
        "model_leg": model_leg,
        "trial_k": 1,
        "source_provider": "cloud-pl",
        "agent_mcp_host": "https://example.invalid",
        "fault": {
            "condition": "none", "target_provider": None,
            "injected_at_rel_s": None, "cleared_at_rel_s": None, "netem_delay_ms": None,
        },
        "contract": {
            "obligated_party": "agent",
            "terms": [{
                "term_id": E3_TERM,
                "guarantee": "file removed everywhere (path absent + zero residue on all providers)",
                "slis": ["get_file_id.enoent", "get_file_distribution.physicalSize"],
                "slo": {"predicate": "path_absent AND total_residue==0", "min_providers_zero": "all"},
                "t_max_s": E3_T_MAX,
                "remedy": "re-issue delete / escalate",
            }],
        },
        "expected": {
            "fixture_space_id": "space-1",
            "poll_until_rel_s": 60.0,
            "per_term": {
                E3_TERM: {
                    "expected_size": 0,
                    "setup_replica_size": 4096,
                    "target_providers": [E3_PROV_A, E3_PROV_B],
                    "target_provider_labels": ["cloud-pl", "de"],
                    "expected_paths": [E3_FULL_PATH],
                }
            },
        },
        "agent": {
            "prompt_verbatim": "synthetic E3 prompt",
            "tool_calls": [],
            "messages": [],
            "final_answer": "done",
            "self_reported": "done",
            "reported_done_at_rel_s": 0.0,
            "raw_trace": "absent-by-design",
        },
        "state_timeline": {
            "poll_interval_s": 2.0,
            "poll_until_rel_s": 60.0,
            "polled_past_t_max": True,
            "samples": [],
            "residue_samples": residue_samples,
        },
        "cost": {"verifier": {}, "model": {}, "retry": {}},
        "otel": {"harness_trace_id": "trace-synthetic-e3", "poll_interval_s": 2.0, "correlated": False},
        "leg_scaffold": "sdk_loop",
        "a4_result": a4_result,
    }


class TestE3FinalStateCheck(unittest.TestCase):
    """E3's own episode-end
    final-state check (`_final_state_violation_e3`) + the `scenario_id`
    dispatch in `_leg_metrics` -- the (a)/(b)/(c)/(d) cases, plus a
    never-probed case.
    """

    def test_a_residue_at_last_probe_is_a_violation(self):
        residue_samples = [
            _e3_present_sample(0.0),
            _e3_present_sample(28.0),  # last probe_ok sample still shows residue
        ]
        recordings = [_e3_recording(
            model_leg="leg-e3-bug", a4_result=_e3_a4_result(final_verdict="Fulfilled"),
            residue_samples=residue_samples,
        )]

        result = rq3.rq3_metrics(recordings)
        leg = result["per_leg"]["leg-e3-bug"]

        self.assertEqual(leg["final_fulfilled"], 1)
        self.assertEqual(leg["post_gate_violations"], 1)
        self.assertEqual(leg["post_gate_violation_rate"], 1.0)

    def test_b_clean_removal_at_last_probe_is_not_a_violation(self):
        residue_samples = [
            _e3_present_sample(0.0),
            _e3_removed_sample(5.0),
        ]
        recordings = [_e3_recording(
            model_leg="leg-e3-clean", a4_result=_e3_a4_result(final_verdict="Fulfilled"),
            residue_samples=residue_samples,
        )]

        result = rq3.rq3_metrics(recordings)
        leg = result["per_leg"]["leg-e3-clean"]

        self.assertEqual(leg["final_fulfilled"], 1)
        self.assertEqual(leg["post_gate_violations"], 0)
        self.assertEqual(leg["post_gate_violation_rate"], 0.0)

    def test_c_anchor_independence_resets_t_rel_s_but_append_last_is_clean(self):
        # Merged multi-cycle timeline: a REJECTED cycle-1 tail (t_rel_s up to
        # 48, still residue-present) followed by an ACCEPTED cycle-2
        # (t_rel_s RESETS to 0, ends clean at t=5) -- appended in
        # chronological (array) order. max(t_rel_s) would (wrongly) land on
        # cycle-1's still-present tail (48 > 5); the append-order last is
        # cycle-2's clean removal -- proves the check uses array order, not
        # max(t_rel_s).
        residue_samples = [
            _e3_present_sample(0.0),
            _e3_present_sample(20.0),
            _e3_present_sample(48.0),  # cycle-1's rejected tail -- HIGH t_rel_s, still present
            _e3_present_sample(0.0),  # cycle-2 re-anchored -- t_rel_s resets
            _e3_removed_sample(5.0),  # cycle-2's accepted, clean final -- LOW t_rel_s but LAST in array order
        ]
        recordings = [_e3_recording(
            model_leg="leg-e3-reanchored",
            a4_result=_e3_a4_result(final_verdict="Fulfilled", gate_cycles=2),
            residue_samples=residue_samples,
        )]

        result = rq3.rq3_metrics(recordings)
        leg = result["per_leg"]["leg-e3-reanchored"]

        self.assertEqual(leg["final_fulfilled"], 1)
        self.assertEqual(leg["post_gate_violations"], 0)
        self.assertEqual(leg["post_gate_violation_rate"], 0.0)

    def test_d_e1_placement_recording_still_uses_placement_check_unchanged(self):
        # An E1 recording (scenario_id == "E1", the module's existing
        # fixture builder default) must still route through
        # `_final_state_violation` (placement), UNCHANGED, even now that
        # this test module also builds E3 recordings.
        recordings = [
            _recording(
                model_leg="leg-e1-unchanged", samples=_converged_samples(),
                a4_result=_a4_result(final_verdict="Fulfilled", first_attempt_verdict="Fulfilled",
                                      gate_cycles=1, terminated=False),
            ),
        ]

        result = rq3.rq3_metrics(recordings)
        leg = result["per_leg"]["leg-e1-unchanged"]

        self.assertEqual(leg["final_fulfilled"], 1)
        self.assertEqual(leg["post_gate_violations"], 0)
        self.assertEqual(leg["post_gate_violation_rate"], 0.0)

    def test_never_probed_is_a_violation(self):
        # No probe_ok residue sample at all -- removal was never
        # successfully confirmed by an independent probe.
        residue_samples = [
            _e3_residue_sample(0.0, probe_ok=False),
            _e3_residue_sample(10.0, probe_ok=False),
        ]
        recordings = [_e3_recording(
            model_leg="leg-e3-neverprobed", a4_result=_e3_a4_result(final_verdict="Fulfilled"),
            residue_samples=residue_samples,
        )]

        result = rq3.rq3_metrics(recordings)
        leg = result["per_leg"]["leg-e3-neverprobed"]

        self.assertEqual(leg["final_fulfilled"], 1)
        self.assertEqual(leg["post_gate_violations"], 1)
        self.assertEqual(leg["post_gate_violation_rate"], 1.0)

    def test_last_probe_missing_a_contract_provider_is_a_violation(self):
        # A Fulfilled claim whose last probe_ok sample
        # covers only ONE of the two contract providers (per_provider missing
        # E3_PROV_B) was never fully confirmed removed -> pgv, not silent pass.
        residue_samples = [
            _e3_residue_sample(0.0, path_absent=True, total_residue=0, per_provider={E3_PROV_A: 0}),
        ]
        recordings = [_e3_recording(
            model_leg="leg-e3-partialcover", a4_result=_e3_a4_result(final_verdict="Fulfilled"),
            residue_samples=residue_samples,
        )]
        leg = rq3.rq3_metrics(recordings)["per_leg"]["leg-e3-partialcover"]
        self.assertEqual(leg["final_fulfilled"], 1)
        self.assertEqual(leg["post_gate_violations"], 1)

    def test_last_probe_path_absent_none_is_a_violation(self):
        # A probe_ok sample whose path_absent is None
        # (the path was never successfully resolved) cannot confirm removal
        # even with zero residue -> pgv.
        residue_samples = [
            _e3_residue_sample(0.0, path_absent=None, total_residue=0,
                               per_provider={E3_PROV_A: 0, E3_PROV_B: 0}),
        ]
        recordings = [_e3_recording(
            model_leg="leg-e3-pathnone", a4_result=_e3_a4_result(final_verdict="Fulfilled"),
            residue_samples=residue_samples,
        )]
        leg = rq3.rq3_metrics(recordings)["per_leg"]["leg-e3-pathnone"]
        self.assertEqual(leg["final_fulfilled"], 1)
        self.assertEqual(leg["post_gate_violations"], 1)

    def test_full_cover_clean_removal_is_not_a_violation(self):
        # This guard must not false-flag a genuine clean removal covering
        # BOTH contract providers with a real path_absent=True.
        residue_samples = [_e3_present_sample(0.0), _e3_removed_sample(5.0)]
        recordings = [_e3_recording(
            model_leg="leg-e3-fullclean", a4_result=_e3_a4_result(final_verdict="Fulfilled"),
            residue_samples=residue_samples,
        )]
        leg = rq3.rq3_metrics(recordings)["per_leg"]["leg-e3-fullclean"]
        self.assertEqual(leg["final_fulfilled"], 1)
        self.assertEqual(leg["post_gate_violations"], 0)


if __name__ == "__main__":
    unittest.main()
