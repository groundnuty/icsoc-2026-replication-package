"""Regression tests for harness/verifier.py's Verifier.convergence_rel_s.

Stdlib `unittest` only, same import-shim convention as harness/test_schema.py.
Run either as:

    python3 -m unittest harness.test_verifier -v      # from repo root
    python3 harness/test_verifier.py                  # directly as a script

Verifier.convergence_rel_s is exercised as a PURE function — no network, no
live federation. Timelines are built by hand.

Covers (provider+term filter, probe_ok gate):
    1. happy path: target-provider probe_ok sample at expected_size converges
    2. provider filter (required): source-side expected_size sample (held from
       t=0) must NOT false-fire convergence for the target provider
    3. term filter: an expected_size sample on the target provider but a
       different term_id must not converge for the queried term
    4. probe-failure (required): an all-failed-probe timeline returns None,
       and is distinguishable from an all-zeros successful timeline
    5. probe_ok gate: a sample at expected_size with probe_ok=False must not
       converge
"""
from __future__ import annotations

import os
import socket
import sys
import time
import unittest
import urllib.error
from unittest.mock import patch

# --- make `harness` importable whether run as a script or as -m -------------
# Same convention as harness/test_schema.py.
if __package__ in (None, ""):
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    try:
        from harness import verifier
    except ImportError:
        import verifier
else:
    from . import verifier


TARGET_PROVIDER = "provider-de"
SOURCE_PROVIDER = "provider-cloud-pl"
TERM = "T1"
OTHER_TERM = "T2"
EXPECTED_SIZE = 4096


def _sample(t_rel_s, provider, term_id=TERM, value=0, probe_ok=True):
    return {
        "t_rel_s": t_rel_s,
        "term_id": term_id,
        "sli_name": "physicalSize",
        "value": value,
        "provider": provider,
        "probe_ok": probe_ok,
    }


class TestConvergenceRelS(unittest.TestCase):
    """Verifier.convergence_rel_s as a pure function over hand-built timelines."""

    def test_happy_path_target_provider_reaches_expected_size(self):
        timeline = {
            "samples": [
                _sample(0.0, TARGET_PROVIDER, value=0),
                _sample(2.0, TARGET_PROVIDER, value=EXPECTED_SIZE),
                _sample(4.0, TARGET_PROVIDER, value=EXPECTED_SIZE),
            ]
        }
        result = verifier.Verifier.convergence_rel_s(
            timeline, EXPECTED_SIZE, TARGET_PROVIDER, TERM
        )
        self.assertEqual(result, 2.0)

    def test_source_provider_holds_expected_size_from_t0_does_not_converge(self):
        # SOURCE holds expected_size trivially from t=0 (the file was written
        # there); the TARGET provider never reaches it in this timeline. The
        # provider filter must prevent the source-side sample from false-firing.
        timeline = {
            "samples": [
                _sample(0.0, SOURCE_PROVIDER, value=EXPECTED_SIZE),
                _sample(0.0, TARGET_PROVIDER, value=0),
                _sample(2.0, TARGET_PROVIDER, value=0),
                _sample(4.0, TARGET_PROVIDER, value=0),
            ]
        }
        result = verifier.Verifier.convergence_rel_s(
            timeline, EXPECTED_SIZE, TARGET_PROVIDER, TERM
        )
        self.assertIsNone(result)

    def test_term_filter_excludes_other_terms_expected_size_sample(self):
        timeline = {
            "samples": [
                _sample(0.0, TARGET_PROVIDER, term_id=OTHER_TERM, value=EXPECTED_SIZE),
                _sample(0.0, TARGET_PROVIDER, term_id=TERM, value=0),
                _sample(2.0, TARGET_PROVIDER, term_id=TERM, value=0),
            ]
        }
        result = verifier.Verifier.convergence_rel_s(
            timeline, EXPECTED_SIZE, TARGET_PROVIDER, TERM
        )
        self.assertIsNone(result)

    def test_all_failed_probes_returns_none_and_is_distinguishable_from_all_zeros(self):
        failed_timeline = {
            "samples": [
                _sample(0.0, TARGET_PROVIDER, value=None, probe_ok=False),
                _sample(2.0, TARGET_PROVIDER, value=None, probe_ok=False),
                _sample(4.0, TARGET_PROVIDER, value=None, probe_ok=False),
            ]
        }
        zeros_timeline = {
            "samples": [
                _sample(0.0, TARGET_PROVIDER, value=0, probe_ok=True),
                _sample(2.0, TARGET_PROVIDER, value=0, probe_ok=True),
            ]
        }
        self.assertIsNone(
            verifier.Verifier.convergence_rel_s(
                failed_timeline, EXPECTED_SIZE, TARGET_PROVIDER, TERM
            )
        )
        self.assertIsNone(
            verifier.Verifier.convergence_rel_s(
                zeros_timeline, EXPECTED_SIZE, TARGET_PROVIDER, TERM
            )
        )
        # Distinguishable at the sample level: the failed-probe timeline has
        # ZERO probe_ok=True samples, while the all-zeros (successful-probe,
        # empty-target) timeline has some — this is exactly the distinction
        # the probe_ok field exists to preserve (NotDetermined computability).
        failed_ok_count = sum(1 for s in failed_timeline["samples"] if s["probe_ok"] is True)
        zeros_ok_count = sum(1 for s in zeros_timeline["samples"] if s["probe_ok"] is True)
        self.assertEqual(failed_ok_count, 0)
        self.assertGreater(zeros_ok_count, 0)

    def test_probe_ok_false_at_expected_size_does_not_converge(self):
        timeline = {
            "samples": [
                _sample(0.0, TARGET_PROVIDER, value=EXPECTED_SIZE, probe_ok=False),
            ]
        }
        result = verifier.Verifier.convergence_rel_s(
            timeline, EXPECTED_SIZE, TARGET_PROVIDER, TERM
        )
        self.assertIsNone(result)


