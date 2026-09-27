"""Regression tests for harness/rq2.py.

Stdlib `unittest` only, same import-shim convention as harness/test_schema.py
and harness/test_lenses.py. Run either as:

    python3 -m unittest harness.test_rq2 -v      # from repo root
    python3 harness/test_rq2.py                  # directly as a script

Fully OFFLINE: every recording here is a hand-built synthetic dict, built by
a LOCAL COPY of test_lenses.py's `_recording(...)` factory (not imported —
test modules don't import each other). No federation
contact, no model calls, no network.

Covers:
    1. `truth()` for each of four worked examples: {agent}
       (row 7), {service} (row 6), {both} (row 8), {neither}/wild (row 5).
    2. `truth()` for the remaining rows the worked examples don't hit:
       Fulfilled -> neither always, even with A=fail and/or F=true (rows
       1-4); NotDetermined -> agent iff A=fail, F never promotes it to
       service/both even when F=true (tie-break T3, rows 9-10).
    3. `F` edge cases: fault off-path (wrong target_provider) ->
       False; fault outside the [w_start, t_max] window -> False;
       `cleared_at_rel_s is None` -> treated as still-active (True if the
       window overlaps).
    4. `confusion_matrix()`: an on-diagonal match per ground-truthable
       label, an off-diagonal MISMATCH within a ground-truthable row
       (truth={both} but emit={agent} — the "uncorroborated both" case),
       and the named wild off-diagonal (truth={neither} -> emit={service}),
       asserting the wild cell lands in `wild_row` and is excluded from
       `accuracy_ground_truthable`'s denominator.
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
        from harness import rq2
        from harness.lenses import competence, a3b
    except ImportError:
        import rq2
        from lenses import competence, a3b
else:
    from . import rq2
    from .lenses import competence, a3b


TERM = "T1"
T_MAX = 30.0
EXPECTED_SIZE = 4096
TARGET = "prov-de"
WRONG_TARGET = "prov-WRONG"
SOURCE = "prov-cloud-pl"
FILE_PATH = "/space-1/harness/synthetic/trial.bin"

NO_FAULT = {
    "condition": "none",
    "target_provider": None,
    "injected_at_rel_s": None,
    "cleared_at_rel_s": None,
    "netem_delay_ms": None,
}


# --- fixture builders (local copy of test_lenses.py's factory pattern) ------

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


def _no_probe_ok_samples(until=60.0, interval=2.0, provider=TARGET):
    """All samples probe_ok=False -- state never observable (NotDetermined)."""
    samples = []
    t = 0.0
    while t <= until + 1e-9:
        samples.append(_physical_sample(round(t, 3), None, provider=provider, probe_ok=False))
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
        "trial_id": "trial-synthetic-rq2",
        "sweep_id": "sweep-synthetic",
        "recorded_at": "2026-07-06T00:00:00Z",
        "harness_version": "test",
        "trial_start_utc": "2026-07-06T00:00:00Z",
        "scenario_id": scenario_id,
        "arm_live": "A4",
        "model_leg": "test-model",
        "trial_k": 1,
        "source_provider": SOURCE,
        "agent_mcp_host": "https://example.invalid",
        "fault": fault if fault is not None else dict(NO_FAULT),
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


# =============================================================================
# 1. truth() for the four worked examples
# =============================================================================

class TestTruthWorkedExamples(unittest.TestCase):
    def test_example_1_truth_agent(self):
        """row 7: Violated * A=fail * F=false -> {agent}. Wrong-target
        replication, never converges, no fault injected at all."""
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0, target=WRONG_TARGET)],
            samples=_flat_samples(0, until=60.0),
            reported_done_at_rel_s=2.0,
        )
        self.assertEqual(competence.agent_competence(recording), "fail")
        self.assertEqual(a3b.verdict(recording)["verdict_per_term"][TERM], "Violated")
        self.assertEqual(rq2.truth(recording), {TERM: "agent"})

    def test_example_2_truth_service(self):
        """row 6: Violated * A=pass * F=true -> {service}. Correct
        replication, never converges, netem_lag injected on the target
        provider overlapping [w_start, t_max]."""
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=_flat_samples(0, until=60.0),
            reported_done_at_rel_s=2.0,
            fault={
                "condition": "netem_lag",
                "target_provider": TARGET,
                "injected_at_rel_s": 5.0,
                "cleared_at_rel_s": 40.0,
                "netem_delay_ms": 60000.0,
            },
        )
        self.assertEqual(competence.agent_competence(recording), "pass")
        self.assertEqual(a3b.verdict(recording)["verdict_per_term"][TERM], "Violated")
        self.assertEqual(rq2.truth(recording), {TERM: "service"})

    def test_example_3_truth_both(self):
        """row 8: Violated * A=fail * F=true -> {both} (tie-break T1,
        over-determination). Wrong-target replication AND a concurrent
        on-path overlapping fault."""
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0, target=WRONG_TARGET)],
            samples=_flat_samples(0, until=60.0),
            reported_done_at_rel_s=2.0,
            fault={
                "condition": "outage",
                "target_provider": TARGET,
                "injected_at_rel_s": 4.0,
                "cleared_at_rel_s": 45.0,
                "netem_delay_ms": None,
            },
        )
        self.assertEqual(competence.agent_competence(recording), "fail")
        self.assertEqual(a3b.verdict(recording)["verdict_per_term"][TERM], "Violated")
        self.assertEqual(rq2.truth(recording), {TERM: "both"})

    def test_example_4_truth_neither_wild(self):
        """row 5: Violated * A=pass * F=false -> {neither} (the wild-caught
        residual service failure). Correct replication, never
        converges, no fault injected."""
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=_flat_samples(0, until=60.0),
            reported_done_at_rel_s=2.0,
        )
        self.assertEqual(competence.agent_competence(recording), "pass")
        self.assertEqual(a3b.verdict(recording)["verdict_per_term"][TERM], "Violated")
        self.assertEqual(rq2.truth(recording), {TERM: "neither"})


# =============================================================================
# 2. truth() for Fulfilled (rows 1-4) and NotDetermined (rows 9-10)
# =============================================================================

class TestTruthFulfilledAndNotDetermined(unittest.TestCase):
    def test_fulfilled_pass_no_fault_is_neither(self):
        """row 1: Fulfilled * A=pass * F=false -> neither."""
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=_ramp_samples(step_at=10.0, until=60.0),
            reported_done_at_rel_s=2.0,
        )
        self.assertEqual(a3b.verdict(recording)["verdict_per_term"][TERM], "Fulfilled")
        self.assertEqual(rq2.truth(recording), {TERM: "neither"})

    def test_fulfilled_fail_with_overlapping_fault_is_still_neither(self):
        """row 4: Fulfilled * A=fail * F=true -> neither, always (T2). A
        Fulfilled term is always {neither} regardless of A or F -- the
        table's most surprising row, worth pinning explicitly."""
        recording = _recording(
            tool_calls=[],  # no qualifying replication call -> competence fail
            samples=_ramp_samples(step_at=5.0, until=60.0),
            reported_done_at_rel_s=2.0,
            fault={
                "condition": "outage",
                "target_provider": TARGET,
                "injected_at_rel_s": 1.0,
                "cleared_at_rel_s": 20.0,
                "netem_delay_ms": None,
            },
        )
        self.assertEqual(competence.agent_competence(recording), "fail")
        self.assertEqual(a3b.verdict(recording)["verdict_per_term"][TERM], "Fulfilled")
        self.assertEqual(rq2.truth(recording), {TERM: "neither"})

    def test_not_determined_fail_is_agent(self):
        """row 10: NotDetermined * A=fail -> agent (trace-confirmed error
        even with the outcome undetermined; never {both}/{service})."""
        recording = _recording(
            tool_calls=[],  # competence fail
            samples=_no_probe_ok_samples(until=60.0),
            reported_done_at_rel_s=2.0,
        )
        self.assertEqual(competence.agent_competence(recording), "fail")
        self.assertEqual(a3b.verdict(recording)["verdict_per_term"][TERM], "NotDetermined")
        self.assertEqual(rq2.truth(recording), {TERM: "agent"})

    def test_not_determined_pass_with_overlapping_fault_is_still_neither(self):
        """row 9: NotDetermined * A=pass * F=true -> neither. Tie-break T3:
        `F` never promotes a NotDetermined term to service/both, even when
        an on-path overlapping fault is genuinely present."""
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],  # competence pass
            samples=_no_probe_ok_samples(until=60.0),
            reported_done_at_rel_s=2.0,
            fault={
                "condition": "outage",
                "target_provider": TARGET,
                "injected_at_rel_s": 5.0,
                "cleared_at_rel_s": 20.0,
                "netem_delay_ms": None,
            },
        )
        self.assertEqual(competence.agent_competence(recording), "pass")
        self.assertEqual(a3b.verdict(recording)["verdict_per_term"][TERM], "NotDetermined")
        self.assertEqual(rq2.truth(recording), {TERM: "neither"})


