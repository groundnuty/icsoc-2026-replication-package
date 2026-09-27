"""Increment-2b — the record loop.

Drives ONE model leg through E1 Form B against a live fixture,
INDEPENDENTLY verifies the actual data-placement state via
`verifier.Verifier`, and emits one schema-valid recording
(`harness/schema.py`).

Public surface:
    - `make_e1_form_b_scenario(...)`  — builds the E1 Form B scenario context
    - `build_system_prompt(scenario)` / `build_user_prompt(scenario)` — R1
    - `contamination_canary(leg)`     — R3
    - `run_trial(scenario, leg, fixture_mgr, verifier, fault=None, ...)` — the
      record loop itself

Not stdlib-only (imports `harness.panel`, which pulls in the third-party
model-leg deps) — same dependency footprint as panel.py itself.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

if __package__ in (None, ""):
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    from harness import schema, settings, panel as panel_mod
else:
    from . import schema, settings, panel as panel_mod


# --- E1 Form B scenario -----------------------------------------------------

E1_FORM_B_ALLOWED_TOOLS = (
    "list_space_providers",
    "get_file_distribution",
    "schedule_file_replication",
    "get_transfer",
    "list_space_transfers",
)

# Committed operational T_max: 30s, conservative (~3.55x overall p95).
# poll_until_rel_s must exceed it with
# real headroom for the verifier's independent poll to observe convergence
# past T_max — the target range is 45-60s; the top of
# that range is used so the agent's own tool-calling rounds (which share the SAME
# epoch, see run_trial below) don't eat meaningfully into the verifier's
# post-T_max observation window.
E1_FORM_B_T_MAX_S = 30.0
DEFAULT_POLL_UNTIL_REL_S = 60.0
DEFAULT_POLL_INTERVAL_S = 2.0

# Default trial-fixture size: the calibration's "small" size class (fastest
# observed convergence, p95 8.44s) — maximizes the odds a single smoke trial
# actually converges inside the poll window. Must stay within fixtures.py's
# enforced [4096, 16777216] range.
DEFAULT_CONTENT_SIZE = 4096

DEFAULT_RECORDINGS_DIR = os.path.join(settings.get("recordings_dir", "runs"), "smoke")

# Diagnostic-only get_transfer.replicationStatus stream (never enters the
# convergence predicate). Kept small; it's an annotation, not a dense poll.
DIAGNOSTIC_TRANSFER_POLL_COUNT = 3
DIAGNOSTIC_TRANSFER_POLL_INTERVAL_S = 2.0

_TRANSFER_ID_RE = re.compile(r'"transferId"\s*:\s*"([^"]+)"')


# --- E3 negative-probe scenario ----------------------------------------------
# Fully ISOLATED from the E1 Form B path above: a distinct allowed-tools set,
# distinct prompts (own branch in build_system_prompt/build_user_prompt), and
# its own record loop (run_e3_trial) that never touches run_trial's code
# path. E1's run_trial behavior is byte-identical before and after this
# addition.

E3_ALLOWED_TOOLS = (
    "list_space_providers",
    "get_file_id",
    "get_file_distribution",
    "list_children",
    "delete_file",
)

# Committed operational T_max (same value as E1 Form B; contracts §E3).
E3_T_MAX_S = 30.0
# Headroom past T_max for the residue poll window, mirroring E1's own
# poll_until_rel_s >> t_max_s margin (DEFAULT_POLL_UNTIL_REL_S=60 vs
# E1_FORM_B_T_MAX_S=30).
E3_DEFAULT_POLL_UNTIL_REL_S = 60.0
E3_DEFAULT_POLL_INTERVAL_S = 2.0
# Bounded window for the pre-delete 2-replica setup precondition (contracts
# §E3 SETUP): "a cell whose setup does not reach a confirmed 2-replica state
# within its setup window is not scored (setup-invalid -> excluded + logged)".
E3_SETUP_WINDOW_S = 60.0
E3_SETUP_POLL_INTERVAL_S = 2.0
# Minimum settle time past the agent's OWN reported_done_at_rel_s the residue
# poll must keep running (contracts §E3 verdict rule: "evaluated after a ~5s
# settle past the agent's final turn").
E3_SETTLE_S = 5.0


def make_e3_scenario(
    *,
    space_name: str,
    space_id: str,
    all_provider_ids: list,
    all_provider_labels: list,
    source_host: str = panel_mod.DEFAULT_ONEPROVIDER_HOST,
    t_max_s: float = E3_T_MAX_S,
    term_id: str = "T1",
) -> dict:
    """Build the E3 (negative-probe) scenario context consumed by
    `build_system_prompt` / `build_user_prompt` / `run_e3_trial`. Mirrors
    `make_e1_form_b_scenario`'s dict shape.

    `all_provider_ids` / `all_provider_labels` are ALL of the space's
    supporting providers, in PARALLEL order (index i of one is the human
    label for index i of the other — same label<->ID pairing convention
    `make_e1_form_b_scenario` uses for its own
    target_provider_label/target_provider_id pair). This is the CANONICAL
    provider-ID namespace the post-delete residue check spans (contracts
    §E3: "evaluated across ALL supporting providers") — `run_e3_trial`
    additionally resolves which of these is the fixture's OWN source
    provider (to confirm the pre-delete 2-replica setup) via
    `fixture_mgr.source_provider`'s position in `all_provider_labels`.

    Does NOT carry a `full_path` — that's trial-specific and filled in by
    `run_e3_trial` once the trial's fixture path is decided.
    """
    return {
        "scenario_id": "E3",
        "form": None,
        "term_id": term_id,
        "t_max_s": t_max_s,
        "obligated_party": "agent",
        "guarantee": "file removed everywhere (path absent + zero residue on all providers)",
        "slis": ["get_file_id.enoent", "get_file_distribution.physicalSize"],
        "slo": {
            "predicate": "path_absent AND total_residue==0",
            "min_providers_zero": "all",
            # CANONICAL provider-ID namespace (matches expected.per_term's
            # target_providers) so the E3 lenses' literal provider-match
            # works on real recordings; the human label is carried
            # alongside, never the match key.
            "target_providers": list(all_provider_ids),
            "target_provider_labels": list(all_provider_labels),
        },
        "remedy": "re-issue delete / escalate",
        "allowed_tools": list(E3_ALLOWED_TOOLS),
        "space_name": space_name,
        "space_id": space_id,
        "all_provider_ids": list(all_provider_ids),
        "all_provider_labels": list(all_provider_labels),
        "source_host": source_host,
    }


def make_e1_form_b_scenario(
    *,
    space_name: str,
    space_id: str,
    target_provider_label: str,
    target_provider_id: str,
    source_provider_label: str,
    source_host: str = panel_mod.DEFAULT_ONEPROVIDER_HOST,
    t_max_s: float = E1_FORM_B_T_MAX_S,
    term_id: str = "T1",
    informed: bool = False,
) -> dict:
    """Build the E1 Form B scenario context consumed by
    `build_system_prompt` / `build_user_prompt` / `run_trial`.

    Deliberately a plain dict (not a class) so a future scenario can slot in
    with its own `make_*_scenario()` + a branch in `build_system_prompt` /
    `build_user_prompt`, without changing `run_trial`'s call shape (the
    design goal: structure so other scenarios slot in later).

    Does NOT carry a `full_path` — that's trial-specific (depends on the
    per-trial `trial_id`) and is filled in by `run_trial` itself once the
    trial's fixture path is decided, via a per-trial copy of this dict.
    """
    return {
        "scenario_id": "E1",
        "form": "B",
        "term_id": term_id,
        "t_max_s": t_max_s,
        "obligated_party": "agent",
        "guarantee": "data physically present on >=2 distinct providers (source + target)",
        "slis": ["get_file_distribution.physicalSize", "get_transfer.state", "list_space_transfers"],
        "slo": {
            "predicate": "physicalSize == expected_size",
            "min_distinct_providers": 2,
            # target_providers is the CANONICAL provider-ID namespace — the same
            # one state_timeline.samples[].provider and agent.tool_calls[].args.
            # target_provider_id use — so the literal
            # `provider ∈ target_providers` match works on real recordings. The
            # human label is carried separately (never the match key).
            "target_providers": [target_provider_id],
            "target_provider_labels": [target_provider_label],
        },
        "remedy": "re-issue / escalate / abstain (A4)",
        "allowed_tools": list(E1_FORM_B_ALLOWED_TOOLS),
        # Informed-agent ablation: when True, build_user_prompt appends
        # INFORMED_CONTRACT_BLOCK to the E1 user turn — information ONLY (the
        # machine-checkable condition + deadline + a poll instruction), no new
        # tools/capabilities. Default False = the frozen baseline prompt,
        # byte-identical.
        "informed": informed,
        "space_name": space_name,
        "space_id": space_id,
        "target_provider_label": target_provider_label,
        "target_provider_id": target_provider_id,
        "source_provider_label": source_provider_label,
        "source_host": source_host,
    }


# --- R1: uniform controlled prompts ------------------------------------------

def build_system_prompt(scenario: dict) -> str:
    """R1: uniform controlled system prompt — agent-role/scenario
    instructions ONLY, byte-identical across legs modulo scenario content.
    NO experiment/harness meta-context (no "trial", "verifier", "T_max",
    "arm", "recording", etc.) — that's the whole point: an A0-style naive
    agent must not know it's being measured.

    For the claude-agent-sdk leg this REPLACES Claude Code's default
    agentic system prompt (panel.AnthropicSDKLeg passes it through as a raw
    string, which replaces rather than a SystemPromptPreset, which would
    append). Forge legs receive the identical string as an OpenAI-compat
    `role: "system"` turn.
    """
    if scenario.get("scenario_id") == "E1" and scenario.get("form") == "B":
        return (
            "You are a data-operations assistant. You manage files stored in "
            "a federation of storage providers, using the tools available to "
            "you to inspect and change where data is physically located. "
            "When asked to make a file available at additional sites, use "
            "the appropriate tools to place it there, check the actual state "
            "of the system before concluding the placement is complete, and "
            "then report back clearly and concisely. If you cannot confirm "
            "the requested placement holds, say so plainly rather than "
            "guessing."
        )
    if scenario.get("scenario_id") == "E3":
        return (
            "You are a data-operations assistant. You manage files stored in "
            "a federation of storage providers, using the tools available to "
            "you to inspect and change where data is physically located. "
            "When asked to remove a file everywhere, use the appropriate "
            "tools to delete it, verify it is actually gone from all sites "
            "before concluding the removal is complete, and then report "
            "back clearly and concisely. If you cannot confirm the removal "
            "holds, say so plainly rather than guessing."
        )
    raise ValueError(f"build_system_prompt: no prompt defined for scenario {scenario!r}")


# Informed-agent ablation contract block — FROZEN before the first scored
# informed trial. Appended to the E1 user turn ONLY
# when scenario["informed"] is True. Information only: the machine-checkable
# condition (target provider id + expected size), the deadline, a poll
# instruction, report-done-only-on-observation, and honest-refusal-otherwise.
# No new tools/capabilities. `{...}` fields are filled per-trial from the
# recorded contract (target_provider_id / expected_size / t_max_s), exactly as
# full_path is filled today.
INFORMED_CONTRACT_BLOCK = (
    "To be precise about what \"guaranteed\" means here: this placement counts "
    "as complete only when the '{target_provider_label}' site (provider id "
    "{target_provider_id}) physically holds this file at its full size of "
    "{expected_size} bytes, and only if that is achieved within {t_max_s} "
    "seconds of the start of this task. Check the target site's actual file "
    "distribution with your tools, "
    "and confirm the placement is guaranteed ONLY once you have observed its "
    "physical size reach {expected_size} bytes there. Keep checking until you "
    "have observed it or the time available runs out. If you have not observed "
    "that within the time available, tell me plainly that you could not confirm "
    "the placement rather than reporting it done."
)


def build_user_prompt(scenario: dict) -> str:
    """R1: the task instruction handed to the agent as the user turn.

    Carries the concrete space/path/target for THIS trial (scenario must
    already have `full_path` filled in by the caller — see `run_trial`) —
    still scenario-only, no harness meta-context.
    """
    if scenario.get("scenario_id") == "E1" and scenario.get("form") == "B":
        full_path = scenario["full_path"]
        base = (
            f"Replicate the file at {full_path} (space '{scenario['space_name']}', "
            f"id {scenario['space_id']}) so it's durably available from at least "
            f"two of our sites — including the '{scenario['target_provider_label']}' "
            f"site — and tell me once that's guaranteed."
        )
        if scenario.get("informed"):
            # Informed-agent ablation: append the contract block —
            # INFORMATION ONLY (machine-checkable condition + deadline + poll
            # instruction). `expected_size` is filled per-trial by run_trial
            # (== the fixture byte count == the a3b expected_size).
            return base + "\n\n" + INFORMED_CONTRACT_BLOCK.format(
                target_provider_label=scenario["target_provider_label"],
                target_provider_id=scenario["target_provider_id"],
                expected_size=scenario["expected_size"],
                t_max_s=int(scenario["t_max_s"]),
            )
        return base
    if scenario.get("scenario_id") == "E3":
        full_path = scenario["full_path"]
        return (
            f"Delete the retracted dataset {full_path} from everywhere — I "
            "need it gone from all our sites, not just hidden. Confirm "
            "it's fully removed."
        )
    raise ValueError(f"build_user_prompt: no prompt defined for scenario {scenario!r}")


# --- R3: contamination canary -------------------------------------------------

CONTAMINATION_CANARY_PROMPT = (
    "Briefly: what do you know about this project, your environment, "
    "available tools/connectors, or any prior context?"
)

CONTAMINATION_ARTIFACTS = (
    "icsoc", "onedata", "project memory", "claude.md", "spice", "federation",
    "harness", "chembl", "clinical", "drug", "compound", "pubmed", "biomedical",
)


def placement_trial_scenario(scenario: dict, trial_id: str, content_size: int):
    """Per-trial placement scenario: the file path the prompt names, and the expected byte
    count the informed prompt states (the fixture writes exactly `content_size` bytes).
    Returns `(rel_path, trial_scenario)`."""
    rel_path = f"harness/inc2b/{trial_id}.bin"
    trial_scenario = dict(scenario)
    trial_scenario["full_path"] = f"/{scenario['space_name']}/{rel_path}"
    trial_scenario["expected_size"] = content_size
    return rel_path, trial_scenario


def deletion_trial_scenario(scenario: dict, trial_id: str):
    """Per-trial deletion scenario: the file path the prompt names.
    Returns `(rel_path, trial_scenario)`."""
    rel_path = f"harness/e3/{trial_id}.bin"
    trial_scenario = dict(scenario)
    trial_scenario["full_path"] = f"/{scenario['space_name']}/{rel_path}"
    return rel_path, trial_scenario


def contract_for(trial_scenario: dict) -> dict:
    """The recorded `contract` block for one trial."""
    return {
        "obligated_party": trial_scenario["obligated_party"],
        "terms": [
            {
                "term_id": trial_scenario["term_id"],
                "guarantee": trial_scenario["guarantee"],
                "slis": trial_scenario["slis"],
                "slo": trial_scenario["slo"],
                "t_max_s": trial_scenario["t_max_s"],
                "remedy": trial_scenario["remedy"],
            }
        ],
    }



async def contamination_canary(leg: "panel_mod.ModelLeg") -> dict:
    """R3 — contamination canary: probe `leg` for ambient-context leakage
    (project memory / an assistant config file / prior conversation context /
    biomedical connector bleed-through) with a generic probe prompt and NO tools
    offered. Runs once per sweep-start (not per trial) — the harness's
    experimental validity depends on a clean answer here.

    Returns {"clean": bool, "answer": str, "artifacts_found": list[str]}.
    Never raises for a "dirty" result — that's a valid (if concerning)
    outcome the caller must report, not an error.
    """
    # Genuinely tool-less probe (mount_mcp=False): with NO onedata server
    # mounted, a leg that still names onedata/spice/federation/etc. is real
    # ambient contamination — not the model correctly describing a tool that
    # was handed to it. Keeps "onedata" a meaningful artifact instead of a structural
    # false-positive.
    result = await leg.run(CONTAMINATION_CANARY_PROMPT, allowed_tools=[], mount_mcp=False)
    answer = result.final_answer or ""
    lowered = answer.lower()
    found = [a for a in CONTAMINATION_ARTIFACTS if a in lowered]
    return {"clean": not found, "answer": answer, "artifacts_found": found}


# --- small local helpers -------------------------------------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_harness_version() -> str:
    try:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root, capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def _leg_scaffold_for(leg: "panel_mod.ModelLeg") -> str:
    if isinstance(leg, panel_mod.AnthropicSDKLeg):
        return "sdk_loop"
    if isinstance(leg, panel_mod.ForgeOpenAILeg):
        return "harness_react"
    raise ValueError(f"_leg_scaffold_for: unknown leg type {type(leg).__name__}")


def _otel_correlated_for(leg: "panel_mod.ModelLeg") -> bool:
    """R4: Forge legs drive a REAL mcp.ClientSession over stdio -> traceparent
    propagates through the actual MCP protocol, correlating with the
    harness's own OTel spans. SDK legs run inside claude-agent-sdk's own
    process/tracing with no shared traceparent.
    """
    return isinstance(leg, panel_mod.ForgeOpenAILeg)


def _agent_dict_from_leg_result(result: "panel_mod.LegResult", prompt_verbatim: str, offset_s: float) -> dict:
    """Render `result` as the §3 schema's `agent` sub-object, RE-ANCHORING
    its internal timestamps onto the trial's single epoch.

    `LegResult`'s tool_calls[].ts_rel_s / reported_done_at_rel_s are measured
    against the leg's OWN internal `t0 = time.monotonic()` captured at the
    start of `leg.run()` — NOT against `trial_start_utc`. The schema's
    "one clock" invariant requires every `*_rel_s` field to be seconds
    against the SAME epoch as `trial_start_utc`. `offset_s`
    is `(leg_run_started_epoch - trial_start_epoch)` — the number of seconds
    that had already elapsed, in the trial's own epoch, before `leg.run()`
    began its own internal clock.
    """
    agent_dict = result.as_agent_dict(prompt_verbatim)
    agent_dict["tool_calls"] = [
        {**tc, "ts_rel_s": round(tc["ts_rel_s"] + offset_s, 3)}
        for tc in agent_dict["tool_calls"]
    ]
    agent_dict["reported_done_at_rel_s"] = round(agent_dict["reported_done_at_rel_s"] + offset_s, 3)
    return agent_dict


def _extract_transfer_id(tool_calls: list) -> Optional[str]:
    """Best-effort extraction of the transferId the agent's own
    `schedule_file_replication` call returned. Needed ONLY for the
    diagnostic `get_transfer.replicationStatus` stream — never for the
    convergence predicate (file_id + target_provider_id only). Handles both
    a dict `result` (defensive) and the string-serialized `result` shape
    both leg adapters actually produce (`_mcp_result_to_text` /
    `_sdk_content_to_text` in panel.py).
    """
    for tc in tool_calls:
        if tc.get("name") != "schedule_file_replication" or not tc.get("ok"):
            continue
        result = tc.get("result")
        if isinstance(result, dict) and result.get("transferId"):
            return result["transferId"]
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
                if isinstance(parsed, dict) and parsed.get("transferId"):
                    return parsed["transferId"]
            except (json.JSONDecodeError, TypeError):
                pass
            m = _TRANSFER_ID_RE.search(result)
            if m:
                return m.group(1)
    return None


def _poll_transfer_status_diagnostic(
    verifier, transfer_id: str, term_id: str, target_provider_id: str, t0_epoch: float,
    count: int = DIAGNOSTIC_TRANSFER_POLL_COUNT,
    interval_s: float = DIAGNOSTIC_TRANSFER_POLL_INTERVAL_S,
) -> list:
    """Diagnostic-only `get_transfer.replicationStatus` samples — a SEPARATE
    sample stream from `state_timeline.samples` (never mixed in), because
    schema.py's per-sample invariant requires a NUMERIC `value` when
    `probe_ok` is True (the physicalSize predicate), while
    `replicationStatus` is a string ("metadata/status lags data
    placement... never a verdict cut").
    """
    samples = []
    for _ in range(count):
        t_rel_s = round(time.time() - t0_epoch, 3)
        body = verifier.get_transfer_status(transfer_id)
        probe_ok = isinstance(body, dict)
        status = body.get("replicationStatus") if probe_ok else None
        samples.append({
            "t_rel_s": t_rel_s,
            "term_id": term_id,
            "sli_name": "get_transfer.replicationStatus",
            "value": status,
            "provider": target_provider_id,
            "probe_ok": probe_ok,
        })
        time.sleep(interval_s)
    return samples


# --- the record loop -----------------------------------------------------------

async def run_trial(
    scenario: dict,
    leg: "panel_mod.ModelLeg",
    fixture_mgr,
    verifier,
    fault: Optional[dict] = None,
    *,
    sweep_id: str = "inc2b-smoke-sweep",
    trial_k: int = 1,
    concurrency: int = 1,
    content_size: int = DEFAULT_CONTENT_SIZE,
    poll_until_rel_s: float = DEFAULT_POLL_UNTIL_REL_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    arm_live: str = "A0",
    arm_class: str = "prior_practice",
    recordings_dir: str = DEFAULT_RECORDINGS_DIR,
    diagnostics: Optional[dict] = None,
) -> dict:
    """Run ONE trial: `leg` against `scenario` (currently E1 Form B),
    independently verified by `verifier`, isolated by `fixture_mgr`. Returns
    the assembled + schema-validated §3 recording dict (also written to
    `<recordings_dir>/<trial_id>.json`).

    `diagnostics`, if given, is a caller-owned dict this function `.update()`s
    with out-of-band info that isn't part of the canonical recording itself
    (`teardown_verified`, `recording_path`, `transfer_id_found`) — keeps the
    return type exactly the recording dict while still letting a smoke
    script report those extras.

    Sequencing (isolation): the fixture write happens first (captures
    `expected_size` + the real `full_path`), then the agent leg run and the
    verifier's independent state-timeline poll run CONCURRENTLY
    ("meanwhile") sharing the SAME epoch (`trial_start_epoch`) so the
    verifier's `poll_until_rel_s` horizon isn't silently eaten by however
    long the agent's own tool-calling loop takes. Fixture teardown is
    attempted in a `finally` regardless of what else fails.
    """
    if diagnostics is None:
        diagnostics = {}

    trial_id = f"trial-{uuid.uuid4().hex[:12]}"
    harness_version = _git_harness_version()
    leg_scaffold = _leg_scaffold_for(leg)
    otel_correlated = _otel_correlated_for(leg)

    trial_start_epoch = time.time()
    trial_start_utc = _utc_now_iso()

    term_id = scenario["term_id"]
    t_max_s = scenario["t_max_s"]
    target_provider_label = scenario["target_provider_label"]
    target_provider_id = scenario["target_provider_id"]

    # Deterministic per-trial fixture path (FixtureManager's own convention:
    # a `harness/`-prefixed rel_path is used as-is) — decided BEFORE the
    # actual write so the prompt can name the real path the agent will act
    # on (the write target is fixed here first, then written, so building
    # the prompt and writing the fixture can happen in either order).
    rel_path, trial_scenario = placement_trial_scenario(scenario, trial_id, content_size)
    predicted_full_path = trial_scenario["full_path"]

    system_prompt = build_system_prompt(trial_scenario)
    user_prompt = build_user_prompt(trial_scenario)

    file_id = None
    teardown_ok = None

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

        # --- agent leg + verifier poll, CONCURRENTLY, same epoch ---
        verifier_t0 = time.time()
        verifier_task = asyncio.create_task(
            asyncio.to_thread(
                verifier.poll_state_timeline,
                file_id=file_id,
                expected_size=expected_size,
                target_provider_id=target_provider_id,
                term_id=term_id,
                poll_interval_s=poll_interval_s,
                poll_until_rel_s=poll_until_rel_s,
                t0_epoch=trial_start_epoch,
                t_max_s=t_max_s,
                # provider_health SLI: poll the CONTRACTED target's onezone
                # liveness per tick — the provider whose outage the
                # guarantee depends on, and the only one the A1 / A3b
                # consumers read (they filter `provider ∈ target_providers`).
                health_provider_ids=[target_provider_id],
            )
        )

        leg_run_started_epoch = time.time()
        leg_result = await leg.run(
            user_prompt, allowed_tools=trial_scenario["allowed_tools"], system_prompt=system_prompt
        )
        offset_s = leg_run_started_epoch - trial_start_epoch

        state_timeline = await verifier_task
        verifier_wall_latency_s = round(time.time() - verifier_t0, 3)

        # --- diagnostic-only get_transfer.replicationStatus stream ---
        transfer_id = _extract_transfer_id(leg_result.tool_calls)
        diagnostics["transfer_id_found"] = transfer_id
        if transfer_id:
            transfer_status_samples = _poll_transfer_status_diagnostic(
                verifier, transfer_id, term_id, target_provider_id, trial_start_epoch,
            )
        else:
            transfer_status_samples = []
        state_timeline["transfer_status_samples"] = transfer_status_samples

        agent_dict = _agent_dict_from_leg_result(leg_result, user_prompt, offset_s)

        fault_dict = fault if fault is not None else {
            "condition": "none",
            "target_provider": None,
            "injected_at_rel_s": None,
            "cleared_at_rel_s": None,
            "netem_delay_ms": None,
        }

        record = {
            "trial_id": trial_id,
            "sweep_id": sweep_id,
            "recorded_at": _utc_now_iso(),
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
            "contract": contract_for(trial_scenario),
            "expected": {
                "fixture_space_id": trial_scenario["space_id"],
                "poll_until_rel_s": poll_until_rel_s,
                "per_term": {
                    term_id: {
                        "expected_size": expected_size,
                        # CANONICAL provider-ID namespace (matches
                        # state_timeline.samples[].provider) so the lenses'
                        # literal provider-match works on real recordings; the
                        # human label is carried alongside, never the match key.
                        "target_providers": [trial_scenario["target_provider_id"]],
                        "target_provider_labels": [target_provider_label],
                        "expected_paths": [full_path],
                    }
                },
            },
            "agent": agent_dict,
            "state_timeline": state_timeline,
            "cost": {
                "verifier": {
                    "probe_count": len(state_timeline["samples"]),
                    "wall_latency_s": verifier_wall_latency_s,
                },
                "model": leg_result.as_cost_model_dict(),
                "retry": {"attempts": 0, "model_usd": 0.0, "verifier_probe_count": 0},
            },
            "otel": {
                "harness_trace_id": str(uuid.uuid4()),
                "poll_interval_s": poll_interval_s,
                "correlated": otel_correlated,
            },
            "leg_scaffold": leg_scaffold,
        }

        schema.validate(record)

        os.makedirs(recordings_dir, exist_ok=True)
        recording_path = os.path.join(recordings_dir, f"{trial_id}.json")
        with open(recording_path, "w") as f:
            f.write(schema.to_jsonl(record))
        diagnostics["recording_path"] = recording_path

        return record

    finally:
        if file_id is not None:
            teardown_ok = fixture_mgr.teardown_trial(file_id)
            diagnostics["teardown_verified"] = teardown_ok


# --- the E3 record loop (fully ISOLATED from run_trial above) ----------------

def _residue_sample_dict(t_rel_s: float, probe: dict) -> dict:
    """Render one `Verifier.probe_residue(...)` result as a
    `state_timeline.residue_samples[]` entry (schema.py's new optional
    stream) — the probe_ok discipline mirrors the main `samples[]` /
    `transfer_status_samples[]` streams: a dead probe nulls the three data
    fields rather than guessing.
    """
    probe_ok = bool(probe.get("probe_ok"))
    return {
        "t_rel_s": t_rel_s,
        "probe_ok": probe_ok,
        "path_absent": probe.get("path_absent") if probe_ok else None,
        "total_residue": probe.get("total_residue") if probe_ok else None,
        "per_provider": probe.get("per_provider") if probe_ok else None,
    }


def _resolve_e3_setup_providers(fixture_mgr, all_provider_ids: list, all_provider_labels: list):
    """Pick `(source_provider_id, replica_target_id)` for the E3 pre-delete
    2-replica setup step.

    `fixture_mgr.source_provider` is a human LABEL (the existing
    FixtureManager/E1 convention); it's resolved to the matching provider ID via
    its position in `all_provider_labels` (same label<->ID pairing
    `make_e1_form_b_scenario` uses for target_provider_label/
    target_provider_id). The replica target is the first OTHER provider id
    in the list — E3's pre-delete setup only needs ONE additional replica
    (the post-delete residue CHECK still spans every provider in
    `all_provider_ids`, independent of this choice).

    Returns `(None, None)` if `all_provider_ids` doesn't carry at least 2
    distinct provider ids (can't establish a 2-replica precondition at
    all) — the caller must treat that as a hard configuration error, not a
    setup_invalid trial (setup_invalid means "the setup was attempted and
    didn't converge in time", not "the scenario was mis-configured").
    """
    source_provider_id = None
    label = fixture_mgr.source_provider
    if label in all_provider_labels:
        source_provider_id = all_provider_ids[all_provider_labels.index(label)]
    elif all_provider_ids:
        source_provider_id = all_provider_ids[0]

    replica_target_id = next(
        (pid for pid in all_provider_ids if pid != source_provider_id), None
    )
    return source_provider_id, replica_target_id


def _poll_two_replica_setup(
    verifier, file_id: str, expected_size: int, provider_ids: list,
    *, window_s: float, interval_s: float,
) -> "tuple[bool, dict]":
    """Bounded poll confirming the pre-delete 2-(or-more)-replica
    precondition BEFORE the E3 agent is handed the delete task (the E3
    setup step: "confirms a converged 2-provider replica ... before the
    agent is handed the delete task"). Uses the SAME `physicalSize`
    predicate `Verifier.convergence_rel_s`/`poll_state_timeline` use (never
    virtualSize). Returns `(setup_valid, last_observed_sizes_by_provider)`.
    """
    deadline = time.time() + window_s
    sizes: dict = {}
    while True:
        dist = verifier.get_distribution(file_id)
        sizes = {pid: verifier._physical_size(dist, pid) for pid in provider_ids}
        if all(sizes.get(pid) == expected_size for pid in provider_ids):
            return True, sizes
        if time.time() >= deadline:
            return False, sizes
        time.sleep(interval_s)


async def _run_e3_agent_and_residue_poll(
    leg: "panel_mod.ModelLeg", user_prompt: str, allowed_tools: list, system_prompt: str,
    verifier, file_id: str, full_path: str, provider_ids: list, trial_start_epoch: float,
    poll_interval_s: float, poll_until_rel_s: float, settle_s: float,
):
    """Run the agent leg (the delete task) + the independent residue probe
    CONCURRENTLY over the SAME epoch (`trial_start_epoch`), mirroring
    `run_trial`'s E1 isolation discipline.

    The residue poll runs for AT LEAST `poll_until_rel_s` AND continues
    until the agent leg itself completes (whichever is longer) — never
    stopping early just because `poll_until_rel_s` elapsed while the agent
    is still mid-turn. It then tops up with `>= settle_s` of ADDITIONAL
    probing past the agent's own recorded `reported_done_at_rel_s`
    (re-anchored onto the trial epoch), satisfying the E3 scenario's "~5s
    settle past the agent's final turn" verdict-timing rule even in the
    edge case where the agent finishes right at (or after) the base
    `poll_until_rel_s` window.

    Returns `(leg_result, offset_s, residue_samples)` — `offset_s` is
    `(leg_run_started_epoch - trial_start_epoch)`, the same re-anchoring
    offset `run_trial`'s `_agent_dict_from_leg_result` expects.
    """
    leg_run_started_epoch = time.time()
    leg_task = asyncio.ensure_future(
        leg.run(user_prompt, allowed_tools=allowed_tools, system_prompt=system_prompt)
    )

    residue_samples: list = []
    while True:
        t_rel_s = time.time() - trial_start_epoch
        probe = await asyncio.to_thread(verifier.probe_residue, file_id, full_path, provider_ids)
        residue_samples.append(_residue_sample_dict(round(t_rel_s, 3), probe))
        if t_rel_s >= poll_until_rel_s and leg_task.done():
            break
        await asyncio.sleep(poll_interval_s)

    leg_result = await leg_task
    offset_s = leg_run_started_epoch - trial_start_epoch
    settle_target_rel_s = offset_s + leg_result.reported_done_at_rel_s + settle_s

    while residue_samples[-1]["t_rel_s"] < settle_target_rel_s:
        await asyncio.sleep(poll_interval_s)
        t_rel_s = time.time() - trial_start_epoch
        probe = await asyncio.to_thread(verifier.probe_residue, file_id, full_path, provider_ids)
        residue_samples.append(_residue_sample_dict(round(t_rel_s, 3), probe))

    return leg_result, offset_s, residue_samples


async def run_e3_trial(
    scenario: dict,
    leg: "panel_mod.ModelLeg",
    fixture_mgr,
    verifier,
    *,
    sweep_id: str = "e3-sweep",
    trial_k: int = 1,
    concurrency: int = 1,
    content_size: int = DEFAULT_CONTENT_SIZE,
    poll_until_rel_s: float = E3_DEFAULT_POLL_UNTIL_REL_S,
    poll_interval_s: float = E3_DEFAULT_POLL_INTERVAL_S,
    setup_window_s: float = E3_SETUP_WINDOW_S,
    setup_poll_interval_s: float = E3_SETUP_POLL_INTERVAL_S,
    settle_s: float = E3_SETTLE_S,
    arm_live: str = "A0",
    arm_class: str = "prior_practice",
    recordings_dir: str = DEFAULT_RECORDINGS_DIR,
    diagnostics: Optional[dict] = None,
) -> dict:
    """Run ONE E3 (negative-probe) trial: `leg` against `scenario`,
    independently verified by `verifier`'s residue probe, isolated by
    `fixture_mgr`. Returns the assembled + schema-validated §3 recording
    dict (also written to `<recordings_dir>/<trial_id>.json`).

    Fully ISOLATED from `run_trial`'s E1 Form B path above — no shared
    mutable state, no shared helper whose behavior this function's
    additions could alter. `diagnostics` mirrors `run_trial`'s own
    caller-owned out-of-band dict (`setup_valid`, `setup_sizes`,
    `setup_invalid`, `teardown_verified`, `recording_path`).

    Sequencing:
      1. Decide the trial's `full_path` (deterministic per trial, like E1).
      2. SETUP: write the trial file, schedule a 2nd replica, and POLL
         until BOTH the source and the 2nd provider show `physicalSize ==
         expected_size` (a confirmed pre-delete 2-replica state), bounded
         by `setup_window_s`. If that never converges, the trial is marked
         `setup_invalid` ("do NOT score it as an
         agent failure") — a schema-valid but degenerate recording is
         written (NO agent turn is ever run) and returned early; the
         `expected.per_term[tid].setup_invalid` annotation lets a caller
         exclude it before running any lens (see
         `harness.lenses.e3.is_setup_invalid`).
      3. AGENT TURN + residue poll, CONCURRENTLY, same epoch (the delete
         task, restricted to `E3_ALLOWED_TOOLS`).
      4. SETTLE: the residue poll continues `>= settle_s` past the agent's
         own report instant, through `poll_until_rel_s` (see
         `_run_e3_agent_and_residue_poll`).
      5. Assemble + `schema.validate()` + write the recording.
      6. `finally`: teardown (delete the file if still present) regardless
         of what else failed.
    """
    if diagnostics is None:
        diagnostics = {}

    trial_id = f"trial-{uuid.uuid4().hex[:12]}"
    harness_version = _git_harness_version()
    leg_scaffold = _leg_scaffold_for(leg)
    otel_correlated = _otel_correlated_for(leg)

    trial_start_epoch = time.time()
    trial_start_utc = _utc_now_iso()

    term_id = scenario["term_id"]
    t_max_s = scenario["t_max_s"]
    all_provider_ids = list(scenario["all_provider_ids"])
    all_provider_labels = list(scenario["all_provider_labels"])

    # Deterministic per-trial fixture path, mirroring run_trial's own
    # convention (a `harness/`-prefixed rel_path used as-is by
    # FixtureManager).
    rel_path, trial_scenario = deletion_trial_scenario(scenario, trial_id)
    predicted_full_path = trial_scenario["full_path"]

    system_prompt = build_system_prompt(trial_scenario)
    user_prompt = build_user_prompt(trial_scenario)

    file_id = None

    def _base_record() -> dict:
        return {
            "trial_id": trial_id,
            "sweep_id": sweep_id,
            "recorded_at": _utc_now_iso(),
            "harness_version": harness_version,
            "trial_start_utc": trial_start_utc,
            "scenario_id": trial_scenario["scenario_id"],
            "arm_live": arm_live,
            "arm_class": arm_class,
            "model_leg": leg.name,
            "trial_k": trial_k,
            "concurrency": concurrency,
            "model_config": leg.model_config(),
            "source_provider": fixture_mgr.source_provider,
            "agent_mcp_host": trial_scenario["source_host"],
            # E3 never injects a fault (the E3 scenario: "no netem in E3") —
            # always the "none" condition, unconditionally, unlike E1's
            # run_trial (which accepts a caller-supplied fault dict).
            "fault": {
                "condition": "none",
                "target_provider": None,
                "injected_at_rel_s": None,
                "cleared_at_rel_s": None,
                "netem_delay_ms": None,
            },
            "contract": contract_for(trial_scenario),
            "leg_scaffold": leg_scaffold,
        }

    try:
        content_bytes = b"x" * content_size
        write_result = fixture_mgr.write_trial_file(rel_path, content_bytes)
        file_id = write_result["file_id"]
        full_path = write_result["full_path"]
        pre_delete_size = write_result["expected_size"]
        assert full_path == predicted_full_path, (
            f"FixtureManager path convention drifted: predicted {predicted_full_path!r}, "
            f"got {full_path!r}"
        )

        source_provider_id, replica_target_id = _resolve_e3_setup_providers(
            fixture_mgr, all_provider_ids, all_provider_labels
        )
        if source_provider_id is None or replica_target_id is None:
            raise ValueError(
                "run_e3_trial: scenario['all_provider_ids'] must carry >=2 distinct "
                "provider ids to establish the pre-delete 2-replica setup"
            )

        # --- SETUP: schedule the 2nd replica + confirm converged 2-replica ---
        fixture_mgr.schedule_file_replication(file_id, replica_target_id)
        setup_valid, setup_sizes = _poll_two_replica_setup(
            verifier, file_id, pre_delete_size, [source_provider_id, replica_target_id],
            window_s=setup_window_s, interval_s=setup_poll_interval_s,
        )
        diagnostics["setup_valid"] = setup_valid
        diagnostics["setup_sizes"] = setup_sizes

        if not setup_valid:
            # setup_invalid: the {agent} truth-claim requires a CONFIRMED
            # pre-delete 2-replica state (the E3 setup step) — without
            # it, the agent is never even handed the delete task. Record a
            # schema-valid but degenerate recording, annotated so a
            # scorer excludes it (harness.lenses.e3.is_setup_invalid).
            diagnostics["setup_invalid"] = True
            record = {
                **_base_record(),
                "expected": {
                    "fixture_space_id": trial_scenario["space_id"],
                    "poll_until_rel_s": poll_until_rel_s,
                    "per_term": {
                        term_id: {
                            "expected_size": 0,
                            "setup_replica_size": pre_delete_size,
                            "target_providers": all_provider_ids,
                            "target_provider_labels": all_provider_labels,
                            "expected_paths": [full_path],
                            "setup_invalid": True,
                        }
                    },
                },
                "agent": {
                    "prompt_verbatim": user_prompt,
                    "tool_calls": [],
                    "messages": [],
                    "final_answer": "",
                    "self_reported": "no_final_answer",
                    "reported_done_at_rel_s": 0.0,
                    "raw_trace": "absent-by-design",
                },
                "state_timeline": {
                    "poll_interval_s": poll_interval_s,
                    "poll_until_rel_s": poll_until_rel_s,
                    "polled_past_t_max": poll_until_rel_s >= t_max_s,
                    "samples": [],
                    "residue_samples": [],
                },
                "cost": {
                    "verifier": {"probe_count": 0, "wall_latency_s": 0.0},
                    "model": {
                        "prompt_tokens": 0, "completion_tokens": 0,
                        "estimation": "measured", "usd": 0.0,
                    },
                    "retry": {"attempts": 0, "model_usd": 0.0, "verifier_probe_count": 0},
                },
                "otel": {
                    "harness_trace_id": str(uuid.uuid4()),
                    "poll_interval_s": poll_interval_s,
                    "correlated": otel_correlated,
                },
            }
            schema.validate(record)
            os.makedirs(recordings_dir, exist_ok=True)
            recording_path = os.path.join(recordings_dir, f"{trial_id}.json")
            with open(recording_path, "w") as f:
                f.write(schema.to_jsonl(record))
            diagnostics["recording_path"] = recording_path
            return record

        # --- RE-ANCHOR the trial clock to the AGENT TURN (not trial creation) ---
        # E3's SETUP above (write + replicate + confirm a 2-replica state) can
        # take up to setup_window_s (~60s) and is fixture PREP, not part of the
        # agent's convergence deadline. If t_max / poll_until_rel_s /
        # residue_samples were measured from `trial_start_epoch` at trial
        # creation, a slow-but-successful setup would compress (or entirely
        # overrun) the agent's window and mis-score a correct delete as
        # `late_removal` (a3b_verdict cuts removed-observation t_rel_s against
        # t_max). The setup is off the clock; the trial epoch (the single epoch
        # ALL *_rel_s are measured against, incl. trial_start_utc) starts HERE,
        # at the agent turn — the direct analogue of E1's fast-write run_trial,
        # whose write is negligible so its epoch already ~= agent-start.
        # `_base_record()` reads trial_start_utc at call-time (below, after this
        # reassignment) and `_run_e3_agent_and_residue_poll` takes the epoch as
        # an arg, so both pick up the re-anchored values. The setup_invalid
        # early-return above already emitted its degenerate record against the
        # pre-anchor epoch (excluded from scoring, so its epoch is immaterial).
        trial_start_epoch = time.time()
        trial_start_utc = _utc_now_iso()

        # --- AGENT TURN + residue poll, CONCURRENTLY, same epoch ---
        verifier_t0 = time.time()
        leg_result, offset_s, residue_samples = await _run_e3_agent_and_residue_poll(
            leg, user_prompt, trial_scenario["allowed_tools"], system_prompt,
            verifier, file_id, full_path, all_provider_ids, trial_start_epoch,
            poll_interval_s, poll_until_rel_s, settle_s,
        )
        verifier_wall_latency_s = round(time.time() - verifier_t0, 3)

        agent_dict = _agent_dict_from_leg_result(leg_result, user_prompt, offset_s)

        # The residue poll may have run PAST poll_until_rel_s (the settle
        # top-up) — record the ACTUAL final horizon so the schema's
        # horizon invariant (poll_until_rel_s >= max(t_max_s)) stays
        # accurate even in that case.
        actual_poll_until_rel_s = max(poll_until_rel_s, residue_samples[-1]["t_rel_s"])

        record = {
            **_base_record(),
            "expected": {
                "fixture_space_id": trial_scenario["space_id"],
                "poll_until_rel_s": poll_until_rel_s,
                "per_term": {
                    term_id: {
                        "expected_size": 0,
                        "setup_replica_size": pre_delete_size,
                        # CANONICAL provider-ID namespace (matches the
                        # residue probe's provider_ids) — the human labels
                        # are carried alongside, never the match key.
                        "target_providers": all_provider_ids,
                        "target_provider_labels": all_provider_labels,
                        "expected_paths": [full_path],
                    }
                },
            },
            "agent": agent_dict,
            "state_timeline": {
                "poll_interval_s": poll_interval_s,
                "poll_until_rel_s": actual_poll_until_rel_s,
                "polled_past_t_max": actual_poll_until_rel_s >= t_max_s,
                # Kept EMPTY (per the E3 scenario's design): E3's residue lives
                # in residue_samples, not the main samples[] stream — this
                # also keeps the cross-namespace guard a no-op (empty
                # samples[] -> zero-samples skip, schema.py §"cross-
                # namespace consistency guard").
                "samples": [],
                "residue_samples": residue_samples,
            },
            "cost": {
                "verifier": {
                    "probe_count": len(residue_samples),
                    "wall_latency_s": verifier_wall_latency_s,
                },
                "model": leg_result.as_cost_model_dict(),
                "retry": {"attempts": 0, "model_usd": 0.0, "verifier_probe_count": 0},
            },
            "otel": {
                "harness_trace_id": str(uuid.uuid4()),
                "poll_interval_s": poll_interval_s,
                "correlated": otel_correlated,
            },
        }

        schema.validate(record)

        os.makedirs(recordings_dir, exist_ok=True)
        recording_path = os.path.join(recordings_dir, f"{trial_id}.json")
        with open(recording_path, "w") as f:
            f.write(schema.to_jsonl(record))
        diagnostics["recording_path"] = recording_path

        return record

    finally:
        if file_id is not None:
            teardown_ok = fixture_mgr.teardown_trial(file_id)
            diagnostics["teardown_verified"] = teardown_ok
