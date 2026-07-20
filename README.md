# Replication package

This package contains the per-trial recordings behind the paper, the scoring code that
turns them into results, and a single command that regenerates every computed table in
the paper from those recordings. The study measures how different verification policies
judge whether an autonomous agent actually fulfilled a data-management guarantee, so the
recordings carry both what each agent did and claimed, and what an independent verifier
observed at the same time.

## Package map

| Path | What it is |
|---|---|
| `regenerate.py` | regenerates every computed table body from the recordings |
| `Makefile` | `make tables` runs the above |
| `recordings/` | one JSON file per trial, grouped by population |
| `RECORDINGS.md` | data dictionary for those files |
| `scoring/` | the verification policies and aggregation code |
| `outputs/` | regenerated table bodies (written by `regenerate.py`) |
| `prompts/` | every prompt used, verbatim |
| `config/` | model, judge, and derivation settings |
| `VERIFICATION.md` | transcript of a clean-checkout run |
| `ANONYMIZATION.md` | what was scrubbed and the audit used |

## Environment

Python 3.10 or newer. **No third-party packages are required** — the scoring and
regeneration code is standard library only, so there is no lockfile to pin and no
install step. Verified on Python 3.14.

```bash
python3 --version     # 3.10+
```

## Quick start

```bash
make tables           # or: python3 regenerate.py
```

Runtime is a few seconds. It reads only `recordings/` and writes `outputs/`. It makes no
network calls and invokes no model, so repeated runs are bit-identical.

## Which output corresponds to which table

`regenerate.py` writes one file per computed table. Compare each against the
corresponding table in the paper.

| Paper table | Regenerated file | Populations read |
|---|---|---|
| Read-back classes | `outputs/tab2-readback-classes-body.tex` | placement, healthy |
| Placement misjudgment by policy | `outputs/tab3-placement-misjudgment-body.tex` | placement healthy + injected-delay, judge verdicts |
| Deletion misjudgment by policy | `outputs/tab5-deletion-misjudgment-body.tex` | deletion |
| Attribution accuracy | `outputs/tab6-attribution-body.tex` | placement, injected delay |
| Rescue mechanism by model | `outputs/tab7-rescue-mechanism-body.tex` | gated placement, healthy |
| Effort per verified success | `outputs/tab8-cost-body.tex` | gated placement, both conditions |
| Gate outcomes | `outputs/tab9-gate-outcomes-body.tex` | gated placement, both conditions |
| Deadline sensitivity | `outputs/tab10-deadline-sensitivity-body.tex` | placement, healthy |
| Recorded dollars | `outputs/tab11-cost-dollars-body.tex` | gated + ungated, the two SDK-served models |

The task-description table is prose, not computed, so nothing regenerates it.

## What this package does and does not reproduce

**Re-scoring is deterministic and exact.** Every number above is recomputed from the
bundled recordings by the bundled code. Given the same recordings you will get the same
table bodies, byte for byte. This is the claim the package supports.

**Producing new recordings is neither deterministic nor self-contained.** Producing new
recordings would require a live multi-site storage deployment, credentials for it, and
paid API access to the models — none of which ship here, and model sampling is
nondeterministic in any case. The harness that produced these recordings is part of the
project's source repository rather than this package. Treat the recordings as the
experimental record and this package as the analysis that runs on them.

## Verification policies in `scoring/`

Five policies score the same recording, differing only in what evidence they may read.

| Policy | Evidence it may use |
|---|---|
| self-report | the agent's own final claim |
| call and health monitoring | whether the agent's operations returned successfully, plus service health |
| transcript judge | the agent's full record, judged by a language model; no independent state |
| state at report time | one verifier observation, taken when the agent reported |
| state checked until deadline | the full verifier timeline up to the deadline; this is the reference |

The last policy also produces the ground truth the others are scored against, so its own
error rate is zero by construction; the finding is how far the others diverge from it.
A sixth component, the enforcement gate, uses the reference policy live: it re-checks
after each attempt and routes a remedy by attribution.

## Prompts

`prompts/` holds every prompt verbatim: the placement and deletion task text as the
agents received it, the variant that additionally states the condition, the transcript
judge's system prompt and user template, and the goal-to-condition derivation prompt.
