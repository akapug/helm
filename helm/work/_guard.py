"""helm work — the guard cluster: the composed deterministic shared-tree
rail (hook templates, snapshots, scope check, transactional install).
Moved verbatim from the pre-split helm/work.py.
"""
import fcntl
import os
import shlex
import stat
import tempfile

from ._lanes import _git
from ._gc import _base


MANAGED_HOOK_MARKER = "# helm work managed hook:"
LEGACY_HOOK_MARKERS = ("# helm work ref-guard", "# helm work guard")


REF_GUARD_HOOK = """#!/bin/sh
# helm work managed hook: reference-transaction v4
# Existing executable hook, when present, runs first with the exact same stdin.
base=%(base)s
user_hook=%(user_hook)s

# THE PEEK DOOR. `git worktree add --detach` births the NEW worktree's HEAD
# in MAIN-TREE context: stdin says `0000.. <sha> HEAD` with git-dir == the
# common dir — measured 2026-08-01, byte-identical on stdin to detaching the
# shared checkout's own HEAD (`git checkout --detach` presents the exact same
# line). The stdin line cannot discriminate; the HALF-BORN ADMIN DIR can: at
# `prepared` time `$common/worktrees/<name>/` already carries `gitdir`
# (naming the new checkout path) but no HEAD yet, and no other operation
# leaves that shape. STRUCTURAL allowance, no env:
#   * bare sha only — a `ref:refs/heads/*` value (any branch checkout, incl.
#     `worktree add <path> <branch>`) is not a peek and still refuses;
#   * EVERY half-born admin must point inside `<toplevel>-wt/peeks/`; one
#     stray concurrent birth refuses ALL (fail toward refusal — a spurious
#     refusal retries, a spurious pass detaches the integrator's tree);
#   * no half-born admin at all = not a worktree birth = the refusal stands.
# Paths are compared AS SPELLED against `<toplevel>-wt/peeks/` — `helm work
# peek` always mints from the canonical root, and an aliased/symlinked
# spelling REFUSES loudly rather than this hook growing a realpath walk in
# sh (fail toward refusal; a refused peek retries from the canonical root).
# The as-spelled compare is sound ONLY once a `..` component is refused first
# (kimi, 2026-08-02): a gitdir like `$top-wt/peeks/../../escape/.git` STARTS
# WITH the sanctioned prefix yet RESOLVES OUTSIDE the peek area — the prefix
# match passes while the birth lands in a sibling of the repo. Git normally
# normalizes the path it writes, but the guard must not lean on that; a
# crafted or aliased half-born admin can carry the traversal literally. So
# `..` is refused BY NAME below — the normalization the prefix check needs,
# done by rejection (no realpath walk, and no dependence on the target
# existing yet at `prepared` time).
helm_work_peek_birth() {
  case "$1" in ref:*|""|*[!0-9a-f]*) return 1 ;; esac
  top=$(git rev-parse --show-toplevel 2>/dev/null) || return 1
  [ -n "$top" ] || return 1
  births=0
  for d in "$common/worktrees"/*/; do
    [ -f "${d}gitdir" ] || continue
    [ -f "${d}HEAD" ] && continue
    births=$((births+1))
    g=$(cat "${d}gitdir" 2>/dev/null)
    case "$g" in
      ..|../*|*/..|*/../*)
        echo "[helm work] REFUSED: worktree birth gitdir carries a '..' path component ($g) — traversal out of $top-wt/peeks/ is refused" >&2
        return 1 ;;
    esac
    case "$g" in
      "$top-wt/peeks/"*) ;;
      *) return 1 ;;
    esac
  done
  [ "$births" -gt 0 ]
}

helm_work_ref_guard() {
  [ "$1" = "prepared" ] || return 0
  [ "$HELM_WORK_INTEGRATOR" = "1" ] && return 0
  [ "$HELM_WORK_CLAIM" = "1" ] && return 0

  git_dir=$(git rev-parse --path-format=absolute --git-dir 2>/dev/null) || {
    echo "[helm work] REFUSED: cannot identify the invoking git directory" >&2
    return 1
  }
  common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || {
    echo "[helm work] REFUSED: cannot identify the common git directory" >&2
    return 1
  }
  current=$(git symbolic-ref -q HEAD 2>/dev/null || :)
  main_tree=0
  [ "$git_dir" = "$common" ] && main_tree=1
  zero=0000000000000000000000000000000000000000
  registered_ready=0

  while IFS=' ' read -r old new ref; do
    case "$ref" in
      HEAD)
        if [ "$main_tree" = "1" ] && [ "$new" != "ref:refs/heads/$base" ] && \\
           ! helm_work_peek_birth "$new"; then
          target_branch=${new#ref:refs/heads/}
          echo "[helm work] REFUSED: shared checkout HEAD must stay on '$base'" >&2
          if [ "$target_branch" != "$new" ]; then
            echo "[helm work] claim a private room instead: helm work claim $target_branch" >&2
          else
            echo "[helm work] claim a private room instead: helm work claim <lane>" >&2
          fi
          echo "[helm work] read-only look at a commit: helm work peek <committish>" >&2
          echo "[helm work] integrator override: HELM_WORK_INTEGRATOR=1" >&2
          return 1
        fi ;;
      refs/heads/*)
        branch=${ref#refs/heads/}
        if [ "$ref" != "$current" ] || [ "$new" = "$zero" ]; then
          if [ "$registered_ready" = "0" ]; then
            registered=$(git worktree list --porcelain) || {
              echo "[helm work] REFUSED: cannot verify worktree occupancy for '$branch'" >&2
              return 1
            }
            registered_ready=1
          fi
          if printf '%%s\n' "$registered" | grep -Fqx "branch $ref"; then
            echo "[helm work] REFUSED: '$branch' is OCCUPIED by a registered worktree" >&2
            echo "[helm work] move/release that room before mutating its branch" >&2
            echo "[helm work] integrator override: HELM_WORK_INTEGRATOR=1" >&2
            return 1
          fi
        fi
        actual_old=$(git rev-parse --verify "$ref" 2>/dev/null || :)
        if [ "$main_tree" = "1" ] && [ -z "$actual_old" ] && \
           [ "$new" != "$zero" ] && [ "$ref" != "refs/heads/$base" ]; then
          echo "[helm work] REFUSED: '$branch' may not be created in the shared checkout" >&2
          echo "[helm work] claim its room: helm work claim $branch" >&2
          echo "[helm work] integrator override: HELM_WORK_INTEGRATOR=1" >&2
          return 1
        fi
        if [ "$main_tree" = "1" ] && [ "$ref" = "refs/heads/$base" ] && \
           [ -n "$actual_old" ] && [ "$new" != "$zero" ] && \
           [ "$actual_old" != "$new" ] && \
           ! git merge-base --is-ancestor "$actual_old" "$new"; then
          echo "[helm work] REFUSED: non-fast-forward update of shared '$base'" >&2
          echo "[helm work] integrator override: HELM_WORK_INTEGRATOR=1" >&2
          return 1
        fi
        # THE INVERSION, closed. Until now this arm refused only a NON-ff
        # update, so a PLAIN COMMIT onto '$base' — always fast-forward — fell
        # straight through, and the guard refused `git branch foo` (creates
        # nothing anyone can lose) while permitting the one operation that
        # silently forks the tree nine seats read. Measured three times on
        # 2026-08-03: a docs commit that stopped the ff for ~80 minutes, then
        # two more within the hour, each found by archaeology rather than by
        # an instrument.
        #
        # THE DISCRIMINATOR IS WHERE THE NEW TIP CAME FROM, not whether it is
        # fast-forward. A SYNC moves '$base' to a commit that IS on its
        # CONFIGURED upstream; a LOCAL COMMIT moves it to one that is not.
        # Resolve that configured ref rather than guessing `origin/$base` — a
        # remote can have any name, and a missing tracking ref is UNKNOWN
        # provenance, never proof this is a solo checkout. The exact hole was
        # deterministic: keep remote `origin`, delete only origin/main, and the
        # old guard permitted the dangerous commit.
        #
        # NO REMOTES => PASS, deliberately: a solo checkout has no shared tree
        # to protect. A remote with no configured upstream, an unreadable
        # remote set, or a configured-but-missing upstream REFUSES because the
        # guard cannot prove the new tip came from the shared source.
        if [ "$main_tree" = "1" ] && [ "$ref" = "refs/heads/$base" ] && \
           [ -n "$actual_old" ] && [ "$new" != "$zero" ] && \
           [ "$actual_old" != "$new" ]; then
          provenance_error=
          if ! upstream=$(git for-each-ref --format='%%(upstream)' \
                          "refs/heads/$base"); then
            provenance_error="cannot read the configured upstream for '$base'"
          elif [ -z "$upstream" ]; then
            if ! remotes=$(git remote 2>/dev/null); then
              provenance_error="cannot read configured remotes"
            elif [ -n "$remotes" ]; then
              provenance_error="'$base' has remotes but no configured upstream"
            fi
          elif ! git rev-parse --verify --quiet "$upstream" >/dev/null 2>&1; then
            provenance_error="configured upstream $upstream is unavailable"
          elif ! git merge-base --is-ancestor "$new" "$upstream"; then
            provenance_error="'$new' is not on $upstream — this is a LOCAL COMMIT onto shared '$base', not a sync"
          fi
          if [ -n "$provenance_error" ]; then
            echo "[helm work] REFUSED: $provenance_error" >&2
            echo "[helm work] the shared checkout is read-mostly: claim a room instead: helm work claim <lane>" >&2
            echo "[helm work] landing? land from the room and push, never from here (#95)" >&2
            echo "[helm work] integrator override: HELM_WORK_INTEGRATOR=1" >&2
            return 1
          fi
        fi
        # LANDLOCK (premise landlock-one-merge-to-main-at-a-time, enforced
        # 2026-07-29 after three same-day land races — a mutex that requires
        # READING the room is not a mutex: SI merged inside CD's held window
        # 8 minutes after the convention was posted, an honest miss this
        # check now makes impossible). An update of shared '$base' REFUSES
        # while landlock:helm is held by a DIFFERENT seat. Edge policy, and
        # every word of it is deliberate — this is a race-narrower, NOT an
        # auth gate: no claim held = PASS (holding is opt-in; the guard is
        # against merging PAST someone's held claim, never a demand that
        # lands serialize on claiming first); claims file unreadable
        # (boot-time absent tmpfs, mid-rewrite) = PASS WITH A LOUD WARNING —
        # a race-narrower that bricked all lands on tmpfs loss would be a
        # worse failure than the race; seat identity unresolvable =
        # PASS with the same warning, same reason. HELM_LANDLOCK=0 disables
        # THIS check alone, never the rest of the guard.
        if [ "$main_tree" = "1" ] && [ "$ref" = "refs/heads/$base" ] && \
           [ "$HELM_LANDLOCK" != "0" ]; then
          claim_check=$(HELM_LANDLOCK_CLAIMS="${HELM_LANDLOCK_CLAIMS:-/dev/shm/helm-chat/.claims.json}" \
            python3 - "$HELM_CHAT_NAME" <<'PYEOF' 2>&1
import json, os, re, sys
me, path = sys.argv[1], os.environ["HELM_LANDLOCK_CLAIMS"]
# the home.chat_name law, mirrored: a seat name is [A-Za-z0-9._-] — an env
# value carrying anything else is not an identity, it is noise (or an
# injection attempt aimed at the refusal line), so it resolves to None
me = me if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", me or "") else None
# $HELM_CHAT_NAME is empty on every claude-direct seat — the resting state,
# not an edge. The claim WRITER already records the ambient harness session,
# and refresh/release bind that exact value. Read the same immutable input
# directly rather than importing helm.seats from the candidate worktree: a
# change being landed must never supply code to its own refusal decision.
session = next((os.environ.get(k) for k in
                ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID",
                 "CODEX_SESSION_ID") if os.environ.get(k)), None)
try:
    with open(path) as f:
        c = json.load(f)
except FileNotFoundError:
    sys.exit(0)                      # no claims file = nobody holds anything
except Exception as e:
    print("WARN unreadable: %%s" %% e.__class__.__name__)
    sys.exit(0)
import time
row = c.get("landlock:helm")
now = time.monotonic()
if not isinstance(row, dict) or row.get("exp_mono", 0) <= now:
    sys.exit(0)                      # nobody holds it: pass, silently
holder = str(row.get("holder") or "?")
holder = holder if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", holder) else "?"
claim_session = str(row.get("session") or "") or None
# A NAME NEVER AUTHORIZES ALONE, BECAUSE THE NAME IS THE ONE THING THE
# CANDIDATE HANDS US. `me` is argv[1] = the candidate's own $HELM_CHAT_NAME,
# so `holder == me` asks the candidate who it is and believes the answer.
# `claim_session` is different in kind: the claim WRITER recorded it, and
# refresh/release bind that exact value, so matching it is corroboration by
# a party that is not the candidate.
#
# THE FAILING SHAPE IS AN ACCIDENT, NOT AN ATTACK, which is why it matters:
# an INHERITED $HELM_CHAT_NAME with a FRESH session id. Measured twice on
# 2026-08-02 — a seat ran session-unbound with its name intact, and another
# auto-bound to the wrong roster row at reboot. Either would have passed
# here against a lock a different seat held, and the hook would have said
# nothing. No adversary is required to produce it; the environment does.
#
# THE BOUNDARY, and it is the easy thing to break while fixing this: a
# NAMELESS holder whose session matches is legitimate and still passes on
# the session alone, and a nameless candidate with a foreign session still
# refuses exactly as before. This narrows only the name-alone door.
corroborated = bool(session and claim_session and claim_session == session)
name_says_mine = bool(me and holder == me)
if corroborated:
    sys.exit(0)                      # my own land window: pass, silently
left = int(row["exp_mono"] - now)
if name_says_mine and claim_session:
    # The row RECORDS a session and ours is absent or different. Two readings
    # disagree, so we refuse and SAY WHICH — an operator who sees only
    # "held by <their own name>" cannot tell this from a stale lock.
    print("REFUSE %%s %%d name-without-session" %% (holder, left))
    sys.exit(0)
if name_says_mine:
    # THE NAME-ALONE DOOR, narrowed to rows that record NO session — and it
    # is REACHABLE, not merely historical. `helm chat claim` sources the
    # session from the ambient harness env ONLY (deliberately: a roster-
    # visible sid must not be assertable through a flag), so a claim minted
    # from a bare shell, a cron unit or a systemd path records session=None.
    # Measured 2026-08-02: _env_session() -> None with no harness env.
    #
    # We still PASS, because refusing would lock a real holder out of their
    # own lock over a field their claim cannot carry — but a name-only
    # admission must never be SILENT, or the one shape that can still be
    # spoofed is also the one nobody sees. Fail open, and say which door.
    print("WARN name-only: holder=%%s %%ds — claim records no session, "
          "admitted on the declared name alone" %% (holder, left))
    sys.exit(0)
if not me and not (session and claim_session):
    print("WARN identity-unresolvable: holder=%%s %%ds" %% (holder, left))
    sys.exit(0)
print("REFUSE %%s %%d foreign-seat" %% (holder, left))
PYEOF
)
          case "$claim_check" in
            "WARN name-only"*)
              # A DIFFERENT WARNING FROM THE UNREADABLE ONE, and conflating
              # them would hide the only door a declared name can still open.
              # Identity here is not unknown — it is KNOWN AND UNCORROBORATED,
              # which is a fact the reader can act on.
              echo "[helm landlock] WARNING: $claim_check" >&2
              # DOUBLE the backslash before each backtick below. This line is
              # SHELL living inside a PYTHON string and the two disagree about
              # backslashes: the shell needs each backtick escaped (an
              # unescaped one inside double quotes is command substitution),
              # and emitting one backslash character from a non-raw Python
              # literal takes two. Backslash-backtick is not a Python escape at
              # all, so the single form warned on every FRESH compile and is
              # slated to become a hard SyntaxError — in the ref-transaction
              # hook, the worst file in the tree to fail at import.
              # This comment says "backtick" in words on purpose: the first
              # draft of it wrote the sequence out and reintroduced the exact
              # defect it was describing, inside the fix commit.
              echo "[helm landlock] this claim predates or was minted without a session (e.g. \\`helm chat claim\\` from a bare shell/cron), so the name is the only evidence that exists — re-claim from a harness session to make it corroborable" >&2 ;;
            WARN*)
              echo "[helm landlock] WARNING: claims unreadable/identity unknown ($claim_check) — passing; this narrows races, it is not an auth gate" >&2 ;;
            REFUSE*)
              # holder+seconds+reason are charset-validated in the python
              # above (reason is a single bare token); word-split is safe
              # here and only here
              set -- $claim_check
              echo "[helm landlock] REFUSED: landlock:helm is held by $2 (${3}s left)" >&2
              # THE ADVICE MUST MATCH THE REASON. "Wait for their lock" is
              # actively wrong when the lock is nominally YOURS and it is your
              # own identity that could not be corroborated — the operator
              # would sit out a timer that will never fix anything.
              if [ "$4" = "name-without-session" ]; then
                # Doubled backslash again, same reason as above: the shell must
                # see one so the variable name is PRINTED rather than expanded
                # (an expanded one would render the operator's own name back at
                # them and hide which variable is at fault), and one backslash
                # out of a non-raw Python literal takes two in.
                echo "[helm landlock] your \\$HELM_CHAT_NAME MATCHES the holder but your session does not — this is an identity binding problem, NOT a busy lock" >&2
                echo "[helm landlock] an INHERITED name with a fresh session reads exactly like this; check: helm chat seats, and helm chat seat disown <seat> <sid> --to <realseat>" >&2
                echo "[helm landlock] waiting for expiry will NOT help; the lock may genuinely be yours under a session helm cannot see" >&2
              else
                echo "[helm landlock] coordinate the release in-room, or land after it expires" >&2
              fi
              echo "[helm landlock] kill switch (this check only): HELM_LANDLOCK=0" >&2
              return 1 ;;
          esac
        fi ;;
    esac
  done
  return 0
}

if [ -x "$user_hook" ]; then
  umask 077
  input=$(mktemp "${TMPDIR:-/tmp}/helm-work-ref.XXXXXX") || {
    echo "[helm work] REFUSED: cannot capture reference transaction input" >&2
    exit 1
  }
  trap 'rm -f "$input"' 0 1 2 3 15
  cat >"$input" || exit 1
  "$user_hook" "$@" <"$input" || exit $?
  helm_work_ref_guard "$@" <"$input"
  exit $?
fi
helm_work_ref_guard "$@"
exit $?
"""


