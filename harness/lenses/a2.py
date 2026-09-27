"""A2 — LLM-as-judge / intra-model self-check (prior practice).
The prompt, model, sampling, and information boundary are fixed in advance;
this module implements it.

**Frozen information boundary.** The judge sees the TASK (the agent's
verbatim request + the SYMBOLIC guarantee/SLO/T_max — the specification) + the
agent's own TRACE (tool_calls with results, messages) + its ANSWER. It NEVER
sees: `state_timeline.*` (the verifier's out-of-band ground-truth poll),
`fault.*` (RQ2 answer key), or the verifier's RESOLVED oracle (`expected_size`'s
concrete byte count, the resolved `target_providers` ids). `render_prompt` pulls
ONLY the allowed fields; `test_a2` asserts no withheld value leaks.

**A2 is the one stochastic, non-re-derivable lens**: its verdict is a
live `claude-sonnet-5` call, so it is RECORDED into the trial (with the verbatim
raw response) rather than recomputed on each aggregate run. This module renders
the prompt, calls an injected `judge`, and parses the response with a
NotDetermined fail-safe. The record-time caller stamps the raw response
+ {model, prompt_sha} into the recording.

Stdlib-only. No top-level side effects on import (the live judge is lazy).
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional, Tuple

VERDICT_VOCAB = ("Fulfilled", "Violated", "NotDetermined")

# Frozen model/sampling. The real judge call is built lazily by
# default_judge so importing this module never requires the SDK.
JUDGE_MODEL_ID = "claude-sonnet-5"
JUDGE_TEMPERATURE = 0.0
JUDGE_MAX_TOKENS = 1024

SYSTEM_PROMPT = (
    "You are an independent reviewer auditing whether a storage-automation agent "
    "fulfilled a data-management guarantee. You are given (a) the guarantee the agent "
    "was asked to satisfy and (b) a complete record of what the agent did and claimed: "
    "every tool call it made with the arguments and the result it received, its "
    "intermediate messages, and its final answer.\n\n"
    "You must judge, for EACH guarantee term, whether the agent fulfilled it, using "
    "ONLY the agent's own record. You have no independent view of the storage system's "
    "true state — reason strictly from what the agent did and what its tool calls "
    "returned.\n\n"
    "Verdict vocabulary (choose exactly one per term):\n"
    '- "Fulfilled": the record shows the guarantee was met.\n'
    '- "Violated": the record shows the guarantee was NOT met.\n'
    '- "NotDetermined": the record is insufficient to decide — the agent did not '
    "gather (or was not returned) enough evidence to establish the term either way. "
    "Use this when the agent claims success but the record does not substantiate it, "
    "as well as when the outcome is genuinely ambiguous. Do NOT default to Fulfilled "
    "on an unsubstantiated claim, and do NOT default to Violated on missing evidence.\n\n"
    "Respond with a single JSON object and nothing else."
)


def _render_slo(slo: dict) -> str:
    """SYMBOLIC SLO string ONLY (spec, not resolved outcome). Renders the predicate
    + any symbolic threshold (`min_distinct_providers`), and DELIBERATELY omits
    `slo.target_providers` — those are the verifier's RESOLVED oracle ids, not
    part of the specification shown to the judge."""
    if not isinstance(slo, dict):
        return str(slo)
    predicate = slo.get("predicate", "(unspecified predicate)")
    min_prov = slo.get("min_distinct_providers")
    if min_prov is not None:
        return f"{predicate} (on >= {min_prov} distinct providers)"
    return str(predicate)


def _render_terms_block(terms: list) -> str:
    lines = []
    for t in terms:
        lines.append(f"  - term_id: {t.get('term_id')}")
        lines.append(f"    guarantee: {t.get('guarantee', '')}")
        lines.append(f"    slo: {_render_slo(t.get('slo', {}))}")
        lines.append(f"    deadline_seconds: {t.get('t_max_s')}")
    return "\n".join(lines)


def _render_tool_calls_block(tool_calls: list) -> str:
    lines = []
    for tc in tool_calls:
        args = json.dumps(tc.get("args", {}), sort_keys=True)
        result = tc.get("result")
        result_s = result if isinstance(result, str) else json.dumps(result, sort_keys=True)
        lines.append(
            f"[t={tc.get('ts_rel_s')}s] {tc.get('name')}({args}) -> ok={tc.get('ok')} result={result_s}"
        )
    return "\n".join(lines) if lines else "(no tool calls)"


def _render_messages_block(messages: list) -> str:
    if not messages:
        return "(no messages)"
    out = []
    for m in messages:
        out.append(m if isinstance(m, str) else json.dumps(m, sort_keys=True))
    return "\n".join(out)


def render_prompt(recording: dict) -> Tuple[str, str]:
    """(system_prompt, user_prompt) for the judge. Serializes ONLY the
    information-boundary-allowed fields: task spec (symbolic) + agent
    trace + agent claim. NEVER state_timeline / fault / resolved expected.*"""
    agent = recording.get("agent", {}) or {}
    terms = recording.get("contract", {}).get("terms") or []

    user_prompt = (
        "GUARANTEE TERMS:\n"
        f"{_render_terms_block(terms)}\n\n"
        "ORIGINAL REQUEST TO THE AGENT:\n"
        f"{agent.get('prompt_verbatim', '')}\n\n"
        "AGENT TOOL CALLS (in order):\n"
        f"{_render_tool_calls_block(agent.get('tool_calls') or [])}\n\n"
        "AGENT MESSAGES:\n"
        f"{_render_messages_block(agent.get('messages') or [])}\n\n"
        "AGENT FINAL ANSWER:\n"
        f"{agent.get('final_answer', '')}\n\n"
        f"AGENT SELF-REPORT: {agent.get('self_reported', '')} "
        f"(reported done at t={agent.get('reported_done_at_rel_s')}s)\n\n"
        'Return JSON of exactly this shape:\n'
        '{"verdicts": {"<term_id>": "Fulfilled"|"Violated"|"NotDetermined", ...},\n'
        ' "rationale": "<=2 sentences citing the specific tool calls / answer that drove each verdict>"}'
    )
    return SYSTEM_PROMPT, user_prompt


def _extract_first_json_object(raw: str) -> Optional[dict]:
    """Best-effort: parse the whole string, else the first {...} balanced span."""
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    # find the first balanced {...} span
    start = raw.find("{") if isinstance(raw, str) else -1
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(raw)):
        if raw[i] == "{":
            depth += 1
        elif raw[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(raw[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def parse_response(raw: str, term_ids: list) -> Tuple[dict, bool]:
    """(verdicts, parse_error). Every term_id gets a verdict; any
    term that is missing / out-of-vocab / unparseable → NotDetermined, and
    parse_error=True is set (surfaced, never silent). A fully-clean parse where
    every term maps to a vocab verdict → parse_error=False."""
    obj = _extract_first_json_object(raw)
    verdicts: dict = {}
    parse_error = False
    raw_verdicts = obj.get("verdicts") if isinstance(obj, dict) else None
    if not isinstance(raw_verdicts, dict):
        # whole response unusable → all terms NotDetermined, flag error
        return ({tid: "NotDetermined" for tid in term_ids}, True)
    for tid in term_ids:
        v = raw_verdicts.get(tid)
        if v in VERDICT_VOCAB:
            verdicts[tid] = v
        else:
            verdicts[tid] = "NotDetermined"
            parse_error = True
    return verdicts, parse_error


def default_judge(system_prompt: str, user_prompt: str) -> str:
    """Live claude-sonnet-5 judge. Lazy — imported/constructed only
    when actually called, so this module imports with no SDK present. Raises a
    clear error rather than ever returning fabricated data, so a missing judge
    can never be silently mistaken for a real verdict (silent-fallback
    discipline)."""
    raise NotImplementedError(
        "a2.default_judge is not wired to a live claude-sonnet-5 client yet — pass an "
        "explicit `judge` callable (record-time), or wire the SDK adapter. Never let a "
        "missing judge fall through to a fabricated verdict."
    )


def verdict(recording: dict, judge: Optional[Callable] = None, *, _capture: Optional[dict] = None) -> dict:
    """Returns `{term_id: verdict, ..., "a2_parse_error": bool}` (parallel to
    a1's meta-key convention). `judge(system_prompt, user_prompt) -> raw_str` is
    injected (default = the live Sonnet-5 judge). If `_capture` is a dict, the
    rendered prompts + raw response are stored there so the record-time caller
    can persist the verbatim raw response without changing the
    return shape."""
    judge = judge or default_judge
    terms = recording.get("contract", {}).get("terms") or []
    term_ids = [t.get("term_id") for t in terms]

    system_prompt, user_prompt = render_prompt(recording)
    raw = judge(system_prompt, user_prompt)

    verdicts, parse_error = parse_response(raw, term_ids)
    if _capture is not None:
        _capture["system_prompt"] = system_prompt
        _capture["user_prompt"] = user_prompt
        _capture["raw_response"] = raw
        _capture["model_id"] = JUDGE_MODEL_ID

    result = dict(verdicts)
    result["a2_parse_error"] = parse_error
    return result
