"""Regression tests for harness/provision.py — Onedata space create / support /
teardown. All offline: `http` is a scriptable fake callable; no network, no
live federation calls.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest

if __package__ in (None, ""):
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    try:
        from harness import provision
    except ImportError:
        import provision
else:
    from . import provision


def _fake_http(responses):
    """A scriptable http stub: each call consumes the next canned
    `(status, headers, body)` response in `responses`, in order. Records
    every call as `(method, url, body, content_type)` on `.calls`.
    """
    calls = []
    it = iter(responses)
    def _http(method, url, body=None, content_type=None, timeout=60):
        calls.append((method, url, body, content_type))
        try:
            return next(it)
        except StopIteration:
            raise AssertionError(f"_fake_http: no canned response left for call {method} {url}")
    _http.calls = calls
    return _http


P1 = provision.ProviderSupport(label="p1", onepanel_host="https://p1.example", storage_id="storage-p1")
P2 = provision.ProviderSupport(label="p2", onepanel_host="https://p2.example", storage_id="storage-p2")


class TestCreateSpace(unittest.TestCase):
    def test_parses_space_id_from_location_header(self):
        http = _fake_http([(201, {"Location": "/api/v3/onezone/user/spaces/abc123"}, None)])
        p = provision.Provisioner(token="tok", http=http)

        space_id = p.create_space("icsoc-sweep-1")

        self.assertEqual(space_id, "abc123")
        method, url, body, content_type = http.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, f"{provision.ONEZONE_URL}/api/v3/onezone/user/spaces")
        self.assertEqual(content_type, "application/json")
        self.assertEqual(json.loads(body), {"name": "icsoc-sweep-1"})

    def test_lowercase_location_header_also_parses(self):
        http = _fake_http([(201, {"location": "/spaces/xyz"}, None)])
        p = provision.Provisioner(token="tok", http=http)
        self.assertEqual(p.create_space("n"), "xyz")

    def test_raises_on_non_201(self):
        http = _fake_http([(400, {}, {"error": "bad request"})])
        p = provision.Provisioner(token="tok", http=http)
        with self.assertRaises(provision.ProvisionError):
            p.create_space("n")

    def test_raises_on_missing_location_header(self):
        http = _fake_http([(201, {}, None)])
        p = provision.Provisioner(token="tok", http=http)
        with self.assertRaises(provision.ProvisionError):
            p.create_space("n")

    def test_raises_on_location_header_without_spaces_segment(self):
        http = _fake_http([(201, {"Location": "/api/v3/onezone/user/somethingelse/abc"}, None)])
        p = provision.Provisioner(token="tok", http=http)
        with self.assertRaises(provision.ProvisionError):
            p.create_space("n")


class TestSupportSpace(unittest.TestCase):
    def test_posts_invite_mint_then_onepanel_support(self):
        http = _fake_http([
            (201, {}, {"token": "invite-tok-1"}),  # mint invite (real API returns 201)
            (201, {}, None),                        # onepanel support
        ])
        p = provision.Provisioner(token="tok", http=http)

        p.support_space("space-1", P1, size_bytes=555)

        self.assertEqual(len(http.calls), 2)

        m1, u1, b1, ct1 = http.calls[0]
        self.assertEqual(m1, "POST")
        self.assertEqual(u1, f"{provision.ONEZONE_URL}/api/v3/onezone/user/tokens/temporary")
        self.assertEqual(ct1, "application/json")
        payload1 = json.loads(b1)
        self.assertEqual(payload1["type"]["inviteToken"]["inviteType"], "supportSpace")
        self.assertEqual(payload1["type"]["inviteToken"]["spaceId"], "space-1")
        self.assertEqual(payload1["caveats"][0]["type"], "time")
        self.assertIsInstance(payload1["caveats"][0]["validUntil"], int)

        m2, u2, b2, ct2 = http.calls[1]
        self.assertEqual(m2, "POST")
        self.assertEqual(u2, "https://p1.example/api/v3/onepanel/provider/spaces")
        self.assertEqual(ct2, "application/json")
        payload2 = json.loads(b2)
        self.assertEqual(payload2["token"], "invite-tok-1")
        self.assertEqual(payload2["size"], 555)
        self.assertEqual(payload2["storageId"], "storage-p1")

    def test_raises_when_invite_mint_fails(self):
        http = _fake_http([(500, {}, {"error": "boom"})])
        p = provision.Provisioner(token="tok", http=http)
        with self.assertRaises(provision.ProvisionError):
            p.support_space("space-1", P1, size_bytes=1)

    def test_raises_when_invite_mint_response_missing_token(self):
        http = _fake_http([(200, {}, {"not_token": "x"})])
        p = provision.Provisioner(token="tok", http=http)
        with self.assertRaises(provision.ProvisionError):
            p.support_space("space-1", P1, size_bytes=1)

    def test_raises_when_onepanel_support_fails(self):
        http = _fake_http([
            (200, {}, {"token": "invite-tok-1"}),
            (500, {}, {"error": "onepanel rejected"}),
        ])
        p = provision.Provisioner(token="tok", http=http)
        with self.assertRaises(provision.ProvisionError):
            p.support_space("space-1", P1, size_bytes=1)

    def test_accepts_200_or_201_from_onepanel(self):
        http = _fake_http([
            (200, {}, {"token": "invite-tok-1"}),
            (200, {}, None),  # onepanel returns 200 instead of 201 — both are accepted
        ])
        p = provision.Provisioner(token="tok", http=http)
        p.support_space("space-1", P1, size_bytes=1)  # should not raise


class TestProvisionHappyPath(unittest.TestCase):
    def test_create_then_support_each_provider_in_order(self):
        http = _fake_http([
            (201, {"Location": "/spaces/space-xyz"}, None),  # create_space
            (200, {}, {"token": "invite-1"}),                  # mint invite p1
            (201, {}, None),                                   # support p1
            (200, {}, {"token": "invite-2"}),                  # mint invite p2
            (200, {}, None),                                   # support p2
        ])
        p = provision.Provisioner(token="tok", http=http)

        result = p.provision("sweep-space", providers=(P1, P2), size_bytes=999)

        self.assertEqual(result, {"space_id": "space-xyz", "name": "sweep-space", "supported": ["p1", "p2"]})
        self.assertEqual(len(http.calls), 5)
        # call order: create_space, then p1's (mint, support), then p2's (mint, support)
        self.assertEqual(http.calls[0][1], f"{provision.ONEZONE_URL}/api/v3/onezone/user/spaces")
        self.assertEqual(http.calls[1][1], f"{provision.ONEZONE_URL}/api/v3/onezone/user/tokens/temporary")
        self.assertEqual(http.calls[2][1], "https://p1.example/api/v3/onepanel/provider/spaces")
        self.assertEqual(http.calls[3][1], f"{provision.ONEZONE_URL}/api/v3/onezone/user/tokens/temporary")
        self.assertEqual(http.calls[4][1], "https://p2.example/api/v3/onepanel/provider/spaces")
        # size_bytes propagated to both support calls
        self.assertEqual(json.loads(http.calls[2][2])["size"], 999)
        self.assertEqual(json.loads(http.calls[4][2])["size"], 999)

    def test_default_providers_and_size(self):
        http = _fake_http([
            (201, {"Location": "/spaces/space-1"}, None),
            (200, {}, {"token": "i1"}), (201, {}, None),
            (200, {}, {"token": "i2"}), (201, {}, None),
        ])
        p = provision.Provisioner(token="tok", http=http)
        result = p.provision("n")
        self.assertEqual(result["supported"], ["cloud-pl", "de"])
        self.assertEqual(json.loads(http.calls[2][2])["size"], 1073741824)


class TestProvisionTeardownOnFailure(unittest.TestCase):
    def test_tears_down_partial_supports_and_reraises(self):
        http = _fake_http([
            (201, {"Location": "/spaces/space-xyz"}, None),  # create_space
            (200, {}, {"token": "invite-1"}),                  # mint invite p1
            (201, {}, None),                                   # support p1 OK
            (200, {}, {"token": "invite-2"}),                  # mint invite p2
            (500, {}, {"error": "boom"}),                       # support p2 FAILS
            (200, {}, None),                                    # teardown DELETE p1 onepanel
            (200, {}, None),                                    # teardown DELETE space
        ])
        p = provision.Provisioner(token="tok", http=http)

        with self.assertRaises(provision.ProvisionError):
            p.provision("sweep-space", providers=(P1, P2), size_bytes=1)

        self.assertEqual(len(http.calls), 7)
        # teardown only revokes p1 (the provider that actually succeeded) — not p2
        teardown_delete_call = http.calls[5]
        self.assertEqual(teardown_delete_call[0], "DELETE")
        self.assertEqual(teardown_delete_call[1], "https://p1.example/api/v3/onepanel/provider/spaces/space-xyz")
        space_delete_call = http.calls[6]
        self.assertEqual(space_delete_call[0], "DELETE")
        self.assertEqual(space_delete_call[1], f"{provision.ONEZONE_URL}/api/v3/onezone/spaces/space-xyz")

    def test_first_provider_failure_still_deletes_space_only(self):
        http = _fake_http([
            (201, {"Location": "/spaces/space-1"}, None),   # create_space
            (200, {}, {"token": "invite-1"}),                  # mint invite p1
            (500, {}, {"error": "boom"}),                       # support p1 FAILS (nothing succeeded yet)
            (200, {}, None),                                    # teardown DELETE space (no providers to revoke)
        ])
        p = provision.Provisioner(token="tok", http=http)

        with self.assertRaises(provision.ProvisionError):
            p.provision("sweep-space", providers=(P1, P2), size_bytes=1)

        self.assertEqual(len(http.calls), 4)
        self.assertEqual(http.calls[3][0], "DELETE")
        self.assertEqual(http.calls[3][1], f"{provision.ONEZONE_URL}/api/v3/onezone/spaces/space-1")


class TestTeardownSpace(unittest.TestCase):
    def test_best_effort_continues_after_a_raising_delete(self):
        calls = []
        def http(method, url, body=None, content_type=None, timeout=60):
            calls.append((method, url))
            if "p1.example" in url:
                raise OSError("connection reset")
            return 200, {}, None
        p = provision.Provisioner(token="tok", http=http)

        result = p.teardown_space("space-1", (P1, P2))

        self.assertEqual(result, {"p1": False, "p2": True, "space_deleted": True})
        # all three DELETEs were attempted despite p1 raising
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], ("DELETE", "https://p1.example/api/v3/onepanel/provider/spaces/space-1"))
        self.assertEqual(calls[1], ("DELETE", "https://p2.example/api/v3/onepanel/provider/spaces/space-1"))
        self.assertEqual(calls[2], ("DELETE", f"{provision.ONEZONE_URL}/api/v3/onezone/spaces/space-1"))

    def test_non_2xx_status_counts_as_not_ok_but_does_not_stop(self):
        http = _fake_http([
            (404, {}, {"error": "not found"}),  # p1 delete -> not ok
            (200, {}, None),                      # p2 delete -> ok
            (500, {}, None),                      # space delete -> not ok
        ])
        p = provision.Provisioner(token="tok", http=http)

        result = p.teardown_space("space-1", (P1, P2))

        self.assertEqual(result, {"p1": False, "p2": True, "space_deleted": False})
        self.assertEqual(len(http.calls), 3)

    def test_empty_providers_only_deletes_space(self):
        http = _fake_http([(200, {}, None)])
        p = provision.Provisioner(token="tok", http=http)
        result = p.teardown_space("space-1", ())
        self.assertEqual(result, {"space_deleted": True})
        self.assertEqual(len(http.calls), 1)


class TestWaitForSpaceReady(unittest.TestCase):
    """Provisioner.wait_for_space_ready — multi-perspective, eventual-
    consistency-safe readiness precheck (guards against trusting
    support-propagation API-boundary success alone). All http/verifier
    interactions are faked; `sleep_fn`/`now_fn` use a virtual clock so
    bounded-wait/timeout tests never actually sleep.
    """

    PL = "ec4761ab47ecda487c46edd328179824chc3bd"
    DE = "7cfe30bef82a2c904c15624f752c58ddchd932"

    @staticmethod
    def _clock(start=0.0):
        state = {"t": start}

        def now():
            return state["t"]

        def sleep(s):
            state["t"] += s

        return now, sleep

    @staticmethod
    def _fake_verifier(online_map):
        class _FakeVerifier:
            def get_provider_online(self, pid):
                return online_map.get(pid)
        return _FakeVerifier()

    def _http_both_ok(self, providers_dict, providers_list):
        def http(method, url, body=None, content_type=None, timeout=60):
            if url.endswith("/onezone/user/spaces/space-1"):
                return 200, {}, {"providers": providers_dict}
            if url.endswith("/onezone/spaces/space-1/providers"):
                return 200, {}, providers_list
            raise AssertionError(f"unexpected url {url}")
        return http

    def test_ready_when_all_three_perspectives_agree(self):
        http = self._http_both_ok({self.PL: 1, self.DE: 1}, [self.PL, self.DE])
        p = provision.Provisioner(token="tok", http=http)
        v = self._fake_verifier({self.PL: True, self.DE: True})
        now, sleep = self._clock()

        result = p.wait_for_space_ready(
            "space-1", [self.PL, self.DE], verifier=v,
            timeout_s=30.0, poll_interval_s=5.0, sleep_fn=sleep, now_fn=now,
        )

        self.assertTrue(result["ready"])
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["elapsed_s"], 0.0)
        self.assertEqual(result["perspectives"]["user_spaces_providers"]["providers"],
                          {self.PL: 1, self.DE: 1})

    def test_never_reads_supportingProviders_even_when_populated_but_wrong(self):
        """The admin-shaped `supportingProviders` field is deliberately
        POPULATED (looks like support) but the CORRECT `.providers` field
        on the user-scoped endpoint is EMPTY. Readiness must be False —
        proving the code keys on `.providers`, never on the null-trap field."""
        def http(method, url, body=None, content_type=None, timeout=60):
            if url.endswith("/onezone/user/spaces/space-1"):
                return 200, {}, {
                    "supportingProviders": {self.PL: 1, self.DE: 1},  # trap field — must be ignored
                    "providers": {},
                }
            if url.endswith("/onezone/spaces/space-1/providers"):
                return 200, {}, []
            raise AssertionError(f"unexpected url {url}")
        p = provision.Provisioner(token="tok", http=http)
        v = self._fake_verifier({self.PL: True, self.DE: True})
        now, sleep = self._clock()

        result = p.wait_for_space_ready(
            "space-1", [self.PL, self.DE], verifier=v,
            timeout_s=10.0, poll_interval_s=5.0, sleep_fn=sleep, now_fn=now,
        )

        self.assertFalse(result["ready"])
        self.assertIn(self.PL, result["missing"])
        self.assertIn(self.DE, result["missing"])
        self.assertEqual(result["perspectives"]["user_spaces_providers"]["providers"], {})

    def test_missing_provider_in_user_view_blocks_readiness_then_times_out(self):
        http = self._http_both_ok({self.PL: 1}, [self.PL, self.DE])  # DE missing from .providers
        p = provision.Provisioner(token="tok", http=http)
        v = self._fake_verifier({self.PL: True, self.DE: True})
        now, sleep = self._clock()

        result = p.wait_for_space_ready(
            "space-1", [self.PL, self.DE], verifier=v,
            timeout_s=12.0, poll_interval_s=5.0, sleep_fn=sleep, now_fn=now,
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["missing"], [self.DE])
        self.assertGreaterEqual(result["elapsed_s"], 12.0)  # gave up at timeout, never converged

    def test_transient_http_error_mid_wait_recovers_on_next_poll(self):
        calls = {"n": 0}

        def http(method, url, body=None, content_type=None, timeout=60):
            if url.endswith("/onezone/user/spaces/space-1"):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("connection reset")  # transient, first tick only
                return 200, {}, {"providers": {self.PL: 1, self.DE: 1}}
            if url.endswith("/onezone/spaces/space-1/providers"):
                return 200, {}, [self.PL, self.DE]
            raise AssertionError(f"unexpected url {url}")

        p = provision.Provisioner(token="tok", http=http)
        v = self._fake_verifier({self.PL: True, self.DE: True})
        now, sleep = self._clock()

        result = p.wait_for_space_ready(
            "space-1", [self.PL, self.DE], verifier=v,
            timeout_s=30.0, poll_interval_s=5.0, sleep_fn=sleep, now_fn=now,
        )

        self.assertTrue(result["ready"])
        self.assertGreaterEqual(calls["n"], 2)  # first tick errored, second succeeded — no abort

    def test_offline_provider_blocks_readiness(self):
        http = self._http_both_ok({self.PL: 1, self.DE: 1}, [self.PL, self.DE])
        p = provision.Provisioner(token="tok", http=http)
        v = self._fake_verifier({self.PL: True, self.DE: False})  # de reports offline
        now, sleep = self._clock()

        result = p.wait_for_space_ready(
            "space-1", [self.PL, self.DE], verifier=v,
            timeout_s=10.0, poll_interval_s=5.0, sleep_fn=sleep, now_fn=now,
        )

        self.assertFalse(result["ready"])
        self.assertIn(self.DE, result["missing"])
        self.assertEqual(result["perspectives"]["provider_online"]["online"][self.DE], False)

    def test_missing_provider_in_list_endpoint_blocks_readiness(self):
        """Perspective 2 (the list endpoint) disagreeing alone is enough to
        block readiness, even when perspective 1 + 3 both agree."""
        http = self._http_both_ok({self.PL: 1, self.DE: 1}, [self.PL])  # DE missing from list
        p = provision.Provisioner(token="tok", http=http)
        v = self._fake_verifier({self.PL: True, self.DE: True})
        now, sleep = self._clock()

        result = p.wait_for_space_ready(
            "space-1", [self.PL, self.DE], verifier=v,
            timeout_s=10.0, poll_interval_s=5.0, sleep_fn=sleep, now_fn=now,
        )

        self.assertFalse(result["ready"])
        self.assertIn(self.DE, result["missing"])

    def test_default_verifier_construction_is_lazy_and_not_touched_when_explicit(self):
        """`verifier=` is honored when supplied — the lazy default-Verifier
        construction path (`Verifier(source_host=ONEZONE_URL)`, only
        exercised when `verifier is None`) is intentionally NOT exercised
        by this offline suite: constructing a real default `Verifier` and
        calling `get_provider_online` on it would mint a token and attempt
        a REAL network call (see `verifier.Verifier._mint_token` /
        `fixtures.mint_token_local`), which is out of bounds for an
        offline, pure, no-federation test suite.
        This test only proves the explicit-`verifier=` path is what runs
        when a caller supplies one — i.e. the fake never gets silently
        bypassed in favor of a live default.
        """
        http = self._http_both_ok({self.PL: 1, self.DE: 1}, [self.PL, self.DE])
        p = provision.Provisioner(token="tok", http=http)
        calls = {"n": 0}

        def _get_provider_online(pid):
            calls["n"] += 1
            return True
        v = self._fake_verifier({})
        v.get_provider_online = _get_provider_online
        now, sleep = self._clock()

        result = p.wait_for_space_ready(
            "space-1", [self.PL, self.DE], verifier=v,
            timeout_s=10.0, poll_interval_s=5.0, sleep_fn=sleep, now_fn=now,
        )

        self.assertTrue(result["ready"])
        self.assertEqual(calls["n"], 2)  # the fake was actually invoked, once per provider


class _FakeFixtureMgr:
    """Duck-typed fake for `fixtures.FixtureManager` — exposes only the two
    methods `assert_transfer_path_healthy` calls. `schedule_results` is a
    list consumed in order (one entry per `schedule_file_replication`
    call); the LAST entry repeats once exhausted."""

    def __init__(self, write_result=None, write_exc=None, schedule_results=None, schedule_exc=None):
        self.write_result = write_result or {"file_id": "file-1", "full_path": "/x", "expected_size": 4096}
        self.write_exc = write_exc
        self.schedule_results = list(schedule_results) if schedule_results is not None else [
            {"status": 201, "body": {"transferId": "xfer-1"}, "transfer_id": "xfer-1"}
        ]
        self.schedule_exc = schedule_exc
        self.write_calls = []
        self.schedule_calls = []

    def write_trial_file(self, rel_path, content_bytes):
        self.write_calls.append((rel_path, content_bytes))
        if self.write_exc is not None:
            raise self.write_exc
        return self.write_result

    def schedule_file_replication(self, file_id, target_provider_id):
        self.schedule_calls.append((file_id, target_provider_id))
        if self.schedule_exc is not None:
            raise self.schedule_exc
        idx = min(len(self.schedule_calls) - 1, len(self.schedule_results) - 1)
        return self.schedule_results[idx]


class _FakeCanaryVerifier:
    """Duck-typed fake for `verifier.Verifier` — exposes only
    `get_transfer_status` + `get_distribution`. Each sequence is consumed in
    order; the LAST entry repeats once exhausted (so a short list expresses
    "eventually settles at this value"). A sequence entry may be a plain
    value or a zero-arg callable (for raising on a specific tick)."""

    def __init__(self, transfer_status_seq=None, distribution_seq=None):
        self.transfer_status_seq = list(transfer_status_seq) if transfer_status_seq is not None else [None]
        self.distribution_seq = list(distribution_seq) if distribution_seq is not None else [None]
        self._t_calls = 0
        self._d_calls = 0

    # NOTE: the index counter is advanced BEFORE invoking a (possibly-
    # raising) callable entry, never after — a raising entry models a
    # transient dead probe on ONE tick, not every subsequent tick; advancing
    # only on successful return would re-raise forever.

    def get_transfer_status(self, transfer_id):
        idx = min(self._t_calls, len(self.transfer_status_seq) - 1)
        self._t_calls += 1
        val = self.transfer_status_seq[idx]
        return val() if callable(val) else val

    def get_distribution(self, file_id):
        idx = min(self._d_calls, len(self.distribution_seq) - 1)
        self._d_calls += 1
        val = self.distribution_seq[idx]
        return val() if callable(val) else val


def _canary_dist(physical_size, provider_id="de-1"):
    return {
        "distributionPerProvider": {
            provider_id: {"distributionPerStorageBackend": {"sb0": {"physicalSize": physical_size}}}
        }
    }


class TestAssertTransferPathHealthy(unittest.TestCase):
    """provision.assert_transfer_path_healthy — the fail-fast transfer-health
    canary. All collaborators (fixture_mgr/verifier) are duck-typed fakes;
    sleep_fn/now_fn use a virtual clock so no test actually sleeps or waits.
    """

    PROVIDER = "de-1"

    @staticmethod
    def _clock(start=0.0):
        state = {"t": start}

        def now():
            return state["t"]

        def sleep(s):
            state["t"] += s

        return now, sleep

    def test_healthy_when_bytes_land_immediately(self):
        fm = _FakeFixtureMgr()
        v = _FakeCanaryVerifier(
            transfer_status_seq=[{"replicationStatus": "scheduled", "startTime": 0}],
            distribution_seq=[_canary_dist(4096, self.PROVIDER)],
        )
        now, sleep = self._clock()

        result = provision.assert_transfer_path_healthy(
            fm, v, self.PROVIDER, timeout_s=60.0, poll_interval_s=3.0,
            sleep_fn=sleep, now_fn=now,
        )

        self.assertTrue(result["healthy"])
        self.assertEqual(result["physical_size"], 4096)
        self.assertEqual(result["elapsed_s"], 0.0)  # converged on the FIRST poll — never waited
        self.assertEqual(len(fm.write_calls), 1)
        self.assertEqual(len(fm.schedule_calls), 1)

    def test_never_gates_healthy_on_replication_status_completed(self):
        """Design deviation from a literal "completed AND bytes" reading —
        documented in the function's own docstring: replicationStatus lags
        physicalSize for the common case (transfers converge while status
        still reads 'scheduled'). Gating healthy on status=='completed' would
        false-negative on this common, perfectly-healthy case — physicalSize
        alone is the frozen convergence predicate."""
        fm = _FakeFixtureMgr()
        v = _FakeCanaryVerifier(
            transfer_status_seq=[{"replicationStatus": "scheduled", "startTime": 12345}],
            distribution_seq=[_canary_dist(4096, self.PROVIDER)],
        )
        now, sleep = self._clock()

        result = provision.assert_transfer_path_healthy(
            fm, v, self.PROVIDER, timeout_s=60.0, poll_interval_s=3.0,
            sleep_fn=sleep, now_fn=now,
        )

        self.assertTrue(result["healthy"])
        self.assertEqual(result["final_status"], "scheduled")  # status never flipped -- irrelevant to the verdict

    def test_unhealthy_at_timeout_on_stuck_scheduled_start_time_zero(self):
        fm = _FakeFixtureMgr()
        v = _FakeCanaryVerifier(
            transfer_status_seq=[{"replicationStatus": "scheduled", "startTime": 0}],
            distribution_seq=[_canary_dist(0, self.PROVIDER)],
        )
        now, sleep = self._clock()

        result = provision.assert_transfer_path_healthy(
            fm, v, self.PROVIDER, timeout_s=60.0, poll_interval_s=3.0,
            sleep_fn=sleep, now_fn=now,
        )

        self.assertFalse(result["healthy"])
        self.assertEqual(result["final_status"], "scheduled")
        self.assertEqual(result["physical_size"], 0)
        self.assertIn("stuck-scheduled/startTime==0", result["reason"])
        # NEVER waits beyond the bounded window (one poll_interval slack for the tick that detects it)
        self.assertGreaterEqual(result["elapsed_s"], 60.0)
        self.assertLess(result["elapsed_s"], 60.0 + 3.0 + 0.001)

    def test_unhealthy_at_timeout_on_completed_with_zero_bytes(self):
        """The mirror-image failure class: status reads 'completed' while
        physicalSize never arrives. physicalSize must still gate UNHEALTHY
        here — a status-trusting check would false-positive 'healthy' on
        this exact class."""
        fm = _FakeFixtureMgr()
        v = _FakeCanaryVerifier(
            transfer_status_seq=[{"replicationStatus": "completed", "startTime": 111}],
            distribution_seq=[_canary_dist(0, self.PROVIDER)],
        )
        now, sleep = self._clock()

        result = provision.assert_transfer_path_healthy(
            fm, v, self.PROVIDER, timeout_s=30.0, poll_interval_s=5.0,
            sleep_fn=sleep, now_fn=now,
        )

        self.assertFalse(result["healthy"])
        self.assertEqual(result["final_status"], "completed")
        self.assertEqual(result["physical_size"], 0)

    def test_short_circuits_on_unauthorized_signature_before_timeout(self):
        fm = _FakeFixtureMgr()
        v = _FakeCanaryVerifier(
            transfer_status_seq=[{
                "replicationStatus": "failed",
                "error": "connection unauthorized",
            }],
            distribution_seq=[_canary_dist(0, self.PROVIDER)],
        )
        now, sleep = self._clock()

        result = provision.assert_transfer_path_healthy(
            fm, v, self.PROVIDER, timeout_s=60.0, poll_interval_s=3.0,
            sleep_fn=sleep, now_fn=now,
        )

        self.assertFalse(result["healthy"])
        self.assertIn("unauthorized", result["reason"])
        # fail-fast: returned on the FIRST poll, nowhere near the 60s window
        self.assertEqual(result["elapsed_s"], 0.0)

    def test_schedule_transient_space_not_supported_by_retries_then_succeeds(self):
        fm = _FakeFixtureMgr(
            schedule_results=[
                {"status": 400, "body": {"error": "spaceNotSupportedBy"}, "transfer_id": None},
                {"status": 201, "body": {"transferId": "xfer-2"}, "transfer_id": "xfer-2"},
            ],
        )
        v = _FakeCanaryVerifier(
            transfer_status_seq=[{"replicationStatus": "scheduled", "startTime": 1}],
            distribution_seq=[_canary_dist(4096, self.PROVIDER)],
        )
        now, sleep = self._clock()

        result = provision.assert_transfer_path_healthy(
            fm, v, self.PROVIDER, timeout_s=60.0, poll_interval_s=3.0,
            schedule_max_retries=3, schedule_retry_backoff_s=2.0,
            sleep_fn=sleep, now_fn=now,
        )

        self.assertTrue(result["healthy"])
        self.assertEqual(len(fm.schedule_calls), 2)  # one transient 400, one success

    def test_schedule_permanent_failure_does_not_retry(self):
        fm = _FakeFixtureMgr(
            schedule_results=[{"status": 500, "body": {"error": "boom"}, "transfer_id": None}],
        )
        v = _FakeCanaryVerifier()
        now, sleep = self._clock()

        result = provision.assert_transfer_path_healthy(
            fm, v, self.PROVIDER, timeout_s=60.0, poll_interval_s=3.0,
            schedule_max_retries=3, max_attempts=1, sleep_fn=sleep, now_fn=now,
        )  # max_attempts=1 -- isolates the SCHEDULE-level retry budget under
           # test here from the OUTER canary-attempt retry (tested separately
           # in TestAssertTransferPathHealthyOuterRetry below)

        self.assertFalse(result["healthy"])
        self.assertEqual(len(fm.schedule_calls), 1)  # non-transient -- no retry burned
        self.assertIn("500", result["reason"])

    def test_schedule_retries_exhausted_all_transient(self):
        fm = _FakeFixtureMgr(
            schedule_results=[{"status": 400, "body": {"error": "spaceNotSupportedBy"}, "transfer_id": None}],
        )
        v = _FakeCanaryVerifier()
        now, sleep = self._clock()

        result = provision.assert_transfer_path_healthy(
            fm, v, self.PROVIDER, timeout_s=60.0, poll_interval_s=3.0,
            schedule_max_retries=3, schedule_retry_backoff_s=1.0, max_attempts=1,
            sleep_fn=sleep, now_fn=now,
        )  # max_attempts=1 -- isolates the SCHEDULE-level retry budget under
           # test here from the OUTER canary-attempt retry (tested separately
           # in TestAssertTransferPathHealthyOuterRetry below)

        self.assertFalse(result["healthy"])
        self.assertEqual(len(fm.schedule_calls), 3)  # exhausted the retry budget, never unbounded
        self.assertIn("retries exhausted", result["reason"])

    def test_write_failure_returns_unhealthy_without_raising(self):
        fm = _FakeFixtureMgr(write_exc=RuntimeError("PUT failed status=500"))
        v = _FakeCanaryVerifier()
        now, sleep = self._clock()

        result = provision.assert_transfer_path_healthy(
            fm, v, self.PROVIDER, timeout_s=60.0, sleep_fn=sleep, now_fn=now,
        )  # must not raise

        self.assertFalse(result["healthy"])
        self.assertIn("write_trial_file failed", result["reason"])
        self.assertEqual(len(fm.schedule_calls), 0)  # never got past the write

    def test_schedule_raising_returns_unhealthy_without_raising(self):
        fm = _FakeFixtureMgr(schedule_exc=OSError("connection reset"))
        v = _FakeCanaryVerifier()
        now, sleep = self._clock()

        result = provision.assert_transfer_path_healthy(
            fm, v, self.PROVIDER, timeout_s=60.0, sleep_fn=sleep, now_fn=now,
        )  # must not raise

        self.assertFalse(result["healthy"])
        self.assertIn("schedule_file_replication raised", result["reason"])

    def test_dead_probes_mid_poll_are_tolerated_then_recover(self):
        """A transient get_transfer_status/get_distribution failure mid-poll
        must not crash the canary — same probe_ok discipline as
        poll_state_timeline's samples."""
        def _raise_transfer(_id=None):
            raise OSError("connection reset")

        def _raise_dist(_id=None):
            raise OSError("connection reset")

        fm = _FakeFixtureMgr()
        v = _FakeCanaryVerifier(
            transfer_status_seq=[_raise_transfer, {"replicationStatus": "scheduled", "startTime": 1}],
            distribution_seq=[_raise_dist, _canary_dist(4096, self.PROVIDER)],
        )
        now, sleep = self._clock()

        result = provision.assert_transfer_path_healthy(
            fm, v, self.PROVIDER, timeout_s=60.0, poll_interval_s=3.0,
            sleep_fn=sleep, now_fn=now,
        )

        self.assertTrue(result["healthy"])
        self.assertEqual(result["elapsed_s"], 3.0)  # one dead tick, then recovered on the next

    def test_never_waits_beyond_canary_timeout_when_permanently_stuck(self):
        fm = _FakeFixtureMgr()
        v = _FakeCanaryVerifier(
            transfer_status_seq=[{"replicationStatus": "scheduled", "startTime": 0}],
            distribution_seq=[_canary_dist(0, self.PROVIDER)],
        )
        now, sleep = self._clock()

        result = provision.assert_transfer_path_healthy(
            fm, v, self.PROVIDER, timeout_s=provision.CANARY_TIMEOUT_S,
            poll_interval_s=provision.CANARY_POLL_INTERVAL_S,
            sleep_fn=sleep, now_fn=now,
        )

        self.assertFalse(result["healthy"])
        self.assertLess(
            result["elapsed_s"],
            provision.CANARY_TIMEOUT_S + provision.CANARY_POLL_INTERVAL_S + 0.001,
        )


