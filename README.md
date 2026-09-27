# Replication package: When the Consumer Is an Agent

Replication package for Michał Orzechowski, Renata G. Słota and Jacek Kitowski, *When the
Consumer Is an Agent: Execution-Grounded Service-Level Agreements*, ICSOC 2026 (Lecture
Notes in Computer Science, Springer).

This repository holds the per-trial recordings behind the paper, the code that scores
them, the harness that produced them, and one command that regenerates every table and
every number stated in the paper's text from the recordings.

## Quick start

```bash
make check    # verify the recordings, regenerate every output, compare with the committed files
```

Python 3.10 or newer (standard library only) and a POSIX shell with `make`; a few seconds. The last line reads
`check: all 13 outputs byte-identical to the committed files`. The same check and the
harness test suite also run in a container with no network; see [Container](#container).

The study measures how verification policies judge whether an autonomous agent actually
fulfilled a data-management guarantee. Each trial recording stores:
- the requested model id and its settings;
- the prompts;
- the agent's messages and its tool calls with their results;
- the timeline of what an independent verifier observed.

There are three ways to use it:

| Path | What it shows | Needs | Time |
|---|---|---|---|
| **Data only** | every table and in-text number, regenerated from the recordings | Python ≥ 3.10, standard library only | seconds |
| **Offline check** | the harness test suite, including checks that the shipped configuration and drivers reproduce the recorded arms, contracts and prompts | Python 3.12–3.14 and `requirements-harness.txt`, or the container; no network, no credentials | seconds after install |
| **New run** | a new run of the experiments on your own storage deployment and models | an Onedata deployment, model endpoints, and the harness | hours; see below |

## Repository map

| Path | Contents |
|---|---|
| `recordings/` | one JSON file per trial, grouped by population; see `RECORDINGS.md` |
| `SHA256SUMS`, `checksums.py` | checksums of every file in `recordings/` |
| `regenerate.py`, `a6_table.py`, `scoring/` | the scoring code (standard library only) |
| `outputs/` | regenerated tables and numbers |
| `harness/`, `scored_sweep_driver.py`, `a4_sweep_driver.py` | the harness and its run drivers |
| `config/` | deployment settings, model arms, and the model and judge settings sheets |
| `prompts/` | every prompt, verbatim |
| `ENVIRONMENT.md` | the storage deployment, trial parameters and host used |
| `Dockerfile` | container for the data-only path and the offline check |
| `VERIFICATION.md` | transcript of a clean-checkout run |
| `audit.sh` | release checks (credentials, personal data, hosts, binaries) |

## Data only

Python 3.10 or newer, standard library only; tested with Python 3.10, 3.12, 3.13 and 3.14.
Any machine.

```bash
make check     # the three steps below, then a comparison with the committed outputs
make verify    # every file in recordings/ against SHA256SUMS
make tables    # regenerate every output in outputs/
```

`make check` leaves `outputs/` as committed; on a mismatch it prints the diff and keeps the
regenerated files in `outputs.regenerated/`. `make tables` overwrites `outputs/`.
`make PYTHON=<interpreter> check` selects the interpreter.

`make tables` first checks that `recordings/` holds exactly the expected files per
population and stops if any file is missing or extra. It reads only `recordings/`, makes
no network calls and invokes no model, so repeated runs are byte-identical.

| Paper | Content | Output file |
|---|---|---|
| Table 3 | Study cells (included/planned) | `outputs/tab-study-matrix-body.tex` |
| Table 4 | Read-back classes of false completion reports | `outputs/tab2-readback-classes-body.tex` |
| Table 5 | Placement-policy error incidence | `outputs/tab3-placement-misjudgment-body.tex` |
| Table 6 | Deadline sensitivity | `outputs/tab10-deadline-sensitivity-body.tex` |
| Table 7 | Deletion false passes | `outputs/tab5-deletion-misjudgment-body.tex` |
| Table 8 | Attribution accounting | `outputs/tab6-attribution-body.tex` |
| Table 9 | Gate outcomes | `outputs/tab9-gate-outcomes-body.tex` |
| Table 10 | First-attempt failures recovered, by mechanism | `outputs/tab7-rescue-mechanism-body.tex` |
| Table 11 | Runtime-activity index per verified completion | `outputs/tab8-cost-body.tex` |
| Table 12 | Recorded model charges, with and without the gate | `outputs/tab11-cost-dollars-body.tex` |
| Sect. 5.3 | Verifier overhead by open contracts and poll interval | `outputs/a6-overhead-table.md` (per run: `outputs/a6-cellruns.json`) |

Tables 1 and 2 are descriptive and are not computed. Every number stated in the paper's
text is in `outputs/in-text-numbers.txt`, one line per number, with its section,
population and level (trial, cycle, first attempt or final); two further lines, marked as
the basis of the always-wait bound, give the counts that bound rests on.

The paper's replication paragraph lists the following contents:

| Content | Where |
|---|---|
| trial records | `recordings/`, described in `RECORDINGS.md` |
| endpoint and generation settings | `config/arms.json`, `config/models.md`, `config/judge.md`, `config/derivation.md`; per trial in `model_config` |
| run order | per trial in `trial_start_utc`; for the overhead benchmark in `recordings/a6_overhead--20260926/manifest.json` |
| setup eligibility | per trial in `expected.per_term.<term>.setup_invalid` (see `RECORDINGS.md`) |
| reset procedures | `harness/provision.py` (`create_space`, `teardown_space`: a fresh space per run) and `harness/fixtures.py` (`write_trial_file`, `teardown_trial`: per-trial file setup and removal) |
| verdict and attribution code | `scoring/` |
| table-generation scripts | `regenerate.py`, `a6_table.py`, `in_text.py` |

## Offline check

Tested with Python 3.12, 3.13 and 3.14.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-harness.txt
make test PYTHON=.venv/bin/python
```

The suite runs with no network access and no credentials. Besides unit tests of the
harness, it checks two things against the recordings:
- **Arms:** the model arms built from `config/arms.json` have the recorded leg name,
  model settings and adapter class for every one of the 448 recorded trials.
- **Switches:** the driver switches rebuild the recorded contract and prompt, byte for
  byte, for all 168 deletion, informed and paired trials.

## Container

```bash
docker build -t consumer-agent-sla-artifact .
docker run --rm --network none consumer-agent-sla-artifact
```

The image is `python:3.14-slim`, pinned by digest, with `requirements-harness.txt`
installed. With no network it runs `make check` and `make test`; the output ends with
`check: all 13 outputs byte-identical to the committed files` and `OK` after 442 tests.

## New run on your own deployment

Running the experiments again is a new experiment, not a reproduction of the recorded one: models are sampled,
and a shared storage deployment has its own timing.

**What the deployment must provide**
- **Storage:** an Onedata 25 deployment (versions in `ENVIRONMENT.md`) with a Onezone and
  two Oneprovider sites, a source and a target, each with POSIX storage and reachable over
  HTTPS. The harness creates a fresh space for each run, supports it on both sites, writes
  its own trial files, and removes the space at the end.
- **Onezone admin credential:** needed to mint short-lived access tokens and to create and
  remove spaces. Its password is read from the environment variable named by
  `onezone_admin_password_env` (default `ONEZONE_ADMIN_PASSWORD`).
- **Agent tools:** the onedata-mcp tool server at the commit given in `ENVIRONMENT.md`,
  installed locally. Point `onedata_mcp_bin` at its executable.
- **Injected delay (faulted runs only):**
  - SSH access to a host with `kubectl` access to the target site's cluster;
  - permission to attach a debug container with `NET_ADMIN` to the target provider pod;
  - the pod's coordinates, set in the `fault` settings.

**Deployment settings** live in `config/federation.json`. Any key can be overridden by an
environment variable `HARNESS_<KEY>`, with dots written as underscores, e.g.
`HARNESS_SOURCE_HOST`.

| Key | Meaning |
|---|---|
| `onezone_url` | Onezone base URL |
| `onezone_admin_user`, `onezone_admin_password_env` | admin user, and the variable that holds its password |
| `source.*`, `target.*` | per site: `label`, `provider_id`, `host` (Oneprovider URL), `storage_id` |
| `onedata_mcp_bin` | path to the onedata-mcp executable |
| `recordings_dir` | where new recordings are written (default `runs/`) |
| `fault.*` | `ssh_host`, `kubeconfig` (path on that host), `namespace`, `pod`, `container`, `iface` |

**Model arms** live in `config/arms.json`, one entry per arm:
- `name`;
- `kind`: `openai-compatible`, or `anthropic-sdk` for the Claude Agent SDK;
- `model_id`;
- `base_url`;
- `key_env`: the name of the variable holding the API key;
- optionally `api_model_id`: the identifier sent to the endpoint when it differs from
  `model_id` (for example an endpoint-side alias); `model_id` stays the recorded name.

No key values are stored anywhere. `anthropic-sdk` arms authenticate through a logged-in
Claude CLI, with the API-key variables unset. Hosted models can be renamed or withdrawn by
their providers; set the model ids you can reach here.

**Commands.** Both drivers read `RUN_ID`, `SWEEP_K` (repetitions, default 8), `SWEEP_LEGS`
(comma-separated model ids, default all arms) and `SWEEP_FAULT` (`none` or `netem_lag`).

| Experiment | Command |
|---|---|
| Placement, no fault | `RUN_ID=placement SWEEP_FAULT=none .venv/bin/python scored_sweep_driver.py` |
| Placement, injected delay | `RUN_ID=placement-delay SWEEP_FAULT=netem_lag SWEEP_LEGS=<up to 3 ids> .venv/bin/python scored_sweep_driver.py` |
| Gated placement (either condition) | the same variables with `a4_sweep_driver.py` |
| Deletion | `RUN_ID=deletion SWEEP_SCENARIO=deletion .venv/bin/python scored_sweep_driver.py` |
| Informed-prompt placement | `RUN_ID=informed SWEEP_INFORMED=1 .venv/bin/python scored_sweep_driver.py` |
| Verifier overhead | `.venv/bin/python harness/a6_overhead.py fixtures --state s.json`, then `run --state s.json --out-dir <dir>`, then `teardown --state s.json` |

Gated deletion has no driver in this repository. The gate functions it uses are included
and tested (`make_live_e3_verify_fn` and `build_e3_feedback` in `harness/a4_gate.py`,
`run_e3_trial` in `harness/runner.py`, `harness/test_e3_a4.py`), and its recordings are
included for the data-only path.

A faulted invocation is limited to 30 cells, i.e. arms × repetitions; larger runs are split
across invocations. Transcript-judge verdicts are computed from finished recordings by `apply_a2` in
`harness/a2_judge.py`.

**Time and cost, from the recorded runs:**
- **Unfaulted populations:** trials run concurrently, so 56 trials take about 5–10 minutes.
- **Faulted populations:** one fault at a time, about 1 minute per trial ungated and about
  2 minutes per trial gated.
- **Overhead benchmark:** about 80 minutes.
- **Spend:** the two Anthropic arms together recorded USD 1.5–2.4 per 56-trial population.
  Costs for the openai-compatible arms depend on the endpoint.
- **Host:** the recordings were made from the host described in `ENVIRONMENT.md`.

## Harness version

The shipped harness is version `c1e76c2` of the project repository, with comments edited
and deployment values moved into `config/`; `make test` exercises it. Each recording's
`harness_version` field names the version that produced it, as a commit of the project
repository's history:

| Population | `harness_version` (trials) |
|---|---|
| placement, no fault; gated placement, no fault | `8d416a1` (56 + 56) |
| placement, injected delay | `a566fa5` (44), `f627555` (12) |
| gated placement, injected delay | `a566fa5` (16), `b07163d` (40) |
| deletion | `df7db39` (45), `c73e9bb` (11) |
| informed prompt; paired uninformed | `005c442` (56 + 56) |
| gated deletion | `5543d4d` (48), `7d06a7c` (8) |

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

`prompts/` holds every prompt verbatim:
- the placement and deletion task text as the agents received it;
- the variant that additionally states the condition;
- the transcript judge's system prompt and user template;
- the goal-to-condition derivation prompt.

## License and citation

Code is under the MIT License; the recordings (including the two test recordings in
`harness/testdata/`) and the prompts are under CC BY 4.0 (see `LICENSE`). Citation metadata is in `CITATION.cff`.