GUARD_HOOK = """#!/bin/sh
# helm work managed hook: post-checkout v2
# Existing executable hook, when present, runs first with the original args.
base=%(base)s
user_hook=%(user_hook)s

helm_work_post_guard() {
  [ "$HELM_WORK_INTEGRATOR" = "1" ] && return 0
  prev="$1"; new="$2"; flag="$3"
  [ "$flag" = "1" ] || return 0
  case "$prev" in 0000*) return 0 ;; esac
  git_dir=$(git rev-parse --path-format=absolute --git-dir 2>/dev/null) || {
    echo "[helm work] ALERT: cannot identify the invoking git directory" >&2
    return 0
  }
  common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || {
    echo "[helm work] ALERT: cannot identify the common git directory" >&2
    return 0
  }
  [ "$git_dir" = "$common" ] || return 0
  cur=$(git symbolic-ref -q HEAD 2>/dev/null || :)
  [ "$cur" = "refs/heads/$base" ] && return 0
  branch=${cur#refs/heads/}
  if [ "$prev" = "$new" ] && [ -n "$cur" ]; then
    git symbolic-ref HEAD "refs/heads/$base" || return 0
    echo "[helm work] shared checkout is the integrator's tree — healed back" >&2
    echo "[helm work] to $base; your branch '$branch' survives — work on it:" >&2
    echo "[helm work]   helm work claim $branch" >&2
    command -v helm >/dev/null 2>&1 && helm chat post \
      "@integrator guard healed 'checkout -b $branch' in the shared checkout" \
      >/dev/null 2>&1
  else
    echo "[helm work] ALERT: shared checkout left $base ($prev -> $new) —" >&2
    echo "[helm work] not healed (content switch); this is the integrator's tree." >&2
    command -v helm >/dev/null 2>&1 && helm chat post \
      "@integrator shared checkout switched off $base — inspect" \
      >/dev/null 2>&1
  fi
  return 0
}

user_rc=0
if [ -x "$user_hook" ]; then
  "$user_hook" "$@" || user_rc=$?
fi
helm_work_post_guard "$@"
[ "$user_rc" = "0" ] || exit "$user_rc"
exit $?
"""


