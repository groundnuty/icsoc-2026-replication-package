"""Offline tests for harness/a2_judge.py — the batch A2-application helper +
prompt_sha, exercised with a STUB judge (no SDK, no network). The live SDK path
(_run_judge_async / make_sdk_judge) is validated by a separate live smoke, not
here.

Runs under stdlib python (a2_judge imports the SDK lazily, live-path only).
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
        from harness import a2_judge, schema
    except ImportError:
        import a2_judge
        import schema
else:
    from . import a2_judge, schema


def _recording():
    return {
        "contract": {"terms": [{
            "term_id": "T1",
            "guarantee": "data physically present on >=2 distinct providers",
            "slo": {"predicate": "physicalSize == expected_size", "min_distinct_providers": 2,
                    "target_providers": ["de"]},
            "t_max_s": 30.0,
        }]},
        "expected": {"per_term": {"T1": {"expected_size": 4096, "target_providers": ["de"]}}},
        "agent": {
            "prompt_verbatim": "Replicate the file to de and confirm.",
            "tool_calls": [{"name": "schedule_file_replication", "args": {"target": "de"},
                            "result": '{"transferId":"x"}', "ts_rel_s": 2.0, "ok": True}],
            "messages": [], "final_answer": "done", "self_reported": "done",
            "reported_done_at_rel_s": 3.0,
        },
    }


class TestPromptSha(unittest.TestCase):
    def test_stable_12_hex(self):
        a, b = a2_judge.prompt_sha(), a2_judge.prompt_sha()
        self.assertEqual(a, b)
        self.assertEqual(len(a), 12)
        int(a, 16)  # is hex


class TestApplyA2(unittest.TestCase):
    def test_builds_a2_result_shape(self):
        judge = lambda s, u: '{"verdicts": {"T1": "Violated"}, "rationale": "never converged"}'
        a2r = a2_judge.apply_a2(_recording(), judge=judge)
        self.assertEqual(a2r["verdict_per_term"], {"T1": "Violated"})
        self.assertFalse(a2r["parse_error"])
        self.assertEqual(a2r["model_id"], a2_judge.JUDGE_MODEL_ID)
        self.assertEqual(a2r["prompt_sha"], a2_judge.prompt_sha())
        self.assertEqual(a2r["temperature"], "sdk-default(unspecified)")  # never a fake 0.0
        self.assertIn("never converged", a2r["raw_response"])
        # verdict_per_term must NOT carry the a2_parse_error meta key
        self.assertNotIn("a2_parse_error", a2r["verdict_per_term"])

    def test_parse_error_surfaced(self):
        judge = lambda s, u: "not json"
        a2r = a2_judge.apply_a2(_recording(), judge=judge)
        self.assertTrue(a2r["parse_error"])
        self.assertEqual(a2r["verdict_per_term"], {"T1": "NotDetermined"})

    def test_output_validates_against_schema_a2_result(self):
        # apply_a2's output must be a schema-valid a2_result field
        judge = lambda s, u: '{"verdicts": {"T1": "Fulfilled"}, "rationale": "ok"}'
        a2r = a2_judge.apply_a2(_recording(), judge=judge)
        rec = _valid_full_record()
        rec["a2_result"] = a2r
        schema.validate(rec)  # no raise

    def test_judge_receives_boundary_safe_prompt(self):
        # the judge must not receive withheld values (resolved target ids /
        # expected_size / timeline / fault) — a2.render_prompt guarantees this;
        # assert it here at the apply_a2 boundary too.
        seen = {}
        def judge(system, user):
            seen["blob"] = system + "\n" + user
            return '{"verdicts": {"T1": "Fulfilled"}}'
        rec = _recording()
        rec["expected"]["per_term"]["T1"]["expected_size"] = 987654321
        a2_judge.apply_a2(rec, judge=judge)
        self.assertNotIn("987654321", seen["blob"])          # concrete expected_size
        self.assertIn("physicalSize == expected_size", seen["blob"])  # symbolic slo present


def _valid_full_record():
    """A minimal schema-valid record to attach a2_result onto."""
    return {
        "trial_id": "t", "sweep_id": "s", "recorded_at": "2026-07-06T00:00:00Z",
        "harness_version": "0.1.0", "trial_start_utc": "2026-07-06T00:00:00Z",
        "scenario_id": "E1", "arm_live": "A0", "model_leg": "sdk", "trial_k": 1,
        "source_provider": "cloud-pl", "agent_mcp_host": "h", "fault": {"condition": "none"},
        "contract": {"obligated_party": "agent", "terms": [
            {"term_id": "T1", "guarantee": "g", "slis": ["physicalSize"],
             "slo": {"predicate": "physicalSize == expected_size"}, "t_max_s": 30.0}]},
        "expected": {"fixture_space_id": "fx", "poll_until_rel_s": 30.0,
                     "per_term": {"T1": {"expected_size": 4096}}},
        "agent": {"prompt_verbatim": "p", "tool_calls": [], "messages": [],
                  "final_answer": "done", "self_reported": "done", "reported_done_at_rel_s": 3.0,
                  "raw_trace": "absent-by-design"},
        "state_timeline": {"poll_interval_s": 1.0, "poll_until_rel_s": 30.0,
                           "polled_past_t_max": True, "samples": []},
        "cost": {"verifier": {}, "model": {}, "retry": {}},
        "otel": {"harness_trace_id": "x", "poll_interval_s": 1.0, "correlated": False},
        "leg_scaffold": "sdk_loop",
    }


if __name__ == "__main__":
    unittest.main()
