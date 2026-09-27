"""Deployment settings for live runs: one JSON file, with per-key environment overrides.

The file is `config/federation.json` next to `harness/`, or the path in `HARNESS_CONFIG`.
Every key can be overridden by an environment variable named `HARNESS_` + the key in
upper case with dots replaced by underscores, e.g. `source.host` -> `HARNESS_SOURCE_HOST`.

Only non-secret values live here. Secrets are never stored in the file; the file names
the environment variables that hold them (e.g. `onezone_admin_password_env`).

Offline code paths (scoring, tests) never need a value from here beyond the defaults.
"""
from __future__ import annotations

import json
import os
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(os.path.dirname(_HERE), "config", "federation.json")

_cache: dict = {}


def _load() -> dict:
    path = os.environ.get("HARNESS_CONFIG", DEFAULT_PATH)
    if path not in _cache:
        with open(path) as fh:
            _cache[path] = json.load(fh)
    return _cache[path]


def env_name(key: str) -> str:
    return "HARNESS_" + key.upper().replace(".", "_").replace("-", "_")


def get(key: str, default: Any = None) -> Any:
    """Value for a dotted key: the environment override if set, else the file, else default."""
    override = os.environ.get(env_name(key))
    if override is not None:
        return override
    node: Any = _load()
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node
