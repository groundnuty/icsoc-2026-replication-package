"""Regression tests for harness/schema.py.

Stdlib `unittest` only — no pytest dependency in this repo. Run either as:

    python3 -m unittest harness.test_schema -v      # from repo root
    python3 harness/test_schema.py                  # directly as a script

Covers:
    - a minimal schema-valid E1/A0 placement-trial record (both the plain-dict
      form and the TrialRecord(...).as_dict() form)
    - round-trip via to_jsonl / from_jsonl
    - the 7 required negative-path invariant violations + a few extra
      sensible negatives
    - the bool-is-not-a-number guard (_is_number excludes bool)
"""
from __future__ import annotations

import copy
import os
import sys
import unittest

# --- make `harness` importable whether run as a script or as -m -------------
# When executed directly
# (python3 harness/test_schema.py) __package__ is None/"" and there's no
# enclosing package context for a relative import; when executed as
# `python3 -m unittest harness.test_schema` __package__ is "harness" and the
# relative import works normally.
if __package__ in (None, ""):
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    try:
        from harness import schema
    except ImportError:
        import schema
else:
    from . import schema


def valid_record() -> dict:
    """A minimal SCHEMA-VALID recording: one E1/A0 placement trial, term T1."""
    return {
        "trial_id": "trial-001",
        "sweep_id": "sweep-001",
        "recorded_at": "2026-07-05T00:00:00Z",
        "harness_version": "0.1.0",
        "trial_start_utc": "2026-07-05T00:00:00Z",
        "scenario_id": "E1",
        "arm_live": "A0",
        "model_leg": "sdk",
        "trial_k": 1,
        "source_provider": "provider-a",
        "agent_mcp_host": "host-1",
        "fault": {"condition": "none"},
        "contract": {
            "obligated_party": "provider-a",
            "terms": [
                {
                    "term_id": "T1",
                    "guarantee": "placement completes on target provider",
                    "slis": ["physicalSize"],
                    "slo": {"op": ">=", "value": "expected_size"},
                    "t_max_s": 30.0,
                    "remedy": "",
                }
            ],
        },
        "expected": {
            "fixture_space_id": "fx-1",
            "poll_until_rel_s": 30.0,
            "per_term": {
                "T1": {"expected_size": 42},
            },
        },
        "agent": {
            "prompt_verbatim": "replicate the fixture to the target provider",
            "tool_calls": [
                {"name": "list_spaces", "args": {}, "result": None, "ts_rel_s": 1.0, "ok": True},
                {"name": "get_file", "args": {}, "result": None, "ts_rel_s": 2.5, "ok": True},
            ],
            "messages": [],
            "final_answer": "done",
            "self_reported": "done",
            "reported_done_at_rel_s": 3.0,
            "raw_trace": "absent-by-design",
        },
        "state_timeline": {
            "poll_interval_s": 1.0,
            "poll_until_rel_s": 30.0,
            "polled_past_t_max": True,
            "samples": [
                {"t_rel_s": 0.0, "term_id": "T1", "sli_name": "physicalSize", "value": 0, "provider": "provider-a", "probe_ok": True},
                {"t_rel_s": 5.0, "term_id": "T1", "sli_name": "physicalSize", "value": 42, "provider": "provider-a", "probe_ok": True},
            ],
        },
        "cost": {
            "verifier": {"probe_count": 2, "wall_latency_s": 0.15},
            "model": {"prompt_tokens": 100, "completion_tokens": 50, "estimation": "measured", "usd": 0.01},
            "retry": {"attempts": 0, "model_usd": 0.0, "verifier_probe_count": 0},
        },
        "otel": {"harness_trace_id": "trace-1", "poll_interval_s": 1.0, "correlated": False},
        "leg_scaffold": "sdk_loop",
    }


