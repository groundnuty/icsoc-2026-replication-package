#!/usr/bin/env python3
"""Regenerate every computed table body in the paper from the bundled recordings.

Deterministic: reads only ./recordings/, writes ./outputs/. No network, no model
calls, no randomness. Run with `make tables` or `python3 regenerate.py`.
"""
import glob
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from scoring.lenses import a0, a1, a3a, a3b, e3          # noqa: E402
from scoring import rq2, rq3                              # noqa: E402

REC = os.path.join(HERE, "recordings")
OUT = os.path.join(HERE, "outputs")
T = "T1"

# Populations (directory name -> what it is; see README table map)
P_HEALTHY   = "scored_sweep--rq1-control-clean-20260706"
P_FAULT     = ["scored_sweep--rq2-ss%d-gated-20260706" % i for i in (1, 2, 3)]
P_DELETION  = "e3_sweep--e3-scored-20260707"
P_GATE_OK   = "a4_sweep--a4-control-v3-20260706"
P_GATE_FLT  = ["a4_sweep--a4-fault-ss%d-20260706" % i for i in (1, 2, 3)]
P_JUDGE_FLT = "a2_fault_deletion_20260716--fault"


# Expected recordings: directory -> number of trial-*.json files. The overhead
# benchmark directory is checked separately (cell-run pairs plus fixed files).
EXPECTED_TRIALS = {
    "scored_sweep--rq1-control-clean-20260706": 56,
    "scored_sweep--rq2-ss1-gated-20260706": 24,
    "scored_sweep--rq2-ss2-gated-20260706": 16,
    "scored_sweep--rq2-ss3-gated-20260706": 16,
    "e3_sweep--e3-scored-20260707": 56,
    "a4_sweep--a4-control-v3-20260706": 56,
    "a4_sweep--a4-fault-ss1-20260706": 24,
    "a4_sweep--a4-fault-ss2-20260706": 16,
    "a4_sweep--a4-fault-ss3-20260706": 16,
    "scored_sweep--e1-informed-20260716": 56,
    "scored_sweep--e1-uninformed-paired-20260716": 56,
    "derivation_pilot_20260716--E1": 56,
    "derivation_pilot_20260716--E3": 56,
    "a2_fault_deletion_20260716--fault": 56,
    "a2_fault_deletion_20260716--deletion": 56,
    "a4_sweep--e3-a4-healthy-v2-20260715": 48,
    "a4_sweep--e3-a4-healthy-v2-glm52-20260715": 8,
}
A6_DIR = "a6_overhead--20260926"
A6_CELL_RUNS = 51
A6_FIXED = {"manifest.json", "files.json", "dryrun_manifest.json",
            "dryrun_P1-3_r0.cell.json", "dryrun_P1-3_r0.canary.json"}


def check_recordings():
    """Fail loudly unless recordings/ holds exactly the expected files."""
    problems = []
    present = {d for d in os.listdir(REC) if os.path.isdir(os.path.join(REC, d))}
    for d in sorted(present - set(EXPECTED_TRIALS) - {A6_DIR}):
        problems.append("unexpected directory: %s" % d)
    for d, n in sorted(EXPECTED_TRIALS.items()):
        if d not in present:
            problems.append("missing directory: %s" % d)
            continue
        names = os.listdir(os.path.join(REC, d))
        trials = [f for f in names if f.startswith("trial-") and f.endswith(".json")]
        if len(trials) != n:
            problems.append("%s: %d trial files, expected %d" % (d, len(trials), n))
        if len(names) != len(trials):
            problems.append("%s: unexpected files %s" % (d, sorted(set(names) - set(trials))))
    if A6_DIR not in present:
        problems.append("missing directory: %s" % A6_DIR)
    else:
        names = set(os.listdir(os.path.join(REC, A6_DIR)))
        cells = {f for f in names if f.endswith(".cell.json") and not f.startswith("dryrun_")}
        canaries = {f for f in names if f.endswith(".canary.json") and not f.startswith("dryrun_")}
        if len(cells) != A6_CELL_RUNS or {c[:-len(".cell.json")] for c in cells} != \
                {c[:-len(".canary.json")] for c in canaries}:
            problems.append("%s: %d cell / %d client files, expected %d matched pairs"
                            % (A6_DIR, len(cells), len(canaries), A6_CELL_RUNS))
        if not A6_FIXED <= names:
            problems.append("%s: missing %s" % (A6_DIR, sorted(A6_FIXED - names)))
        extra = names - cells - canaries - A6_FIXED
        if extra:
            problems.append("%s: unexpected files %s" % (A6_DIR, sorted(extra)))
    if problems:
        sys.exit("recordings check FAILED:\n  " + "\n  ".join(problems))
    total = sum(EXPECTED_TRIALS.values()) + 2 * A6_CELL_RUNS + len(A6_FIXED)
    print("recordings check: %d files in %d directories, as expected" % (total, len(EXPECTED_TRIALS) + 1))