NEVER_TRACK_HOOK = """#!/bin/sh
# helm work managed hook: pre-commit v3
# Existing executable hook, when present, runs first with the original args.
#
# The staged-set instruments share this timing seam. The vacuous-assertion
# rung WARNS on tests that can pass without proving a non-empty effect. The
# conflict-marker rung REFUSES staged merge-conflict marker lines. The
# hardcode rung WARNS on context-identity baked into portable logic. The
# never-track scanner REFUSES private paths/content. All scanners are SNAPSHOT
# beside the shared hook at install time, so a lane cannot edit its checked-out
# detector or disappear and thereby neuter every worktree's guard.
#
# EACH RUNG'S SKIP AND EACH RUNG'S ABSENCE ISOLATE TO ITSELF (v3). The v2
# never-track skip line `exit 0`ed the WHOLE hook from above the hardcode
# rung, and a missing never-track snapshot did the same — one law's
# documented bypass silently disarmed an unrelated law. Rungs now run
# warn-to-refuse with never-track LAST; only the last rung may exit clean.
scanner=%(scanner)s
vacuous=%(vacuous)s
conflict=%(conflict)s
docref=%(docref)s
hardcode_rung=%(hardcode_rung)s
inflight_rung=%(inflight_rung)s
lane_discipline=%(lane_rung)s
user_hook=%(user_hook)s

if [ -x "$user_hook" ]; then
  "$user_hook" "$@" || exit $?
fi
# TWO RUNGS RUN HERE AND BOTH DOCSTRINGS CLAIMED TO BE FIRST, so the order
# is stated rather than inherited: VENUE before TIMING. Both are hard
# refusals, so neither can mask the other's protection and the order is a
# UX call, not a correctness one — but a commit in the WRONG TREE should
# not first be told a gate is running, because that sends the author to
# investigate a suite when their real problem is where they are standing.
# The lane-discipline rung REFUSES a commit that ORIGINATES work on the shared
# checkout's base branch — no lane, no gate, no verdict (2026-08-03, measured:
# two other seats' lanes then inherited an unreviewed commit, and an APPROVE on
# either tree would have covered it). It is a VENUE check, not a content one,
# so it runs FIRST: in the wrong tree every content advisory below is noise.
# A FOLD is untouched — `git merge` builds through pre-merge-commit and never
# reaches here, and a conflicted fold carries MERGE_HEAD which the rung admits.
# Its skip is its OWN (HELM_LANE_DISCIPLINE_SKIP=1) and guards ONLY this block.
if [ "$HELM_LANE_DISCIPLINE_SKIP" != "1" ]; then
  if [ -f "$lane_discipline" ]; then
    python3 "$lane_discipline" --staged || exit $?
  else
    echo "[helm lane-discipline] WARNING: rung missing at $lane_discipline —" >&2
    echo "[helm lane-discipline] shared-checkout venue check SKIPPED; reinstall: helm work install-guard --apply" >&2
  fi
fi

# THE IN-FLIGHT GATE RUNG REFUSES, and it is FIRST because it is the only
# rung about the COMMIT'S TIMING rather than its CONTENT: a commit here moves
# HEAD under a running suite whose receipt already recorded the OLD head, so
# the receipt describes no single tree and bind() refuses it afterward. Three
# seats paid for that inside one hour on 2026-08-03 — the bracket rule catches
# it AFTER; this is the cheap BEFORE.
#
# ROOM-SCOPED BY CONSTRUCTION. The marker lives in THIS room's .git, so no
# filtering can mistake another room's gate for this one — `gatelock:<project>`
# records the HOLDER and never the REPO (measured), so a lock-based guard would
# refuse every commit everywhere while any seat gated anywhere.
#
# A STALE MARKER IS NOT A GATE: the reader treats a dead pid as no gate, so a
# killed runner cannot wedge this room shut. Its skip is its OWN, and its
# ABSENCE isolates to itself per the v3 rung law.
if [ "$HELM_INFLIGHT_GATE_SKIP" != "1" ]; then
  if [ -f "$inflight_rung" ]; then
    python3 "$inflight_rung" || exit $?
  else
    echo "[helm in-flight-gate] WARNING: scanner missing at $inflight_rung —" >&2
    echo "[helm in-flight-gate] commit-during-gate check SKIPPED; reinstall: helm work install-guard --apply" >&2
  fi
fi
if [ -f "$vacuous" ]; then
  python3 "$vacuous" --staged || true
else
  echo "[helm vacuous-assertion-rung] WARNING: scanner missing at $vacuous —" >&2
  echo "[helm vacuous-assertion-rung] staged-test advisory SKIPPED; reinstall: helm work install-guard --apply" >&2
fi
# The conflict-marker rung REFUSES: a marker line inside a docstring or a
# markdown file survives every parser and suite (measured 2026-08-01,
# tests/test_vcs.py carried a whole block through commit+push+review), so the
# add->commit seam is the only instrument that can see the shape. Its skip is
# its OWN (HELM_CONFLICT_MARKER_SKIP=1) and guards ONLY this block.
if [ "$HELM_CONFLICT_MARKER_SKIP" != "1" ]; then
  if [ -f "$conflict" ]; then
    python3 "$conflict" --staged || exit $?
  else
    echo "[helm conflict-marker] WARNING: scanner missing at $conflict —" >&2
    echo "[helm conflict-marker] staged conflict-marker scan SKIPPED; reinstall: helm work install-guard --apply" >&2
  fi
fi
# THE CITATION RUNG. Same law as never-track and the same position argument:
# refuse an unaccounted cited token BEFORE it becomes history. It was a
# GATE-only check, which made the loop for a one-character slip ~8 minutes on
# a cap-2 node -- MEASURED 36 gate runs and 229 minutes burned 2026-08-01..04,
# three seats in one night, two of them AFTER reporting it. REFUSING (a dead
# citation is history the moment it commits) with its OWN skip.
if [ "$HELM_DOCREF_SKIP" != "1" ]; then
  if [ -f "$docref" ]; then
    python3 "$docref" --staged || exit $?
  else
    echo "[helm docref] WARNING: scanner missing at $docref —" >&2
    echo "[helm docref] staged citation scan SKIPPED; reinstall: helm work install-guard --apply" >&2
  fi
fi
# The hardcode rung rides the same moment: a staged-diff WARNER for
# context-identity baked into portable logic (owner canon
# `hardcode-is-eventual-failure`). WARN-only by design — it never refuses a
# commit, so it runs BEFORE the refusing never-track scan and its output
# cannot be confused with never-track's enumerated block. Missing rung is a
# no-op; it sits ABOVE never-track's skip so that skip cannot disarm it.
if [ -f "$hardcode_rung" ]; then
  python3 "$hardcode_rung" --staged || true
fi
# never-track is LAST, so its skip and its missing-snapshot exit reach
# nothing but itself.
[ "$HELM_NEVER_TRACK_SKIP" = "1" ] && exit 0
if [ ! -f "$scanner" ]; then
  echo "[helm never-track] WARNING: scanner missing at $scanner —" >&2
  echo "[helm never-track] staged-set scan SKIPPED; reinstall: helm work install-guard --apply" >&2
  exit 0
fi
exec python3 "$scanner" --staged
"""