def trial_record_as_dict() -> dict:
    """Same record, built via the TrialRecord dataclass + .as_dict()."""
    tr = schema.TrialRecord(
        trial_id="trial-001",
        sweep_id="sweep-001",
        recorded_at="2026-07-05T00:00:00Z",
        harness_version="0.1.0",
        trial_start_utc="2026-07-05T00:00:00Z",
        scenario_id="E1",
        arm_live="A0",
        model_leg="sdk",
        trial_k=1,
        source_provider="provider-a",
        agent_mcp_host="host-1",
        fault={"condition": "none"},
        contract={
            "obligated_party": "provider-a",
            "terms": [
                {
                    "term_id": "T1",
                    "guarantee": "placement completes on target provider",
                    "slis": ["physicalSize"],
                    "slo": {"op": ">=", "value": "expected_size"},
                    "t_max_s": 30.0,
                    "remedy": "",
                }
            ],
        },
        expected={
            "fixture_space_id": "fx-1",
            "poll_until_rel_s": 30.0,
            "per_term": {
                "T1": {"expected_size": 42},
            },
        },
        agent={
            "prompt_verbatim": "replicate the fixture to the target provider",
            "tool_calls": [
                {"name": "list_spaces", "args": {}, "result": None, "ts_rel_s": 1.0, "ok": True},
                {"name": "get_file", "args": {}, "result": None, "ts_rel_s": 2.5, "ok": True},
            ],
            "messages": [],
            "final_answer": "done",
            "self_reported": "done",
            "reported_done_at_rel_s": 3.0,
            "raw_trace": "absent-by-design",
        },
        state_timeline={
            "poll_interval_s": 1.0,
            "poll_until_rel_s": 30.0,
            "polled_past_t_max": True,
            "samples": [
                {"t_rel_s": 0.0, "term_id": "T1", "sli_name": "physicalSize", "value": 0, "provider": "provider-a", "probe_ok": True},
                {"t_rel_s": 5.0, "term_id": "T1", "sli_name": "physicalSize", "value": 42, "provider": "provider-a", "probe_ok": True},
            ],
        },
        cost={
            "verifier": {"probe_count": 2, "wall_latency_s": 0.15},
            "model": {"prompt_tokens": 100, "completion_tokens": 50, "estimation": "measured", "usd": 0.01},
            "retry": {"attempts": 0, "model_usd": 0.0, "verifier_probe_count": 0},
        },
        otel={"harness_trace_id": "trace-1", "poll_interval_s": 1.0, "correlated": False},
        leg_scaffold="sdk_loop",
    )
    return tr.as_dict()


class TestValidRecordShapes(unittest.TestCase):
    """Positive-path tests: both construction routes validate + round-trip."""

    def test_plain_dict_record_validates(self):
        schema.validate(valid_record())  # must not raise

    def test_trial_record_as_dict_validates(self):
        schema.validate(trial_record_as_dict())  # must not raise

    def test_jsonl_round_trip(self):
        rec = valid_record()
        rec_back = schema.from_jsonl(schema.to_jsonl(rec))
        self.assertEqual(rec_back, rec)

    def test_jsonl_round_trip_of_trial_record_as_dict(self):
        rec = trial_record_as_dict()
        rec_back = schema.from_jsonl(schema.to_jsonl(rec))
        self.assertEqual(rec_back, rec)
        schema.validate(rec_back)  # still valid after the round trip


