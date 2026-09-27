"""Regression tests for harness/lenses/{competence,a0,a1,a3a,a3b}.py +
harness/aggregate.py.

Stdlib `unittest` only, same import-shim convention as harness/test_schema.py
and harness/test_verifier.py. Run either as:

    python3 -m unittest harness.test_lenses -v      # from repo root
    python3 harness/test_lenses.py                  # directly as a script

Fully OFFLINE: every recording here is a hand-built synthetic dict (built by
the `_recording(...)` factory below) or one of two REAL early
smoke recordings shipped in `harness/testdata/inc2b_smoke/`.
No federation contact, no model calls, no network.

Covers:
    1. The headline seeded-false-pass pair:
       (a) converged-late-but-within-t_max -> A0/A1 Fulfilled, A3a
           false-FAILS (Violated), A3b Fulfilled (the A3a story).
       (b) never-converged -> A0/A1 Fulfilled (the FALSE-PASS headline),
           A3a Violated, A3b Violated. aggregate.py tallies (b) as an
           A0 & A1 false-pass.
    2. A3b emit reachability: {agent},
       {service}, {both}, {neither}, plus the two named off-diagonals
       (wild -> service; uncorroborated-both -> agent), plus a
       property-style assertion that emit() never reads `fault.*`.
    3. A0 edges: refused -> Violated; null reported_done -> NotDetermined.
       A3a null-report -> NotDetermined.
    4. A1 health: provider_health false in-window -> Violated; absent
       stream -> a1_health_available=False + call-returns-only.
    5. Both real early recordings run through all four lenses: no crash +
       schema-consistent shapes.

NOTE on schema.validate() and these fixtures: two classes of synthetic
recording below are DELIBERATELY not schema-valid under the CURRENT
harness/schema.py, for reasons documented inline at each site:
  - a null `agent.reported_done_at_rel_s` (schema.py currently requires
    this field to be numeric; the lenses are nonetheless required to
    handle the null-report path as a normal, meaningful case -- see A0/A3a
    edge-case tests below).
  - `provider_health` samples (schema.py's per-sample invariant requires a
    NUMERIC `value` when `probe_ok` is True; `provider_health`'s value is
    boolean). `provider_health` is a NEW
    field whose schema revision lands separately -- not in this one.
    Fixtures needing it are intentionally not
    run through schema.validate().
Every OTHER fixture below (the ones with a numeric report and no health
samples) IS run through schema.validate() to keep them honest.
"""
from __future__ import annotations

import copy
import json
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
        from harness import aggregate
        from harness.lenses import competence, a0, a1, a3a, a3b, a2
    except ImportError:
        import schema
        import aggregate
        from lenses import competence, a0, a1, a3a, a3b, a2
else:
    from . import schema, aggregate
    from .lenses import competence, a0, a1, a3a, a3b, a2


REAL_RECORDINGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testdata", "inc2b_smoke")
REAL_RECORDING_FILES = ("trial-36c2f19ac595.json", "trial-c61e08ffc7a7.json")

TERM = "T1"
T_MAX = 30.0
EXPECTED_SIZE = 4096
TARGET = "prov-de"          # single consistent identifier namespace for the
SOURCE = "prov-cloud-pl"    # synthetic fixtures (see module docstring re:
                             # the real-data label-vs-ID mismatch, not
                             # reproduced here on purpose).
FILE_PATH = "/space-1/harness/synthetic/trial.bin"


# --- fixture builders --------------------------------------------------------

def _tool_call(name, args, ts_rel_s, ok=True, result=None):
    return {
        "name": name,
        "args": args,
        "result": result if result is not None else {},
        "ts_rel_s": ts_rel_s,
        "ok": ok,
    }


def _replication_call(ts_rel_s, target=TARGET, file_path=FILE_PATH, ok=True):
    return _tool_call(
        "schedule_file_replication",
        {"file_id_or_path": file_path, "target_provider_id": target},
        ts_rel_s,
        ok=ok,
        result={"transferId": "xfer-1"},
    )


def _physical_sample(t_rel_s, value, provider=TARGET, term_id=TERM, probe_ok=True):
    return {
        "t_rel_s": t_rel_s,
        "term_id": term_id,
        "sli_name": "physicalSize",
        "value": value if probe_ok else None,
        "provider": provider,
        "probe_ok": probe_ok,
    }


def _health_sample(t_rel_s, value, provider=TARGET, term_id=TERM):
    return {
        "t_rel_s": t_rel_s,
        "term_id": term_id,
        "sli_name": "provider_health",
        "value": value,
        "provider": provider,
        "probe_ok": True,
    }


def _transfer_status_sample(t_rel_s, value, provider=TARGET, term_id=TERM):
    return {
        "t_rel_s": t_rel_s,
        "term_id": term_id,
        "sli_name": "get_transfer.replicationStatus",
        "value": value,
        "provider": provider,
        "probe_ok": True,
    }


def _ramp_samples(step_at, before=0, after=EXPECTED_SIZE, until=60.0, interval=2.0, provider=TARGET):
    """physicalSize samples every `interval`s from 0 to `until`, `before` up
    to (exclusive of) `step_at`, `after` from `step_at` onward — a clean
    step-function convergence at `t_rel_s == step_at`.
    """
    samples = []
    t = 0.0
    while t <= until + 1e-9:
        value = after if t >= step_at else before
        samples.append(_physical_sample(round(t, 3), value, provider=provider))
        t += interval
    return samples


def _flat_samples(value, until=60.0, interval=2.0, provider=TARGET, probe_ok=True):
    samples = []
    t = 0.0
    while t <= until + 1e-9:
        samples.append(_physical_sample(round(t, 3), value, provider=provider, probe_ok=probe_ok))
        t += interval
    return samples


def _recording(
    *,
    tool_calls,
    samples,
    self_reported="done",
    reported_done_at_rel_s=0.0,
    transfer_status_samples=None,
    health_samples=None,
    fault=None,
    term_id=TERM,
    t_max_s=T_MAX,
    expected_size=EXPECTED_SIZE,
    target_providers=None,
    poll_until_rel_s=60.0,
    leg_scaffold="sdk_loop",
    scenario_id="E1",
):
    target_providers = target_providers if target_providers is not None else [TARGET]
    all_samples = list(samples) + list(health_samples or [])
    return {
        "trial_id": "trial-synthetic",
        "sweep_id": "sweep-synthetic",
        "recorded_at": "2026-07-05T00:00:00Z",
        "harness_version": "test",
        "trial_start_utc": "2026-07-05T00:00:00Z",
        "scenario_id": scenario_id,
        "arm_live": "A0",
        "model_leg": "test-model",
        "trial_k": 1,
        "source_provider": SOURCE,
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
                }
            ],
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
            "tool_calls": tool_calls,
            "messages": [],
            "final_answer": "done",
            "self_reported": self_reported,
            "reported_done_at_rel_s": reported_done_at_rel_s,
            "raw_trace": "absent-by-design",
        },
        "state_timeline": {
            "poll_interval_s": 2.0,
            "poll_until_rel_s": poll_until_rel_s,
            "polled_past_t_max": poll_until_rel_s >= t_max_s,
            "samples": all_samples,
            "transfer_status_samples": transfer_status_samples or [],
        },
        "cost": {
            "verifier": {"probe_count": len(all_samples), "wall_latency_s": 0.0},
            "model": {"prompt_tokens": 0, "completion_tokens": 0, "estimation": "measured", "usd": 0.0},
            "retry": {"attempts": 0, "model_usd": 0.0, "verifier_probe_count": 0},
        },
        "otel": {"harness_trace_id": "trace-synthetic", "poll_interval_s": 2.0, "correlated": False},
        "leg_scaffold": leg_scaffold,
    }


def _assert_schema_valid(testcase, recording):
    """Run schema.validate() and fail the test with the raised message if it
    doesn't validate — used for every fixture EXCEPT the two documented
    exceptions (null report; provider_health samples), per the module
    docstring.
    """
    try:
        schema.validate(recording)
    except schema.SchemaError as e:  # pragma: no cover - failure path
        testcase.fail(f"fixture should be schema-valid but got: {e}")


# =============================================================================
# 1. The headline seeded-false-pass pair
# =============================================================================

class TestHeadlineDoneState(unittest.TestCase):
    def test_case_a_converged_late_but_within_t_max(self):
        """Agent reports "done" at t=2 (right after issuing the correct
        replication call at t=1); the target only reaches expected_size at
        t=20 (< T_max=30). A0/A1 Fulfilled (naive/intent-agnostic both miss
        the lateness); A3a false-FAILS (report-instant value is still 0);
        A3b correctly says Fulfilled (converged within T_max) -- "the A3a
        story" the task names.
        """
        tool_calls = [_replication_call(ts_rel_s=1.0)]
        samples = _ramp_samples(step_at=20.0, until=60.0)
        recording = _recording(
            tool_calls=tool_calls,
            samples=samples,
            self_reported="done",
            reported_done_at_rel_s=2.0,
        )
        _assert_schema_valid(self, recording)

        self.assertEqual(competence.agent_competence(recording), "pass")
        self.assertEqual(a0.verdict(recording)[TERM], "Fulfilled")
        self.assertEqual(a1.verdict(recording)[TERM], "Fulfilled")
        self.assertEqual(a3a.verdict(recording)[TERM], "Violated")  # the false-fail
        a3b_result = a3b.verdict(recording)
        self.assertEqual(a3b_result["verdict_per_term"][TERM], "Fulfilled")

    def test_case_b_never_converged_is_the_false_pass_headline(self):
        """Agent reports "done"; the correct replication call returns ok;
        target physicalSize stays 0 through T_max AND through the full
        poll window. A0/A1 both Fulfilled -- the headline false-pass this
        harness is designed to detect. A3a and A3b both correctly say Violated.
        """
        tool_calls = [_replication_call(ts_rel_s=1.0)]
        samples = _flat_samples(0, until=60.0)
        recording = _recording(
            tool_calls=tool_calls,
            samples=samples,
            self_reported="done",
            reported_done_at_rel_s=2.0,
        )
        _assert_schema_valid(self, recording)

        self.assertEqual(competence.agent_competence(recording), "pass")
        self.assertEqual(a0.verdict(recording)[TERM], "Fulfilled")
        self.assertEqual(a1.verdict(recording)[TERM], "Fulfilled")
        self.assertEqual(a3a.verdict(recording)[TERM], "Violated")
        a3b_result = a3b.verdict(recording)
        self.assertEqual(a3b_result["verdict_per_term"][TERM], "Violated")
        self.assertEqual(a3b_result["violation_mode_per_term"][TERM], "never_converged")

        # aggregate.py must tally this as an A0 AND A1 false-pass.
        cls_a0 = aggregate.compare_recording_arm(recording, "A0")
        cls_a1 = aggregate.compare_recording_arm(recording, "A1")
        self.assertEqual(cls_a0[TERM], "false_pass")
        self.assertEqual(cls_a1[TERM], "false_pass")

        agg = aggregate.aggregate([recording], arms=("A0", "A1", "A3a", "A3b"))
        cells_by_arm = {c["arm"]: c for c in agg["cells"]}
        self.assertEqual(cells_by_arm["A0"]["false_pass"], 1)
        self.assertEqual(cells_by_arm["A0"]["false_pass_rate"], 1.0)
        self.assertEqual(cells_by_arm["A1"]["false_pass"], 1)
        self.assertEqual(cells_by_arm["A1"]["false_pass_rate"], 1.0)
        self.assertEqual(cells_by_arm["A0"]["arm_class"], "prior_practice")
        self.assertEqual(cells_by_arm["A1"]["arm_class"], "prior_practice")
        # A3b agrees with ground-truth 100% by construction -- stated
        # explicitly, not hidden.
        self.assertEqual(cells_by_arm["A3b"]["agreement_rate"], 1.0)
        self.assertEqual(cells_by_arm["A3a"]["arm_class"], "contribution")


# =============================================================================
# 2. A3b emit reachability
# =============================================================================

