# Data-only path: `make verify` checks the recordings; `make tables` regenerates every output.
# Offline check: `make test` runs the harness test suite (needs requirements-harness.txt).
PYTHON ?= python3

.PHONY: tables a6 verify test clean
tables:
	PYTHONDONTWRITEBYTECODE=1 python3 regenerate.py
	PYTHONDONTWRITEBYTECODE=1 python3 a6_table.py recordings/a6_overhead--20260926 outputs
	PYTHONDONTWRITEBYTECODE=1 python3 in_text.py
a6:
	PYTHONDONTWRITEBYTECODE=1 python3 a6_table.py recordings/a6_overhead--20260926 outputs
verify:
	PYTHONDONTWRITEBYTECODE=1 python3 checksums.py verify
test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s harness -p 'test_*.py'
clean:
	rm -f outputs/*.tex outputs/*.txt outputs/a6-*
	find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