class TestGetProviderOnline(unittest.TestCase):
    """Verifier.get_provider_online — onezone liveness, _http mocked. Returns
    the `.online` bool on 200+dict+bool, else None (dead probe)."""

    def _verifier_with_http(self, status, body):
        v = verifier.Verifier(source_host="https://prov.example")
        v._http = lambda method, url, **kw: (status, body)  # type: ignore
        return v

    def test_online_true(self):
        v = self._verifier_with_http(200, {"providerId": "p1", "online": True, "name": "de"})
        self.assertIs(v.get_provider_online("p1"), True)

    def test_online_false(self):
        v = self._verifier_with_http(200, {"providerId": "p1", "online": False})
        self.assertIs(v.get_provider_online("p1"), False)

    def test_non_200_returns_none(self):
        v = self._verifier_with_http(503, {"online": True})
        self.assertIsNone(v.get_provider_online("p1"))

    def test_missing_online_field_returns_none(self):
        v = self._verifier_with_http(200, {"providerId": "p1", "name": "de"})
        self.assertIsNone(v.get_provider_online("p1"))

    def test_non_dict_body_returns_none(self):
        v = self._verifier_with_http(200, "not-a-dict")
        self.assertIsNone(v.get_provider_online("p1"))

    def test_non_bool_online_returns_none(self):
        # a malformed body where online is a string must not be trusted as bool
        v = self._verifier_with_http(200, {"online": "true"})
        self.assertIsNone(v.get_provider_online("p1"))