def load(*dirs):
    out = []
    for d in dirs:
        out += [json.load(open(f)) for f in sorted(glob.glob(os.path.join(REC, d, "trial-*.json")))]
    return out


def leg(r):
    return r["model_leg"].split(":")[-1].split("/")[-1]


def pct(k, n):
    return round(100.0 * k / n) if n else 0


def _g(d):
    return d.get("verdict_per_term", d) if isinstance(d, dict) else {}


# ---- arms (placement family) -------------------------------------------------
A0 = lambda r: a0.verdict(r).get(T)                                    # noqa: E731
A1 = lambda r: _g(a1.verdict(r, vacuous_pass=True)).get(T)             # noqa: E731
A3A = lambda r: _g(a3a.verdict(r)).get(T)                              # noqa: E731
TRUTH = lambda r: a3b.verdict(r)["verdict_per_term"].get(T)            # noqa: E731
# deletion family has its own entry points
D_A0 = lambda r: _g(e3.a0_verdict(r)).get(T)                           # noqa: E731
D_A1 = lambda r: _g(e3.a1_verdict(r, vacuous_pass=True)).get(T)        # noqa: E731
D_A3A = lambda r: _g(e3.a3a_verdict(r)).get(T)                         # noqa: E731
D_TRUTH = lambda r: e3.a3b_verdict(r)["verdict_per_term"].get(T)       # noqa: E731


def judge_fault_verdicts():
    """The transcript-judge pass over the fault population is stored beside the
    recordings (the fault trials themselves carry no stamped judge verdict)."""
    m = {}
    for f in glob.glob(os.path.join(REC, P_JUDGE_FLT, "*.json")):
        s = json.load(open(f))
        m[s["trial_id"]] = (s["a2_result"].get("verdict_per_term") or {}).get(T)
    return m


def A2_of(r, fault_map):
    v = ((r.get("a2_result") or {}).get("verdict_per_term") or {}).get(T)
    return v if v is not None else fault_map.get(r["trial_id"])


# ---- read-back classification (tab2) ----------------------------------------
def _j(x):
    try:
        return json.loads(x) if isinstance(x, str) else x
    except Exception:
        return None


def _target_bytes(res, tgt):
    d = _j(res)
    if not isinstance(d, dict):
        return None
    e = (d.get("distributionPerProvider") or {}).get(tgt) or {}
    back = e.get("distributionPerStorageBackend") or {}
    return sum(b.get("physicalSize", 0) for b in back.values() if isinstance(b, dict))


