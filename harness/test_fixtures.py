"""Unit tests for FixtureManager — the resolve_root post-provision ENOENT
settle-retry (2026-07-06 third eventual-consistency surface).

FixtureManager.__init__ does no network I/O (lazy token mint), so these tests
construct one and replace ._http with a fake to drive resolve_root deterministically.
"""
import unittest

from harness import fixtures


def _enoent_body():
    return {"error": {"id": "posix", "details": {"errno": "enoent"},
                      "description": "Operation failed with POSIX error: enoent."}}


class ResolveRootSettleRetryTest(unittest.TestCase):
    def _fm(self):
        fm = fixtures.FixtureManager(
            space_name="spaceX", space_id="sid",
            source_host="https://src.example", source_provider="cloud-pl",
        )
        # Pre-seed a token so _get_token() never attempts a live mint.
        fm._token = "fake-token"
        fm._token_minted_at = 1.0e12
        return fm

    def test_transient_enoent_retried_then_succeeds(self):
        fm = self._fm()
        seq = [(400, _enoent_body()), (400, _enoent_body()), (200, {"fileId": "ROOT"})]
        calls = []

        def fake_http(method, url, **kw):
            calls.append((method, url))
            return seq[len(calls) - 1]

        fm._http = fake_http
        slept = []
        rid = fm.resolve_root(_sleep=lambda s: slept.append(s))
        self.assertEqual(rid, "ROOT")
        self.assertEqual(len(calls), 3)          # 2 transient + 1 success
        self.assertEqual(slept, [fixtures.ROOT_RESOLVE_SETTLE_INTERVAL_S] * 2)

    def test_success_first_try_never_sleeps(self):
        fm = self._fm()
        fm._http = lambda m, u, **k: (200, {"fileId": "R0"})
        slept = []
        self.assertEqual(fm.resolve_root(_sleep=lambda s: slept.append(s)), "R0")
        self.assertEqual(slept, [])              # no retry on the happy path

    def test_non_enoent_400_fails_immediately_no_retry(self):
        fm = self._fm()
        calls = []

        def fake_http(method, url, **kw):
            calls.append(1)
            return (400, {"error": {"id": "posix", "details": {"errno": "eacces"}}})

        fm._http = fake_http
        with self.assertRaises(fixtures.FixtureError):
            fm.resolve_root(_sleep=lambda s: None)
        self.assertEqual(len(calls), 1)          # a different error is NOT the transient

    def test_persistent_enoent_is_bounded_then_raises(self):
        fm = self._fm()
        calls = []

        def fake_http(method, url, **kw):
            calls.append(1)
            return (400, _enoent_body())

        fm._http = fake_http
        with self.assertRaises(fixtures.FixtureError) as ctx:
            fm.resolve_root(_sleep=lambda s: None)
        # first try + MAX_RETRIES retries, then give up — never unbounded.
        self.assertEqual(len(calls), fixtures.ROOT_RESOLVE_SETTLE_MAX_RETRIES + 1)
        self.assertIn("persisted past", str(ctx.exception))

    def test_non_200_non_400_fails_immediately(self):
        fm = self._fm()
        calls = []

        def fake_http(method, url, **kw):
            calls.append(1)
            return (500, {"error": "internal"})

        fm._http = fake_http
        with self.assertRaises(fixtures.FixtureError):
            fm.resolve_root(_sleep=lambda s: None)
        self.assertEqual(len(calls), 1)

    def test_cached_root_short_circuits(self):
        fm = self._fm()
        fm._root_id = "CACHED"
        called = []
        fm._http = lambda m, u, **k: called.append(1) or (200, {"fileId": "X"})
        self.assertEqual(fm.resolve_root(), "CACHED")
        self.assertEqual(called, [])


class IsPosixEnoentTest(unittest.TestCase):
    def test_matches_400_enoent(self):
        self.assertTrue(fixtures._is_posix_enoent(400, _enoent_body()))

    def test_rejects_non_400(self):
        self.assertFalse(fixtures._is_posix_enoent(404, _enoent_body()))
        self.assertFalse(fixtures._is_posix_enoent(200, {"fileId": "x"}))

    def test_rejects_400_without_enoent(self):
        self.assertFalse(fixtures._is_posix_enoent(400, {"error": {"errno": "eacces"}}))

    def test_survives_non_dict_body(self):
        self.assertFalse(fixtures._is_posix_enoent(400, b"raw bytes"))
        self.assertTrue(fixtures._is_posix_enoent(400, "posix error: enoent"))


if __name__ == "__main__":
    unittest.main()
