#!/usr/bin/env bash
# Release audit. Exit 0 = clean. Checks:
#   secrets      credential and token shapes in any text file
#   hosts        every URL host is on the allowlist below
#   binary       no compiled or binary files in the tree
#   prose        documentation carries no operational narrative
set -uo pipefail
cd "$(dirname "$0")"
# Local environments and new-run output are not part of the release tree.
SKIP_DIRS=(--exclude-dir=.venv --exclude-dir=runs --exclude-dir=.git)
fail=0

report () {  # <label> <hits>
  if [ -n "$2" ]; then echo "FAIL [$1]:"; echo "$2" | sed 's/^/  /'; fail=1; else echo "ok   [$1]"; fi
}

TEXT=("${SKIP_DIRS[@]}" --include='*.py' --include='*.json' --include='*.md' --include='*.txt' --include='*.cff'
      --include='*.sh' --include='*.toml' --include='*.yml' --include='*.yaml' --include='Makefile')

# Credential shapes: GitHub tokens, Onedata/macaroon tokens, Anthropic and OpenAI-style keys,
# the Forge key prefix, and bearer / auth-token headers carrying a value.
SECRETS='\bgh[psoru]_[A-Za-z0-9_.-]{20,}|\bolp_[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{16,}|\bsk-[A-Za-z0-9_-]{20,}|MDAx[A-Za-z0-9_-]{40,}|(Authorization|X-Auth-Token)["'"'"':= ]+(Bearer )?[A-Za-z0-9._-]{16,}'
report "secrets" "$(grep -rlE "$SECRETS" "${TEXT[@]}" . 2>/dev/null | grep -v '^./audit.sh$')"

# Personal data anywhere in the tree, recordings included: home-directory paths carrying a
# user name, e-mail addresses (the citation file's author list carries none), and private
# or overlay-network IPv4 addresses.
HOME_PATHS='/home/[A-Za-z0-9_.-]+|/Users/[A-Za-z0-9_.-]+'
EMAILS='[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
PRIV_IP='\b(10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3}|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3})\b'
report "home paths" "$(grep -rlE "$HOME_PATHS" "${TEXT[@]}" . 2>/dev/null | grep -v '^./audit.sh$')"
report "e-mails"    "$(grep -rlE "$EMAILS" "${TEXT[@]}" . 2>/dev/null | grep -v '^./audit.sh$')"
report "private IPs" "$(grep -rlE "$PRIV_IP" "${TEXT[@]}" . 2>/dev/null | grep -v '^./audit.sh$')"

# URL hosts must be on this list: the public service endpoints (Onezone, the two storage
# sites, the model endpoint), the public chart repository, license URLs, a library
# documentation link in recorded output, ORCID identifiers, this repository on GitHub,
# and reserved example domains used in tests.
ALLOW='^(cloud-pl\.data\.spice-platform\.eu|data\.spice-platform\.eu|149-156-182-166\.sslip\.io|llmlab\.plgrid\.pl|onedata\.github\.io|creativecommons\.org|wiki\.creativecommons\.org|errors\.pydantic\.dev|orcid\.org|github\.com|localhost|127\.0\.0\.1|[A-Za-z0-9.-]+\.(example|invalid))$'
hosts=$(grep -rhoE 'https?://[A-Za-z0-9.-]+' "${TEXT[@]}" . 2>/dev/null | sed -E 's#https?://##' | sort -u | grep -vE "$ALLOW")
report "hosts" "$hosts"

# Binary scan: no bytecode, archives or other non-text files inside the tree.
bins=$(find . -type f ! -path './.git/*' ! -path './.venv/*' ! -path './runs/*' \( -name '*.pyc' -o -path '*__pycache__*' -o -name '*.so' \
        -o -name '*.tar*' -o -name '*.zip' -o -name '*.whl' \) 2>/dev/null)
nontext=$(find . -type f ! -path './.git/*' ! -path './.venv/*' ! -path './runs/*' -print0 | xargs -0 file --mime-encoding 2>/dev/null \
        | grep -E ': binary$' | cut -d: -f1)
report "binary" "$(printf '%s\n%s' "$bins" "$nontext" | sed '/^$/d')"

# Operational narrative in documentation (patterns kept here so the docs never contain them).
OPS='wedge|quarantine|canary|incident|restart|degraded|provider-inactive|post-incident|re-run|amendment|pre-regist'
# License texts are standard legal wording and are excluded (e.g. "INCIDENTAL" in the disclaimer).
report "prose" "$(grep -rliE "$OPS" "${SKIP_DIRS[@]}" --include='*.md' --include='*.txt' --include='*.cff' . 2>/dev/null | grep -vE '^\./LICENSE')"

exit $fail