class TestA3bEmitReachability(unittest.TestCase):
    def test_emits_agent_pure_agent_error_no_fault(self):
        """Wrong-target replication (competence fail); target never
        converges; no corroborating evidence anywhere (probes always ok,
        no health stream, no transfer-status signal recorded) -> {agent}.
        (No transfer_status_samples here
        -- a terminal "completed" status would correctly fire
        E-status-shortfall, since the target's physicalSize never reached
        expected_size; that scenario is covered separately below.)
        """
        tool_calls = [_replication_call(ts_rel_s=1.0, target="prov-WRONG")]
        samples = _flat_samples(0, until=60.0, probe_ok=True)
        recording = _recording(
            tool_calls=tool_calls,
            samples=samples,
            self_reported="done",
            reported_done_at_rel_s=2.0,
        )
        _assert_schema_valid(self, recording)

        self.assertEqual(competence.agent_competence(recording), "fail")
        result = a3b.emit(recording)
        self.assertEqual(result["attribution_per_term"][TERM], "agent")
        self.assertEqual(result["emitted_attribution"], "agent")
        self.assertIsNone(result["confidence_per_term"][TERM])
        self.assertEqual(result["corroboration_per_term"][TERM], [])

    def test_emits_service_competent_agent_with_corroboration(self):
        """Correct replication (competence pass); target never converges;
        E-probe fires (probe_ok=False samples on target in-window,
        simulating a service fault) -> {service}, confidence "high".
        """
        tool_calls = [_replication_call(ts_rel_s=1.0)]
        samples = [
            _physical_sample(0.0, 0),
            _physical_sample(4.0, None, probe_ok=False),
            _physical_sample(8.0, None, probe_ok=False),
            _physical_sample(20.0, 0),
            _physical_sample(40.0, 0),
        ]
        recording = _recording(
            tool_calls=tool_calls,
            samples=samples,
            self_reported="done",
            reported_done_at_rel_s=2.0,
        )
        _assert_schema_valid(self, recording)

        self.assertEqual(competence.agent_competence(recording), "pass")
        a3b_v = a3b.verdict(recording)
        self.assertEqual(a3b_v["verdict_per_term"][TERM], "Violated")
        result = a3b.emit(recording, verdict_result=a3b_v)
        self.assertEqual(result["attribution_per_term"][TERM], "service")
        self.assertEqual(result["emitted_attribution"], "service")
        self.assertEqual(result["confidence_per_term"][TERM], "high")
        self.assertIn("E-probe", result["corroboration_per_term"][TERM])

    def test_emits_both_agent_error_with_corroboration(self):
        """Reachability: wrong-target replication (competence
        fail) AND a concurrent, OBSERVABLE fault (E-health fires on the
        CONTRACTED target) -> {both}: an over-determination case,
        corroborated via health rather than filepath.
        """
        tool_calls = [_replication_call(ts_rel_s=1.0, target="prov-WRONG")]
        samples = _flat_samples(0, until=60.0, provider=TARGET)
        health_samples = [
            _health_sample(5.0, True),
            _health_sample(10.0, False),  # killed mid-window
            _health_sample(40.0, False),
        ]
        recording = _recording(
            tool_calls=tool_calls,
            samples=samples,
            self_reported="done",
            reported_done_at_rel_s=2.0,
            health_samples=health_samples,
        )
        # NOT schema-valid: provider_health samples carry a boolean `value`
        # with probe_ok=True, which schema.py's current per-sample
        # invariant (numeric value required when probe_ok is True) rejects
        # -- see module docstring.

        self.assertEqual(competence.agent_competence(recording), "fail")
        result = a3b.emit(recording)
        self.assertEqual(result["attribution_per_term"][TERM], "both")
        self.assertEqual(result["emitted_attribution"], "both")
        self.assertEqual(result["confidence_per_term"][TERM], "high")
        self.assertIn("E-health", result["corroboration_per_term"][TERM])

    def test_emits_neither_on_fulfilled(self):
        """Converges on time, competent agent -> {neither}, no
        data_quality_flag (competence pass on a Fulfilled term).
        """
        tool_calls = [_replication_call(ts_rel_s=1.0)]
        samples = _ramp_samples(step_at=10.0, until=60.0)
        recording = _recording(
            tool_calls=tool_calls,
            samples=samples,
            self_reported="done",
            reported_done_at_rel_s=2.0,
        )
        _assert_schema_valid(self, recording)

        result = a3b.emit(recording)
        self.assertEqual(result["attribution_per_term"][TERM], "neither")
        self.assertEqual(result["emitted_attribution"], "neither")
        self.assertFalse(result["data_quality_flag_per_term"][TERM])

    def test_data_quality_flag_on_fulfilled_with_competence_fail(self):
        """T2 tie-break, restated for the EMIT procedure:
        a Fulfilled term with agent_competence == fail still emits
        {neither} but raises data_quality_flag.
        """
        # No qualifying replication call at all (so competence = fail) yet
        # the target somehow converges anyway (pre-seeded / another actor) --
        # a data-quality flag case, not an attribution.
        samples = _ramp_samples(step_at=5.0, until=60.0)
        recording = _recording(
            tool_calls=[],
            samples=samples,
            self_reported="done",
            reported_done_at_rel_s=2.0,
        )
        _assert_schema_valid(self, recording)

        self.assertEqual(competence.agent_competence(recording), "fail")
        a3b_v = a3b.verdict(recording)
        self.assertEqual(a3b_v["verdict_per_term"][TERM], "Fulfilled")
        result = a3b.emit(recording, verdict_result=a3b_v)
        self.assertEqual(result["attribution_per_term"][TERM], "neither")
        self.assertTrue(result["data_quality_flag_per_term"][TERM])

    def test_named_off_diagonal_wild_emits_service_via_elimination(self):
        """Wild: competent agent, verdict Violated, NO corroborating class
        fires anywhere (no fault ever injected, no transfer-status signal
        recorded) -> STILL {service} via the `agent_competence==pass`
        elimination arm. Confidence "inferred" (not "high") since nothing
        corroborated it. (The sibling "wild + completed-with-0-bytes IS
        observed" case -- where E-status-shortfall fires and confidence
        becomes "high" -- is covered separately below.)
        """
        tool_calls = [_replication_call(ts_rel_s=1.0)]
        samples = _flat_samples(0, until=60.0, probe_ok=True)  # always probe_ok
        recording = _recording(
            tool_calls=tool_calls,
            samples=samples,
            self_reported="done",
            reported_done_at_rel_s=2.0,
        )
        _assert_schema_valid(self, recording)

        self.assertEqual(competence.agent_competence(recording), "pass")
        result = a3b.emit(recording)
        self.assertEqual(result["attribution_per_term"][TERM], "service")
        self.assertEqual(result["emitted_attribution"], "service")
        self.assertEqual(result["confidence_per_term"][TERM], "inferred")
        self.assertEqual(result["corroboration_per_term"][TERM], [])

    def test_wild_completed_with_zero_bytes_fires_e_status_shortfall(self):
        """The gap this class closes: a terminal
        transfer status (`"completed"`) with the target's physicalSize
        still short of expected_size is itself a corroborating signal, even
        though it is neither a mid-flight non-terminal stall (E-stuck) nor
        a probe/health failure (E-probe/E-health). Competent agent, Violated
        verdict -> E-status-shortfall fires and emit() recovers {service}
        with confidence "high"; without this class, a terminal-status-with-
        shortfall case fires NO corroborating class at all.
        """
        tool_calls = [_replication_call(ts_rel_s=1.0)]
        samples = _flat_samples(0, until=60.0, probe_ok=True)
        transfer_status = [_transfer_status_sample(35.0, "completed")]  # terminal
        recording = _recording(
            tool_calls=tool_calls,
            samples=samples,
            self_reported="done",
            reported_done_at_rel_s=2.0,
            transfer_status_samples=transfer_status,
        )
        _assert_schema_valid(self, recording)

        self.assertEqual(competence.agent_competence(recording), "pass")
        result = a3b.emit(recording)
        self.assertIn("E-status-shortfall", result["corroboration_per_term"][TERM])
        self.assertEqual(result["attribution_per_term"][TERM], "service")
        self.assertEqual(result["emitted_attribution"], "service")
        self.assertEqual(result["confidence_per_term"][TERM], "high")

    def test_named_off_diagonal_uncorroborated_both_emits_agent(self):
        """uncorroborated-both: agent error (wrong target) AND a fault
        that is marked as injected in `fault.*` but which leaves NO
        observable trace (probes stay ok, health stays up, no
        transfer-status signal recorded) -> emit() can only see the agent
        error -> {agent}. Also asserts the structural "emit() never reads
        fault.*" property: toggling `fault.condition` between an injected
        fault and "none"
        must not change the emitted result, since emit() is a pure function
        of verifier observables only.
        """
        tool_calls = [_replication_call(ts_rel_s=1.0, target="prov-WRONG")]
        samples = _flat_samples(0, until=60.0, probe_ok=True)
        recording_with_fault = _recording(
            tool_calls=tool_calls,
            samples=samples,
            self_reported="done",
            reported_done_at_rel_s=2.0,
            fault={
                "condition": "outage",
                "target_provider": TARGET,
                "injected_at_rel_s": 4.0,
                "cleared_at_rel_s": 45.0,
                "netem_delay_ms": None,
                # condition != "none" requires
                # inject-verification evidence in the fault block.
                "inject_verified": True,
                "qdisc_snapshot": "qdisc netem 8001: root refcnt 2 limit 1000 delay 40.0s\n",
            },
        )
        _assert_schema_valid(self, recording_with_fault)

        self.assertEqual(competence.agent_competence(recording_with_fault), "fail")
        result = a3b.emit(recording_with_fault)
        self.assertEqual(result["attribution_per_term"][TERM], "agent")
        self.assertEqual(result["emitted_attribution"], "agent")

        # Property: emit() must not read record["fault"] at all -- swap it
        # to "none" and confirm the output is byte-for-byte identical.
        recording_no_fault = copy.deepcopy(recording_with_fault)
        recording_no_fault["fault"] = {
            "condition": "none",
            "target_provider": None,
            "injected_at_rel_s": None,
            "cleared_at_rel_s": None,
            "netem_delay_ms": None,
        }
        result_no_fault = a3b.emit(recording_no_fault)
        self.assertEqual(result, result_no_fault)


# =============================================================================
# 3. A0 / A3a edges
# =============================================================================

class TestA0Edges(unittest.TestCase):
    def test_refused_is_violated(self):
        recording = _recording(
            tool_calls=[],
            samples=_flat_samples(0, until=10.0),
            self_reported="refused",
            reported_done_at_rel_s=3.0,
        )
        _assert_schema_valid(self, recording)
        self.assertEqual(a0.verdict(recording)[TERM], "Violated")

    def test_null_reported_done_is_not_determined_even_if_self_reported_done(self):
        # NOT schema-valid: schema.py currently requires
        # agent.reported_done_at_rel_s to be numeric; this
        # null-report path is a case lenses must nonetheless handle
        # (see module docstring).
        recording = _recording(
            tool_calls=[],
            samples=_flat_samples(0, until=10.0),
            self_reported="done",
            reported_done_at_rel_s=None,
        )
        self.assertEqual(a0.verdict(recording)[TERM], "NotDetermined")


class TestA3aEdges(unittest.TestCase):
    def test_null_report_is_not_determined(self):
        # NOT schema-valid, same reason as above.
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=_ramp_samples(step_at=5.0, until=60.0),
            self_reported="done",
            reported_done_at_rel_s=None,
        )
        self.assertEqual(a3a.verdict(recording)[TERM], "NotDetermined")


# =============================================================================
# 4. A1 health
# =============================================================================

