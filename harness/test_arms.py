"""The default arm file reproduces the seven arms the recorded runs used: for every recorded
trial, the leg built from config/arms.json has the recorded leg name, the recorded model
settings (`model_config`), and the recorded scaffold (adapter class). Offline: no network,
no model credentials (a placeholder key is set; the SDK login directory is stubbed)."""
import glob
import json
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from harness import arms, panel, runner  # noqa: E402

RECORDINGS = os.environ.get("HARNESS_TEST_RECORDINGS", os.path.join(_ROOT, "recordings"))
TRIAL_POPULATIONS = [
    "scored_sweep--rq1-control-clean-20260706", "scored_sweep--rq2-ss1-gated-20260706",
    "scored_sweep--rq2-ss2-gated-20260706", "scored_sweep--rq2-ss3-gated-20260706",
    "e3_sweep--e3-scored-20260707", "a4_sweep--a4-control-v3-20260706",
    "a4_sweep--a4-fault-ss1-20260706", "a4_sweep--a4-fault-ss2-20260706",
    "a4_sweep--a4-fault-ss3-20260706", "scored_sweep--e1-informed-20260716",
    "scored_sweep--e1-uninformed-paired-20260716",
    "a4_sweep--e3-a4-healthy-v2-20260715", "a4_sweep--e3-a4-healthy-v2-glm52-20260715",
]


def build_default_legs():
    arm_list = arms.load_arms(arms.DEFAULT_PATH)
    keys = {a["key_env"]: "placeholder-not-a-key" for a in arm_list if a["key_env"]}
    with mock.patch.dict(os.environ, keys), \
            mock.patch.object(panel, "make_isolated_config_dir", return_value="/nonexistent"):
        return {a["model_id"]: (a, arms.build_leg(a)) for a in arm_list}


class TestDefaultArmFile(unittest.TestCase):
    def test_seven_arms_with_approved_fields(self):
        arm_list = arms.load_arms(arms.DEFAULT_PATH)
        self.assertEqual(len(arm_list), 7)
        for a in arm_list:
            self.assertEqual(set(a), set(arms.FIELDS))
            self.assertIn(a["kind"], arms.KINDS)

    def test_kind_selects_leg_class(self):
        for model_id, (arm, leg) in build_default_legs().items():
            expected = (panel.ForgeOpenAILeg if arm["kind"] == "openai-compatible"
                        else panel.AnthropicSDKLeg)
            self.assertIsInstance(leg, expected, model_id)

    def test_api_model_id_is_optional_and_keeps_the_recorded_name(self):
        arm = {"name": "x", "kind": "openai-compatible", "model_id": "vendor/model-a",
               "base_url": "https://endpoint.example/v1", "key_env": "X_KEY",
               "api_model_id": "vendor/alias-b"}
        with mock.patch.dict(os.environ, {"X_KEY": "placeholder-not-a-key"}):
            leg = arms.build_leg(arm)
        self.assertEqual(leg.name, "forge:vendor/model-a")
        self.assertEqual(leg.model_config()["model_id"], "vendor/model-a")
        self.assertEqual(leg.model_config()["api_model_id"], "vendor/alias-b")
        for a in arms.load_arms(arms.DEFAULT_PATH):
            self.assertNotIn("api_model_id", a)       # unpopulated in the shipped file

    def test_invalid_arm_file_is_rejected(self):
        import tempfile
        bad = {"arms": [{"name": "x", "kind": "openai-compatible", "model_id": "m",
                         "base_url": None, "key_env": None}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(bad, fh)
        with self.assertRaises(ValueError):
            arms.load_arms(fh.name)
        os.unlink(fh.name)
        unknown = {"arms": [{"name": "x", "kind": "anthropic-sdk", "model_id": "m",
                             "base_url": None, "key_env": None, "extra": 1}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(unknown, fh)
        with self.assertRaises(ValueError):
            arms.load_arms(fh.name)
        os.unlink(fh.name)


@unittest.skipUnless(os.path.isdir(RECORDINGS), "recordings/ not present")
class TestArmsReproduceRecordings(unittest.TestCase):
    def test_every_recorded_trial_matches_its_arm(self):
        legs = build_default_legs()
        by_name = {leg.name: leg for _, leg in legs.values()}
        seen = set()
        for pop in TRIAL_POPULATIONS:
            for f in sorted(glob.glob(os.path.join(RECORDINGS, pop, "trial-*.json"))):
                with open(f) as fh:
                    rec = json.load(fh)
                with self.subTest(population=pop, trial=rec["trial_id"]):
                    leg = by_name.get(rec["model_leg"])
                    self.assertIsNotNone(leg, f"no arm builds leg {rec['model_leg']!r}")
                    self.assertEqual(leg.model_config(), rec["model_config"])
                    self.assertEqual(runner._leg_scaffold_for(leg), rec["leg_scaffold"])
                    seen.add(rec["model_leg"])
        self.assertEqual(seen, set(by_name), "arm file and recorded arms differ")


if __name__ == "__main__":
    unittest.main()
