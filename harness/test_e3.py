"""Regression tests for harness/lenses/e3.py.

Stdlib `unittest` only, same import-shim convention as harness/test_schema.py
and harness/test_lenses.py. Run either as:

    python3 -m unittest harness.test_e3 -v      # from repo root
    python3 harness/test_e3.py                  # directly as a script

Fully OFFLINE: every recording here is a hand-built SYNTHETIC dict (the
`_recording(...)` factory below). No federation contact, no model calls, no
network — E3's own record loop (`runner.run_e3_trial`) is NOT exercised
here (it needs a live leg/fixture_mgr/verifier); this file only tests the
pure offline lenses in harness/lenses/e3.py against hand-built recordings,
each round-tripped through schema.validate() so shape drift is caught.

Covers 5 cases:
    (a) clean removal -> A3b Fulfilled, A0/A1/A3a Fulfilled, competence pass
    (b) no-op-claim-done (claims removed, NO delete call, residue persists)
        -> A3b Violated attribution=agent, A0 Fulfilled (false-pass), A1
        vacuous_pass=True Fulfilled (false-pass) / vacuous_pass=False ND,
        A3a Violated
    (c) delete-call-ok-but-residue (service-lag): residue present <= t_max,
        clears only AFTER t_max -> A3b Violated, mode="late_removal";
        attribution "neither" even though competence passed (no {service}
        path exists in E3)
    (d) never-observable (every residue probe in-window is dead) -> A3b
        NotDetermined; A3a NotDetermined
    (e) setup_invalid annotation -> is_setup_invalid() flags it; still
        schema-valid (schema.py ignores the unknown key on
        expected.per_term[*])
plus a few extra edge cases (a0 explicit inability, competence wrong-path,
a3b_emit's fault-blindness, residue_samples out-of-order robustness).
"""
from __future__ import annotations

import os
import sys
import unittest

# --- make `harness` importable whether run as a script or as -m -------------
if __package__ in (None, ""):
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    try:
        from harness import schema
        from harness.lenses import e3
    except ImportError:
        import schema
        from lenses import e3
else:
    from . import schema
    from .lenses import e3


TERM = "T1"
T_MAX = 30.0
CONTENT_SIZE = 4096
PROV_A = "prov-cloud-pl"
PROV_B = "prov-de"
FULL_PATH = "/space-1/harness/e3/trial-synthetic.bin"


# --- fixture builders --------------------------------------------------------

def _delete_call(ts_rel_s, path=FULL_PATH, ok=True, result=None):
    return {
        "name": "delete_file",
        "args": {"file_id_or_path": path},
        "result": result if result is not None else {},
        "ts_rel_s": ts_rel_s,
        "ok": ok,
    }


def _other_call(name, ts_rel_s, ok=True):
    return {"name": name, "args": {}, "result": {}, "ts_rel_s": ts_rel_s, "ok": ok}


def _residue_sample(t_rel_s, *, probe_ok=True, path_absent=None, total_residue=None, per_provider=None):
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
        "per_provider": per_provider if per_provider is not None else {PROV_A: total_residue, PROV_B: 0},
    }


def _present_sample(t_rel_s, residue=CONTENT_SIZE):
    """A residue probe that found the path still present with bytes on it."""
    return _residue_sample(
        t_rel_s, path_absent=False, total_residue=residue,
        per_provider={PROV_A: residue, PROV_B: 0},
    )


def _removed_sample(t_rel_s):
    """A residue probe confirming full removal (path gone, zero residue everywhere)."""
    return _residue_sample(
        t_rel_s, path_absent=True, total_residue=0, per_provider={PROV_A: 0, PROV_B: 0}
    )


def _dead_sample(t_rel_s):
    """A residue probe that itself failed (network) -- never a guess."""
    return _residue_sample(t_rel_s, probe_ok=False)