def hook_path(root, name="post-checkout"):
    """The hook Git will actually execute, including a repo-local hooksPath."""
    rc, out, _err = _git(root, "rev-parse", "--path-format=absolute",
                         "--git-path", "hooks")
    if rc == 0 and out:
        return os.path.join(out, name)
    rc, out, _err = _git(root, "rev-parse", "--path-format=absolute",
                         "--git-common-dir")
    gitdir = out if rc == 0 and out else os.path.join(root, ".git")
    return os.path.join(gitdir, "hooks", name)


HOSTPATH_PUSH_HOOK = """#!/bin/sh
# helm work managed hook: pre-push v2
# Existing executable hook, when present, runs first with the original args.
#
# The host-path push guard — 2026-07-29: the CLIProxyAPI fork carried three
# literal home-directory paths inside a test that ASSERTED they were absent,
# and the forbidden list necessarily contained what it forbids — THE GUARD
# BECAME THE LEAK. The fix is PATTERN-based (regex over host-path shapes),
# and this pre-push hook generalises the guard to EVERY repo we push.
#
# v2 — "$@" IS THE WHOLE CHANGE. Git hands pre-push the remote NAME and URL
# as $1 $2; v1 dropped them, so the scanner visibility-checked `origin`
# regardless of where the push was going. Measured 2026-08-06: a push to a
# SECOND remote printed "remote 'origin' is private — scan skipped". Both
# remotes were private that day; against a public second remote the guard
# would skip the exact push it exists to refuse.
scanner=%(scanner)s
user_hook=%(user_hook)s

if [ -x "$user_hook" ]; then
  "$user_hook" "$@" || exit $?
fi
[ "$HELM_HOSTPATH_SKIP" = "1" ] && exit 0
if [ ! -f "$scanner" ]; then
  echo "[helm hostpath] WARNING: scanner missing at $scanner —" >&2
  echo "[helm hostpath] host-path scan SKIPPED; reinstall: helm work install-guard --apply" >&2
  exit 0
fi
exec python3 "$scanner" --pre-push "$@"
"""


