"""Regression tests for harness/netem.py — envelope enforcement + command
construction + the persistent-injector-container lifecycle + the
sweep.LiveFaultInjector integration. All offline: the SSH/kubectl execution
is a mocked `runner`; no cluster, no network.
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
        from harness import netem, sweep
    except ImportError:
        import netem
        import sweep
else:
    from . import netem, sweep


DE_TARGET = netem.ProviderTarget(
    provider="de", kubeconfig="~/.kube/de-config", namespace="oneprovider-de",
    pod="oneprovider-0", container="oneprovider", iface="eth0",
)


def _no_sleep(_seconds):
    """Stub for NetemInjector(sleep_fn=...) so ensure-injector polling tests
    never actually sleep."""


def _fake_runner(rc=0, out="", err="", injector_running=False):
    """Stateful fake `runner`. Recognizes the two `_ensure_injector` command
    shapes (the ephemeral-container-status check, and the `kubectl debug`
    create) and resolves them against an internal "is the injector running"
    flag, so ordinary inject/clear tests don't need to hand-script every
    ensure-injector round-trip. The actual inject/clear exec gets the
    configured (rc, out, err).

    `tc qdisc show` (the post-inject verify AND `verify_clean`'s read) is
    modeled separately: it starts by returning `(rc, out, err)` verbatim (so
    tests that call `verify_clean` directly, like `TestVerifyClean`, control
    its output exactly as before this fake runner grew inject-verify
    awareness) but is updated whenever a LATER apply/clear exec succeeds —
    a successful `... netem delay ...` apply (rc==0) flips the tracked show
    body to a netem-bearing string; a successful `tc qdisc del` flips it
    back to clean. This lets ONE runner instance drive both a post-inject
    verify (wants netem present) and a later clear-verify (wants netem
    absent) with the physically-correct opposite expectations, without
    disturbing any test that never applies/clears (its literal `out` keeps
    being returned for `tc qdisc show`).

    `injector_running=True` starts as though the persistent container already
    exists (models the idempotent-reuse path)."""
    calls = []
    state = {"running": injector_running, "show_body": out}

    def _run(remote_cmd):
        calls.append(remote_cmd)
        if "ephemeralContainerStatuses" in remote_cmd:
            return (0, "true", "") if state["running"] else (0, "", "")
        if "kubectl debug" in remote_cmd:
            state["running"] = True
            return (0, "", "")
        if "tc qdisc show" in remote_cmd:
            return rc, state["show_body"], err
        if rc == 0 and "netem delay" in remote_cmd:
            state["show_body"] = "qdisc netem 8001: root refcnt 2 limit 1000 delay 40.0s\n"
        elif rc == 0 and "tc qdisc del" in remote_cmd:
            state["show_body"] = "qdisc noqueue 0: root refcnt 2\n"
        return rc, out, err

    _run.calls = calls
    return _run


def _clear_retry_runner(del_outcomes, show_outcomes):
    """Fake runner for clear-retry tests: dispatches on whether the command
    is the `tc qdisc del` (clear exec) or the `tc qdisc show` (verify_clean's
    plain-exec) shape, returning the next scripted (rc, out, err) from the
    matching list in order — the last entry repeats once exhausted. Distinct
    from `_fake_runner`, which returns the SAME (rc, out, err) for both,
    making it unable to model "del fails, show still reports dirty" or "del
    succeeds, show confirms clean" independently."""
    calls = []
    state = {"del_idx": 0, "show_idx": 0}

    def _run(remote_cmd):
        calls.append(remote_cmd)
        if "tc qdisc del" in remote_cmd:
            idx = min(state["del_idx"], len(del_outcomes) - 1)
            state["del_idx"] += 1
            return del_outcomes[idx]
        if "tc qdisc show" in remote_cmd:
            idx = min(state["show_idx"], len(show_outcomes) - 1)
            state["show_idx"] += 1
            return show_outcomes[idx]
        raise AssertionError(f"unexpected clear-retry command: {remote_cmd!r}")

    _run.calls = calls
    return _run


def _inject_verify_runner(add_outcome, show_outcome):
    """Fake runner for `exec_fn("inject", ...)`'s post-apply verify tests.
    The ensure-injector bootstrap is pre-resolved as already-running (skips
    check/create/poll noise); the apply exec (`tc qdisc add ... netem
    delay ...`) returns `add_outcome`; the post-apply verify (`tc qdisc
    show`) returns `show_outcome` INDEPENDENTLY of the apply's outcome.
    Unlike `_fake_runner`'s automatic apply-implies-show-sees-netem state
    tracking (which models the physically-honest case), this lets a test
    represent the silent-non-apply case `_fake_runner` cannot: add rc==0
    (looks like success) but the show does NOT see netem."""
    calls = []

    def _run(remote_cmd):
        calls.append(remote_cmd)
        if "ephemeralContainerStatuses" in remote_cmd:
            return (0, "true", "")
        if "kubectl debug" in remote_cmd:
            return (0, "", "")
        if "tc qdisc show" in remote_cmd:
            return show_outcome
        return add_outcome

    _run.calls = calls
    return _run


def _scripted_runner(responses):
    """Fake `runner` that returns `responses` (a list of (rc, out, err))
    strictly in order, one per call; the LAST entry repeats once exhausted.
    Used where a test needs precise control over the ensure-injector
    check/create/poll sequence rather than the state-machine shortcuts in
    `_fake_runner`."""
    calls = []

    def _run(remote_cmd):
        calls.append(remote_cmd)
        idx = min(len(calls) - 1, len(responses) - 1)
        return responses[idx]

    _run.calls = calls
    return _run


class TestEnvelopeEnforcement(unittest.TestCase):
    def _injector(self, runner=None, envelope=None):
        return netem.NetemInjector(
            runner or _fake_runner(), {"de": DE_TARGET},
            envelope or netem.NetemEnvelope(provider_allow=("de",), max_delay_ms=60000.0),
            sleep_fn=_no_sleep,
        )

    def test_rejects_non_netem_condition(self):
        inj = self._injector()
        outage = sweep.FaultSpec(condition="outage", target_provider="de")
        with self.assertRaises(netem.NetemEnvelopeError):
            inj.exec_fn("inject", outage)

    def test_rejects_provider_outside_allowlist(self):
        inj = self._injector()
        fs = sweep.FaultSpec(condition="netem_lag", target_provider="uibk", netem_delay_ms=40000.0)
        with self.assertRaises(netem.NetemEnvelopeError):
            inj.exec_fn("inject", fs)

    def test_rejects_delay_over_max(self):
        inj = self._injector()
        fs = sweep.FaultSpec(condition="netem_lag", target_provider="de", netem_delay_ms=90000.0)
        with self.assertRaises(netem.NetemEnvelopeError):
            inj.exec_fn("inject", fs)

    def test_rejects_unconfigured_provider(self):
        inj = netem.NetemInjector(
            _fake_runner(), {},  # de allowed by envelope but no ProviderTarget
            netem.NetemEnvelope(provider_allow=("de",)),
            sleep_fn=_no_sleep,
        )
        fs = sweep.FaultSpec(condition="netem_lag", target_provider="de", netem_delay_ms=40000.0)
        with self.assertRaises(netem.NetemEnvelopeError):
            inj.exec_fn("inject", fs)

    def test_accepts_in_envelope_inject(self):
        runner = _fake_runner(rc=0)
        inj = self._injector(runner=runner)
        fs = sweep.FaultSpec(condition="netem_lag", target_provider="de", netem_delay_ms=40000.0)
        result = inj.exec_fn("inject", fs)
        self.assertEqual(result["rc"], 0)
        self.assertTrue(result["inject_verified"])
        self.assertIn("netem", result["qdisc_snapshot"])
        # ensure-injector check (not running) + create + poll-check (running)
        # + the inject exec + the post-inject verify (tc qdisc show)
        self.assertEqual(len(runner.calls), 5)
        self.assertIn("netem delay 40000ms", runner.calls[-2])
        self.assertIn("tc qdisc show", runner.calls[-1])


class TestCommandConstruction(unittest.TestCase):
    def test_inject_cmd_shape(self):
        cmd = netem.build_inject_cmd(DE_TARGET, 40000.0)
        self.assertIn("kubectl exec", cmd)
        self.assertIn("-c 'netem-injector'", cmd)
        self.assertIn("-n 'oneprovider-de'", cmd)
        # PORT-NARROWED to :6665 (rtransfer) — prio qdisc + netem band + port filters
        self.assertIn("netem delay 40000ms", cmd)
        self.assertIn("root handle 1: prio", cmd)
        self.assertIn("dport 6665", cmd)
        self.assertIn("sport 6665", cmd)
        self.assertIn("flowid 1:3", cmd)
        self.assertIn("-- sh -c", cmd)
        self.assertIn("KUBECONFIG='~/.kube/de-config'", cmd)
        self.assertNotIn("kubectl debug", cmd)
        self.assertNotIn("--profile=netadmin", cmd)
        # whole-interface netem (delaying ALL traffic) must NOT be used — it
        # would break the verifier read (RQ2 gate b regression guard)
        self.assertNotIn("root netem delay", cmd)

    def test_inject_cmd_rounds_delay_to_int_ms(self):
        cmd = netem.build_inject_cmd(DE_TARGET, 24999.6)
        self.assertIn("netem delay 25000ms", cmd)

    def test_clear_cmd_shape(self):
        cmd = netem.build_clear_cmd(DE_TARGET)
        self.assertIn("kubectl exec", cmd)
        self.assertIn("-c 'netem-injector'", cmd)
        self.assertIn("tc qdisc del dev 'eth0' root", cmd)
        self.assertNotIn("kubectl debug", cmd)
        self.assertNotIn("--profile=netadmin", cmd)

    def test_show_cmd_shape(self):
        cmd = netem.build_show_cmd(DE_TARGET)
        self.assertIn("kubectl exec", cmd)
        self.assertIn("-c 'oneprovider'", cmd)  # execs into the op-worker container itself
        self.assertIn("tc qdisc show dev 'eth0'", cmd)
        self.assertNotIn("kubectl debug", cmd)
        self.assertNotIn("--profile=netadmin", cmd)
        self.assertNotIn(" -i ", cmd)  # no `-i` interactive flag


class TestEnsureInjectorCommandConstruction(unittest.TestCase):
    """Command shapes for the persistent-injector bootstrap (check + create),
    which now carry the `kubectl debug` + image-discovery logic that used to
    live in build_inject_cmd/build_clear_cmd."""

    def test_check_cmd_shape(self):
        cmd = netem.build_ensure_injector_check_cmd(DE_TARGET)
        self.assertIn("kubectl get pod", cmd)
        self.assertIn("ephemeralContainerStatuses", cmd)
        self.assertIn("netem-injector", cmd)
        self.assertIn("state.running", cmd)
        self.assertNotIn("kubectl debug", cmd)

    def test_create_cmd_shape(self):
        cmd = netem.build_ensure_injector_create_cmd(DE_TARGET)
        self.assertIn("kubectl debug", cmd)
        self.assertIn("--profile=netadmin", cmd)
        self.assertIn("--container='netem-injector'", cmd)
        self.assertIn("--target='oneprovider'", cmd)
        self.assertIn("-n 'oneprovider-de'", cmd)
        self.assertIn("sleep 7200", cmd)
        self.assertNotIn(" -i ", cmd)  # detached, not interactive

    def test_create_cmd_image_default_discovers_from_pod(self):
        # empty image → command discovers the op-worker image via jsonpath
        cmd = netem.build_ensure_injector_create_cmd(DE_TARGET)
        self.assertIn("kubectl get pod", cmd)
        self.assertIn("jsonpath", cmd)

    def test_create_cmd_explicit_image_used(self):
        t = netem.ProviderTarget(
            provider="de", kubeconfig="~/.kube/de-config", namespace="oneprovider-de",
            pod="oneprovider-0", container="oneprovider", image="netshoot:latest",
        )
        cmd = netem.build_ensure_injector_create_cmd(t)
        self.assertIn("--image='netshoot:latest'", cmd)
        self.assertNotIn("kubectl get pod", cmd)  # no discovery needed


class TestEnsureInjector(unittest.TestCase):
    """`NetemInjector._ensure_injector` — the idempotent create-or-reuse
    lifecycle for the persistent netem-injector ephemeral container."""

    def test_creates_when_not_running(self):
        # check(not running) -> create(ok) -> poll-check(running)
        runner = _scripted_runner([
            (0, "", ""),
            (0, "", ""),
            (0, "true", ""),
        ])
        inj = netem.NetemInjector(runner, {"de": DE_TARGET}, sleep_fn=_no_sleep)
        inj._ensure_injector(DE_TARGET)
        create_calls = [c for c in runner.calls if "kubectl debug" in c]
        self.assertEqual(len(create_calls), 1)

    def test_reuses_when_already_running_no_second_create(self):
        runner = _fake_runner(injector_running=True)
        inj = netem.NetemInjector(runner, {"de": DE_TARGET}, sleep_fn=_no_sleep)
        inj._ensure_injector(DE_TARGET)
        self.assertEqual(len(runner.calls), 1)  # just the check — no create at all
        self.assertTrue(all("kubectl debug" not in c for c in runner.calls))

        # Calling again still doesn't create a second one.
        inj._ensure_injector(DE_TARGET)
        self.assertEqual(len(runner.calls), 2)
        self.assertTrue(all("kubectl debug" not in c for c in runner.calls))

    def test_create_failure_raises(self):
        runner = _scripted_runner([
            (0, "", ""),        # initial check: not running
            (1, "", "denied"),  # create fails
        ])
        inj = netem.NetemInjector(runner, {"de": DE_TARGET}, sleep_fn=_no_sleep)
        with self.assertRaises(netem.NetemEnvelopeError):
            inj._ensure_injector(DE_TARGET)

    def test_never_reaches_running_raises_after_bounded_polls(self):
        # check never reports running, even after create "succeeds"
        runner = _scripted_runner([(0, "", "")])
        inj = netem.NetemInjector(runner, {"de": DE_TARGET}, sleep_fn=_no_sleep)
        with self.assertRaises(netem.NetemEnvelopeError):
            inj._ensure_injector(DE_TARGET)
        # bounded: initial check + create + N polls, not unbounded retries
        self.assertLessEqual(len(runner.calls), 2 + netem._ENSURE_INJECTOR_MAX_POLLS)

    def test_takes_a_few_polls_before_running(self):
        runner = _scripted_runner([
            (0, "", ""),      # initial check: not running
            (0, "", ""),      # create
            (0, "", ""),      # poll 1: still not running
            (0, "", ""),      # poll 2: still not running
            (0, "true", ""),  # poll 3: running
        ])
        inj = netem.NetemInjector(runner, {"de": DE_TARGET}, sleep_fn=_no_sleep)
        inj._ensure_injector(DE_TARGET)  # must not raise
        self.assertEqual(len(runner.calls), 5)


class TestInjectClearBehaviour(unittest.TestCase):
    def test_inject_failure_raises(self):
        runner = _fake_runner(rc=1, err="boom")
        inj = netem.NetemInjector(
            runner, {"de": DE_TARGET}, netem.NetemEnvelope(provider_allow=("de",)),
            sleep_fn=_no_sleep,
        )
        fs = sweep.FaultSpec(condition="netem_lag", target_provider="de", netem_delay_ms=40000.0)
        with self.assertRaises(netem.NetemEnvelopeError):
            inj.exec_fn("inject", fs)

    def test_clear_failure_does_not_raise(self):
        # clear is best-effort (watchdog + restart also clear) — must not
        # raise, even after exhausting all retry attempts. `_fake_runner`
        # returns the SAME (rc=1, err) for every command that isn't an
        # ensure-injector shape, so verify_clean's show-exec also fails
        # (rc != 0 -> fail-closed "not clean"), and every retry attempt
        # stays dirty.
        runner = _fake_runner(rc=1, err="no qdisc")
        inj = netem.NetemInjector(
            runner, {"de": DE_TARGET}, netem.NetemEnvelope(provider_allow=("de",)),
            sleep_fn=_no_sleep,
        )
        fs = sweep.FaultSpec(condition="netem_lag", target_provider="de", netem_delay_ms=40000.0)
        result = inj.exec_fn("clear", fs)
        self.assertEqual(result["action"], "clear")
        self.assertEqual(result["rc"], 1)  # surfaced, not raised
        self.assertFalse(result["verified_clean"])
        self.assertEqual(result["attempts"], netem._CLEAR_MAX_ATTEMPTS)
        # clear does NOT ensure the injector — best-effort; retries del+verify
        # up to _CLEAR_MAX_ATTEMPTS times (one del + one verify per attempt)
        self.assertEqual(len(runner.calls), netem._CLEAR_MAX_ATTEMPTS * 2)
        self.assertTrue(all("kubectl debug" not in c for c in runner.calls))

    def test_unknown_action_raises(self):
        inj = netem.NetemInjector(
            _fake_runner(), {"de": DE_TARGET}, netem.NetemEnvelope(provider_allow=("de",)),
            sleep_fn=_no_sleep,
        )
        fs = sweep.FaultSpec(condition="netem_lag", target_provider="de", netem_delay_ms=40000.0)
        with self.assertRaises(netem.NetemEnvelopeError):
            inj.exec_fn("frobnicate", fs)


class TestVerifyClean(unittest.TestCase):
    """`NetemInjector.verify_clean` — the clear-verification rider used by
    `sweep_run.py`'s `verify_between` between serial-group cells. Now backed
    by the plain-exec `build_show_cmd` — creates no ephemeral container."""

    def _injector(self, runner):
        return netem.NetemInjector(
            runner, {"de": DE_TARGET}, netem.NetemEnvelope(provider_allow=("de",)),
            sleep_fn=_no_sleep,
        )

    def test_netem_present_is_not_clean(self):
        runner = _fake_runner(rc=0, out="qdisc netem 8001: root refcnt 2 limit 1000 delay 40.0s\n")
        inj = self._injector(runner)
        self.assertFalse(inj.verify_clean("de"))
        self.assertEqual(len(runner.calls), 1)  # plain exec, no container bootstrap

    def test_noqueue_is_clean(self):
        runner = _fake_runner(rc=0, out="qdisc noqueue 0: root refcnt 2\n")
        inj = self._injector(runner)
        self.assertTrue(inj.verify_clean("de"))
        self.assertEqual(len(runner.calls), 1)

    def test_nonzero_rc_is_fail_closed_not_clean(self):
        runner = _fake_runner(rc=1, out="", err="error dialing backend")
        inj = self._injector(runner)
        self.assertFalse(inj.verify_clean("de"))

    def test_runner_raising_is_fail_closed_not_clean(self):
        def _raising_runner(remote_cmd):
            raise RuntimeError("ssh connection refused")
        inj = self._injector(_raising_runner)
        self.assertFalse(inj.verify_clean("de"))


class TestClearRetry(unittest.TestCase):
    """`NetemInjector.exec_fn("clear", ...)` retries a transient-slow `tc
    qdisc del`, using `verify_clean` as the authoritative success oracle —
    a single slow del (ssh/kubectl latency spike, not a stuck injector)
    would otherwise trip the between-cells clear-rider and abort most of a
    serial fault sub-sweep."""

    def _injector(self, runner, sleep_fn=None):
        return netem.NetemInjector(
            runner, {"de": DE_TARGET}, netem.NetemEnvelope(provider_allow=("de",)),
            sleep_fn=sleep_fn or _no_sleep,
        )

    def _fault(self):
        return sweep.FaultSpec(condition="netem_lag", target_provider="de", netem_delay_ms=40000.0)

    def test_clear_succeeds_first_try(self):
        runner = _clear_retry_runner(
            del_outcomes=[(0, "", "")],
            show_outcomes=[(0, "qdisc noqueue 0: root refcnt 2\n", "")],
        )
        inj = self._injector(runner)
        result = inj.exec_fn("clear", self._fault())
        self.assertEqual(result["action"], "clear")
        self.assertTrue(result["verified_clean"])
        self.assertEqual(result["attempts"], 1)
        del_calls = [c for c in runner.calls if "tc qdisc del" in c]
        self.assertEqual(len(del_calls), 1)

    def test_clear_transient_then_succeeds(self):
        sleep_calls = []
        runner = _clear_retry_runner(
            # attempt 1: del "times out" (surfaced here as a non-zero rc —
            # the retry doesn't care WHY the del looked bad, only that
            # verify_clean still sees netem present)
            del_outcomes=[(1, "", "context deadline exceeded"), (0, "", "")],
            show_outcomes=[
                (0, "qdisc netem 8001: root refcnt 2 limit 1000 delay 40.0s\n", ""),
                (0, "qdisc noqueue 0: root refcnt 2\n", ""),
            ],
        )
        inj = self._injector(runner, sleep_fn=lambda s: sleep_calls.append(s))
        result = inj.exec_fn("clear", self._fault())
        self.assertTrue(result["verified_clean"])
        self.assertEqual(result["attempts"], 2)
        del_calls = [c for c in runner.calls if "tc qdisc del" in c]
        self.assertEqual(len(del_calls), 2)
        # exactly one pause, between attempt 1 and attempt 2
        self.assertEqual(sleep_calls, [netem._CLEAR_RETRY_SLEEP_S])

    def test_clear_ssh_timeout_raise_then_succeeds(self):
        # ssh_runner._run is subprocess.run(timeout=...), which RAISES
        # subprocess.TimeoutExpired
        # on the 90s ssh-layer timeout (NOT rc!=0). The clear loop must catch
        # it, treat it as a failed attempt, and retry — else the very timeout
        # this retry exists to survive propagates straight out of exec_fn.
        import subprocess
        sleep_calls = []
        del_calls = []
        show_state = {"idx": 0}
        show_outcomes = [
            # after the raised del (attempt 1): netem still present → not clean
            (0, "qdisc netem 8001: root refcnt 2 limit 1000 delay 40.0s\n", ""),
            # after the successful del (attempt 2): clean
            (0, "qdisc noqueue 0: root refcnt 2\n", ""),
        ]

        def _run(remote_cmd):
            if "tc qdisc del" in remote_cmd:
                del_calls.append(remote_cmd)
                if len(del_calls) == 1:
                    raise subprocess.TimeoutExpired(cmd=remote_cmd, timeout=90)
                return (0, "", "")
            if "tc qdisc show" in remote_cmd:
                idx = min(show_state["idx"], len(show_outcomes) - 1)
                show_state["idx"] += 1
                return show_outcomes[idx]
            raise AssertionError(f"unexpected clear command: {remote_cmd!r}")

        inj = self._injector(_run, sleep_fn=lambda s: sleep_calls.append(s))
        result = inj.exec_fn("clear", self._fault())  # must NOT raise
        self.assertTrue(result["verified_clean"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(del_calls), 2)          # raised del retried
        self.assertEqual(sleep_calls, [netem._CLEAR_RETRY_SLEEP_S])

    def test_clear_never_succeeds_does_not_raise(self):
        # verify_clean stays dirty on every attempt — the genuinely-stuck
        # case the clear-rider (verify_between) is meant to catch.
        runner = _clear_retry_runner(
            del_outcomes=[(1, "", "boom")],
            show_outcomes=[(0, "qdisc netem 8001: root refcnt 2 limit 1000 delay 40.0s\n", "")],
        )
        inj = self._injector(runner)
        result = inj.exec_fn("clear", self._fault())  # must not raise
        self.assertEqual(result["action"], "clear")
        self.assertFalse(result["verified_clean"])
        self.assertEqual(result["attempts"], netem._CLEAR_MAX_ATTEMPTS)
        del_calls = [c for c in runner.calls if "tc qdisc del" in c]
        self.assertEqual(len(del_calls), netem._CLEAR_MAX_ATTEMPTS)


class TestInjectVerify(unittest.TestCase):
    """`NetemInjector.exec_fn("inject", ...)`'s post-apply verify: asserts
    the qdisc state via a read-only `tc qdisc show` rather than trusting the
    add's rc=0 alone."""

    def _injector(self, runner):
        return netem.NetemInjector(
            runner, {"de": DE_TARGET}, netem.NetemEnvelope(provider_allow=("de",)),
            sleep_fn=_no_sleep,
        )

    def _fault(self):
        return sweep.FaultSpec(condition="netem_lag", target_provider="de", netem_delay_ms=40000.0)

    def test_inject_verified_returns_snapshot(self):
        show_body = "qdisc netem 8001: root refcnt 2 limit 1000 delay 40.0s\n"
        runner = _inject_verify_runner(
            add_outcome=(0, "", ""),
            show_outcome=(0, show_body, ""),
        )
        inj = self._injector(runner)
        result = inj.exec_fn("inject", self._fault())
        self.assertTrue(result["inject_verified"])
        self.assertEqual(result["qdisc_snapshot"], show_body)

    def test_inject_silent_nonapply_raises(self):
        # add returns rc=0 (looks like success) but the show does NOT see
        # netem — a silent non-apply that must be surfaced loud, never
        # passed through as a false "verified" success.
        runner = _inject_verify_runner(
            add_outcome=(0, "", ""),
            show_outcome=(0, "qdisc noqueue 0: root refcnt 2\n", ""),
        )
        inj = self._injector(runner)
        with self.assertRaises(netem.NetemEnvelopeError):
            inj.exec_fn("inject", self._fault())

    def test_inject_verify_show_failure_also_raises(self):
        # the show call itself fails (rc!=0) — can't confirm netem is
        # applied, so this must ALSO fail loud, not silently pass.
        runner = _inject_verify_runner(
            add_outcome=(0, "", ""),
            show_outcome=(1, "", "error dialing backend"),
        )
        inj = self._injector(runner)
        with self.assertRaises(netem.NetemEnvelopeError):
            inj.exec_fn("inject", self._fault())


class TestLiveFaultInjectorIntegration(unittest.TestCase):
    """NetemInjector.exec_fn plugs into sweep.LiveFaultInjector (which enforces
    the operator_sanctioned gate) — end-to-end inject+clear through both."""

    def test_end_to_end_inject_clear(self):
        runner = _fake_runner(rc=0)
        netem_inj = netem.NetemInjector(
            runner, {"de": DE_TARGET}, netem.NetemEnvelope(provider_allow=("de",)),
            sleep_fn=_no_sleep,
        )
        live = sweep.LiveFaultInjector(exec_fn=netem_inj.exec_fn, operator_sanctioned=True)
        fs = sweep.FaultSpec(condition="netem_lag", target_provider="de", netem_delay_ms=40000.0)
        handle = live.inject(fs)
        self.assertTrue(handle["exec_result"]["inject_verified"])
        live.clear(handle)
        # inject = ensure-injector (check + create + poll-check) + 1 exec +
        # 1 post-inject verify show (5); clear = 1 del exec + 1 verify_clean
        # show exec (2) — the fake runner tracks netem-applied/cleared state
        # across the two show reads, so the post-inject show correctly sees
        # netem present and the post-clear show correctly sees it cleared.
        self.assertEqual(len(runner.calls), 7)
        self.assertIn("netem delay 40000ms", runner.calls[3])
        self.assertIn("tc qdisc show", runner.calls[4])
        self.assertIn("qdisc del", runner.calls[5])

    def test_live_injector_still_refuses_without_sanction(self):
        netem_inj = netem.NetemInjector(_fake_runner(), {"de": DE_TARGET}, netem.NetemEnvelope())
        with self.assertRaises(sweep.FaultInjectionError):
            sweep.LiveFaultInjector(exec_fn=netem_inj.exec_fn)  # no operator_sanctioned


if __name__ == "__main__":
    unittest.main()