def _recording(
    *,
    tool_calls=None,
    residue_samples=None,
    self_reported="done",
    final_answer="done",
    reported_done_at_rel_s=0.0,
    t_max_s=T_MAX,
    poll_until_rel_s=60.0,
    target_providers=None,
    setup_invalid=False,
    fault=None,
    leg_scaffold="sdk_loop",
):
    target_providers = target_providers if target_providers is not None else [PROV_A, PROV_B]
    residue_samples = residue_samples if residue_samples is not None else []

    per_term_entry = {
        "expected_size": 0,
        "setup_replica_size": CONTENT_SIZE,
        "target_providers": target_providers,
        "target_provider_labels": ["cloud-pl", "de"],
        "expected_paths": [FULL_PATH],
    }
    if setup_invalid:
        per_term_entry["setup_invalid"] = True

    return {
        "trial_id": "trial-synthetic",
        "sweep_id": "sweep-synthetic",
        "recorded_at": "2026-07-07T00:00:00Z",
        "harness_version": "test",
        "trial_start_utc": "2026-07-07T00:00:00Z",
        "scenario_id": "E3",
        "arm_live": "A0",
        "arm_class": "prior_practice",
        "model_leg": "test-model",
        "trial_k": 1,
        "source_provider": "cloud-pl",
        "agent_mcp_host": "https://example.invalid",
        "fault": fault
        if fault is not None
        else {
            "condition": "none",
            "target_provider": None,
            "injected_at_rel_s": None,
            "cleared_at_rel_s": None,
            "netem_delay_ms": None,
        },
        "contract": {
            "obligated_party": "agent",
            "terms": [
                {
                    "term_id": TERM,
                    "guarantee": "file removed everywhere (path absent + zero residue on all providers)",
                    "slis": ["get_file_id.enoent", "get_file_distribution.physicalSize"],
                    "slo": {"predicate": "path_absent AND total_residue==0", "min_providers_zero": "all"},
                    "t_max_s": t_max_s,
                    "remedy": "re-issue delete / escalate",
                }
            ],
        },
        "expected": {
            "fixture_space_id": "space-1",
            "poll_until_rel_s": poll_until_rel_s,
            "per_term": {TERM: per_term_entry},
        },
        "agent": {
            "prompt_verbatim": "synthetic E3 prompt",
            "tool_calls": tool_calls or [],
            "messages": [],
            "final_answer": final_answer,
            "self_reported": self_reported,
            "reported_done_at_rel_s": reported_done_at_rel_s,
            "raw_trace": "absent-by-design",
        },
        "state_timeline": {
            "poll_interval_s": 2.0,
            "poll_until_rel_s": poll_until_rel_s,
            "polled_past_t_max": poll_until_rel_s >= t_max_s,
            "samples": [],
            "residue_samples": residue_samples,
        },
        "cost": {
            "verifier": {"probe_count": len(residue_samples), "wall_latency_s": 0.0},
            "model": {"prompt_tokens": 0, "completion_tokens": 0, "estimation": "measured", "usd": 0.0},
            "retry": {"attempts": 0, "model_usd": 0.0, "verifier_probe_count": 0},
        },
        "otel": {"harness_trace_id": "trace-synthetic", "poll_interval_s": 2.0, "correlated": False},
        "leg_scaffold": leg_scaffold,
    }


def _assert_schema_valid(testcase, recording):
    try:
        schema.validate(recording)
    except schema.SchemaError as e:  # pragma: no cover - failure path
        testcase.fail(f"fixture should be schema-valid but got: {e}")


# =============================================================================
# (a) clean removal
# =============================================================================

class TestCleanRemoval(unittest.TestCase):
    def test_case_a_clean_removal(self):
        tool_calls = [_delete_call(ts_rel_s=1.0)]
        residue_samples = [
            _present_sample(0.0),
            _removed_sample(2.5),
            _removed_sample(5.0),
            _removed_sample(35.0),
        ]
        rec = _recording(
            tool_calls=tool_calls,
            residue_samples=residue_samples,
            self_reported="done",
            final_answer="The file has been removed from all sites.",
            reported_done_at_rel_s=2.5,
        )
        _assert_schema_valid(self, rec)

        self.assertEqual(e3.competence(rec), "pass")

        a3b_result = e3.a3b_verdict(rec)
        self.assertEqual(a3b_result["verdict_per_term"][TERM], "Fulfilled")
        self.assertIsNone(a3b_result["violation_mode_per_term"][TERM])

        emit = e3.a3b_emit(rec, a3b_result)
        self.assertEqual(emit["attribution_per_term"][TERM], "neither")
        self.assertEqual(emit["corroboration_per_term"][TERM], [])
        self.assertEqual(emit["agent_competence"], "pass")

        self.assertEqual(e3.a0_verdict(rec)[TERM], "Fulfilled")
        self.assertEqual(e3.a1_verdict(rec, vacuous_pass=True)[TERM], "Fulfilled")
        self.assertEqual(e3.a1_verdict(rec, vacuous_pass=False)[TERM], "Fulfilled")
        self.assertEqual(e3.a3a_verdict(rec)[TERM], "Fulfilled")
        self.assertFalse(e3.is_setup_invalid(rec))


