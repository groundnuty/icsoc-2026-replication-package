"""The driver switches (SWEEP_SCENARIO=deletion, SWEEP_INFORMED=1, and the default placement
prompt used by the paired runs) construct the recorded contract and prompt, byte for byte,
for every recorded trial of those runs. Offline: reads only the bundled recordings."""
import glob
import json
import os
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from harness import runner  # noqa: E402
import scored_sweep_driver as driver  # noqa: E402

RECORDINGS = os.environ.get("HARNESS_TEST_RECORDINGS", os.path.join(_ROOT, "recordings"))
POPULATIONS = {  # directory -> (scenario switch, informed switch)
    "e3_sweep--e3-scored-20260707": ("deletion", False),
    "scored_sweep--e1-informed-20260716": ("placement", True),
    "scored_sweep--e1-uninformed-paired-20260716": ("placement", False),
}
_PATH = re.compile(r"/([^/\s'\"]+)/harness/(?:inc2b|e3)/(trial-[0-9a-f]+)\.bin")


def rebuild(recording, scenario, informed):
    """Contract and prompt from the switches, taking only per-trial inputs from the recording
    (the space name and id, and the trial id); the file size is the driver's own constant."""
    space_name, trial_id = _PATH.search(recording["agent"]["prompt_verbatim"]).groups()
    base = driver.build_base_scenario(space_name, recording["expected"]["fixture_space_id"],
                                      scenario=scenario, informed=informed)
    if scenario == "deletion":
        _, ts = runner.deletion_trial_scenario(base, trial_id)
    else:
        _, ts = runner.placement_trial_scenario(base, trial_id, runner.DEFAULT_CONTENT_SIZE)
    return trial_id, runner.contract_for(ts), runner.build_user_prompt(ts)


@unittest.skipUnless(os.path.isdir(RECORDINGS), "recordings/ not present")
class TestSwitchesReproduceRecordings(unittest.TestCase):
    def test_every_trial_contract_and_prompt_match(self):
        for pop, (scenario, informed) in POPULATIONS.items():
            files = sorted(glob.glob(os.path.join(RECORDINGS, pop, "trial-*.json")))
            self.assertEqual(len(files), 56, pop)
            for f in files:
                with open(f) as fh:
                    rec = json.load(fh)
                trial_id, contract, prompt = rebuild(rec, scenario, informed)
                with self.subTest(population=pop, trial=trial_id):
                    self.assertEqual(trial_id, rec["trial_id"])
                    self.assertEqual(contract, rec["contract"])
                    self.assertEqual(prompt, rec["agent"]["prompt_verbatim"])
                    # the driver's poll settings are the ones recorded
                    self.assertEqual(rec["state_timeline"]["poll_interval_s"], driver.POLL_INTERVAL_S)
                    self.assertEqual(rec["expected"]["poll_until_rel_s"], driver.POLL_UNTIL_REL_S)
                    if scenario == "placement":
                        self.assertEqual(rec["expected"]["per_term"]["T1"]["expected_size"],
                                         runner.DEFAULT_CONTENT_SIZE)

    def test_switches_select_the_expected_scenarios(self):
        self.assertEqual(driver.build_base_scenario("s", "i", scenario="deletion")["scenario_id"], "E3")
        self.assertEqual(driver.build_base_scenario("s", "i", scenario="placement")["scenario_id"], "E1")
        self.assertTrue(driver.build_base_scenario("s", "i", scenario="placement", informed=True)["informed"])
        self.assertFalse(driver.build_base_scenario("s", "i", scenario="placement", informed=False)["informed"])


if __name__ == "__main__":
    unittest.main()
