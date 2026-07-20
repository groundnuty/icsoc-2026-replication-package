# One command regenerates every computed table body from the bundled recordings.
.PHONY: tables clean
tables:
	PYTHONDONTWRITEBYTECODE=1 python3 regenerate.py
clean:
	rm -f outputs/*.tex
	find . -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
