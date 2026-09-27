"""A6: verifier overhead micro-benchmark.

Measures the cost and scaling of the verifier itself: N concurrent open
contracts x poll interval delta, placement and deletion predicates, using the
scored runs' probe code UNCHANGED (`Verifier.poll_state_timeline` with
`health_provider_ids=[target]`, `Verifier.poll_residue_timeline`). The only
addition is an observer around `urllib.request.urlopen` that records every
wire request (start, duration, status or exception class, endpoint class) and
re-raises, so `Verifier._http`'s own retry behaviour is untouched.

Subcommands:
  fixtures  create a space on cloud-pl + de, write 100 x 4096 B files, replicate
            to de, confirm convergence; writes files.json
  run       orchestrate the frozen, seeded cell order (dry run first)
  cell      one verifier process (N polling loops) for one cell-run
  canary    one canary client (1 read/s) for one cell-run
  teardown  delete the space and verify its absence

Recorded output carries endpoint classes only, never URLs or hostnames.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import resource
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from harness import fixtures, provision, settings  # noqa: E402
from harness import verifier as verifier_mod  # noqa: E402

SOURCE_HOST = settings.get("source.host")
CLOUDPL_ID = settings.get("source.provider_id")
DE_ID = settings.get("target.provider_id")
TERM_ID = "T1"
T_MAX_S = 30.0
FILE_SIZE = 4096
N_FILES = 100
WARMUP_S = 10.0
WINDOW_S = 60.0
CANARY_LEAD_S = 5.0
PAUSE_S = 15.0
READS_PER_ROUND = 2          # placement: distribution + provider_status; deletion: lookup + distribution
SEED = 20260926
ORDER = ("P10/1#1 B0/3#3 P100/5#1 D100/3#1 P100/5#3 P10/5#3 P100/1#3 P50/1#3 D10/3#1 D1/3#3 "
         "D50/3#1 B0/3#1 P50/1#1 P50/5#1 D100/3#3 P50/3#1 P100/3#1 P10/3#2 P1/1#1 D50/3#2 "
         "D10/3#3 D1/3#1 D50/3#3 P10/1#2 P10/5#2 P100/5#2 P50/5#2 P100/1#1 P1/5#1 P1/3#3 "
         "P50/3#2 P100/1#2 P10/3#1 P50/1#2 P1/3#1 P50/5#3 P1/5#3 D100/3#2 P10/5#1 P100/3#2 "
         "P50/3#3 P1/5#2 B0/3#2 D10/3#2 P10/1#3 P100/3#3 P10/3#3 D1/3#2 P1/1#2 P1/1#3 P1/3#2").split()


# --- wire observer -------------------------------------------------------------

WIRE: list = []
_real_urlopen = urllib.request.urlopen


def endpoint_class(url: str) -> str:
    if "/onezone/user/tokens/temporary" in url:
        return "token_mint"
    if "/onezone/providers/" in url:
        return "provider_status"
    if "/oneprovider/lookup-file-id/" in url:
        return "lookup"
    if "/oneprovider/data/" in url and url.endswith("/distribution"):
        return "distribution"
    return "other"


def _observed_urlopen(req, *args, **kwargs):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    rec = {"t": time.time(), "ep": endpoint_class(url), "status": None, "exc": None, "dur": None}
    try:
        resp = _real_urlopen(req, *args, **kwargs)
        rec["status"] = resp.getcode()
        return resp
    except urllib.error.HTTPError as e:
        rec["status"] = e.code
        raise
    except Exception as e:  # network-layer failure: record the class, re-raise
        rec["exc"] = type(e).__name__
        raise
    finally:
        rec["dur"] = time.time() - rec["t"]
        WIRE.append(rec)


def install_observer() -> None:
    urllib.request.urlopen = _observed_urlopen


def wire_failed(rec: dict) -> bool:
    return rec["exc"] is not None or rec["status"] != 200


def host_info() -> dict:
    cpu = [ln.split(":", 1)[1].strip() for ln in open("/proc/cpuinfo") if ln.startswith("model name")]
    return {"cpu_model": cpu[0] if cpu else None, "cores": os.cpu_count(),
            "python": platform.python_version()}


def parse_cell(tag: str) -> dict:
    head, rep = tag.split("#")
    pred, rest = head[0], head[1:]
    n, delta = rest.split("/")
    return {"pred": {"P": "placement", "D": "deletion", "B": "baseline"}[pred],
            "n": int(n), "delta": float(delta), "rep": int(rep), "tag": tag}


# --- fixtures / teardown -------------------------------------------------

def cmd_fixtures(args) -> int:
    prov = provision.Provisioner()
    name = "a6_overhead_" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    info = prov.provision(name)
    sid = info["space_id"]
    state = {"space_id": sid, "space_name": name, "files": []}
    json.dump(state, open(args.state, "w"), indent=1)          # teardown needs sid even if later steps fail
    v = verifier_mod.Verifier(source_host=SOURCE_HOST)
    ready = prov.wait_for_space_ready(sid, [CLOUDPL_ID, DE_ID], verifier=v, timeout_s=120)
    if not ready.get("ready"):
        print("FATAL: space not ready", ready.get("missing")); return 2
    fm = fixtures.FixtureManager(space_name=name, space_id=sid, source_host=SOURCE_HOST,
                                 source_provider="cloud-pl")
    rng = random.Random(SEED)
    for i in range(N_FILES):
        w = fm.write_trial_file(f"a6/f{i:03d}.bin", bytes(rng.getrandbits(8) for _ in range(FILE_SIZE)))
        state["files"].append({"i": i, "file_id": w["file_id"], "full_path": w["full_path"]})
    json.dump(state, open(args.state, "w"), indent=1)
    print(f"wrote {N_FILES} files")
    pending = list(range(N_FILES))
    for attempt in range(1, 5):                                  # initial schedule + up to 3 re-schedules
        for k in range(0, len(pending), 10):
            for i in pending[k:k + 10]:
                fm.schedule_file_replication(state["files"][i]["file_id"], DE_ID)
            time.sleep(2)
        deadline = time.time() + 120
        while pending and time.time() < deadline:
            pending = [i for i in pending
                       if v._physical_size(v.get_distribution(state["files"][i]["file_id"]), DE_ID) != FILE_SIZE]
            if pending:
                time.sleep(5)
        print(f"schedule round {attempt}: {N_FILES - len(pending)}/{N_FILES} converged at de")
        if not pending:
            break
    state["converged_at_de"] = N_FILES - len(pending)
    json.dump(state, open(args.state, "w"), indent=1)
    return 0 if not pending else 3


def cmd_teardown(args) -> int:
    state = json.load(open(args.state))
    prov = provision.Provisioner()
    res = prov.teardown_space(state["space_id"], provision.DEFAULT_PROVIDERS)
    tok = fixtures.mint_token_local()
    req = urllib.request.Request(f"{fixtures.ONEZONE_URL}/api/v3/onezone/user/spaces")
    req.add_header("X-Auth-Token", tok)
    with urllib.request.urlopen(req, timeout=30, context=fixtures.SSL_CTX) as r:
        spaces = json.loads(r.read())["spaces"]
    print("teardown:", res, "| space still listed:", state["space_id"] in spaces)
    return 0 if state["space_id"] not in spaces else 4


# --- one cell-run: verifier process -------------------------------------------

def cmd_cell(args) -> int:
    install_observer()
    cell = parse_cell(args.tag)
    state = json.load(open(args.state))
    t0 = args.t0
    v = verifier_mod.Verifier(source_host=SOURCE_HOST)
    v._get_token()                                              # one mint, before any loop
    rng = random.Random(f"{SEED}:{args.tag}")
    n = cell["n"]
    offsets = [rng.uniform(0, cell["delta"]) for _ in range(n)]
    results: list = [None] * n
    poll_until = args.warmup + args.window

    def loop(i: int) -> None:
        f = state["files"][i]
        time.sleep(max(0.0, t0 + offsets[i] - time.time()))
        if cell["pred"] == "placement":
            r = v.poll_state_timeline(f["file_id"], FILE_SIZE, DE_ID, TERM_ID, cell["delta"],
                                      poll_until, t0, T_MAX_S, health_provider_ids=[DE_ID])
            results[i] = r["samples"]
        else:
            r = v.poll_residue_timeline(f["file_id"], f["full_path"], [CLOUDPL_ID, DE_ID],
                                        cell["delta"], poll_until, t0)
            results[i] = r["residue_samples"]

    threads = [threading.Thread(target=loop, args=(i,), daemon=True) for i in range(n)]
    for th in threads:
        th.start()
    rus = {}
    for label, at in (("window_start", t0 + args.warmup), ("window_end", t0 + args.warmup + args.window)):
        time.sleep(max(0.0, at - time.time()))
        ru = resource.getrusage(resource.RUSAGE_SELF)
        rus[label] = {"t": time.time(), "utime": ru.ru_utime, "stime": ru.ru_stime,
                      "load1": os.getloadavg()[0]}
    for th in threads:
        th.join(timeout=args.window)
    out = {"kind": "cell", "cell": cell, "dry_run": args.dry_run, "t0": t0,
           "warmup_s": args.warmup, "window_s": args.window, "offsets": offsets,
           "reads_per_round": READS_PER_ROUND, "host": host_info(), "rusage": rus,
           "maxrss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
           "wire": WIRE, "samples": results}
    json.dump(out, open(args.out, "w"))
    return 0


# --- one cell-run: canary process ---------------------------------------------

def cmd_canary(args) -> int:
    install_observer()
    state = json.load(open(args.state))
    v = verifier_mod.Verifier(source_host=SOURCE_HOST)
    v._get_token()
    start, end = args.t0 - CANARY_LEAD_S, args.t0 + args.warmup + args.window
    k, failed = 0, False
    while True:
        at = start + k
        if at > end:
            break
        time.sleep(max(0.0, at - time.time()))
        before = len(WIRE)
        v.get_distribution(state["files"][0]["file_id"])
        if any(wire_failed(r) for r in WIRE[before:]):
            failed = True
            break
        k += 1
    out = {"kind": "canary", "tag": args.tag, "t0": args.t0, "aborted_on_failure": failed,
           "wire": WIRE}
    json.dump(out, open(args.out, "w"))
    return 3 if failed else 0


# --- orchestrator ---------------------------------------------------------------

def run_one(tag: str, args, dry_run: bool, warmup: float, window: float) -> dict:
    t0 = time.time() + CANARY_LEAD_S + 3.0
    safe = tag.replace("/", "-").replace("#", "_r")
    base = [sys.executable, os.path.abspath(__file__)]
    common = ["--state", args.state, "--tag", tag, "--t0", repr(t0),
              "--warmup", str(warmup), "--window", str(window)]
    cell_out = os.path.join(args.out_dir, f"{'dryrun_' if dry_run else ''}{safe}.cell.json")
    can_out = os.path.join(args.out_dir, f"{'dryrun_' if dry_run else ''}{safe}.canary.json")
    canary = subprocess.Popen(base + ["canary", "--out", can_out] + common)
    cell = subprocess.Popen(base + ["cell", "--out", cell_out] + common + (["--dry-run"] if dry_run else []))
    status = "ok"
    while cell.poll() is None:
        rc = canary.poll()
        if rc is not None and rc != 0:          # canary failed (3) or crashed: abort the cell-run now
            cell.send_signal(signal.SIGTERM)
            cell.wait()
            status = "aborted_canary_failure" if rc == 3 else f"harness_crash_canary_rc{rc}"
            break
        time.sleep(0.2)
    crc = canary.wait()
    if status == "ok" and crc != 0:
        status = "aborted_canary_failure" if crc == 3 else f"harness_crash_canary_rc{crc}"
    if status == "ok" and cell.returncode != 0:
        status = f"harness_crash_rc{cell.returncode}"
    verr = None
    if status == "ok":
        c = json.load(open(cell_out))
        w0, w1 = t0 + warmup, t0 + warmup + window
        win = [r for r in c["wire"] if w0 <= r["t"] < w1 and r["ep"] != "token_mint"]
        verr = (sum(wire_failed(r) for r in win) / len(win)) if win else 0.0
    return {"tag": tag, "dry_run": dry_run, "t0": t0, "status": status,
            "verifier_wire_error_rate": verr, "cell_file": os.path.basename(cell_out),
            "canary_file": os.path.basename(can_out)}


def cmd_run(args) -> int:
    os.makedirs(args.out_dir, exist_ok=True)
    manifest_path = os.path.join(args.out_dir, "manifest.json")
    manifest = {"order": ORDER, "seed": SEED, "warmup_s": WARMUP_S, "window_s": WINDOW_S,
                "pause_s": PAUSE_S, "host": host_info(), "runs": []}

    def save():
        json.dump(manifest, open(manifest_path, "w"), indent=1)

    if not args.skip_dry_run:
        r = run_one("P1/3#0", args, True, 5.0, 15.0)
        manifest["runs"].append(r); save()
        print("DRY RUN:", r)
        if args.dry_run_only or r["status"] != "ok":
            return 0 if r["status"] == "ok" else 5
        time.sleep(PAUSE_S)
    queue = list(ORDER if not args.only else args.only.split(","))
    rerun_used = set()
    while queue:
        tag = queue.pop(0)
        r = run_one(tag, args, False, WARMUP_S, WINDOW_S)
        manifest["runs"].append(r); save()
        print(f"[{len(manifest['runs'])}] {tag}: {r['status']} verr={r['verifier_wire_error_rate']}", flush=True)
        if r["status"] == "aborted_canary_failure":
            manifest["stopped"] = f"stop rule: canary wire failure in {tag}"; save(); return 6
        if r["verifier_wire_error_rate"] is not None and r["verifier_wire_error_rate"] > 0.05:
            manifest["stopped"] = f"stop rule: verifier wire error rate > 5% in {tag}"; save(); return 6
        if r["status"].startswith("harness_crash") and tag not in rerun_used:
            rerun_used.add(tag); queue.append(tag)
        if queue:
            time.sleep(PAUSE_S)
    manifest["completed"] = True; save()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("fixtures", "teardown"):
        sp = sub.add_parser(name); sp.add_argument("--state", required=True)
    for name in ("cell", "canary"):
        sp = sub.add_parser(name)
        for a in ("--state", "--tag", "--out"):
            sp.add_argument(a, required=True)
        sp.add_argument("--t0", type=float, required=True)
        sp.add_argument("--warmup", type=float, default=WARMUP_S)
        sp.add_argument("--window", type=float, default=WINDOW_S)
        sp.add_argument("--dry-run", action="store_true")
    sp = sub.add_parser("run")
    sp.add_argument("--state", required=True); sp.add_argument("--out-dir", required=True)
    sp.add_argument("--dry-run-only", action="store_true"); sp.add_argument("--skip-dry-run", action="store_true")
    sp.add_argument("--only", default="")
    args = p.parse_args(argv)
    return {"fixtures": cmd_fixtures, "teardown": cmd_teardown, "cell": cmd_cell,
            "canary": cmd_canary, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
