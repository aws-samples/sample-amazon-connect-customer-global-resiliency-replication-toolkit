#!/usr/bin/env bash
set -euo pipefail

# Script root — always resolve paths relative to acgr-replication-starter-pack/
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

fail=0

# --- P5.1: No /api/health references in user-facing docs -----------------
echo "[check_docs] P5.1: /api/health references in README.md and USER-GUIDE.md"
if grep -n "/api/health" README.md USER-GUIDE.md 2>/dev/null; then
  echo "  FAIL: /api/health mentioned in docs" >&2
  fail=1
else
  echo "  OK"
fi

# --- P5.2: Documentation_Set completeness --------------------------------
echo "[check_docs] P5.2: required documentation files exist and are non-empty"
REQUIRED_DOCS=(README.md SECURITY.md CONTRIBUTING.md LICENSE)
for doc in "${REQUIRED_DOCS[@]}"; do
  if [[ ! -s "${doc}" ]]; then
    echo "  FAIL: ${doc} missing or empty" >&2
    fail=1
  fi
done
if [[ -e CHANGELOG.md && ! -s CHANGELOG.md ]]; then
  echo "  FAIL: CHANGELOG.md exists but is empty" >&2
  fail=1
fi
[[ ${fail} -eq 0 ]] && echo "  OK" || true

# --- P5.3 / P8: Inclusive-language invariant -----------------------------
echo "[check_docs] P5.3 / P8: inclusive-language invariant across docs"
BLOCKED_TERMS='master|slave|whitelist|blacklist|whiteday|blackday'
DOC_FILES=(README.md SECURITY.md CONTRIBUTING.md LICENSE)
[[ -e CHANGELOG.md ]] && DOC_FILES+=(CHANGELOG.md)

# Exclude this script's own list of blocked terms if it were ever scanned;
# the Documentation_Set above does not include check_docs.sh so we're safe.
if grep -inE "\b(${BLOCKED_TERMS})\b" "${DOC_FILES[@]}" 2>/dev/null; then
  echo "  FAIL: blocked non-inclusive term found in Documentation_Set" >&2
  fail=1
else
  echo "  OK"
fi

if [[ ${fail} -ne 0 ]]; then
  echo "[check_docs] FAIL" >&2
  exit 1
fi

echo "[check_docs] PASS"