class TestA1Health(unittest.TestCase):
    """Default (`vacuous_pass=True`, green-unless-alarm) cases live
    here first; the strict three-valued predicate
    (`vacuous_pass=False`) is exercised in the mirror class below —
    both predicates stay computable for the mandatory
    dual-tally.
    """

    def test_health_false_in_window_is_violated(self):
        tool_calls = [_replication_call(ts_rel_s=1.0, ok=True)]
        samples = _flat_samples(EXPECTED_SIZE, until=40.0)  # even if converged...
        health_samples = [
            _health_sample(2.0, True),
            _health_sample(10.0, False),  # killed mid-window -> A1 catches it
        ]
        recording = _recording(
            tool_calls=tool_calls,
            samples=samples,
            self_reported="done",
            reported_done_at_rel_s=2.0,
            health_samples=health_samples,
        )
        # NOT schema-valid (provider_health boolean values) -- see docstring.

        result = a1.verdict(recording)
        self.assertEqual(result[TERM], "Violated")
        self.assertTrue(result["a1_health_available"])
        # Same under the strict predicate -- an alarm is an alarm
        # regardless of which predicate reads it.
        self.assertEqual(a1.verdict(recording, vacuous_pass=False)[TERM], "Violated")

    def test_absent_health_stream_degrades_to_call_returns_only(self):
        tool_calls = [_replication_call(ts_rel_s=1.0, ok=True)]
        samples = _flat_samples(0, until=10.0)  # never converges -- but A1 doesn't look
        recording = _recording(
            tool_calls=tool_calls,
            samples=samples,
            self_reported="done",
            reported_done_at_rel_s=2.0,
        )
        _assert_schema_valid(self, recording)

        result = a1.verdict(recording)
        self.assertFalse(result["a1_health_available"])
        self.assertEqual(result[TERM], "Fulfilled")  # call ok, no health signal -> false-pass
        # Strict predicate agrees here too: health stream wholly
        # absent degrades to call-returns-only under BOTH predicates.
        self.assertEqual(a1.verdict(recording, vacuous_pass=False)[TERM], "Fulfilled")

    def test_health_stream_present_but_no_in_window_samples_is_fulfilled_not_nd(self):
        """Default-predicate-specific: the health SLI stream exists in the
        recording (so `a1_health_available=True`), but there happen to be
        no in-window samples for THIS term's target providers. Absence of
        samples is not an alarm -- green-unless-alarm reads Fulfilled, not
        NotDetermined (the case the task instructions call out explicitly:
        `all_health_true` is False only because `health_in_window` is
        empty, which must not trip the strict predicate's trailing-else ND
        under the default predicate).
        """
        tool_calls = [_replication_call(ts_rel_s=1.0, ok=True)]
        samples = _flat_samples(0, until=10.0)
        # A health sample exists in the stream (so health_available=True)
        # but for a different term_id -- so THIS term's in-window health
        # samples are empty.
        health_samples = [_health_sample(2.0, True, term_id="some-other-term")]
        recording = _recording(
            tool_calls=tool_calls,
            samples=samples,
            self_reported="done",
            reported_done_at_rel_s=2.0,
            health_samples=health_samples,
        )
        # NOT schema-valid (provider_health boolean values) -- see docstring.

        result = a1.verdict(recording)
        self.assertTrue(result["a1_health_available"])
        self.assertEqual(result[TERM], "Fulfilled")  # default: no alarm -> green
        # Strict predicate: health stream present-and-inconclusive
        # for this term -> conservative ND (unchanged old behavior).
        self.assertEqual(a1.verdict(recording, vacuous_pass=False)[TERM], "NotDetermined")

    def test_absent_health_stream_with_errored_call_is_violated(self):
        tool_calls = [_replication_call(ts_rel_s=1.0, ok=False)]
        recording = _recording(
            tool_calls=tool_calls,
            samples=_flat_samples(0, until=10.0),
            self_reported="refused",
            reported_done_at_rel_s=2.0,
        )
        _assert_schema_valid(self, recording)

        result = a1.verdict(recording)
        self.assertFalse(result["a1_health_available"])
        self.assertEqual(result[TERM], "Violated")
        self.assertEqual(a1.verdict(recording, vacuous_pass=False)[TERM], "Violated")

    def test_no_relevant_call_is_fulfilled_vacuously_under_amended_default(self):
        """The default predicate: A1 is two-valued
        green-unless-alarm -- no required call present is a VACUOUS pass
        (nothing ran, dashboard stays green), never NotDetermined, under
        the default. See test_no_relevant_call_is_not_determined_
        under_pre_registered_predicate below for the retained strict-predicate
        behavior (dual-tally).
        """
        recording = _recording(
            tool_calls=[],
            samples=_flat_samples(0, until=10.0),
            self_reported=None,
            reported_done_at_rel_s=2.0,
        )
        _assert_schema_valid(self, recording)

        result = a1.verdict(recording)
        self.assertEqual(result[TERM], "Fulfilled")

    def test_no_relevant_call_is_not_determined_under_pre_registered_predicate(self):
        """Strict predicate (`vacuous_pass=False`) -- unchanged
        from the original spec: no relevant call -> NotDetermined. Kept
        computable for the results doc's mandatory dual-tally alongside
        the default predicate's Fulfilled (see the sibling test above).
        """
        recording = _recording(
            tool_calls=[],
            samples=_flat_samples(0, until=10.0),
            self_reported=None,
            reported_done_at_rel_s=2.0,
        )
        _assert_schema_valid(self, recording)

        result = a1.verdict(recording, vacuous_pass=False)
        self.assertEqual(result[TERM], "NotDetermined")


class TestA1PredicateInvariant(unittest.TestCase):
    """The default (`vacuous_pass=True`) and strict
    (`vacuous_pass=False`) predicates must produce IDENTICAL per-term
    verdicts on EVERY recording EXCEPT the cells where the strict
    rule itself reads NotDetermined -- i.e. failed-call->Violated,
    health-false->Violated, and call-ok+health-green->Fulfilled are the
    SAME under both flags; the two modes diverge ONLY where the strict
    rule would have said NotDetermined (there: True->Fulfilled,
    False->NotDetermined). This proves the default predicate is a clean,
    LOCALIZED change -- the dual-tally difference is exactly, and only,
    the former-ND cells, nowhere else.
    """

    def _cases(self):
        """[(label, recording, divergent)] -- `divergent=False` cases must
        agree verdict-for-verdict under both predicates (and that shared
        verdict must not itself be NotDetermined); `divergent=True` cases
        must show the strict predicate reading NotDetermined and the
        default predicate reading Fulfilled exactly.
        """
        failed_call = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0, ok=False)],
            samples=_flat_samples(0, until=10.0),
            self_reported="refused",
            reported_done_at_rel_s=2.0,
        )
        health_false_in_window = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0, ok=True)],
            samples=_flat_samples(EXPECTED_SIZE, until=40.0),
            self_reported="done",
            reported_done_at_rel_s=2.0,
            health_samples=[_health_sample(2.0, True), _health_sample(10.0, False)],
        )
        call_ok_health_green = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0, ok=True)],
            samples=_flat_samples(EXPECTED_SIZE, until=40.0),
            self_reported="done",
            reported_done_at_rel_s=2.0,
            health_samples=[_health_sample(2.0, True), _health_sample(10.0, True)],
        )
        call_ok_no_health_stream = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0, ok=True)],
            samples=_flat_samples(0, until=10.0),
            self_reported="done",
            reported_done_at_rel_s=2.0,
        )
        no_relevant_call = _recording(
            tool_calls=[],
            samples=_flat_samples(0, until=10.0),
            self_reported=None,
            reported_done_at_rel_s=2.0,
        )
        health_present_but_inconclusive = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0, ok=True)],
            samples=_flat_samples(0, until=10.0),
            self_reported="done",
            reported_done_at_rel_s=2.0,
            health_samples=[_health_sample(2.0, True, term_id="some-other-term")],
        )
        return [
            ("failed_call", failed_call, False),
            ("health_false_in_window", health_false_in_window, False),
            ("call_ok_health_green", call_ok_health_green, False),
            ("call_ok_no_health_stream", call_ok_no_health_stream, False),
            ("no_relevant_call", no_relevant_call, True),
            ("health_present_but_inconclusive", health_present_but_inconclusive, True),
        ]

    def test_predicates_agree_everywhere_except_the_former_nd_cells(self):
        for label, recording, divergent in self._cases():
            with self.subTest(case=label):
                amended = a1.verdict(recording, vacuous_pass=True)[TERM]
                pre_reg = a1.verdict(recording, vacuous_pass=False)[TERM]
                if divergent:
                    self.assertEqual(
                        pre_reg, "NotDetermined",
                        f"{label}: expected this to be a former-ND cell under pre-reg",
                    )
                    self.assertEqual(
                        amended, "Fulfilled",
                        f"{label}: amended predicate must read vacuous-pass Fulfilled here",
                    )
                else:
                    self.assertNotEqual(
                        pre_reg, "NotDetermined",
                        f"{label}: non-divergent case must not itself be pre-reg ND",
                    )
                    self.assertEqual(
                        amended, pre_reg,
                        f"{label}: predicates must agree outside the former-ND cells",
                    )


# =============================================================================
# 5. Both real early recordings, all four lenses
# =============================================================================

class TestRealRecordings(unittest.TestCase):
    """No hard assertions on WHAT the lenses say for the real recordings
    beyond "runs without crashing" + "shape is schema-consistent". This
    surfaces the documented label-vs-ID
    mismatch (expected.per_term.target_providers is a human label like
    "de" while state_timeline.samples[].provider / tool_calls[].args.
    target_provider_id are raw provider-ID hashes) that this exposes on
    these two specific recordings.
    """

    @classmethod
    def setUpClass(cls):
        cls.recordings = {}
        for fname in REAL_RECORDING_FILES:
            path = os.path.join(REAL_RECORDINGS_DIR, fname)
            if not os.path.exists(path):
                cls.recordings = None
                return
            with open(path) as f:
                cls.recordings[fname] = json.load(f)

    def setUp(self):
        if self.recordings is None:
            self.skipTest(f"real recordings not found under {REAL_RECORDINGS_DIR}")

    def test_all_lenses_run_without_crashing_and_are_schema_consistent(self):
        for fname, recording in self.recordings.items():
            with self.subTest(recording=fname):
                # NB: these two on-disk smokes PREDATE the namespace fix (label
                # target_providers vs ID sample providers), so under the
                # cross-namespace guard they are NO LONGER
                # schema-valid — that rejection is asserted directly in
                # test_raw_label_namespace_recordings_rejected_by_guard. This test
                # only asserts the LENSES are robust to the raw shape (they run,
                # they don't crash, and the mismatched term honestly reads
                # NotDetermined) — no schema.validate() on the raw record.
                term_ids = {t["term_id"] for t in recording["contract"]["terms"]}

                comp = competence.agent_competence(recording)
                self.assertIn(comp, ("pass", "fail"))

                a0_result = a0.verdict(recording)
                self.assertEqual(set(a0_result.keys()), term_ids)
                for v in a0_result.values():
                    self.assertIn(v, ("Fulfilled", "Violated", "NotDetermined"))

                a1_result = a1.verdict(recording)
                self.assertEqual(set(a1_result.keys()) - {"a1_health_available"}, term_ids)
                self.assertIn(a1_result["a1_health_available"], (True, False))
                for tid in term_ids:
                    self.assertIn(a1_result[tid], ("Fulfilled", "Violated", "NotDetermined"))

                a3a_result = a3a.verdict(recording)
                self.assertEqual(set(a3a_result.keys()), term_ids)
                for v in a3a_result.values():
                    self.assertIn(v, ("Fulfilled", "Violated", "NotDetermined"))

                a3b_result = a3b.verdict(recording)
                self.assertEqual(set(a3b_result["verdict_per_term"].keys()), term_ids)
                for v in a3b_result["verdict_per_term"].values():
                    self.assertIn(v, ("Fulfilled", "Violated", "NotDetermined"))

                emit_result = a3b.emit(recording, verdict_result=a3b_result)
                self.assertIn(
                    emit_result["emitted_attribution"], ("agent", "service", "both", "neither")
                )

    def test_raw_label_namespace_recordings_rejected_by_guard(self):
        """Cross-namespace guard on REAL data: both on-disk smokes
        carry the label-vs-ID namespace bug, so schema.validate() must
        reject them — proving the guard catches the exact bug class these
        recordings exhibit."""
        for fname, recording in self.recordings.items():
            with self.subTest(recording=fname):
                with self.assertRaises(schema.SchemaError) as cm:
                    schema.validate(recording)
                self.assertIn("cross-namespace", str(cm.exception))

    def test_sdk_leg_a3a_runs_and_is_recorded(self):
        """The sdk_loop leg (trial-36c2f19ac595) self-reports at
        reported_done_at_rel_s=28.788s; the real convergence (per the raw
        physicalSize values, string-provider mismatch aside) happens at
        ~22.775s -- i.e. the report trails convergence by ~6s in wall
        time. Just assert A3a runs and record what it says
        (no specific value asserted -- the provider-identity mismatch
        documented in the module docstring means A3a's `converged_
        sample` lookup can't actually match "de" against the raw provider
        ID hash in this recording, so it evaluates to NotDetermined here;
        this is reported honestly rather than asserted around).
        """
        recording = self.recordings["trial-36c2f19ac595.json"]
        result = a3a.verdict(recording)
        self.assertIn(result["T1"], ("Fulfilled", "Violated", "NotDetermined"))
        # Printed for visibility (see test runner -v output):
        print(f"\n[real sdk leg] A3a verdict for T1: {result['T1']!r}")

    def test_real_recording_with_id_namespace_evaluates_correctly(self):
        """Regression guard for the runner namespace handling: once
        expected.per_term.target_providers carries the provider-ID namespace
        (matching state_timeline.samples[].
        provider), a real converged recording must evaluate correctly. The two
        on-disk smoke recordings predate this (label namespace), so this test
        simulates the corrected shape by swapping the label for the sample's ID.
        The sdk recording converged at t=22.775s < t_max=30 → all arms Fulfilled
        (the A0-conservative-lag case: reported_done=28.788s > convergence).
        """
        recording = json.loads(json.dumps(self.recordings["trial-36c2f19ac595.json"]))
        sample_pid = sorted({s["provider"] for s in recording["state_timeline"]["samples"]})[0]
        recording["expected"]["per_term"]["T1"]["target_providers"] = [sample_pid]
        recording["expected"]["per_term"]["T1"]["target_provider_labels"] = ["de"]
        # with the namespace repaired, the record now PASSES the guard too
        schema.validate(recording)
        self.assertEqual(competence.agent_competence(recording), "pass")
        self.assertEqual(a3b.verdict(recording)["verdict_per_term"]["T1"], "Fulfilled")
        # emit on a Fulfilled term attributes nothing
        self.assertEqual(a3b.emit(recording)["emitted_attribution"], "neither")


