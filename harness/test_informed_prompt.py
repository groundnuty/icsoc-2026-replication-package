"""Informed-agent ablation — the `informed` flag on the E1 Form B
prompt. Verifies: (a) informed=False (default) is BYTE-IDENTICAL to the frozen
baseline; (b) informed=True appends the frozen contract block, information only,
with the deadline anchored to task-start; (c) the fields fill per-trial."""
import os
import sys
import unittest

if __package__ in (None, ""):
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    from harness import runner
else:
    from . import runner


def _e1_scenario(*, informed):
    # make_e1_form_b_scenario builds the static scenario; run_trial fills
    # full_path + expected_size per trial — mirror that here for the unit test.
    s = runner.make_e1_form_b_scenario(
        space_name="space-1", space_id="sid-1",
        target_provider_label="de", target_provider_id="de-id-hash",
        source_provider_label="cloud-pl", informed=informed,
    )
    s["full_path"] = "/space-1/harness/inc2b/trial-x.bin"
    s["expected_size"] = 4096
    return s


class TestInformedFlag(unittest.TestCase):
    def test_default_is_false_and_baseline_is_byte_identical(self):
        s = _e1_scenario(informed=False)
        self.assertFalse(runner.make_e1_form_b_scenario(
            space_name="a", space_id="b", target_provider_label="de",
            target_provider_id="i", source_provider_label="cloud-pl")["informed"])
        p = runner.build_user_prompt(s)
        # exactly the baseline sentence, nothing appended
        self.assertTrue(p.startswith("Replicate the file at /space-1/harness/inc2b/trial-x.bin"))
        self.assertTrue(p.rstrip().endswith("tell me once that's guaranteed."))
        self.assertNotIn("provider id", p)
        self.assertNotIn("of the start of this task", p)

    def test_informed_appends_the_frozen_block_information_only(self):
        base = runner.build_user_prompt(_e1_scenario(informed=False))
        p = runner.build_user_prompt(_e1_scenario(informed=True))
        # the informed prompt is the baseline + "\n\n" + the block (baseline unchanged)
        self.assertTrue(p.startswith(base))
        self.assertEqual(p[:len(base) + 2], base + "\n\n")
        # information content: target provider id + expected size + deadline anchor
        self.assertIn("provider id de-id-hash", p)
        self.assertIn("4096 bytes", p)
        self.assertIn("within 30 seconds of the start of this task", p)
        # poll + report-only-on-observation + honest-refusal
        self.assertIn("Check the target site's actual file distribution", p)
        self.assertIn("ONLY once you have observed", p)
        # v3: explicit re-poll-until-deadline imperative
        self.assertIn("Keep checking until you have observed it or the time available runs out", p)
        self.assertIn("could not confirm the placement", p)

    def test_frozen_constant_v3_adjustments(self):
        # (v2) task-start deadline anchor + (v3) re-poll-until-deadline imperative.
        self.assertIn("of the start of this task", runner.INFORMED_CONTRACT_BLOCK)
        self.assertIn("Keep checking until you have observed it or the time available runs out",
                      runner.INFORMED_CONTRACT_BLOCK)


if __name__ == "__main__":
    unittest.main()
