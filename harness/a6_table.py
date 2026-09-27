"""A6 analysis: raw cell-run JSON -> per-cell-run metrics -> the A6 table.

Deterministic, stdlib only. All metrics are computed over each cell-run's
observation window [t0 + warmup, t0 + warmup + window); token-mint requests are
excluded. Cell value = median of the repetitions (frozen analysis rule);
counts (errors) are shown summed over repetitions. Percentiles use linear
interpolation between closest ranks.

Usage: python3 a6_table.py <recordings_dir> <out_dir>
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from collections import defaultdict

EPS = ("distribution", "provider_status", "lookup")


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def failed(r):
    return r["exc"] is not None or r["status"] != 200


def cellrun_metrics(cell: dict, canary: dict) -> dict:
    c = cell["cell"]
    w0 = cell["t0"] + cell["warmup_s"]
    w1 = w0 + cell["window_s"]
    win = [r for r in cell["wire"] if w0 <= r["t"] < w1 and r["ep"] != "token_mint"]
    lat = [1000 * r["dur"] for r in win]
    per_ep = {}
    for ep in EPS:
        e = [r for r in win if r["ep"] == ep]
        if e:
            per_ep[ep] = {"reads_per_s": len(e) / cell["window_s"],
                          "lat_p50_ms": pct([1000 * r["dur"] for r in e], 50),
                          "lat_p95_ms": pct([1000 * r["dur"] for r in e], 95),
                          "failed": sum(failed(r) for r in e), "n": len(e)}
    # probe-level errors + drift, from each contract's own samples in the window
    rel0, rel1 = cell["warmup_s"], cell["warmup_s"] + cell["window_s"]
    probe_n = probe_bad = 0
    gaps = []
    for samples in cell["samples"] or []:
        if not samples:
            continue
        s_in = [s for s in samples if rel0 <= s["t_rel_s"] < rel1]
        probe_n += len(s_in)
        probe_bad += sum(1 for s in s_in if s.get("probe_ok") is not True)
        rounds = sorted({s["t_rel_s"] for s in samples})
        gaps += [b - a - c["delta"] for a, b in zip(rounds, rounds[1:]) if rel0 <= b < rel1]
    ru = cell["rusage"]
    cpu = (ru["window_end"]["utime"] + ru["window_end"]["stime"]
           - ru["window_start"]["utime"] - ru["window_start"]["stime"])
    minutes = cell["window_s"] / 60.0
    cwin = [r for r in canary["wire"] if w0 <= r["t"] < w1 and r["ep"] != "token_mint"]
    return {
        "tag": c["tag"], "pred": c["pred"], "n": c["n"], "delta": c["delta"], "rep": c["rep"],
        "reads_per_s": len(win) / cell["window_s"],
        "formula_reads_per_s": c["n"] * cell["reads_per_round"] / c["delta"],
        "lat_p50_ms": pct(lat, 50), "lat_p95_ms": pct(lat, 95),
        "wire_failed": sum(failed(r) for r in win), "wire_n": len(win),
        "probe_failed": probe_bad, "probe_n": probe_n,
        "drift_p50_s": pct(gaps, 50), "drift_p95_s": pct(gaps, 95), "drift_max_s": max(gaps) if gaps else None,
        "cpu_core_s": cpu,
        "cpu_core_s_per_contract_min": (cpu / (c["n"] * minutes)) if c["n"] else None,
        "peak_rss_mib": cell["maxrss_kb"] / 1024.0,
        "canary_p50_ms": pct([1000 * r["dur"] for r in cwin], 50),
        "canary_p95_ms": pct([1000 * r["dur"] for r in cwin], 95),
        "canary_failed": sum(failed(r) for r in cwin), "canary_n": len(cwin),
        "load1_start": ru["window_start"]["load1"], "load1_end": ru["window_end"]["load1"],
        "per_endpoint": per_ep,
    }


def load_runs(rec_dir: str) -> list:
    man = json.load(open(os.path.join(rec_dir, "manifest.json")))
    out = []
    for r in man["runs"]:
        if r["dry_run"] or r["status"] != "ok":
            continue
        cell = json.load(open(os.path.join(rec_dir, r["cell_file"])))
        canary = json.load(open(os.path.join(rec_dir, r["canary_file"])))
        out.append(cellrun_metrics(cell, canary))
    return out


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def f(x, fmt):
    return "–" if x is None else format(x, fmt)


def table(runs: list) -> str:
    by = defaultdict(list)
    for m in runs:
        by[(m["pred"], m["n"], m["delta"])].append(m)
    base = [m for m in runs if m["pred"] == "baseline"]
    b50, b95 = med([m["canary_p50_ms"] for m in base]), med([m["canary_p95_ms"] for m in base])
    order = {"baseline": 0, "placement": 1, "deletion": 2}
    lines = ["| predicate | N | δ (s) | reads/s (formula) | latency p50/p95 ms | probe errors k/n "
             "| drift p50/p95 s | CPU core-s/contract-min | peak RSS MiB | reference client p50/p95 ms (× baseline) | reps |",
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for key in sorted(by, key=lambda k: (order[k[0]], k[1], k[2])):
        ms = by[key]
        p50, p95 = med([m["canary_p50_ms"] for m in ms]), med([m["canary_p95_ms"] for m in ms])
        ratio = (f"{p50 / b50:.2f}× / {p95 / b95:.2f}×" if (b50 and b95 and p50 and p95) else "–")
        lines.append(
            f"| {key[0]} | {key[1]} | {key[2]:g} "
            f"| {f(med([m['reads_per_s'] for m in ms]), '.1f')} ({f(ms[0]['formula_reads_per_s'], '.1f')}) "
            f"| {f(med([m['lat_p50_ms'] for m in ms]), '.0f')} / {f(med([m['lat_p95_ms'] for m in ms]), '.0f')} "
            f"| {sum(m['probe_failed'] for m in ms)}/{sum(m['probe_n'] for m in ms)} "
            f"| {f(med([m['drift_p50_s'] for m in ms]), '.2f')} / {f(med([m['drift_p95_s'] for m in ms]), '.2f')} "
            f"| {f(med([m['cpu_core_s_per_contract_min'] for m in ms]), '.3f')} "
            f"| {f(med([m['peak_rss_mib'] for m in ms]), '.0f')} "
            f"| {f(p50, '.0f')} / {f(p95, '.0f')} ({ratio}) | {len(ms)} |")
    return "\n".join(lines) + "\n"


def main(argv) -> int:
    rec_dir, out_dir = argv[1], argv[2]
    os.makedirs(out_dir, exist_ok=True)
    runs = load_runs(rec_dir)
    with open(os.path.join(out_dir, "a6-cellruns.json"), "w") as fh:
        json.dump(sorted(runs, key=lambda m: m["tag"]), fh, indent=1, sort_keys=True)
    with open(os.path.join(out_dir, "a6-overhead-table.md"), "w") as fh:
        fh.write(table(runs))
    print(f"{len(runs)} cell-runs -> {out_dir}/a6-overhead-table.md, a6-cellruns.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