# =============================================================================
# (b) no-op-claim-done -- the headline false-pass
# =============================================================================

class TestNoOpClaimDone(unittest.TestCase):
    def test_case_b_no_op_claim_done_false_pass(self):
        # NO delete_file call anywhere in the trace -- the agent never
        # acted, yet claims the file was removed.
        residue_samples = [
            _present_sample(0.0),
            _present_sample(5.0),
            _present_sample(35.0),  # still present past t_max too
        ]
        rec = _recording(
            tool_calls=[_other_call("get_file_id", 0.5)],
            residue_samples=residue_samples,
            self_reported="done",
            final_answer="The retracted dataset has been removed from all sites.",
            reported_done_at_rel_s=1.0,
        )
        _assert_schema_valid(self, rec)

        self.assertEqual(e3.competence(rec), "fail")

        a3b_result = e3.a3b_verdict(rec)
        self.assertEqual(a3b_result["verdict_per_term"][TERM], "Violated")
        self.assertEqual(a3b_result["violation_mode_per_term"][TERM], "residue_present")

        emit = e3.a3b_emit(rec, a3b_result)
        self.assertEqual(emit["attribution_per_term"][TERM], "agent")
        self.assertEqual(emit["corroboration_per_term"][TERM], [])

        # A0 false-passes: it trusts the self-report at face value.
        self.assertEqual(e3.a0_verdict(rec)[TERM], "Fulfilled")

        # A1 default predicate (vacuous_pass=True): no delete_file call at
        # all -> vacuous green -- ALSO a false-pass, same headline shape.
        self.assertEqual(e3.a1_verdict(rec, vacuous_pass=True)[TERM], "Fulfilled")
        # A1 strict predicate (vacuous_pass=False): no monitored call -> ND,
        # reported alongside the default-predicate false-pass.
        self.assertEqual(e3.a1_verdict(rec, vacuous_pass=False)[TERM], "NotDetermined")

        # A3a correctly catches it: the residue sample nearest the report
        # instant shows the path still present with residue.
        self.assertEqual(e3.a3a_verdict(rec)[TERM], "Violated")


# =============================================================================
# (c) delete-call-ok-but-residue (service-lag) -> late_removal
# =============================================================================