class TestNegativePaths(unittest.TestCase):
    """The 7 required negative-path invariant violations, plus extras.

    Each test starts from a fresh valid_record() and mutates exactly one
    thing, then asserts SchemaError is raised.
    """

    # 1. missing trial_start_utc
    def test_missing_trial_start_utc(self):
        rec = valid_record()
        del rec["trial_start_utc"]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    # 2. bad arm_live
    def test_bad_arm_live(self):
        rec = valid_record()
        rec["arm_live"] = "A9"
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    # 3. horizon violation: state_timeline.poll_until_rel_s < max(contract.terms[].t_max_s)
    def test_horizon_violation(self):
        rec = valid_record()
        self.assertEqual(rec["contract"]["terms"][0]["t_max_s"], 30.0)
        rec["state_timeline"]["poll_until_rel_s"] = 5.0  # < 30.0 -> violates the invariant
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    # 4. missing expected_size AND expected_metadata on a per_term entry
    def test_per_term_missing_size_and_metadata(self):
        rec = valid_record()
        del rec["expected"]["per_term"]["T1"]["expected_size"]
        # no expected_metadata present either -> neither branch satisfied
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    # 5. a tool_calls[] entry missing ts_rel_s
    def test_tool_call_missing_ts_rel_s(self):
        rec = valid_record()
        del rec["agent"]["tool_calls"][0]["ts_rel_s"]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    # 6. bad raw_trace type (not "absent-by-design" and not a dict)
    def test_bad_raw_trace_type(self):
        rec = valid_record()
        rec["agent"]["raw_trace"] = 5
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    # 7. a non-"none" fault.condition but injected_at_rel_s missing
    def test_fault_condition_without_injected_at(self):
        rec = valid_record()
        rec["fault"] = {"condition": "outage"}  # no injected_at_rel_s
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    # --- extra sensible negatives beyond the required 7 ---------------------

    def test_empty_contract_terms(self):
        rec = valid_record()
        rec["contract"]["terms"] = []
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_non_numeric_t_max_s(self):
        rec = valid_record()
        rec["contract"]["terms"][0]["t_max_s"] = "thirty"
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_invalid_arm_class(self):
        rec = valid_record()
        rec["arm_class"] = "not_a_real_class"
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_missing_cost_bucket(self):
        rec = valid_record()
        del rec["cost"]["retry"]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    # --- probe_ok / value invariant negatives -------------------------------

    def test_sample_missing_probe_ok(self):
        rec = valid_record()
        del rec["state_timeline"]["samples"][0]["probe_ok"]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_sample_probe_ok_false_with_numeric_value(self):
        rec = valid_record()
        rec["state_timeline"]["samples"][0]["probe_ok"] = False
        rec["state_timeline"]["samples"][0]["value"] = 0  # must be null when probe_ok is False
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_sample_probe_ok_true_with_null_value(self):
        rec = valid_record()
        rec["state_timeline"]["samples"][0]["probe_ok"] = True
        rec["state_timeline"]["samples"][0]["value"] = None  # must be numeric when probe_ok is True
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    # --- leg_scaffold / otel.correlated negatives ---------------------------

    def test_missing_leg_scaffold(self):
        rec = valid_record()
        del rec["leg_scaffold"]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_bad_leg_scaffold_enum(self):
        rec = valid_record()
        rec["leg_scaffold"] = "not_a_real_scaffold"
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_missing_otel_correlated(self):
        rec = valid_record()
        del rec["otel"]["correlated"]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_non_bool_otel_correlated(self):
        rec = valid_record()
        rec["otel"]["correlated"] = "yes"
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)


class TestBoolIsNotANumber(unittest.TestCase):
    """schema._is_number excludes bool — a numeric field set to True must fail.

    In Python, bool is a subclass of int, so a naive isinstance(x, (int, float))
    check would silently accept True/False as numbers. schema._is_number()
    explicitly guards against this; this test locks that guard in at the
    validate() level (not just unit-testing _is_number directly).
    """

    def test_t_max_s_as_bool_fails(self):
        rec = valid_record()
        rec["contract"]["terms"][0]["t_max_s"] = True
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_reported_done_at_rel_s_as_bool_fails(self):
        rec = valid_record()
        rec["agent"]["reported_done_at_rel_s"] = False
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_is_number_helper_directly_rejects_bool(self):
        self.assertFalse(schema._is_number(True))
        self.assertFalse(schema._is_number(False))
        self.assertTrue(schema._is_number(0))
        self.assertTrue(schema._is_number(0.0))


class TestValidRecordIsUnmutatedByCopy(unittest.TestCase):
    """Sanity check that valid_record() returns an independent dict each call,
    so mutating one test's record can't leak into another test."""

    def test_two_calls_are_independent(self):
        a = valid_record()
        b = valid_record()
        a["arm_live"] = "A4"
        self.assertEqual(b["arm_live"], "A0")
        # also check nested mutation doesn't leak (would indicate accidental
        # shared-reference state in valid_record()'s literals)
        a["contract"]["terms"][0]["t_max_s"] = 999.0
        self.assertEqual(b["contract"]["terms"][0]["t_max_s"], 30.0)

    def test_deepcopy_not_required_between_helper_calls(self):
        # valid_record() builds a fresh literal every call; copy.deepcopy is
        # not needed, but confirm one wouldn't change anything either.
        a = valid_record()
        b = copy.deepcopy(a)
        self.assertEqual(a, b)


