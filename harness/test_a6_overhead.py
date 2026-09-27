"""A6 overhead driver: the offline parts — endpoint classing, cell
parsing, the frozen order, and that the wire observer records failures and
re-raises them without swallowing."""
import io
import os
import sys
import unittest
import urllib.error
import urllib.request

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from harness import a6_overhead as a6
else:
    from . import a6_overhead as a6


class TestA6Offline(unittest.TestCase):
    def test_endpoint_class(self):
        self.assertEqual(a6.endpoint_class("https://h.example/api/v3/oneprovider/data/abc/distribution"), "distribution")
        self.assertEqual(a6.endpoint_class("https://h.example/api/v3/onezone/providers/xyz"), "provider_status")
        self.assertEqual(a6.endpoint_class("https://h.example/api/v3/oneprovider/lookup-file-id/%2Fs%2Ff"), "lookup")
        self.assertEqual(a6.endpoint_class("https://h.example/api/v3/onezone/user/tokens/temporary"), "token_mint")
        self.assertEqual(a6.endpoint_class("https://h.example/api/v3/oneprovider/configuration"), "other")

    def test_parse_cell(self):
        self.assertEqual(a6.parse_cell("P100/1#3"),
                         {"pred": "placement", "n": 100, "delta": 1.0, "rep": 3, "tag": "P100/1#3"})
        self.assertEqual(a6.parse_cell("B0/3#1")["pred"], "baseline")
        self.assertEqual(a6.parse_cell("D50/3#2")["n"], 50)

    def test_frozen_order_is_complete_grid(self):
        self.assertEqual(len(a6.ORDER), 51)
        self.assertEqual(len(set(a6.ORDER)), 51)
        cells = {t.split("#")[0] for t in a6.ORDER}
        expected = {f"P{n}/{d}" for n in (1, 10, 50, 100) for d in (1, 3, 5)} \
            | {f"D{n}/3" for n in (1, 10, 50, 100)} | {"B0/3"}
        self.assertEqual(cells, expected)

    def test_observer_records_and_reraises(self):
        a6.WIRE.clear()
        real = a6._real_urlopen

        def boom(req, *a, **k):
            raise urllib.error.HTTPError(req.full_url, 503, "x", {}, io.BytesIO(b""))

        a6._real_urlopen = boom
        try:
            req = urllib.request.Request("https://h.example/api/v3/oneprovider/data/f/distribution")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                a6._observed_urlopen(req)
            cm.exception.close()
        finally:
            a6._real_urlopen = real
        self.assertEqual(len(a6.WIRE), 1)
        rec = a6.WIRE[0]
        self.assertEqual((rec["ep"], rec["status"]), ("distribution", 503))
        self.assertTrue(a6.wire_failed(rec))
        self.assertNotIn("url", rec)
        a6.WIRE.clear()



class TestA6Table(unittest.TestCase):
    def test_pct_linear(self):
        from harness import a6_table as t
        self.assertEqual(t.pct([1, 2, 3, 4], 50), 2.5)
        self.assertEqual(t.pct([10], 95), 10)
        self.assertIsNone(t.pct([], 50))

    def test_cellrun_metrics_window_and_rates(self):
        from harness import a6_table as t
        t0 = 1000.0
        ok = lambda ts, ep: {"t": ts, "ep": ep, "status": 200, "exc": None, "dur": 0.1}
        wire = [ok(t0 + 5, "token_mint")]                       # excluded
        wire += [ok(t0 + 10 + i, "distribution") for i in range(60)]  # 1/s in window
        wire += [ok(t0 + 10 + i, "provider_status") for i in range(60)]
        wire += [ok(t0 + 75, "distribution")]                   # after window: excluded
        samples = [[{"t_rel_s": 10.0 + 3.1 * k, "probe_ok": True} for k in range(20)]]
        cell = {"cell": {"tag": "P1/3#1", "pred": "placement", "n": 1, "delta": 3.0, "rep": 1},
                "t0": t0, "warmup_s": 10.0, "window_s": 60.0, "reads_per_round": 2,
                "rusage": {"window_start": {"utime": 1.0, "stime": 0.0, "load1": 1},
                           "window_end": {"utime": 1.5, "stime": 0.1, "load1": 1}},
                "maxrss_kb": 51200, "wire": wire, "samples": samples}
        canary = {"wire": [ok(t0 + 10 + i, "distribution") for i in range(60)]}
        m = t.cellrun_metrics(cell, canary)
        self.assertAlmostEqual(m["reads_per_s"], 2.0)
        self.assertAlmostEqual(m["formula_reads_per_s"], 2 / 3)
        self.assertEqual(m["wire_n"], 120)
        self.assertAlmostEqual(m["drift_p50_s"], 0.1, places=6)
        self.assertAlmostEqual(m["cpu_core_s_per_contract_min"], 0.6)
        self.assertEqual(m["peak_rss_mib"], 50.0)
        self.assertEqual(m["canary_n"], 60)
        self.assertEqual(m["per_endpoint"]["provider_status"]["n"], 60)


if __name__ == "__main__":
    unittest.main()
