#!/usr/bin/env python3
"""Scored RQ1+RQ2 sweep driver — parameterized (env-var), envelope-bounded,
teardown-safe live driver over the locked 7-leg panel.

One driver, two regimes, selected entirely by env vars so the SAME file
serves the healthy control sweep AND every RQ2 netem fault sub-sweep:

    SWEEP_FAULT=none        -> RQ1 healthy control (no cap on cell count)
    SWEEP_FAULT=netem_lag   -> RQ2 fault sub-sweep against `de`, ENVELOPE-
                               CAPPED at 30 fault cells (len(legs) * SWEEP_K)
                               -- split a bigger ask into multiple
                               SWEEP_LEGS-scoped sub-sweep invocations.

Run (from the repo root, UNSANDBOXED -- a sandboxing TLS proxy
corrupts large PUTs and this needs the harness's own bounded transient
retry, not the sandbox's):

    cd /path/to/repo
    export PLGRID_FORGE_API_KEY=...   # each key variable named in config/arms.json

    # (a) healthy control run -- all 7 legs, K=8, no fault:
    RUN_ID=rq1-control-<date-or-slug> SWEEP_FAULT=none SWEEP_K=8 \\
        .venv/bin/python scored_sweep_driver.py

    # (b) an example netem_lag fault sub-sweep -- a <=30-cell leg x K subset
    #     (3 legs x K=8 = 24 <= 30). This driver constructs
    #     LiveFaultInjector(operator_sanctioned=True) unconditionally, so
    #     running with SWEEP_FAULT=netem_lag on live federation access IS
    #     the sanction boundary; only run this arm once the netem envelope
    #     for this invocation has been confirmed):
    RUN_ID=rq2-netem-sub1 SWEEP_FAULT=netem_lag SWEEP_K=8 \\
        SWEEP_LEGS="zai-org/GLM-4.7-Flash,claude-haiku-4-5-20251001,claude-sonnet-5" \\
        .venv/bin/python scored_sweep_driver.py

`RUN_ID` namespaces both the provisioned space's name (`icsoc_sweep_<RUN_ID>_
<uuid8>`) and the recordings directory
(`<recordings_dir>/scored_sweep/<RUN_ID>/`, with `recordings_dir` from config/federation.json), so re-runs / parallel
sub-sweeps never collide. `.venv` must already have `harness/requirements.txt`
installed (claude-agent-sdk, openai, mcp).

--- Env vars (all optional; defaults produce the full 7-leg healthy sweep) ---

    RUN_ID        namespace slug (default "scored_sweep")
    SWEEP_LEGS    comma-separated leg model_ids from the locked 7-leg panel
                  below (default: all 7, Forge-then-SDK order)
    SWEEP_K       int, trial replicate count per (leg, fault) cell (default 8)
    SWEEP_FAULT   "none" (default) | "netem_lag"

--- Replicate construction (read before trusting SWEEP_K) ---

`_run_scored_sweep` (below) builds the SWEEP_K replicates per (leg, fault)
cell by calling `sweep.enumerate_cells(...)` once for each k in 1..SWEEP_K
-- each call producing one `Cell` per (scenario, leg, fault) combination,
with `trial_k=k` stamped on that batch -- then concatenating all the
resulting Cell lists into ONE list and handing that single concatenated
list to `sweep_run.run_plan(...)` in one call. Running all K batches
through a single `run_plan` call keeps `sweep.serialization_plan`'s
same-provider-serializes policy scoped over every replicate together (not
just within one `enumerate_cells()` batch): every fault-cell replicate
targeting the same provider serializes against every other, while every
no-fault cell across all K replicates runs concurrently.

`sweep_driver.make_execute_cell`'s `execute_cell(cell)` closure calls
`run_trial_fn(...)` exactly once per Cell, so each (leg, fault) cell yields
SWEEP_K separate trials -- each a distinct `run_trial_fn` invocation with
its own uuid4 `trial_id` and its own recording file. The recorded
`"trial_k"` field inside each trial's JSON is 1 for these placement runs
(RQ1/RQ2 statistical aggregation counts matching recordings by
`(arm, leg_scaffold, scenario_id)` / by truth-label, never by the
`trial_k` field). The tail -- `aggregate.aggregate` + `rq2.confusion_matrix`
over the resulting recordings, with `arms=("A0","A1","A2","A3a","A3b")` --
mirrors `sweep_driver.run_sweep`'s own tail.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import uuid
from typing import Optional

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from harness import aggregate  # noqa: E402
from harness import fixtures  # noqa: E402
from harness import netem  # noqa: E402
from harness import panel as panel_mod  # noqa: E402
from harness import provision  # noqa: E402
from harness import rq2  # noqa: E402
from harness import runner as runner_mod  # noqa: E402
from harness import sweep  # noqa: E402
from harness import sweep_driver  # noqa: E402
from harness import sweep_run  # noqa: E402
from harness import verifier as verifier_mod  # noqa: E402
from harness import settings  # noqa: E402
from harness import arms as arms_mod  # noqa: E402

# --- scenario / infra constants -----------------------------------------------

SOURCE_HOST = settings.get("source.host")
SOURCE_PROVIDER_LABEL = settings.get("source.label")
TARGET_PROVIDER_LABEL = settings.get("target.label")

# The "de" oneprovider/onezone entity id -- the CANONICAL provider-ID
# namespace (matches state_timeline.samples[].provider, per
# runner.make_e1_form_b_scenario's own target_providers comment, and
# harness/test_sweep_driver.py's `DE = "7cfe30bef82a2c904c15624f752c58ddchd932"`
# constant). This is DELIBERATELY the same value used for BOTH the
# scenario's `target_provider_id` AND `FaultSpec.target_provider` -- see the
# long comment above `DE_NETEM_TARGET` below for why that second use is load-
# bearing (schema.py's fault cross-namespace guard) and differs from
# harness/test_netem.py's own convention of keying NetemInjector by the
# human label "de".
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

# The locked 7-leg panel. Defined here as plain literals (not
# reused from panel.STAGE1_FORGE_MODEL_IDS, which panel.py's own docstring
# warns may drift) -- these are the exact fixed ids for this study.
ARMS = arms_mod.load_arms()
ARMS_BY_MODEL_ID = {a["model_id"]: a for a in ARMS}
FORGE_MODEL_IDS = tuple(a["model_id"] for a in ARMS if a["kind"] == "openai-compatible")
SDK_MODEL_IDS = tuple(a["model_id"] for a in ARMS if a["kind"] == "anthropic-sdk")
ALL_LEG_MODEL_IDS = FORGE_MODEL_IDS + SDK_MODEL_IDS

# The sanctioned fault-cell cap (netem_lag only; healthy `none` sweeps are
# uncapped) -- per the build task's ENVELOPE ASSERT requirement.
FAULT_CELL_CAP = 30
NETEM_DELAY_MS = 35000.0  # > T_max=30s, comfortably under the 60s netem envelope cap

# Poll window: > T_max (30s) to observe (non-)convergence, < the 60s netem
# clear-verification envelope. NOT env-parameterized (only RUN_ID/SWEEP_LEGS/
# SWEEP_K/SWEEP_FAULT are, per the build task's env-var list).
POLL_UNTIL_REL_S = 45.0
POLL_INTERVAL_S = 3.0

SPACE_NAME_PREFIX = "icsoc_sweep_"

RUN_ID = os.environ.get("RUN_ID", "scored_sweep")
SWEEP_FAULT = os.environ.get("SWEEP_FAULT", "none").strip()
_SWEEP_LEGS_RAW = os.environ.get("SWEEP_LEGS", "").strip()
# Experiment switches: "placement" (default) or "deletion"; SWEEP_INFORMED=1 adds the
# contract block to the placement prompt (informed-agent variant).
SWEEP_SCENARIO = os.environ.get("SWEEP_SCENARIO", "placement").strip()
SWEEP_INFORMED = os.environ.get("SWEEP_INFORMED", "0").strip() == "1"
RECORDINGS_DIR = os.path.join(settings.get("recordings_dir", "runs"), "scored_sweep", RUN_ID) + os.sep


# --- env preconditions (Pattern D -- aggregate-fail-loud, silent-fallback-hazards.md) ---

def _parse_sweep_legs() -> list:
    """`SWEEP_LEGS` (comma-separated model_ids) -> the ordered list of legs
    this run should build. Empty/unset -> the full locked 7-leg panel,
    Forge-then-SDK order (matches FORGE_MODEL_IDS + SDK_MODEL_IDS above)."""
    if not _SWEEP_LEGS_RAW:
        return list(ALL_LEG_MODEL_IDS)
    return [s.strip() for s in _SWEEP_LEGS_RAW.split(",") if s.strip()]


def build_base_scenario(space_name: str, space_id: str, *, scenario: str = None,
                        informed: bool = None) -> dict:
    """The scenario every cell of this run uses, selected by SWEEP_SCENARIO / SWEEP_INFORMED.
    Pure: no I/O. `make test` checks it reproduces the recorded contracts and prompts."""
    scenario = SWEEP_SCENARIO if scenario is None else scenario
    informed = SWEEP_INFORMED if informed is None else informed
    if scenario == "deletion":
        return runner_mod.make_e3_scenario(
            space_name=space_name,
            space_id=space_id,
            all_provider_ids=[CLOUD_PL_ID, DE_ID],
            all_provider_labels=[SOURCE_PROVIDER_LABEL, TARGET_PROVIDER_LABEL],
            source_host=SOURCE_HOST,
        )
    return runner_mod.make_e1_form_b_scenario(
        space_name=space_name,
        space_id=space_id,
        target_provider_label=TARGET_PROVIDER_LABEL,
        target_provider_id=DE_ID,
        source_provider_label=SOURCE_PROVIDER_LABEL,
        source_host=SOURCE_HOST,
        informed=informed,
    )


async def _run_deletion_trial(scenario, leg, fixture_mgr, verifier, fault=None, **kwargs):
    """Adapter so the shared cell executor can drive deletion trials (run without a fault)."""
    if (fault or {}).get("condition", "none") != "none":
        raise ValueError(f"deletion trials run without an injected fault, got {fault!r}")
    return await runner_mod.run_e3_trial(scenario, leg, fixture_mgr, verifier, **kwargs)


def _check_env_preconditions(selected_leg_ids: list, sweep_k: int, sweep_fault: str) -> None:
    """Fail loud, aggregating ALL problems in one message, before touching
    the federation, minting any token, or provisioning a space. Bundles the
    build task's four listed preconditions (Forge key / stray Anthropic-key
    vars / unknown-leg-id / bad SWEEP_FAULT value) PLUS the ENVELOPE ASSERT
    (netem_lag fault-cell cap) into one aggregate-fail-loud check.
    """
    problems: list = []

    unknown = [m for m in selected_leg_ids if m not in ALL_LEG_MODEL_IDS]
    if unknown:
        problems.append(
            f"SWEEP_LEGS names model id(s) not in the locked 7-leg panel: {unknown!r} "
            f"(panel is {ALL_LEG_MODEL_IDS!r})."
        )

    if SWEEP_SCENARIO not in ("placement", "deletion"):
        problems.append(f"SWEEP_SCENARIO={SWEEP_SCENARIO!r} is not 'placement' or 'deletion'.")
    if SWEEP_SCENARIO == "deletion" and sweep_fault != sweep.FAULT_NONE:
        problems.append("SWEEP_SCENARIO=deletion runs without an injected fault (SWEEP_FAULT=none).")
    if SWEEP_SCENARIO == "deletion" and SWEEP_INFORMED:
        problems.append("SWEEP_INFORMED applies to the placement scenario only.")

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


# --- leg construction ----------------------------------------------------------

def _build_leg(model_id: str) -> "panel_mod.ModelLeg":
    """The leg for one arm of config/arms.json, built by `harness.arms.build_leg`.
    Membership is validated by `_check_env_preconditions` before this is called;
    the error below is a defensive backstop."""
    arm = ARMS_BY_MODEL_ID.get(model_id)
    if arm is not None:
        return arms_mod.build_leg(arm)
    raise ValueError(f"_build_leg: {model_id!r} is not in the locked 7-leg panel")


# --- netem fault-injection plumbing (constructed regardless of SWEEP_FAULT;
# only ever EXERCISED when SWEEP_FAULT=netem_lag -- a no-fault cell never
# calls injector.inject/.clear at all, per sweep_driver.make_execute_cell's
# own `if cell.fault.is_fault:` gate) ------------------------------------------

# NAMESPACE NOTE (read before changing `provider=` below):
# `FaultSpec.target_provider` ends up written verbatim into each recording's
# `fault.target_provider` field, which harness/schema.py's fault
# cross-namespace guard (the label-vs-ID class) REQUIRES to be in the
# SAME namespace as `state_timeline.samples[].provider` -- i.e. the
# oneprovider entity-ID hash (DE_ID), NOT the human label "de". That sample
# namespace comes from `runner.make_e1_form_b_scenario`'s `target_provider_id`
# (see its own comment: "target_providers is the CANONICAL provider-ID
# namespace"). So `FaultSpec(target_provider=DE_ID, ...)` below is required
# for schema validity, NOT a style choice.
#
# `netem.NetemInjector`'s OWN test convention (harness/test_netem.py's
# `DE_TARGET = netem.ProviderTarget(provider="de", ...)`) keys its `targets`
# dict + `NetemEnvelope.provider_allow` by the human label "de" instead --
# but `NetemInjector._validate()` only ever does `self._targets[fault_spec.
# target_provider]` and `fault_spec.target_provider in envelope.
# provider_allow`, both fully caller-controlled. So THIS driver keys
# `targets`/`provider_allow` by DE_ID (matching the FaultSpec/schema
# namespace) instead of "de" (matching netem.py's own tests) -- the
# kubeconfig/namespace/pod/container/iface fields below are UNCHANGED from
# harness/test_netem.py's DE_TARGET (those route the literal kubectl
# commands and don't participate in the label/ID mismatch at all).
DE_NETEM_TARGET = netem.ProviderTarget(
    provider=DE_ID,
    # ABSOLUTE path (not ~/.kube/de-config): netem.py builds `KUBECONFIG=<_q(path)>`
    # which SINGLE-QUOTES the value, so a leading ~ is NOT expanded by the remote
    # shell → kubectl falls back to localhost:8080 ("connection refused").
    # See harness/netem.py ProviderTarget.kubeconfig note.
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
    the same jump host `verifier.py`/`fixtures.py` already use for all other
    federation SSH ops (`fault.ssh_host` in config/federation.json),
    matching netem.py's own `ssh_runner(ssh_host)` factory. Pure/no I/O at
    construction time (see `NetemInjector.__init__`) -- safe to build even
    for a SWEEP_FAULT=none run."""
    runner_fn = netem.ssh_runner(settings.get("fault.ssh_host"))
    return netem.NetemInjector(
        runner=runner_fn, targets={DE_ID: DE_NETEM_TARGET}, envelope=NETEM_ENVELOPE,
    )