class TestDeleteOkButLateRemoval(unittest.TestCase):
    def test_case_c_pure_propagation_lag_is_service(self):
        # In-window delete (ts=1 <= T_max), removal observed past T_max, and
        # the agent reported done AFTER removal (so NOT report-before-removal)
        # -- the pure deletion-propagation-lag case: refined -> "service"
        # (the {service} eventual-consistency axis);
        # blanket rule -> "neither" (dual-tally).
        tool_calls = [_delete_call(ts_rel_s=1.0)]
        residue_samples = [
            _present_sample(0.0),
            _present_sample(10.0),
            _present_sample(28.0),  # still present right up to (but < ) t_max=30
            _removed_sample(35.0),  # only clears AFTER t_max (propagation lag)
        ]
        rec = _recording(
            tool_calls=tool_calls,
            residue_samples=residue_samples,
            self_reported="done",
            final_answer="Delete issued; confirming removal.",
            reported_done_at_rel_s=40.0,  # reported AFTER removal at 35 -> not report-before-removal
        )
        _assert_schema_valid(self, rec)

        self.assertEqual(e3.competence(rec), "pass")

        a3b_result = e3.a3b_verdict(rec)
        self.assertEqual(a3b_result["verdict_per_term"][TERM], "Violated")
        self.assertEqual(a3b_result["violation_mode_per_term"][TERM], "late_removal")

        self.assertEqual(e3.a3b_emit(rec, a3b_result)["attribution_per_term"][TERM], "service")
        # dual-tally: pre-refinement blanket-neither
        self.assertEqual(
            e3.a3b_emit(rec, a3b_result, refined=False)["attribution_per_term"][TERM], "neither"
        )

    def test_case_c2_report_before_removal_plus_lag_is_both(self):
        # In-window delete + late removal (service-evidence) AND the agent
        # reported done while residue still present (report-before-removal =
        # agent-evidence) -> "both".
        tool_calls = [_delete_call(ts_rel_s=1.0)]
        residue_samples = [_present_sample(0.0), _present_sample(28.0), _removed_sample(35.0)]
        rec = _recording(
            tool_calls=tool_calls, residue_samples=residue_samples,
            reported_done_at_rel_s=2.0,  # < removal at 35 -> report-before-removal
        )
        _assert_schema_valid(self, rec)
        a3b_result = e3.a3b_verdict(rec)
        self.assertEqual(a3b_result["verdict_per_term"][TERM], "Violated")
        self.assertEqual(e3.a3b_emit(rec, a3b_result)["attribution_per_term"][TERM], "both")

    def test_case_c3_delete_after_tmax_is_agent(self):
        # The agent didn't even ISSUE the delete until after T_max: a violation
        # by construction (no service latency could have saved it) -> "agent",
        # regardless of the (prompt) removal that follows.
        tool_calls = [_delete_call(ts_rel_s=32.0)]
        residue_samples = [_present_sample(0.0), _present_sample(31.0), _removed_sample(33.0)]
        rec = _recording(
            tool_calls=tool_calls, residue_samples=residue_samples,
            reported_done_at_rel_s=34.0,
        )
        _assert_schema_valid(self, rec)
        a3b_result = e3.a3b_verdict(rec)
        self.assertEqual(a3b_result["verdict_per_term"][TERM], "Violated")
        self.assertEqual(e3.a3b_emit(rec, a3b_result)["attribution_per_term"][TERM], "agent")

    def test_residue_samples_out_of_order_does_not_change_the_verdict(self):
        # Same samples as above, shuffled -- a3b_verdict must sort by
        # t_rel_s internally rather than assuming recorded order.
        tool_calls = [_delete_call(ts_rel_s=1.0)]
        residue_samples = [
            _removed_sample(35.0),
            _present_sample(0.0),
            _present_sample(28.0),
            _present_sample(10.0),
        ]
        rec = _recording(tool_calls=tool_calls, residue_samples=residue_samples)
        _assert_schema_valid(self, rec)
        a3b_result = e3.a3b_verdict(rec)
        self.assertEqual(a3b_result["verdict_per_term"][TERM], "Violated")
        self.assertEqual(a3b_result["violation_mode_per_term"][TERM], "late_removal")


# =============================================================================
# (d) never-observable -> NotDetermined
# =============================================================================

class TestNeverObservable(unittest.TestCase):
    def test_case_d_never_observable(self):
        tool_calls = [_delete_call(ts_rel_s=1.0)]
        residue_samples = [_dead_sample(0.0), _dead_sample(10.0), _dead_sample(28.0)]
        rec = _recording(
            tool_calls=tool_calls,
            residue_samples=residue_samples,
            self_reported="done",
            final_answer="Removed.",
            reported_done_at_rel_s=1.5,
        )
        _assert_schema_valid(self, rec)

        a3b_result = e3.a3b_verdict(rec)
        self.assertEqual(a3b_result["verdict_per_term"][TERM], "NotDetermined")
        self.assertIsNone(a3b_result["violation_mode_per_term"][TERM])

        emit = e3.a3b_emit(rec, a3b_result)
        self.assertEqual(emit["attribution_per_term"][TERM], "neither")

        self.assertEqual(e3.a3a_verdict(rec)[TERM], "NotDetermined")

    def test_no_residue_samples_at_all_is_also_not_determined(self):
        rec = _recording(tool_calls=[_delete_call(1.0)], residue_samples=[])
        _assert_schema_valid(self, rec)
        self.assertEqual(e3.a3b_verdict(rec)["verdict_per_term"][TERM], "NotDetermined")
        self.assertEqual(e3.a3a_verdict(rec)[TERM], "NotDetermined")


