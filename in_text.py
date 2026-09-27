"""In-text numbers: every number the paper states in prose, regenerated from the recordings.

Writes outputs/in-text-numbers.txt, one line per number:
    section | key | value | population | level
Design constants are read from the recordings. The one value that exists only in code
(the tool allowlist size) is marked as such. Standard library only.
"""
import glob
import json
import math
import os
import re
from collections import Counter

import regenerate as g
import a6_table

LINES = []


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - h) / d, (c + h) / d)


def emit(section, key, value, population, level=""):
    LINES.append(" | ".join((section, key, str(value), population, level)))


def load_dir(d):
    out = []
    for f in sorted(glob.glob(os.path.join(g.REC, d, "*.json"))):
        with open(f) as fh:
            out.append(json.load(fh))
    return out


def conv_or_none(r):
    return g.convergence_time(r)


def main():
    H, F, D = g.load(g.P_HEALTHY), g.load(*g.P_FAULT), g.load(g.P_DELETION)
    G, GF, GD = g.load(g.P_GATE_OK), g.load(*g.P_GATE_FLT), g.load(*g.P_GATE_DEL)
    INF = g.load("scored_sweep--e1-informed-20260716")
    PAIR = g.load("scored_sweep--e1-uninformed-paired-20260716")
    fj = g.judge_fault_verdicts()
    dj = g.judge_side_verdicts(g.P_JUDGE_DEL)
    A2 = lambda r: g.A2_of(r, fj)                                          # noqa: E731
    D_A2 = lambda r: dj.get(r["trial_id"])                                 # noqa: E731
    POP_H, POP_F = "ungated placement, no fault (56)", "ungated placement, injected delay (56)"
    POP_D, POP_G = "ungated deletion (56)", "gated placement, no fault (56)"
    POP_GF, POP_GD = "gated placement, injected delay (56)", "gated deletion (56 planned)"

    # ---------------- §4 design ----------------
    legs = sorted({r["model_leg"] for r in H})
    fam = {("anthropic" if l.startswith("sdk:") else l.split(":", 1)[1].split("/")[0]) for l in legs}
    emit("§4", "models / model families", "%d / %d" % (len(legs), len(fam)), POP_H)
    per_leg = {pop: Counter(r["model_leg"] for r in recs) for pop, recs in
               ((POP_H, H), (POP_F, F), (POP_D, D), (POP_G, G), (POP_GF, GF), (POP_GD, GD))}
    emit("§4", "runs per model per cell", "/".join(sorted({str(v) for c in per_leg.values() for v in c.values()})),
         "all six 56-trial cells")
    emit("§4", "tool allowlist size (placement)", "%d (code: runner.E1_FORM_B_ALLOWED_TOOLS; not stored in recordings)"
         % _allowlist_size(), "placement task")
    rounds = [r["agent"]["raw_trace"].get("rounds_used") for r in H + F + G + GF
              if isinstance(r["agent"].get("raw_trace"), dict)]
    rounds = [x for x in rounds if isinstance(x, int)]
    emit("§4", "tool rounds per attempt: cap / max used (openai-compatible arms)",
         "%d (code: harness/panel.py ForgeOpenAILeg max_tool_rounds) / %d" % (_tool_round_cap(), max(rounds)),
         "placement, all four cells")
    emit("§4", "deadline T_max (s)", _uniq(t["t_max_s"] for r in H + F + D + G + GF for t in r["contract"]["terms"]),
         "all placement and deletion trials")
    emit("§4", "verifier poll interval (s)", _uniq(r["state_timeline"]["poll_interval_s"] for r in H + F + D + G + GF),
         "all placement and deletion trials")
    emit("§4", "injected delay (s)", _uniq(r["fault"]["netem_delay_ms"] / 1000.0 for r in F + GF), "faulted cells")
    emit("§4", "gate cycle cap / wall budget (s)", "%d / %.0f (code: harness/a4_gate.py; not stored in recordings)"
         % _gate_limits(), "gated cells")
    emit("§4", "max gate cycles used", max(r["a4_result"]["gate_cycles"] for r in G + GF + GD if r.get("a4_result")),
         "gated cells")
    elig = sum(1 for r in GD if g.setup_valid(r))
    emit("§4", "gated deletion eligible / planned", "%d/%d" % (elig, len(GD)), POP_GD,
         "eligible = setup established a converged two-site replica (expected.per_term.setup_invalid false)")
    hv = sum(1 for r in H if g.TRUTH(r) == "Violated")
    emit("§4", "healthy placement Violated / Fulfilled", "%d / %d" % (hv, len(H) - hv), POP_H, "reference")
    emit("§4", "paired control trials (informed + uninformed)", "%d + %d" % (len(INF), len(PAIR)), "same-day paired runs")
    emit("§4", "derivation pilot goals (placement / deletion)",
         "%d / %d" % (len(load_dir("derivation_pilot_20260716--E1")), len(load_dir("derivation_pilot_20260716--E3"))),
         "derivation pilot")

    # ---------------- §5.1 ----------------
    a0fp = [r for r in H if g.A0(r) == "Fulfilled" and g.TRUTH(r) == "Violated"]
    emit("§5.1", "self-report accepted violations", len(a0fp), POP_H, "trial")
    emit("§5.1", "fulfilled trials with no completion report",
         sum(1 for r in H if g.TRUTH(r) == "Fulfilled" and g.A0(r) != "Fulfilled"), POP_H, "trial")
    nd = [sum(1 for r in recs if t(r) == "NotDetermined") for recs, t in ((H, g.TRUTH), (F, g.TRUTH), (D, g.D_TRUTH))]
    emit("§5.1", "reference NotDetermined (healthy / faulted / deletion)", "%d / %d / %d" % tuple(nd),
         "ungated placement and deletion")
    no_rb = sum(1 for r in a0fp if _readback(r) == "no read-back")
    emit("§5.1", "false reports that followed a read-back", "%d of %d" % (len(a0fp) - no_rb, len(a0fp)), POP_H, "trial")
    dfp = [r for r in D if g.D_A0(r) == "Fulfilled" and g.D_TRUTH(r) == "Violated"]
    emit("§5.1", "false deletion reports that followed a post-delete read",
         "%d of %d" % (sum(1 for r in dfp if _read_after_delete(r)), len(dfp)), POP_D,
         "trial; read = a read call (get_* or list_*) after the first delete_file, whatever its result")
    emit("§5.1", "false deletion reports that followed a successful post-delete read",
         "%d of %d" % (sum(1 for r in dfp if _read_after_delete(r, ok_only=True)), len(dfp)), POP_D,
         "trial; successful read = a get_* or list_* call after the first delete_file that returned without error")
    fv = sum(1 for r in F if g.TRUTH(r) == "Violated")
    emit("§5.1", "call/health accepted violations (healthy; faulted)",
         "%d of %d; %d of %d" % (sum(1 for r in H if g.A1(r) == "Fulfilled" and g.TRUTH(r) == "Violated"), hv,
                                 sum(1 for r in F if g.A1(r) == "Fulfilled" and g.TRUTH(r) == "Violated"), fv),
         "ungated placement", "trial")
    hf = len(H) - hv
    emit("§5.1", "transcript judge accepted violations; did not accept fulfilled",
         "%d of %d; %d of %d" % (sum(1 for r in H if A2(r) == "Fulfilled" and g.TRUTH(r) == "Violated"), hv,
                                 sum(1 for r in H if A2(r) != "Fulfilled" and g.TRUTH(r) == "Fulfilled"), hf),
         POP_H, "trial")
    unp = [r for r in H if (r.get("a2_result") or {}).get("parse_error")]
    emit("§5.1", "unparseable judge outputs (on Fulfilled / Violated)",
         "%d (%d / %d)" % (len(unp), sum(1 for r in unp if g.TRUTH(r) == "Fulfilled"),
                           sum(1 for r in unp if g.TRUTH(r) == "Violated")), POP_H, "trial")
    emit("§5.1", "report-time read false passes", sum(1 for r in H if g.A3A(r) == "Fulfilled" and g.TRUTH(r) == "Violated"),
         POP_H, "trial")
    conv = [conv_or_none(r) for r in H]
    late30 = sum(1 for c in conv if c is not None and c > 30)
    emit("§5.1", "healthy violations at 30 s: later converged / never converged in horizon",
         "%d / %d of %d" % (late30, sum(1 for c in conv if c is None), hv), POP_H, "trial")
    fconv = [conv_or_none(r) for r in F]
    emit("§5.1", "faulted trajectories Violated at every T up to 40 s",
         "%d of %d" % (sum(1 for c in fconv if c is None or c > 40), len(F)), POP_F, "trial")
    grid = _deadline_grid(H, F, conv, fconv, A2)
    flips = [T for T, v in grid.items() if not (v["fp"]["self-report"] > 0 and v["fp"]["call/health"] > 0
                                                  and v["fa"]["judge"] > v["fp"]["judge"] and v["fault_pass"] == 0)]
    emit("§5.1", "headline claims that flip for T in [25, 44] s", len(flips),
         "ungated placement, both conditions",
         "claims as stated in the RQ1 results: self-report and call/health false passes exceed the "
         "reference's (0); the judge errs in the opposite direction (false alarms exceed false passes); "
         "0 fault passes. Checked at every integer T in [25, 44]" + ("; flips at T=%s" % flips if flips else ""))
    emit("§5.1", "pairwise crossing: false alarms, call/health vs report-time read",
         _runs({T: "%d vs %d" % (v["fa"]["call/health"], v["fa"]["report-time"]) for T, v in grid.items()}),
         POP_H, "pairs of policies whose false-pass or false-alarm counts change order in [25, 44] s: %s"
         % _direction_changes(H, conv, A2))
    emit("§5.1", "counts that vary within [25, 44] s",
         "; ".join("%s %s: %s" % (pol, kind, _runs({T: str(v[kind][pol]) for T, v in grid.items()}))
                   for kind in ("fp", "fa") for pol in ("self-report", "call/health", "judge", "report-time")
                   if len({v[kind][pol] for v in grid.values()}) > 1), POP_H,
         "fp = false passes, fa = false alarms, per T range")
    dv = sum(1 for r in D if g.D_TRUTH(r) == "Violated")
    emit("§5.1", "deletion Violated / Fulfilled", "%d / %d" % (dv, len(D) - dv), POP_D, "reference")
    emit("§5.1", "deletion violations accepted (self-report / call-health / report-time read)",
         "%d / %d / %d of %d" % tuple([sum(1 for r in D if a(r) == "Fulfilled" and g.D_TRUTH(r) == "Violated")
                                       for a in (g.D_A0, g.D_A1, g.D_A3A)] + [dv]), POP_D, "trial")
    jnd = [r for r in D if D_A2(r) == "NotDetermined"]
    emit("§5.1", "deletion judge NotDetermined (on Fulfilled / Violated); judge Violated",
         "%d (%d / %d); %d" % (len(jnd), sum(1 for r in jnd if g.D_TRUTH(r) == "Fulfilled"),
                               sum(1 for r in jnd if g.D_TRUTH(r) == "Violated"),
                               sum(1 for r in D if D_A2(r) == "Violated")), POP_D, "trial")
    emit("§5.1", "deletion judge false alarms on Fulfilled",
         "%d of %d" % (sum(1 for r in D if D_A2(r) != "Fulfilled" and g.D_TRUTH(r) == "Fulfilled"), len(D) - dv),
         POP_D, "trial")

    # ---------------- §5.2 ----------------
    m = g.rq2.confusion_matrix(F)["matrix"]
    truthable = sum(sum(m[t].values()) for t in ("agent", "service", "both"))
    obs, agree = sum(m["service"].values()), m["service"]["service"]
    emit("§5.2", "delay verified active", sum(1 for r in F if r["fault"].get("inject_verified")), POP_F, "trial")
    emit("§5.2", "ground-truthable (injected-cause window)", "%d of %d" % (truthable, len(F)), POP_F, "trial")
    emit("§5.2", "shadowed by missing valid operation", sum(m["both"].values()), POP_F, "trial")
    emit("§5.2", "agreement when evidence observable (Wilson 95% lower bound)",
         "%d of %d (%.1f%%)" % (agree, obs, 100 * wilson(agree, obs)[0]), POP_F, "trial")
    emit("§5.2", "raw attribution accuracy", "%d of %d (%d%%)" % (agree, truthable, g.pct(agree, truthable)), POP_F, "trial")
    emit("§5.2", "blinded adjudication", "not regenerated: the adjudication data is not included in this repository", "—")

    # ---------------- §5.3 ----------------
    gp = G + GF
    adm = sum(1 for r in gp if r["a4_result"]["final_verdict"] == "Fulfilled")
    unv = sum(1 for r in gp if r["a4_result"].get("terminated"))
    mh, mf = g.rq3.rq3_metrics(G), g.rq3.rq3_metrics(GF)
    contra = mh["overall"]["post_gate_violations"] + mf["overall"]["post_gate_violations"]
    emit("§5.3", "admitted / unverified / contradicted (Wilson 95% upper bound)",
         "%d of %d / %d / %d (%.1f%%)" % (adm, len(gp), unv, contra, 100 * wilson(contra, adm)[1]),
         "gated placement (112)", "final")
    emit("§5.3", "healthy verified", "%d/%d" % (mh["overall"]["final_fulfilled"], len(G)), POP_G, "final")
    emit("§5.3", "faulted first attempts that missed the deadline",
         "%d of %d" % (sum(1 for r in GF if r["a4_result"]["first_attempt_verdict"] != "Fulfilled"), len(GF)),
         POP_GF, "first attempt")
    rec_f = [r for r in GF if r["a4_result"]["final_verdict"] == "Fulfilled"
             and r["a4_result"]["first_attempt_verdict"] != "Fulfilled"]
    emit("§5.3", "faulted recovered / unverified", "%d / %d" % (len(rec_f), sum(1 for r in GF if r["a4_result"].get("terminated"))),
         POP_GF, "final")
    emit("§5.3", "recovered trials whose recorded first-attempt verdict is not Fulfilled",
         "%d of %d" % (sum(1 for r in rec_f if r["a4_result"]["first_attempt_verdict"] != "Fulfilled"), len(rec_f)),
         POP_GF, "first attempt")
    gde = [r for r in GD if g.setup_valid(r)]
    mgd = g.rq3.rq3_metrics(gde)["overall"]
    emit("§5.3", "gated deletion verified / contradicted (Wilson 95% upper bound)",
         "%d of %d / %d (%.1f%%)" % (mgd["final_fulfilled"], len(gde), mgd["post_gate_violations"],
                                     100 * wilson(mgd["post_gate_violations"], len(gde))[1]), POP_GD + ", eligible 50", "final")
    emit("§5.3", "final NotDetermined (placement / deletion)",
         "%d / %d" % (sum(1 for r in gp if r["a4_result"]["final_verdict"] == "NotDetermined"),
                      sum(1 for r in gde if r["a4_result"]["final_verdict"] == "NotDetermined")), "gated", "final")
    emit("§5.3", "unverified trials ending NotDetermined",
         "%d of %d" % (sum(1 for r in gp if r["a4_result"].get("terminated") and r["a4_result"]["final_verdict"] == "NotDetermined"), unv),
         "gated placement", "final")
    paths_h, bound, reinv, no_reengage = _recovery(G), {}, {}, {}
    for name, recs in (("healthy", G), ("faulted", GF)):
        fin = [r for r in recs if r["a4_result"]["final_verdict"] == "Fulfilled"]
        bound[name] = sum(1 for r in fin if not (_remedies(r) & REINV))
        reinv[name] = [r for r in recs if _remedies(r) & REINV]
        no_reengage[name] = sum(1 for r in fin if r["a4_result"]["first_attempt_verdict"] != "Fulfilled"
                                and not (_remedies(r) & REINV))
    emit("§5.3", "healthy recovered = re-invoke + re-check + service wait",
         "%d = %d + %d + %d" % (sum(paths_h[k] for k in ("re-invoke", "re-check", "service wait")),
                                paths_h["re-invoke"], paths_h["re-check"], paths_h["service wait"]), POP_G, "trial")
    for name in ("healthy", "faulted"):
        first = [_first_attempt_calls(r) for r in reinv[name]]
        assert all(sep for _, sep in first), "attempt-1 boundary not separable in a re-invoked trial"
        lack = sum(1 for (calls, _), r in zip(first, reinv[name]) if not any(_valid_target_op(c, r) for c in calls))
        tried = sum(1 for (calls, _), r in zip(first, reinv[name])
                    if any(c.get("name") == "schedule_file_replication" and
                           (c.get("args") or {}).get("target_provider_id") == _target(r) for c in calls))
        emit("§5.3 (basis of the always-wait bound)", "re-invoked trials whose first attempt had no valid target operation (%s)" % name,
             "%d of %d (%d of them sent a schedule to the target with a malformed file path, rejected by the service)"
             % (lack, len(reinv[name]), tried), POP_G if name == "healthy" else POP_GF,
             "first attempt; its calls are the recorded calls before 45 s (the cycle-1 verify window closes at 45 s, "
             "so a second attempt cannot start earlier); valid = schedule_file_replication to the target provider "
             "for the trial's file, accepted by the service")
    emit("§5.3", "always-wait bound (verified without re-invoke) vs routed",
         "%d/56 vs %d; %d/56 vs %d" % (bound["healthy"], mh["overall"]["final_fulfilled"], bound["faulted"], mf["overall"]["final_fulfilled"]),
         "gated placement", "final")
    emit("§5.3", "recovered without re-engaging the agent (healthy / faulted)",
         "%d / %d" % (no_reengage["healthy"], no_reengage["faulted"]), "gated placement", "trial")
    changes, reached = [], []
    for model in ("haiku", "sonnet"):
        for U, Gt in ((H, G), (F, GF)):
            u, gg = g.usd(U, model), g.usd(Gt, model)
            changes.append(100.0 * (gg - u) / u)
    emit("§5.3", "spend change with the gate (min–max)", "%+.1f%% to %+.1f%%" % (min(changes), max(changes)),
         "SDK-billed arms, 8-trial cells")
    for model in ("haiku", "sonnet"):
        emit("§5.3", "under the fault, %s: gated reached vs ungated in time" % model,
             "%d/8 vs %d/8" % (sum(1 for r in GF if model in r["model_leg"] and r["a4_result"]["final_verdict"] == "Fulfilled"),
                               sum(1 for r in F if model in r["model_leg"] and g.TRUTH(r) == "Fulfilled")),
             "SDK-billed arm, 8-trial cells", "final / in time")
    for name, recs in (("ungated healthy", H), ("ungated faulted", F), ("gated healthy", G), ("gated faulted", GF)):
        emit("§5.3", "mean verifier probes per trial (%s)" % name,
             "%.1f" % (sum(r["cost"]["verifier"]["probe_count"] for r in recs) / len(recs)), name, "trial")
    for key, val in _a6_numbers():
        emit("§5.3", key, val, "verifier overhead benchmark", "median of 3 repetitions")

    # ---------------- §5.4 ----------------
    for name, recs in (("uninformed", PAIR), ("informed", INF)):
        cls = Counter(_target_class(r) for r in recs)
        viol = sum(1 for r in recs if g.TRUTH(r) == "Violated")
        emit("§5.4", "wrong-target schedules / omitted transfers / violations (%s)" % name,
             "%d/56 / %d/56 / %d/56" % (cls["wrong"], cls["none"], viol), "same-day paired run (%s)" % name, "trial")
        emit("§5.4", "violations reported complete (%s)" % name,
             "%d of %d" % (sum(1 for r in recs if g.A0(r) == "Fulfilled" and g.TRUTH(r) == "Violated"), viol),
             "same-day paired run (%s)" % name, "trial")
    by_pop = _blocked_calls()
    main5 = ["rq1-control-clean-20260706", "rq2-ss1-gated-20260706", "rq2-ss2-gated-20260706",
             "rq2-ss3-gated-20260706", "e3-scored-20260707", "a4-control-v3-20260706",
             "a4-fault-ss1-20260706", "a4-fault-ss2-20260706", "a4-fault-ss3-20260706"]
    tot = lambda keys: tuple(sum(by_pop[k][i] for k in keys) for i in (0, 1))   # noqa: E731
    emit("§5.4", "admission-control blocks (all / rule-setting operation)", "%d / %d" % tot(main5),
         "anthropic-sdk arms in ungated placement (both conditions), ungated deletion, gated placement "
         "(both conditions); per directory: " + ", ".join("%s %d/%d" % (k, by_pop[k][0], by_pop[k][1]) for k in main5),
         "call")
    for fam_, d_ in (("placement", "derivation_pilot_20260716--E1"), ("deletion", "derivation_pilot_20260716--E3")):
        recs = load_dir(d_)
        ok = sum(1 for r in recs if _fully_correct(r))
        emit("§5.4", "derivation fully correct, %s (Wilson 95%% lower bound)" % fam_,
             "%d/%d (%.1f%%)" % (ok, len(recs), 100 * wilson(ok, len(recs))[0]), "derivation pilot", "goal")

    with open(os.path.join(g.OUT, "in-text-numbers.txt"), "w") as fh:
        fh.write("section | number | value | population | level\n" + "\n".join(LINES) + "\n")
    print("  wrote outputs/in-text-numbers.txt (%d numbers)" % len(LINES))