def readback_class(r, expected_size, tgt):
    tc = r["agent"].get("tool_calls") or []
    dist = [t.get("result") for t in tc if t.get("name") == "get_file_distribution" and t.get("ok")]
    xfer = [t.get("result") for t in tc if t.get("name") == "get_transfer" and t.get("ok")]
    lst = [t for t in tc if t.get("name") == "list_space_transfers" and t.get("ok")]
    if not dist and not xfer and not lst:
        return "no read-back"
    if any(_target_bytes(x, tgt) == expected_size for x in dist):
        return "late-but-real"
    if any((_j(x) or {}).get("replicationStatus") == "completed" for x in xfer):
        return "job status"
    if any((_j(x) or {}).get("type") == "DIR" for x in dist):
        return "wrong signal"
    if any(_target_bytes(x, tgt) == 0 for x in dist) or lst:
        return "read then ignored"
    return "unclassified"


def convergence_time(r):
    """First moment the verifier observed the target holding the full byte count."""
    ex = r["expected"]["per_term"][T]
    tgt = (ex.get("target_providers") or [None])[0]
    exp = ex.get("expected_size")
    s = [x["t_rel_s"] for x in (r["state_timeline"].get("samples") or [])
         if x.get("sli_name") == a3b.PHYSICAL_SIZE_SLI and x.get("provider") == tgt
         and x.get("probe_ok") and x.get("value") == exp]
    return min(s) if s else None


def usd(recs, model):
    return round(sum((r.get("cost", {}).get("model", {}) or {}).get("usd", 0) or 0
                     for r in recs if model in r["model_leg"]), 4)


def write(name, text):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, name), "w") as f:
        f.write(text)
    print("  wrote outputs/%s" % name)


P_GATE_DEL = ["a4_sweep--e3-a4-healthy-v2-20260715", "a4_sweep--e3-a4-healthy-v2-glm52-20260715"]
P_JUDGE_DEL = "a2_fault_deletion_20260716--deletion"
SHORT = {"Qwen3-Coder-30B-A3B-Instruct": "Qwen3-Coder-30B", "Llama-3.3-70B-Instruct": "Llama-3.3-70B",
         "claude-haiku-4-5-20251001": "claude-haiku-4-5"}


def judge_side_verdicts(directory):
    """Transcript-judge verdicts stored beside a population, keyed by trial id."""
    m = {}
    for f in glob.glob(os.path.join(REC, directory, "*.json")):
        with open(f) as fh:
            s = json.load(fh)
        m[s["trial_id"]] = (s["a2_result"].get("verdict_per_term") or {}).get(T)
    return m


def frac(k, n):
    """Cell text as printed: 'k/n (p%)', or 'k/n' when k is 0."""
    return "%d/%d" % (k, n) if k == 0 else r"%d/%d (%d\%%)" % (k, n, pct(k, n))


def setup_valid(r):
    """A trial counts only if its setup established the pre-agent state it requires
    (for deletion: a converged two-site replica). Recorded per trial."""
    return not (r["expected"]["per_term"].get(T, {}) or {}).get("setup_invalid", False)