GUARD_HOOKS = (("reference-transaction", REF_GUARD_HOOK),
               ("post-checkout", GUARD_HOOK),
               ("pre-commit", NEVER_TRACK_HOOK),
               ("pre-push", HOSTPATH_PUSH_HOOK))


def _scanner_path():
    """The source nevertrack.py whose bytes the installer snapshots."""
    from .. import nevertrack
    return os.path.abspath(nevertrack.__file__)


def _vacuous_assertion_rung_path():
    """The source staged-test advisory whose bytes the installer snapshots."""
    from .. import vacuous_assertion
    return os.path.abspath(vacuous_assertion.__file__)


def _hostpath_scanner_path():
    """The source host-path scanner whose bytes the installer snapshots."""
    from .. import hostpath_guard
    return os.path.abspath(hostpath_guard.__file__)


def _inflight_rung_path():
    """The installing helm's own inflight_gate.py, baked into the pre-commit
    hook — same canonical-scanner law as the others: a lane worktree cannot
    edit or delete the rung that polices its own commit."""
    from .. import inflight_gate
    return os.path.abspath(inflight_gate.__file__)


def _hardcode_rung_path():
    """The installing helm's own hardcode.py, baked into the pre-commit hook
    — same canonical-scanner law as nevertrack (a lane worktree cannot neuter
    the rung for its own commit)."""
    from .. import hardcode
    return os.path.abspath(hardcode.__file__)