# =============================================================================
# 6. A2 — LLM-as-judge
# =============================================================================

class TestA2Judge(unittest.TestCase):
    """A2 render + parse + verdict with an INJECTED mock judge (no live call).
    The key test is the information-boundary leak canary."""

    def _recording(self):
        # a minimal E1 recording with distinctive WITHHELD values so the leak
        # canary can assert none of them reach the judge prompt.
        return {
            "contract": {"terms": [{
                "term_id": "T1",
                "guarantee": "data physically present on >=2 distinct providers",
                "slo": {"predicate": "physicalSize == expected_size",
                        "min_distinct_providers": 2,
                        "target_providers": ["RESOLVED_ORACLE_ID_zzz"]},
                "t_max_s": 30.0,
            }]},
            "expected": {"per_term": {"T1": {"expected_size": 987654321,
                                             "target_providers": ["RESOLVED_ORACLE_ID_zzz"]}}},
            "fault": {"condition": "outage", "target_provider": "FAULT_SECRET_ppp",
                      "injected_at_rel_s": 5.0},
            "state_timeline": {"samples": [
                {"t_rel_s": 5.0, "term_id": "T1", "sli_name": "physicalSize",
                 "value": 424242, "provider": "RESOLVED_ORACLE_ID_zzz", "probe_ok": True},
            ]},
            "agent": {
                "prompt_verbatim": "Replicate the file to our SK site and confirm.",
                "tool_calls": [
                    {"name": "schedule_file_replication", "args": {"target": "de"},
                     "result": '{"transferId":"abc"}', "ts_rel_s": 2.0, "ok": True},
                ],
                "messages": ["I'll trigger the replication now."],
                "final_answer": "Done — replication triggered and confirmed.",
                "self_reported": "done",
                "reported_done_at_rel_s": 3.0,
            },
        }

    def test_render_prompt_does_not_leak_withheld_values(self):
        """THE canary: resolved oracle id, concrete expected_size,
        state_timeline values, and fault.* must NOT appear in the judge prompt.
        The symbolic SLO + guarantee + t_max + trace MUST appear."""
        system, user = a2.render_prompt(self._recording())
        blob = system + "\n" + user
        # withheld — none may leak
        self.assertNotIn("RESOLVED_ORACLE_ID_zzz", blob)  # resolved target ids
        self.assertNotIn("987654321", blob)               # concrete expected_size
        self.assertNotIn("424242", blob)                  # state_timeline value
        self.assertNotIn("FAULT_SECRET_ppp", blob)        # fault target
        self.assertNotIn("outage", blob)                  # fault condition
        # spec + trace — must be present
        self.assertIn("physicalSize == expected_size", blob)  # SYMBOLIC slo
        self.assertIn(">= 2 distinct providers", blob)        # symbolic threshold
        self.assertIn("30.0", blob)                            # t_max
        self.assertIn("schedule_file_replication", blob)       # trace
        self.assertIn("Done — replication triggered", blob)    # answer

    def test_verdict_uses_injected_judge_and_parses(self):
        rec = self._recording()
        judge = lambda s, u: '{"verdicts": {"T1": "Violated"}, "rationale": "never confirmed"}'
        result = a2.verdict(rec, judge=judge)
        self.assertEqual(result["T1"], "Violated")
        self.assertFalse(result["a2_parse_error"])

    def test_capture_records_raw_response(self):
        rec = self._recording()
        raw = '{"verdicts": {"T1": "Fulfilled"}, "rationale": "ok"}'
        cap = {}
        a2.verdict(rec, judge=lambda s, u: raw, _capture=cap)
        self.assertEqual(cap["raw_response"], raw)
        self.assertEqual(cap["model_id"], "claude-sonnet-5")
        self.assertIn("physicalSize == expected_size", cap["user_prompt"])

    def test_parse_fail_safe_on_garbage(self):
        verdicts, err = a2.parse_response("not json at all", ["T1"])
        self.assertEqual(verdicts["T1"], "NotDetermined")
        self.assertTrue(err)

    def test_parse_fail_safe_on_missing_term(self):
        verdicts, err = a2.parse_response('{"verdicts": {"T2": "Fulfilled"}}', ["T1", "T2"])
        self.assertEqual(verdicts["T1"], "NotDetermined")  # missing → ND + flag
        self.assertEqual(verdicts["T2"], "Fulfilled")
        self.assertTrue(err)

    def test_parse_fail_safe_on_out_of_vocab(self):
        verdicts, err = a2.parse_response('{"verdicts": {"T1": "Maybe"}}', ["T1"])
        self.assertEqual(verdicts["T1"], "NotDetermined")
        self.assertTrue(err)

    def test_parse_clean_multi_term(self):
        verdicts, err = a2.parse_response(
            '{"verdicts": {"T1": "Fulfilled", "T2": "Violated"}}', ["T1", "T2"]
        )
        self.assertEqual(verdicts, {"T1": "Fulfilled", "T2": "Violated"})
        self.assertFalse(err)

    def test_extracts_json_embedded_in_prose(self):
        raw = 'Here is my assessment:\n{"verdicts": {"T1": "Violated"}, "rationale": "x"}\nThanks.'
        verdicts, err = a2.parse_response(raw, ["T1"])
        self.assertEqual(verdicts["T1"], "Violated")
        self.assertFalse(err)

    def test_default_judge_raises_not_fabricates(self):
        # a missing live judge must NEVER silently return a fabricated verdict
        with self.assertRaises(NotImplementedError):
            a2.default_judge("sys", "user")


# =============================================================================
# 7. aggregate.py A2 arm — reads the RECORDED a2_result, never recomputes
# =============================================================================

class TestAggregateA2(unittest.TestCase):
    """A2 is opt-in (not in DEFAULT_ARMS, needs the recorded a2_result field)
    and its per-term verdict must come from the persisted field, never from a
    live re-judge call. Also covers the ND-own-column aggregate() adds."""

    def _false_pass_recording(self):
        # Ground truth is Violated (never converges -- same shape as
        # TestHeadlineDoneState's case b), but the RECORDED A2 judge verdict
        # says Fulfilled: an A2 false-pass, same headline story A0/A1 have.
        tool_calls = [_replication_call(ts_rel_s=1.0)]
        samples = _flat_samples(0, until=60.0)
        rec = _recording(
            tool_calls=tool_calls,
            samples=samples,
            self_reported="done",
            reported_done_at_rel_s=2.0,
        )
        rec["a2_result"] = {
            "verdict_per_term": {TERM: "Fulfilled"},
            "raw_response": '{"verdicts": {"T1": "Fulfilled"}, "rationale": "looks done"}',
            "model_id": "claude-sonnet-5",
            "parse_error": False,
        }
        _assert_schema_valid(self, rec)
        return rec

    def _not_determined_recording(self):
        tool_calls = [_replication_call(ts_rel_s=1.0)]
        samples = _flat_samples(0, until=60.0)
        rec = _recording(
            tool_calls=tool_calls,
            samples=samples,
            self_reported="done",
            reported_done_at_rel_s=2.0,
        )
        rec["a2_result"] = {
            "verdict_per_term": {TERM: "NotDetermined"},
            "raw_response": "not valid json at all",
            "model_id": "claude-sonnet-5",
            "parse_error": True,
        }
        _assert_schema_valid(self, rec)
        return rec

    def test_a2_verdict_read_from_recorded_field_not_recomputed(self):
        rec = self._false_pass_recording()
        # If compare_recording_arm recomputed A2 via a2.verdict() (which needs
        # an injected `judge` callable), it would hit a2.default_judge() and
        # raise NotImplementedError. It must not raise here -- proving the
        # arm reads the persisted a2_result instead.
        result = aggregate.compare_recording_arm(rec, "A2")
        self.assertEqual(result[TERM], "false_pass")
        self.assertEqual(aggregate.a2_recorded_verdict(rec), {TERM: "Fulfilled"})

    def test_a2_recorded_verdict_absent_field_returns_empty(self):
        rec = self._false_pass_recording()
        del rec["a2_result"]
        self.assertEqual(aggregate.a2_recorded_verdict(rec), {})

    def test_a2_not_in_default_arms_but_registered(self):
        self.assertNotIn("A2", aggregate.DEFAULT_ARMS)
        self.assertIn("A2", aggregate.ARM_VERDICT_FNS)
        self.assertEqual(aggregate.ARM_CLASS["A2"], "prior_practice")

    def test_a2_false_pass_and_not_determined_column(self):
        recordings = [self._false_pass_recording(), self._not_determined_recording()]
        agg = aggregate.aggregate(recordings, arms=("A2",))
        cells_by_arm = {c["arm"]: c for c in agg["cells"]}
        cell = cells_by_arm["A2"]

        self.assertEqual(cell["arm_class"], "prior_practice")
        self.assertEqual(cell["n_terms"], 2)
        self.assertEqual(cell["false_pass"], 1)
        self.assertEqual(cell["false_pass_rate"], 0.5)
        self.assertEqual(cell["not_determined"], 1)
        self.assertEqual(cell["not_determined_rate"], 0.5)
        # NotDetermined vs. ground-truth Violated is neither false_pass,
        # false_fail, nor agreement -> other_mismatch. not_determined is a
        # SEPARATE, overlapping count, not a replacement for that bucket.
        self.assertEqual(cell["other_mismatch"], 1)


# =============================================================================
# 8. Verification of a3b (ground-truth lens) at its edges.
#
# ADDITIVE ONLY: documents harness/lenses/a3b.py's CURRENT behavior at the
# edges across 8 categories (info-boundary, T_max boundary,
# multi-term rollup, converged_sample exactness, the four corroboration
# classes, service-evidence/confidence logic, degenerate inputs, the known
# namespace-mismatch behavior). a3b.py itself is NOT modified by this
# section.
# =============================================================================

