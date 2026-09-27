# Data-only path: `make check` verifies the recordings, regenerates every output and compares it
# with the committed file, leaving outputs/ unchanged; `make verify` and `make tables` run those
# steps separately (`make tables` overwrites outputs/).
# Offline check: `make test` runs the harness test suite (needs requirements-harness.txt).
PYTHON ?= python3

.PHONY: check tables a6 verify test clean
check: verify
	@committed=$$(mktemp -d); cp outputs/* "$$committed"/; \
	$(MAKE) --no-print-directory tables > /dev/null; \
	if diff -r "$$committed" outputs; then \
	  echo "check: all $$(ls outputs | wc -l) outputs byte-identical to the committed files"; status=0; \
	else \
	  rm -rf outputs.regenerated; cp -r outputs outputs.regenerated; \
	  echo "check: outputs differ from the committed files (diff above; regenerated files kept in outputs.regenerated/)"; status=1; \
	fi; \
	rm -rf outputs; mkdir outputs; cp "$$committed"/* outputs/; rm -rf "$$committed"; exit $$status
tables:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) regenerate.py
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) a6_table.py recordings/a6_overhead--20260926 outputs
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) in_text.py
a6:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) a6_table.py recordings/a6_overhead--20260926 outputs
verify:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) checksums.py verify
test:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s harness -p 'test_*.py'
clean:
	rm -f outputs/*.tex outputs/*.txt outputs/a6-*
	find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