# =============================================================================
# 3. F edge cases
# =============================================================================

class TestFaultOverlapEdgeCases(unittest.TestCase):
    def test_fault_off_path_is_f_false(self):
        """Fault targets a DIFFERENT provider than the term's
        target_providers -- off-path, F=false even though it temporally
        overlaps the window. Violated * A=pass * F=false -> {neither}
        (wild), not {service}."""
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=_flat_samples(0, until=60.0),
            reported_done_at_rel_s=2.0,
            fault={
                "condition": "outage",
                "target_provider": "prov-OFF-PATH",
                "injected_at_rel_s": 5.0,
                "cleared_at_rel_s": 20.0,
                "netem_delay_ms": None,
            },
        )
        self.assertEqual(rq2.truth(recording), {TERM: "neither"})

    def test_fault_after_t_max_is_f_false(self):
        """Fault is on-path but starts AFTER t_max -- no overlap with
        [w_start, t_max] -> F=false -> {neither} (wild), not {service}."""
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=_flat_samples(0, until=60.0),
            reported_done_at_rel_s=2.0,
            fault={
                "condition": "outage",
                "target_provider": TARGET,
                "injected_at_rel_s": 50.0,
                "cleared_at_rel_s": 55.0,
                "netem_delay_ms": None,
            },
        )
        self.assertEqual(rq2.truth(recording), {TERM: "neither"})

    def test_fault_cleared_before_w_start_is_f_false(self):
        """Fault is on-path but cleared BEFORE w_start (the replication
        call's ts_rel_s) -- no overlap -> F=false -> {neither}."""
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=10.0)],
            samples=_flat_samples(0, until=60.0),
            reported_done_at_rel_s=11.0,
            fault={
                "condition": "outage",
                "target_provider": TARGET,
                "injected_at_rel_s": 0.0,
                "cleared_at_rel_s": 2.0,
                "netem_delay_ms": None,
            },
        )
        self.assertEqual(rq2.truth(recording), {TERM: "neither"})

    def test_fault_cleared_at_none_is_treated_as_still_active(self):
        """`cleared_at_rel_s is None` means the fault is still active at
        recording time -- treated as +inf, so it overlaps as long as
        `injected_at_rel_s <= t_max`. Violated * A=pass * F=true ->
        {service}."""
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=_flat_samples(0, until=60.0),
            reported_done_at_rel_s=2.0,
            fault={
                "condition": "outage",
                "target_provider": TARGET,
                "injected_at_rel_s": 25.0,
                "cleared_at_rel_s": None,
                "netem_delay_ms": None,
            },
        )
        self.assertEqual(rq2.truth(recording), {TERM: "service"})

    def test_fault_condition_none_is_f_false_even_with_matching_provider(self):
        """`fault.condition == "none"` short-circuits F to false regardless
        of the other (unset) fields -- the RQ1/baseline no-injection cell."""
        recording = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=_flat_samples(0, until=60.0),
            reported_done_at_rel_s=2.0,
        )
        self.assertEqual(rq2.truth(recording), {TERM: "neither"})


