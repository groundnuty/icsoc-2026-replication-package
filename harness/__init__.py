"""Execution-grounded SLA-verification harness: runs agent trials against the storage
service, records what the agent did and what an independent verifier observed, and
scores the recordings. The core modules are standard-library only; the model adapters
(panel.py, a2_judge.py) need the packages in requirements-harness.txt.
"""