class TestAssertTransferPathHealthyOuterRetry(unittest.TestCase):
    """The bounded OUTER retry wrapping `_canary_attempt` above. The live
    transfer path has an intermittent "completed-with-0-bytes" transient
    failure — this retry distinguishes that TRANSIENT failure (clears on a
    fresh attempt) from a PERSISTENT stall (fails every attempt), so a single
    transient failure no longer fail-fast-aborts an entire sweep, while a
    genuine stall still fails loud."""

    PROVIDER = "de-1"

    @staticmethod
    def _clock(start=0.0):
        state = {"t": start}

        def now():
            return state["t"]

        def sleep(s):
            state["t"] += s

        return now, sleep

    def test_transient_flake_then_success(self):
        """Attempt 1 has a transient failure (terminal status, 0 bytes, never
        converges within its own timeout window); attempt 2 uses a FRESH
        canary write and converges on its first poll. Overall result must be
        healthy with attempts==2 — a single transient failure must NOT abort
        the whole canary."""
        fm = _FakeFixtureMgr()
        v = _FakeCanaryVerifier(
            transfer_status_seq=[{"replicationStatus": "completed", "startTime": 111}],
            distribution_seq=[
                _canary_dist(0, self.PROVIDER),     # attempt 1, poll 1: transient failure
                _canary_dist(0, self.PROVIDER),     # attempt 1, poll 2 (timeout): still failing
                _canary_dist(4096, self.PROVIDER),  # attempt 2, poll 1: converged
            ],
        )
        now, sleep = self._clock()

        result = provision.assert_transfer_path_healthy(
            fm, v, self.PROVIDER, timeout_s=3.0, poll_interval_s=3.0,
            sleep_fn=sleep, now_fn=now,
        )

        self.assertTrue(result["healthy"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["physical_size"], 4096)
        # a FRESH canary file per attempt -- the failed attempt's file is
        # never reused/re-polled.
        self.assertEqual(len(fm.write_calls), 2)
        self.assertNotEqual(fm.write_calls[0][0], fm.write_calls[1][0])
        self.assertEqual(len(fm.schedule_calls), 2)

    def test_persistent_wedge_exhausts_all_attempts(self):
        """Every attempt fails identically -- a genuine persistent stall, not
        a one-off. Must exhaust `max_attempts` (never retry unbounded) and
        report healthy=False with a reason that makes clear this is
        persistent, not a transient failure -- the fail-fast contract is
        preserved."""
        fm = _FakeFixtureMgr()
        v = _FakeCanaryVerifier(
            transfer_status_seq=[{"replicationStatus": "completed", "startTime": 111}],
            distribution_seq=[_canary_dist(0, self.PROVIDER)],  # never converges
        )
        now, sleep = self._clock()

        result = provision.assert_transfer_path_healthy(
            fm, v, self.PROVIDER, timeout_s=3.0, poll_interval_s=3.0,
            max_attempts=3, sleep_fn=sleep, now_fn=now,
        )

        self.assertFalse(result["healthy"])
        self.assertEqual(result["attempts"], 3)
        self.assertIn("persistent", result["reason"])
        self.assertEqual(len(fm.write_calls), 3)  # exactly max_attempts -- never unbounded

    def test_happy_path_first_attempt_no_retry_sleep(self):
        """The common case: attempt 1 converges on the FIRST poll. Must
        return fast (attempts==1) and never invoke the outer retry backoff
        sleep -- only a genuine persistent stall pays the retry cost."""
        fm = _FakeFixtureMgr()
        v = _FakeCanaryVerifier(
            transfer_status_seq=[{"replicationStatus": "scheduled", "startTime": 1}],
            distribution_seq=[_canary_dist(4096, self.PROVIDER)],
        )
        now, _unused_sleep = self._clock()
        sleep_calls = []

        def _tracked_sleep(s):
            sleep_calls.append(s)

        result = provision.assert_transfer_path_healthy(
            fm, v, self.PROVIDER, timeout_s=60.0, poll_interval_s=3.0,
            sleep_fn=_tracked_sleep, now_fn=now,
        )

        self.assertTrue(result["healthy"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(sleep_calls, [])  # converged on poll 1 -- no poll sleep, no retry backoff


class TestNoSupportingProvidersFieldRead(unittest.TestCase):
    """Static regression guard (the `.supportingProviders` null-trap): no
    code path in provision.py may READ the admin-scoped `.supportingProviders`
    field — the ONLY correct support map is the user-scoped `.providers` field
    (see wait_for_space_ready's own module-section comment). The plain
    string "supportingProviders" legitimately appears in prose comments/
    docstrings WARNING against reading it — so this checks for an actual
    field-access pattern (`.get("supportingProviders")` /
    `["supportingProviders"]` / `.supportingProviders`), not mere presence
    of the string, which would false-positive on the very comments that
    document the trap.
    """

    # The third alternative requires a word-character immediately before the
    # dot (a real Python attribute/chained-access position, e.g.
    # `body.supportingProviders`) — deliberately NOT matching the markdown
    # backtick-quoted prose form `` `.supportingProviders` `` that the
    # existing docstrings/comments legitimately use to WARN against reading
    # the field (preceded by a backtick or `{id}`, neither a word char).
    FIELD_ACCESS_RE = re.compile(
        r'\.get\(\s*["\']supportingProviders["\']\s*\)'
        r'|\[\s*["\']supportingProviders["\']\s*\]'
        r'|(?<=\w)\.supportingProviders\b'
    )

    def test_no_supportingProviders_field_access_in_code_lines(self):
        provision_path = os.path.join(os.path.dirname(os.path.abspath(provision.__file__)), "provision.py")
        with open(provision_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        offending = [
            (i + 1, line.strip()) for i, line in enumerate(lines)
            if not line.strip().startswith("#") and self.FIELD_ACCESS_RE.search(line)
        ]
        self.assertEqual(offending, [], f"found a live supportingProviders field-access: {offending!r}")


if __name__ == "__main__":
    unittest.main()