# --- multi-term test infra ---------------------------------------------------
# The single-term `_recording()` factory above only supports one contract
# term (its `term_id=` kwarg is singular and `_flat_samples`/`_ramp_samples`
# hardcode the module-level TERM constant on every sample). The item-3
# cross-term independence + rollup checks need >1 term with INDEPENDENT
# per-term samples, so these helpers build samples/recordings with an
# explicit `term_id` per sample/term instead of reusing the single-term
# helpers (which would silently mis-tag every sample as the same term).

def _term_flat_samples(term_id, value, until=60.0, interval=2.0, provider=TARGET, probe_ok=True):
    samples = []
    t = 0.0
    while t <= until + 1e-9:
        samples.append({
            "t_rel_s": round(t, 3),
            "term_id": term_id,
            "sli_name": "physicalSize",
            "value": value if probe_ok else None,
            "provider": provider,
            "probe_ok": probe_ok,
        })
        t += interval
    return samples


def _term_ramp_samples(term_id, step_at, before=0, after=EXPECTED_SIZE, until=60.0, interval=2.0, provider=TARGET):
    samples = []
    t = 0.0
    while t <= until + 1e-9:
        value = after if t >= step_at else before
        samples.append({
            "t_rel_s": round(t, 3),
            "term_id": term_id,
            "sli_name": "physicalSize",
            "value": value,
            "provider": provider,
            "probe_ok": True,
        })
        t += interval
    return samples


def _term_health_sample(t_rel_s, value, term_id, provider):
    return {
        "t_rel_s": t_rel_s,
        "term_id": term_id,
        "sli_name": "provider_health",
        "value": value,
        "provider": provider,
        "probe_ok": True,
    }


def _multi_term_recording(term_specs, tool_calls, reported_done_at_rel_s=2.0, self_reported="done"):
    """Build a >1-contract-term synthetic recording. `term_specs` is a list of
    dicts: {term_id, t_max_s, expected_size, target_providers, samples,
    health_samples? (optional), transfer_status_samples? (optional)}.
    Mirrors `_recording()`'s overall schema shape (same top-level field set)
    but generalizes `contract.terms` / `expected.per_term` /
    `state_timeline.samples` to N independently-specified terms.
    """
    terms, per_term, all_samples, all_transfer_status = [], {}, [], []
    for spec in term_specs:
        tid = spec["term_id"]
        terms.append({
            "term_id": tid,
            "guarantee": "data physically present on >=2 distinct providers",
            "slis": ["get_file_distribution.physicalSize"],
            "slo": {
                "predicate": "physicalSize == expected_size",
                "min_distinct_providers": 2,
                "target_providers": spec["target_providers"],
            },
            "t_max_s": spec["t_max_s"],
            "remedy": "re-issue / escalate / abstain (A4)",
        })
        per_term[tid] = {
            "expected_size": spec["expected_size"],
            "target_providers": spec["target_providers"],
            "expected_paths": [FILE_PATH],
        }
        all_samples.extend(spec.get("samples", []))
        all_samples.extend(spec.get("health_samples", []))
        all_transfer_status.extend(spec.get("transfer_status_samples", []))
    poll_until = max(spec["t_max_s"] for spec in term_specs) + 30.0
    return {
        "trial_id": "trial-synthetic-multiterm",
        "sweep_id": "sweep-synthetic",
        "recorded_at": "2026-07-05T00:00:00Z",
        "harness_version": "test",
        "trial_start_utc": "2026-07-05T00:00:00Z",
        "scenario_id": "E2",
        "arm_live": "A0",
        "model_leg": "test-model",
        "trial_k": 1,
        "source_provider": SOURCE,
        "agent_mcp_host": "https://example.invalid",
        "fault": {
            "condition": "none",
            "target_provider": None,
            "injected_at_rel_s": None,
            "cleared_at_rel_s": None,
            "netem_delay_ms": None,
        },
        "contract": {"obligated_party": "agent", "terms": terms},
        "expected": {
            "fixture_space_id": "space-1",
            "poll_until_rel_s": poll_until,
            "per_term": per_term,
        },
        "agent": {
            "prompt_verbatim": "synthetic multi-term prompt",
            "tool_calls": tool_calls,
            "messages": [],
            "final_answer": "done",
            "self_reported": self_reported,
            "reported_done_at_rel_s": reported_done_at_rel_s,
            "raw_trace": "absent-by-design",
        },
        "state_timeline": {
            "poll_interval_s": 2.0,
            "poll_until_rel_s": poll_until,
            "polled_past_t_max": True,
            "samples": all_samples,
            "transfer_status_samples": all_transfer_status,
        },
        "cost": {
            "verifier": {"probe_count": len(all_samples), "wall_latency_s": 0.0},
            "model": {"prompt_tokens": 0, "completion_tokens": 0, "estimation": "measured", "usd": 0.0},
            "retry": {"attempts": 0, "model_usd": 0.0, "verifier_probe_count": 0},
        },
        "otel": {"harness_trace_id": "trace-synthetic", "poll_interval_s": 2.0, "correlated": False},
        "leg_scaffold": "sdk_loop",
    }


# --- 8.1 Info-boundary (item 1) ------------------------------------

class TestA3bInfoBoundaryAdversarial(unittest.TestCase):
    """`emit()` (and `verdict()`, which is
    likewise fault-blind by construction -- it never even references
    `record["fault"]`) MUST be a pure function of verifier observables only.
    The existing `test_named_off_diagonal_uncorroborated_both_emits_agent`
    property-checks ONE fault swap (fault-on-target -> none). This
    adversarially mutates EVERY `fault.*` subfield independently -- including
    a `target_provider` that MATCHES a contracted sample provider, which the
    existing swap-test doesn't exercise (its swap target was chosen to be the
    WRONG provider) -- plus outright deletion of the `fault` key, and asserts
    `verdict()`/`emit()` are byte-identical across all of them.
    """

    def test_emit_and_verdict_ignore_all_fault_subfield_mutations(self):
        tool_calls = [_replication_call(ts_rel_s=1.0, target="prov-WRONG")]
        samples = _flat_samples(0, until=60.0, provider=TARGET)
        base = _recording(
            tool_calls=tool_calls,
            samples=samples,
            self_reported="done",
            reported_done_at_rel_s=2.0,
            fault={
                "condition": "none",
                "target_provider": None,
                "injected_at_rel_s": None,
                "cleared_at_rel_s": None,
                "netem_delay_ms": None,
            },
        )
        _assert_schema_valid(self, base)
        baseline_verdict = a3b.verdict(base)
        baseline_emit = a3b.emit(base)

        fault_variants = [
            # condition + target_provider MATCHING the contracted target --
            # the case the existing swap-test's WRONG-target fault doesn't
            # cover.
            {
                "condition": "netem",
                "target_provider": TARGET,
                "injected_at_rel_s": 0.5,
                "cleared_at_rel_s": 50.0,
                "netem_delay_ms": 5000,
            },
            # condition + a different (off-path) target.
            {
                "condition": "outage",
                "target_provider": "prov-elsewhere",
                "injected_at_rel_s": 4.0,
                "cleared_at_rel_s": 45.0,
                "netem_delay_ms": None,
            },
            # only injected_at_rel_s / cleared_at_rel_s change (near the
            # convergence-window edges), same condition+target as variant 0.
            {
                "condition": "netem",
                "target_provider": TARGET,
                "injected_at_rel_s": 59.9,
                "cleared_at_rel_s": 59.99,
                "netem_delay_ms": 100,
            },
            # only netem_delay_ms varies.
            {
                "condition": "netem",
                "target_provider": TARGET,
                "injected_at_rel_s": 0.5,
                "cleared_at_rel_s": 50.0,
                "netem_delay_ms": 999999,
            },
        ]
        for i, fault in enumerate(fault_variants):
            with self.subTest(variant=i, fault=fault):
                mutated = copy.deepcopy(base)
                mutated["fault"] = fault
                self.assertEqual(a3b.verdict(mutated), baseline_verdict)
                self.assertEqual(a3b.emit(mutated), baseline_emit)

        # Deletion: the `fault` key entirely absent from the recording dict.
        mutated_missing = copy.deepcopy(base)
        del mutated_missing["fault"]
        self.assertEqual(a3b.verdict(mutated_missing), baseline_verdict)
        self.assertEqual(a3b.emit(mutated_missing), baseline_emit)


# --- 8.2 T_max boundary (item 2) -----------------------------------

class TestA3bTmaxBoundary(unittest.TestCase):
    """`Fulfilled iff
    converged_sample(tid) exists at t_rel_s <= t_max_s` -- the `<=` boundary,
    not `<`. `Violated` + `violation_mode=late_convergence` iff a converged
    sample exists strictly after `t_max_s`. `NotDetermined` iff no `probe_ok`
    sample anywhere in `[0, t_max]`, REGARDLESS of what a later, out-of-window
    sample shows (epistemic-only)."""

    def test_fulfilled_at_exact_t_max_boundary(self):
        samples = [_physical_sample(0.0, 0), _physical_sample(T_MAX, EXPECTED_SIZE)]
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=samples,
            reported_done_at_rel_s=2.0,
        )
        _assert_schema_valid(self, recording)
        result = a3b.verdict(recording)
        self.assertEqual(result["verdict_per_term"][TERM], "Fulfilled")
        self.assertIsNone(result["violation_mode_per_term"][TERM])

    def test_violated_late_convergence_just_after_t_max(self):
        samples = [_physical_sample(0.0, 0), _physical_sample(T_MAX + 0.0001, EXPECTED_SIZE)]
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=samples,
            reported_done_at_rel_s=2.0,
        )
        _assert_schema_valid(self, recording)
        result = a3b.verdict(recording)
        self.assertEqual(result["verdict_per_term"][TERM], "Violated")
        self.assertEqual(result["violation_mode_per_term"][TERM], "late_convergence")

    def test_not_determined_when_unobservable_in_window_despite_later_convergence(self):
        """No probe_ok sample anywhere in [0, t_max] (the only in-window
        sample failed the probe); a LATER, out-of-window sample converges.
        Per spec this is NotDetermined -- epistemic-only -- not Violated,
        even though a converged_sample technically exists (just outside the
        window the Violated/NotDetermined boundary reads)."""
        samples = [
            {"t_rel_s": 5.0, "term_id": TERM, "sli_name": "physicalSize",
             "value": None, "provider": TARGET, "probe_ok": False},
            _physical_sample(50.0, EXPECTED_SIZE),
        ]
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=samples,
            reported_done_at_rel_s=2.0,
            poll_until_rel_s=60.0,
        )
        # NOT schema-run here: not needed to demonstrate the boundary, and
        # avoids coupling this test to unrelated schema invariants.
        result = a3b.verdict(recording)
        self.assertEqual(result["verdict_per_term"][TERM], "NotDetermined")
        # The converged sample DOES exist -- just outside the window that
        # decides observability; converged_sample() itself has no time bound,
        # confirming the NotDetermined comes from the
        # window-observability check, not from converged_sample failing.
        self.assertIsNotNone(a3b.converged_sample(recording, TERM))


# --- 8.3 Multi-term rollup (item 3) --------------------------------