class TestSliValueTyping(unittest.TestCase):
    """Increment-4: sample `value` type is keyed on `sli_name` when probe_ok is
    True. physicalSize numeric, provider_health bool, replicationStatus string;
    unknown SLIs require only a non-null value."""

    def _with_extra_sample(self, sample: dict) -> dict:
        rec = valid_record()
        rec["state_timeline"]["samples"].append(sample)
        return rec

    def test_provider_health_bool_sample_validates(self):
        rec = self._with_extra_sample(
            {"t_rel_s": 6.0, "term_id": "T1", "sli_name": "provider_health",
             "value": True, "provider": "provider-a", "probe_ok": True}
        )
        schema.validate(rec)  # no raise

    def test_provider_health_non_bool_value_fails(self):
        rec = self._with_extra_sample(
            {"t_rel_s": 6.0, "term_id": "T1", "sli_name": "provider_health",
             "value": 1, "provider": "provider-a", "probe_ok": True}  # int, not bool
        )
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_physical_size_bool_value_fails(self):
        # bool is not a number (schema._is_number excludes it) — the placement
        # predicate must not silently accept True as a size.
        rec = valid_record()
        rec["state_timeline"]["samples"][1]["value"] = True
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_unknown_sli_requires_non_null_value(self):
        ok = self._with_extra_sample(
            {"t_rel_s": 6.0, "term_id": "T1", "sli_name": "someFutureSli",
             "value": {"nested": 1}, "provider": "provider-a", "probe_ok": True}
        )
        schema.validate(ok)  # forward-compatible: non-null value accepted
        bad = self._with_extra_sample(
            {"t_rel_s": 6.0, "term_id": "T1", "sli_name": "someFutureSli",
             "value": None, "provider": "provider-a", "probe_ok": True}
        )
        with self.assertRaises(schema.SchemaError):
            schema.validate(bad)


def _status_sample(t_rel_s, value="completed", probe_ok=True, provider="provider-a", term_id="T1"):
    """Matches the REAL producer shape (runner._poll_transfer_status_diagnostic):
    sli_name "get_transfer.replicationStatus", probe_ok/value discipline where
    value is the status string enum."""
    return {
        "t_rel_s": t_rel_s,
        "term_id": term_id,
        "sli_name": "get_transfer.replicationStatus",
        "value": value,
        "provider": provider,
        "probe_ok": probe_ok,
    }


class TestTransferStatusSamples(unittest.TestCase):
    """replicationStatus lives in the separate diagnostic
    state_timeline.transfer_status_samples array, NOT the
    main samples[] SLI stream (its value is a string, not the numeric
    physicalSize predicate). Entries mirror the main-stream probe_ok/value
    discipline with a STRING value — the real runner producer shape."""

    def test_absent_is_ok(self):
        rec = valid_record()
        self.assertNotIn("transfer_status_samples", rec["state_timeline"])
        schema.validate(rec)  # no raise — the array is optional

    def test_valid_transfer_status_samples_validate(self):
        rec = valid_record()
        rec["state_timeline"]["transfer_status_samples"] = [
            _status_sample(5.0, "replicating"),
            _status_sample(10.0, "completed"),
        ]
        schema.validate(rec)  # no raise

    def test_probe_failure_null_value_validates(self):
        rec = valid_record()
        rec["state_timeline"]["transfer_status_samples"] = [
            _status_sample(5.0, value=None, probe_ok=False),
        ]
        schema.validate(rec)  # no raise — a dead diagnostic probe is null

    def test_non_list_fails(self):
        rec = valid_record()
        rec["state_timeline"]["transfer_status_samples"] = {"not": "a list"}
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_missing_t_rel_s_fails(self):
        rec = valid_record()
        entry = _status_sample(5.0)
        del entry["t_rel_s"]
        rec["state_timeline"]["transfer_status_samples"] = [entry]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_missing_probe_ok_fails(self):
        rec = valid_record()
        entry = _status_sample(5.0)
        del entry["probe_ok"]
        rec["state_timeline"]["transfer_status_samples"] = [entry]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_probe_ok_true_empty_string_value_fails(self):
        rec = valid_record()
        rec["state_timeline"]["transfer_status_samples"] = [_status_sample(5.0, value="")]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_probe_ok_true_numeric_value_fails(self):
        rec = valid_record()
        rec["state_timeline"]["transfer_status_samples"] = [_status_sample(5.0, value=5)]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_probe_ok_false_with_non_null_value_fails(self):
        rec = valid_record()
        rec["state_timeline"]["transfer_status_samples"] = [
            _status_sample(5.0, value="completed", probe_ok=False)
        ]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)


def _residue_sample(t_rel_s, probe_ok=True, path_absent=False, total_residue=0, per_provider=None):
    """Matches the E3 producer shape (runner._residue_sample_dict /
    Verifier.probe_residue): probe_ok True -> the three data fields present;
    probe_ok False -> all three null."""
    if not probe_ok:
        return {"t_rel_s": t_rel_s, "probe_ok": False, "path_absent": None,
                "total_residue": None, "per_provider": None}
    return {
        "t_rel_s": t_rel_s,
        "probe_ok": True,
        "path_absent": path_absent,
        "total_residue": total_residue,
        "per_provider": per_provider if per_provider is not None else {"provider-a": total_residue},
    }


