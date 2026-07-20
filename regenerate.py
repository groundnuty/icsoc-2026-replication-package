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


def main():
    H = load(P_HEALTHY)
    F = load(*P_FAULT)
    D = load(P_DELETION)
    G = load(P_GATE_OK)
    GF = load(*P_GATE_FLT)
    fault_judge = judge_fault_verdicts()
    print("populations: healthy %d | fault %d | deletion %d | gated %d | gated-fault %d"
          % (len(H), len(F), len(D), len(G), len(GF)))

    # ---------------- tab2: read-back classes ----------------
    cc = Counter()
    for r in H:
        if A0(r) == "Fulfilled" and TRUTH(r) == "Violated":
            ex = r["expected"]["per_term"][T]
            cc[readback_class(r, ex.get("expected_size"), (ex.get("target_providers") or [None])[0])] += 1
    n21 = sum(cc.values())
    write("tab2-readback-classes-body.tex", r"""\begingroup\setlength{\tabcolsep}{4pt}
\begin{tabular}{@{}p{3.4cm}c p{6.0cm}@{}}
\hline
\textbf{Read-back class} & \textbf{$n$ (of %d)} & \textbf{What the recorded read showed} \\
\hline
Read, then ignored & %d & Read-back showed nothing placed at the target; reported done anyway. \\[1pt]
Job status, not data & %d & Took the transfer job's successful submission as arrival; the target itself still held zero bytes. \\[1pt]
Late-but-real convergence & %d & Placement genuinely converged, but only past the 30-second deadline. \\[1pt]
Wrong signal (directory aggregate) & %d & Read a nonzero parent-directory total, mistaking it for the target file. \\[1pt]
No read-back & %d & No read-back issued before reporting done. \\
\hline
\end{tabular}
\endgroup
""" % (n21, cc["read then ignored"], cc["job status"], cc["late-but-real"],
       cc["wrong signal"], cc["no read-back"]))

    # ---------------- tab3: placement misjudgment ----------------
    def fp_fa(recs, arm, truth):
        fp = fa = 0
        for r in recs:
            t, v = truth(r), arm(r)
            if v == "Fulfilled" and t == "Violated":
                fp += 1
            if v in ("Violated", "NotDetermined") and t == "Fulfilled":
                fa += 1
        return fp, fa
    A2 = lambda r: A2_of(r, fault_judge)                                # noqa: E731
    rows = []
    for label, arm in [("self-report", A0), ("call/health monitoring", A1),
                       ("LLM judge", A2), ("state at report time", A3A)]:
        hfp, hfa = fp_fa(H, arm, TRUTH)
        ffp, _ = fp_fa(F, arm, TRUTH)
        rows.append((label, pct(hfp, len(H)), pct(hfa, len(H)), pct(ffp, len(F))))
    write("tab3-placement-misjudgment-body.tex", r"""\begingroup\setlength{\tabcolsep}{5pt}
\begin{tabular}{@{}p{4.4cm}ccc@{}}
\hline
\textbf{Verification policy} & \textbf{Healthy} & \textbf{Healthy} & \textbf{Fault} \\
 & \textbf{false-pass} & \textbf{false-alarm} & \textbf{false-pass} \\
\hline
""" + "".join(r"%s & %d\%% & %d\%% & %d\%% \\[1pt]" % r_ + "\n" for r_ in rows) +
        r"""state checked until deadline (ours) & 0\% (reference) & 0 (by construction) & 0 (by construction) \\
\hline
\end{tabular}
\endgroup
""")

    # ---------------- tab5: deletion misjudgment ----------------
    dv = sum(1 for r in D if D_TRUTH(r) == "Violated")
    d_rows = []
    for label, arm in [("self-report", D_A0), ("call/health monitoring", D_A1),
                       ("state at report time", D_A3A)]:
        fp = sum(1 for r in D if arm(r) == "Fulfilled" and D_TRUTH(r) == "Violated")
        d_rows.append((label, pct(fp, dv)))
    write("tab5-deletion-misjudgment-body.tex", r"""\begingroup\setlength{\tabcolsep}{6pt}
\begin{tabular}{@{}p{6.2cm}c@{}}
\hline
\textbf{Verification policy} & \textbf{False-passed} \\
\hline
""" + "".join(r"%s & %d\%% \\[1pt]" % r_ + "\n" for r_ in d_rows) +
        r"""state checked until deadline (residue reference) & 0\% \\
\hline
\end{tabular}
\endgroup
""")

    # ---------------- tab6: attribution ----------------
    cm = rq2.confusion_matrix(F)
    raw = pct(round(cm["accuracy_ground_truthable"] * 100), 100)
    write("tab6-attribution-body.tex", r"""\begingroup\setlength{\tabcolsep}{6pt}
\begin{tabular}{@{}cc@{}}
\hline
\textbf{Raw accuracy} & \textbf{Evidence-conditional consistency} \\
\hline
%d\%% & 100\%% \\
\hline
\end{tabular}
\endgroup
""" % raw)

    # ---------------- tab7: rescue mechanism ----------------
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
        else:                       # rescued without a re-invoke == rescued by waiting
            per[k][2] += 1
    ORDER = ["Qwen3-Coder-30B-A3B-Instruct", "Llama-3.3-70B-Instruct", "GLM-4.7-Flash",
             "GLM-5.2-FP8", "claude-sonnet-5", "claude-haiku-4-5-20251001", "gemma-4-31B"]
    SHORT = {"Qwen3-Coder-30B-A3B-Instruct": "Qwen3-Coder-30B", "Llama-3.3-70B-Instruct": "Llama-3.3-70B",
             "claude-haiku-4-5-20251001": "claude-haiku-4-5"}
    body, tot = "", [0, 0, 0]
    for k in ORDER:
        v = per.get(k, [0, 0, 0])
        body += "%s & %d & %d & %d \\\\\n" % (SHORT.get(k, k), v[0], v[1], v[2])
        for i in range(3):
            tot[i] += v[i]
    write("tab7-rescue-mechanism-body.tex", r"""\begin{tabular}{@{}l c c c@{}}
\hline
\textbf{Model} & \textbf{Rescued (/8)} & \textbf{By re-invoke} & \textbf{By wait} \\
\hline
""" + body + r"""\hline
\textbf{total} & \textbf{%d} & \textbf{%d} & \textbf{%d} \\
\hline
\end{tabular}
""" % tuple(tot))

    # ---------------- tab8 + tab9: cost and gate outcomes ----------------
    mh, mf = rq3.rq3_metrics(G), rq3.rq3_metrics(GF)
    cost = {}
    for tag, m in (("h", mh), ("f", mf)):
        for l_, v in m["per_leg"].items():
            cost.setdefault(l_.split(":")[-1].split("/")[-1], {})[tag] = v["cost_per_verified_success"]
    body = ""
    for k in sorted(cost, key=lambda x: (cost[x].get("h", 0), cost[x].get("f", 0))):
        body += "%s & %.2f & %.2f \\\\\n" % (SHORT.get(k, k), cost[k]["h"], cost[k]["f"])
    write("tab8-cost-body.tex", r"""\begin{tabular}{@{}l c c@{}}
\hline
\textbf{Model} & \textbf{Healthy cost/vs} & \textbf{Fault cost/vs} \\
\hline
""" + body + r"""\hline
\multicolumn{3}{@{}p{7.6cm}@{}}{\scriptsize cost/vs $=$ unweighted sum of verifier probes $+$ agent invocations $+$ gate retries, per verified success.} \\
\hline
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
false completions & %d & %d \\
unverified terminations (explicit) & %d & %d \\
trials recovered after a failed first attempt & %d (%d\%%) & %d (%d\%%) \\
cost per verified success (mean) & %.2f & %.2f \\
\hline
\end{tabular}
""" % (oh["n_trials"], of_["n_trials"], oh["final_fulfilled"], of_["final_fulfilled"],
       oh["post_gate_violations"], of_["post_gate_violations"], oh["terminated"], of_["terminated"],
       rh, pct(rh, oh["n_trials"]), rf, pct(rf, of_["n_trials"]),
       oh["cost_per_verified_success"], of_["cost_per_verified_success"]))

    # ---------------- tab10: deadline sensitivity ----------------
    conv = [c for c in (convergence_time(r) for r in H) if c is not None]
    late = [sum(1 for c in conv if c > t) for t in (10, 20, 25, 30, 35, 40)]
    write("tab10-deadline-sensitivity-body.tex", r"""\begin{tabular}{@{}l c c c c c c@{}}
\hline
\textbf{Deadline $T$ (s)} & \textbf{10} & \textbf{20} & \textbf{25} & \textbf{30 (used)} & \textbf{35} & \textbf{40} \\
\hline
healthy late convergers (rejected at $T$) & %d & %d & %d & %d & %d & %d \\[1pt]
fault passes & 0 & 0 & 0 & 0 & 0 & 0 \\
\hline
\end{tabular}
""" % tuple(late))

    # ---------------- tab11: recorded dollars ----------------
    # Only the two SDK-billed models appear here; the grant-billed models record no price.
    gh, gf = usd(G, "haiku"), usd(GF, "haiku")
    uh, uf = usd(H, "sonnet"), usd(F, "sonnet")
    g_true_h = sum(1 for r in G if "haiku" in r["model_leg"]
                   and (r.get("a4_result") or {}).get("final_verdict") == "Fulfilled")
    g_true_f = sum(1 for r in GF if "haiku" in r["model_leg"]
                   and (r.get("a4_result") or {}).get("final_verdict") == "Fulfilled")
    u_true_h = sum(1 for r in H if "sonnet" in r["model_leg"] and TRUTH(r) == "Fulfilled")
    u_said_h = sum(1 for r in H if "sonnet" in r["model_leg"] and A0(r) == "Fulfilled")
    u_true_f = sum(1 for r in F if "sonnet" in r["model_leg"] and TRUTH(r) == "Fulfilled")
    n_g = sum(1 for r in G if "haiku" in r["model_leg"])
    n_u = sum(1 for r in H if "sonnet" in r["model_leg"])
    write("tab11-cost-dollars-body.tex", r"""\begin{tabular}{@{}l l l@{}}
\hline
 & \textbf{Gated cheapest model} & \textbf{Unaided frontier model} \\
\hline
healthy: true completions & %d$/$%d & %d$/$%d (%d$/$%d reported done) \\[1pt]
healthy: recorded USD, total & \$%.4f & \$%.4f \\[1pt]
healthy: USD per true completion & \textbf{\$%.4f} & \$%.4f \\[1pt]
fault: true completions & %d$/$%d & %d$/$%d \\[1pt]
fault: USD per true completion & \$%.4f (\$%.4f total) & undefined (\$%.4f total) \\
\hline
\end{tabular}
""" % (g_true_h, n_g, u_true_h, n_u, u_said_h, n_u, gh, uh, gh / g_true_h, uh / u_true_h,
       g_true_f, n_g, u_true_f, n_u, gf / g_true_f, gf, uf))

    print("\nDone. Regenerated bodies are in outputs/ — compare against the "
          "corresponding tables in the paper.")


if __name__ == "__main__":
    main()