def _docref_rung_path():
    """The citation rung: refuse an unaccounted cited token BEFORE it becomes
    history. It carries the REGISTRY (LEDGER_CITED/SKIP) that
    tests/test_docstring_refs imports back, so there is one registry with two
    readers rather than a copy that drifts."""
    from .. import docref_guard
    return os.path.abspath(docref_guard.__file__)


def _conflict_marker_rung_path():
    """The source conflict-marker scanner whose bytes the installer snapshots."""
    from .. import conflict_marker
    return os.path.abspath(conflict_marker.__file__)


def _lane_discipline_rung_path():
    """The source lane-discipline rung whose bytes the installer snapshots —
    same canonical-scanner law as nevertrack: the rung that judges whether a
    commit belongs in the shared checkout must not be the copy the committing
    lane can edit."""
    from .. import lane_discipline
    return os.path.abspath(lane_discipline.__file__)


def _path_snapshot(path):
    """A restorable hook node. Symlinks and executable mode are semantic."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return ("absent", None, None)
    if stat.S_ISLNK(st.st_mode):
        return ("symlink", os.readlink(path), None)
    if stat.S_ISREG(st.st_mode):
        with open(path, "rb") as f:
            return ("file", f.read(), stat.S_IMODE(st.st_mode))
    return ("other", None, stat.S_IMODE(st.st_mode))


def _put_snapshot(path, snap):
    """Atomically put one file/symlink; chmod happens before visibility."""
    kind, value, mode = snap
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    if kind == "absent":
        if os.path.lexists(path):
            os.unlink(path)
        return
    fd, tmp = tempfile.mkstemp(prefix=".helm-work-hook-", dir=parent)
    try:
        if kind == "file":
            with os.fdopen(fd, "wb", closefd=False) as f:
                f.write(value)
                f.flush()
                os.fsync(f.fileno())
            os.fchmod(fd, mode)
        elif kind == "symlink":
            os.close(fd)
            fd = None
            os.unlink(tmp)
            os.symlink(value, tmp)
        else:
            raise OSError("unsupported hook node type at %s" % path)
        if fd is not None:
            os.close(fd)
            fd = None
        os.replace(tmp, path)
        dfd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if fd is not None:
            os.close(fd)
        if os.path.lexists(tmp):
            os.unlink(tmp)


def _owned_hook(snap):
    if snap[0] != "file":
        return False
    lines = snap[1].decode("utf-8", "replace").splitlines()[:8]
    return (any(line.startswith(MANAGED_HOOK_MARKER) for line in lines)
            or any(marker in lines for marker in LEGACY_HOOK_MARKERS))


def _hook_scope(root, targets):
    """Only mutate this repo's main checkout or common git directory."""
    rc, common, err = _git(root, "rev-parse", "--path-format=absolute",
                           "--git-common-dir")
    if rc != 0 or not common:
        return False, "cannot identify common git directory: " + err
    parents = {os.path.dirname(p) for p in targets}
    if len(parents) != 1:
        return False, "guard hooks resolve to different directories"
    parent = os.path.realpath(next(iter(parents)))
    anchors = (os.path.realpath(root), os.path.realpath(common))
    try:
        safe = any(os.path.commonpath((parent, a)) == a for a in anchors)
    except ValueError:
        safe = False
    if not safe:
        return False, ("effective hooksPath is outside this repo/common git dir: "
                       + parent)
    return True, parent


