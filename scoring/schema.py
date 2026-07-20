"""The per-trial recording schema — single source of truth (docs/phase1-harness-architecture.md §3).

Stdlib-only (dataclasses; no pydantic) so this runs under plain `python3`,
matching the calibration driver's dependency footprint.

Public surface:
    - dataclasses mirroring the §3 record shape (nested `{...}` placeholders
      in §3 are modeled as permissive dict/Any fields per the increment-1
      instruction, but the named invariants below are still validated).
    - validate(record: dict) -> None            (raises SchemaError)
    - to_jsonl(record: dict) -> str
    - from_jsonl(line: str) -> dict

No top-level side effects on import.
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any, Optional


class SchemaError(ValueError):
    """Raised by validate() when a recording violates a §3 schema invariant."""


# --- required top-level keys, verbatim per §3 "Notes / invariants" ---------
REQUIRED_TOP_LEVEL_KEYS = (
    "trial_id",
    "sweep_id",
    "recorded_at",
    "harness_version",
    "trial_start_utc",
    "scenario_id",
    "arm_live",
    "model_leg",
    "trial_k",
    "source_provider",
    "agent_mcp_host",
    "fault",
    "contract",
    "expected",
    "agent",
    "state_timeline",
    "cost",
    "otel",
    "leg_scaffold",
)

ARM_LIVE_VALUES = ("A0", "A4")
ARM_CLASS_VALUES = ("prior_practice", "contribution")
# Increment-2b R4: which execution scaffold produced the agent trace.
# "harness_react" = hosted-endpoint legs (this harness's own tool-call loop
# over the deployment's MCP stdio tool server via the `mcp` client SDK). "sdk_loop" =
# claude-agent-sdk legs (the SDK's own agentic loop; raw_trace is
# "absent-by-design" for these — the harness record is canonical).
LEG_SCAFFOLD_VALUES = ("harness_react", "sdk_loop")

# Increment-4 (sweep): per-SLI value typing for the main `state_timeline.samples[]`
# stream. Two SLIs live here (lens-spec §1): the placement predicate
# (`physicalSize`, numeric — the ONLY stream a verdict cuts on) and a per-provider
# liveness stream (`provider_health`, boolean — so A1 catches a pod-kill without a
# strawman, lens-spec §1a). A sample's `value` type is keyed on its `sli_name`
# when `probe_ok` is True; `probe_ok` False always means `value` is null regardless
# of SLI. Unknown SLI names are forward-compatible (a new per-provider diagnostic
# stream requires only a non-null value, not a schema bump) — only the known SLIs
# below are strictly typed.
#
# NB replicationStatus is NOT a main-stream SLI: it is transfer-level (per
# transfer, from get_transfer), a different granularity than the per-provider
# per-poll samples[] shape, so per merged lens-spec §1 it lives in the separate
# `state_timeline.transfer_status_samples` array (diagnostic-only, the 3rd
# status-lags-placement instance, contracts §1) — validated below, never a
# verdict cut.
SLI_VALUE_TYPES = {
    "physicalSize": "number",
    "provider_health": "bool",
}

# Increment: a2_result (optional top-level field) — the A2 LLM-judge verdict
# vocabulary, kept as a local constant (not imported from harness.lenses.a2)
# so schema.py stays dependency-free of the lenses package, matching the
# existing "schema has no lens imports" convention (see
# TestValidatedIngest.setUp in test_schema.py, which lazily imports
# aggregate specifically so a schema-only test run doesn't require it).
A2_VERDICT_VALUES = ("Fulfilled", "Violated", "NotDetermined")

# Increment: a4_result (optional top-level field) — the A4 live-gate trial
# outcome vocabulary (docs/phase1-a4-enforcement.md), kept as local constants
# for the same "schema has no lens/gate imports" reason A2_VERDICT_VALUES is
# local rather than imported from harness.a4_gate / harness.rq2.
A4_VERDICT_VALUES = ("Fulfilled", "Violated", "NotDetermined")
A4_ATTRIBUTION_VALUES = ("agent", "service", "both", "neither")


# --- dataclasses -------------------------------------------------------------
# Nested `{...}` placeholders from §3 are kept permissive (dict/Any) per the
# increment-1 instruction; validate() enforces the named invariants directly
# on the dict form regardless of whether callers build records via these
# dataclasses or hand-roll plain dicts.

@dataclasses.dataclass
class ToolCall:
    name: str
    args: dict
    result: Any
    ts_rel_s: float
    ok: bool


@dataclasses.dataclass
class Agent:
    prompt_verbatim: str
    tool_calls: list
    messages: list
    final_answer: str
    self_reported: str
    reported_done_at_rel_s: float
    raw_trace: Any = "absent-by-design"  # sdk legs: literal string; else an object


@dataclasses.dataclass
class Fault:
    condition: str = "none"
    target_provider: Optional[str] = None
    injected_at_rel_s: Optional[float] = None
    cleared_at_rel_s: Optional[float] = None
    netem_delay_ms: Optional[float] = None
    # 2026-07-06 revision (Refs #3, science-approved binding condition 1):
    # per-cell netem INJECT-VERIFICATION. When condition != "none" the fault
    # block must carry evidence the qdisc actually carried netem during the
    # trial window, not just the injector's add-rc=0 claim (Pattern A at the
    # injection boundary, silent-fallback-hazards.md). `inject_verified` is
    # the boolean outcome of a read-only `tc qdisc show` re-check;
    # `qdisc_snapshot` is the literal show output line (provenance, not just
    # a bool). Absent/None for condition == "none" (no injection to verify).
    inject_verified: Optional[bool] = None
    qdisc_snapshot: Optional[str] = None


@dataclasses.dataclass
class ContractTerm:
    term_id: str
    guarantee: str
    slis: list
    slo: dict
    t_max_s: float
    remedy: str = ""


@dataclasses.dataclass
class Contract:
    obligated_party: str
    terms: list  # list[ContractTerm] or list[dict]


@dataclasses.dataclass
class ExpectedPerTerm:
    # Placement terms carry expected_size (first-class, write-time-from-source).
    # Metadata terms carry expected_metadata instead. A term entry must carry
    # at least one of the two (judgment call — see harness/_smoke_increment1.py
    # notes / report to the design-lead).
    expected_size: Optional[int] = None
    target_providers: Optional[list] = None
    expected_paths: Optional[list] = None
    expected_metadata: Optional[dict] = None


@dataclasses.dataclass
class Expected:
    fixture_space_id: str
    poll_until_rel_s: float
    per_term: dict  # term_id -> ExpectedPerTerm|dict


@dataclasses.dataclass
class StateSample:
    t_rel_s: float
    term_id: str
    sli_name: str
    value: Any
    provider: str


@dataclasses.dataclass
class StateTimeline:
    poll_interval_s: float
    poll_until_rel_s: float
    polled_past_t_max: bool
    samples: list  # list[StateSample|dict]


@dataclasses.dataclass
class VerifierCost:
    probe_count: int = 0
    wall_latency_s: float = 0.0


@dataclasses.dataclass
class ModelCost:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimation: str = "measured"  # "measured" | "estimated_sdk_x_listprice"
    usd: float = 0.0


@dataclasses.dataclass
class RetryCost:
    attempts: int = 0
    model_usd: float = 0.0
    verifier_probe_count: int = 0


@dataclasses.dataclass
class Cost:
    verifier: VerifierCost
    model: ModelCost
    retry: RetryCost


@dataclasses.dataclass
class Otel:
    harness_trace_id: str
    poll_interval_s: float
    # Increment-2b R4: does the harness trace CORRELATE with the model-leg's
    # own OTel spans via traceparent propagation? True for hosted-endpoint legs (raw
    # mcp.ClientSession -> traceparent propagates through the real stdio MCP
    # protocol); False for SDK legs (claude-agent-sdk owns its own process +
    # tracing; no shared traceparent with the harness root span).
    correlated: bool = False


@dataclasses.dataclass
class ModelConfig:
    model_id: str
    temperature: float = 1.0
    max_tokens: int = 4096
    sampling: Optional[dict] = None
    config_ref: Optional[str] = None


@dataclasses.dataclass
class TrialRecord:
    trial_id: str
    sweep_id: str
    recorded_at: str
    harness_version: str
    trial_start_utc: str
    scenario_id: str
    arm_live: str
    model_leg: str
    trial_k: int
    source_provider: str
    agent_mcp_host: str
    fault: Any
    contract: Any
    expected: Any
    agent: Any
    state_timeline: Any
    cost: Any
    otel: Any
    leg_scaffold: str
    # Optional fields shown in the §3 example but NOT in the explicit
    # required-top-level-keys list; kept optional here, dropped from
    # as_dict() output when unset.
    concurrency: Optional[int] = None
    model_config: Optional[Any] = None
    arm_class: Optional[str] = None
    # The A2 LLM-judge is the one non-re-derivable lens (a live model call) —
    # its verdict is RECORDED here at record-time and read (never recomputed)
    # by aggregate.py's a2_recorded_verdict(). Optional: absent for any
    # recording not run through the A2 judge.
    a2_result: Optional[Any] = None
    # A4 is the one LIVE verification arm (docs/phase1-a4-enforcement.md) —
    # its attempt/verify/remedy gate-loop outcome is RECORDED here at trial
    # time, mirroring a2_result's non-re-derivable-lens discipline above.
    # Optional: absent for any recording not run through the A4 gate.
    a4_result: Optional[Any] = None

    def as_dict(self) -> dict:
        """Render to a plain JSON-able dict, dropping unset optional fields."""
        return _strip_none(dataclasses.asdict(self))


def _strip_none(obj):
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none(v) for v in obj]
    return obj


# --- validation helpers -------------------------------------------------------

def _is_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SchemaError(msg)


def is_leg_error(record: dict) -> bool:
    """True when `record` carries a leg-level failure (adapter/API error —
    e.g. a hosted model that returns an API error),
    NOT an agent-competence signal. Leg-error trials are NO-DATA: scorers
    (aggregate.py, rq2.py, panel_score.py) must exclude them from
    competence/attribution scoring and tally them separately rather than
    treating them as competence-0.
    """
    return (record.get("agent") or {}).get("error") is not None


def validate(record: dict) -> None:
    """Validate a trial recording against the §3 schema + invariants.

    Raises SchemaError with a precise message on the first violation found.
    """
    _require(isinstance(record, dict), "record must be a dict")

    missing = [k for k in REQUIRED_TOP_LEVEL_KEYS if k not in record]
    _require(not missing, f"missing required top-level keys: {missing}")

    # --- single epoch: trial_start_utc ---
    _require(
        isinstance(record["trial_start_utc"], str) and record["trial_start_utc"],
        "trial_start_utc must be a non-empty string (the single epoch all *_rel_s fields are measured against)",
    )

    # --- arm_live / arm_class ---
    _require(
        record["arm_live"] in ARM_LIVE_VALUES,
        f"arm_live must be one of {ARM_LIVE_VALUES}, got {record['arm_live']!r}",
    )
    if record.get("arm_class") is not None:
        _require(
            record["arm_class"] in ARM_CLASS_VALUES,
            f"arm_class must be one of {ARM_CLASS_VALUES}, got {record['arm_class']!r}",
        )

    # --- leg_scaffold (increment-2b R4) ---
    _require(
        record["leg_scaffold"] in LEG_SCAFFOLD_VALUES,
        f"leg_scaffold must be one of {LEG_SCAFFOLD_VALUES}, got {record['leg_scaffold']!r}",
    )

    # --- contract.terms ---
    contract = record["contract"]
    _require(isinstance(contract, dict), "contract must be a dict")
    terms = contract.get("terms")
    _require(isinstance(terms, list) and len(terms) > 0, "contract.terms must be a non-empty list")

    term_ids = []
    t_max_values = []
    for i, term in enumerate(terms):
        _require(isinstance(term, dict), f"contract.terms[{i}] must be a dict")
        _require(
            "term_id" in term and isinstance(term["term_id"], str) and term["term_id"],
            f"contract.terms[{i}].term_id must be present (non-empty string)",
        )
        _require(
            "t_max_s" in term and _is_number(term["t_max_s"]),
            f"contract.terms[{i}].t_max_s must be present and numeric",
        )
        term_ids.append(term["term_id"])
        t_max_values.append(term["t_max_s"])

    max_t_max = max(t_max_values) if t_max_values else None

    # --- expected ---
    expected = record["expected"]
    _require(isinstance(expected, dict), "expected must be a dict")
    per_term = expected.get("per_term")
    _require(isinstance(per_term, dict), "expected.per_term must be a dict")

    for term_id in term_ids:
        _require(term_id in per_term, f"expected.per_term is missing an entry for term_id={term_id!r}")
        entry = per_term[term_id]
        _require(isinstance(entry, dict), f"expected.per_term[{term_id!r}] must be a dict")
        has_size = "expected_size" in entry and entry["expected_size"] is not None
        has_metadata = "expected_metadata" in entry and entry["expected_metadata"] is not None
        _require(
            has_size or has_metadata,
            f"expected.per_term[{term_id!r}] must carry expected_size (placement term) "
            "or expected_metadata (metadata term)",
        )
        if has_size:
            _require(
                _is_number(entry["expected_size"]) and entry["expected_size"] >= 0,
                f"expected.per_term[{term_id!r}].expected_size must be a non-negative number "
                "(first-class — the convergence predicate keys on it)",
            )

    expected_poll_until = expected.get("poll_until_rel_s")
    _require(
        _is_number(expected_poll_until),
        "expected.poll_until_rel_s must be a number",
    )

    # --- agent ---
    agent = record["agent"]
    _require(isinstance(agent, dict), "agent must be a dict")

    tool_calls = agent.get("tool_calls", [])
    _require(isinstance(tool_calls, list), "agent.tool_calls must be a list")
    for i, tc in enumerate(tool_calls):
        _require(isinstance(tc, dict), f"agent.tool_calls[{i}] must be a dict")
        _require(
            "ts_rel_s" in tc and _is_number(tc["ts_rel_s"]),
            f"agent.tool_calls[{i}].ts_rel_s must be present and numeric (seconds vs trial_start_utc)",
        )

    _require(
        "reported_done_at_rel_s" in agent and _is_number(agent["reported_done_at_rel_s"]),
        "agent.reported_done_at_rel_s must be present and numeric (seconds vs trial_start_utc)",
    )

    raw_trace = agent.get("raw_trace")
    _require(
        raw_trace == "absent-by-design" or isinstance(raw_trace, dict),
        'agent.raw_trace must be the literal string "absent-by-design" or a dict',
    )

    # --- agent.error (optional; leg-level failure detail — see is_leg_error) --
    # Absent or None means the leg ran without an adapter/API-level failure.
    # When present and non-None, it must be a string (the failure detail);
    # scorers key on is_leg_error(record) to exclude these trials from
    # competence/attribution scoring as no-data.
    if "error" in agent and agent["error"] is not None:
        _require(
            isinstance(agent["error"], str),
            "agent.error must be a string when present and non-None",
        )

    # --- fault ---
    fault = record["fault"]
    _require(isinstance(fault, dict), "fault must be a dict")
    _require("condition" in fault, "fault.condition is required")
    if fault["condition"] != "none":
        _require(
            _is_number(fault.get("injected_at_rel_s")),
            "fault.injected_at_rel_s must be numeric when fault.condition != 'none'",
        )
        if fault.get("cleared_at_rel_s") is not None:
            _require(
                _is_number(fault["cleared_at_rel_s"]),
                "fault.cleared_at_rel_s must be numeric when present",
            )
        # 2026-07-06: per-cell netem inject-verification (Refs #3, science-
        # approved binding condition 1) — a fault cell must carry EVIDENCE
        # the qdisc actually carried netem, never just the injector's
        # add-rc=0 claim (Pattern A, silent-fallback-hazards.md). Required
        # for ANY non-"none" condition, not just netem_lag.
        _require(
            isinstance(fault.get("inject_verified"), bool),
            "fault.inject_verified must be present and a bool when fault.condition != 'none'",
        )
        _require(
            isinstance(fault.get("qdisc_snapshot"), str),
            "fault.qdisc_snapshot must be present and a string when fault.condition != 'none'",
        )

    # --- state_timeline ---
    st = record["state_timeline"]
    _require(isinstance(st, dict), "state_timeline must be a dict")

    samples = st.get("samples")
    _require(isinstance(samples, list), "state_timeline.samples must be a list")
    for i, s in enumerate(samples):
        _require(isinstance(s, dict), f"state_timeline.samples[{i}] must be a dict")
        _require(
            "t_rel_s" in s and _is_number(s["t_rel_s"]),
            f"state_timeline.samples[{i}].t_rel_s must be present and numeric (seconds vs trial_start_utc)",
        )
        _require(
            "probe_ok" in s and isinstance(s["probe_ok"], bool),
            f"state_timeline.samples[{i}].probe_ok must be present and a bool "
            "(distinguishes a dead probe from a genuinely empty target)",
        )
        if s["probe_ok"] is True:
            sli_name = s.get("sli_name")
            value = s.get("value")
            expected_type = SLI_VALUE_TYPES.get(sli_name)
            if expected_type == "number":
                _require(
                    _is_number(value),
                    f"state_timeline.samples[{i}].value must be numeric when probe_ok is True "
                    f"for sli_name={sli_name!r} (the placement predicate cuts on it)",
                )
            elif expected_type == "bool":
                _require(
                    isinstance(value, bool),
                    f"state_timeline.samples[{i}].value must be a bool when probe_ok is True "
                    f"for sli_name={sli_name!r} (a boolean liveness SLI)",
                )
            elif expected_type == "string":
                _require(
                    isinstance(value, str) and value != "",
                    f"state_timeline.samples[{i}].value must be a non-empty string when probe_ok "
                    f"is True for sli_name={sli_name!r} (a diagnostic status enum)",
                )
            else:
                # Unknown SLI — forward-compatible: a new diagnostic stream can
                # land without a schema bump, but a successful probe must still
                # carry a non-null value.
                _require(
                    value is not None,
                    f"state_timeline.samples[{i}].value must be non-null when probe_ok is True "
                    f"for unknown sli_name={sli_name!r}",
                )
        else:
            _require(
                s.get("value") is None,
                f"state_timeline.samples[{i}].value must be null when probe_ok is False",
            )

    # --- transfer_status_samples (diagnostic-only; merged lens-spec §1) ------
    # get_transfer.replicationStatus snapshots — a SEPARATE array from the
    # per-provider samples[] stream (because replicationStatus is a string, not
    # the numeric physicalSize predicate). NEVER a verdict cut (contracts §1,
    # 3rd status-lags-placement instance). Optional array; each entry mirrors
    # the main-stream probe_ok/value discipline, but `value` is the status
    # string enum (not numeric): probe_ok True -> non-empty string; probe_ok
    # False -> null. This matches the real producer shape
    # (runner._poll_transfer_status_diagnostic → sli_name
    # "get_transfer.replicationStatus").
    tss = st.get("transfer_status_samples")
    if tss is not None:
        _require(
            isinstance(tss, list),
            "state_timeline.transfer_status_samples must be a list when present",
        )
        for i, ts in enumerate(tss):
            _require(
                isinstance(ts, dict),
                f"state_timeline.transfer_status_samples[{i}] must be a dict",
            )
            _require(
                "t_rel_s" in ts and _is_number(ts["t_rel_s"]),
                f"state_timeline.transfer_status_samples[{i}].t_rel_s must be present and numeric",
            )
            _require(
                "probe_ok" in ts and isinstance(ts["probe_ok"], bool),
                f"state_timeline.transfer_status_samples[{i}].probe_ok must be present and a bool",
            )
            if ts["probe_ok"] is True:
                _require(
                    isinstance(ts.get("value"), str) and ts["value"] != "",
                    f"state_timeline.transfer_status_samples[{i}].value must be a non-empty "
                    "string when probe_ok is True (the replicationStatus enum)",
                )
            else:
                _require(
                    ts.get("value") is None,
                    f"state_timeline.transfer_status_samples[{i}].value must be null when "
                    "probe_ok is False",
                )

    # --- residue_samples (E3 negative-probe; NEW optional stream) ------------
    # A THIRD state_timeline stream (alongside samples[], kept empty for E3
    # to skip the cross-namespace guard below, and transfer_status_samples[],
    # diagnostic-only) — the verifier's independent residue probe
    # (Verifier.probe_residue) over the E3 poll window
    # (docs/_e3_build_spec.md Deliverable 1). Each entry mirrors the main
    # samples[] probe_ok discipline, but with THREE data fields instead of
    # one: probe_ok True -> path_absent (bool) + total_residue (non-negative
    # int, bool excluded) + per_provider (dict) all required; probe_ok False
    # -> all three must be null/absent (a dead probe never guesses).
    residue_samples = st.get("residue_samples")
    if residue_samples is not None:
        _require(
            isinstance(residue_samples, list),
            "state_timeline.residue_samples must be a list when present",
        )
        for i, rs in enumerate(residue_samples):
            _require(
                isinstance(rs, dict),
                f"state_timeline.residue_samples[{i}] must be a dict",
            )
            _require(
                "t_rel_s" in rs and _is_number(rs["t_rel_s"]),
                f"state_timeline.residue_samples[{i}].t_rel_s must be present and numeric",
            )
            _require(
                "probe_ok" in rs and isinstance(rs["probe_ok"], bool),
                f"state_timeline.residue_samples[{i}].probe_ok must be present and a bool",
            )
            if rs["probe_ok"] is True:
                _require(
                    isinstance(rs.get("path_absent"), bool),
                    f"state_timeline.residue_samples[{i}].path_absent must be a bool "
                    "when probe_ok is True",
                )
                total_residue = rs.get("total_residue")
                _require(
                    isinstance(total_residue, int)
                    and not isinstance(total_residue, bool)
                    and total_residue >= 0,
                    f"state_timeline.residue_samples[{i}].total_residue must be a "
                    "non-negative int when probe_ok is True",
                )
                _require(
                    isinstance(rs.get("per_provider"), dict),
                    f"state_timeline.residue_samples[{i}].per_provider must be a dict "
                    "when probe_ok is True",
                )
            else:
                _require(
                    rs.get("path_absent") is None
                    and rs.get("total_residue") is None
                    and rs.get("per_provider") is None,
                    f"state_timeline.residue_samples[{i}] path_absent/total_residue/"
                    "per_provider must all be null when probe_ok is False",
                )

    st_poll_until = st.get("poll_until_rel_s")
    _require(_is_number(st_poll_until), "state_timeline.poll_until_rel_s must be a number")

    # --- the load-bearing horizon invariant ---
    # "a T_max candidate can never exceed the recorded horizon"
    if max_t_max is not None:
        _require(
            st_poll_until >= max_t_max,
            f"state_timeline.poll_until_rel_s ({st_poll_until}) must be >= "
            f"max(contract.terms[].t_max_s) ({max_t_max})",
        )

    # --- cross-namespace consistency guard (increment-4; science #15 rider) ---
    # The #15 namespace bug: expected.per_term[t].target_providers recorded a
    # human LABEL ("de") while state_timeline.samples[].provider recorded the
    # provider-ID hash — so every convergence lookup silently missed and every
    # placement term false-scored NotDetermined. Made structurally unrecurrable
    # here: a placement term (one carrying target_providers) that HAS samples
    # but none of them on any of its target_providers means the two fields are
    # in different namespaces → data-quality error. Zero samples for a term is a
    # DISTINCT condition (no namespace to compare — a never-probed / all-failed
    # term) and is intentionally NOT flagged. Labels belong in the parallel
    # target_provider_labels field, which is not cross-checked.
    samples_by_term: dict = {}
    for s in samples:
        samples_by_term.setdefault(s.get("term_id"), set()).add(s.get("provider"))
    for term_id in term_ids:
        entry = per_term.get(term_id, {})
        targets = entry.get("target_providers") if isinstance(entry, dict) else None
        if not targets:
            continue  # metadata term or no placement target — nothing to cross-check
        seen_providers = samples_by_term.get(term_id, set())
        if seen_providers and seen_providers.isdisjoint(set(targets)):
            raise SchemaError(
                f"cross-namespace mismatch for term_id={term_id!r}: "
                f"expected.per_term.target_providers={sorted(targets)!r} does not intersect "
                f"state_timeline sample providers={sorted(str(p) for p in seen_providers)!r} — "
                "the two fields are in different namespaces (the #15 label-vs-ID bug). Record "
                "provider IDs in target_providers; human labels go in target_provider_labels."
            )

    # --- fault cross-namespace guard (the #15 label-vs-ID class recurring in
    # the fault block) --------------------------------------------------------
    # Same #15 namespace bug as the expected.per_term guard above, different
    # field: fault.target_provider was recorded as a human LABEL ("de") while
    # state_timeline.samples[].provider carries the provider-ID hash namespace
    # — silently making the RQ2 truth-labeler read an injected fault as
    # un-injected (the fault target never matches any sample provider by
    # construction, so fault-detection logic keyed on provider identity
    # false-scores "no fault"). Unlike the expected.per_term guard, a fault
    # target is NOT required to be a *contracted* target_providers member (a
    # fault can legitimately target a provider outside any term's target
    # list) — only that it live in the SAME NAMESPACE as the recorded
    # provider IDs (the sample-provider set). Zero samples for the whole
    # recording is a distinct condition (no namespace to compare against) and
    # is intentionally not flagged, mirroring the existing guard's
    # zero-samples skip.
    if fault["condition"] != "none" and fault.get("target_provider") is not None:
        all_sample_providers = {s.get("provider") for s in samples}
        if all_sample_providers and fault["target_provider"] not in all_sample_providers:
            raise SchemaError(
                f"cross-namespace mismatch for fault.target_provider={fault['target_provider']!r}: "
                f"not found in state_timeline sample providers={sorted(str(p) for p in all_sample_providers)!r} "
                "— the two fields are in different namespaces (the #15 label-vs-ID bug, recurring "
                "in the fault block). Record provider IDs in fault.target_provider; human labels "
                "belong elsewhere."
            )

    # --- cost buckets (never blended) ---
    cost = record["cost"]
    _require(isinstance(cost, dict), "cost must be a dict")
    for bucket in ("verifier", "model", "retry"):
        _require(
            bucket in cost and isinstance(cost[bucket], dict),
            f"cost.{bucket} must be present and a dict",
        )

    # --- otel ---
    otel = record["otel"]
    _require(isinstance(otel, dict), "otel must be a dict")
    # increment-2b R4: otel.correlated is required + must be a bool (see
    # TestBoolIsNotANumber's pattern — bool is fine HERE since this field IS
    # semantically boolean, unlike the numeric *_rel_s / t_max_s fields).
    _require("correlated" in otel, "otel.correlated is required")
    _require(isinstance(otel["correlated"], bool), "otel.correlated must be a bool")

    # --- a2_result (optional; the one non-re-derivable lens) -----------------
    # A2's verdict is a live LLM call, so it is RECORDED into the trial rather
    # than recomputed on each aggregate run (lenses/a2.py module docstring).
    # Absent entirely for recordings not run through the A2 judge.
    if "a2_result" in record:
        a2_result = record["a2_result"]
        _require(isinstance(a2_result, dict), "a2_result must be a dict when present")

        verdict_per_term = a2_result.get("verdict_per_term")
        _require(
            isinstance(verdict_per_term, dict),
            "a2_result.verdict_per_term must be present and a dict",
        )
        _require(
            bool(verdict_per_term) or not term_ids,
            "a2_result.verdict_per_term must not be empty when contract.terms is non-empty",
        )
        for tid, v in verdict_per_term.items():
            _require(
                v in A2_VERDICT_VALUES,
                f"a2_result.verdict_per_term[{tid!r}] must be one of {A2_VERDICT_VALUES}, got {v!r}",
            )

        _require(
            isinstance(a2_result.get("raw_response"), str),
            "a2_result.raw_response must be present and a string (the verbatim judge response)",
        )
        _require(
            isinstance(a2_result.get("model_id"), str),
            "a2_result.model_id must be present and a string",
        )
        _require(
            isinstance(a2_result.get("parse_error"), bool),
            "a2_result.parse_error must be present and a bool",
        )
        if "prompt_sha" in a2_result and a2_result["prompt_sha"] is not None:
            _require(
                isinstance(a2_result["prompt_sha"], str),
                "a2_result.prompt_sha must be a string when present",
            )
        # Science-ratified (temp=0 revision): temperature is recorded as a
        # STRING ("sdk-default(unspecified)"), never a fake numeric 0.0 — the
        # subscription SDK exposes no temperature knob. Optional; string when present.
        if "temperature" in a2_result and a2_result["temperature"] is not None:
            _require(
                isinstance(a2_result["temperature"], str),
                "a2_result.temperature must be a string when present "
                "(e.g. 'sdk-default(unspecified)', never a fake 0.0)",
            )

    # --- a4_result (optional; the one LIVE arm's gate-loop trial record) -----
    # A4's outcome is a live attempt/verify/remedy loop, so it is RECORDED
    # into the trial rather than recomputed on each aggregate/rq3 run
    # (a4_gate.py module docstring). Absent entirely for recordings not run
    # through the A4 gate.
    if "a4_result" in record:
        a4_result = record["a4_result"]
        _require(isinstance(a4_result, dict), "a4_result must be a dict when present")

        final_verdict = a4_result.get("final_verdict")
        _require(
            final_verdict in A4_VERDICT_VALUES,
            f"a4_result.final_verdict must be present and one of {A4_VERDICT_VALUES}, "
            f"got {final_verdict!r}",
        )

        gate_cycles = a4_result.get("gate_cycles")
        _require(
            isinstance(gate_cycles, int) and not isinstance(gate_cycles, bool) and gate_cycles >= 1,
            "a4_result.gate_cycles must be present and an int >= 1",
        )

        _require(
            isinstance(a4_result.get("terminated"), bool),
            "a4_result.terminated must be present and a bool",
        )
        _require(
            isinstance(a4_result.get("attempts"), list),
            "a4_result.attempts must be present and a list",
        )
        _require(
            isinstance(a4_result.get("remedy_trail"), list),
            "a4_result.remedy_trail must be present and a list",
        )

        cost_per_bucket = a4_result.get("cost_per_bucket")
        _require(
            isinstance(cost_per_bucket, dict),
            "a4_result.cost_per_bucket must be present and a dict",
        )
        for key in ("verifier_probes", "agent_invocations", "gate_retries"):
            value = cost_per_bucket.get(key) if isinstance(cost_per_bucket, dict) else None
            _require(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0,
                f"a4_result.cost_per_bucket[{key!r}] must be present and an int >= 0",
            )

        if "first_attempt_verdict" in a4_result and a4_result["first_attempt_verdict"] is not None:
            _require(
                a4_result["first_attempt_verdict"] in A4_VERDICT_VALUES,
                f"a4_result.first_attempt_verdict must be one of {A4_VERDICT_VALUES} when present, "
                f"got {a4_result['first_attempt_verdict']!r}",
            )

        if "final_attribution" in a4_result and a4_result["final_attribution"] is not None:
            _require(
                a4_result["final_attribution"] in A4_ATTRIBUTION_VALUES,
                f"a4_result.final_attribution must be one of {A4_ATTRIBUTION_VALUES} when present, "
                f"got {a4_result['final_attribution']!r}",
            )


def to_jsonl(record: dict) -> str:
    """Render a (plain-dict) recording as a single compact JSON line (no trailing newline)."""
    return json.dumps(record, sort_keys=False)


def from_jsonl(line: str) -> dict:
    """Parse a single JSONL recording line back into a dict."""
    return json.loads(line.strip())
