"""Model-leg panel adapters — free-first, two-stage.

Two adapters, one uniform `ModelLeg.run(prompt, allowed_tools) -> LegResult`
interface:

  - `ForgeOpenAILeg`  — OpenAI-compatible legs over PLGrid LLM Forge, driving
    onedata-mcp as a REAL stdio MCP server via the official `mcp` client SDK
    (spawn -> initialize -> list_tools -> call_tool). Adapted from the
    onedata-mcp benchmark's `OpenAICompatAdapter`
    (benchmark/llm_adapters/openai_compat.py), but talking to onedata-mcp
    over the actual MCP protocol instead of an in-process FastMCP import —
    this is what makes it a genuine "drive a model leg through the MCP
    server" leg rather than a shortcut.
  - `AnthropicSDKLeg` — `claude-agent-sdk` legs, ambient-subscription auth
    (no API key), registering onedata-mcp as an MCP stdio server via
    `ClaudeAgentOptions.mcp_servers` (the SDK's own native mechanism —
    mirrors the onedata-mcp benchmark's `ClaudeAgentSdkAdapter`).

`LegResult` fields map directly onto schema.py's `agent` + `cost.model`
sub-objects (see `LegResult.as_agent_dict` / `.as_cost_model_dict`).

Not stdlib-only (see harness/requirements.txt) — this module is NOT imported
by schema.py/fixtures.py/verifier.py, which stay dependency-free by design.
No top-level side effects on import (network calls, token minting, and
subprocess spawns all happen inside `run()` / `verify_leg()` / `fc_smoke()`).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
import tempfile
import time
from typing import Any, Optional

from openai import AsyncOpenAI

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from claude_agent_sdk.types import ToolResultBlock

if __package__ in (None, ""):
    import sys as _sys

    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _sys.path.insert(0, _REPO_ROOT)
    from harness import fixtures, settings
else:
    from . import fixtures, settings

# --- onedata-mcp server location (the MCP server the legs drive) -------------
# The onedata-mcp package + its own venv live outside this
# repo. Override via env for portability.
ONEDATA_MCP_BIN = os.environ.get("ONEDATA_MCP_BIN") or settings.get("onedata_mcp_bin", "")

# Federation env the onedata-mcp child needs (onedata_mcp/config.py). Onezone
# host mirrors fixtures.ONEZONE_URL (the single onezone-admin-mintable token
# type this test federation uses for both onezone + oneprovider calls).
ONEZONE_HOST = fixtures.ONEZONE_URL
DEFAULT_ONEPROVIDER_HOST = settings.get("source.host")

# --- Forge (OpenAI-compatible) ------------------------------------------------
FORGE_BASE_URL = os.environ.get("PLGRID_FORGE_BASE_URL", "https://llmlab.plgrid.pl/api/v1")

# --- Anthropic SDK legs: list-price table for the estimated_sdk_x_listprice
# cost bucket ("cost = estimated (SDK tokens x list price)"). Values from
# a cached Anthropic pricing table, USD per 1M
# tokens as (input, output). Add an entry here before adding a new anthropic-sdk
# arm to config/arms.json, or cost_usd silently falls back to 0.0.
ANTHROPIC_LIST_PRICE_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
}

# W1 Stage-1 candidate pool. Model IDs verified present on Forge /models;
# RE-VERIFY with panel.verify_leg (GET /models) before any live
# run — model IDs drift. Haiku is the subscription control leg.
STAGE1_FORGE_MODEL_IDS = (
    "zai-org/GLM-5.2-FP8",
    "zai-org/GLM-4.7-Flash",
    "Qwen/Qwen3.6-35B-A3B",
    "Qwen/Qwen3.6-27B",
    "google/gemma-4-31B",
    "meta-llama/Llama-3.3-70B-Instruct",
    "Qwen/Qwen3-Coder-30B-A3B-Instruct",
)

MCP_SERVER_NAME = "onedata"
TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"

# Deny the full Claude Code built-in toolset on SDK legs so the ONLY tool
# surface available is the onedata-mcp server — comparison-parity with the
# Forge leg's curated allowlist. Mirrors the onedata-mcp benchmark's
# ClaudeAgentSdkAdapter defense-in-depth list (empirically, allowed_tools
# alone did not block ToolSearch there).
_SDK_BUILTIN_TOOLS_TO_DENY = (
    "AskUserQuestion", "Bash", "CronCreate", "CronDelete", "CronList", "Edit",
    "EnterPlanMode", "EnterWorktree", "ExitPlanMode", "ExitWorktree", "Glob",
    "Grep", "Monitor", "NotebookEdit", "PushNotification", "Read",
    "RemoteTrigger", "ScheduleWakeup", "SendMessage", "Skill", "Task",
    "TaskOutput", "TaskStop", "TeamCreate", "TeamDelete", "TodoWrite",
    "ToolSearch", "WebFetch", "WebSearch", "Write",
)

# Concurrency note ("concurrency-capped"): SDK legs share ONE Anthropic
# subscription rate-pool. This module does not itself enforce a cap (no
# sweep orchestrator exists yet — single-leg smoke only);
# a future sweep.py MUST stagger/cap concurrent AnthropicSDKLeg.run() calls,
# e.g. via an asyncio.Semaphore(N) sized well below the subscription's own
# concurrent-request ceiling, so the shared subscription isn't starved and
# per-call latency stays clean.


def make_isolated_config_dir() -> str:
    """SDK ambient-context isolation: `tempfile.mkdtemp()`s a fresh per-leg dir
    and copies ONLY the CLI's credentials file (chmod 600) into it.

    Used as `CLAUDE_CONFIG_DIR` for `AnthropicSDKLeg`'s spawned `claude` CLI
    subprocess. The CLI resolves its config/credentials/project-instructions/
    settings relative to `CLAUDE_CONFIG_DIR` (defaulting to the CLI's
    per-user config directory); pointing it
    at a dir that holds *only* the credentials file preserves ambient OAuth
    (auth survives) while dropping the project instruction file + user/project
    memory + settings/hooks/plugins (context is gone) — see
    `AnthropicSDKLeg.__init__`'s existing `self.cwd` isolation note for the
    concrete leak this defends against (a project-memory leak into
    a live trial's final answer).
    """
    iso_dir = tempfile.mkdtemp(prefix="icsoc-claude-config-")
    src = os.path.join(os.path.expanduser("~"), ".claude", ".credentials.json")
    dst = os.path.join(iso_dir, ".credentials.json")
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o600)
    return iso_dir


def mint_onedata_mcp_env(oneprovider_host: str = DEFAULT_ONEPROVIDER_HOST) -> dict:
    """Mint a fresh onezone access token locally and build the env dict the
    onedata-mcp stdio child needs to reach the federation. Reuses
    fixtures.mint_token_local() (own HTTPS mint, no SSH) — never
    logs/prints/writes the token value.
    """
    token = fixtures.mint_token_local()
    return {
        "ONEDATA_ONEZONE_HOST": ONEZONE_HOST,
        "ONEDATA_ONEZONE_TOKEN": token,
        "ONEDATA_ONEPROVIDER_HOST": oneprovider_host,
        "ONEDATA_ONEPROVIDER_TOKEN": token,
        "ONEDATA_ALLOW_INSECURE_TLS": "true",
        "FASTMCP_LOG_LEVEL": "WARNING",
    }


# --- LegResult -----------------------------------------------------------------

@dataclasses.dataclass
class LegResult:
    """Uniform per-leg-run result. Maps directly onto schema.py's `agent` +
    `cost.model` sub-objects — see `as_agent_dict` / `as_cost_model_dict`.
    """

    tool_calls: list  # [{name, args, result, ts_rel_s, ok}, ...]
    messages: list
    final_answer: str
    self_reported: str  # "done" | "error" | "no_final_answer"
    reported_done_at_rel_s: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    cost_estimation: str = "measured"  # "measured" | "estimated_sdk_x_listprice"
    raw_trace: Any = "absent-by-design"  # sdk legs: literal string; else an object
    error: Optional[str] = None  # leg-level failure detail (not part of schema.py)

    def as_agent_dict(self, prompt_verbatim: str) -> dict:
        """Render as the recording schema's `agent` sub-object."""
        return {
            "prompt_verbatim": prompt_verbatim,
            "tool_calls": self.tool_calls,
            "messages": self.messages,
            "final_answer": self.final_answer,
            "self_reported": self.self_reported,
            "reported_done_at_rel_s": self.reported_done_at_rel_s,
            "raw_trace": self.raw_trace,
            "error": self.error,
        }

    def as_cost_model_dict(self) -> dict:
        """Render as the recording schema's `cost.model` sub-object."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "estimation": self.cost_estimation,
            "usd": round(self.cost_usd, 6),
        }


class ModelLeg:
    """The uniform interface every panel adapter implements."""

    name: str
    model_id: str

    async def run(self, prompt: str, allowed_tools: list) -> LegResult:
        """Run one trial: hand `prompt` to the model with exactly
        `allowed_tools` (onedata-mcp tool names, unprefixed) available,
        drive the agent loop through the REAL onedata-mcp MCP server, and
        capture the full trace + cost into a LegResult."""
        raise NotImplementedError


# --- Forge (OpenAI-compatible) leg -------------------------------------------

def _mcp_result_to_text(result) -> str:
    """Best-effort serialization of an mcp.types.CallToolResult -> string."""
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        try:
            return json.dumps(structured, ensure_ascii=False)
        except (TypeError, ValueError):
            return repr(structured)
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts) if parts else repr(getattr(result, "content", None))


class ForgeOpenAILeg(ModelLeg):
    """OpenAI-compatible leg over PLGrid LLM Forge. Temperature/max_tokens
    ARE settable here. Cost is measured (real tokens); Forge is grant-billed
    so usd is left at 0.0 unless a list-price note is added later.
    """

    def __init__(
        self,
        model_id: str,
        temperature: float = 1.0,
        max_tokens: int = 4096,
        max_tool_rounds: int = 8,
        onedata_env: Optional[dict] = None,
        mcp_command: str = ONEDATA_MCP_BIN,
        base_url: Optional[str] = None,
        key_env: str = "PLGRID_FORGE_API_KEY",
        api_model_id: Optional[str] = None,
    ):
        api_key = os.environ.get(key_env, "").strip()
        if not api_key:
            raise RuntimeError(f"ForgeOpenAILeg requires {key_env} in env")

        self.name = f"forge:{model_id}"
        self.model_id = model_id
        self.api_model_id = api_model_id
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_tool_rounds = max_tool_rounds
        self.mcp_command = mcp_command
        # Onedata env is minted lazily per-run() when None (fresh token per
        # trial); pass an explicit dict to reuse a token across several runs.
        self._onedata_env = onedata_env
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url or FORGE_BASE_URL)

    def model_config(self) -> dict:
        cfg = {
            "model_id": self.model_id,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.api_model_id:
            cfg["api_model_id"] = self.api_model_id
        return cfg

    async def run(self, prompt: str, allowed_tools: list, system_prompt: Optional[str] = None,
                  mount_mcp: bool = True) -> LegResult:
        """`system_prompt`: uniform controlled system
        message, prepended as an OpenAI-compat `role: "system"` turn when
        given. Forge legs are bare OpenAI-compatible calls with no ambient
        Claude Code session, so they need no ambient-context isolation — they
        only ever
        see exactly what's handed to them here.

        `mount_mcp` is accepted for a uniform ModelLeg.run() interface but is a
        no-op here: a Forge leg exposes ONLY the tools in `allowed_tools`, so
        the contamination canary's `allowed_tools=[]` already makes it genuinely tool-less
        regardless of mount_mcp.
        """
        t0 = time.monotonic()
        onedata_env = self._onedata_env or mint_onedata_mcp_env()
        server_params = StdioServerParameters(command=self.mcp_command, args=[], env=onedata_env)

        tool_calls: list = []
        messages: list = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        final_answer = ""
        usage_in = 0
        usage_out = 0
        error: Optional[str] = None
        rounds_used = 0

        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                by_name = {t.name: t for t in listed.tools}
                missing = set(allowed_tools) - set(by_name)
                if missing:
                    raise RuntimeError(f"onedata-mcp doesn't expose tools: {sorted(missing)}")
                openai_tools = [
                    {
                        "type": "function",
                        "function": {
                            "name": name,
                            "description": t.description or "",
                            "parameters": t.inputSchema or {"type": "object", "properties": {}},
                        },
                    }
                    for name, t in sorted(by_name.items())
                    if name in allowed_tools
                ]

                for round_ix in range(self.max_tool_rounds):
                    rounds_used = round_ix + 1
                    try:
                        response = await self._client.chat.completions.create(
                            model=self.api_model_id or self.model_id,
                            messages=messages,
                            tools=openai_tools,
                            tool_choice="auto",
                            temperature=self.temperature,
                            max_tokens=self.max_tokens,
                        )
                    except Exception as e:  # noqa: BLE001 -- surfaced via LegResult.error
                        error = f"{type(e).__name__}: {e}"
                        break

                    usage = getattr(response, "usage", None)
                    if usage:
                        usage_in += getattr(usage, "prompt_tokens", 0) or 0
                        usage_out += getattr(usage, "completion_tokens", 0) or 0

                    choice = response.choices[0]
                    msg = choice.message
                    tcs = list(msg.tool_calls or [])

                    assistant_entry: dict = {"role": "assistant", "content": msg.content or None}
                    if tcs:
                        assistant_entry["tool_calls"] = [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments or "{}",
                                },
                            }
                            for tc in tcs
                        ]
                    messages.append(assistant_entry)

                    if not tcs:
                        final_answer = msg.content or ""
                        break

                    for tc in tcs:
                        name = tc.function.name
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                            if not isinstance(args, dict):
                                args = {}
                        except json.JSONDecodeError:
                            args = {}

                        ts_rel_s = round(time.monotonic() - t0, 3)
                        try:
                            call_result = await session.call_tool(name, args)
                            content_text = _mcp_result_to_text(call_result)
                            ok = not bool(call_result.isError)
                        except Exception as e:  # noqa: BLE001
                            content_text = json.dumps({"error": f"{type(e).__name__}: {e}"})
                            ok = False

                        tool_calls.append({
                            "name": name,
                            "args": args,
                            "result": content_text,
                            "ts_rel_s": ts_rel_s,
                            "ok": ok,
                        })
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": content_text,
                        })
                # else-less: if the loop exhausts max_tool_rounds without an
                # empty-tool_calls turn, final_answer stays "" (no final
                # answer reached) — this IS the integrity signal
                # fc_smoke()/verify_leg() consume.

        reported_done_at_rel_s = round(time.monotonic() - t0, 3)
        return LegResult(
            tool_calls=tool_calls,
            messages=messages,
            final_answer=final_answer,
            self_reported="done" if final_answer else ("error" if error else "no_final_answer"),
            reported_done_at_rel_s=reported_done_at_rel_s,
            prompt_tokens=usage_in,
            completion_tokens=usage_out,
            cost_usd=0.0,
            cost_estimation="measured",  # Forge is grant-billed — tokens real, usd=0
            raw_trace={"protocol": "openai-compat-via-mcp-stdio", "rounds_used": rounds_used},
            error=error,
        )


# --- Anthropic claude-agent-sdk leg -------------------------------------------

def _strip_tool_prefix(name: str) -> str:
    return name[len(TOOL_PREFIX):] if name.startswith(TOOL_PREFIX) else name


def _sdk_content_to_text(content) -> str:
    """Best-effort conversion of a ToolResultBlock.content payload -> string.
    Content can be a string, a list of dicts, or a list of block objects.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                t = item.get("text") or item.get("content") or ""
                if isinstance(t, str):
                    parts.append(t)
            else:
                t = getattr(item, "text", None)
                if isinstance(t, str):
                    parts.append(t)
        return "".join(parts)
    return repr(content)


def _estimate_anthropic_cost_usd(model_id: str, prompt_tokens: int, completion_tokens: int) -> float:
    prices = ANTHROPIC_LIST_PRICE_USD_PER_MTOK.get(model_id)
    if prices is None:
        return 0.0
    in_price, out_price = prices
    return round((prompt_tokens / 1_000_000) * in_price + (completion_tokens / 1_000_000) * out_price, 6)


# MCP-server-connect race: onedata-mcp is a heavier stdio child (imports fastmcp +
# opentelemetry) than the pre-warmed claude.ai-hosted connectors, so it can
# still be "pending" in ClaudeSDKClient's very first `init` SystemMessage —
# the model's first inference turn then correctly reports it has no such
# tool (the tool genuinely isn't in its exposed tool list yet), NOT an
# FC-capability gap. `ClaudeSDKClient.get_mcp_status()` gives a way to poll
# for "connected" before sending the first query, closing the race.
_MCP_READY_POLL_INTERVAL_S = 0.5
_MCP_READY_TIMEOUT_S = 20.0


async def _await_mcp_server_ready(client, server_name: str) -> "tuple[bool, str]":
    """Poll `client.get_mcp_status()` until `server_name` is 'connected'
    (or a terminal failure state), bounded by `_MCP_READY_TIMEOUT_S`.
    Returns (ready, detail) — never raises; a status-query error is treated
    as not-ready with the exception recorded in `detail`.
    """
    deadline = time.monotonic() + _MCP_READY_TIMEOUT_S
    last_status = "unknown"
    while time.monotonic() < deadline:
        try:
            status = await client.get_mcp_status()
        except Exception as e:  # noqa: BLE001
            return False, f"get_mcp_status() raised {type(e).__name__}: {e}"

        servers = status.get("mcpServers", []) if isinstance(status, dict) else []
        entry = next((s for s in servers if s.get("name") == server_name), None)
        if entry is None:
            last_status = f"server {server_name!r} not present in get_mcp_status()"
        else:
            last_status = entry.get("status", "unknown")
            if last_status == "connected":
                return True, "connected"
            if last_status in ("failed", "needs-auth", "disabled"):
                return False, f"{last_status}: {entry.get('error', '<no error detail>')}"
        await asyncio.sleep(_MCP_READY_POLL_INTERVAL_S)

    return False, f"timed out after {_MCP_READY_TIMEOUT_S}s (last status: {last_status})"


class AnthropicSDKLeg(ModelLeg):
    """`claude-agent-sdk` leg — ambient subscription auth (no API key),
    driving onedata-mcp as a REAL stdio MCP server via
    `ClaudeAgentOptions.mcp_servers`. No temperature/max_tokens knob exists
    on the SDK; recorded as "sdk-not-settable" in model_config(). Cost is
    always "estimated_sdk_x_listprice" (uses ResultMessage.total_cost_usd
    when the SDK reports it, else a token x list-price fallback).
    raw_trace is the literal "absent-by-design" (the harness record
    is canonical for SDK legs).
    """

    def __init__(
        self,
        model_id: str,
        effort: Optional[str] = None,
        max_turns: int = 8,
        max_budget_usd: Optional[float] = None,
        onedata_env: Optional[dict] = None,
        mcp_command: str = ONEDATA_MCP_BIN,
        cwd: Optional[str] = None,
    ):
        self.name = f"sdk:{model_id}"
        self.model_id = model_id
        self.effort = effort
        self.max_turns = max_turns
        self.max_budget_usd = max_budget_usd
        self.mcp_command = mcp_command
        self._onedata_env = onedata_env
        # SDK ambient-context isolation: a per-leg isolated
        # CLAUDE_CONFIG_DIR holding ONLY the CLI's credentials file — see
        # make_isolated_config_dir()'s docstring. Created once per leg
        # instance (same lifecycle as self.cwd below).
        self.claude_config_dir = make_isolated_config_dir()
        # Experimental-validity guard: the SDK
        # spawns `claude` as a subprocess that inherits cwd from the parent
        # process by default. `setting_sources=None` suppresses
        # settings.json but NOT project-instruction-file / project-memory
        # loading, which is
        # a separate always-on mechanism — a trial run from a working
        # directory whose project instructions or user-global memory mention
        # the experiment can leak that knowledge into a live trial's
        # final answer (e.g. a mention of "the Onedata spice federation"
        # surfacing in the agent's answer). That's a real confound for
        # A0-style "naive agent" arms, which must not have a-priori
        # knowledge the harness didn't hand them via the prompt. Default to
        # a fresh, empty scratch dir (no project instructions, no project
        # memory) per
        # instance unless the caller overrides.
        self.cwd = cwd or tempfile.mkdtemp(prefix="icsoc-sdk-leg-")

    def model_config(self) -> dict:
        return {
            "model_id": self.model_id,
            "temperature": "sdk-not-settable",
            "max_tokens": "sdk-not-settable",
            "sampling": {"effort": self.effort} if self.effort else None,
        }

    def _build_options(
        self, allowed_tools: list, onedata_env: dict, system_prompt: Optional[str] = None,
        mount_mcp: bool = True,
    ) -> ClaudeAgentOptions:
        sdk_allowed = sorted(f"{TOOL_PREFIX}{name}" for name in allowed_tools)
        # mount_mcp=False → NO task tools at all (mcp_servers={}). Used by the
        # contamination canary so the probe leg is genuinely tool-less: a
        # tool-less leg that still mentions onedata/spice/federation/etc. is
        # real contamination (ambient bleed), not the model correctly describing
        # a tool that was handed to it. With strict_mcp_config=True + mcp_servers={} the
        # account's connectors stay suppressed AND onedata is absent.
        mcp_servers = {
            MCP_SERVER_NAME: {
                "type": "stdio",
                "command": self.mcp_command,
                "args": [],
                "env": onedata_env,
            },
        } if mount_mcp else {}
        kwargs = dict(
            allowed_tools=sdk_allowed,
            disallowed_tools=list(_SDK_BUILTIN_TOOLS_TO_DENY),
            mcp_servers=mcp_servers,
            # strict_mcp_config=True TOGETHER WITH the
            # explicit mcp_servers={} above is the key line that suppresses
            # every other configured/ambient connector (this VM's account is
            # a biomedical-research subscription; without this the SDK leg
            # would see ChEMBL/ClinicalTrials/PubMed/etc alongside onedata).
            strict_mcp_config=True,
            # A per-leg isolated CLAUDE_CONFIG_DIR (see
            # make_isolated_config_dir()) — merged on top of the inherited
            # process env by the SDK transport (never a full replacement),
            # so ambient auth-adjacent env (PATH, HOME, ...) still flows
            # through; this ONLY redirects where the spawned `claude` CLI
            # looks for its config/credentials/project-instruction-file/
            # settings. Distinct
            # namespace from `onedata_env` above (that's the onedata-mcp
            # child's own env) — never merged together.
            env={"CLAUDE_CONFIG_DIR": self.claude_config_dir},
            permission_mode="acceptEdits",
            model=self.model_id,
            max_turns=self.max_turns,
            setting_sources=None,
            cwd=self.cwd,
        )
        if self.max_budget_usd is not None:
            kwargs["max_budget_usd"] = self.max_budget_usd
        if self.effort is not None:
            kwargs["effort"] = self.effort
        if system_prompt is not None:
            # R1: a raw string REPLACES Claude Code's default agentic system
            # prompt (a SystemPromptPreset would APPEND instead) — this is
            # what makes the uniform controlled prompt uniform across legs.
            kwargs["system_prompt"] = system_prompt
        return ClaudeAgentOptions(**kwargs)

    async def run(self, prompt: str, allowed_tools: list, system_prompt: Optional[str] = None,
                  mount_mcp: bool = True) -> LegResult:
        t0 = time.monotonic()
        onedata_env = self._onedata_env or mint_onedata_mcp_env()
        opts = self._build_options(allowed_tools, onedata_env, system_prompt=system_prompt,
                                   mount_mcp=mount_mcp)

        tool_calls: list = []
        pending: dict = {}
        messages_log: list = []
        final_text = ""
        usage_in = 0
        usage_out = 0
        total_cost_usd: Optional[float] = None
        error: Optional[str] = None
        reported_done_at_rel_s: Optional[float] = None

        try:
            async with ClaudeSDKClient(options=opts) as client:
                mcp_ready, mcp_status_detail = await _await_mcp_server_ready(client, MCP_SERVER_NAME)
                if not mcp_ready:
                    error = f"onedata MCP server never reached 'connected': {mcp_status_detail}"
                    reported_done_at_rel_s = round(time.monotonic() - t0, 3)
                    return LegResult(
                        tool_calls=[], messages=[], final_answer="",
                        self_reported="error", reported_done_at_rel_s=reported_done_at_rel_s,
                        prompt_tokens=0, completion_tokens=0, cost_usd=0.0,
                        cost_estimation="estimated_sdk_x_listprice",
                        raw_trace="absent-by-design", error=error,
                    )

                await client.query(prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        for block in msg.content:
                            if isinstance(block, ToolUseBlock):
                                idx = len(tool_calls)
                                tool_calls.append({
                                    "name": _strip_tool_prefix(block.name),
                                    "args": dict(block.input or {}),
                                    "result": None,
                                    "ts_rel_s": round(time.monotonic() - t0, 3),
                                    "ok": True,  # flipped to False below on ToolResultBlock.is_error
                                })
                                pending[block.id] = idx
                                messages_log.append({
                                    "role": "assistant",
                                    "tool_use": {"name": block.name, "input": block.input},
                                })
                            elif isinstance(block, TextBlock):
                                messages_log.append({"role": "assistant", "text": block.text})
                    elif isinstance(msg, UserMessage):
                        content = msg.content
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, ToolResultBlock):
                                    idx = pending.pop(block.tool_use_id, None)
                                    if idx is not None:
                                        is_error = bool(block.is_error)
                                        tool_calls[idx]["result"] = _sdk_content_to_text(block.content)
                                        tool_calls[idx]["ok"] = not is_error
                    elif isinstance(msg, ResultMessage):
                        final_text = getattr(msg, "result", None) or ""
                        reported_done_at_rel_s = round(time.monotonic() - t0, 3)
                        usage = getattr(msg, "usage", None) or {}
                        if isinstance(usage, dict):
                            usage_in = usage.get("input_tokens", 0) or 0
                            usage_out = usage.get("output_tokens", 0) or 0
                        total_cost_usd = getattr(msg, "total_cost_usd", None)
                    elif isinstance(msg, SystemMessage):
                        pass  # init / hook events — not part of the trace
        except Exception as e:  # noqa: BLE001
            error = f"{type(e).__name__}: {e}"

        if reported_done_at_rel_s is None:
            reported_done_at_rel_s = round(time.monotonic() - t0, 3)

        cost_usd = (
            total_cost_usd
            if total_cost_usd is not None
            else _estimate_anthropic_cost_usd(self.model_id, usage_in, usage_out)
        )

        return LegResult(
            tool_calls=tool_calls,
            messages=messages_log,
            final_answer=final_text,
            self_reported="done" if final_text else ("error" if error else "no_final_answer"),
            reported_done_at_rel_s=reported_done_at_rel_s,
            prompt_tokens=usage_in,
            completion_tokens=usage_out,
            cost_usd=cost_usd,
            cost_estimation="estimated_sdk_x_listprice",
            raw_trace="absent-by-design",
            error=error,
        )


# --- verification + FC gate -----------------------------------------------------

async def verify_leg(leg: ModelLeg) -> "tuple[bool, str]":
    """Fail-loud per-leg config check ("Per-leg config verified
    fail-loud"): auth reachable only, no onedata-mcp / tool-calling
    involved. Forge: GET /models returns 200 and lists the leg's model_id,
    THEN a minimal completion probe confirms the model actually serves
    (catches the "listed in /models but 400s on completion" class — e.g.
    inactive or grant-scoped models — that the /models check alone misses;
    see the W1 smoke: Llama-3.3-70B "currently inactive" + GLM-5.2-FP8 "not
    available for grant" both passed /models but 400'd on completion).
    SDK: a trivial 1-turn call completes and yields a ResultMessage.
    Returns (ok, detail) — never raises.
    """
    if isinstance(leg, ForgeOpenAILeg):
        try:
            resp = await leg._client.models.list()
            ids = {m.id for m in resp.data}
            if leg.model_id not in ids:
                return False, f"model {leg.model_id!r} not in Forge /models list ({len(ids)} seen)"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

        # Second gate: a minimal completion probe (added on top of the
        # /models check above, not a replacement for it).
        try:
            await leg._client.chat.completions.create(
                model=leg.model_id,
                messages=[{"role": "user", "content": "ok"}],
                max_tokens=1,
            )
        except Exception as e:  # noqa: BLE001
            return False, f"completion probe failed: {type(e).__name__}: {e}"

        return True, f"Forge /models 200 + completion probe OK; {leg.model_id!r} present ({len(ids)} models total)"

    if isinstance(leg, AnthropicSDKLeg):
        try:
            opts = ClaudeAgentOptions(
                model=leg.model_id,
                max_turns=1,
                setting_sources=None,
                disallowed_tools=list(_SDK_BUILTIN_TOOLS_TO_DENY),
                cwd=leg.cwd,  # same project-memory isolation as run() — see AnthropicSDKLeg.__init__
            )
            saw_result = False
            async for msg in query(prompt="Reply with exactly one word: OK", options=opts):
                if isinstance(msg, ResultMessage):
                    saw_result = True
            if not saw_result:
                return False, "no ResultMessage observed from claude-agent-sdk"
            return True, f"claude-agent-sdk 1-turn call to {leg.model_id!r} completed"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    return False, f"unknown leg type {type(leg).__name__}"


async def fc_smoke(leg: ModelLeg, tool_name: str = "list_user_spaces") -> "tuple[bool, LegResult]":
    """Integrity guard — one-shot function-calling smoke: does the
    model emit a tool_call when handed exactly one trivial, real, read-only
    onedata-mcp tool? Competence/tool-calling/cost gate ONLY —
    ("never gate-uplift"), this never elevates a lens verdict; it's a panel
    admission check (does this leg even call tools).

    Uses `list_user_spaces` by default — a zero-argument, always-safe
    read-only tool, so this smoke never touches federation state.

    The prompt deliberately does NOT name `tool_name` literally: Forge legs
    expose the bare onedata-mcp tool name, but SDK legs expose it prefixed
    (`mcp__onedata__list_user_spaces`) per the Claude Agent SDK's own MCP
    naming convention. A prompt that says "call the `list_user_spaces`
    tool" causes the SDK leg to correctly-but-uselessly report that no tool
    with that literal name exists — a prompt-construction bug, not an
    FC-capability gap (observed: claude-haiku-4-5
    answered "I don't have access to a `list_user_spaces` tool" while
    listing its OTHER available tools, then called the equivalent tool
    fine moments later in the unnamed-task smoke below). Describing the
    task without the identifier lets each leg's own naming scheme resolve
    correctly.
    """
    prompt = (
        "You have access to tools for interacting with a data-management "
        "federation. Use the appropriate available tool right now to list "
        "the spaces you can see. Do not explain first — call the tool "
        "immediately, then summarize the result."
    )
    result = await leg.run(prompt, allowed_tools=[tool_name])
    passed = any(tc["name"] == tool_name for tc in result.tool_calls)
    return passed, result
