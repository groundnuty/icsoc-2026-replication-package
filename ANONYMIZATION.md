# Anonymization

## What was removed

The recordings were produced on a named research deployment, and the storage spaces
created by the harness were named after the submission venue. Both would identify the
authors, so the packaged copy was rewritten before release:

- the venue name, which appeared inside every generated space name and therefore in
  most file paths, was replaced with a neutral word;
- the name of the specific multi-site deployment, and the one external hostname that
  appeared in recorded tool results, were replaced with neutral placeholders;
- author names, institution names, repository and account names, and any thanks or
  funding section do not appear anywhere in the package (checked, not assumed).

The substitutions are uniform across the whole package, so cross-references between
paths, prompts and recorded results remain consistent. **The rewrite touches only
identifying strings — no measured value, timestamp, verdict or count was altered.**
This was verified directly: the regenerated tables are byte-identical before and after
the rewrite.

Site labels (such as the two-letter names distinguishing one site from another) are
kept, because they carry experimental meaning and identify no one.

## What is not in the package

Operational records of how the runs were conducted — scheduling notes, health records,
service-behaviour narratives, and the evolution of the measurement rules — are not part
of this package. The package is the data, the code that scores it, and the minimum
documentation needed to repeat that scoring.

## The audit

`./audit.sh` repeats the check: it scans the documentation and data for identifying
strings, for operational vocabulary, and for external hostnames, and exits non-zero on
any hit. It is included so a reader can repeat the audit rather than trust this note.
It carries the prose-vocabulary, operational-vocabulary and external-host batteries.

**Identity battery (externalized).** The patterns for author handles, surnames,
institution, framework and venue names are deliberately not written down anywhere in
this package. This script ships with the package, so listing those strings — in the
script or in this note — would itself identify the authors, which is the failure the
battery exists to prevent. That battery is maintained outside the package and was run
against this release; it reports no hits.

Recorded trial data retains serving-tier model prefixes in `model_leg` fields; these
label serving infrastructure, not authors, and recorded data is never edited.
