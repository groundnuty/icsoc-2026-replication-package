"""Offline tests for the E3 A4-gate glue: drives `harness/a4_gate.py::run_gate` with a MOCK E3
`verify_fn` (no live federation calls, no live model calls) using
`build_e3_feedback` as the injected `feedback_fn`, exercising the 5 remedy
paths E3's own `emitted_attribution` values route to. Mirrors
`harness/test_a4_gate.py`'s `TestRunGate` shape, but scripted with the E3
feedback-relevant fields (`full_path`, `t_max_s`, `path_present`,
`residue_by_provider`, `all_provider_ids`) instead of the placement ones.

Fully OFFLINE: every verify/attempt/wait/escalate callable here is a
synthetic in-process stub. No federation contact, no model calls, no
network.

Runs either as:
    python3 -m unittest harness.test_e3_a4 -v      # from repo root
    python3 harness/test_e3_a4.py                  # directly as a script
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest

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


ALL_PROVIDER_IDS = ["prov-cloud-pl", "prov-de"]
ALL_PROVIDER_LABELS = ["cloud-pl", "de"]
FULL_PATH = "/space-1/harness/e3/trial-synthetic.bin"
T_MAX = 30.0


# --- injected async mocks (same shapes as harness/test_a4_gate.py) ----------

class ScriptedE3Verify:
    """An async verify_fn stub replaying a fixed script of E3 verify results,
    one per call -- the LAST scripted result repeats for any call past the
    end (so "always Violated" style tests need only one scripted entry).
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


def _e3_violated(attribution, *, path_present=True, residue_by_provider=None, n_probes=3):
    residue_by_provider = (
        residue_by_provider
        if residue_by_provider is not None
        else [
            {"label": "cloud-pl", "id": "prov-cloud-pl", "bytes": 4096},
            {"label": "de", "id": "prov-de", "bytes": 0},
        ]
    )
    return {
        "verdict_per_term": {"T1": "Violated"},
        "emitted_attribution": attribution,
        "n_probes": n_probes,
        "full_path": FULL_PATH,
        "t_max_s": T_MAX,
        "path_present": path_present,
        "residue_by_provider": residue_by_provider,
        "all_provider_ids": ALL_PROVIDER_IDS,
    }


def _e3_fulfilled(*, n_probes=3):
    return {
        "verdict_per_term": {"T1": "Fulfilled"},
        "emitted_attribution": "neither",
        "n_probes": n_probes,
        "full_path": FULL_PATH,
        "t_max_s": T_MAX,
        "path_present": False,
        "residue_by_provider": [],
        "all_provider_ids": ALL_PROVIDER_IDS,
    }


def _e3_not_determined():
    return {
        "verdict_per_term": {"T1": "NotDetermined"},
        "emitted_attribution": "neither",
        "n_probes": 0,
        "full_path": FULL_PATH,
        "t_max_s": T_MAX,
        "path_present": None,
        "residue_by_provider": [],
        "all_provider_ids": ALL_PROVIDER_IDS,
    }


# =============================================================================
# 1. run_gate remedy-path reachability, driven by a mock E3 verify_fn
# =============================================================================

class TestE3RemedyPaths(unittest.TestCase):
    def test_1_agent_evidence_violated_reinvokes(self):
        attempt = RecordingAttempt()
        verify = ScriptedE3Verify([_e3_violated("agent"), _e3_fulfilled()])

        result = asyncio.run(a4_gate.run_gate(
            attempt, verify, feedback_fn=a4_gate.build_e3_feedback,
        ))

        self.assertEqual(len(result["remedy_trail"]), 1)
        self.assertEqual(result["remedy_trail"][0]["remedy"], "reinvoke_agent")
        self.assertIsNone(attempt.calls[0])       # initial attempt: no feedback
        self.assertIsNotNone(attempt.calls[1])    # re-invoke: fed back the violation
        self.assertIn(FULL_PATH, attempt.calls[1])
        self.assertIn("Delete it from all sites.", attempt.calls[1])
        self.assertEqual(result["cost_per_bucket"]["agent_invocations"], 2)
        self.assertEqual(result["final_verdict"], "Fulfilled")
        self.assertFalse(result["terminated"])

    def test_2_service_evidence_violated_waits_no_reinvoke(self):
        attempt = RecordingAttempt()
        wait = RecordingWait()
        verify = ScriptedE3Verify([_e3_violated("service"), _e3_fulfilled()])

        result = asyncio.run(a4_gate.run_gate(
            attempt, verify, wait_fn=wait, feedback_fn=a4_gate.build_e3_feedback,
        ))

        self.assertEqual(result["remedy_trail"][0]["remedy"], "wait_repoll")
        self.assertEqual(len(wait.calls), 1)
        self.assertEqual(wait.calls[0], 1)
        # agent was NOT re-invoked -- only the initial attempt.
        self.assertEqual(len(attempt.calls), 1)
        self.assertEqual(result["cost_per_bucket"]["agent_invocations"], 1)
        self.assertEqual(result["final_verdict"], "Fulfilled")

    def test_3_both_evidence_reinvokes_then_waits(self):
        attempt = RecordingAttempt()
        wait = RecordingWait()
        verify = ScriptedE3Verify([_e3_violated("both"), _e3_fulfilled()])

        result = asyncio.run(a4_gate.run_gate(
            attempt, verify, wait_fn=wait, feedback_fn=a4_gate.build_e3_feedback,
        ))

        self.assertEqual(result["remedy_trail"][0]["remedy"], "reinvoke_then_wait")
        self.assertEqual(result["cost_per_bucket"]["agent_invocations"], 2)
        self.assertEqual(len(wait.calls), 1)
        self.assertIsNotNone(attempt.calls[1])
        self.assertEqual(result["final_verdict"], "Fulfilled")

    def test_4_not_determined_neither_escalates(self):
        attempt = RecordingAttempt()
        wait = RecordingWait()
        escalate = RecordingEscalate()
        verify = ScriptedE3Verify([_e3_not_determined(), _e3_fulfilled()])

        result = asyncio.run(a4_gate.run_gate(
            attempt, verify, wait_fn=wait, escalate_fn=escalate,
            feedback_fn=a4_gate.build_e3_feedback,
        ))

        self.assertEqual(result["remedy_trail"][0]["remedy"], "escalate")
        self.assertEqual(escalate.calls, 1)
        # escalate does not itself build feedback / re-invoke.
        self.assertEqual(len(attempt.calls), 1)
        self.assertEqual(result["final_verdict"], "Fulfilled")

    def test_5_fulfilled_first_cycle_accepts_immediately(self):
        attempt = RecordingAttempt()
        verify = ScriptedE3Verify([_e3_fulfilled()])

        result = asyncio.run(a4_gate.run_gate(
            attempt, verify, feedback_fn=a4_gate.build_e3_feedback,
        ))

        self.assertEqual(result["gate_cycles"], 1)
        self.assertFalse(result["terminated"])
        self.assertEqual(result["final_verdict"], "Fulfilled")
        self.assertEqual(result["first_attempt_verdict"], "Fulfilled")
        self.assertEqual(result["remedy_trail"], [])
        self.assertEqual(result["cost_per_bucket"]["agent_invocations"], 1)

    def test_cost_buckets_sum_verifier_probes_across_cycles(self):
        attempt = RecordingAttempt()
        verify = ScriptedE3Verify([
            _e3_violated("agent", n_probes=4),
            _e3_fulfilled(n_probes=2),
        ])

        result = asyncio.run(a4_gate.run_gate(attempt, verify, feedback_fn=a4_gate.build_e3_feedback))

        self.assertEqual(result["cost_per_bucket"]["verifier_probes"], 6)
        self.assertEqual(result["cost_per_bucket"]["gate_retries"], 1)