def _scanner_assets(root):
    """Stable installed snapshot -> source module for each hook scanner.

    The hook may be installed FROM a lane, but it must never execute FROM that
    lane: an unstaged edit would change every worktree's guard and deleting the
    room would make the estate fail open. Snapshots live beside the shared hook.
    """
    d = os.path.join(os.path.dirname(hook_path(root, "pre-commit")),
                     ".helm-scanners")
    return {os.path.join(d, "nevertrack.py"): _scanner_path(),
            os.path.join(d, "vacuous_assertion.py"):
                _vacuous_assertion_rung_path(),
            os.path.join(d, "hardcode.py"): _hardcode_rung_path(),
            os.path.join(d, "inflight_gate.py"): _inflight_rung_path(),
            os.path.join(d, "conflict_marker.py"): _conflict_marker_rung_path(),
            os.path.join(d, "lane_discipline.py"): _lane_discipline_rung_path(),
            os.path.join(d, "docref_guard.py"): _docref_rung_path(),
            os.path.join(d, "hostpath_guard.py"): _hostpath_scanner_path()}


def _guard_plan(root):
    base = _base(root)
    assets = _scanner_assets(root)
    scanner = next(p for p in assets if p.endswith("/nevertrack.py"))
    vacuous = next(p for p in assets if p.endswith("/vacuous_assertion.py"))
    hardcode_rung = next(p for p in assets if p.endswith("/hardcode.py"))
    inflight_rung = next(p for p in assets if p.endswith("/inflight_gate.py"))
    conflict = next(p for p in assets if p.endswith("/conflict_marker.py"))
    lane_rung = next(p for p in assets if p.endswith("/lane_discipline.py"))
    hostpath = next(p for p in assets if p.endswith("/hostpath_guard.py"))
    docref = next(p for p in assets if p.endswith("/docref_guard.py"))
    plan = []
    for name, template in GUARD_HOOKS:
        target = hook_path(root, name)
        user = target + ".helm-user"
        sc = hostpath if name == "pre-push" else scanner
        subs = {"base": shlex.quote(base), "user_hook": shlex.quote(user),
                "scanner": shlex.quote(sc),
                "docref": shlex.quote(docref),
                "vacuous": shlex.quote(vacuous),
                "conflict": shlex.quote(conflict),
                "hardcode_rung": shlex.quote(hardcode_rung),
                "inflight_rung": shlex.quote(inflight_rung),
                "lane_rung": shlex.quote(lane_rung)}
        script = template % subs
        plan.append({"name": name, "target": target, "user": user,
                     "script": script})
    return base, plan


def stale_guard_hooks(root):
    """[(state, name, why)] for every non-FRESH generated guard hook.

    A GUARD THAT IS LANDED BUT NOT INSTALLED IS INERT, and nothing was
    watching. MEASURED 2026-08-04: the shared checkout nine seats run was
    executing hook v3 while trunk generated v4, so v4's rules had never been
    armed there. The first detector then repeated the class in its own output:
    MISSING, unreadable, and unrenderable all returned [], indistinguishable
    from a byte-identical installed hook.

    DETECTION ONLY: installing rewrites an executable in someone's .git and
    must never be a side effect of a read pass. STALE means compared and
    different; MISSING means the planned target is absent; UNKNOWN means the
    comparison could not run. Only a readable byte-identical hook is silent.

    THE SCANNER SNAPSHOTS COUNT AS THE RAIL, and leaving them out was the same
    bug one layer in. A hook is a few lines of shell that DELEGATES to a
    snapshot under .helm-scanners — the never-track scan, the in-flight gate,
    the vacuity advisory all live there. So a scanner source can land while its
    installed copy keeps enforcing the old rules with every hook byte-identical
    and this function silent. `helm doctor`'s check_work_guard already read
    both halves (doctor.py:613); this one read only the hooks, which made it
    strictly weaker than the check it was meant to move to a louder surface.
    ONE predicate for both, so the two cannot drift apart again."""
    try:
        _base_, plan = _guard_plan(root)
    except Exception as exc:
        return [("UNKNOWN", "guard-plan",
                 "cannot render the expected hooks: %s: %s" % (
                     type(exc).__name__, exc))]
    out = []
    for p in plan:
        try:
            with open(p["target"]) as f:
                installed = f.read()
        except FileNotFoundError:
            out.append(("MISSING", p["name"],
                        "generated guard hook is not installed"))
            continue
        except OSError as exc:
            out.append(("UNKNOWN", p["name"],
                        "installed hook cannot be read: %s" % exc))
            continue
        if installed != p["script"]:
            out.append(("STALE", p["name"],
                        "installed hook differs from the one this tree "
                        "generates — it is running older rules than the "
                        "repo's"))
    for snap, source in sorted(_scanner_assets(root).items()):
        name = "scanner:" + os.path.basename(snap)
        try:
            with open(source, "rb") as f:
                want = f.read()
        except OSError as exc:
            # The SOURCE is unreadable: nothing can be concluded about the
            # snapshot, and a can't-tell is never a clean bill.
            out.append(("UNKNOWN", name,
                        "scanner source cannot be read: %s" % exc))
            continue
        try:
            with open(snap, "rb") as f:
                have = f.read()
        except FileNotFoundError:
            out.append(("MISSING", name,
                        "hook scanner is not installed — the hook delegates "
                        "to a file that is not there"))
            continue
        except OSError as exc:
            out.append(("UNKNOWN", name,
                        "installed scanner cannot be read: %s" % exc))
            continue
        if have != want:
            out.append(("STALE", name,
                        "installed scanner differs from this tree's source — "
                        "the hook runs, and enforces older rules"))
    return out