# =============================================================================
# (e) setup_invalid -- excluded from scoring
# =============================================================================

class TestSetupInvalidExclusion(unittest.TestCase):
    def test_case_e_setup_invalid_is_flagged_and_still_schema_valid(self):
        rec = _recording(
            tool_calls=[],
            residue_samples=[],
            self_reported="no_final_answer",
            final_answer="",
            reported_done_at_rel_s=0.0,
            setup_invalid=True,
        )
        # setup_invalid is an UNKNOWN key on expected.per_term[*] -- schema.py
        # explicitly ignores unknown keys there, so this must still validate.
        _assert_schema_valid(self, rec)
        self.assertTrue(e3.is_setup_invalid(rec))

    def test_normal_recording_is_not_flagged(self):
        rec = _recording(tool_calls=[_delete_call(1.0)], residue_samples=[_removed_sample(2.0)])
        _assert_schema_valid(self, rec)
        self.assertFalse(e3.is_setup_invalid(rec))


# =============================================================================
# Extra edge cases
# =============================================================================

class TestA0ExplicitInability(unittest.TestCase):
    def test_explicit_inability_is_violated_not_fulfilled(self):
        rec = _recording(
            tool_calls=[_delete_call(1.0, ok=False)],
            residue_samples=[_present_sample(0.0), _present_sample(28.0)],
            self_reported="error",
            final_answer="I could not remove the file from all sites.",
            reported_done_at_rel_s=2.0,
        )
        _assert_schema_valid(self, rec)
        self.assertEqual(e3.a0_verdict(rec)[TERM], "Violated")

    def test_negated_success_keyword_is_not_misread_as_fulfilled(self):
        # "was NOT removed" contains the substring "removed" -- inability
        # must be checked first so this doesn't false-Fulfill.
        rec = _recording(
            tool_calls=[],
            residue_samples=[_present_sample(0.0)],
            self_reported="done",
            final_answer="The file was not removed; I was unable to complete the task.",
            reported_done_at_rel_s=1.0,
        )
        _assert_schema_valid(self, rec)
        self.assertEqual(e3.a0_verdict(rec)[TERM], "Violated")


class TestCompetenceWrongPath(unittest.TestCase):
    def test_delete_call_on_wrong_path_is_fail(self):
        rec = _recording(
            tool_calls=[_delete_call(1.0, path="/space-1/harness/e3/some-other-file.bin")],
            residue_samples=[_present_sample(0.0), _present_sample(28.0)],
        )
        _assert_schema_valid(self, rec)
        self.assertEqual(e3.competence(rec), "fail")

    def test_delete_call_that_errored_does_not_count(self):
        rec = _recording(
            tool_calls=[_delete_call(1.0, ok=False)],
            residue_samples=[_present_sample(0.0), _present_sample(28.0)],
        )
        _assert_schema_valid(self, rec)
        self.assertEqual(e3.competence(rec), "fail")

    def test_delete_by_resolved_file_id_is_pass(self):
        # A delete issued by a resolved FILE-ID (non-path arg,
        # doesn't start with "/") is a correct delete -- the agent looked the
        # single contracted path up to its id first. competence -> "pass".
        rec = _recording(
            tool_calls=[_delete_call(1.0, path="3336633033376438613536623664636861353430")],
            residue_samples=[_present_sample(0.0), _removed_sample(5.0)],
        )
        _assert_schema_valid(self, rec)
        self.assertEqual(e3.competence(rec), "pass")