# --- the K-fold-replicating equivalent of sweep_driver.run_sweep --------------
# (see the module docstring's "Replicate construction" section).

def _run_scored_sweep(
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
    """K-fold-replicating sibling of `sweep_driver.run_sweep` — same
    signature shape (minus `apply_a2_fn`, unused by this driver), same
    output shape (`{recordings, errors, n_cells, aggregate, confusion}`),
    but ACTUALLY produces `trial_k` independent trial executions per
    (scenario, leg, fault) combination rather than stamping `trial_k` as an
    inert label on a single cell — see the module docstring.
    """
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
        "aggregate": aggregate.aggregate(recordings, arms=("A0", "A1", "A2", "A3a", "A3b")),
        "confusion": rq2.confusion_matrix(recordings),
    }


# --- best-effort orphan report (report-only; NEVER a gate on teardown) ---------
# The shape of `GET user/spaces`'s response body was not independently
# verified against the live API before this was written, so both plausible
# shapes ({"spaces": [...]} and a bare list) are handled below, and an
# unrecognized shape reports "inconclusive" rather than raising.

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
        f"=== scored sweep: run_id={run_id!r} space_name={space_name!r} "
        f"sweep_fault={SWEEP_FAULT!r} sweep_k={sweep_k} legs={selected_leg_ids!r} "
        f"recordings_dir={recordings_dir!r} ==="
    )

    candidate_pool = [_build_leg(m) for m in selected_leg_ids]
    print(f"candidate pool ({len(candidate_pool)} legs): {[leg.name for leg in candidate_pool]}")

    # Provisioner() + the netem injector are both constructed OUTSIDE the
    # try/finally so they're available to teardown/best-effort checks in
    # `finally` even if something inside the try raises before they'd
    # otherwise be built. Provisioner() mints its own onezone-admin token
    # immediately (no space touched yet); NetemInjector construction is pure
    # (no I/O at all -- see netem.py's __init__).
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
        base_scenario = build_base_scenario(space_name, space_id)

        def _scenario_of_cell(_cell):
            # ONE scenario (E1 Form B, cloud-pl -> de) for this whole driver
            # invocation -- every cell (regardless of leg/fault/replicate)
            # maps to the SAME base scenario dict. `run_trial` only READS
            # from `scenario` (it copies into its own `trial_scenario` before
            # mutating), so sharing this one dict object across concurrently
            # executing cells is safe.
            return base_scenario

        sweep_id = f"scored-sweep-{run_id}"
        verify_between = (
            (lambda cell: netem_injector.verify_clean(cell.perturbs_provider))
            if SWEEP_FAULT == sweep.FAULT_NETEM_LAG else None
        )
        # Non-optional on the fault path: netem must never run
        # without a real cross-cell clear-verification rider. Belt-and-suspenders
        # over the wiring above — if this ever fires, the fault-path verify_clean
        # was silently dropped and the netem-contamination safety was dormant.
        if SWEEP_FAULT == sweep.FAULT_NETEM_LAG:
            assert verify_between is not None, (
                "fault path requires a real verify_between (netem clear-rider) — refusing "
                "to run netem without cross-cell clear-verification"
            )

        execute_cell = sweep_driver.make_execute_cell(
            fixture_mgr=fixture_mgr,
            verifier=verifier,
            legs_by_name=legs_by_name,
            injector=live_injector,
            scenario_of_cell=_scenario_of_cell,
            run_trial_fn=(runner_mod.run_trial if SWEEP_SCENARIO == "placement"
                          else _run_deletion_trial),
            sweep_id=sweep_id,
            poll_until_rel_s=POLL_UNTIL_REL_S,
            poll_interval_s=POLL_INTERVAL_S,
            content_size=runner_mod.DEFAULT_CONTENT_SIZE,
            recordings_dir=recordings_dir,
        )

        # --- 5. run it (K-fold-replicating; see module docstring) ---
        print(
            f"\n--- running sweep: {len(verified_pool)} leg(s) x {sweep_k} replicate(s) x "
            f"{len(fault_specs)} fault_spec(s) = {len(verified_pool) * sweep_k * len(fault_specs)} "
            "cells ---"
        )
        result = _run_scored_sweep(
            scenarios=[base_scenario],
            legs=[leg.name for leg in verified_pool],
            fault_specs=fault_specs,
            execute_cell=execute_cell,
            trial_k=sweep_k,
            arm_live="A0",
            max_concurrency=8,
            verify_between=verify_between,
        )

        # --- 6. report ---
        print(f"\n=== sweep complete: n_cells={result['n_cells']} "
              f"recordings={len(result['recordings'])} errors={len(result['errors'])} ===")

        print("\n=== RQ1 aggregate cells with false_pass > 0 ===")
        fp_cells = [c for c in result["aggregate"]["cells"] if c.get("false_pass", 0) > 0]
        if fp_cells:
            for c in fp_cells:
                rate = c["false_pass_rate"]
                rate_str = f"{rate:.3f}" if rate is not None else "n/a"
                print(
                    f"  arm={c['arm']:5s} leg_scaffold={c['leg_scaffold']:14s} "
                    f"scenario_id={c['scenario_id']} false_pass={c['false_pass']}/{c['n_terms']} "
                    f"rate={rate_str}"
                )
        else:
            print("  none (0 false-pass cells across all scored arms)")

        conf = result["confusion"]
        print("\n=== RQ2 confusion matrix (rows=truth, cols=a3b.emit) ===")
        for truth_label, row in conf["matrix"].items():
            print(f"  truth={truth_label:8s} -> {row}")
        print(f"  accuracy_ground_truthable={conf['accuracy_ground_truthable']}")
        print(f"  wild_row (truth=neither)={conf['wild_row']}")
        print(f"  leg_errors (scorer-excluded, from rq2.confusion_matrix)={conf['leg_errors']}")

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
        # non-convergence would be indistinguishable from the behaviour under
        # test (or from netem's own effect). This TAIL canary checks the path
        # is still healthy at sweep END; if not, the run is flagged
        # `quarantined` in the JSON output (results are kept; teardown and
        # reporting continue unchanged). Runs
        # HERE (inside the `try`, after the sweep + its report, before the
        # results-JSON write) so the space is still alive for the canary
        # transfer -- NOT in `finally`, which is teardown-only. ---
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
            "arm_live": "A0",
            "netem_delay_ms": NETEM_DELAY_MS if SWEEP_FAULT == sweep.FAULT_NETEM_LAG else None,
        }

        results_path = os.path.join(recordings_dir, "sweep_results.json")
        with open(results_path, "w") as f:
            json.dump(
                {
                    "run_params": run_params,
                    "n_cells": result["n_cells"],
                    "n_recordings": len(result["recordings"]),
                    "aggregate": result["aggregate"],
                    "confusion": result["confusion"],
                    "errors": [_cell_error_to_json(e) for e in result["errors"]],
                    "excluded_legs": excluded_legs,
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
        # ran above regardless). Runs unconditionally (cheap, read-only
        # `tc qdisc show`) even for a SWEEP_FAULT=none run, as a sanity check
        # that no residual netem state lingers from a PRIOR sweep on `de`.
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
