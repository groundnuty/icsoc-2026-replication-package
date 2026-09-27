#!/usr/bin/env python3
"""A4 live-enforcement sweep driver — parameterized (env-var), envelope-
bounded, teardown-safe live driver that runs the A4 attempt/verify/remedy
GATE LOOP (`harness/a4_gate.py::run_gate`) per trial over the locked 7-leg
panel, records the resulting `a4_result` onto each recording, and
computes RQ3 attainment/cost/safety metrics (`harness/rq3.py`).

Sibling of `scored_sweep_driver.py` (the RQ1+RQ2 driver) — same env-var
convention, same envelope-assert, same teardown-safety shape — but each
CELL runs the A4 gate loop instead of one `run_trial` call:

    SWEEP_FAULT=none        -> healthy A4 sweep (no cap on cell count)
    SWEEP_FAULT=netem_lag   -> A4 fault sub-sweep against `de`, ENVELOPE-
                               CAPPED at 30 fault cells (len(legs) * SWEEP_K)
                               -- split a bigger ask into multiple
                               SWEEP_LEGS-scoped sub-sweep invocations.

Run (from the repo root, UNSANDBOXED — a sandboxing TLS proxy corrupts
large PUTs):

    cd /path/to/repo
    export PLGRID_FORGE_API_KEY=...   # each key variable named in config/arms.json

    # (a) healthy A4 run -- all 7 legs, K=8, no fault:
    RUN_ID=a4-control-<date-or-slug> SWEEP_FAULT=none SWEEP_K=8 \\
        .venv/bin/python a4_sweep_driver.py

    # (b) an example netem_lag fault sub-sweep -- a <=30-cell leg x K subset
    #     (3 legs x K=8 = 24 <= 30). This driver constructs
    #     LiveFaultInjector(operator_sanctioned=True) unconditionally, so
    #     running with SWEEP_FAULT=netem_lag on live federation access IS
    #     the sanction boundary; only run this arm once the netem envelope
    #     for this invocation has been confirmed):
    RUN_ID=a4-netem-sub1 SWEEP_FAULT=netem_lag SWEEP_K=8 \\
        SWEEP_LEGS="zai-org/GLM-4.7-Flash,claude-haiku-4-5-20251001,claude-sonnet-5" \\
        .venv/bin/python a4_sweep_driver.py

`RUN_ID` namespaces both the provisioned space's name (`icsoc_sweep_<RUN_ID>_
<uuid8>`) and the recordings directory
(`<recordings_dir>/a4_sweep/<RUN_ID>/`, with `recordings_dir` from config/federation.json), so re-runs / parallel
sub-sweeps never collide. `.venv` must already have `harness/requirements.txt`
installed (claude-agent-sdk, openai, mcp).

--- Env vars (all optional; defaults produce the full 7-leg healthy sweep) ---

    RUN_ID        namespace slug (default "a4_sweep")
    SWEEP_LEGS    comma-separated leg model_ids from the locked 7-leg panel
                  below (default: all 7, Forge-then-SDK order)
    SWEEP_K       int, trial replicate count per (leg, fault) cell (default 8)
    SWEEP_FAULT   "none" (default) | "netem_lag"

Poll window (`POLL_UNTIL_REL_S`=45.0 / `POLL_INTERVAL_S`=3.0) is NOT
env-parameterized, matching scored_sweep_driver.py's convention (only
RUN_ID/SWEEP_LEGS/SWEEP_K/SWEEP_FAULT are). `POLL_UNTIL_REL_S` is
descriptive-only for cycle 1 (`harness/a4_gate.T_MAX_S +
harness/a4_gate.OBSERVATION_MARGIN_S` == 45.0) — see the per-cycle
deadline-window design below.

--- Per-cycle deadline window (why it is a mutable, per-trial dict, not a
fixed window) ---

A FIXED `poll_until_rel_s` + `trial_start_epoch` pair passed into
`a4_gate.make_live_verify_fn` would make EVERY gate cycle re-poll the SAME
`[0, poll_until_rel_s]` window relative to the ORIGINAL trial start. A
retried cycle (>=2) would then run AFTER that horizon had already elapsed in
real wall-clock time, so `poll_state_timeline` would return only a single,
already-late sample and `a3b.verdict()` could never again see a sample
inside `[0, T_max]` — `Fulfilled` would be structurally UNREACHABLE on any
retried cycle, making RQ3's `attainment_uplift` ≡ 0 by construction,
independent of any live data. Instead, `_run_a4_trial` threads a MUTABLE,
per-trial `window = {"anchor_epoch", "deadline_epoch"}` dict (both ABSOLUTE
wall-clock epochs) through BOTH `a4_gate.make_live_attempt_fn` and
`a4_gate.make_live_verify_fn` — an agent re-invoke RE-ANCHORS the window to
a fresh `T_max` horizon; this driver's own `wait_fn` EXTENDS the deadline to
an ABSOLUTE `time.time() + units * a4_gate.WAIT_UNIT_S` (never an offset
accumulated relative to the original anchor, which would chase elapsed real
time and never catch up — see `harness/a4_gate.py`'s module docstring for
the design rationale). `verify_fn`'s own forward-blocking poll IS
the wait for a `wait_repoll` cycle, so `wait_fn` never needs a separate
sleep.

--- Replicate construction (read before trusting the recorded `"trial_k"`
field) ---

Exactly like `scored_sweep_driver.py` (see that file's own module docstring),
this driver reuses `sweep_driver.make_execute_cell` verbatim as the per-cell
executor, with `run_trial_fn` set to `_run_a4_trial` (this file's
run_trial-shaped A4 gate-loop function). `_run_a4_sweep` (below) builds the
SWEEP_K replicates per (leg, fault) cell the same way `_run_scored_sweep`
does: it calls `sweep.enumerate_cells(...)` once for each k in 1..SWEEP_K,
concatenates the resulting Cell lists into one list, and hands that single
list to `sweep_run.run_plan(...)` in one call. `make_execute_cell`'s
`execute_cell` closure calls `run_trial_fn(...)` exactly once per Cell
without forwarding `cell.trial_k`, so each (leg, fault) cell yields SWEEP_K
separate `_run_a4_trial` invocations, each with its own trial_id, recording
file, and A4 gate episode, while every trial's recorded `"trial_k"` field
reads `1`. `rq3.py`'s `rq3_metrics` groups recordings purely by
`recording["model_leg"]`, never by `trial_k`.

--- DESIGN NOTES (the parts most likely to drift from run_trial's/
scored_sweep_driver's shape) ---

`a4_gate.make_live_verify_fn`'s returned `verify_fn` does NOT surface the
raw `Verifier.poll_state_timeline` return value it uses internally (it only
returns the DERIVED verdict/attribution dict); `a4_gate.make_live_attempt_fn`
similarly appends the underlying `LegResult` to a caller-owned `trace_sink`
but exposes no per-attempt wall-clock anchor. Both are needed to assemble a
correct top-level `agent` / `state_timeline` for the FINAL recording (which
must independently pass `harness/schema.py`'s validate() and must be
faithful enough for `rq3.py`'s post-gate-violation safety check — the check
that A4 never records final_verdict == 'Fulfilled' for a trial whose FULL
recorded state-timeline actually disagrees). `a4_gate.py` is left
unmodified; instead, this driver wraps its two live dependencies in
transparent, side-effect-free capturing proxies
(`_TimestampingLeg`, `_PollCapturingVerifier`, below) that forward every real
call unchanged and additionally stash what's needed for the final record.
See those classes' docstrings for the exact reasoning.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import time
import uuid
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from harness import a4_gate  # noqa: E402
from harness import fixtures  # noqa: E402
from harness import netem  # noqa: E402
from harness import panel as panel_mod  # noqa: E402
from harness import provision  # noqa: E402
from harness import runner as runner_mod  # noqa: E402
from harness import rq3  # noqa: E402
from harness import schema  # noqa: E402
from harness import sweep  # noqa: E402
from harness import sweep_driver  # noqa: E402
from harness import sweep_run  # noqa: E402
from harness import verifier as verifier_mod  # noqa: E402
from harness import settings  # noqa: E402
from harness import arms as arms_mod  # noqa: E402

# --- scenario / infra constants (verbatim from scored_sweep_driver.py) --------

SOURCE_HOST = settings.get("source.host")
SOURCE_PROVIDER_LABEL = settings.get("source.label")
TARGET_PROVIDER_LABEL = settings.get("target.label")

# The "de" oneprovider/onezone entity id -- the CANONICAL provider-ID
# namespace (matches state_timeline.samples[].provider). Same value used for
# BOTH the scenario's target_provider_id AND FaultSpec.target_provider — see
# scored_sweep_driver.py's DE_NETEM_TARGET comment for why the second use
# matters (schema.py's fault cross-namespace guard).
DE_ID = settings.get("target.provider_id")

# The "cloud-pl" oneprovider/onezone entity id — same provider-ID namespace
# as DE_ID above. Needed ONLY for the multi-perspective space-readiness
# precheck below (provision.Provisioner.wait_for_space_ready requires
# provider IDs, not human labels — the onezone `supportingProviders` field
# reported for a space is not reliable for this). Matches
# harness/test_sweep_driver.py's `PL` constant.
CLOUD_PL_ID = settings.get("source.provider_id")

# Bounded wait for the space-readiness precheck (provision.wait_for_space_
# ready). Kept tight (45.0, not a more generous 120.0): inter-provider
# support propagation is seconds-scale eventual consistency (a four-way
# cross-check across onezone's support map, provider-list endpoint, and
# per-provider liveness); 45s is still generous relative to that, while
# refusing to tolerate an arbitrary long wait. On timeout this FAILS LOUD
# (see the FATAL block below) rather than waiting further — the
# module-level default lives at provision.SPACE_READY_TIMEOUT_S; this
# driver-local constant is kept for the print/log lines below.
SPACE_READY_TIMEOUT_S = provision.SPACE_READY_TIMEOUT_S
SPACE_READY_POLL_INTERVAL_S = 5.0

# The locked 7-leg panel. Verbatim from scored_sweep_driver.py.
ARMS = arms_mod.load_arms()
ARMS_BY_MODEL_ID = {a["model_id"]: a for a in ARMS}
FORGE_MODEL_IDS = tuple(a["model_id"] for a in ARMS if a["kind"] == "openai-compatible")
SDK_MODEL_IDS = tuple(a["model_id"] for a in ARMS if a["kind"] == "anthropic-sdk")
ALL_LEG_MODEL_IDS = FORGE_MODEL_IDS + SDK_MODEL_IDS

# The sanctioned fault-cell cap (netem_lag only; healthy `none` sweeps are
# uncapped) -- per the build task's ENVELOPE ASSERT requirement.
FAULT_CELL_CAP = 30
NETEM_DELAY_MS = 35000.0  # > T_max=30s, comfortably under the 60s netem envelope cap

# Poll window per gate-cycle verify_fn call: > T_max (30s) to observe
# (non-)convergence, < the 60s netem clear-verification envelope. Fixed (not
# env-parameterized) per the build task's env-var list.
POLL_UNTIL_REL_S = 45.0
POLL_INTERVAL_S = 3.0

SPACE_NAME_PREFIX = "icsoc_sweep_"

RUN_ID = os.environ.get("RUN_ID", "a4_sweep")
SWEEP_FAULT = os.environ.get("SWEEP_FAULT", "none").strip()
_SWEEP_LEGS_RAW = os.environ.get("SWEEP_LEGS", "").strip()
RECORDINGS_DIR = os.path.join(settings.get("recordings_dir", "runs"), "a4_sweep", RUN_ID) + os.sep


# --- env preconditions (Pattern D -- aggregate-fail-loud, silent-fallback-hazards.md) ---
# Verbatim logic from scored_sweep_driver.py's _check_env_preconditions.

def _parse_sweep_legs() -> list:
    """`SWEEP_LEGS` (comma-separated model_ids) -> the ordered list of legs
    this run should build. Empty/unset -> the full locked 7-leg panel,
    Forge-then-SDK order (matches FORGE_MODEL_IDS + SDK_MODEL_IDS above)."""
    if not _SWEEP_LEGS_RAW:
        return list(ALL_LEG_MODEL_IDS)
    return [s.strip() for s in _SWEEP_LEGS_RAW.split(",") if s.strip()]


def _check_env_preconditions(selected_leg_ids: list, sweep_k: int, sweep_fault: str) -> None:
    """Fail loud, aggregating ALL problems in one message, before touching
    the federation, minting any token, or provisioning a space."""
    problems: list = []

    unknown = [m for m in selected_leg_ids if m not in ALL_LEG_MODEL_IDS]
    if unknown:
        problems.append(
            f"SWEEP_LEGS names model id(s) not in the locked 7-leg panel: {unknown!r} "
            f"(panel is {ALL_LEG_MODEL_IDS!r})."
        )

    if sweep_fault not in (sweep.FAULT_NONE, sweep.FAULT_NETEM_LAG):
        problems.append(
            f"SWEEP_FAULT={sweep_fault!r} is not one of the two sanctioned values for this "
            f"driver: {sweep.FAULT_NONE!r} or {sweep.FAULT_NETEM_LAG!r}."
        )

    for key_env in sorted({ARMS_BY_MODEL_ID[m]["key_env"] for m in selected_leg_ids
                           if m in ARMS_BY_MODEL_ID and ARMS_BY_MODEL_ID[m]["key_env"]}):
        if not os.environ.get(key_env, "").strip():
            problems.append(f"{key_env} is missing/empty; the selected arm(s) need it "
                            "(see config/arms.json).")
    for stray_var in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY"):
        if os.environ.get(stray_var, "").strip():
            problems.append(
                f"{stray_var} is SET in env — the Anthropic SDK legs must run on the ambient "
                "subscription through the Claude CLI login. "
                f"Unset {stray_var} for this run."
            )

    if sweep_fault == sweep.FAULT_NETEM_LAG:
        n_fault_cells = len(selected_leg_ids) * sweep_k
        if n_fault_cells > FAULT_CELL_CAP:
            problems.append(
                f"envelope violation: SWEEP_FAULT=netem_lag would enumerate {n_fault_cells} "
                f"fault cells (legs={len(selected_leg_ids)} x SWEEP_K={sweep_k}), exceeding the "
                f"sanctioned fault-cell cap of {FAULT_CELL_CAP} — split into sub-sweeps via "
                "SWEEP_LEGS (a smaller leg subset per invocation)."
            )

    if problems:
        print("FATAL: env preconditions not met — aborting before any federation/model contact:",
              file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        raise SystemExit(1)


# --- leg construction (verbatim from scored_sweep_driver.py) ------------------

def _build_leg(model_id: str) -> "panel_mod.ModelLeg":
    """The leg for one arm of config/arms.json, built by `harness.arms.build_leg`."""
    arm = ARMS_BY_MODEL_ID.get(model_id)
    if arm is not None:
        return arms_mod.build_leg(arm)
    raise ValueError(f"_build_leg: {model_id!r} is not in the locked 7-leg panel")


# --- netem fault-injection plumbing (verbatim from scored_sweep_driver.py;
# constructed regardless of SWEEP_FAULT, only ever EXERCISED when
# SWEEP_FAULT=netem_lag) -----------------------------------------------------

DE_NETEM_TARGET = netem.ProviderTarget(
    provider=DE_ID,
    # ABSOLUTE path (not ~/.kube/de-config): netem.py builds `KUBECONFIG=<_q(path)>`
    # which SINGLE-QUOTES the value, so a leading ~ is NOT expanded by the remote
    # shell → kubectl falls back to localhost:8080 ("connection refused").
    kubeconfig=settings.get("fault.kubeconfig"),
    namespace=settings.get("fault.namespace"),
    pod=settings.get("fault.pod"),
    container=settings.get("fault.container"),
    iface=settings.get("fault.iface", "eth0"),
)

NETEM_ENVELOPE = netem.NetemEnvelope(
    provider_allow=(DE_ID,), max_delay_ms=60000.0, netem_only=True,
)


def _make_netem_injector() -> netem.NetemInjector:
    """Construct the (idle until exercised) NetemInjector — SSH runner over
    the same jump host verifier.py/fixtures.py already use. Pure/no I/O at
    construction time — safe to build even for a SWEEP_FAULT=none run."""
    runner_fn = netem.ssh_runner(settings.get("fault.ssh_host"))
    return netem.NetemInjector(
        runner=runner_fn, targets={DE_ID: DE_NETEM_TARGET}, envelope=NETEM_ENVELOPE,
    )


# --- transparent capturing proxies (this file's own addition — a4_gate.py is
# untouched) ------------------------------------------------------------------

class _TimestampingLeg:
    """Transparent proxy around a real `panel.ModelLeg`: forwards `.run(...)`
    unchanged but ALSO records `time.time()` immediately before every call
    into `timestamps` (a caller-owned list). Every other attribute access
    (`.name`, `.model_config()`, isinstance checks elsewhere) delegates to
    the real leg via `__getattr__` — this proxy is only ever handed to
    `a4_gate.make_live_attempt_fn`, which calls nothing but `.run(...)`.

    Why: `a4_gate.make_live_attempt_fn`'s own `attempt_fn` appends the
    resulting `LegResult` to a caller-owned `trace_sink` (already exposed to
    this driver), but records no timestamp for when that attempt's `leg.run()`
    actually started — needed to re-anchor that attempt's `tool_calls[].
    ts_rel_s` (measured against the LEG's own internal monotonic clock) onto
    the trial's single epoch, exactly as `runner._agent_dict_from_leg_result`
    already does for `run_trial`'s single-attempt case. Since `attempt_fn`
    calls are always awaited to completion before the next one starts
    (`run_gate`'s loop is sequential), `timestamps[i]` and `trace_sink[i]`
    (the caller's own list, populated by `make_live_attempt_fn`) are always
    index-aligned: attempt i's `.run()` call appends its start time here
    FIRST, then (after the real leg resolves) `make_live_attempt_fn` appends
    its `LegResult` to `trace_sink` — both exactly once per attempt, in the
    same order.
    """

    def __init__(self, real_leg, timestamps: list):
        self._real = real_leg
        self._timestamps = timestamps

    async def run(self, *args, **kwargs):
        self._timestamps.append(time.time())
        return await self._real.run(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _PollCapturingVerifier:
    """Transparent proxy around a real `verifier.Verifier`: forwards
    `.poll_state_timeline(...)` unchanged but ALSO appends its raw return
    dict to `capture` (a caller-owned list) after the real call completes.
    Every other attribute delegates via `__getattr__`.

    Why: `a4_gate.make_live_verify_fn`'s own `verify_fn` calls
    `verifier.poll_state_timeline(...)` internally to build a MINIMAL
    scoring-only recording for A3b, then returns only the DERIVED verdict/
    attribution dict — the raw poll result (the actual `state_timeline`
    samples) is never surfaced to the caller. This driver needs that raw
    poll result, from EVERY gate cycle, to assemble the top-level record's
    `state_timeline` field (see `_run_a4_trial`'s state_timeline-merge
    comment for why ALL captured polls are merged, not just the last one).
    ZERO extra network calls: this only observes the return value of a call
    `make_live_verify_fn` was already going to make.
    """

    def __init__(self, real_verifier, capture: list):
        self._real = real_verifier
        self._capture = capture

    def poll_state_timeline(self, *args, **kwargs):
        result = self._real.poll_state_timeline(*args, **kwargs)
        self._capture.append(result)
        return result

    def __getattr__(self, name):
        return getattr(self._real, name)


# --- the per-trial A4 gate-loop function --------------------------------------
# run_trial-SHAPED (same positional/keyword call convention as
# runner.run_trial) so it slots UNCHANGED into sweep_driver.make_execute_cell
# as `run_trial_fn` -- this is how netem inject-before/clear-after AND
# per-cell error isolation are inherited for free (see module docstring).

async def _run_a4_trial(
    scenario: dict,
    leg: "panel_mod.ModelLeg",
    fixture_mgr,
    verifier,
    fault: Optional[dict] = None,
    *,
    sweep_id: str = "a4-sweep",
    trial_k: int = 1,
    concurrency: int = 1,
    content_size: int = runner_mod.DEFAULT_CONTENT_SIZE,
    poll_until_rel_s: float = POLL_UNTIL_REL_S,
    poll_interval_s: float = POLL_INTERVAL_S,
    arm_live: str = "A4",
    arm_class: str = "contribution",
    recordings_dir: str = RECORDINGS_DIR,
) -> dict:
    """Run ONE A4 trial: fixture-write, then the full `a4_gate.run_gate`
    attempt/verify/remedy episode against `leg`, then assemble + validate +
    write a schema-valid §3 recording carrying `a4_result`. Mirrors
    `runner.run_trial`'s fixture-write / scenario-build / record-assembly
    shape up to the point where A4 differs (the gate loop replaces the
    single leg.run() + single verifier poll).
    """
    trial_id = f"trial-{uuid.uuid4().hex[:12]}"
    harness_version = runner_mod._git_harness_version()
    leg_scaffold = runner_mod._leg_scaffold_for(leg)
    otel_correlated = runner_mod._otel_correlated_for(leg)

    trial_start_epoch = time.time()
    trial_start_utc = runner_mod._utc_now_iso()

    term_id = scenario["term_id"]
    t_max_s = scenario["t_max_s"]
    target_provider_label = scenario["target_provider_label"]
    target_provider_id = scenario["target_provider_id"]

    rel_path = f"harness/a4_sweep/{trial_id}.bin"
    predicted_full_path = f"/{scenario['space_name']}/{rel_path}"

    trial_scenario = dict(scenario)
    trial_scenario["full_path"] = predicted_full_path

    system_prompt = runner_mod.build_system_prompt(trial_scenario)
    user_prompt = runner_mod.build_user_prompt(trial_scenario)

    file_id = None

    try:
        content_bytes = b"x" * content_size
        write_result = fixture_mgr.write_trial_file(rel_path, content_bytes)
        file_id = write_result["file_id"]
        full_path = write_result["full_path"]
        expected_size = write_result["expected_size"]
        assert full_path == predicted_full_path, (
            f"FixtureManager path convention drifted: predicted {predicted_full_path!r}, "
            f"got {full_path!r}"
        )

        contract = {
            "obligated_party": trial_scenario["obligated_party"],
            "terms": [
                {
                    "term_id": term_id,
                    "guarantee": trial_scenario["guarantee"],
                    "slis": trial_scenario["slis"],
                    "slo": trial_scenario["slo"],
                    "t_max_s": t_max_s,
                    "remedy": trial_scenario["remedy"],
                }
            ],
        }
        expected = {
            "fixture_space_id": trial_scenario["space_id"],
            "poll_until_rel_s": poll_until_rel_s,
            "per_term": {
                term_id: {
                    "expected_size": expected_size,
                    "target_providers": [trial_scenario["target_provider_id"]],
                    "target_provider_labels": [target_provider_label],
                    "expected_paths": [full_path],
                }
            },
        }

        # --- gate-loop plumbing: trace_sink is a4_gate's OWN convention
        # (shared with make_live_verify_fn for its internal per-cycle A3b
        # scoring); leg_call_timestamps / poll_captures are THIS driver's
        # additions via the transparent proxies above. `window` is the
        # per-cycle deadline-window design (see this module's docstring
        # "Per-cycle deadline window" section + harness/a4_gate.py's module
        # docstring) — SHARED, mutable, and passed to BOTH live factories
        # below so an agent re-invoke (attempt_fn) and a service wait
        # (this file's own wait_fn) both mutate the SAME window `verify_fn`
        # reads on its NEXT call. ---
        trace_sink: list = []
        leg_call_timestamps: list = []
        poll_captures: list = []
        window = {"anchor_epoch": trial_start_epoch, "deadline_epoch": trial_start_epoch + a4_gate.T_MAX_S}

        wrapped_leg = _TimestampingLeg(leg, leg_call_timestamps)
        capturing_verifier = _PollCapturingVerifier(verifier, poll_captures)

        attempt_fn = a4_gate.make_live_attempt_fn(
            wrapped_leg,
            trial_scenario,
            allowed_tools=runner_mod.E1_FORM_B_ALLOWED_TOOLS,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            trace_sink=trace_sink,
            window=window,
        )
        verify_fn = a4_gate.make_live_verify_fn(
            capturing_verifier,
            file_id=file_id,
            expected_size=expected_size,
            target_provider_id=target_provider_id,
            target_provider_label=target_provider_label,
            term_id=term_id,
            t_max_s=t_max_s,
            poll_interval_s=poll_interval_s,
            trial_start_epoch=trial_start_epoch,
            trace_sink=trace_sink,
            contract=contract,
            expected=expected,
            window=window,
        )

        # wait_fn(units): run_gate calls this ONLY between a rejected verify
        # and the NEXT one, on the "service"/"both"/"neither" remedy paths
        # (extending the observation window might let a still-in-flight
        # replication converge). `units` is always literally `1` in the
        # current run_gate implementation (test_a4_gate.py's WaitStub only
        # ever records `1`).
        #
        # `wait_fn` deliberately does NOT also `asyncio.sleep(units *
        # t_max_s)` — a real sleep — on top of a verify_fn that re-polls a
        # window. Combining a real sleep with a FIXED window would make a
        # `wait_repoll`-only cycle's Fulfilled verdict UNREACHABLE: by the
        # time the next verify_fn's poll started, real elapsed time (from
        # the ORIGINAL anchor) would already have passed the window's own
        # horizon, so poll_state_timeline would return a single, already-late
        # sample every time. Instead: `wait_fn` ONLY extends
        # `window["deadline_epoch"]` to an ABSOLUTE (never-in-the-past)
        # `time.time() + units * WAIT_UNIT_S` — the anchor is left UNCHANGED
        # (a pure wait never re-anchors; only an agent re-invoke does, in
        # `a4_gate.make_live_attempt_fn`). No separate sleep is needed:
        # `verify_fn`'s own `poll_state_timeline` call polls
        # forward from `anchor` to the EXTENDED horizon
        # (`deadline_epoch - anchor_epoch + OBSERVATION_MARGIN_S`), and that
        # forward-blocking poll itself consumes the real wall-clock wait
        # time — "the poll IS the wait" (see harness/a4_gate.py's module
        # docstring). One "unit" of extension is one `WAIT_UNIT_S` (== one
        # T_max, the SLA's own convergence horizon) — the natural timescale
        # the remedy table's own language ("extending the observation
        # window") is measured against.
        async def wait_fn(units: int) -> None:
            window["deadline_epoch"] = time.time() + units * a4_gate.WAIT_UNIT_S

        # escalate_fn(): the "neither"-attribution / NotDetermined-rollup
        # remedy — no corroborating evidence anywhere, so the
        # policy is "escalate (signal an escalation hook) and
        # still allow one bounded re-poll". No escalation hook
        # exists yet for a live driver invocation, so this is a log-only
        # no-op ("async no-op that logs") — `run_gate` still applies its
        # own wait_fn(1) right after this on the same remedy branch, and the
        # stopping rule (n_max / wall-clock budget) still bounds the loop
        # regardless of whether escalation is wired to anything real.
        async def escalate_fn() -> None:
            print(
                f"[A4 escalate] trial_id={trial_id} leg={leg.name}: no corroborating "
                "evidence for the rejected verdict (emitted_attribution='neither' or "
                "unrecognized) — no escalation hook is wired for this "
                "driver; logging only. The stopping rule (n_max/wall_clock_budget) still "
                "bounds the gate loop."
            )

        a4_result = await a4_gate.run_gate(
            attempt_fn,
            verify_fn,
            wait_fn=wait_fn,
            escalate_fn=escalate_fn,
            n_max=a4_gate.N_MAX_DEFAULT,
            wall_clock_budget_s=a4_gate.WALL_CLOCK_BUDGET_S_DEFAULT,
        )

        trial_elapsed_s = round(time.time() - trial_start_epoch, 3)

        # --- state_timeline: MERGE every captured verify_fn poll into ONE
        # timeline, rather than using only the LAST call's poll result. Each
        # `Verifier.poll_state_timeline` call (via `a4_gate.make_live_verify_fn`'s
        # per-cycle deadline window) polls from THAT cycle's
        # own `anchor_epoch` forward to THAT cycle's own extended horizon —
        # so cycle N's own poll_result alone does NOT carry the earlier
        # cycles' samples. Using only the LAST call's poll_result for a
        # multi-cycle trial would therefore hand rq3.py's post-gate-violation
        # safety check (the EPISODE-END final-state truth check) an
        # incomplete timeline, unable to see samples from earlier cycles.
        # Merging every captured poll's samples (chronologically concatenated
        # -- each call only ever samples AFTER the previous one's window, so
        # append-order is already time-order) is a superset of "the last
        # call's result" (identical when there was only one gate cycle, the
        # common Fulfilled-on-first-try case) and gives the safety check the
        # FULL episode's observations, matching the check's own framing
        # ("the FULL recorded state-timeline").
        merged_samples: list = []
        for poll_result in poll_captures:
            merged_samples.extend(poll_result.get("samples") or [])
        state_timeline = {
            "poll_interval_s": poll_interval_s,
            "poll_until_rel_s": poll_until_rel_s,
            "polled_past_t_max": (
                poll_captures[-1].get("polled_past_t_max", True) if poll_captures else True
            ),
            "samples": merged_samples,
        }

        # --- agent: the LAST attempt's LegResult is the base for the
        # "final state" fields (final_answer/messages/self_reported/
        # reported_done_at_rel_s/raw_trace/error) -- these are inherently
        # about how the episode ENDED. tool_calls is instead the MERGED
        # (re-anchored-per-attempt) trace across the WHOLE episode: this
        # mirrors what a4_gate.make_live_verify_fn's OWN internal verify_fn
        # already does for ITS per-cycle A3b scoring (it flattens
        # trace_sink's tool_calls across every attempt so far), and matters
        # for real: competence.agent_competence (read by a3b.emit, and
        # transitively anything scoring these recordings later) looks for
        # the qualifying schedule_file_replication call anywhere in
        # agent.tool_calls -- restricting to only the LAST attempt would
        # silently drop an earlier attempt's correct action from view.
        assert trace_sink, "run_gate always makes >=1 attempt_fn call before returning"
        merged_tool_calls: list = []
        for i, leg_result_i in enumerate(trace_sink):
            offset_s_i = (
                (leg_call_timestamps[i] - trial_start_epoch) if i < len(leg_call_timestamps) else 0.0
            )
            per_attempt_dict = runner_mod._agent_dict_from_leg_result(leg_result_i, user_prompt, offset_s_i)
            merged_tool_calls.extend(per_attempt_dict["tool_calls"])

        last_leg_result = trace_sink[-1]
        # Reconstruct the exact composed prompt a4_gate.make_live_attempt_fn
        # used for the LAST attempt, from a4_result's own recorded feedback
        # (a4_result["attempts"][-1]["feedback"]) -- mirrors
        # make_live_attempt_fn's own formula
        # (`user_prompt if feedback is None else f"{user_prompt}\n\n{feedback}"`)
        # verbatim, since that composed string is otherwise internal to the
        # factory's closure and never surfaced.
        last_feedback = a4_result["attempts"][-1]["feedback"]
        last_prompt_verbatim = (
            user_prompt if last_feedback is None else f"{user_prompt}\n\n{last_feedback}"
        )
        last_offset_s = (
            (leg_call_timestamps[-1] - trial_start_epoch) if leg_call_timestamps else 0.0
        )
        agent_dict = runner_mod._agent_dict_from_leg_result(
            last_leg_result, last_prompt_verbatim, last_offset_s
        )
        agent_dict["tool_calls"] = merged_tool_calls

        fault_dict = dict(fault) if fault is not None else {"condition": "none"}
        if fault_dict.get("condition") != "none":
            # sweep_driver.make_execute_cell seeds cleared_at_rel_s =
            # poll_until_rel_s -- correct for a single-attempt run_trial
            # trial, whose netem-active window IS exactly [0,
            # poll_until_rel_s]. An A4 gate episode can run well past that
            # horizon on a retry (up to wall_clock_budget_s), and
            # make_execute_cell's OWN `finally: injector.clear(handle)`
            # only runs once THIS function returns -- so netem really was
            # active for the whole `trial_elapsed_s` gate episode, not just
            # poll_until_rel_s. Overwrite with the ACTUAL measured episode
            # duration: the "honest record of when the perturbation was
            # actually in effect" make_execute_cell's own docstring asks
            # for, generalized to A4's variable-duration case.
            fault_dict["cleared_at_rel_s"] = trial_elapsed_s

        total_prompt_tokens = sum(lr.prompt_tokens for lr in trace_sink)
        total_completion_tokens = sum(lr.completion_tokens for lr in trace_sink)
        total_cost_usd = sum(lr.cost_usd for lr in trace_sink)
        model_cost_dict = {
            "prompt_tokens": total_prompt_tokens,
            "completion_tokens": total_completion_tokens,
            "estimation": last_leg_result.cost_estimation,
            "usd": round(total_cost_usd, 6),
        }

        record = {
            "trial_id": trial_id,
            "sweep_id": sweep_id,
            "recorded_at": runner_mod._utc_now_iso(),
            "harness_version": harness_version,
            "trial_start_utc": trial_start_utc,
            "scenario_id": trial_scenario["scenario_id"],
            "arm_live": arm_live,
            "arm_class": arm_class,
            "model_leg": leg.name,
            "trial_k": trial_k,
            "concurrency": concurrency,
            "model_config": leg.model_config(),
            "source_provider": trial_scenario["source_provider_label"],
            "agent_mcp_host": trial_scenario["source_host"],
            "fault": fault_dict,
            "contract": contract,
            "expected": expected,
            "agent": agent_dict,
            "state_timeline": state_timeline,
            "cost": {
                "verifier": {
                    "probe_count": len(state_timeline["samples"]),
                    # No clean "verifier-only" wall-clock isolate exists for
                    # a multi-cycle gate episode (verification is interleaved
                    # with agent attempts across cycles, unlike run_trial's
                    # single concurrent leg-run + verifier-poll pair) -- the
                    # trial's own total elapsed time is the best available
                    # proxy; it over-counts (includes agent-attempt time
                    # too), documented here rather than silently assumed
                    # precise.
                    "wall_latency_s": trial_elapsed_s,
                },
                "model": model_cost_dict,
                "retry": {
                    "attempts": a4_result["cost_per_bucket"]["agent_invocations"] - 1,
                    "model_usd": round(sum(lr.cost_usd for lr in trace_sink[1:]), 6),
                    "verifier_probe_count": a4_result["cost_per_bucket"]["verifier_probes"],
                },
            },
            "otel": {
                "harness_trace_id": str(uuid.uuid4()),
                "poll_interval_s": poll_interval_s,
                "correlated": otel_correlated,
            },
            "leg_scaffold": leg_scaffold,
            "a4_result": a4_result,
        }

        schema.validate(record)

        os.makedirs(recordings_dir, exist_ok=True)
        recording_path = os.path.join(recordings_dir, f"{trial_id}.json")
        with open(recording_path, "w") as f:
            f.write(schema.to_jsonl(record))

        return record

    finally:
        if file_id is not None:
            fixture_mgr.teardown_trial(file_id)


# --- the K-fold-replicating equivalent of sweep_driver.run_sweep --------------
# (same construction as scored_sweep_driver._run_scored_sweep — see this
# file's module docstring's "Replicate construction" section — minus the RQ1/RQ2
# aggregate/confusion tail, which A4 doesn't need; RQ3 is computed separately
# in main() from the raw recordings.)

def _run_a4_sweep(
    *,
    scenarios: list,
    legs: list,
    fault_specs: list,
    execute_cell,
    trial_k: int,
    arm_live: str,
    max_concurrency: int,
    verify_between,
) -> dict:
    all_cells: list = []
    for k in range(1, trial_k + 1):
        all_cells.extend(
            sweep.enumerate_cells(
                scenarios=scenarios, legs=legs, fault_specs=fault_specs,
                trial_k=k, arm_live=arm_live,
            )
        )

    outcome = sweep_run.run_plan(
        all_cells, execute_cell, max_concurrency=max_concurrency, verify_between=verify_between,
    )
    recordings = [r["result"] for r in outcome["results"] if r["result"] is not None]

    return {
        "recordings": recordings,
        "errors": outcome["errors"],
        "n_cells": outcome["n_cells"],
    }


# --- best-effort orphan report (report-only; NEVER a gate on teardown) ---------
# Verbatim from scored_sweep_driver.py.

def _best_effort_orphan_check(
    provisioner: "provision.Provisioner",
    space_name_prefix: str,
    just_torn_down_space_id: Optional[str],
) -> str:
    try:
        status, _headers, body = provisioner._http(
            "GET", f"{provision.ONEZONE_URL}/api/v3/onezone/user/spaces"
        )
        if status != 200:
            return f"inconclusive — GET user/spaces returned status={status}"

        if isinstance(body, dict) and isinstance(body.get("spaces"), list):
            space_ids = body["spaces"]
        elif isinstance(body, list):
            space_ids = body
        else:
            return f"inconclusive — unrecognized response shape {type(body).__name__} (guessed shape was wrong)"

        other_ids = [sid for sid in space_ids if sid != just_torn_down_space_id]
        if not other_ids:
            return "0 other spaces visible to this token (clean)"

        orphans: list = []
        unresolved: list = []
        for sid in other_ids:
            try:
                s2, _h2, b2 = provisioner._http("GET", f"{provision.ONEZONE_URL}/api/v3/onezone/spaces/{sid}")
                name = b2.get("name") if s2 == 200 and isinstance(b2, dict) else None
            except Exception as e:  # noqa: BLE001
                name = None
                unresolved.append(f"{sid}: {type(e).__name__}: {e}")
            if isinstance(name, str) and name.startswith(space_name_prefix):
                orphans.append(f"{sid} ({name})")

        if orphans:
            report = f"{len(orphans)} ORPHAN '{space_name_prefix}*' space(s) found: {orphans}"
        else:
            report = f"0 '{space_name_prefix}*' orphans found ({len(other_ids)} other space(s) checked)"
        if unresolved:
            report += f"; {len(unresolved)} space(s) could not be name-resolved: {unresolved}"
        return report
    except Exception as e:  # noqa: BLE001 -- report-only; never raise out of a teardown-safety check
        return f"inconclusive (best-effort check itself failed): {type(e).__name__}: {e}"


def _cell_error_to_json(err_entry: dict) -> dict:
    """`{"cell": Cell, "error": str}` -> JSON-safe dict (Cell/FaultSpec are
    frozen dataclasses; `dataclasses.asdict` recursively converts them)."""
    cell = err_entry["cell"]
    cell_json = dataclasses.asdict(cell) if dataclasses.is_dataclass(cell) else str(cell)
    return {"cell": cell_json, "error": err_entry["error"]}


# --- RQ3 report printing (safety-first ordering:
# 1. post_gate_violation_rate, 2. cost_per_verified_success,
# 3. attainment_uplift, 4. termination_rate) -----------------------------------

def _print_rq3_group(label: str, m: dict) -> None:
    print(f"\n--- RQ3 [{label}] (n_trials={m['n_trials']}) ---")
    print(
        f"  1. post_gate_violation_rate = {m['post_gate_violation_rate']} "
        f"(post_gate_violations={m['post_gate_violations']}, final_fulfilled={m['final_fulfilled']}) "
        "[SAFETY CHECK -- expected ~=0]"
    )
    print(
        f"  2. cost_per_verified_success = {m['cost_per_verified_success']} "
        f"(total_cost_buckets={m['total_cost_buckets']})"
    )
    print(
        f"  3. attainment_uplift = {m['attainment_uplift']} "
        f"(final_fulfilled={m['final_fulfilled']} first_attempt_fulfilled={m['first_attempt_fulfilled']} "
        f"attempts_to_success={m['attempts_to_success']})"
    )
    print(f"  4. termination_rate = {m['termination_rate']} (terminated={m['terminated']})")


# --- the driver ------------------------------------------------------------------

async def main() -> int:
    try:
        sweep_k = int(os.environ.get("SWEEP_K", "8"))
    except ValueError:
        print(f"FATAL: SWEEP_K={os.environ.get('SWEEP_K')!r} is not a valid integer", file=sys.stderr)
        return 1

    selected_leg_ids = _parse_sweep_legs()
    _check_env_preconditions(selected_leg_ids, sweep_k, SWEEP_FAULT)

    run_id = RUN_ID
    space_name = f"{SPACE_NAME_PREFIX}{run_id}_{uuid.uuid4().hex[:8]}"
    recordings_dir = RECORDINGS_DIR
    os.makedirs(recordings_dir, exist_ok=True)

    if SWEEP_FAULT == sweep.FAULT_NETEM_LAG:
        fault_specs = [
            sweep.FaultSpec(
                condition=sweep.FAULT_NETEM_LAG, target_provider=DE_ID, netem_delay_ms=NETEM_DELAY_MS,
            )
        ]
    else:
        fault_specs = [sweep.FaultSpec()]  # condition="none"

    print(
        f"=== A4 sweep: run_id={run_id!r} space_name={space_name!r} "
        f"sweep_fault={SWEEP_FAULT!r} sweep_k={sweep_k} legs={selected_leg_ids!r} "
        f"recordings_dir={recordings_dir!r} ==="
    )

    candidate_pool = [_build_leg(m) for m in selected_leg_ids]
    print(f"candidate pool ({len(candidate_pool)} legs): {[leg.name for leg in candidate_pool]}")

    provisioner = provision.Provisioner()
    netem_injector = _make_netem_injector()
    live_injector = sweep.LiveFaultInjector(exec_fn=netem_injector.exec_fn, operator_sanctioned=True)

    space_id: Optional[str] = None

    try:
        # --- 1. provision ONE fresh space (cloud-pl + de) ---
        print(f"\n--- provisioning space {space_name!r} (providers: cloud-pl, de) ---")
        prov_result = provisioner.provision(space_name)
        space_id = prov_result["space_id"]
        print(f"  space_id={space_id} supported={prov_result['supported']}")

        verifier = verifier_mod.Verifier(source_host=SOURCE_HOST)

        # --- 1b. multi-perspective space-readiness precheck (eventual-
        # consistency hardening against a support-propagation silent-
        # fallback). provision()'s 201/200 responses above only prove the
        # API BOUNDARY accepted the requests -- NOT that inter-provider
        # support/auth has actually propagated/stabilised, which is exactly
        # the gap that can produce an rtransfer `unauthorized` failure.
        # NEVER reads `.supportingProviders` (always null -- a trap, not a
        # signal); asserts onezone's user-scoped support map + the onezone
        # provider-list endpoint + each required provider's onezone-reported
        # liveness all agree before spending any trial against this space. ---
        print(
            f"\n--- verifying space readiness (multi-perspective) for {space_id!r} "
            f"(required providers: {CLOUD_PL_ID}, {DE_ID}) ---"
        )
        readiness = provisioner.wait_for_space_ready(
            space_id, [CLOUD_PL_ID, DE_ID], verifier=verifier,
            timeout_s=SPACE_READY_TIMEOUT_S, poll_interval_s=SPACE_READY_POLL_INTERVAL_S,
        )
        print(
            f"  ready={readiness['ready']} elapsed_s={readiness['elapsed_s']:.1f}s "
            f"missing={readiness['missing']!r}"
        )
        if not readiness["ready"]:
            print(
                "FATAL: space-readiness precheck failed -- support has not propagated/"
                f"stabilised across all required perspectives within {SPACE_READY_TIMEOUT_S:.0f}s. "
                f"missing={readiness['missing']!r}\n"
                f"perspectives={json.dumps(readiness['perspectives'], default=str)}\n"
                "Support that has not reached every perspective can leave the inter-site "
                "data path unauthorized, so no trial is spent against this space. "
                "Space teardown still runs below.",
                file=sys.stderr,
            )
            return 1

        fixture_mgr = fixtures.FixtureManager(
            space_name=space_name, space_id=space_id,
            source_host=SOURCE_HOST, source_provider=SOURCE_PROVIDER_LABEL,
        )

        # --- 1c. fail-fast transfer-health canary: the harness fails
        # immediately on a stalled transfer path rather than waiting or
        # tolerating latency. Step 1b above only proves inter-provider
        # SUPPORT has converged; it does NOT prove the rtransfer DATA PATH
        # actually moves bytes -- a fully-supported space can still hit an
        # rtransfer fetch 'unauthorized' failure, or a transfer stuck
        # 'scheduled' with startTime==0 indefinitely. ONE cheap canary
        # transfer, bounded by provision.CANARY_TIMEOUT_S, run ONCE per
        # sweep (not per cell) -- never a long/arbitrary wait; the only
        # remedy for a stalled path is an operational action outside this
        # harness, which never waits on it. ---
        print(f"\n--- transfer-health canary (target={DE_ID}) ---")
        # The canary now self-retries up to provision.CANARY_MAX_ATTEMPTS
        # times (each with its OWN full timeout_s window) to distinguish a
        # transient "completed-with-0-bytes" failure from a persistent stall, so
        # a single transient failure does not abort the sweep.
        head_canary = provision.assert_transfer_path_healthy(fixture_mgr, verifier, DE_ID)
        print(
            f"  healthy={head_canary['healthy']} elapsed_s={head_canary['elapsed_s']:.1f}s "
            f"final_status={head_canary['final_status']!r} physical_size={head_canary['physical_size']!r}"
        )
        print(f"  reason: {head_canary['reason']}")
        if not head_canary["healthy"]:
            print(
                "FATAL: transfer-health canary failed -- the rtransfer data path to "
                f"{DE_ID!r} is not moving bytes within {provision.CANARY_TIMEOUT_S:.0f}s. "
                f"{head_canary['reason']}\n"
                "This is a fail-fast gate: the harness does not wait for a stalled "
                "transfer path to recover and does not attempt to repair it. "
                "Space teardown still runs below.",
                file=sys.stderr,
            )
            return 1

        # --- 2. pre-flight verify_leg on every selected leg (completion-probe form) ---
        print(f"\n--- pre-flight verify_leg over {len(candidate_pool)} selected leg(s) ---")
        verified_pool: list = []
        excluded_legs: list = []
        for leg in candidate_pool:
            ok, detail = await panel_mod.verify_leg(leg)
            print(f"  verify_leg({leg.name}): ok={ok} detail={detail}")
            if ok:
                verified_pool.append(leg)
            else:
                excluded_legs.append({"leg_name": leg.name, "model_id": leg.model_id, "detail": detail})

        if excluded_legs:
            print(
                f"  EXCLUDED {len(excluded_legs)} leg(s) from this sweep (sweep continues): "
                f"{[e['leg_name'] for e in excluded_legs]}"
            )
        if not verified_pool:
            print(
                "FATAL: zero legs survived verify_leg pre-flight — nothing to sweep. "
                "Space teardown still runs below.", file=sys.stderr,
            )
            return 1

        legs_by_name = {leg.name: leg for leg in verified_pool}

        # --- 3. contamination canary (R3 isolation guard) on the first SDK leg, if any ---
        sdk_in_pool = [leg for leg in verified_pool if isinstance(leg, panel_mod.AnthropicSDKLeg)]
        if sdk_in_pool:
            canary_leg = sdk_in_pool[0]
            print(f"\n--- contamination_canary({canary_leg.name}) [R3 isolation guard] ---")
            canary = await runner_mod.contamination_canary(canary_leg)
            print(
                f"  clean={canary['clean']} artifacts_found={canary['artifacts_found']} "
                f"answer={canary['answer']!r}"
            )
            if not canary["clean"]:
                print(
                    "FATAL: contamination canary reports ambient-context leakage on "
                    f"{canary_leg.name} — aborting BEFORE spending any trial. Space teardown "
                    "still runs below.", file=sys.stderr,
                )
                return 1
        else:
            print(
                "\n(no Anthropic SDK leg in the verified pool — skipping the R3 contamination "
                "canary; it only screens claude-agent-sdk ambient-context leakage, which does "
                "not apply to Forge legs)"
            )

        # --- 4. wire the scenario + execute_cell + fault plumbing ---
        base_scenario = runner_mod.make_e1_form_b_scenario(
            space_name=space_name,
            space_id=space_id,
            target_provider_label=TARGET_PROVIDER_LABEL,
            target_provider_id=DE_ID,
            source_provider_label=SOURCE_PROVIDER_LABEL,
            source_host=SOURCE_HOST,
        )

        def _scenario_of_cell(_cell):
            # ONE scenario (E1 Form B, cloud-pl -> de) for this whole driver
            # invocation -- every cell maps to the SAME base scenario dict.
            # `_run_a4_trial` only reads from `scenario` (copies into its own
            # `trial_scenario` before mutating), so sharing this one dict
            # object across concurrently executing cells is safe.
            return base_scenario

        sweep_id = f"a4-sweep-{run_id}"
        verify_between = (
            (lambda cell: netem_injector.verify_clean(cell.perturbs_provider))
            if SWEEP_FAULT == sweep.FAULT_NETEM_LAG else None
        )
        # Non-optional on the fault path: netem must never run without a
        # real cross-cell clear-verification rider (mirrors
        # scored_sweep_driver.py's same assert).
        if SWEEP_FAULT == sweep.FAULT_NETEM_LAG:
            assert verify_between is not None, (
                "fault path requires a real verify_between (netem clear-rider) — refusing "
                "to run netem without cross-cell clear-verification"
            )

        # REUSE sweep_driver.make_execute_cell VERBATIM as the per-cell
        # executor, with `_run_a4_trial` (this file's run_trial-shaped A4
        # gate-loop function) as `run_trial_fn`. This is what gives this
        # driver, "for free": (a) inject-before/clear-in-finally netem
        # wrapping around the WHOLE gate episode (make_execute_cell's own
        # `try/finally` wraps the full `asyncio.run(run_trial_fn(...))`
        # call, i.e. the entire multi-cycle A4 episode, not just one
        # attempt); (b) per-cell error isolation (sweep_run._run_one wraps
        # `execute_cell(cell)` in its own try/except) — satisfying the build
        # task's "wrap each trial in try/except so one leg erroring is
        # captured + the sweep continues (+ netem always cleared)"
        # requirement without re-implementing either mechanism.
        execute_cell = sweep_driver.make_execute_cell(
            fixture_mgr=fixture_mgr,
            verifier=verifier,
            legs_by_name=legs_by_name,
            injector=live_injector,
            scenario_of_cell=_scenario_of_cell,
            run_trial_fn=_run_a4_trial,
            sweep_id=sweep_id,
            poll_until_rel_s=POLL_UNTIL_REL_S,
            poll_interval_s=POLL_INTERVAL_S,
            content_size=runner_mod.DEFAULT_CONTENT_SIZE,
            recordings_dir=recordings_dir,
        )

        # --- 5. run it (K-fold-replicating; see module docstring) ---
        print(
            f"\n--- running A4 sweep: {len(verified_pool)} leg(s) x {sweep_k} replicate(s) x "
            f"{len(fault_specs)} fault_spec(s) = {len(verified_pool) * sweep_k * len(fault_specs)} "
            "gate-loop trial(s) ---"
        )
        result = _run_a4_sweep(
            scenarios=[base_scenario],
            legs=[leg.name for leg in verified_pool],
            fault_specs=fault_specs,
            execute_cell=execute_cell,
            trial_k=sweep_k,
            arm_live="A4",
            max_concurrency=8,
            verify_between=verify_between,
        )

        recordings = result["recordings"]

        # --- 6. RQ3 metrics ---
        rq3_result = rq3.rq3_metrics(recordings)

        print(
            f"\n=== A4 sweep complete: n_cells={result['n_cells']} "
            f"recordings={len(recordings)} errors={len(result['errors'])} ==="
        )

        print("\n=== RQ3 metrics (safety-first ordering) ===")
        _print_rq3_group("overall", rq3_result["overall"])
        for leg_name, m in rq3_result["per_leg"].items():
            _print_rq3_group(leg_name, m)

        # Leg-level failures (schema.is_leg_error — e.g. a Forge model
        # erroring on completion) are surfaced as a DIAGNOSTIC alongside
        # rq3_metrics, not excluded from it: rq3.py itself does not filter
        # on schema.is_leg_error (unlike aggregate.py/rq2.py/panel_score.py)
        # — every recording carrying an a4_result contributes, since
        # a4_gate.run_gate always returns a well-formed a4_result regardless
        # of whether the underlying leg.run() call errored internally (both
        # ForgeOpenAILeg.run and AnthropicSDKLeg.run catch their own
        # exceptions and return a LegResult with `.error` set, never raise).
        leg_errors = [
            {"trial_id": r.get("trial_id"), "model_leg": r.get("model_leg"),
             "error": (r.get("agent") or {}).get("error")}
            for r in recordings if schema.is_leg_error(r)
        ]
        if leg_errors:
            print(
                f"\n{len(leg_errors)} recording(s) carry a leg-level error (schema.is_leg_error) "
                f"— NOT excluded from rq3_metrics above (rq3.py doesn't filter on this), reported "
                f"here for visibility: {[e['model_leg'] for e in leg_errors]}"
            )
        else:
            print("\nno leg-level errors (schema.is_leg_error) among the recordings.")

        if result["errors"]:
            print(f"\n{len(result['errors'])} cell-level error(s) (sweep continued past each):")
            for e in result["errors"]:
                cell = e["cell"]
                print(
                    f"  - leg={cell.model_leg} fault={cell.fault.condition} "
                    f"trial_k={cell.trial_k}: {e['error']}"
                )
        else:
            print("\nno cell-level errors.")

        # --- 6b. TAIL transfer-health canary. The HEAD canary (step 1c
        # above) only proves the rtransfer data path was healthy at sweep
        # START. If the data path stalled during a FAULT arm, stall-driven
        # non-convergence would be indistinguishable from the A4 gate's own
        # behaviour (or from netem's effect). This TAIL canary checks the path
        # is still healthy at sweep END; if not, the run is flagged
        # `quarantined` in the JSON output (results are kept; teardown and
        # reporting continue unchanged). Runs HERE (inside the `try`, after the sweep + its
        # report, before the results-JSON write) so the space is still
        # alive for the canary transfer -- NOT in `finally`, which is
        # teardown-only. ---
        print(f"\n--- TAIL transfer-health canary (target={DE_ID}) ---")
        if SWEEP_FAULT == sweep.FAULT_NETEM_LAG:
            # Clear netem BEFORE the tail probe so it measures PATH health,
            # not the injected fault. sweep_run's own verify_between rider
            # already clears between cells, so `de` should already be clean
            # here -- this is belt-and-suspenders. A still-netem'd path
            # failing the tail canary is a SAFE OVER-QUARANTINE (not a
            # silent pass), so this never gates -- it only tries to give
            # the tail probe a fair (fault-free) read.
            if not netem_injector.verify_clean(DE_ID):
                print(f"  netem not clean on {DE_ID!r} before tail canary -- clearing ...")
                clear_result = netem_injector.exec_fn("clear", fault_specs[0])
                print(f"  clear_result={clear_result}")
                if not netem_injector.verify_clean(DE_ID):
                    print(
                        f"  WARNING: {DE_ID!r} still not netem-clean after clear attempt -- "
                        "tail canary proceeds anyway (a still-netem'd path failing the tail "
                        "is a safe over-quarantine, not a silent pass).",
                        file=sys.stderr,
                    )
        # Same self-retry as the head canary above (provision.CANARY_MAX_ATTEMPTS
        # attempts, transient-versus-persistent discriminator), so a single
        # transient failure does not flag the run.
        tail_canary = provision.assert_transfer_path_healthy(fixture_mgr, verifier, DE_ID)
        print(
            f"  healthy={tail_canary['healthy']} elapsed_s={tail_canary['elapsed_s']:.1f}s "
            f"final_status={tail_canary['final_status']!r} physical_size={tail_canary['physical_size']!r}"
        )
        print(f"  reason: {tail_canary['reason']}")
        print(
            f"  head_canary.healthy={head_canary['healthy']} "
            f"tail_canary.healthy={tail_canary['healthy']}"
        )
        quarantined = not tail_canary["healthy"]
        if quarantined:
            print(
                "QUARANTINE: the TAIL transfer-health canary failed -- the rtransfer data "
                f"path to {DE_ID!r} was NOT healthy at sweep END (it WAS healthy at sweep "
                "START; see head_canary above). A stall during the sweep cannot be "
                "separated from injected-fault or agent effects in the affected window, "
                "so the run is flagged `quarantined`. "
                "Teardown still runs below; this run's JSON output carries "
                "head_canary/tail_canary/quarantined for downstream lens/aggregate code to "
                "filter on.",
                file=sys.stderr,
            )

        run_params = {
            "run_id": run_id,
            "space_name": space_name,
            "space_id": space_id,
            "sweep_fault": SWEEP_FAULT,
            "sweep_k": sweep_k,
            "sweep_legs_requested": selected_leg_ids,
            "sweep_legs_verified": [leg.name for leg in verified_pool],
            "poll_until_rel_s": POLL_UNTIL_REL_S,
            "poll_interval_s": POLL_INTERVAL_S,
            "target_provider_id": DE_ID,
            "target_provider_label": TARGET_PROVIDER_LABEL,
            "source_provider_label": SOURCE_PROVIDER_LABEL,
            "source_host": SOURCE_HOST,
            "arm_live": "A4",
            "n_max": a4_gate.N_MAX_DEFAULT,
            "wall_clock_budget_s": a4_gate.WALL_CLOCK_BUDGET_S_DEFAULT,
            "netem_delay_ms": NETEM_DELAY_MS if SWEEP_FAULT == sweep.FAULT_NETEM_LAG else None,
        }

        results_path = os.path.join(recordings_dir, "a4_rq3_metrics.json")
        with open(results_path, "w") as f:
            json.dump(
                {
                    "run_params": run_params,
                    "n_cells": result["n_cells"],
                    "n_recordings": len(recordings),
                    "rq3": rq3_result,
                    "errors": [_cell_error_to_json(e) for e in result["errors"]],
                    "excluded_legs": excluded_legs,
                    "leg_errors": leg_errors,
                    "n_leg_errors": len(leg_errors),
                    "head_canary": head_canary,
                    "tail_canary": tail_canary,
                    "quarantined": quarantined,
                },
                f, indent=2,
            )
        print(f"\nresults written: {results_path}")

        return 0

    finally:
        # --- teardown-safety: ALWAYS attempted, regardless of what failed/returned above ---
        if space_id is not None:
            print(f"\n--- tearing down space {space_name!r} (space_id={space_id}) ---")
            teardown_result = provisioner.teardown_space(space_id, provision.DEFAULT_PROVIDERS)
            print(f"  teardown_result={teardown_result}")
        else:
            print(
                "\n(space_id was never assigned — provisioning itself must have failed; "
                "provision.Provisioner.provision()'s own internal partial-teardown, if any, "
                "already ran; nothing further of ours to tear down here)"
            )

        # Best-effort de-netem-clean assert -- NEVER a gate (teardown already
        # ran above regardless). Runs unconditionally even for a
        # SWEEP_FAULT=none run, as a sanity check that no residual netem
        # state lingers from a PRIOR sweep on `de`.
        try:
            clean = netem_injector.verify_clean(DE_ID)
            print(f"netem_clean_check (de, provider_key={DE_ID!r}): verify_clean={clean}")
            if not clean:
                rc, out, err = netem_injector.show(DE_ID)
                print(f"  tc qdisc show (de): rc={rc} out={out!r} err={err!r}")
        except Exception as e:  # noqa: BLE001 -- best-effort; never raise out of teardown
            print(
                f"netem_clean_check (de): inconclusive (best-effort check itself failed): "
                f"{type(e).__name__}: {e}"
            )

        orphan_report = _best_effort_orphan_check(provisioner, SPACE_NAME_PREFIX, space_id)
        print(f"orphan_check ({SPACE_NAME_PREFIX}* spaces): {orphan_report}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