class TestA3bMultiTermRollup(unittest.TestCase):
    """Per-term -> recording rollup:
    `verdict_per_term` must be independent per term, and the recording-level
    `emitted_attribution` rollup must follow `agent_present`/`service_present`
    precedence (both > service > agent > neither)."""

    def test_verdict_per_term_independent_across_fulfilled_violated_notdetermined(self):
        pa, pb, pc = "prov-a", "prov-b", "prov-c"
        term_specs = [
            {"term_id": "T1", "t_max_s": T_MAX, "expected_size": EXPECTED_SIZE, "target_providers": [pa],
             "samples": _term_ramp_samples("T1", step_at=5.0, until=60.0, provider=pa)},
            {"term_id": "T2", "t_max_s": T_MAX, "expected_size": EXPECTED_SIZE, "target_providers": [pb],
             "samples": _term_flat_samples("T2", 0, until=60.0, provider=pb)},
            {"term_id": "T3", "t_max_s": T_MAX, "expected_size": EXPECTED_SIZE, "target_providers": [pc],
             "samples": []},
        ]
        tool_calls = [_replication_call(ts_rel_s=1.0, target=pa)]
        recording = _multi_term_recording(term_specs, tool_calls)
        schema.validate(recording)

        result = a3b.verdict(recording)
        self.assertEqual(
            result["verdict_per_term"],
            {"T1": "Fulfilled", "T2": "Violated", "T3": "NotDetermined"},
        )
        self.assertEqual(result["violation_mode_per_term"]["T2"], "never_converged")

        # agent_competence is scoped to the FIRST term only (competence.py's
        # documented E1-Form-B scope); T1's correct call makes it "pass".
        self.assertEqual(competence.agent_competence(recording), "pass")

        emit_result = a3b.emit(recording, verdict_result=result)
        self.assertEqual(
            emit_result["attribution_per_term"],
            {"T1": "neither", "T2": "service", "T3": "neither"},
        )
        self.assertEqual(emit_result["emitted_attribution"], "service")

    def test_emitted_attribution_rollup_mixes_agent_and_both_into_both(self):
        """A rollup case Patterns A/B alone don't exercise: one Violated term
        attributes {agent} (no corroboration), a SECOND Violated term
        attributes {both} (agent_competence=fail is a recording-level
        constant shared by both terms, but corroboration is per-term) ->
        the recording-level rollup must read {both} (agent_present from
        BOTH terms, service_present from the second)."""
        pa, pb = "prov-a", "prov-b"
        term_specs = [
            {"term_id": "T1", "t_max_s": T_MAX, "expected_size": EXPECTED_SIZE, "target_providers": [pa],
             "samples": _term_flat_samples("T1", 0, until=60.0, provider=pa)},
            {"term_id": "T2", "t_max_s": T_MAX, "expected_size": EXPECTED_SIZE, "target_providers": [pb],
             "samples": _term_flat_samples("T2", 0, until=60.0, provider=pb),
             "health_samples": [_term_health_sample(10.0, False, "T2", pb)]},
        ]
        tool_calls = [_replication_call(ts_rel_s=1.0, target="prov-WRONG")]  # competence fail, both terms
        recording = _multi_term_recording(term_specs, tool_calls)
        schema.validate(recording)  # provider_health is a supported bool SLI (schema.py SLI_VALUE_TYPES)

        self.assertEqual(competence.agent_competence(recording), "fail")
        result = a3b.verdict(recording)
        self.assertEqual(result["verdict_per_term"], {"T1": "Violated", "T2": "Violated"})

        emit_result = a3b.emit(recording, verdict_result=result)
        self.assertEqual(emit_result["attribution_per_term"]["T1"], "agent")
        self.assertEqual(emit_result["attribution_per_term"]["T2"], "both")
        self.assertEqual(emit_result["emitted_attribution"], "both")


# --- 8.4 converged_sample exactness (item 4) -----------------------

class TestConvergedSampleExactness(unittest.TestCase):
    """`converged_sample` requires `value == expected_size`
    EXACTLY -- neither a value one below, nor a value above, counts. The
    `probe_ok` gate applies independent of value: a probe_ok=False
    sample carrying a "correct" value (constructed directly here, bypassing
    `_physical_sample`'s auto-None-on-probe_ok=False convention, to prove the
    gate can't be gamed by a malformed/adversarial record) must not count."""

    def _recording_with(self, sample):
        return _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=[sample],
            reported_done_at_rel_s=2.0,
        )

    def test_value_one_below_expected_not_converged(self):
        recording = self._recording_with(_physical_sample(5.0, EXPECTED_SIZE - 1))
        _assert_schema_valid(self, recording)
        self.assertIsNone(a3b.converged_sample(recording, TERM))

    def test_value_equal_expected_converged(self):
        recording = self._recording_with(_physical_sample(5.0, EXPECTED_SIZE))
        _assert_schema_valid(self, recording)
        cs = a3b.converged_sample(recording, TERM)
        self.assertIsNotNone(cs)
        self.assertEqual(cs["t_rel_s"], 5.0)

    def test_value_above_expected_not_converged(self):
        """Exact equality only -- an overshoot does NOT count as converged,
        per the literal `value == expected_size` (not `>=`)."""
        recording = self._recording_with(_physical_sample(5.0, EXPECTED_SIZE + 100))
        _assert_schema_valid(self, recording)
        self.assertIsNone(a3b.converged_sample(recording, TERM))

    def test_probe_ok_false_with_matching_value_not_converged(self):
        # Deliberately NOT using `_physical_sample` (which forces value=None
        # whenever probe_ok=False) -- hand-crafted to adversarially test that
        # the probe_ok gate holds even when `value` happens to already equal
        # expected_size on a failed probe.
        gamed_sample = {
            "t_rel_s": 5.0, "term_id": TERM, "sli_name": "physicalSize",
            "value": EXPECTED_SIZE, "provider": TARGET, "probe_ok": False,
        }
        recording = self._recording_with(gamed_sample)
        # NOT schema-valid (probe_ok=False must carry a null value per
        # schema.py's per-sample invariant) -- that's the point: this
        # fixture is intentionally malformed to probe converged_sample()'s
        # own defensiveness independent of the schema layer.
        self.assertIsNone(a3b.converged_sample(recording, TERM))


# --- 8.5 The four corroboration classes, isolated (item 5) --------

class TestA3bCorroborationClassesIsolated(unittest.TestCase):
    """Each of E-probe/E-stuck/E-health/E-status-shortfall
    fires independently; this isolates each so exactly one class fires per
    fixture (except the last test, which deliberately fires two), and checks
    the `[w_start, t_max]` window correctly EXCLUDES E-probe/E-health signals
    that occur before the placement action's `ts_rel_s`."""

    def _emit_for(self, tool_calls, samples, reported_done_at_rel_s,
                  transfer_status_samples=None, health_samples=None, schema_check=True):
        recording = _recording(
            tool_calls=tool_calls,
            samples=samples,
            reported_done_at_rel_s=reported_done_at_rel_s,
            transfer_status_samples=transfer_status_samples,
            health_samples=health_samples,
        )
        if schema_check:
            _assert_schema_valid(self, recording)
        result = a3b.verdict(recording)
        self.assertEqual(result["verdict_per_term"][TERM], "Violated")
        return a3b.emit(recording, verdict_result=result)

    def test_e_probe_fires_alone(self):
        samples = [
            _physical_sample(0.0, 0),
            {"t_rel_s": 10.0, "term_id": TERM, "sli_name": "physicalSize",
             "value": None, "provider": TARGET, "probe_ok": False},
            _physical_sample(40.0, 0),
        ]
        result = self._emit_for([_replication_call(ts_rel_s=5.0)], samples, reported_done_at_rel_s=6.0)
        self.assertEqual(set(result["corroboration_per_term"][TERM]), {"E-probe"})
        self.assertEqual(result["confidence_per_term"][TERM], "high")

    def test_e_probe_excludes_sample_before_w_start(self):
        """w_start = the placement action's ts_rel_s (5.0 here). A probe
        failure at t=2.0 -- BEFORE w_start -- must NOT count toward E-probe,
        even though it's within [0, t_max]."""
        samples = [
            _physical_sample(0.0, 0),
            {"t_rel_s": 2.0, "term_id": TERM, "sli_name": "physicalSize",
             "value": None, "provider": TARGET, "probe_ok": False},
            _physical_sample(20.0, 0),
        ]
        result = self._emit_for([_replication_call(ts_rel_s=5.0)], samples, reported_done_at_rel_s=6.0)
        self.assertEqual(result["corroboration_per_term"][TERM], [])
        self.assertEqual(result["confidence_per_term"][TERM], "inferred")

    def test_e_stuck_fires_alone(self):
        transfer_status = [_transfer_status_sample(T_MAX + 1.0, "scheduled")]  # non-terminal, at/after t_max
        result = self._emit_for(
            [_replication_call(ts_rel_s=1.0)], _flat_samples(0, until=60.0),
            reported_done_at_rel_s=2.0, transfer_status_samples=transfer_status,
        )
        self.assertEqual(set(result["corroboration_per_term"][TERM]), {"E-stuck"})

    def test_e_health_fires_alone(self):
        health_samples = [_health_sample(10.0, False)]
        result = self._emit_for(
            [_replication_call(ts_rel_s=1.0)], _flat_samples(0, until=60.0),
            reported_done_at_rel_s=2.0, health_samples=health_samples,
        )
        self.assertEqual(set(result["corroboration_per_term"][TERM]), {"E-health"})

    def test_e_health_excludes_sample_before_w_start(self):
        health_samples = [_health_sample(2.0, False)]  # before w_start=10.0
        result = self._emit_for(
            [_replication_call(ts_rel_s=10.0)], _flat_samples(0, until=60.0),
            reported_done_at_rel_s=11.0, health_samples=health_samples,
        )
        self.assertEqual(result["corroboration_per_term"][TERM], [])
        self.assertEqual(result["confidence_per_term"][TERM], "inferred")

    def test_e_status_shortfall_fires_alone_exact(self):
        """Strengthens the existing
        `test_wild_completed_with_zero_bytes_fires_e_status_shortfall`
        (which uses assertIn) with an exact-set check: a terminal status
        alone must not ALSO spuriously fire E-stuck."""
        transfer_status = [_transfer_status_sample(35.0, "completed")]  # terminal
        result = self._emit_for(
            [_replication_call(ts_rel_s=1.0)], _flat_samples(0, until=60.0),
            reported_done_at_rel_s=2.0, transfer_status_samples=transfer_status,
        )
        self.assertEqual(set(result["corroboration_per_term"][TERM]), {"E-status-shortfall"})

    def test_e_stuck_and_e_status_shortfall_can_both_fire(self):
        """A non-terminal status at/after t_max (E-stuck) AND a terminal
        status somewhere in the record (E-status-shortfall) are independent
        `any(...)` checks over the SAME transfer_status_samples array --
        both can legitimately fire together."""
        transfer_status = [
            _transfer_status_sample(T_MAX + 1.0, "scheduled"),
            _transfer_status_sample(35.0, "completed"),
        ]
        result = self._emit_for(
            [_replication_call(ts_rel_s=1.0)], _flat_samples(0, until=60.0),
            reported_done_at_rel_s=2.0, transfer_status_samples=transfer_status,
        )
        self.assertEqual(set(result["corroboration_per_term"][TERM]), {"E-stuck", "E-status-shortfall"})


# --- 8.5b `strict_target_shortfall` guard ------------------------------------