# ---------------- helpers ----------------
REINV = {"reinvoke_agent", "reinvoke_then_wait"}


def _uniq(values):
    vs = sorted(set(values))
    return vs[0] if len(vs) == 1 else "values: %s" % vs


def _allowlist_size():
    src = open(os.path.join(g.HERE, "harness", "runner.py")).read()
    block = re.search(r"E1_FORM_B_ALLOWED_TOOLS = \((.*?)\)", src, re.S).group(1)
    return len(re.findall(r"\"[^\"]+\"", block))


def _tool_round_cap():
    src = open(os.path.join(g.HERE, "harness", "panel.py")).read()
    return int(re.search(r"max_tool_rounds: int = (\d+)", src).group(1))


def _gate_limits():
    src = open(os.path.join(g.HERE, "harness", "a4_gate.py")).read()
    n = int(re.search(r"^N_MAX_DEFAULT\s*=\s*(\d+)", src, re.M).group(1))
    w = float(re.search(r"^WALL_CLOCK_BUDGET_S_DEFAULT\s*=\s*([0-9.]+)", src, re.M).group(1))
    return n, w


def _readback(r):
    ex = r["expected"]["per_term"][g.T]
    return g.readback_class(r, ex.get("expected_size"), (ex.get("target_providers") or [None])[0])