class TestResidueSamples(unittest.TestCase):
    """state_timeline.residue_samples
    is a NEW optional stream — mirrors the transfer_status_samples probe_ok
    discipline but with three data fields (path_absent/total_residue/
    per_provider) instead of one."""

    def test_absent_is_ok(self):
        rec = valid_record()
        self.assertNotIn("residue_samples", rec["state_timeline"])
        schema.validate(rec)  # no raise — the array is optional

    def test_valid_removed_sample_validates(self):
        rec = valid_record()
        rec["state_timeline"]["residue_samples"] = [
            _residue_sample(0.0, path_absent=False, total_residue=4096, per_provider={"provider-a": 4096}),
            _residue_sample(5.0, path_absent=True, total_residue=0, per_provider={"provider-a": 0}),
        ]
        schema.validate(rec)  # no raise

    def test_probe_failure_all_null_validates(self):
        rec = valid_record()
        rec["state_timeline"]["residue_samples"] = [_residue_sample(5.0, probe_ok=False)]
        schema.validate(rec)  # no raise — a dead probe nulls all three fields

    def test_non_list_fails(self):
        rec = valid_record()
        rec["state_timeline"]["residue_samples"] = {"not": "a list"}
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_missing_t_rel_s_fails(self):
        rec = valid_record()
        entry = _residue_sample(5.0)
        del entry["t_rel_s"]
        rec["state_timeline"]["residue_samples"] = [entry]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_missing_probe_ok_fails(self):
        rec = valid_record()
        entry = _residue_sample(5.0)
        del entry["probe_ok"]
        rec["state_timeline"]["residue_samples"] = [entry]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_probe_ok_true_non_bool_path_absent_fails(self):
        rec = valid_record()
        entry = _residue_sample(5.0)
        entry["path_absent"] = "yes"
        rec["state_timeline"]["residue_samples"] = [entry]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_probe_ok_true_negative_total_residue_fails(self):
        rec = valid_record()
        entry = _residue_sample(5.0, total_residue=-1)
        rec["state_timeline"]["residue_samples"] = [entry]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_probe_ok_true_bool_total_residue_fails(self):
        # bool is not an int here either (schema._is_number-style guard) --
        # a numeric field set to True must fail.
        rec = valid_record()
        entry = _residue_sample(5.0)
        entry["total_residue"] = True
        rec["state_timeline"]["residue_samples"] = [entry]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_probe_ok_true_non_dict_per_provider_fails(self):
        rec = valid_record()
        entry = _residue_sample(5.0)
        entry["per_provider"] = "not-a-dict"
        rec["state_timeline"]["residue_samples"] = [entry]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_probe_ok_false_with_non_null_path_absent_fails(self):
        rec = valid_record()
        entry = _residue_sample(5.0, probe_ok=False)
        entry["path_absent"] = False
        rec["state_timeline"]["residue_samples"] = [entry]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_probe_ok_false_with_non_null_total_residue_fails(self):
        rec = valid_record()
        entry = _residue_sample(5.0, probe_ok=False)
        entry["total_residue"] = 0
        rec["state_timeline"]["residue_samples"] = [entry]
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)


class TestCrossNamespaceGuard(unittest.TestCase):
    """A placement term whose target_providers
    is disjoint from its own sample providers is the label-vs-ID namespace bug
    → data-quality error. Zero samples for the term is a distinct condition and
    is not flagged."""

    def test_matching_namespace_passes(self):
        rec = valid_record()
        # samples are on "provider-a"; declare the target in the same namespace
        rec["expected"]["per_term"]["T1"]["target_providers"] = ["provider-a"]
        schema.validate(rec)  # no raise

    def test_disjoint_namespace_raises(self):
        rec = valid_record()
        rec["expected"]["per_term"]["T1"]["target_providers"] = ["de"]  # a label
        with self.assertRaises(schema.SchemaError) as cm:
            schema.validate(rec)
        self.assertIn("cross-namespace", str(cm.exception))

    def test_no_target_providers_is_not_checked(self):
        # base valid_record has expected_size but no target_providers (metadata
        # /unspecified-target term) — guard must skip it.
        rec = valid_record()
        self.assertNotIn("target_providers", rec["expected"]["per_term"]["T1"])
        schema.validate(rec)  # no raise

    def test_zero_samples_for_term_is_not_flagged(self):
        # a never-probed / all-failed term has no namespace to compare against
        rec = valid_record()
        rec["expected"]["per_term"]["T1"]["target_providers"] = ["de"]
        rec["state_timeline"]["samples"] = []
        schema.validate(rec)  # no raise (no samples → no namespace mismatch)

    def test_intersection_nonempty_passes(self):
        # target set that partially overlaps the sample providers still passes
        rec = valid_record()
        rec["expected"]["per_term"]["T1"]["target_providers"] = ["provider-a", "de"]
        schema.validate(rec)  # no raise — intersection is non-empty