# =============================================================================
# 4. confusion_matrix()
# =============================================================================

class TestConfusionMatrix(unittest.TestCase):
    def _recording_truth_agent_emit_agent(self):
        """On-diagonal: truth={agent}, emit={agent} (Example 1 shape, no
        corroborating evidence anywhere -- no transfer_status_samples here,
        since a terminal "completed" status would now correctly fire
        E-status-shortfall given the target's physicalSize never reaches
        expected_size)."""
        return _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0, target=WRONG_TARGET)],
            samples=_flat_samples(0, until=60.0, probe_ok=True),
            reported_done_at_rel_s=2.0,
        )

    def _recording_truth_service_emit_service(self):
        """On-diagonal: truth={service}, emit={service} (Example 2 shape;
        emit reaches {service} via the agent_competence==pass elimination
        arm)."""
        return _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=_flat_samples(0, until=60.0),
            reported_done_at_rel_s=2.0,
            fault={
                "condition": "netem_lag",
                "target_provider": TARGET,
                "injected_at_rel_s": 5.0,
                "cleared_at_rel_s": 40.0,
                "netem_delay_ms": 60000.0,
                # confusion_matrix's fault_unverified
                # gate requires inject_verified=True for a fault cell to
                # enter the truth population; the gate must change ZERO
                # outcomes on verified data.
                "inject_verified": True,
                "qdisc_snapshot": "qdisc netem 8001: root refcnt 2 limit 1000 delay 60.0s\n",
            },
        )

    def _recording_truth_both_emit_both(self):
        """On-diagonal: truth={both}, emit={both} (Example 3 shape,
        corroborated via E-health so emit's service_evidence fires
        despite agent_competence==fail)."""
        return _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0, target=WRONG_TARGET)],
            samples=_flat_samples(0, until=60.0, provider=TARGET),
            reported_done_at_rel_s=2.0,
            health_samples=[
                _health_sample(5.0, True),
                _health_sample(10.0, False),
                _health_sample(40.0, False),
            ],
            fault={
                "condition": "outage",
                "target_provider": TARGET,
                "injected_at_rel_s": 4.0,
                "cleared_at_rel_s": 45.0,
                "netem_delay_ms": None,
                "inject_verified": True,
                "qdisc_snapshot": "qdisc netem 8001: root refcnt 2 limit 1000 delay 60.0s\n",
            },
        )

    def _recording_truth_both_emit_agent_mismatch(self):
        """Off-diagonal WITHIN a ground-truthable row: truth={both} (wrong
        target + genuinely on-path overlapping fault) but emit={agent}
        (no observable corroborating trace anywhere -- no
        transfer_status_samples here, since a terminal "completed" status
        would now correctly fire E-status-shortfall -- the "uncorroborated
        both" case, a real estimator miss)."""
        return _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0, target=WRONG_TARGET)],
            samples=_flat_samples(0, until=60.0, probe_ok=True),
            reported_done_at_rel_s=2.0,
            fault={
                "condition": "outage",
                "target_provider": TARGET,
                "injected_at_rel_s": 4.0,
                "cleared_at_rel_s": 45.0,
                "netem_delay_ms": None,
                "inject_verified": True,
                "qdisc_snapshot": "qdisc netem 8001: root refcnt 2 limit 1000 delay 60.0s\n",
            },
        )

    def _recording_truth_neither_emit_service_wild(self):
        """The named honest off-diagonal: truth={neither} (wild --
        competent agent, no fault ever injected, still Violated) but
        emit={service} (the estimator cannot distinguish injected from
        spontaneous service failure)."""
        return _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=_flat_samples(0, until=60.0),
            reported_done_at_rel_s=2.0,
        )

    def test_matrix_wild_off_diagonal_and_mismatch_excluded_from_accuracy(self):
        recordings = [
            self._recording_truth_agent_emit_agent(),
            self._recording_truth_service_emit_service(),
            self._recording_truth_both_emit_both(),
            self._recording_truth_both_emit_agent_mismatch(),
            self._recording_truth_neither_emit_service_wild(),
        ]

        # Sanity-check the fixtures actually produce the shapes the test
        # name promises before scoring them.
        self.assertEqual(rq2.truth(recordings[0]), {TERM: "agent"})
        self.assertEqual(a3b.emit(recordings[0])["attribution_per_term"][TERM], "agent")
        self.assertEqual(rq2.truth(recordings[1]), {TERM: "service"})
        self.assertEqual(a3b.emit(recordings[1])["attribution_per_term"][TERM], "service")
        self.assertEqual(rq2.truth(recordings[2]), {TERM: "both"})
        self.assertEqual(a3b.emit(recordings[2])["attribution_per_term"][TERM], "both")
        self.assertEqual(rq2.truth(recordings[3]), {TERM: "both"})
        self.assertEqual(a3b.emit(recordings[3])["attribution_per_term"][TERM], "agent")
        self.assertEqual(rq2.truth(recordings[4]), {TERM: "neither"})
        self.assertEqual(a3b.emit(recordings[4])["attribution_per_term"][TERM], "service")

        result = rq2.confusion_matrix(recordings)
        matrix = result["matrix"]

        # All 16 cells present, zero-filled where unused.
        self.assertEqual(set(matrix.keys()), set(rq2.ATTRIBUTION_LABELS))
        for row in matrix.values():
            self.assertEqual(set(row.keys()), set(rq2.ATTRIBUTION_LABELS))

        self.assertEqual(matrix["agent"]["agent"], 1)
        self.assertEqual(matrix["service"]["service"], 1)
        self.assertEqual(matrix["both"]["both"], 1)
        self.assertEqual(matrix["both"]["agent"], 1)
        self.assertEqual(matrix["neither"]["service"], 1)

        # Ground-truthable rows: agent(1) + service(1) + both(2) = 4 terms;
        # 3 correct (agent/agent, service/service, both/both), 1 wrong
        # (both/agent) -> accuracy = 3/4. The wild {neither} row (1 term)
        # must NOT appear in this denominator.
        self.assertEqual(result["accuracy_ground_truthable"], 0.75)

        self.assertEqual(
            result["wild_row"],
            {"agent": 0, "service": 1, "both": 0, "neither": 0},
        )

        # Explicit proof the wild cell is excluded from the accuracy
        # denominator: ground-truthable total across {agent,service,both}
        # rows is 4, not 5 (which would be the case if {neither} leaked in).
        gt_total = sum(
            matrix[t][e] for t in ("agent", "service", "both") for e in rq2.ATTRIBUTION_LABELS
        )
        self.assertEqual(gt_total, 4)

    def test_confusion_matrix_empty_input(self):
        result = rq2.confusion_matrix([])
        for row in result["matrix"].values():
            self.assertEqual(set(row.values()), {0})
        self.assertIsNone(result["accuracy_ground_truthable"])
        self.assertEqual(result["wild_row"], {"agent": 0, "service": 0, "both": 0, "neither": 0})

    def test_confusion_matrix_only_wild_recordings_accuracy_is_none(self):
        """When every recording is wild (truth={neither}), the
        ground-truthable denominator is 0 -- accuracy must be None, not a
        spurious 0/0-derived 0.0 or 1.0."""
        recordings = [self._recording_truth_neither_emit_service_wild()]
        result = rq2.confusion_matrix(recordings)
        self.assertIsNone(result["accuracy_ground_truthable"])
        self.assertEqual(result["wild_row"]["service"], 1)