def install_guard(root, apply=False):
    """Print or transactionally install the deterministic shared-tree rail.

    reference-transaction refuses branch creation/HEAD departure in the main
    checkout and mutations of branches occupied by another worktree. The
    post-checkout hook is a last-resort pointer-only heal. pre-commit runs the
    refusing lane-discipline VENUE check first (an originated commit on the
    shared checkout's base branch), then the warn-only vacuous-assertion
    advisory, then the refusing conflict-marker rung, then the refusing
    never-track staged-set scan, in
    EVERY tree that shares these hooks — the timing leg
    the 2026-07-29 leak proved a suite-run guard cannot supply. Existing hooks are
    preserved byte-for-byte as executable-mode-aware `.helm-user` companions
    and composed before Helm. Concurrent installers serialize on the hook dir;
    any write failure rolls every changed path back to its exact prior node."""
    base, plan = _guard_plan(root)
    assets = _scanner_assets(root)
    targets = [p["target"] for p in plan]
    safe, scope = _hook_scope(root, targets)
    if not safe:
        return 1, ["helm work: REFUSED guard install — " + scope]
    if not apply:
        lines = ["# scanner snapshot %s <- %s" % (dest, src)
                 for dest, src in sorted(assets.items())]
        for p in plan:
            prior = _path_snapshot(p["target"])
            if prior[0] != "absent" and not _owned_hook(prior):
                lines.append("# preserves existing hook as %s" % p["user"])
            lines += ["# ---- %s ----" % p["target"],
                      p["script"].rstrip("\n"), ""]
        return 0, lines + [
            "helm work: DRY — would atomically install %d composed hooks "
            "(--apply installs; HELM_WORK_INTEGRATOR=1 clears the "
            "shared-tree ref rail and the lane-discipline venue rung, "
            "nothing else — every other pre-commit rung names its own "
            "one-commit skip in its refusal)"
            % len(plan)]

    rc, current, _err = _git(root, "symbolic-ref", "--short", "HEAD")
    if rc != 0 or current != base:
        return 1, ["helm work: REFUSED guard install — shared checkout HEAD is "
                   "%s, expected %s" % (current or "detached", base)]

    os.makedirs(scope, exist_ok=True)
    lockfd = os.open(scope, os.O_RDONLY)
    try:
        fcntl.flock(lockfd, fcntl.LOCK_EX)
        asset_paths = sorted(assets)
        paths = asset_paths + targets + [p["user"] for p in plan]
        before = {path: _path_snapshot(path) for path in paths}
        desired = {}
        notes = []
        for dest, source in assets.items():
            try:
                with open(source, "rb") as f:
                    desired[dest] = ("file", f.read(), 0o644)
            except OSError as exc:
                return 1, ["helm work: REFUSED guard install — scanner source "
                           "%s unreadable: %s" % (source, exc)]
        for p in plan:
            target, user = p["target"], p["user"]
            prior, preserved = before[target], before[user]
            if prior[0] == "other" or preserved[0] == "other":
                return 1, ["helm work: REFUSED guard install — unsupported hook "
                           "node at %s" % (target if prior[0] == "other" else user)]
            if not _owned_hook(prior) and prior[0] != "absent":
                if preserved[0] == "absent":
                    desired[user] = prior
                    notes.append("helm work: preserved existing %s as %s"
                                 % (target, user))
                elif prior != preserved:
                    return 1, ["helm work: REFUSED guard install — %s and its "
                               "preserved companion differ; nothing changed"
                               % target]
            desired[target] = ("file", p["script"].encode("utf-8"), 0o755)

        changed = []
        try:
            for path in asset_paths + [p["user"] for p in plan] + targets:
                want = desired.get(path)
                if want is None or before[path] == want:
                    continue
                changed.append(path)
                _put_snapshot(path, want)
        except Exception as exc:
            rollback = []
            for path in reversed(changed):
                try:
                    _put_snapshot(path, before[path])
                except Exception as restore_exc:
                    rollback.append("%s: %s" % (path, restore_exc))
            detail = "helm work: guard install failed and was rolled back — %s" % exc
            if rollback:
                detail += "; ROLLBACK FAILED: " + "; ".join(rollback)
            return 1, [detail]
        state = "updated" if changed else "already up to date"
        return 0, notes + ["helm work: guard rail %s in %s" % (state, scope),
                           "helm work: shared checkout branch creation/switch now "
                           "FAILS before mutation; occupied worktree branches are "
                           "protected (override: HELM_WORK_INTEGRATOR=1)",
                           "helm work: pre-commit now REFUSES a commit that "
                           "ORIGINATES work on '%s' in the shared checkout — no "
                           "lane, no gate, no verdict (a FOLD is untouched: "
                           "merges build through pre-merge-commit, and a "
                           "conflicted fold's MERGE_HEAD is admitted). "
                           "Integrator's own direct commits: "
                           "HELM_WORK_INTEGRATOR=1; one-commit skip for that "
                           "rung alone: HELM_LANE_DISCIPLINE_SKIP=1" % base,
                           "helm work: pre-commit now WARNS on staged vacuous tests, "
                           "REFUSES staged merge-conflict marker lines (one-commit "
                           "skip: HELM_CONFLICT_MARKER_SKIP=1), then enforces the "
                           "never-track law before any commit becomes history "
                           "(never-track one-commit skip: HELM_NEVER_TRACK_SKIP=1)"]
    finally:
        os.close(lockfd)