class TestFaultCrossNamespaceGuard(unittest.TestCase):
    """The label-vs-ID namespace bug recurring in a DIFFERENT field:
    fault.target_provider was recorded as a human LABEL ("de") while
    state_timeline.samples[].provider carries the provider-ID hash namespace,
    silently making the RQ2 truth-labeler read an injected fault as
    un-injected. Mirrors TestCrossNamespaceGuard's shape for the fault block."""

    def _verified_netem_fields(self):
        """The mandatory inject-verification fields,
        added to every non-"none" fault fixture below so THIS class keeps
        testing the cross-namespace guard specifically, not tripping over
        the separate inject-verification requirement first."""
        return {
            "inject_verified": True,
            "qdisc_snapshot": "qdisc netem 8001: root refcnt 2 limit 1000 delay 40.0s\n",
        }

    def test_matching_namespace_passes(self):
        rec = valid_record()
        # samples are on "provider-a"; fault target in the same namespace
        rec["fault"] = {
            "condition": "netem_lag", "injected_at_rel_s": 1.0, "target_provider": "provider-a",
            **self._verified_netem_fields(),
        }
        schema.validate(rec)  # no raise

    def test_disjoint_namespace_raises(self):
        rec = valid_record()
        rec["fault"] = {
            "condition": "netem_lag", "injected_at_rel_s": 1.0, "target_provider": "de",  # a label
            **self._verified_netem_fields(),
        }
        with self.assertRaises(schema.SchemaError) as cm:
            schema.validate(rec)
        self.assertIn("cross-namespace", str(cm.exception))

    def test_target_not_a_contracted_target_provider_still_passes(self):
        # fault.target_provider need not be a member of any term's
        # target_providers — only in the SAME NAMESPACE as recorded sample
        # providers. Add a second sample provider not in any term's targets.
        rec = valid_record()
        rec["state_timeline"]["samples"].append(
            {"t_rel_s": 6.0, "term_id": "T1", "sli_name": "physicalSize",
             "value": 42, "provider": "provider-b", "probe_ok": True}
        )
        rec["fault"] = {
            "condition": "netem_lag", "injected_at_rel_s": 1.0, "target_provider": "provider-b",
            **self._verified_netem_fields(),
        }
        schema.validate(rec)  # no raise — provider-b is in-namespace even if uncontracted

    def test_condition_none_is_not_checked(self):
        rec = valid_record()
        self.assertEqual(rec["fault"], {"condition": "none"})
        # no target_provider at all -> guard doesn't apply regardless
        schema.validate(rec)  # no raise

    def test_zero_samples_is_not_flagged(self):
        # no namespace to compare against -> the guard is skipped, same as
        # the expected.per_term guard's zero-samples skip
        rec = valid_record()
        rec["fault"] = {
            "condition": "netem_lag", "injected_at_rel_s": 1.0, "target_provider": "de",
            **self._verified_netem_fields(),
        }
        rec["state_timeline"]["samples"] = []
        schema.validate(rec)  # no raise