# =============================================================================
# 5. leg-level failures are excluded from the confusion matrix (no-data)
# =============================================================================

class TestConfusionMatrixLegErrors(unittest.TestCase):
    """A leg-level failure (agent.error set -- e.g. a Forge model that 400s
    because it's inactive/grant-scoped) must be excluded from RQ2 scoring
    as no-data, never scored as a truth/emit label pair, and tallied
    separately via `confusion_matrix()`'s `leg_errors` count."""

    def _leg_error_recording(self):
        rec = _recording(
            tool_calls=[],
            samples=_flat_samples(0, until=60.0),
            self_reported="error",
            reported_done_at_rel_s=0.0,
        )
        rec["trial_id"] = "trial-leg-error"
        rec["agent"]["error"] = "BadRequestError: 400 model currently inactive"
        return rec

    def test_leg_error_recording_excluded_and_tallied(self):
        clean = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0, target=WRONG_TARGET)],
            samples=_flat_samples(0, until=60.0, probe_ok=True),
            reported_done_at_rel_s=2.0,
        )
        leg_error_rec = self._leg_error_recording()

        result_clean_only = rq2.confusion_matrix([clean])
        result_with_leg_error = rq2.confusion_matrix([clean, leg_error_rec])

        # Clean-data-unaffected: the matrix + accuracy + wild_row are
        # IDENTICAL whether or not the leg-error trial is present -- the
        # exclusion is a no-op on clean data (this is why published RQ2
        # results are safe).
        self.assertEqual(result_clean_only["matrix"], result_with_leg_error["matrix"])
        self.assertEqual(
            result_clean_only["accuracy_ground_truthable"],
            result_with_leg_error["accuracy_ground_truthable"],
        )
        self.assertEqual(result_clean_only["wild_row"], result_with_leg_error["wild_row"])

        # The leg-error trial contributes to NEITHER the matrix NOR the
        # wild_row -- it is tallied separately, never a silent "neither".
        self.assertEqual(result_clean_only["leg_errors"], 0)
        self.assertEqual(result_with_leg_error["leg_errors"], 1)

    def test_all_leg_error_recordings_matrix_is_all_zero(self):
        result = rq2.confusion_matrix([self._leg_error_recording()])
        for row in result["matrix"].values():
            self.assertEqual(set(row.values()), {0})
        self.assertIsNone(result["accuracy_ground_truthable"])
        self.assertEqual(result["leg_errors"], 1)


