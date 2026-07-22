#!/usr/bin/env bash
# helm-session — safe session RSH (move/copy/convert agent sessions) over clustervision (cv).
#
# cv OWNS the mechanism (`cv port` = rehome to a new cwd, copy w/ a NEW id; `cv convert` = cross-harness;
# `cv export` = read). This wrapper adds the SAFETY RAILS cv doesn't force:
#   - DRY-RUN by default: cv runs with `--out <scratch>` so nothing touches real storage; you SEE the new
#     id + resume command + carried context before committing. Pass --apply to write for real.
#   - original-untouched: cv port/convert are COPIES (new id), never mutating the source. After --apply the
#     wrapper ASSERTS the original still exists with an unchanged cwd — a loud failure if not.
#   - verify-before-resume: after --apply it confirms the NEW session exists, then prints the resume cmd.
set -euo pipefail

SCRATCH="${HELM_SESSION_SCRATCH:-${TMPDIR:-/tmp}/helm-session-dryrun}"
CVBIN="${HELM_SESSION_CV_BIN:-cv}"

usage() {
  cat >&2 <<'EOF'
usage: helm-session <cmd> <session-id> [opts]
  rehome <id> --to-dir <cwd> [--to <harness>] [--no-context] [--apply]   copy a session to a new cwd (new id)
  clone  <id> --to-dir <cwd> [--apply]                                    alias of rehome (a parallel-lane copy)
  port-harness <id> --to <harness> [--cwd <cwd>] [--apply]               cross-harness convert (copy; new id)
  resurrect <id> --project-dir <worktree> [--to <harness>] [--no-context] [--apply]
                                                                          rehome an expert into its OWN worktree
                                                                          (isolated .remember channel) + emit the
                                                                          CLAUDE_PROJECT_DIR resume line
  export <id> [--format md|json|html]                                    print a session (read-only)
Default is a DRY-RUN (cv --out to a scratch dir; real storage untouched). Pass --apply to write for real;
the wrapper then verifies the NEW session exists and the ORIGINAL is unchanged before printing the resume cmd.
`resurrect` additionally REQUIRES --project-dir to be a dedicated git worktree (its ${dir}/.remember is the
expert's isolated channel), pre-creates that .remember (mv-steal defuse), and refuses unless the channel is
silent — because CLAUDE_PROJECT_DIR (not REMEMBER_DIR, which the plugin's shell resolve clobbers) is the only
working per-expert isolation lever.
Env: HELM_SESSION_SCRATCH (default $TMPDIR/helm-session-dryrun), HELM_SESSION_CV_BIN.
EOF
}

session_cwd() { "$CVBIN" show "$1" --json --range 0-0 2>/dev/null | python3 -c 'import sys,json
try: print(json.load(sys.stdin).get("cwd",""))
except Exception: print("")' 2>/dev/null || true; }

session_exists() { [ -n "$("$CVBIN" show "$1" --json --range 0-0 2>/dev/null | head -c1)" ]; }

# run cv (port|convert) either as a dry-run (--out scratch) or for real, then verify.
run_copy() { # <verb> <id> <apply> <cv-args...>
  local verb="$1" id="$2" apply="$3"; shift 3
  if ! session_exists "$id"; then echo "helm-session: source session '$id' not found" >&2; exit 4; fi
  local orig_cwd; orig_cwd="$(session_cwd "$id")"
  if [ "$apply" != "1" ]; then
    echo "helm-session: DRY-RUN (real storage untouched; pass --apply to commit)" >&2
    "$CVBIN" "$verb" "$id" "$@" --out "$SCRATCH"
    echo "helm-session: dry-run wrote a preview under $SCRATCH — inspect, then re-run with --apply" >&2
    return 0
  fi
  # original-untouched rail needs a readable baseline cwd; refuse rather than assert vacuously.
  # cv emits session-level cwd even under `--range 0-0` today, so this never trips in normal use; it
  # guards a future cv that range-gates cwd, which would make `orig_cwd == now_cwd` a silent no-op
  # ("" == "" passes). Fail loud instead.
  if [ -z "$orig_cwd" ]; then
    echo "helm-session: ABORT — cannot read source $id cwd via cv show --json; the original-untouched rail cannot be verified, refusing --apply" >&2
    exit 7
  fi
  # real apply: capture cv output, parse the new id, verify
  local out; out="$("$CVBIN" "$verb" "$id" "$@" 2>&1)" || { echo "helm-session: cv $verb failed:" >&2; echo "$out" >&2; exit 3; }
  echo "$out"
  local newid; newid="$(printf '%s' "$out" | grep -oE '\(([0-9a-f-]{8,})\)' | tr -d '()' | head -1)"
  # verify original untouched
  local now_cwd; now_cwd="$(session_cwd "$id")"
  if [ "$now_cwd" != "$orig_cwd" ]; then
    echo "helm-session: ABORT — original $id cwd changed ($orig_cwd -> $now_cwd); cv $verb should be a copy, not a move" >&2
    exit 5
  fi
  # verify new session exists
  if [ -n "$newid" ] && ! session_exists "$newid"; then
    echo "helm-session: WARNING — new session id '$newid' not resolvable via cv show (verify manually before resume)" >&2
    exit 6
  fi
  echo "helm-session: OK — original $id intact; new session ${newid:-<see output>} ready. Resume it per the cv line above." >&2
}

# --- expert resurrection: enforce per-expert channel isolation ---
# The remember-plugin SessionStart hook resolves its buffer as ${PROJECT_DIR}/.remember and IGNORES a
# REMEMBER_DIR env override on the INJECT path (only its python honors it — an upstream inconsistency). So
# the ONLY working per-expert isolation lever is a distinct PROJECT_DIR: a dedicated git WORKTREE used as
# both --cwd and CLAUDE_PROJECT_DIR, whose ${dir}/.remember is isolated by construction.

# exit 8 unless <project_dir> is a dedicated (linked) worktree, never the shared/main checkout.
assert_isolated_worktree() {
  local pd="$1"
  [ -d "$pd" ] || { echo "helm-session: PROJECT_DIR '$pd' is not a directory — create the expert's worktree first (git worktree add)" >&2; exit 8; }
  local gd; gd="$(git -C "$pd" rev-parse --git-dir 2>/dev/null)" \
    || { echo "helm-session: PROJECT_DIR '$pd' is not inside a git repo — an expert home must be a dedicated worktree of this repo" >&2; exit 8; }
  case "$gd" in
    *"/worktrees/"*) : ;;  # a LINKED worktree — its .remember is isolated from the shared buffer
    *) echo "helm-session: REFUSING — PROJECT_DIR '$pd' is the SHARED/main checkout, not a dedicated worktree; its .remember IS the shared main-session buffer (the poison). Spawn the expert in its own 'git worktree add' home." >&2; exit 8 ;;
  esac
}

# channel-verify (amended keep-gate): a linked worktree's .remember is isolated BY CONSTRUCTION (exit 8), so
# a non-empty buffer here is the expert's OWN memory — EXCEPT if the shared main-checkout buffer was copied
# in. So abort only when an injected file is byte-identical to the SHARED (main-checkout) buffer (the actual
# poison), NOT on mere non-emptiness — that survives re-resurrect into a permanent, reused home. Globs the
# whole set the SessionStart hook injects (now/recent/archive/today-*), not just now.md.
channel_has_shared_buffer() { # <project_dir> ; 0 = the shared main-checkout buffer is present (poison)
  local pd="$1" main shared f base
  main="$(git -C "$pd" worktree list --porcelain 2>/dev/null | awk '/^worktree /{print $2; exit}')"
  [ -n "$main" ] || return 1                 # can't locate the main checkout → nothing to compare
  shared="$main/.remember"
  [ "$shared" = "$pd/.remember" ] && return 1 # pd IS the main checkout (can't happen post exit-8)
  for f in "$pd"/.remember/now.md "$pd"/.remember/recent.md "$pd"/.remember/archive.md "$pd"/.remember/today-*.md; do
    [ -s "$f" ] || continue                   # absent/empty → not poison
    base="$(basename "$f")"
    [ -s "$shared/$base" ] || continue        # shared has no counterpart → this is the expert's own memory
    cmp -s "$f" "$shared/$base" && return 0    # byte-identical to the shared buffer → SHARED-BUFFER POISON
  done
  return 1
}

# native-memory re-key verify: Claude Code keys native memory off the SLUG DIR the transcript lives in
# (<projects-root>/<slug>/, slug = the cwd path with '/' and '.' -> '-'). `cv port --to-dir <wt>` writes the
# ported transcript into the WORKTREE's slug dir, so native memory follows the worktree BY CONSTRUCTION.
# This ASSERTS that actually happened (verify the pipe, not the person) instead of assuming it — the same
# discipline as the .remember channel check, made standing for native memory. Distinct exit code (10).
native_mem_slug() { printf '%s' "$1" | sed 's#[/.]#-#g'; }

native_mem_rekeyed() { # <project_dir> <projects_root> ; 0 if a ported transcript landed in the worktree slug dir
  local pd_abs slug
  pd_abs="$(cd "$1" 2>/dev/null && pwd)" || return 1
  slug="$(native_mem_slug "$pd_abs")"
  ls "$2/$slug"/*.jsonl >/dev/null 2>&1
}

resurrect() { # <id> <project_dir> <apply> <harness> <noctx>
  local id="$1" pd="$2" apply="$3" harness="$4" noctx="$5"
  assert_isolated_worktree "$pd"
  local inject="$pd/.remember"
  if [ "$apply" = "1" ]; then
    mkdir -p "$inject"   # pre-create → defuses bootstrap-dirs.sh's mv-steal of the shared .remember
    if channel_has_shared_buffer "$pd"; then
      echo "helm-session: ABORT — $inject carries the SHARED main-checkout buffer (channel poisoned): a .remember file here is byte-identical to the main checkout's. Clear it before resuming (verify the pipe, not the person)." >&2
      exit 9
    fi
  else
    echo "helm-session: DRY-RUN — would pre-create $inject (mv-steal defuse) and reject only if it carries the SHARED main-checkout buffer (channel-verify) before resume." >&2
  fi
  # rehome the session into the isolated worktree cwd (reuses the copy rails: dry-run/apply, untouched, verify)
  local cargs=(--to-dir "$pd"); [ -n "$harness" ] && cargs+=(--to "$harness"); [ -n "$noctx" ] && cargs+=("$noctx")
  run_copy port "$id" "$apply" "${cargs[@]}"
  # native-memory re-key verify: the ported transcript must land in the WORKTREE slug dir, else native
  # memory would NOT isolate per-worktree. Runs in BOTH paths: dry-run checks the scratch slug dir cv
  # wrote under --out; apply checks the real projects root. Fail loud (exit 10) on a mismatch.
  local proj_root
  if [ "$apply" = "1" ]; then proj_root="${HELM_SESSION_PROJECTS_DIR:-$HOME/.claude/projects}"; else proj_root="$SCRATCH"; fi
  if ! native_mem_rekeyed "$pd" "$proj_root"; then
    echo "helm-session: ABORT — the ported transcript did not land in the worktree native-memory slug ($proj_root/$(native_mem_slug "$(cd "$pd" 2>/dev/null && pwd)")); native memory would NOT isolate per-worktree." >&2
    exit 10
  fi
  echo "helm-session: native memory re-keyed to the worktree slug (isolated by construction)." >&2
  # emit the resume incantation WITH the working isolation lever
  echo "helm-session: resume the expert on its ISOLATED channel:" >&2
  echo "    CLAUDE_PROJECT_DIR='$pd' claude --resume <new-id>   # (new-id = the cv line above)" >&2
  echo "helm-session: channel-verify after resume — have the expert quote its injected '=== MEMORY ===' block; it MUST be empty/its-own, never the shared buffer." >&2
}

[ $# -ge 1 ] || { usage; exit 2; }
CMD="$1"; shift
case "$CMD" in
  -h|--help) usage; exit 0;;
esac
[ $# -ge 1 ] || { echo "helm-session: $CMD needs a <session-id>" >&2; usage; exit 2; }
ID="$1"; shift

APPLY=0 TO="" TO_DIR="" CWD="" FORMAT="md" NOCTX="" PROJECT_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift;;
    --to) TO="${2:-}"; shift 2;;
    --to-dir) TO_DIR="${2:-}"; shift 2;;
    --cwd) CWD="${2:-}"; shift 2;;
    --project-dir) PROJECT_DIR="${2:-}"; shift 2;;
    --format) FORMAT="${2:-}"; shift 2;;
    --no-context) NOCTX="--no-context"; shift;;
    *) echo "helm-session: unknown option: $1" >&2; usage; exit 2;;
  esac
done

case "$CMD" in
  rehome|clone)
    [ -n "$TO_DIR" ] || { echo "helm-session: $CMD needs --to-dir <cwd>" >&2; exit 2; }
    args=(--to-dir "$TO_DIR"); [ -n "$TO" ] && args+=(--to "$TO"); [ -n "$NOCTX" ] && args+=("$NOCTX")
    run_copy port "$ID" "$APPLY" "${args[@]}";;
  port-harness)
    [ -n "$TO" ] || { echo "helm-session: port-harness needs --to <harness>" >&2; exit 2; }
    args=(--to "$TO"); [ -n "$CWD" ] && args+=(--cwd "$CWD")
    run_copy convert "$ID" "$APPLY" "${args[@]}";;
  resurrect)
    [ -n "$PROJECT_DIR" ] || { echo "helm-session: resurrect needs --project-dir <expert-worktree> (its own git worktree = the isolated channel)" >&2; exit 2; }
    resurrect "$ID" "$PROJECT_DIR" "$APPLY" "$TO" "$NOCTX";;
  export)
    case "$FORMAT" in md|json|html) ;; *) echo "helm-session: --format must be md|json|html" >&2; exit 2;; esac
    "$CVBIN" export "$ID" --format "$FORMAT";;
  *) echo "helm-session: unknown command '$CMD'" >&2; usage; exit 2;;
esac