class TestFaultInjectVerification(unittest.TestCase):
    """A fault cell (`condition != "none"`) must carry `inject_verified` (bool)
    + `qdisc_snapshot` (str) — evidence the qdisc actually carried netem,
    never just the injector's add-rc=0 claim."""

    def _base_fault_fields(self):
        return {"condition": "netem_lag", "injected_at_rel_s": 1.0, "target_provider": "provider-a"}

    def test_netem_lag_with_inject_verified_and_snapshot_validates(self):
        rec = valid_record()
        rec["fault"] = {
            **self._base_fault_fields(),
            "inject_verified": True,
            "qdisc_snapshot": "qdisc netem 8001: root refcnt 2 limit 1000 delay 40.0s\n",
        }
        schema.validate(rec)  # no raise

    def test_missing_inject_verified_raises(self):
        rec = valid_record()
        rec["fault"] = {
            **self._base_fault_fields(),
            "qdisc_snapshot": "qdisc netem 8001: root refcnt 2 limit 1000 delay 40.0s\n",
        }
        with self.assertRaises(schema.SchemaError) as cm:
            schema.validate(rec)
        self.assertIn("inject_verified", str(cm.exception))

    def test_missing_qdisc_snapshot_raises(self):
        rec = valid_record()
        rec["fault"] = {**self._base_fault_fields(), "inject_verified": True}
        with self.assertRaises(schema.SchemaError) as cm:
            schema.validate(rec)
        self.assertIn("qdisc_snapshot", str(cm.exception))

    def test_non_bool_inject_verified_raises(self):
        rec = valid_record()
        rec["fault"] = {
            **self._base_fault_fields(),
            "inject_verified": "true",  # string, not bool
            "qdisc_snapshot": "qdisc netem 8001: root refcnt 2 limit 1000 delay 40.0s\n",
        }
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_inject_verified_false_still_validates(self):
        # False is a legitimate (if unwelcome) recorded outcome -- the GATE
        # on what a False cell may be used for lives in rq2.py, not here.
        rec = valid_record()
        rec["fault"] = {
            **self._base_fault_fields(),
            "inject_verified": False,
            "qdisc_snapshot": "qdisc noqueue 0: root refcnt 2\n",
        }
        schema.validate(rec)  # no raise

    def test_condition_none_does_not_require_inject_verified(self):
        rec = valid_record()
        self.assertEqual(rec["fault"], {"condition": "none"})
        schema.validate(rec)  # no raise — condition="none" cells are exempt


class TestA2ResultField(unittest.TestCase):
    """a2_result is an OPTIONAL top-level field (lenses/a2.py: the one
    non-re-derivable lens — RECORDED at record-time, never recomputed).
    Absent records still validate; present records get their shape checked."""

    def _valid_a2_result(self) -> dict:
        return {
            "verdict_per_term": {"T1": "Fulfilled"},
            "raw_response": '{"verdicts": {"T1": "Fulfilled"}, "rationale": "ok"}',
            "model_id": "claude-sonnet-5",
            "parse_error": False,
        }

    def test_absent_a2_result_validates(self):
        rec = valid_record()
        self.assertNotIn("a2_result", rec)
        schema.validate(rec)  # no raise

    def test_valid_a2_result_validates(self):
        rec = valid_record()
        rec["a2_result"] = self._valid_a2_result()
        schema.validate(rec)  # no raise

    def test_valid_a2_result_with_prompt_sha_validates(self):
        rec = valid_record()
        a2_result = self._valid_a2_result()
        a2_result["prompt_sha"] = "sha256:abc123"
        rec["a2_result"] = a2_result
        schema.validate(rec)  # no raise

    def test_bad_verdict_vocab_in_verdict_per_term_raises(self):
        rec = valid_record()
        a2_result = self._valid_a2_result()
        a2_result["verdict_per_term"]["T1"] = "Maybe"  # out of vocab
        rec["a2_result"] = a2_result
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_non_bool_parse_error_raises(self):
        rec = valid_record()
        a2_result = self._valid_a2_result()
        a2_result["parse_error"] = "false"  # string, not bool
        rec["a2_result"] = a2_result
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_non_dict_a2_result_raises(self):
        rec = valid_record()
        rec["a2_result"] = "not a dict"
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_non_string_prompt_sha_raises(self):
        rec = valid_record()
        a2_result = self._valid_a2_result()
        a2_result["prompt_sha"] = 12345
        rec["a2_result"] = a2_result
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_empty_verdict_per_term_with_nonempty_terms_raises(self):
        rec = valid_record()
        a2_result = self._valid_a2_result()
        a2_result["verdict_per_term"] = {}  # valid_record() has a non-empty
        rec["a2_result"] = a2_result        # contract.terms -> must not be empty
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_missing_raw_response_raises(self):
        rec = valid_record()
        a2_result = self._valid_a2_result()
        del a2_result["raw_response"]
        rec["a2_result"] = a2_result
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_missing_model_id_raises(self):
        rec = valid_record()
        a2_result = self._valid_a2_result()
        del a2_result["model_id"]
        rec["a2_result"] = a2_result
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_missing_verdict_per_term_raises(self):
        rec = valid_record()
        a2_result = self._valid_a2_result()
        del a2_result["verdict_per_term"]
        rec["a2_result"] = a2_result
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)