class TestA3bStatusShortfallTargetGuard(unittest.TestCase):
    """A wrong-target replication (e.g. scheduled to the SOURCE instead of
    the contracted target) completes trivially -- the file is already
    there -- producing a terminal transfer status while the CONTRACT
    target's physicalSize stays short of expected. Without this guard,
    E-status-shortfall fires on this pattern regardless of which provider
    the op targeted, crediting the SERVICE with the agent's own wrong op
    echoed back. `strict_target_shortfall` (default True) closes that gap;
    `False` disables the guard for alternative
    scoring (`rq2.confusion_matrix`'s sibling flag)."""

    def _wrong_target_recording(self):
        # Scheduled to SOURCE ("prov-cloud-pl"), not the contract target
        # TARGET ("prov-de") -- mirrors the real cloud-pl-source-provider
        # shape.
        tool_calls = [_replication_call(ts_rel_s=1.0, target=SOURCE)]
        samples = _flat_samples(0, until=60.0, probe_ok=True)  # contract target never converges
        transfer_status = [_transfer_status_sample(35.0, "completed")]  # terminal (the wrong-target op)
        return _recording(
            tool_calls=tool_calls,
            samples=samples,
            reported_done_at_rel_s=2.0,
            transfer_status_samples=transfer_status,
        )

    def test_wrong_target_completion_is_not_corroboration_under_strict_default(self):
        recording = self._wrong_target_recording()
        _assert_schema_valid(self, recording)
        self.assertEqual(competence.agent_competence(recording), "fail")

        result = a3b.emit(recording)  # default strict_target_shortfall=True
        self.assertNotIn("E-status-shortfall", result["corroboration_per_term"][TERM])
        self.assertEqual(result["corroboration_per_term"][TERM], [])
        # No corroborating evidence anywhere else in this fixture -> the
        # wrong-target completion is agent-evidence only, never service/both.
        self.assertEqual(result["attribution_per_term"][TERM], "agent")
        self.assertEqual(result["emitted_attribution"], "agent")
        self.assertIsNone(result["confidence_per_term"][TERM])

    def test_wrong_target_completion_fires_under_pre_amendment_flag(self):
        recording = self._wrong_target_recording()
        result = a3b.emit(recording, strict_target_shortfall=False)
        self.assertIn("E-status-shortfall", result["corroboration_per_term"][TERM])
        self.assertEqual(result["attribution_per_term"][TERM], "both")
        self.assertEqual(result["emitted_attribution"], "both")
        self.assertEqual(result["confidence_per_term"][TERM], "high")

    def test_contract_target_completion_fires_under_both_flags(self):
        """A CONTRACT-target op (the ordinary case every other E-status-
        shortfall test in this file already covers) is unaffected by the
        guard -- E-status-shortfall fires identically under both flag
        values."""
        tool_calls = [_replication_call(ts_rel_s=1.0)]  # default target == TARGET (contract)
        samples = _flat_samples(0, until=60.0, probe_ok=True)
        transfer_status = [_transfer_status_sample(35.0, "completed")]
        recording = _recording(
            tool_calls=tool_calls,
            samples=samples,
            reported_done_at_rel_s=2.0,
            transfer_status_samples=transfer_status,
        )
        _assert_schema_valid(self, recording)
        self.assertEqual(competence.agent_competence(recording), "pass")

        result_strict = a3b.emit(recording)
        self.assertIn("E-status-shortfall", result_strict["corroboration_per_term"][TERM])
        self.assertEqual(result_strict["attribution_per_term"][TERM], "service")
        self.assertEqual(result_strict["confidence_per_term"][TERM], "high")

        result_pre_amendment = a3b.emit(recording, strict_target_shortfall=False)
        self.assertIn("E-status-shortfall", result_pre_amendment["corroboration_per_term"][TERM])
        self.assertEqual(result_pre_amendment["attribution_per_term"][TERM], "service")
        self.assertEqual(result_pre_amendment["confidence_per_term"][TERM], "high")

    def test_no_schedule_op_at_all_preserves_pre_amendment_behavior(self):
        """The shadow case (no ok schedule_file_replication call anywhere):
        the guard doesn't apply -- E-status-shortfall's existing behavior
        (fire on terminal-status + shortfall alone) is preserved regardless
        of `strict_target_shortfall`, since there is no op-target to judge."""
        samples = _flat_samples(0, until=60.0, probe_ok=True)
        transfer_status = [_transfer_status_sample(35.0, "completed")]
        recording = _recording(
            tool_calls=[],
            samples=samples,
            reported_done_at_rel_s=2.0,
            transfer_status_samples=transfer_status,
        )
        _assert_schema_valid(self, recording)
        self.assertEqual(competence.agent_competence(recording), "fail")

        result_strict = a3b.emit(recording)
        self.assertIn("E-status-shortfall", result_strict["corroboration_per_term"][TERM])
        self.assertEqual(result_strict["attribution_per_term"][TERM], "both")

        result_pre_amendment = a3b.emit(recording, strict_target_shortfall=False)
        self.assertIn("E-status-shortfall", result_pre_amendment["corroboration_per_term"][TERM])
        self.assertEqual(result_pre_amendment["attribution_per_term"][TERM], "both")


# --- 8.6 service_evidence / confidence matrix (item 6) ------------

class TestA3bServiceEvidenceConfidenceMatrix(unittest.TestCase):
    """Attribution + confidence as a joint function of
    (agent-evidence = competence==fail, service-evidence = competence==pass
    OR fired-non-empty), with confidence = "high" if fired else "inferred"
    if service-evidence else None. Existing tests cover each cell under
    different fixture flavors (wrong-target for agent-only, E-probe for
    service+corroborated, etc.); this consolidates all four cells against
    the SAME fixture shape so the only varying inputs are (competent,
    corroborated)."""

    def _case(self, *, competent, corroborated):
        target = TARGET if competent else "prov-WRONG"
        tool_calls = [_replication_call(ts_rel_s=1.0, target=target)]
        if corroborated:
            samples = [
                _physical_sample(0.0, 0),
                {"t_rel_s": 4.0, "term_id": TERM, "sli_name": "physicalSize",
                 "value": None, "provider": TARGET, "probe_ok": False},
                _physical_sample(20.0, 0),
                _physical_sample(40.0, 0),
            ]
        else:
            samples = _flat_samples(0, until=60.0, probe_ok=True)
        recording = _recording(
            tool_calls=tool_calls,
            samples=samples,
            reported_done_at_rel_s=2.0,
        )
        _assert_schema_valid(self, recording)
        self.assertEqual(competence.agent_competence(recording), "pass" if competent else "fail")
        a3b_v = a3b.verdict(recording)
        self.assertEqual(a3b_v["verdict_per_term"][TERM], "Violated")
        return a3b.emit(recording, verdict_result=a3b_v)

    def test_competent_no_corroboration_is_service_inferred(self):
        result = self._case(competent=True, corroborated=False)
        self.assertEqual(result["attribution_per_term"][TERM], "service")
        self.assertEqual(result["confidence_per_term"][TERM], "inferred")

    def test_competent_with_corroboration_is_service_high(self):
        result = self._case(competent=True, corroborated=True)
        self.assertEqual(result["attribution_per_term"][TERM], "service")
        self.assertEqual(result["confidence_per_term"][TERM], "high")

    def test_incompetent_no_corroboration_is_agent_none_confidence(self):
        result = self._case(competent=False, corroborated=False)
        self.assertEqual(result["attribution_per_term"][TERM], "agent")
        self.assertIsNone(result["confidence_per_term"][TERM])

    def test_incompetent_with_corroboration_is_both_high(self):
        result = self._case(competent=False, corroborated=True)
        self.assertEqual(result["attribution_per_term"][TERM], "both")
        self.assertEqual(result["confidence_per_term"][TERM], "high")


# --- 8.7 Empty/degenerate inputs (item 7) --------------------------
#
# Split into two classes: cases where current behavior IS sensible
# (TestA3bDegenerateInputsSensible) and cases where it's the SAME underlying
# gap as the item-8 namespace mismatch (TestA3bObservabilityScopeGap, next
# section).

class TestA3bDegenerateInputsSensible(unittest.TestCase):
    def test_no_contract_terms_returns_empty_without_crashing(self):
        recording = _recording(tool_calls=[], samples=[])
        recording["contract"]["terms"] = []
        recording["expected"]["per_term"] = {}
        result = a3b.verdict(recording)
        self.assertEqual(result, {"verdict_per_term": {}, "violation_mode_per_term": {}})
        emit_result = a3b.emit(recording, verdict_result=result)
        self.assertEqual(emit_result["attribution_per_term"], {})
        self.assertEqual(emit_result["emitted_attribution"], "neither")
        # agent_competence still runs (recording-level, no terms -> "fail" by
        # its own documented convention -- see competence.py's _first_term_id).
        self.assertEqual(competence.agent_competence(recording), "fail")

    def test_no_samples_at_all_is_not_determined(self):
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)], samples=[], reported_done_at_rel_s=2.0,
        )
        _assert_schema_valid(self, recording)
        result = a3b.verdict(recording)
        self.assertEqual(result["verdict_per_term"][TERM], "NotDetermined")
        emit_result = a3b.emit(recording, verdict_result=result)
        self.assertEqual(emit_result["attribution_per_term"][TERM], "neither")

    def test_t_max_none_is_not_determined(self):
        # Built via the factory then mutated post-hoc: `_recording()` itself
        # computes `polled_past_t_max = poll_until_rel_s >= t_max_s` at
        # construction time, which would raise on a None t_max_s.
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=_flat_samples(0, until=60.0),
            reported_done_at_rel_s=2.0,
        )
        recording["contract"]["terms"][0]["t_max_s"] = None
        result = a3b.verdict(recording)
        self.assertEqual(result["verdict_per_term"][TERM], "NotDetermined")
        emit_result = a3b.emit(recording, verdict_result=result)
        self.assertEqual(emit_result["attribution_per_term"][TERM], "neither")


# --- 8.8 Observability provider-scope gap (items 7 + 8) --

