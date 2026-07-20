# Verification

Transcript of a clean-directory run.

```
$ python3 --version
Python 3.14.6

$ make tables
python3 regenerate.py
populations: healthy 56 | fault 56 | deletion 56 | gated 56 | gated-fault 56
  wrote outputs/tab2-readback-classes-body.tex
  wrote outputs/tab3-placement-misjudgment-body.tex
  wrote outputs/tab5-deletion-misjudgment-body.tex
  wrote outputs/tab6-attribution-body.tex
  wrote outputs/tab7-rescue-mechanism-body.tex
  wrote outputs/tab8-cost-body.tex
  wrote outputs/tab9-gate-outcomes-body.tex
  wrote outputs/tab10-deadline-sensitivity-body.tex
  wrote outputs/tab11-cost-dollars-body.tex

Done. Regenerated bodies are in outputs/ — compare against the corresponding tables in the paper.

$ ./audit.sh
ok   [identity / prose]
ok   [operational / prose]
ok   [identity / data]
ok   [external hosts]
exit=0
```

The nine regenerated bodies in `outputs/` were compared byte for byte against the
corresponding table bodies in the submitted paper: no differences.