def _read_after_delete(r, ok_only=False):
    calls = r["agent"].get("tool_calls") or []
    deletes = [c.get("ts_rel_s", 0) for c in calls if c.get("name") == "delete_file"]
    if not deletes:
        return False
    first = min(deletes)
    # a read tool called after the first delete, whatever its result (a read that finds the
    # file gone returns an error but is still a read)
    return any(c.get("name", "").startswith(("get_", "list_")) and c.get("ts_rel_s", 0) > first
               and (c.get("ok") or not ok_only) for c in calls)


def _deadline_grid(H, F, conv, fconv, A2):
    pols = {"self-report": g.A0, "call/health": g.A1, "judge": A2, "report-time": g.A3A}
    grid = {}
    for T in range(25, 45):
        truth = ["Fulfilled" if (c is not None and c <= T) else "Violated" for c in conv]
        grid[T] = {
            "fp": {k: sum(1 for r, t in zip(H, truth) if a(r) == "Fulfilled" and t == "Violated") for k, a in pols.items()},
            "fa": {k: sum(1 for r, t in zip(H, truth) if a(r) != "Fulfilled" and t == "Fulfilled") for k, a in pols.items()},
            "fault_pass": sum(1 for c in fconv if c is not None and c <= T),
        }
    return grid


def _runs(by_t):
    """{T: value} -> 'T=a-b: value; ...' over runs of equal values."""
    out, ts = [], sorted(by_t)
    start = ts[0]
    for i, T in enumerate(ts):
        if i + 1 == len(ts) or by_t[ts[i + 1]] != by_t[T]:
            out.append("T=%s: %s" % (str(start) if start == T else "%d-%d" % (start, T), by_t[T]))
            if i + 1 < len(ts):
                start = ts[i + 1]
    return "; ".join(out)