class TestPollStateTimelineHealth(unittest.TestCase):
    """poll_state_timeline emits provider_health samples per tick when
    health_provider_ids is given — get_distribution + get_provider_online
    mocked, no network. Single-tick (poll_until_rel_s=0) keeps it fast."""

    def _verifier(self, dist_value, online_value):
        v = verifier.Verifier(source_host="https://prov.example")
        # get_distribution returns a distribution dict that _physical_size reads;
        # feed a shape it understands for the target provider.
        v.get_distribution = lambda file_id: {  # type: ignore
            "distributionPerProvider": {
                TARGET_PROVIDER: {
                    "distributionPerStorageBackend": {"sb0": {"physicalSize": dist_value}}
                }
            }
        }
        v.get_provider_online = lambda pid: online_value  # type: ignore
        return v

    def test_health_sample_emitted_per_tick(self):
        v = self._verifier(dist_value=EXPECTED_SIZE, online_value=True)
        st = v.poll_state_timeline(
            file_id="f1", expected_size=EXPECTED_SIZE, target_provider_id=TARGET_PROVIDER,
            term_id=TERM, poll_interval_s=0.0, poll_until_rel_s=0.0, t0_epoch=0.0, t_max_s=30.0,
            health_provider_ids=[TARGET_PROVIDER],
        )
        health = [s for s in st["samples"] if s["sli_name"] == "provider_health"]
        physical = [s for s in st["samples"] if s["sli_name"] == "physicalSize"]
        self.assertEqual(len(health), 1)
        self.assertEqual(len(physical), 1)
        h = health[0]
        self.assertEqual(h["provider"], TARGET_PROVIDER)
        self.assertEqual(h["term_id"], TERM)
        self.assertIs(h["value"], True)
        self.assertIs(h["probe_ok"], True)

    def test_health_probe_failure_is_null_and_not_probe_ok(self):
        v = self._verifier(dist_value=EXPECTED_SIZE, online_value=None)  # dead health probe
        st = v.poll_state_timeline(
            file_id="f1", expected_size=EXPECTED_SIZE, target_provider_id=TARGET_PROVIDER,
            term_id=TERM, poll_interval_s=0.0, poll_until_rel_s=0.0, t0_epoch=0.0, t_max_s=30.0,
            health_provider_ids=[TARGET_PROVIDER],
        )
        h = [s for s in st["samples"] if s["sli_name"] == "provider_health"][0]
        self.assertIsNone(h["value"])
        self.assertIs(h["probe_ok"], False)

    def test_offline_provider_records_false(self):
        v = self._verifier(dist_value=0, online_value=False)
        st = v.poll_state_timeline(
            file_id="f1", expected_size=EXPECTED_SIZE, target_provider_id=TARGET_PROVIDER,
            term_id=TERM, poll_interval_s=0.0, poll_until_rel_s=0.0, t0_epoch=0.0, t_max_s=30.0,
            health_provider_ids=[TARGET_PROVIDER],
        )
        h = [s for s in st["samples"] if s["sli_name"] == "provider_health"][0]
        self.assertIs(h["value"], False)
        self.assertIs(h["probe_ok"], True)  # a successful probe of an OFFLINE provider

    def test_no_health_ids_emits_no_health_samples(self):
        v = self._verifier(dist_value=EXPECTED_SIZE, online_value=True)
        st = v.poll_state_timeline(
            file_id="f1", expected_size=EXPECTED_SIZE, target_provider_id=TARGET_PROVIDER,
            term_id=TERM, poll_interval_s=0.0, poll_until_rel_s=0.0, t0_epoch=0.0, t_max_s=30.0,
        )
        health = [s for s in st["samples"] if s["sli_name"] == "provider_health"]
        self.assertEqual(health, [])

    def test_emitted_timeline_is_schema_valid_for_health(self):
        # the health samples the producer emits must pass the schema's
        # per-SLI value typing (provider_health → bool when probe_ok True)
        from harness import schema as _schema
        v = self._verifier(dist_value=EXPECTED_SIZE, online_value=True)
        st = v.poll_state_timeline(
            file_id="f1", expected_size=EXPECTED_SIZE, target_provider_id=TARGET_PROVIDER,
            term_id=TERM, poll_interval_s=0.0, poll_until_rel_s=0.0, t0_epoch=0.0, t_max_s=30.0,
            health_provider_ids=[TARGET_PROVIDER],
        )
        for s in st["samples"]:
            if s["sli_name"] == "provider_health" and s["probe_ok"]:
                self.assertIsInstance(s["value"], bool)


