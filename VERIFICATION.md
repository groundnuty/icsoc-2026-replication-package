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
Ran 442 tests in 2.443s

OK

[real sdk leg] A3a verdict for T1: 'NotDetermined'

$ docker build -q -t consumer-agent-sla-artifact .
sha256:f293fc117d255657fa7d10c86cfe4012ed875a98b5e6c9f50f78667ea92add7f

$ docker run --rm --network none consumer-agent-sla-artifact
PYTHONDONTWRITEBYTECODE=1 python3 checksums.py verify
verify: 779 files, all OK
check: all 13 outputs byte-identical to the committed files
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s harness -p 'test_*.py'
..........................................................................................................................................................................................................................................................................................................................................................................................................................................................
----------------------------------------------------------------------
Ran 442 tests in 1.828s

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