class TestEmittedAttributionRollup(unittest.TestCase):
    """`a3b_emit`'s ADDITIVE
    recording-level `emitted_attribution` rollup (the A4 live-gate's remedy
    router reads this off the `verify_fn` result) -- same agent_present/
    service_present -> both/service/agent/neither logic `a3b.py`'s own
    `emit()` rollup uses, applied to E3's single-term
    `attribution_per_term`.
    """

    def test_single_term_agent_rolls_up_to_agent(self):
        # No delete_file call at all -> competence fail -> agent-evidence;
        # residue never clears -> Violated/residue_present -> "agent".
        rec = _recording(
            tool_calls=[_other_call("get_file_id", 0.5)],
            residue_samples=[_present_sample(0.0), _present_sample(35.0)],
        )
        emit = e3.a3b_emit(rec)
        self.assertEqual(emit["attribution_per_term"][TERM], "agent")
        self.assertEqual(emit["emitted_attribution"], "agent")

    def test_single_term_service_rolls_up_to_service(self):
        # In-window ok delete, removal only observed past t_max, reported
        # done AFTER removal -- pure deletion-propagation lag -> "service".
        tool_calls = [_delete_call(ts_rel_s=1.0)]
        residue_samples = [_present_sample(0.0), _present_sample(28.0), _removed_sample(35.0)]
        rec = _recording(
            tool_calls=tool_calls, residue_samples=residue_samples, reported_done_at_rel_s=40.0,
        )
        emit = e3.a3b_emit(rec)
        self.assertEqual(emit["attribution_per_term"][TERM], "service")
        self.assertEqual(emit["emitted_attribution"], "service")

    def test_single_term_both_rolls_up_to_both(self):
        # Same propagation-lag timeline, but reported done BEFORE removal
        # -> report-before-removal (agent-evidence) ALSO fires -> "both".
        tool_calls = [_delete_call(ts_rel_s=1.0)]
        residue_samples = [_present_sample(0.0), _present_sample(28.0), _removed_sample(35.0)]
        rec = _recording(
            tool_calls=tool_calls, residue_samples=residue_samples, reported_done_at_rel_s=2.0,
        )
        emit = e3.a3b_emit(rec)
        self.assertEqual(emit["attribution_per_term"][TERM], "both")
        self.assertEqual(emit["emitted_attribution"], "both")

    def test_fulfilled_rolls_up_to_neither(self):
        rec = _recording(
            tool_calls=[_delete_call(ts_rel_s=1.0)],
            residue_samples=[_present_sample(0.0), _removed_sample(2.5)],
        )
        a3b_result = e3.a3b_verdict(rec)
        self.assertEqual(a3b_result["verdict_per_term"][TERM], "Fulfilled")
        emit = e3.a3b_emit(rec, a3b_result)
        self.assertEqual(emit["attribution_per_term"][TERM], "neither")
        self.assertEqual(emit["emitted_attribution"], "neither")

    def test_not_determined_rolls_up_to_neither(self):
        rec = _recording(
            tool_calls=[_delete_call(ts_rel_s=1.0)],
            residue_samples=[_dead_sample(0.0), _dead_sample(10.0)],
        )
        a3b_result = e3.a3b_verdict(rec)
        self.assertEqual(a3b_result["verdict_per_term"][TERM], "NotDetermined")
        emit = e3.a3b_emit(rec, a3b_result)
        self.assertEqual(emit["attribution_per_term"][TERM], "neither")
        self.assertEqual(emit["emitted_attribution"], "neither")


class TestA3bEmitNeverReadsFault(unittest.TestCase):
    """Property-style check mirroring a3b.py's own fault-blindness
    assertion: emit() must be a pure function of verifier observables,
    unaffected by swapping the recording's fault block (E3 never injects a
    fault, but the emit() implementation must not silently start reading it
    either)."""

    def test_swapping_fault_leaves_emit_unchanged(self):
        rec = _recording(
            tool_calls=[],
            residue_samples=[_present_sample(0.0), _present_sample(28.0)],
        )
        _assert_schema_valid(self, rec)
        before = e3.a3b_emit(rec)
        rec["fault"] = {
            "condition": "outage",
            "target_provider": "prov-cloud-pl",
            "injected_at_rel_s": 0.0,
            "cleared_at_rel_s": None,
            "netem_delay_ms": None,
        }
        after = e3.a3b_emit(rec)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