def _direction_changes(H, conv, A2):
    pols = {"self-report": g.A0, "call/health": g.A1, "judge": A2, "report-time": g.A3A}
    signs = {}
    for T in range(25, 45):
        truth = ["Fulfilled" if (c is not None and c <= T) else "Violated" for c in conv]
        fp = {k: sum(1 for r, t in zip(H, truth) if a(r) == "Fulfilled" and t == "Violated") for k, a in pols.items()}
        fa = {k: sum(1 for r, t in zip(H, truth) if a(r) != "Fulfilled" and t == "Fulfilled") for k, a in pols.items()}
        fp["reference"] = fa["reference"] = 0
        for metric, vals in (("FP", fp), ("FA", fa)):
            ks = sorted(vals)
            for i in range(len(ks)):
                for j in range(i + 1, len(ks)):
                    s = (vals[ks[i]] > vals[ks[j]]) - (vals[ks[i]] < vals[ks[j]])
                    signs.setdefault((metric, ks[i], ks[j]), set()).add(s)
    changed = sorted("%s %s vs %s" % k for k, v in signs.items() if len(v - {0}) > 1)
    return len(changed) if not changed else "%d (%s)" % (len(changed), "; ".join(changed))


def _remedies(r):
    return {s.get("remedy") for s in r["a4_result"]["remedy_trail"]}


