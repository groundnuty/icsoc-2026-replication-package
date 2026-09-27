"""Model arms: which agent models a run uses, read from one file.

`config/arms.json` (or the path in `HARNESS_ARMS`) lists the arms. Each arm has:

  name      short label
  kind      "openai-compatible" (any OpenAI-compatible chat endpoint with tool calling)
            or "anthropic-sdk" (the Claude Agent SDK; authenticates through the Claude CLI login)
  model_id  the model identifier sent to the endpoint and stored in each recording
  base_url  endpoint base URL (openai-compatible only; null otherwise)
  key_env   NAME of the environment variable holding the API key (openai-compatible only)
  api_model_id (optional) identifier sent to the endpoint when it differs from model_id
            (e.g. an endpoint-side alias); model_id stays the recorded name

No key values are ever stored in the file.
"""
from __future__ import annotations

import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(os.path.dirname(_HERE), "config", "arms.json")
KINDS = ("openai-compatible", "anthropic-sdk")
FIELDS = ("name", "kind", "model_id", "base_url", "key_env")
OPTIONAL_FIELDS = ("api_model_id",)


def load_arms(path: str = None) -> list:
    path = path or os.environ.get("HARNESS_ARMS", DEFAULT_PATH)
    with open(path) as fh:
        arms = json.load(fh)["arms"]
    names, ids = set(), set()
    for a in arms:
        missing = [f for f in FIELDS if f not in a]
        if missing:
            raise ValueError(f"arm {a!r}: missing field(s) {missing}")
        unknown = [f for f in a if f not in FIELDS + OPTIONAL_FIELDS]
        if unknown:
            raise ValueError(f"arm {a['name']!r}: unknown field(s) {unknown}")
        if a["kind"] not in KINDS:
            raise ValueError(f"arm {a['name']!r}: kind must be one of {KINDS}")
        if a["kind"] == "openai-compatible" and not (a["base_url"] and a["key_env"]):
            raise ValueError(f"arm {a['name']!r}: openai-compatible arms need base_url and key_env")
        if a["name"] in names or a["model_id"] in ids:
            raise ValueError(f"arm {a['name']!r}: duplicate name or model_id")
        names.add(a["name"])
        ids.add(a["model_id"])
    return arms


def build_leg(arm: dict):
    """The model leg for one arm (imports the model adapters on first use)."""
    try:
        from . import panel
    except ImportError:
        import panel
    if arm["kind"] == "openai-compatible":
        return panel.ForgeOpenAILeg(model_id=arm["model_id"], base_url=arm["base_url"],
                                    key_env=arm["key_env"], api_model_id=arm.get("api_model_id"))
    return panel.AnthropicSDKLeg(model_id=arm["model_id"])
