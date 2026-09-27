# Verification

Transcript of a run in a fresh clone of this repository, following the steps in README.md.
The offline test suite ran with network access disabled and credentials unset; the container
ran with `--network none`.

```
$ python3 --version
Python 3.14.6

$ make check
PYTHONDONTWRITEBYTECODE=1 python3 checksums.py verify
verify: 779 files, all OK
check: all 13 outputs byte-identical to the committed files

$ python3 -m venv .venv && .venv/bin/pip install -q --no-cache-dir --disable-pip-version-check -r requirements-harness.txt

$ make test PYTHON=.venv/bin/python    # network disabled, credentials unset
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s harness -p 'test_*.py'
..........................................................................................................................................................................................................................................................................................................................................................................................................................................................
----------------------------------------------------------------------
Ran 442 tests in 2.408s

OK

[real sdk leg] A3a verdict for T1: 'NotDetermined'

$ docker build -q -t consumer-agent-sla-artifact .
sha256:d41c85ed8eabb5694eabeab3f4593559525e62f603e4c67bbd4377da27015f3b

$ docker run --rm --network none consumer-agent-sla-artifact
PYTHONDONTWRITEBYTECODE=1 python3 checksums.py verify
verify: 779 files, all OK
check: all 13 outputs byte-identical to the committed files
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s harness -p 'test_*.py'
..........................................................................................................................................................................................................................................................................................................................................................................................................................................................
----------------------------------------------------------------------
Ran 442 tests in 1.844s

OK

[real sdk leg] A3a verdict for T1: 'NotDetermined'

$ ./audit.sh
ok   [secrets]
ok   [home paths]
ok   [e-mails]
ok   [private IPs]
ok   [hosts]
ok   [binary]
ok   [prose]
exit=0
```