def _recovery(recs):
    c = Counter()
    for r in recs:
        a4 = r["a4_result"]
        if a4["final_verdict"] != "Fulfilled" or a4["first_attempt_verdict"] == "Fulfilled":
            continue
        rs = _remedies(r)
        c["re-invoke" if rs & REINV else "re-check" if rs == {"escalate"} else
          "service wait" if rs == {"wait_repoll"} else "re-check and wait"] += 1
    return c


def _target(r):
    return (r["expected"]["per_term"][g.T].get("target_providers") or [None])[0]


def _first_attempt_calls(r, window_s=45.0):
    """Calls of the first attempt in a gated trial (tool_calls is merged across attempts on the
    trial clock) and whether the boundary is unambiguous (a gap > 3 s before any later call)."""
    calls = sorted(r["agent"].get("tool_calls") or [], key=lambda c: c.get("ts_rel_s", 0))
    early = [c for c in calls if c.get("ts_rel_s", 0) < window_s]
    late = [c for c in calls if c.get("ts_rel_s", 0) >= window_s]
    sep = not early or not late or late[0]["ts_rel_s"] - early[-1]["ts_rel_s"] > 3.0
    return early, sep


def _valid_target_op(c, r):
    e = r["expected"]["per_term"][g.T]
    a = c.get("args") or {}
    paths = e.get("expected_paths") or []
    return (c.get("name") == "schedule_file_replication" and a.get("target_provider_id") == _target(r)
            and (not paths or a.get("file_id_or_path") in paths) and bool(c.get("ok")))