class TestAgentErrorField(unittest.TestCase):
    """agent.error is an OPTIONAL leg-level-failure detail field (panel.py's
    LegResult.error, propagated via LegResult.as_agent_dict) -- absent/None
    means the leg ran without an adapter/API-level failure; when present and
    non-None it must be a string. schema.is_leg_error(record) is the
    discriminator scorers (aggregate.py/rq2.py/panel_score.py) key on to
    exclude these trials from competence/attribution scoring as no-data."""

    def test_absent_error_validates(self):
        rec = valid_record()
        self.assertNotIn("error", rec["agent"])
        schema.validate(rec)  # no raise
        self.assertFalse(schema.is_leg_error(rec))

    def test_none_error_validates(self):
        rec = valid_record()
        rec["agent"]["error"] = None
        schema.validate(rec)  # no raise
        self.assertFalse(schema.is_leg_error(rec))

    def test_string_error_validates_and_flags_leg_error(self):
        rec = valid_record()
        rec["agent"]["error"] = "BadRequestError: 400 model currently inactive"
        schema.validate(rec)  # no raise
        self.assertTrue(schema.is_leg_error(rec))

    def test_non_string_error_raises(self):
        rec = valid_record()
        rec["agent"]["error"] = {"not": "a string"}
        with self.assertRaises(schema.SchemaError):
            schema.validate(rec)

    def test_is_leg_error_false_on_missing_agent_block(self):
        # Defensive: must not raise on a malformed/absent agent block.
        self.assertFalse(schema.is_leg_error({}))


class TestValidatedIngest(unittest.TestCase):
    """aggregate.validated_ingest partitions valid vs excluded vs leg-error
    without raising, surfacing the exclusion reason / leg-error detail (no
    silent drops, no silent competence-0 scoring of a leg-level failure)."""

    def setUp(self):
        # import here so a schema-only test run doesn't require the lenses pkg
        if __package__ in (None, ""):
            from harness import aggregate
        else:
            from . import aggregate
        self.aggregate = aggregate

    def test_valid_and_excluded_are_partitioned(self):
        good = valid_record()
        bad = valid_record()
        bad["trial_id"] = "trial-bad"
        bad["expected"]["per_term"]["T1"]["target_providers"] = ["de"]  # namespace bug
        out = self.aggregate.validated_ingest([good, bad])
        self.assertEqual(len(out["valid"]), 1)
        self.assertEqual(len(out["excluded"]), 1)
        self.assertEqual(out["excluded"][0]["trial_id"], "trial-bad")
        self.assertIn("cross-namespace", out["excluded"][0]["reason"])
        self.assertEqual(out["leg_errors"], [])

    def test_never_raises_on_bad_record(self):
        out = self.aggregate.validated_ingest([{"garbage": True}])
        self.assertEqual(out["valid"], [])
        self.assertEqual(len(out["excluded"]), 1)
        self.assertEqual(out["leg_errors"], [])

    def test_leg_error_trial_routed_to_leg_errors_not_valid(self):
        good = valid_record()
        leg_error_rec = valid_record()
        leg_error_rec["trial_id"] = "trial-leg-error"
        leg_error_rec["model_leg"] = "forge:zai-org/GLM-5.2-FP8"
        leg_error_rec["agent"]["error"] = "BadRequestError: 400 not available for grant"
        out = self.aggregate.validated_ingest([good, leg_error_rec])

        self.assertEqual(len(out["valid"]), 1)
        self.assertEqual(out["valid"][0]["trial_id"], "trial-001")
        self.assertEqual(out["excluded"], [])
        self.assertEqual(len(out["leg_errors"]), 1)
        self.assertEqual(out["leg_errors"][0], {
            "trial_id": "trial-leg-error",
            "model_leg": "forge:zai-org/GLM-5.2-FP8",
            "error": "BadRequestError: 400 not available for grant",
        })

    def test_no_leg_error_trials_leaves_valid_unaffected(self):
        good1 = valid_record()
        good2 = valid_record()
        good2["trial_id"] = "trial-002"
        out = self.aggregate.validated_ingest([good1, good2])
        self.assertEqual(len(out["valid"]), 2)
        self.assertEqual(out["excluded"], [])
        self.assertEqual(out["leg_errors"], [])


if __name__ == "__main__":
    unittest.main()
