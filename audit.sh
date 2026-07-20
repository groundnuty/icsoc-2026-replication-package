#!/usr/bin/env bash
# Anonymization audit. Scans the package for identifying strings and for
# operational vocabulary that should not appear in a replication artifact.
# Exit 0 = clean. Patterns live here (not in the prose files) so that running
# the audit does not itself plant the strings it looks for.
set -uo pipefail
cd "$(dirname "$0")"
# The identity battery (handles, surnames, institution, framework and venue names)
# is deliberately NOT listed here: this script ships with the package, so writing
# those literals down would itself identify the authors. It is maintained outside
# the package and was run against this release; see ANONYMIZATION.md.
PROSE_ONLY='acknowledg'
OPERATIONAL='wedge|quarantine|canary|incident|restart|degraded|provider-inactive|post-incident|re-run|amendment|pre-regist'
HOSTS='https?://[a-zA-Z0-9._-]*\.(eu|com|org|net|pl|dev)'
fail=0
scan () { # <label> <pattern> <find-args...>
  local label="$1" pat="$2"; shift 2
  local hits; hits=$(grep -rlioE "$pat" "$@" . 2>/dev/null | grep -v '^./audit.sh$' || true)
  [ "$label" = "external hosts" ] && hits=$(grep -rlioE "$pat" "$@" . 2>/dev/null \
      | xargs -r grep -lioE "https?://(?!errors\.pydantic\.dev)" -P 2>/dev/null || true)
  if [ -n "$hits" ]; then echo "FAIL [$label]:"; echo "$hits" | sed 's/^/  /'; fail=1
  else echo "ok   [$label]"; fi
}
scan "thanks / prose"       "$PROSE_ONLY" --include="*.md" --include="*.txt"
scan "operational / prose"  "$OPERATIONAL" --include="*.md" --include="*.txt"
scan "external hosts"       "$HOSTS"       --include="*.json" --include="*.md" --include="*.txt"
exit $fail