def _target_class(r):
    tgt = (r["expected"]["per_term"][g.T].get("target_providers") or [None])[0]
    for c in r["agent"].get("tool_calls") or []:
        if c.get("name") == "schedule_file_replication" and c.get("ok"):
            return "correct" if (c.get("args") or {}).get("target_provider_id") == tgt else "wrong"
    return "none"


def _blocked_calls():
    pops = ["scored_sweep--rq1-control-clean-20260706"] + g.P_FAULT + [g.P_DELETION, g.P_GATE_OK] + g.P_GATE_FLT \
        + g.P_GATE_DEL + ["scored_sweep--e1-informed-20260716", "scored_sweep--e1-uninformed-paired-20260716"]
    by = {}
    for p in pops:
        a = b = 0
        for r in g.load(p):
            if not r["model_leg"].startswith("sdk:"):
                continue
            for c in r["agent"].get("tool_calls") or []:
                if "requested permissions to use" in str(c.get("result")):
                    a += 1
                    b += c.get("name") == "add_file_qos_requirement"
        by[p.split("--")[-1]] = (a, b)
    return by


def _fully_correct(r):
    s = r.get("score")
    if isinstance(s, str):
        return "'fully_correct': True" in s
    return bool((s or {}).get("fully_correct"))


def _a6_numbers():
    runs = a6_table.load_runs(os.path.join(g.REC, "a6_overhead--20260926"))
    import statistics as st

    def med(pred, n, d, key):
        return st.median(m[key] for m in runs if m["pred"] == pred and m["n"] == n and m["delta"] == d)
    base_p50 = st.median(m["canary_p50_ms"] for m in runs if m["pred"] == "baseline")
    out = [("CPU core-s per contract-minute at %s s (N = 1 / 10 / 50 / 100)" % ("%g" % d),
            " / ".join("%.3f" % med("placement", n, d, "cpu_core_s_per_contract_min") for n in (1, 10, 50, 100)))
           for d in (3.0, 5.0, 1.0)]
    cpu100 = med("placement", 100, 3.0, "cpu_core_s_per_contract_min")
    out.append(("100 contracts at 3 s: cores used / peak RSS MiB",
                "%.2f / %.0f" % (cpu100 * 100 / 60.0, med("placement", 100, 3.0, "peak_rss_mib"))))
    out.append(("probe failures, all cells", sum(m["probe_failed"] for m in runs)))
    cells = {(m["pred"], m["n"], m["delta"]) for m in runs if m["pred"] != "baseline"}
    ratios = [med(p, n, d, "canary_p50_ms") / base_p50 for p, n, d in cells]
    out.append(("other client's median latency vs baseline (range over cells)", "%.2f to %.2f" % (min(ratios), max(ratios))))
    return out


if __name__ == "__main__":
    main()