class TestA3bObservabilityScopeGap(unittest.TestCase):
    """`_observable_in_window`
    (the helper deciding the Violated/NotDetermined boundary) scopes its
    search to the term's contracted `target_providers`, matching its three
    siblings `converged_sample`, `_latest_physical`, and A3a's own candidate
    filter (a3a.py) -- all three already require `provider in
    target_providers`. Without this scoping, whenever ANY physicalSize sample for a
    term/sli succeeded (probe_ok=True) ANYWHERE in [0, t_max] -- even on a
    provider that is NOT one of the term's contracted targets, or even when
    the term's target-identity metadata itself was missing/corrupt -- the
    term would score "observable" and a never-converged term would read Violated
    instead of the epistemically-honest NotDetermined ("the CONTRACTED
    TARGET's state was never actually established").

    Most of these tests document this CORRECTED behavior; one
    (`test_expected_size_none_on_present_entry_produces_violated_not_notdetermined`)
    is unaffected by the provider-scoping rule -- its samples are already correctly scoped to
    the target, so the provider-scoping rule doesn't change its verdict.
    That one documents a DIFFERENT, still-open gap (a missing `expected_size`
    doesn't block `_observable_in_window`, only `converged_sample`'s exact-
    match) -- left as-is, out of scope here.
    """

    def test_off_target_provider_probe_leaves_unobserved_target_notdetermined(self):
        """Headline repro: a fully SCHEMA-VALID recording. The contracted target's OWN
        physicalSize probes ALL fail (probe_ok=False) throughout [0, t_max]
        -- the target was never successfully observed. A completely
        different, off-target provider (imagine a diagnostic or source-side
        probe sharing the same term_id + sli_name by accident or by a future
        multi-provider-per-SLI extension) has ONE successful probe in-window.
        The schema's cross-namespace guard does NOT reject this shape (it
        only requires a NON-DISJOINT intersection between sample providers
        and target_providers, not full containment) -- so this reaches
        a3b.verdict() as legitimately schema-valid data.

        Correct behavior: the off-target provider's successful
        probe does not count toward "observable" for this term, so the
        contracted target's total probe-failure correctly reads
        NotDetermined, not a false Violated.
        """
        other = "prov-off-target-diagnostic"
        samples = [
            {"t_rel_s": 0.0, "term_id": TERM, "sli_name": "physicalSize",
             "value": None, "provider": TARGET, "probe_ok": False},
            {"t_rel_s": 10.0, "term_id": TERM, "sli_name": "physicalSize",
             "value": None, "provider": TARGET, "probe_ok": False},
            {"t_rel_s": 20.0, "term_id": TERM, "sli_name": "physicalSize",
             "value": None, "provider": TARGET, "probe_ok": False},
            _physical_sample(5.0, 0, provider=other, probe_ok=True),
        ]
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=samples,
            reported_done_at_rel_s=2.0,
        )
        schema.validate(recording)  # asserts this IS schema-valid -- the crux of this test

        self.assertEqual(competence.agent_competence(recording), "pass")
        self.assertIsNone(a3b.converged_sample(recording, TERM))
        # Corrected behavior: NotDetermined -- the off-target probe_ok=True
        # sample is no longer counted; the CONTRACTED target itself was
        # never successfully probed at all, so this is epistemically ND.
        result = a3b.verdict(recording)
        self.assertEqual(result["verdict_per_term"][TERM], "NotDetermined")
        self.assertIsNone(result["violation_mode_per_term"][TERM])
        # emit() follows: NotDetermined -> "neither" (the
        # non-Violated branch), no corroboration, no confidence -- this
        # also avoids a compounding false {service}/"high" attribution
        # for a term whose target state was
        # never actually observed.
        emit_result = a3b.emit(recording, verdict_result=result)
        self.assertEqual(emit_result["attribution_per_term"][TERM], "neither")
        self.assertIsNone(emit_result["confidence_per_term"][TERM])
        self.assertEqual(emit_result["corroboration_per_term"][TERM], [])

    def test_missing_expected_per_term_entry_produces_notdetermined(self):
        """Secondary repro (schema-INVALID input -- schema.py's general
        `expected.per_term is missing an entry for term_id=...` check
        rejects this; included to
        test "missing expected.per_term entry" as a degenerate a3b.py input,
        independent of whether schema.validate() would also catch it).

        Same root cause + same behavior as the headline test: with the
        per_term entry gone, `target_providers` resolves to an empty set, so
        NO sample can match `provider in target_providers` regardless of
        where it was recorded -- correctly reading NotDetermined ("the
        target" is not known for this term) rather than a naive
        Violated/never_converged.
        """
        recording = _recording(tool_calls=[], samples=_flat_samples(0, until=60.0))
        del recording["expected"]["per_term"][TERM]
        with self.assertRaises(schema.SchemaError):
            schema.validate(recording)

        self.assertIsNone(a3b.converged_sample(recording, TERM))
        result = a3b.verdict(recording)
        self.assertEqual(result["verdict_per_term"][TERM], "NotDetermined")
        self.assertIsNone(result["violation_mode_per_term"][TERM])

    def test_expected_size_none_on_present_entry_produces_violated_not_notdetermined(self):
        """Same shape as the previous test, minimal variant: the per_term
        ENTRY is present but `expected_size` is explicitly None (also
        schema-invalid -- schema.py requires expected_size or
        expected_metadata). No off-target provider needed here at all: the
        term's OWN samples (on the correct target, probe_ok=True) are enough
        to trip `_observable_in_window`, since that helper doesn't consult
        expected_size either."""
        recording = _recording(tool_calls=[], samples=_flat_samples(0, until=60.0))
        recording["expected"]["per_term"][TERM]["expected_size"] = None
        with self.assertRaises(schema.SchemaError):
            schema.validate(recording)

        result = a3b.verdict(recording)
        self.assertEqual(result["verdict_per_term"][TERM], "Violated")
        self.assertEqual(result["violation_mode_per_term"][TERM], "never_converged")

    def test_real_recordings_with_namespace_mismatch_verdict_is_notdetermined(self):
        """Using REAL on-disk data: two early
        smoke recordings exhibit the documented label-vs-ID namespace bug
        (a3b.py's own module docstring + competence.py's docstring discuss
        it at length; `test_raw_label_namespace_recordings_rejected_by_guard`
        above already confirms schema.validate() REJECTS both -- this
        test does not re-test that guard). Without the provider-scoping rule,
        what a3b.verdict() would return on these RAW (pre-guard)
        recordings is: Violated/never_converged, not
        the epistemically-honest
        NotDetermined -- the same observability provider-scope gap as the
        synthetic repros above, demonstrated on real data.

        Correct behavior: `converged_sample` already correctly
        returns None (the part of the gap the provider-scoping rule doesn't
        touch); because
        `_observable_in_window` is also scoped to `target_providers`, the
        raw provider-ID-hash samples never match the human-label
        `target_providers` entry, so NO sample is counted observable and the
        verdict correctly reads NotDetermined.
        """
        for fname in REAL_RECORDING_FILES:
            path = os.path.join(REAL_RECORDINGS_DIR, fname)
            if not os.path.exists(path):
                self.skipTest(f"real recordings not found under {REAL_RECORDINGS_DIR}")
            with open(path) as f:
                recording = json.load(f)
            with self.subTest(recording=fname):
                term_id = recording["contract"]["terms"][0]["term_id"]
                self.assertIsNone(a3b.converged_sample(recording, term_id))
                result = a3b.verdict(recording)
                self.assertEqual(result["verdict_per_term"][term_id], "NotDetermined")
                self.assertIsNone(result["violation_mode_per_term"][term_id])


# =============================================================================
# 9. leg-level failures are NO-DATA, not agent-competence (schema.is_leg_error)
# =============================================================================

class TestLegErrorNoData(unittest.TestCase):
    """A leg-level failure (panel.py's LegResult.error -- e.g. a Forge model
    that 400s because it's inactive/grant-scoped) must be surfaced +
    excluded from competence/attribution scoring as no-data, never scored
    as agent-incompetence. `competence.py` and `a3b.py` are UNCHANGED --
    the exclusion happens at the aggregation boundary
    (aggregate.py), exactly like the existing schema-invalid exclusion."""

    def _leg_error_recording(self):
        # Empty tool_calls + a flat/never-converged trace -- the observable
        # shape of a leg that never got to run because the API call itself
        # failed (the W1 smoke: Llama-3.3-70B "currently inactive" +
        # GLM-5.2-FP8 "not available for grant" both recorded exactly this
        # shape; without the no-data exclusion this would score competence
        # 0.0 instead).
        rec = _recording(
            tool_calls=[],
            samples=_flat_samples(0, until=60.0),
            self_reported="error",
            reported_done_at_rel_s=0.0,
        )
        rec["trial_id"] = "trial-leg-error"
        rec["agent"]["error"] = "BadRequestError: 400 model currently inactive"
        return rec

    def test_leg_error_recording_still_schema_valid(self):
        # documents the boundary: agent.error doesn't make the record
        # schema-INVALID -- it's a separate NO-DATA discriminator.
        _assert_schema_valid(self, self._leg_error_recording())

    def test_is_leg_error_flags_it(self):
        self.assertTrue(schema.is_leg_error(self._leg_error_recording()))

    def test_competence_and_a3b_are_unchanged_by_the_fix(self):
        # competence.py / a3b.py are structurally unaffected: they
        # still score a leg-error recording exactly as they would any other
        # empty-trace/never-converged recording -- the NO-DATA exclusion
        # happens at the aggregation boundary (aggregate.py/rq2.py/
        # panel_score.py), never inside the lenses themselves.
        rec = self._leg_error_recording()
        self.assertEqual(competence.agent_competence(rec), "fail")
        self.assertEqual(a3b.verdict(rec)["verdict_per_term"][TERM], "Violated")

    def test_aggregate_excludes_leg_error_trial_and_tallies_it(self):
        clean = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=_ramp_samples(step_at=5.0, until=60.0),
            reported_done_at_rel_s=2.0,
        )
        _assert_schema_valid(self, clean)
        leg_error_rec = self._leg_error_recording()

        ingested_clean_only = aggregate.validated_ingest([clean])
        ingested_with_leg_error = aggregate.validated_ingest([clean, leg_error_rec])

        self.assertEqual(len(ingested_with_leg_error["valid"]), 1)
        self.assertEqual(ingested_with_leg_error["excluded"], [])
        self.assertEqual(len(ingested_with_leg_error["leg_errors"]), 1)
        self.assertEqual(ingested_with_leg_error["leg_errors"][0]["trial_id"], "trial-leg-error")

        # Crucially: adding the leg-error trial changes NOTHING about how
        # the clean recording is scored -- the exclusion is a no-op on
        # clean data (this is why published RQ1/RQ2 are safe).
        agg_clean_only = aggregate.aggregate(ingested_clean_only["valid"])
        agg_with_leg_error = aggregate.aggregate(ingested_with_leg_error["valid"])
        self.assertEqual(agg_clean_only, agg_with_leg_error)


class TestStatusVsDataDivergence(unittest.TestCase):
    """aggregate.status_vs_data_divergence — job-reported-terminal vs
    bytes-actually-there (the OneData
    completed-with-0-bytes status-lie as a first-class metric)."""

    def _sched(self, ts=1.0):
        return _tool_call(
            "schedule_file_replication",
            {"file_id_or_path": FILE_PATH, "target_provider_id": TARGET}, ts,
        )

    def test_completed_with_data(self):
        rec = _recording(
            tool_calls=[self._sched()],
            samples=[_physical_sample(5, 0), _physical_sample(12, EXPECTED_SIZE)],
            transfer_status_samples=[_transfer_status_sample(12, "completed")],
            reported_done_at_rel_s=13.0,
        )
        d = aggregate.status_vs_data_divergence([rec])
        self.assertEqual(d["completed_with_data"], 1)
        self.assertEqual(d["completed_but_data_absent"], 0)
        self.assertEqual(d["no_terminal_status"], 0)
        self.assertEqual(d["terminal_status_reported"], 1)

    def test_completed_but_data_absent_is_the_status_lie(self):
        # status 'completed' but physicalSize never leaves 0 -> OneData lied.
        rec = _recording(
            tool_calls=[self._sched()],
            samples=[_physical_sample(5, 0), _physical_sample(35, 0)],
            transfer_status_samples=[_transfer_status_sample(35, "completed")],
            reported_done_at_rel_s=13.0,
            poll_until_rel_s=45.0,
        )
        d = aggregate.status_vs_data_divergence([rec])
        self.assertEqual(d["completed_but_data_absent"], 1)
        self.assertEqual(d["completed_with_data"], 0)
        self.assertEqual(d["terminal_status_reported"], 1)

    def test_late_convergence_with_terminal_counts_as_with_data(self):
        # bytes arrive AFTER t_max (SLA Violated) but data IS present -> NOT a lie.
        rec = _recording(
            tool_calls=[self._sched()],
            samples=[_physical_sample(5, 0), _physical_sample(35, EXPECTED_SIZE)],
            transfer_status_samples=[_transfer_status_sample(35, "completed")],
            reported_done_at_rel_s=13.0,
            poll_until_rel_s=45.0,
        )
        d = aggregate.status_vs_data_divergence([rec])
        self.assertEqual(d["completed_with_data"], 1)
        self.assertEqual(d["completed_but_data_absent"], 0)

    def test_no_terminal_status_scheduled_forever(self):
        rec = _recording(
            tool_calls=[self._sched()],
            samples=[_physical_sample(5, 0), _physical_sample(35, 0)],
            transfer_status_samples=[_transfer_status_sample(35, "scheduled")],
            reported_done_at_rel_s=13.0,
            poll_until_rel_s=45.0,
        )
        d = aggregate.status_vs_data_divergence([rec])
        self.assertEqual(d["no_terminal_status"], 1)
        self.assertEqual(d["terminal_status_reported"], 0)

    def test_no_transfer_status_samples_at_all(self):
        rec = _recording(
            tool_calls=[self._sched()],
            samples=[_physical_sample(5, EXPECTED_SIZE)],
            transfer_status_samples=[],
            reported_done_at_rel_s=6.0,
        )
        d = aggregate.status_vs_data_divergence([rec])
        self.assertEqual(d["no_terminal_status"], 1)
        self.assertEqual(d["terminal_status_reported"], 0)

    def test_mixed_batch_totals(self):
        with_data = _recording(
            tool_calls=[self._sched()],
            samples=[_physical_sample(12, EXPECTED_SIZE)],
            transfer_status_samples=[_transfer_status_sample(12, "completed")],
            reported_done_at_rel_s=13.0,
        )
        lie = _recording(
            tool_calls=[self._sched()],
            samples=[_physical_sample(5, 0), _physical_sample(35, 0)],
            transfer_status_samples=[_transfer_status_sample(35, "completed")],
            reported_done_at_rel_s=13.0, poll_until_rel_s=45.0,
        )
        stuck = _recording(
            tool_calls=[self._sched()],
            samples=[_physical_sample(35, 0)],
            transfer_status_samples=[_transfer_status_sample(35, "scheduled")],
            reported_done_at_rel_s=13.0, poll_until_rel_s=45.0,
        )
        d = aggregate.status_vs_data_divergence([with_data, lie, stuck])
        self.assertEqual(d, {
            "completed_with_data": 1,
            "completed_but_data_absent": 1,
            "no_terminal_status": 1,
            "terminal_status_reported": 2,
        })


if __name__ == "__main__":
    unittest.main()
