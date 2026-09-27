# Verification

Transcript of a run in a fresh clone, following the steps in README.md.

```
$ python3 --version
Python 3.14.6

$ make verify
PYTHONDONTWRITEBYTECODE=1 python3 checksums.py verify
verify: 779 files, all OK

$ make tables
PYTHONDONTWRITEBYTECODE=1 python3 regenerate.py
recordings check: 779 files in 18 directories, as expected
populations: healthy 56 | fault 56 | deletion 56 | gated 56 | gated-fault 56 | gated-deletion 56
  wrote outputs/tab2-readback-classes-body.tex
  wrote outputs/tab3-placement-misjudgment-body.tex
  wrote outputs/tab5-deletion-misjudgment-body.tex
  wrote outputs/tab6-attribution-body.tex
  wrote outputs/tab7-rescue-mechanism-body.tex
  wrote outputs/tab8-cost-body.tex
  wrote outputs/tab9-gate-outcomes-body.tex
  wrote outputs/tab10-deadline-sensitivity-body.tex
  wrote outputs/tab11-cost-dollars-body.tex
  wrote outputs/tab-study-matrix-body.tex

Done. Regenerated table bodies are in outputs/.
PYTHONDONTWRITEBYTECODE=1 python3 a6_table.py recordings/a6_overhead--20260926 outputs
51 cell-runs -> outputs/a6-overhead-table.md, a6-cellruns.json
PYTHONDONTWRITEBYTECODE=1 python3 in_text.py
  wrote outputs/in-text-numbers.txt (72 numbers)

$ git status --porcelain outputs/    # empty: every output byte-identical to the committed file

$ python3 -m venv .venv && .venv/bin/pip install -q -r requirements-harness.txt

$ make test PYTHON=.venv/bin/python    # network disabled, credentials unset
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s harness -p 'test_*.py'
..........................................................................................................................................................................................................................................................................................................................................................................................................................................................
----------------------------------------------------------------------
Ran 442 tests in 1.833s

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
