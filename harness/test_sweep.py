"""Regression tests for harness/sweep.py — cell enumeration, serialization
policy, health-gate, and the fault-injection sanction gate.

Stdlib `unittest` only, same import-shim as test_schema.py. All pure/offline —
no live federation, no kubectl, no tc.
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
        from harness import sweep
    except ImportError:
        import sweep
else:
    from . import sweep


DE = "7cfe30bef82a2c904c15624f752c58ddchd932"
UIBK = "af63236ac5942dbc22bd9159a8154c8ach6020"
PL = "ec4761ab47ecda487c46edd328179824chc3bd"


def _scenario(scenario_id="E1", target=DE, source=PL):
    return {
        "scenario_id": scenario_id,
        "target_provider_id": target,
        "source_provider_id": source,
        "target_provider_label": "de",
    }


class TestFaultSpec(unittest.TestCase):
    def test_none_ok_without_target(self):
        fs = sweep.FaultSpec()
        self.assertEqual(fs.condition, "none")
        self.assertFalse(fs.is_fault)
        self.assertIsNone(fs.target_provider)

    def test_none_with_target_rejected(self):
        with self.assertRaises(ValueError):
            sweep.FaultSpec(condition="none", target_provider=DE)

    def test_outage_requires_target(self):
        with self.assertRaises(ValueError):
            sweep.FaultSpec(condition="outage")
        fs = sweep.FaultSpec(condition="outage", target_provider=DE)
        self.assertTrue(fs.is_fault)

    def test_netem_requires_delay(self):
        with self.assertRaises(ValueError):
            sweep.FaultSpec(condition="netem_lag", target_provider=DE)
        fs = sweep.FaultSpec(condition="netem_lag", target_provider=DE, netem_delay_ms=25000.0)
        self.assertEqual(fs.netem_delay_ms, 25000.0)

    def test_netem_delay_only_valid_for_netem(self):
        with self.assertRaises(ValueError):
            sweep.FaultSpec(condition="outage", target_provider=DE, netem_delay_ms=10.0)

    def test_unknown_condition_rejected(self):
        with self.assertRaises(ValueError):
            sweep.FaultSpec(condition="meltdown", target_provider=DE)

    def test_to_record_shape(self):
        self.assertEqual(sweep.FaultSpec().to_record(), {"condition": "none"})
        self.assertEqual(
            sweep.FaultSpec(condition="outage", target_provider=DE).to_record(),
            {"condition": "outage", "target_provider": DE},
        )
        rec = sweep.FaultSpec(condition="netem_lag", target_provider=DE, netem_delay_ms=25000.0).to_record()
        self.assertEqual(rec["netem_delay_ms"], 25000.0)


class TestEnumerateCells(unittest.TestCase):
    def test_cross_product_count_and_order(self):
        scenarios = [_scenario("E1")]
        legs = ["forge-a", "sdk-b"]
        faults = [
            sweep.FaultSpec(),
            sweep.FaultSpec(condition="outage", target_provider=DE),
        ]
        cells = sweep.enumerate_cells(scenarios=scenarios, legs=legs, fault_specs=faults, trial_k=3)
        self.assertEqual(len(cells), 1 * 2 * 2)
        # scenario-major, then leg, then fault
        self.assertEqual(cells[0].model_leg, "forge-a")
        self.assertEqual(cells[0].fault.condition, "none")
        self.assertEqual(cells[1].fault.condition, "outage")
        self.assertEqual(cells[2].model_leg, "sdk-b")
        for c in cells:
            self.assertEqual(c.trial_k, 3)
            self.assertEqual(c.arm_live, "A0")
            self.assertEqual(c.target_provider, DE)

    def test_cells_carry_scenario_target(self):
        cells = sweep.enumerate_cells(
            scenarios=[_scenario("E1", target=UIBK)], legs=["l"], fault_specs=[sweep.FaultSpec()]
        )
        self.assertEqual(cells[0].target_provider, UIBK)


class TestSerializationPlan(unittest.TestCase):
    def test_no_fault_cells_all_concurrent(self):
        cells = sweep.enumerate_cells(
            scenarios=[_scenario()], legs=["a", "b", "c"], fault_specs=[sweep.FaultSpec()]
        )
        plan = sweep.serialization_plan(cells)
        self.assertEqual(len(plan["concurrent"]), 3)
        self.assertEqual(plan["serial_groups"], {})

    def test_same_provider_faults_serialize(self):
        faults = [
            sweep.FaultSpec(condition="outage", target_provider=DE),
            sweep.FaultSpec(condition="netem_lag", target_provider=DE, netem_delay_ms=25000.0),
        ]
        cells = sweep.enumerate_cells(scenarios=[_scenario()], legs=["a"], fault_specs=faults)
        plan = sweep.serialization_plan(cells)
        self.assertEqual(plan["concurrent"], [])
        self.assertEqual(len(plan["serial_groups"][DE]), 2)  # both serialize on DE

    def test_different_provider_faults_are_separate_groups(self):
        faults_de = sweep.FaultSpec(condition="outage", target_provider=DE)
        faults_uibk = sweep.FaultSpec(condition="outage", target_provider=UIBK)
        # two scenarios targeting different providers, each with its own fault
        cells = (
            sweep.enumerate_cells(scenarios=[_scenario(target=DE)], legs=["a"], fault_specs=[faults_de])
            + sweep.enumerate_cells(scenarios=[_scenario(target=UIBK)], legs=["a"], fault_specs=[faults_uibk])
        )
        plan = sweep.serialization_plan(cells)
        self.assertEqual(set(plan["serial_groups"].keys()), {DE, UIBK})
        self.assertEqual(len(plan["serial_groups"][DE]), 1)
        self.assertEqual(len(plan["serial_groups"][UIBK]), 1)

    def test_mixed_fault_and_no_fault(self):
        faults = [sweep.FaultSpec(), sweep.FaultSpec(condition="outage", target_provider=DE)]
        cells = sweep.enumerate_cells(scenarios=[_scenario()], legs=["a", "b"], fault_specs=faults)
        plan = sweep.serialization_plan(cells)
        self.assertEqual(len(plan["concurrent"]), 2)          # 2 legs × no-fault
        self.assertEqual(len(plan["serial_groups"][DE]), 2)   # 2 legs × outage-on-DE


class TestHealthGate(unittest.TestCase):
    def _online_map(self, mapping):
        return lambda pid: mapping.get(pid)

    def test_all_online_passes(self):
        health = sweep.health_gate(self._online_map({DE: True, PL: True}), [DE, PL])
        self.assertEqual(health, {DE: True, PL: True})
        self.assertTrue(sweep.gate_pass(health, [DE, PL]))

    def test_offline_provider_fails_gate(self):
        health = sweep.health_gate(self._online_map({DE: False, PL: True}), [DE, PL])
        self.assertFalse(sweep.gate_pass(health, [DE, PL]))

    def test_dead_probe_none_fails_closed(self):
        health = sweep.health_gate(self._online_map({DE: None, PL: True}), [DE, PL])
        self.assertFalse(sweep.gate_pass(health, [DE, PL]))

    def test_missing_provider_fails_closed(self):
        health = sweep.health_gate(self._online_map({PL: True}), [PL])
        self.assertFalse(sweep.gate_pass(health, [DE, PL]))  # DE never probed → not True

    def test_dedups_provider_ids(self):
        calls = []
        def probe(pid):
            calls.append(pid)
            return True
        sweep.health_gate(probe, [DE, DE, PL, PL])
        self.assertEqual(calls, [DE, PL])  # each distinct provider probed once


class TestDryRunFaultInjector(unittest.TestCase):
    def test_records_intended_fault_executes_nothing(self):
        inj = sweep.DryRunFaultInjector()
        fs = sweep.FaultSpec(condition="outage", target_provider=DE)
        handle = inj.inject(fs)
        self.assertTrue(handle["dry_run"])
        inj.clear(handle)
        self.assertEqual(inj.injected, [fs])
        self.assertEqual(inj.cleared, [fs])


class TestLiveFaultInjectorSanctionGate(unittest.TestCase):
    def test_refuses_without_sanction(self):
        with self.assertRaises(sweep.FaultInjectionError):
            sweep.LiveFaultInjector(exec_fn=lambda *a: None)

    def test_refuses_with_explicit_false(self):
        with self.assertRaises(sweep.FaultInjectionError):
            sweep.LiveFaultInjector(exec_fn=lambda *a: None, operator_sanctioned=False)

    def test_sanctioned_injects_via_exec_fn(self):
        calls = []
        inj = sweep.LiveFaultInjector(
            exec_fn=lambda action, fs: calls.append((action, fs.condition)),
            operator_sanctioned=True,
        )
        fs = sweep.FaultSpec(condition="outage", target_provider=DE)
        handle = inj.inject(fs)
        inj.clear(handle)
        self.assertEqual(calls, [("inject", "outage"), ("clear", "outage")])

    def test_sanctioned_noop_on_none_fault(self):
        calls = []
        inj = sweep.LiveFaultInjector(
            exec_fn=lambda action, fs: calls.append(action), operator_sanctioned=True
        )
        handle = inj.inject(sweep.FaultSpec())  # no-fault
        inj.clear(handle)
        self.assertEqual(calls, [])  # exec_fn never called for a no-fault cell


if __name__ == "__main__":
    unittest.main()