# =============================================================================
# 2. build_e3_feedback shape (frozen template)
# =============================================================================

class TestBuildE3FeedbackShape(unittest.TestCase):
    def test_full_combined_form_matches_frozen_template_shape(self):
        text = a4_gate.build_e3_feedback(_e3_violated("agent"))

        self.assertIn(f"The file at {FULL_PATH}", text)
        self.assertIn(f"within the {T_MAX}s deadline", text)
        self.assertIn("path still resolves", text)
        self.assertIn("residue remains on", text)
        self.assertIn("provider 'cloud-pl' (prov-cloud-pl): 4096 bytes", text)
        self.assertIn(
            "The required end state is: the path absent AND zero bytes on every provider",
            text,
        )
        self.assertIn("prov-cloud-pl", text.split("every provider")[1])
        self.assertIn("prov-de", text.split("every provider")[1])
        self.assertIn("The guarantee is not met. Delete it from all sites.", text)
        # zero-residue provider (de: 0 bytes) is not individually named in
        # the residue clause -- only providers with residue > 0 are listed.
        self.assertNotIn("'de' (prov-de): 0 bytes", text)

    def test_zero_residue_everywhere_states_path_half_only(self):
        text = a4_gate.build_e3_feedback(_e3_violated(
            "agent", path_present=True,
            residue_by_provider=[
                {"label": "cloud-pl", "id": "prov-cloud-pl", "bytes": 0},
                {"label": "de", "id": "prov-de", "bytes": 0},
            ],
        ))
        self.assertIn("path still resolves", text)
        self.assertNotIn("residue remains on", text)

    def test_path_absent_states_residue_half_only(self):
        text = a4_gate.build_e3_feedback(_e3_violated("agent", path_present=False))
        self.assertNotIn("path still resolves", text)
        self.assertNotIn("path absent and", text)
        self.assertIn("residue remains on", text)

    def test_never_probed_provider_reads_no_successful_probe(self):
        text = a4_gate.build_e3_feedback(_e3_violated(
            "agent", path_present=True,
            residue_by_provider=[
                {"label": "cloud-pl", "id": "prov-cloud-pl", "bytes": None},
                {"label": "de", "id": "prov-de", "bytes": 0},
            ],
        ))
        self.assertIn("provider 'cloud-pl' (prov-cloud-pl): no successful probe", text)

    def test_no_data_at_all_falls_back_to_no_successful_probe(self):
        text = a4_gate.build_e3_feedback({
            "full_path": FULL_PATH, "t_max_s": T_MAX,
            "path_present": None, "residue_by_provider": [], "all_provider_ids": ALL_PROVIDER_IDS,
        })
        self.assertIn("no successful probe", text)

    def test_feedback_quotes_original_t_max_not_effective(self):
        # The agent-facing template must always report the ORIGINAL,
        # caller-supplied t_max_s -- never a gate-internal extended window
        # (mirrors build_feedback's own placement-side discipline).
        text = a4_gate.build_e3_feedback(_e3_violated("agent"))
        self.assertIn(f"within the {T_MAX}s deadline", text)
        self.assertNotIn("45.0s deadline", text)  # T_MAX + OBSERVATION_MARGIN_S, must never leak in


if __name__ == "__main__":
    unittest.main()