class TestHttpRobustness(unittest.TestCase):
    """_http must survive network-layer failures (URLError/timeout/connection
    reset — the federation `Errno 104` reset class) with a bounded transient
    retry, returning the `(0, None)` dead-probe sentinel rather than raising.
    HTTPError (a real HTTP status) must still pass through unretried."""

    def _verifier(self):
        v = verifier.Verifier(source_host="https://prov.example")
        # Pre-seed a token so _http's self._get_token() doesn't try to mint
        # one (no network, no SSH) — same trick as the poll_state_timeline
        # tests above, one level down (those stub get_distribution instead).
        v._token = "faketoken"
        v._token_minted_at = time.time()
        return v

    def test_url_error_every_attempt_returns_sentinel_after_bounded_retries(self):
        v = self._verifier()
        with patch("harness.verifier.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("connection reset")) as m_urlopen, \
             patch("harness.verifier.time.sleep") as m_sleep:
            status, body = v._http("GET", "https://prov.example/x")
        self.assertEqual((status, body), (0, None))
        self.assertEqual(m_urlopen.call_count, verifier.HTTP_TRANSIENT_RETRIES)
        # backoff sleeps between attempts, not after the last exhausted one
        self.assertEqual(m_sleep.call_count, verifier.HTTP_TRANSIENT_RETRIES - 1)

    def test_socket_timeout_every_attempt_returns_sentinel(self):
        v = self._verifier()
        with patch("harness.verifier.urllib.request.urlopen",
                   side_effect=socket.timeout("timed out")) as m_urlopen, \
             patch("harness.verifier.time.sleep"):
            status, body = v._http("GET", "https://prov.example/x")
        self.assertEqual((status, body), (0, None))
        self.assertEqual(m_urlopen.call_count, verifier.HTTP_TRANSIENT_RETRIES)

    def test_url_error_twice_then_success_recovers(self):
        v = self._verifier()

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def getcode(self):
                return 200

            def read(self):
                return b'{"ok": true}'

        with patch(
            "harness.verifier.urllib.request.urlopen",
            side_effect=[urllib.error.URLError("reset"), urllib.error.URLError("reset"), _FakeResp()],
        ) as m_urlopen, patch("harness.verifier.time.sleep") as m_sleep:
            status, body = v._http("GET", "https://prov.example/x")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(m_urlopen.call_count, 3)
        self.assertEqual(m_sleep.call_count, 2)

    def test_http_error_is_not_retried_and_returns_real_status(self):
        v = self._verifier()

        def _raise_http_error(*a, **kw):
            err = urllib.error.HTTPError(
                url="https://prov.example/x", code=404, msg="Not Found",
                hdrs=None, fp=None,
            )
            err.read = lambda: b""
            raise err

        with patch("harness.verifier.urllib.request.urlopen",
                   side_effect=_raise_http_error) as m_urlopen, \
             patch("harness.verifier.time.sleep") as m_sleep:
            status, body = v._http("GET", "https://prov.example/x")
        self.assertEqual(status, 404)
        self.assertIsNone(body)
        self.assertEqual(m_urlopen.call_count, 1)
        m_sleep.assert_not_called()

    def test_dead_probe_from_network_failure_yields_none_not_exception(self):
        # Downstream contract: get_distribution must return None (dead-probe
        # path), not propagate an exception, when _http exhausts retries.
        v = self._verifier()
        v._http = lambda method, url, **kw: (0, None)  # type: ignore
        self.assertIsNone(v.get_distribution("file-1"))


class TestVerifyConvergenceMultiperspective(unittest.TestCase):
    """verify_convergence_multiperspective — outcome-side hardening helper
    (reuses Verifier._physical_size, the SAME predicate convergence_rel_s
    uses: physicalSize only, never virtualSize/QoS). All polls run against
    a virtual clock (sleep_fn/now_fn injected) — no real sleeping."""

    @staticmethod
    def _clock(start=0.0):
        state = {"t": start}

        def now():
            return state["t"]

        def sleep(s):
            state["t"] += s

        return now, sleep

    @staticmethod
    def _dist(value):
        return {
            "distributionPerProvider": {
                TARGET_PROVIDER: {
                    "distributionPerStorageBackend": {"sb0": {"physicalSize": value}}
                }
            }
        }

    def test_converges_after_required_consecutive_matches(self):
        v = verifier.Verifier(source_host="https://prov.example")
        v.get_distribution = lambda file_id: self._dist(EXPECTED_SIZE)  # type: ignore
        now, sleep = self._clock()

        result = verifier.verify_convergence_multiperspective(
            v, "f1", EXPECTED_SIZE, TARGET_PROVIDER,
            timeout_s=30.0, poll_interval_s=3.0, consecutive_required=2,
            sleep_fn=sleep, now_fn=now,
        )

        self.assertTrue(result["converged"])
        self.assertEqual(result["physical_size"], EXPECTED_SIZE)
        self.assertGreaterEqual(len(result["checks"]), 2)
        self.assertTrue(all(c["matches"] for c in result["checks"][-2:]))

    def test_never_matching_times_out_not_converged(self):
        v = verifier.Verifier(source_host="https://prov.example")
        v.get_distribution = lambda file_id: self._dist(0)  # type: ignore  # never reaches expected
        now, sleep = self._clock()

        result = verifier.verify_convergence_multiperspective(
            v, "f1", EXPECTED_SIZE, TARGET_PROVIDER,
            timeout_s=9.0, poll_interval_s=3.0, consecutive_required=2,
            sleep_fn=sleep, now_fn=now,
        )

        self.assertFalse(result["converged"])
        self.assertEqual(result["physical_size"], 0)

    def test_single_blip_then_reversion_does_not_falsely_converge(self):
        """A read that matches ONCE then reverts on the very next poll must
        NOT be reported as converged with consecutive_required=2 — this is
        exactly the eventually-consistent-blip case this helper exists to
        catch (a one-shot convergence_rel_s-style check would have missed
        it, mid-loop, without a second confirming poll)."""
        seq = [EXPECTED_SIZE, 0, 0]
        calls = {"n": -1}

        def get_dist(file_id):
            calls["n"] += 1
            idx = min(calls["n"], len(seq) - 1)
            return self._dist(seq[idx])
        v = verifier.Verifier(source_host="https://prov.example")
        v.get_distribution = get_dist  # type: ignore
        now, sleep = self._clock()

        result = verifier.verify_convergence_multiperspective(
            v, "f1", EXPECTED_SIZE, TARGET_PROVIDER,
            timeout_s=9.0, poll_interval_s=3.0, consecutive_required=2,
            sleep_fn=sleep, now_fn=now,
        )

        self.assertFalse(result["converged"])
        self.assertTrue(result["checks"][0]["matches"])   # the blip really happened
        self.assertFalse(result["checks"][1]["matches"])  # ...and reverted

    def test_dead_probe_does_not_crash_and_is_recorded(self):
        v = verifier.Verifier(source_host="https://prov.example")
        v.get_distribution = lambda file_id: None  # type: ignore  # dead probe every tick
        now, sleep = self._clock()

        result = verifier.verify_convergence_multiperspective(
            v, "f1", EXPECTED_SIZE, TARGET_PROVIDER,
            timeout_s=6.0, poll_interval_s=3.0, consecutive_required=2,
            sleep_fn=sleep, now_fn=now,
        )

        self.assertFalse(result["converged"])
        self.assertIsNone(result["physical_size"])
        self.assertTrue(all(c["probe_ok"] is False for c in result["checks"]))

    def test_get_distribution_raising_is_treated_as_dead_probe(self):
        def _raise(file_id):
            raise OSError("connection reset")
        v = verifier.Verifier(source_host="https://prov.example")
        v.get_distribution = _raise  # type: ignore
        now, sleep = self._clock()

        result = verifier.verify_convergence_multiperspective(
            v, "f1", EXPECTED_SIZE, TARGET_PROVIDER,
            timeout_s=3.0, poll_interval_s=3.0, consecutive_required=2,
            sleep_fn=sleep, now_fn=now,
        )

        self.assertFalse(result["converged"])
        self.assertTrue(all(c["probe_ok"] is False for c in result["checks"]))


if __name__ == "__main__":
    unittest.main()
