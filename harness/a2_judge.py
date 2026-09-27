"""A2 live judge adapter — drives the A2 LLM judge against the
ambient Anthropic subscription via claude-agent-sdk, producing the
recording's `a2_result` field.

`harness/lenses/a2.py` is the pure lens (render_prompt / parse_response /
verdict-with-injected-judge). THIS module supplies the live `judge` callable it
needs: a one-shot claude-sonnet-5 completion that sees ONLY the rendered
system+user prompt (the frozen information boundary — a2.py already guarantees
the prompt carries only trace+answer+symbolic-spec). It reuses panel.py's
empirically-verified R2 isolation recipe (strict_mcp_config=True + isolated
CLAUDE_CONFIG_DIR + setting_sources=None) so the judge is a NAIVE Sonnet with
ambient auth and NONE of this VM's biomedical account connectors.

**Temperature caveat.** The A2 specification
fixes temp=0, but `ClaudeAgentOptions` exposes no `temperature` field (the
subscription SDK runs Claude Code at its default sampling). So the judge
runs at CC-default temperature, NOT 0. The reproducibility guarantees the
spec actually leans on still hold — k=1 + the verbatim raw response recorded
per trial (so any verdict is auditable / hand-re-scoreable) — but the
"temp=0" clause needs a documented revision. Flagged, not silently ignored.

Not stdlib-only (claude-agent-sdk). No top-level side effects on import.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Callable, Optional

# NB: claude_agent_sdk + panel are imported LAZILY inside the live path
# (_run_judge_async) so this module — and apply_a2()/prompt_sha() with an
# injected stub judge — import + run under stdlib python (the offline test
# suite), without the model-leg venv. Only the live Sonnet judge needs the SDK.
try:
    from harness.lenses import a2
except ImportError:  # running from inside the package
    from .lenses import a2

JUDGE_MODEL_ID = a2.JUDGE_MODEL_ID  # "claude-sonnet-5"


def prompt_sha() -> str:
    """A stable id for the frozen judge prompt version — sha256 of the a2
    SYSTEM_PROMPT (first 12 hex). Stamped into a2_result so a re-run under a
    changed prompt is detectable."""
    return hashlib.sha256(a2.SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12]


async def _run_judge_async(system_prompt: str, user_prompt: str, model_id: str) -> str:
    """One-shot Sonnet completion: no MCP tools, naive ambient-auth Sonnet.
    Returns the final assistant text (ResultMessage.result, with a TextBlock
    concatenation fallback). Imports the SDK + panel lazily (live path only)."""
    try:
        from harness import panel
    except ImportError:
        from . import panel
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
    )
    opts = ClaudeAgentOptions(
        model=model_id,
        system_prompt=system_prompt,          # raw string REPLACES the default agentic prompt (a2 §4)
        mcp_servers={},                        # no tools — a judge, not an agent
        strict_mcp_config=True,                # + mcp_servers={} suppresses this VM's ambient connectors (R2)
        env={"CLAUDE_CONFIG_DIR": panel.make_isolated_config_dir()},  # ambient-auth cred only, no project config/settings
        setting_sources=None,                  # no project/user settings bleed
        max_turns=1,                           # single response, no tool loop
        permission_mode="acceptEdits",
    )
    final_text = ""
    text_blocks: list = []
    async with ClaudeSDKClient(options=opts) as client:
        await client.query(user_prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_blocks.append(block.text)
            elif isinstance(msg, ResultMessage):
                final_text = getattr(msg, "result", None) or ""
    return final_text or "\n".join(text_blocks)


def make_sdk_judge(model_id: str = JUDGE_MODEL_ID) -> Callable:
    """Return a SYNC `judge(system_prompt, user_prompt) -> raw_str` for
    a2.verdict(). Wraps the async SDK call in asyncio.run — so it must be
    called from a NON-async context (the A2 pass runs as a batch over recorded
    trials, not inside runner.run_trial's event loop)."""
    def _judge(system_prompt: str, user_prompt: str) -> str:
        return asyncio.run(_run_judge_async(system_prompt, user_prompt, model_id))
    return _judge


def apply_a2(recording: dict, judge: Optional[Callable] = None) -> dict:
    """Run the A2 judge over ONE recording and return the `a2_result` dict
    (schema shape: verdict_per_term / raw_response / model_id / prompt_sha /
    parse_error) ready to stamp onto the recording. `judge` defaults to the live
    Sonnet judge; pass a stub in tests."""
    judge = judge or make_sdk_judge()
    capture: dict = {}
    verdict = a2.verdict(recording, judge=judge, _capture=capture)
    parse_error = verdict.pop("a2_parse_error", False)
    return {
        "verdict_per_term": verdict,
        "raw_response": capture.get("raw_response", ""),
        "model_id": capture.get("model_id", JUDGE_MODEL_ID),
        "prompt_sha": prompt_sha(),
        "parse_error": parse_error,
        # (temp=0 revision, opt-a): the subscription SDK exposes
        # no temperature knob, so the judge runs at CC-default sampling. Record
        # the honest value, NEVER a fake "0.0" — reproducibility rests on k=1 +
        # the verbatim raw_response, not on temperature.
        "temperature": "sdk-default(unspecified)",
    }
