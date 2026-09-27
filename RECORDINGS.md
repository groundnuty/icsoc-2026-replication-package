# Recordings: one file per trial

Every trial is one JSON file, `trial-<id>.json`. A trial is one agent episode against
the storage service, plus everything an outside observer recorded while it ran. The
scoring code reads nothing else.

## Top level

| Field | Meaning |
|---|---|
| `trial_id` | unique id for this episode |
| `sweep_id` | the run this trial belongs to |
| `scenario_id` | `E1` placement, `E3` deletion |
| `model_leg` | the agent model, as `<serving>:<model id>` |
| `model_config` | the requested model id and its generation settings |
| `trial_start_utc` | trial start time (UTC); orders the trials of a run |
| `harness_version` | the harness version that produced the trial |
| `trial_k` | replicate index for the deletion runs; 1 for the placement runs, whose replicates are separate trials distinguished by `trial_id` |
| `source_provider` | site that initially holds the file |
| `fault.condition` | the injected condition, or absent on healthy trials |
| `contract` | the guarantee the agent is held to (see below) |
| `expected` | the resolved parameters the verifier checks against |
| `agent` | what the agent did and claimed |
| `state_timeline` | what the independent verifier observed |
| `cost` | verifier probes, model tokens/USD, retry accounting |
| `a2_result` | transcript-judge verdict, where that pass was run |
| `a4_result` | enforcement-gate outcome, on gated runs only |

## `contract.terms[]` — the guarantee

`term_id`, `guarantee` (prose), `slo` (the machine-checkable predicate, its target
sites, and the size or absence condition), `t_max_s` (the deadline in seconds).

## `expected` — resolved per-trial parameters

`per_term.<term_id>` carries `expected_size`, `target_providers`,
`target_provider_labels`, and `expected_paths`. `poll_until_rel_s` is how far past the
deadline the verifier kept polling.

## `agent` — the episode

`prompt_verbatim` (exact task text the agent received), `tool_calls[]` (each with
`name`, `args`, `result`, `ts_rel_s`, `ok`), `messages`, `final_answer`,
`self_reported` (the parsed claim), `reported_done_at_rel_s`.

## `state_timeline` — the independent observation

`samples[]`, each `t_rel_s`, `term_id`, `sli_name`, `value`, `provider`, `probe_ok`.
For placement the byte count at each site is polled every `poll_interval_s` seconds
to `poll_until_rel_s`; for deletion the path-absence and per-site residue probes are
polled the same way. `transfer_status_samples` records the service's own job status
alongside, which the scoring treats as diagnostic only.

## `a4_result` — enforcement gate (gated runs)

`final_verdict`, `final_attribution`, `first_attempt_verdict`, `gate_cycles`,
`attempts`, `remedy_trail[]` (which remedy fired each cycle), `terminated`,
`cost_per_bucket`.

## Populations in `recordings/`

| Directory | Trials | What it is |
|---|---|---|
| `scored_sweep--rq1-control-clean-*` | 56 | placement, no injected condition |
| `scored_sweep--rq2-ss{1,2,3}-gated-*` | 56 | placement, injected delay |
| `e3_sweep--e3-scored-*` | 56 | deletion |
| `a4_sweep--a4-control-v3-*` | 56 | placement under the enforcement gate, no injected condition |
| `a4_sweep--a4-fault-ss{1,2,3}-*` | 56 | placement under the gate, injected delay |
| `scored_sweep--e1-informed-*` | 56 | placement, agent additionally given the condition |
| `scored_sweep--e1-uninformed-paired-*` | 56 | same-day paired control for the above |
| `derivation_pilot_*--E1`, `--E3` | 56 each | goal-to-condition derivation, per item |
| `a2_fault_deletion_*--fault`, `--deletion` | 56 each | transcript-judge verdicts for those two populations |
| `a4_sweep--e3-a4-healthy-v2-*`, `…-v2-glm52-*` | 48 + 8 | deletion under the enforcement gate, no injected condition |
| `a6_overhead--20260926` | 51 runs | verifier overhead benchmark (see below) |

## Two conditions on every trial

**Fixture in place.** A trial counts only if its setup established the state the task assumes before the agent's turn. Each recording stores the outcome in `expected.per_term.<term>.setup_invalid` (with `setup_invalid_reason`). For deletion the setup must confirm a converged two-site replica of the file; in the gated deletion run 6 of the 56 planned trials carry `setup_invalid: true` (reason `no_2replica`) and have no gate result, so 50 are counted. Every other population counts all 56.

**Injected condition.** Where a condition is injected, it is a delay of 35 seconds on the
target site's inter-site transfer traffic, which exceeds the 30-second deadline. It is
recorded per trial in `fault` (`condition`, `netem_delay_ms`, and when it was cleared, in
`cleared_at_rel_s`).

## Verifier overhead benchmark (`a6_overhead--20260926`)

One run = one cell (predicate, N open contracts, poll interval δ) × one repetition. The
verifier process runs N polling loops using the same probe functions as the trials. A
separate reference client reads one file once per second.

| File | Content |
|---|---|
| `<cell>_r<k>.cell.json` | `cell` (predicate, `n`, `delta`, `rep`), `t0`, `warmup_s`, `window_s`, `offsets` (per-loop start offsets), `reads_per_round`, `host` (CPU model, cores, Python), `rusage` (CPU time and load average at window start and end), `maxrss_kb`, `wire` (every request: `t`, `dur`, `status`, `exc`, endpoint class `ep`), `samples` (each loop's recorded samples) |
| second file with the same `<cell>_r<k>` stem | the reference client's `wire` records |
| `manifest.json` | run order, seed, timing constants, per-run status |
| `files.json` | the fixture files the loops polled |
| `dryrun_*` | the single driver check before the grid; not used in results |

Metrics use each run's observation window `[t0 + warmup_s, t0 + warmup_s + window_s)`.
Token requests (`ep = token_mint`) are excluded.