# =============================================================================
# 6. unverified fault cells are excluded from the confusion matrix (no-data)
# =============================================================================

class TestConfusionMatrixFaultUnverified(unittest.TestCase):
    """A fault cell (`fault.condition != "none"`) whose `inject_verified` is
    not `True` (missing or False) must be excluded from RQ2 scoring as
    no-data -- never scored as a truth/emit label pair -- and tallied
    separately via `confusion_matrix()`'s `fault_unverified` count. Mirrors
    `TestConfusionMatrixLegErrors`'s shape for the leg_errors bucket."""

    def _service_recording(self, inject_verified):
        fault = {
            "condition": "netem_lag",
            "target_provider": TARGET,
            "injected_at_rel_s": 5.0,
            "cleared_at_rel_s": 40.0,
            "netem_delay_ms": 60000.0,
        }
        if inject_verified is not None:
            fault["inject_verified"] = inject_verified
            fault["qdisc_snapshot"] = (
                "qdisc netem 8001: root refcnt 2 limit 1000 delay 60.0s\n"
                if inject_verified
                else "qdisc noqueue 0: root refcnt 2\n"
            )
        rec = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=_flat_samples(0, until=60.0),
            reported_done_at_rel_s=2.0,
            fault=fault,
        )
        rec["trial_id"] = f"trial-fault-unverified-{inject_verified}"
        return rec

    def test_unverified_fault_excluded_and_tallied(self):
        clean = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0, target=WRONG_TARGET)],
            samples=_flat_samples(0, until=60.0, probe_ok=True),
            reported_done_at_rel_s=2.0,
        )
        unverified = self._service_recording(inject_verified=False)

        result_clean_only = rq2.confusion_matrix([clean])
        result_with_unverified = rq2.confusion_matrix([clean, unverified])

        # Clean-data-unaffected, same discipline as the leg_errors bucket:
        # the matrix + accuracy + wild_row are IDENTICAL whether or not the
        # unverified-fault trial is present.
        self.assertEqual(result_clean_only["matrix"], result_with_unverified["matrix"])
        self.assertEqual(
            result_clean_only["accuracy_ground_truthable"],
            result_with_unverified["accuracy_ground_truthable"],
        )
        self.assertEqual(result_clean_only["wild_row"], result_with_unverified["wild_row"])

        # The unverified-fault trial contributes to NEITHER the matrix NOR
        # the wild_row -- it is tallied separately, never a silent "neither".
        self.assertEqual(result_clean_only["fault_unverified"], 0)
        self.assertEqual(result_with_unverified["fault_unverified"], 1)

    def test_missing_inject_verified_is_also_excluded(self):
        # inject_verified absent entirely (not just False) must be treated
        # identically -- "missing" is never silently coerced to a pass.
        rec = self._service_recording(inject_verified=None)
        self.assertNotIn("inject_verified", rec["fault"])
        result = rq2.confusion_matrix([rec])
        self.assertEqual(result["fault_unverified"], 1)
        for row in result["matrix"].values():
            self.assertEqual(set(row.values()), {0})
        self.assertIsNone(result["accuracy_ground_truthable"])

    def test_verified_fault_behaves_exactly_as_today(self):
        # The gate must change ZERO
        # attribution outcomes vs ungated on verified data -- a fault cell
        # with inject_verified=True must land in the matrix exactly as it
        # did before this gate existed.
        verified = self._service_recording(inject_verified=True)
        result = rq2.confusion_matrix([verified])
        self.assertEqual(result["matrix"]["service"]["service"], 1)
        self.assertEqual(result["fault_unverified"], 0)

    def test_no_fault_cells_never_counted_as_unverified(self):
        # condition="none" cells never carry inject_verified at all -- the
        # gate must not misfire on the RQ1 baseline/no-injection population.
        no_fault = _recording(
            tool_calls=[_replication_call(ts_rel_s=1.0)],
            samples=_ramp_samples(step_at=10.0, until=60.0),
            reported_done_at_rel_s=2.0,
        )
        self.assertEqual(no_fault["fault"]["condition"], "none")
        result = rq2.confusion_matrix([no_fault])
        self.assertEqual(result["fault_unverified"], 0)


if __name__ == "__main__":
    unittest.main()
