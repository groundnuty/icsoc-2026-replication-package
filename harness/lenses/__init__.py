"""The offline verification lenses.

Every lens in this package is a PURE function of one recording (a plain
dict matching `harness/schema.py`'s §3 shape) — no federation contact, no
model calls, no network, fully re-derivable.

Modules:
    - `competence.py` — the shared A-axis (`agent_competence`), one function
      used by every lens.
    - `a0.py`   — naive deployment default (A0).
    - `a1.py`   — classical intent-agnostic SLA monitoring (A1).
    - `a3a.py`  — contract-aware, convergence-naive (A3a).
    - `a3b.py`  — convergence-aware two-axis verifier + the A3b evidential
      emission procedure (A3b).

    - `a2.py`   — the transcript judge's prompt rendering and verdict parsing (A2).

Stdlib-only. No top-level side effects on import.
"""
from __future__ import annotations
