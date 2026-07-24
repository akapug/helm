#!/usr/bin/env bash
# Deterministic guard tests for scripts/recall.sh — the safety-critical invariant is that EVERY
# cv invocation is constructed under a memory cgroup (the OOM premise), plus arg validation. Parse/
# scope behavior is live-smoked at review time (needs cv + a user systemd bus, not CI-portable).
# Discovered by scripts/run-agent-hook-tests.sh via agents/**/tests/test-*.sh.
set -uo pipefail

REPO="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null || echo "$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../../.." && pwd)")"
SUT="$REPO/scripts/recall.sh"
fails=0
tmpout="$(mktemp)"; trap 'rm -f "$tmpout"' EXIT

check() { # <desc> <expected-exit> <cmd...>
  local desc="$1" want="$2"; shift 2
  "$@" >"$tmpout" 2>/dev/null; local got=$?
  if [ "$got" != "$want" ]; then echo "FAIL: $desc (exit $got, want $want)"; fails=$((fails+1)); fi
}
grep_out() { # <desc> <needle>
  if ! grep -qF -- "$2" "$tmpout"; then echo "FAIL: $1 (missing: $2)"; fails=$((fails+1)); fi
}

# 1. echo-mode always constructs the cgroup cap
HELM_RECALL_ECHO=1 "$SUT" "memgc garbage collection" >"$tmpout" 2>/dev/null
grep_out "isolation: systemd-run present" "systemd-run --user --scope"
grep_out "isolation: MemoryMax cap present" "MemoryMax=4G"
grep_out "isolation: MemorySwapMax present" "MemorySwapMax=0"
grep_out "runs a cv search" "cv search"

# 2. MemoryMax is env-tunable
HELM_RECALL_ECHO=1 HELM_RECALL_MEMORY_MAX=2G "$SUT" "q" >"$tmpout" 2>/dev/null
grep_out "MemoryMax env override" "MemoryMax=2G"

# 3. semantic auto-detected from a CLUSTERVISION_HOME with an embeddings.bin, absent without
sem="$(mktemp -d)"; : > "$sem/embeddings.bin"
HELM_RECALL_ECHO=1 CLUSTERVISION_HOME="$sem" "$SUT" "q" >"$tmpout" 2>/dev/null
grep_out "semantic on when index present" "--semantic"
HELM_RECALL_ECHO=1 CLUSTERVISION_HOME="$sem/nope" "$SUT" "q" >"$tmpout" 2>/dev/null
if grep -qF -- "--semantic" "$tmpout"; then echo "FAIL: keyword fallback when no index"; fails=$((fails+1)); fi
rm -rf "$sem"

# 4. arg validation
check "missing query exits 2" 2 env HELM_RECALL_ECHO=0 "$SUT"
check "bad --scope exits 2"   2 "$SUT" --scope sideways q
check "bad --format exits 2"  2 "$SUT" --format xml q
check "unknown flag exits 2"  2 "$SUT" --nope q
check "help exits 0"          0 "$SUT" --help

# 5. --limit validation (positive integer, bounded) — checked before any cv call, so CI-deterministic
check "limit non-integer exits 2" 2 "$SUT" --limit abc q
check "limit negative exits 2"    2 "$SUT" --limit -1 q
check "limit zero exits 2"        2 "$SUT" --limit 0 q
check "limit too-large exits 2"   2 "$SUT" --limit 999999 q
HELM_RECALL_ECHO=1 "$SUT" --limit 5 q >"$tmpout" 2>/dev/null
grep_out "valid limit accepted" "--limit 5"

# 6. fail-loud: a cv/tool failure exits 3 with "recall unavailable" — never a silent 0-match exit 0.
#    /bin/false fails whether or not a user systemd bus exists (both paths -> nonzero -> exit 3).
HELM_RECALL_CV_BIN=/bin/false "$SUT" --scope global --limit 2 "anything" >/dev/null 2>"$tmpout"; got=$?
if [ "$got" != 3 ]; then echo "FAIL: cv failure must exit 3 (got $got)"; fails=$((fails+1)); fi
if ! grep -qF -- "recall unavailable" "$tmpout"; then echo "FAIL: cv failure must say 'recall unavailable'"; fails=$((fails+1)); fi

if [ "$fails" -eq 0 ]; then echo "recall guard tests: PASS"; else echo "recall guard tests: $fails FAILURE(S)"; fi
exit "$fails"