def main():
    check_recordings()
    H = load(P_HEALTHY)
    F = load(*P_FAULT)
    D = load(P_DELETION)
    G = load(P_GATE_OK)
    GF = load(*P_GATE_FLT)
    GD = load(*P_GATE_DEL)
    fault_judge = judge_fault_verdicts()
    del_judge = judge_side_verdicts(P_JUDGE_DEL)
    print("populations: healthy %d | fault %d | deletion %d | gated %d | gated-fault %d | gated-deletion %d"
          % (len(H), len(F), len(D), len(G), len(GF), len(GD)))

    # ---------------- read-back classes ----------------
    cc = Counter()
    for r in H:
        if A0(r) == "Fulfilled" and TRUTH(r) == "Violated":
            ex = r["expected"]["per_term"][T]
            cc[readback_class(r, ex.get("expected_size"), (ex.get("target_providers") or [None])[0])] += 1
    write("tab2-readback-classes-body.tex", r"""\begingroup\setlength{\tabcolsep}{4pt}
\begin{tabular}{@{}p{3.4cm}c p{6.0cm}@{}}
\hline
\textbf{Read-back class} & \textbf{$n$ (of %d)} & \textbf{What the recorded read showed} \\
\hline
Read, then ignored & %d & Nothing at the target; reported done anyway. \\[1pt]
Job status, not data & %d & Took the transfer job's successful submission as arrival; the target itself still held zero bytes. \\[1pt]
Late-but-real convergence & %d & Placement genuinely converged, but only past the 30-second deadline. \\[1pt]
Wrong signal (directory aggregate) & %d & Read a nonzero parent-directory total, mistaking it for the target file. \\[1pt]
No read-back & %d & No read-back issued before reporting done. \\
\hline
\end{tabular}
\endgroup
""" % (sum(cc.values()), cc["read then ignored"], cc["job status"], cc["late-but-real"],
       cc["wrong signal"], cc["no read-back"]))

    # ---------------- placement misjudgment ----------------
    A2 = lambda r: A2_of(r, fault_judge)                                # noqa: E731

    def fp_fa(recs, arm, truth):
        fp = sum(1 for r in recs if arm(r) == "Fulfilled" and truth(r) == "Violated")
        fa = sum(1 for r in recs if arm(r) in ("Violated", "NotDetermined") and truth(r) == "Fulfilled")
        return fp, fa
    rows = []
    for label, arm in [("self-report", A0), ("call/health monitoring", A1), ("transcript judge", A2),
                       ("state at report time", A3A), ("state until deadline (reference)", TRUTH)]:
        hfp, hfa = fp_fa(H, arm, TRUTH)
        ffp, _ = fp_fa(F, arm, TRUTH)
        rows.append("%s & %s & %s & %s" % (label, frac(hfp, len(H)), frac(hfa, len(H)), frac(ffp, len(F))))
    write("tab3-placement-misjudgment-body.tex", r"""\begingroup\setlength{\tabcolsep}{5pt}
\begin{tabular}{@{}p{4.4cm}ccc@{}}
\hline
\textbf{Completion policy} & \textbf{Healthy FP} & \textbf{Healthy FA} & \textbf{Fault FP} \\
\hline
""" + " \\\\[1pt]\n".join(rows) + r""" \\
\hline
\end{tabular}
\endgroup
""")

    # ---------------- deletion misjudgment ----------------
    D_A2 = lambda r: del_judge.get(r["trial_id"])                       # noqa: E731
    dv = sum(1 for r in D if D_TRUTH(r) == "Violated")
    rows = []
    for label, arm in [("self-report", D_A0), ("call/health monitoring", D_A1), ("transcript judge", D_A2),
                       ("state at report time", D_A3A), ("state until deadline (reference)", D_TRUTH)]:
        fp = sum(1 for r in D if arm(r) == "Fulfilled" and D_TRUTH(r) == "Violated")
        rows.append("%s & %s & %s" % (label, frac(fp, len(D)), frac(fp, dv)))
    write("tab5-deletion-misjudgment-body.tex", r"""\begingroup\setlength{\tabcolsep}{6pt}
\begin{tabular}{@{}p{5.2cm}cc@{}}
\hline
\textbf{Completion policy} & \textbf{FP incidence} & \textbf{FP among Violated} \\
\hline
""" + " \\\\[1pt]\n".join(rows) + r""" \\
\hline
\end{tabular}
\endgroup
""")

    # ---------------- attribution ----------------
    m = rq2.confusion_matrix(F)["matrix"]            # rows: truth; columns: emitted attribution
    n_all = sum(sum(row.values()) for row in m.values())
    truthable = sum(sum(m[t].values()) for t in ("agent", "service", "both"))
    observable = sum(m["service"].values())
    shadowed = sum(m["both"].values())
    agree = m["service"]["service"]
    write("tab6-attribution-body.tex", r"""\begingroup\setlength{\tabcolsep}{6pt}
\begin{tabular}{@{}l c@{}}
\hline
\textbf{Attribution quantity} & \textbf{Observed result} \\
\hline
ground-truthable against injected cause & %s \\
evidence observable & %s \\
shadowed by missing valid operation & %s \\
agreement when evidence observable & %s \\
\hline
\end{tabular}
\endgroup
""" % (frac(truthable, n_all), frac(observable, truthable), frac(shadowed, truthable), frac(agree, observable)))

    # ---------------- recovery mechanism (healthy gated placement) ----------------
    per = defaultdict(lambda: [0, 0, 0])
    for r in G:
        a4 = r.get("a4_result") or {}
        if a4.get("final_verdict") != "Fulfilled" or a4.get("first_attempt_verdict") == "Fulfilled":
            continue
        k = leg(r)
        per[k][0] += 1
        rem = [s.get("remedy") for s in a4.get("remedy_trail", [])]
        if any(x in ("reinvoke_agent", "reinvoke_then_wait") for x in rem):
            per[k][1] += 1
        else:                       # recovered without re-engaging the agent: the re-check
            per[k][2] += 1
    ORDER = ["Qwen3-Coder-30B-A3B-Instruct", "Llama-3.3-70B-Instruct", "GLM-4.7-Flash",
             "GLM-5.2-FP8", "claude-sonnet-5", "claude-haiku-4-5-20251001", "gemma-4-31B"]
    body, tot = "", [0, 0, 0]
    for k in ORDER:
        v = per.get(k, [0, 0, 0])
        body += "%s & %d & %d & %d \\\\\n" % (SHORT.get(k, k), v[0], v[1], v[2])
        for i in range(3):
            tot[i] += v[i]
    write("tab7-rescue-mechanism-body.tex", r"""\begin{tabular}{@{}l c c c@{}}
\hline
\textbf{Model} & \textbf{Rescued (/8)} & \textbf{By re-invoke} & \textbf{By re-check} \\
\hline
""" + body + r"""\hline
\textbf{total} & \textbf{%d} & \textbf{%d} & \textbf{%d} \\
\hline
\end{tabular}
""" % tuple(tot))

    # ---------------- runtime-activity index and gate outcomes ----------------
    mh, mf = rq3.rq3_metrics(G), rq3.rq3_metrics(GF)
    cost = {}
    for tag, mm in (("h", mh), ("f", mf)):
        for l_, v in mm["per_leg"].items():
            cost.setdefault(l_.split(":")[-1].split("/")[-1], {})[tag] = v["cost_per_verified_success"]
    body = ""
    for k in sorted(cost, key=lambda x: (cost[x].get("h", 0), cost[x].get("f", 0))):
        body += "%s & %.2f & %.2f \\\\\n" % (SHORT.get(k, k), cost[k]["h"], cost[k]["f"])
    write("tab8-cost-body.tex", r"""\begin{tabular}{@{}p{2.6cm}>{\centering\arraybackslash}p{2.3cm}>{\centering\arraybackslash}p{2.3cm}@{}}
\hline
& \multicolumn{2}{c}{\textbf{Runtime-activity index}} \\
\textbf{Model} & \textbf{Healthy} & \textbf{Injected delay} \\
\hline
""" + body + r"""\hline
\end{tabular}
""")
    oh, of_ = mh["overall"], mf["overall"]
    rh = oh["final_fulfilled"] - oh["first_attempt_fulfilled"]
    rf = of_["final_fulfilled"] - of_["first_attempt_fulfilled"]
    write("tab9-gate-outcomes-body.tex", r"""\begin{tabular}{@{}l c c@{}}
\hline
\textbf{Gate outcome} & \textbf{Healthy (%d)} & \textbf{Fault (%d)} \\
\hline
verified completions & %d & %d \\
later-contradicted completions & %d & %d \\
unverified terminations (explicit) & %d & %d \\
trials recovered after a failed first attempt & %d (%d\%%) & %d (%d\%%) \\
runtime-activity index per verified completion (mean) & %.2f & %.2f \\
\hline
\end{tabular}
""" % (oh["n_trials"], of_["n_trials"], oh["final_fulfilled"], of_["final_fulfilled"],
       oh["post_gate_violations"], of_["post_gate_violations"], oh["terminated"], of_["terminated"],
       rh, pct(rh, oh["n_trials"]), rf, pct(rf, of_["n_trials"]),
       oh["cost_per_verified_success"], of_["cost_per_verified_success"]))

    # ---------------- deadline sensitivity ----------------
    conv = [c for c in (convergence_time(r) for r in H) if c is not None]
    fconv = [convergence_time(r) for r in F]
    Ts = (10, 20, 25, 30, 35, 40)
    late = [sum(1 for c in conv if c > t) for t in Ts]
    fault_pass = [sum(1 for c in fconv if c is not None and c <= t) for t in Ts]
    write("tab10-deadline-sensitivity-body.tex", r"""\begin{tabular}{@{}l c c c c c c@{}}
\hline
\textbf{Deadline $T$ (s)} & \textbf{10} & \textbf{20} & \textbf{25} & \textbf{30 (used)} & \textbf{35} & \textbf{40} \\
\hline
healthy late cases at $T$ & %d & %d & %d & %d & %d & %d \\[1pt]
fault passes & %d & %d & %d & %d & %d & %d \\
\hline
\end{tabular}
""" % tuple(late + fault_pass))

    # ---------------- recorded dollars, same model with and without the gate ----------------
    def in_time(recs, model):
        return sum(1 for r in recs if model in r["model_leg"] and TRUTH(r) == "Fulfilled")

    def reached(recs, model):
        return sum(1 for r in recs if model in r["model_leg"]
                   and (r.get("a4_result") or {}).get("final_verdict") == "Fulfilled")

    def n_of(recs, model):
        return sum(1 for r in recs if model in r["model_leg"])
    body = ""
    for model, name in (("haiku", "claude-haiku-4-5"), ("sonnet", "claude-sonnet-5")):
        for i, (cond, U, Gt) in enumerate((("healthy", H, G), ("fault", F, GF))):
            u_usd, g_usd = usd(U, model), usd(Gt, model)
            body += "%s & %s & %d$/$%d & %.4f & %d$/$%d & %.4f & %+.1f\\%% \\\\\n" % (
                name if i == 0 else "", cond, in_time(U, model), n_of(U, model), u_usd,
                reached(Gt, model), n_of(Gt, model), g_usd, 100.0 * (g_usd - u_usd) / u_usd)
    write("tab11-cost-dollars-body.tex", r"""\setlength{\tabcolsep}{6pt}
\begin{tabular}{@{}l l r r r r r@{}}
\hline
 & & \multicolumn{2}{c}{\textbf{Ungated}} & \multicolumn{2}{c}{\textbf{Gated}} & \\
\textbf{Model} & \textbf{Condition} & in time & USD & reached & USD & \textbf{Spend change} \\
\hline
""" + body + r"""\hline
\end{tabular}
""")

    # ---------------- study matrix: included / planned per cell ----------------
    def ip(recs):
        return "%d/%d" % (sum(1 for r in recs if setup_valid(r)), len(recs))
    write("tab-study-matrix-body.tex", r"""\begin{tabular}{@{}l l c c l@{}}
\hline
\textbf{Mode} & \textbf{Task} & \textbf{Healthy I/P} & \textbf{Fault I/P} & \textbf{Primary use} \\
\hline
ungated & placement & %s & %s & RQ1; RQ2 \\
ungated & deletion & %s & --- & RQ1; organic violations \\
gated & placement & %s & %s & RQ3 \\
gated & deletion & %s & --- & RQ3 \\
\hline
\end{tabular}
""" % (ip(H), ip(F), ip(D), ip(G), ip(GF), ip(GD)))

    print("\nDone. Regenerated table bodies are in outputs/.")


if __name__ == "__main__":
    main()
