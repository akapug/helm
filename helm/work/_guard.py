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
# WHERE A SUPERSEDED HELM HOOK GOES. Not a git hook name, so git never
# runs it, and the managed hook invokes only the .helm-user path.
RETIRED_HOOK_SUFFIX = ".helm-superseded"


REF_GUARD_HOOK = """#!/bin/sh
# helm work managed hook: reference-transaction v6
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

helm_work_refuse_head() {
  target_branch=${1#ref:refs/heads/}
  echo "[helm work] REFUSED: shared checkout HEAD must stay on '$base'" >&2
  if [ "$target_branch" != "$1" ]; then
    echo "[helm work] claim a private room instead: helm work claim $target_branch" >&2
  else
    echo "[helm work] claim a private room instead: helm work claim <lane>" >&2
  fi
  echo "[helm work] read-only look at a commit: helm work peek <committish>" >&2
  echo "[helm work] integrator override: HELM_WORK_INTEGRATOR=1" >&2
  return 1
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
  oid_head_seen=0
  oid_head_old=
  oid_head_new=
  base_update_seen=0
  base_update_old=
  base_update_new=

  while IFS=' ' read -r old new ref; do
    case "$ref" in
      HEAD)
        if [ "$main_tree" = "1" ]; then
          case "$new" in
            "ref:refs/heads/$base") ;;
            ref:*) helm_work_refuse_head "$new"; return $? ;;
            *)
              # Git 2.43 reports an ordinary symbolic-HEAD branch update
              # twice: once as an OID-valued HEAD row and once as the real
              # refs/heads/<current> row. Git 2.51+ normally reports only the
              # branch row. The OID row alone is ALSO the byte-identical shape
              # of detaching this checkout, so current symbolic-ref state at
              # prepared time cannot distinguish them. Defer the decision and
              # require the exact (old,new) pair on the base branch below.
              if ! helm_work_peek_birth "$new"; then
                [ "$oid_head_seen" = "0" ] || {
                  helm_work_refuse_head "$new"
                  return $?
                }
                oid_head_seen=1
                oid_head_old=$old
                oid_head_new=$new
              fi ;;
          esac
        fi ;;
      refs/heads/*)
        if [ "$main_tree" = "1" ] && [ "$ref" = "refs/heads/$base" ]; then
          [ "$base_update_seen" = "0" ] || {
            helm_work_refuse_head "$new"
            return $?
          }
          base_update_seen=1
          base_update_old=$old
          base_update_new=$new
        fi
        branch=${ref#refs/heads/}
        actual_old=$(git rev-parse --verify "$ref" 2>/dev/null || :)
        # pack-refs moves one logical ref across two backends. First it reports
        # 0000.. -> <current> while adding packed-refs; then <current> -> 0000..
        # while deleting the identical loose copy. Occupancy protects the
        # checked-out COMMIT, not its representation, so admit only when either
        # transaction leaves the repository's effective ref value unchanged.
        value_preserved=0
        if [ "$actual_old" = "$new" ]; then
          value_preserved=1
        elif [ "$new" = "$zero" ] && [ "$actual_old" = "$old" ]; then
          loose_old=$(cat "$common/$ref" 2>/dev/null || :)
          if [ "$loose_old" = "$old" ] && \
             grep -Fqx "$old $ref" "$common/packed-refs" 2>/dev/null; then
            value_preserved=1
          fi
        fi
        if [ "$value_preserved" = "0" ]; then
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
        fi
        if [ "$main_tree" = "1" ] && [ -z "$actual_old" ] && \
           [ "$new" != "$zero" ] && [ "$ref" != "refs/heads/$base" ]; then
          echo "[helm work] REFUSED: '$branch' may not be created in the shared checkout" >&2
          echo "[helm work] claim its room: helm work claim ${branch#lane/}" >&2
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
  if [ "$oid_head_seen" = "1" ]; then
    head_pair_matches=0
    if [ "$base_update_seen" = "1" ] && \\
       [ "$oid_head_old" = "$base_update_old" ] && \\
       [ "$oid_head_new" = "$base_update_new" ]; then
      head_pair_matches=1
    fi
    if [ "$head_pair_matches" = "0" ]; then
      # No exact base-ref twin means this is the other operation with the same
      # OID-valued HEAD shape: detaching/moving the shared checkout itself.
      helm_work_refuse_head "$oid_head_new"
      return $?
    fi
  fi
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
    command -v helm >/dev/null 2>&1 && helm chat post --to-integrator \
      "guard healed 'checkout -b $branch' in the shared checkout" \
      >/dev/null 2>&1
  else
    echo "[helm work] ALERT: shared checkout left $base ($prev -> $new) —" >&2
    echo "[helm work] not healed (content switch); this is the integrator's tree." >&2
    command -v helm >/dev/null 2>&1 && helm chat post --to-integrator \
      "shared checkout switched off $base — inspect" \
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


#: The shell every refusing hook carries so a refusal is COUNTED. Each refusing
#: rung names itself in `helm_rung` before it runs; when the hook exits
#: non-zero with a rung named, one line goes to the friction ledger through
#: `helm friction record`. An EXIT trap, so the rungs keep their own
#: exit-propagating spelling and the hook's exit status is untouched: the trap
#: calls no `exit`, and a shell leaves with the status it was already leaving
#: with. The count is silent, bounded and fail-open -- a missing or slow helm
#: changes neither the refusal nor its code. A user hook's failure names no
#: rung and is not counted. `_counted` splices it into a template where the
#: template is DEFINED, with the hook's own name as the reason token, so a
#: template still renders from exactly the keys it rendered from before. It
#: carries no `%`, so the template's `%` pass reads straight through it.
REFUSAL_COUNT_SH = """# A REFUSAL IS COUNTED: see `helm friction`. Silent, bounded, fail-open.
helm_rung=
helm_refusal_counted() {
  helm_rc=$?
  if [ "$helm_rc" != "0" ] && [ -n "$helm_rung" ] && command -v helm >/dev/null 2>&1; then
    timeout 5 helm friction record "$helm_rung" --reason %s >/dev/null 2>&1
  fi
}
trap helm_refusal_counted EXIT"""

_REFUSAL_COUNT_MARK = "@helm-refusal-count@"


def _counted(template, reason):
    """`template` with the refusal-count shell spliced in at its mark."""
    return template.replace(_REFUSAL_COUNT_MARK, REFUSAL_COUNT_SH % reason)


NEVER_TRACK_HOOK = _counted("""#!/bin/sh
# helm work managed hook: pre-commit v7
# Existing executable hook, when present, runs first with the original args.
#
# The staged-set instruments share this timing seam. The vacuous-assertion
# rung WARNS on tests that can pass without proving a non-empty effect. The
# orphaned-mock rung WARNS on a double planted where the modeled call graph
# does not reach it — the sibling half of vacuous-assertion, and the one that
# cannot go red on its own: an arm asserting an ABSENCE against a double
# nothing can invoke passes forever, because a double that never fires and a
# path that never runs produce the identical observable. The
# conflict-marker rung REFUSES staged merge-conflict marker lines. The
# hardcode rung WARNS on context-identity baked into portable logic. The
# seat-name rung REFUSES a real seat identity entering public-bound tests/. The
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
orphaned=%(orphaned)s
silent_cap=%(silent_cap)s
seatname=%(seatname)s
conflict=%(conflict)s
docref=%(docref)s
world_prose=%(world_prose)s
retired_name=%(retired_name)s
split_budget=%(split_budget)s
hardcode_rung=%(hardcode_rung)s
inflight_rung=%(inflight_rung)s
lane_discipline=%(lane_rung)s
user_hook=%(user_hook)s
@helm-refusal-count@

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
# A FOLD is untouched by THIS rung — `git merge` builds through pre-merge-commit
# and never reaches here, and a conflicted fold carries MERGE_HEAD which the
# rung admits. The fold is not unguarded: the pre-merge-commit hook runs the
# conflict-marker and never-track rungs over the merge result, and no venue rung.
# Its skip is its OWN (HELM_LANE_DISCIPLINE_SKIP=1) and guards ONLY this block.
if [ "$HELM_LANE_DISCIPLINE_SKIP" != "1" ]; then
  if [ -f "$lane_discipline" ]; then
    helm_rung=lane-discipline
    python3 "$lane_discipline" --staged || exit $?
  else
    echo "[helm lane-discipline] WARNING: rung missing at $lane_discipline —" >&2
    echo "[helm lane-discipline] shared-checkout venue check SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
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
    helm_rung=in-flight-gate
    python3 "$inflight_rung" || exit $?
  else
    echo "[helm in-flight-gate] WARNING: scanner missing at $inflight_rung —" >&2
    echo "[helm in-flight-gate] commit-during-gate check SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
  fi
fi
if [ -f "$vacuous" ]; then
  python3 "$vacuous" --staged || true
else
  echo "[helm vacuous-assertion-rung] WARNING: scanner missing at $vacuous —" >&2
  echo "[helm vacuous-assertion-rung] staged-test advisory SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
fi
# THE SILENT-CAP RUNG WARNS when a staged write persists a value the same
# function cut and left unmarked -- a slice or a *_CAP/*_MAX/*_LIMIT bound
# whose result reaches a serializing, file-writing, ledger-appending or
# publishing call with nothing in the written value naming the loss. Every
# reader downstream then holds a short value indistinguishable from a whole
# one. ADVISORY on a measured rate the rung's own banner carries: an
# identifier prefix and a label rendered short inside a human line are not
# separable from a lossy cut by shape alone, and a rung that refuses over its
# own judgement calls teaches its bypass. Its ABSENCE isolates to itself.
if [ -f "$silent_cap" ]; then
  python3 "$silent_cap" --staged || true
else
  echo "[helm silent-cap] WARNING: rung missing at $silent_cap —" >&2
  echo "[helm silent-cap] staged bounded-write advisory SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
fi
# ADVISORY, and deliberately so: this census names CANDIDATES, not proven
# orphans — it does not follow test helper methods, cross-module consumers or
# verb dispatch tables, and a hand-labelled sample of ten found nine reached
# through exactly those. A refusing rung at that precision is a wolf-crier.
if [ -f "$orphaned" ]; then
  python3 "$orphaned" --staged || true
else
  echo "[helm orphaned-mock] WARNING: scanner missing at $orphaned —" >&2
  echo "[helm orphaned-mock] staged-double advisory SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
fi
# The conflict-marker rung REFUSES: a marker line inside a docstring or a
# markdown file survives every parser and suite (measured 2026-08-01,
# tests/test_vcs.py carried a whole block through commit+push+review), so the
# add->commit seam is the only instrument that can see the shape. Its skip is
# its OWN (HELM_CONFLICT_MARKER_SKIP=1) and guards ONLY this block.
if [ "$HELM_CONFLICT_MARKER_SKIP" != "1" ]; then
  if [ -f "$conflict" ]; then
    helm_rung=conflict-marker
    python3 "$conflict" --staged || exit $?
  else
    echo "[helm conflict-marker] WARNING: scanner missing at $conflict —" >&2
    echo "[helm conflict-marker] staged conflict-marker scan SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
  fi
fi
# THE SEAT-NAME RUNG keeps a real seat identity out of public-bound tests/.
#
# ITS RULES LIVE IN helm/seatname_guard.py AND ARE NOT REPEATED HERE. This
# comment used to restate them — what it refuses, the counts, the held-back
# names — and every one of those sentences went stale as the rung changed,
# because one behaviour described in two files is two readers of one question
# and they drift exactly the way every other duplicated fact here drifts. Three
# separate false claims were caught in review before this was cut back to what
# is true OF THE HOOK.
#
# What is true of the hook: it runs at this position, its skip is its OWN
# (HELM_SEATNAME_SKIP=1) and guards ONLY this block, and a missing snapshot
# WARNS and stands down like every sibling's — the rung refuses on a broken
# measurement, never on a broken install.
if [ "$HELM_SEATNAME_SKIP" != "1" ]; then
  if [ -f "$seatname" ]; then
    helm_rung=seat-name
    python3 "$seatname" --staged || exit $?
  else
    echo "[helm seat-name] WARNING: rung missing at $seatname —" >&2
    echo "[helm seat-name] public-bound seat-identity scan SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
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
    helm_rung=docref
    python3 "$docref" --staged || exit $?
  else
    echo "[helm docref] WARNING: scanner missing at $docref —" >&2
    echo "[helm docref] staged citation scan SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
  fi
fi
# Public-bound source prose states the current property, not internal review,
# measurement, or repair chronology. The scanner owns the exact vocabulary and
# the path-identity exemptions. Its skip guards only this refusing block.
if [ "$HELM_WORLD_PROSE_SKIP" != "1" ]; then
  if [ -f "$world_prose" ]; then
    helm_rung=world-prose
    python3 "$world_prose" --staged || exit $?
  else
    echo "[helm world-prose] WARNING: scanner missing at $world_prose —" >&2
    echo "[helm world-prose] staged source-prose scan SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
  fi
fi
# A top-level name this commit retires must not stay spelled anywhere in the
# tree it produces: the consumer that reaches into the module from another
# lane is invisible to the author's focused set and red only when the train
# composes. The rung derives the retired names from the staged diff and greps
# the index. Its skip guards only this refusing block.
if [ "$HELM_RETIRED_NAME_SKIP" != "1" ]; then
  if [ -f "$retired_name" ]; then
    helm_rung=retired-name
    python3 "$retired_name" --staged || exit $?
  else
    echo "[helm retired-name] WARNING: scanner missing at $retired_name —" >&2
    echo "[helm retired-name] staged retired-name scan SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
  fi
fi
# The seats-split line budget, asked here in milliseconds instead of from a
# whole suite ten minutes away. It refuses ONLY a module this commit takes
# past the budget -- a standing debt in a file nobody touched is reported and
# never walls an unrelated lane, and a commit that SHRINKS an over-budget
# module is progress and passes. Its skip guards only this refusing block.
if [ "$HELM_SPLIT_BUDGET_SKIP" != "1" ]; then
  if [ -f "$split_budget" ]; then
    helm_rung=split-budget
    python3 "$split_budget" --staged || exit $?
  else
    echo "[helm split-budget] WARNING: rung missing at $split_budget —" >&2
    echo "[helm split-budget] staged budget check SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
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
  echo "[helm never-track] staged-set scan SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
  exit 0
fi
%(stale_check)s
helm_rung=never-track
python3 "$scanner" --staged || exit $?
""", "pre-commit")


def _stale_scanner_check(quoted_source, profile):
    """The shell that tells a committer their scanner snapshot is old.

    THE SCANNER RUNS AS A FROZEN COPY under .helm-scanners — a lane must not be
    able to edit the detector that judges it — so a scanner cure that LANDS
    never reaches a repo whose guard was installed earlier. That repo keeps
    refusing by the old rules, and the only advice its refusal offers is the
    bypass flag: the habit this guard family exists not to teach (bug class
    `guard-refuses-what-it-cannot-fix`).

    THE FAILURE MODE IT CLOSES: a repo refuses a hand-resolved merge, naming
    files every one of which its second parent already carries — the symptom
    the merge-aware parent set exists to prevent — because the snapshot it
    runs predates that cure and holds no parent logic at all. Nothing at the
    point of pain says so, so an unexplainable refusal reads as a fresh defect
    in a producer that measures correct, and the reader reaches for the skip
    flag. The estate surface already answers this (`helm doctor` reads
    `stale_guard_hooks` and reports such a repo as drift); the committer is not
    in the doctor's room.

    Same byte compare `stale_guard_hooks` runs on the scanner leg, asked where
    the commit is. WARN-only: a guard must never refuse a commit over its own
    housekeeping. SILENT when the source is not a readable file (`cmp` exit 2,
    or the path gone) — the installing checkout can legitimately have been
    retired, and a nag nobody in this repo can act on is how a guard's output
    gets taught to be ignored; `helm doctor` reports that case as UNKNOWN,
    which is a maintainer's question, not a committer's."""
    return "\n".join((
        'scanner_src=' + quoted_source,
        'if [ -f "$scanner_src" ]; then',
        '  cmp -s "$scanner" "$scanner_src"',
        '  if [ $? -eq 1 ]; then',
        '    echo "[helm never-track] WARNING: the installed scanner is NOT '
        'the one its source tree now ships:" >&2',
        '    echo "[helm never-track]   running:  $scanner" >&2',
        '    echo "[helm never-track]   source:   $scanner_src" >&2',
        # THE PROFILE IS RENDERED HERE, not left as a template key: this
        # snippet is substituted INTO the hook template as a value, so the
        # single `%` pass that expands the template never looks inside it and
        # a `%(profile)s` left here would reach the hook verbatim.
        '    echo "[helm never-track] this repo is enforcing OLDER rules than '
        'that tree — a refusal you cannot explain may already be fixed there. '
        'Refresh: helm work install-guard --apply --profile ' + profile
        + '" >&2',
        '  fi',
        'fi',
    ))


def hooks_dir(root):
    """The hooks DIRECTORY Git will actually execute from, including a
    repo-local hooksPath."""
    rc, out, _err = _git(root, "rev-parse", "--path-format=absolute",
                         "--git-path", "hooks")
    if rc == 0 and out:
        return out
    rc, out, _err = _git(root, "rev-parse", "--path-format=absolute",
                         "--git-common-dir")
    gitdir = out if rc == 0 and out else os.path.join(root, ".git")
    return os.path.join(gitdir, "hooks")


class Hooks(object):
    """The hooks directory of ONE guard evaluation, asked of git at most once
    and only when the evaluation first needs it (task/3039).

    MEASURED BEFORE: `hook_path` asked git once per hook NAME, about 41 times
    per `helm work claim`, 7,995 times in tests.test_stop_seam. An evaluation
    (`stale_guard_hooks`, one remedy, `installed_profile`, `install_guard`)
    makes one of these at its top and hands it down.

    NEVER KEPT ACROSS EVALUATIONS. `core.hooksPath` can change between two of
    them, and a directory remembered by repository path would then name hooks
    git no longer runs; the next evaluation asks again."""

    __slots__ = ("root", "_dir")

    def __init__(self, root):
        self.root = root
        self._dir = None

    def path(self, name):
        if self._dir is None:
            self._dir = hooks_dir(self.root)
        return os.path.join(self._dir, name)


def hook_path(root, name="post-checkout", hooks=None):
    """The hook Git will actually execute, including a repo-local hooksPath.
    `hooks` is the caller's evaluation (`Hooks`), when it has one."""
    return (hooks or Hooks(root)).path(name)


HOSTPATH_PUSH_HOOK = _counted("""#!/bin/sh
# helm work managed hook: pre-push v4
# Existing executable hook, when present, runs first with the original args
# and the same ref lines on stdin.
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
#
# v3 — A REFUSAL IS COUNTED. The scanner is no longer exec'd, so the EXIT
# trap below sees its status and `helm friction` can say how often it refuses.
#
# v4 — GIT WRITES THE REF LINES ONCE, AND TWO READERS NEED THEM. The user hook
# ran on the hook's own stdin, so a user hook that reads it — `git lfs
# pre-push` reads to the end, and so does any gate that inspects the refs —
# left the scanner an EMPTY stdin. Empty is the protocol for "nothing to
# push", so the scan allowed without reading a byte: pushes from an LFS repo
# printed "nothing to push" and went out unscanned. Now the hook reads stdin
# ONCE and feeds the SAME bytes to both readers: the user hook still gets the
# refs git-lfs uploads by, an up-to-date push stays empty for both, and a
# stdin the hook cannot read REFUSES instead of reaching the scanner as
# "nothing to push". The bytes live in a shell variable, not a temp file, so
# no exit path (a refusal, a skip, Ctrl-C mid-upload) can leave a file
# behind, and a full or unwritable TMPDIR cannot refuse a push. The trailing
# `x` survives the newline stripping of command substitution and is removed
# after it, so the bytes fed on are the bytes git wrote.
scanner=%(scanner)s
user_hook=%(user_hook)s
@helm-refusal-count@

if ! helm_push_stdin=$(cat && printf x); then
  helm_rung=hostpath
  echo "[helm hostpath] REFUSED: cannot read git's pre-push ref lines — nothing was scanned" >&2
  exit 2
fi
helm_push_stdin=${helm_push_stdin%%x}
helm_push_refs() { printf '%%s' "$helm_push_stdin"; }

if [ -x "$user_hook" ]; then
  helm_push_refs | "$user_hook" "$@" || exit $?
fi
[ "$HELM_HOSTPATH_SKIP" = "1" ] && exit 0
if [ ! -f "$scanner" ]; then
  echo "[helm hostpath] WARNING: scanner missing at $scanner —" >&2
  echo "[helm hostpath] host-path scan SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
  exit 0
fi
helm_rung=hostpath
helm_push_refs | python3 "$scanner" --pre-push "$@" || exit $?
""", "pre-push")


TRAILER_MSG_HOOK = _counted("""#!/bin/sh
# helm work managed hook: commit-msg v2
# Existing executable hook, when present, runs first with the original args.
#
# THE ONLY MESSAGE-TIME RUNG IN THE ESTATE, and it earns that seam narrowly.
# Every other rung reads STAGED CONTENT at pre-commit, where no message exists
# yet. This one asks whether the commit carries helm's attribution trailer --
# a FORM with no referent, not a claim about the world -- and says nothing
# about any other trailer. Read helm/trailer_rung.py before adding a second
# message-time check here; the argument that admits this one is specific to it.
#
# ITS SKIP IS ITS OWN (HELM_TRAILER_SKIP=1) and a missing snapshot WARNS
# rather than exiting the hook, so this rung can never disarm another.
trailer=%(trailer)s
user_hook=%(user_hook)s
@helm-refusal-count@

if [ -x "$user_hook" ]; then
  "$user_hook" "$@" || exit $?
fi

if [ -f "$trailer" ]; then
  helm_rung=trailer
  python3 "$trailer" "$@" || exit $?
else
  echo "[helm trailer] WARNING: rung missing at $trailer —" >&2
  echo "[helm trailer] attribution check SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
fi
exit 0
""", "commit-msg")


MERGE_COMMIT_HOOK = _counted("""#!/bin/sh
# helm work managed hook: pre-merge-commit v2
# Existing executable hook, when present, runs first with the original args.
#
# A MERGE COMMIT NEVER REACHES pre-commit. Git builds the commit of a merge it
# completes itself (`git merge --no-ff`, or any merge that is not a
# fast-forward and has no conflicts) through THIS hook, so a repo armed only at
# pre-commit lets a merge land every blob the merged branch carries with no
# scanner invoked. This hook runs the two refusing LEAK rungs over the merge
# result staged in the index: conflict-marker, then never-track last.
#
# WHAT A MERGE ADDS HERE IS WHAT THE TARGET BRANCH DID NOT ALREADY TRACK. Git
# writes MERGE_HEAD only after this hook has passed, so both scanners measure
# added-ness against HEAD — the first parent: a blob HEAD already carries is not
# named, a blob only the merged branch carries is. A fast-forward creates no
# commit and runs no hook. `--no-verify` skips this hook as it skips pre-commit.
#
# ON THE TWO LEAK RUNGS THE DOORS AGREE ON BULK AND DIFFER ON NEEDLES, MARKERS
# AND ADDRESSES, and the refusal says so. A merge git does not commit itself (--no-commit, a conflict, or
# this refused merge, which git leaves in progress) is concluded with
# `git commit` and runs pre-commit with MERGE_HEAD present. There a blob's size
# and bulk shape are judged against HEAD, exactly as here, so a blob over the
# size line that HEAD did not already carry is refused by both doors and takes
# helm-bulk=ok. Needles, conflict markers and addresses are judged there
# against EVERY parent, so content a merged parent already carries is admitted
# at that door and refused at this one. Nothing is recorded between the two.
#
# THIS DOOR RUNS ONLY THOSE TWO RUNGS. Under the rail profile pre-commit also
# runs the refusing lane-discipline (which admits a commit carrying
# MERGE_HEAD), in-flight-gate, seat-name, docref (citation), world-prose,
# retired-name and split-budget rungs, so a merge git commits itself never meets them while the
# same merge concluded with `git commit` can be refused by one of them. Under
# the leak profile both doors run the same two rungs.
#
# Each rung's skip and each rung's absence isolate to itself, exactly as in
# the pre-commit hook: HELM_CONFLICT_MARKER_SKIP=1 and HELM_NEVER_TRACK_SKIP=1.
scanner=%(scanner)s
conflict=%(conflict)s
user_hook=%(user_hook)s
@helm-refusal-count@

if [ -x "$user_hook" ]; then
  "$user_hook" "$@" || exit $?
fi
helm_merge_refused() {
  echo "[helm merge] the merge is NOT committed and HEAD did not move; git leaves it in progress, and \\`git merge --abort\\` backs it out." >&2
  echo "[helm merge] a blob over the size line is judged against HEAD at BOTH doors: \\`git commit\\` refuses it too, and a legitimate one is declared helm-bulk=ok in .gitattributes." >&2
  echo "[helm merge] needles, conflict markers and e-mail addresses are judged HERE against HEAD alone, but \\`git merge --no-commit\\` then \\`git commit\\` (or \\`git commit\\` on this merge) credits every parent: content a merged parent already carries is admitted there, content no parent carries is refused there too." >&2
  exit "$1"
}
if [ "$HELM_CONFLICT_MARKER_SKIP" != "1" ]; then
  if [ -f "$conflict" ]; then
    helm_rung=conflict-marker
    python3 "$conflict" --staged || helm_merge_refused $?
  else
    echo "[helm conflict-marker] WARNING: scanner missing at $conflict —" >&2
    echo "[helm conflict-marker] merge-result conflict-marker scan SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
  fi
fi
[ "$HELM_NEVER_TRACK_SKIP" = "1" ] && exit 0
if [ ! -f "$scanner" ]; then
  echo "[helm never-track] WARNING: scanner missing at $scanner —" >&2
  echo "[helm never-track] merge-result scan SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
  exit 0
fi
%(stale_check)s
helm_rung=never-track
python3 "$scanner" --staged || helm_merge_refused $?
exit 0
""", "pre-merge-commit")


GUARD_HOOKS = (("reference-transaction", REF_GUARD_HOOK),
               ("post-checkout", GUARD_HOOK),
               ("pre-commit", NEVER_TRACK_HOOK),
               ("pre-merge-commit", MERGE_COMMIT_HOOK),
               ("pre-push", HOSTPATH_PUSH_HOOK),
               ("commit-msg", TRAILER_MSG_HOOK))


LEAK_PRECOMMIT_HOOK = _counted("""#!/bin/sh
# helm work managed hook: pre-commit leak-profile v2
# Existing executable hook, when present, runs first with the same args.
#
# THE LEAK PROFILE. A project repo that is not run on the shared-checkout
# rail still has the one property every repo has: history is forever and a
# remote is a distribution. This hook carries the rail's LEAK legs only —
# the never-track staged-set scan with its bulk-data leg (exports, dumps,
# snapshots, archives, address lists, oversized blobs) and the private
# needles — and none of the lane/venue/citation rungs that presume the
# rail's process. A repo that grows into the rail re-installs with the
# rail profile; the profile is recorded in git config (helm.guard.profile)
# so `helm doctor` and a later install judge the repo by what it declared.
#
# THE CONFLICT-MARKER RUNG runs first, as on the rail: a marker line is a
# half-finished merge, and the pre-merge-commit hook refuses one, so the
# commit door beside it refuses one too. Its skip (HELM_CONFLICT_MARKER_SKIP=1)
# and its missing snapshot isolate to itself and never disarm never-track.
scanner=%(scanner)s
conflict=%(conflict)s
user_hook=%(user_hook)s
@helm-refusal-count@

if [ -x "$user_hook" ]; then
  "$user_hook" "$@" || exit $?
fi
if [ "$HELM_CONFLICT_MARKER_SKIP" != "1" ]; then
  if [ -f "$conflict" ]; then
    helm_rung=conflict-marker
    python3 "$conflict" --staged || exit $?
  else
    echo "[helm conflict-marker] WARNING: scanner missing at $conflict —" >&2
    echo "[helm conflict-marker] staged conflict-marker scan SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
  fi
fi
[ "$HELM_NEVER_TRACK_SKIP" = "1" ] && exit 0
if [ ! -f "$scanner" ]; then
  echo "[helm never-track] WARNING: scanner missing at $scanner —" >&2
  echo "[helm never-track] staged-set scan SKIPPED; reinstall: helm work install-guard --apply --profile %(profile)s" >&2
  exit 0
fi
%(stale_check)s
helm_rung=never-track
python3 "$scanner" --staged || exit $?
""", "pre-commit")

# WHICH HOOKS A REPO GETS is a declared PROFILE, recorded in the repo's own
# git config under PROFILE_KEY so every reader (install, drift, doctor)
# judges the repo against the same declaration. "rail" is the composed
# shared-checkout rail; "leak" is the legs every repo owes regardless: the
# staged-set scan at both commit doors git has (pre-commit for an ordinary
# commit, pre-merge-commit for a merge git completes itself) and the push scan.
GUARD_PROFILES = {
    "rail": GUARD_HOOKS,
    "leak": (("pre-commit", LEAK_PRECOMMIT_HOOK),
             ("pre-merge-commit", MERGE_COMMIT_HOOK),
             ("pre-push", HOSTPATH_PUSH_HOOK)),
}
PROFILE_KEY = "helm.guard.profile"
# THE DEFAULT IS THE REPO'S, NOT THE PRODUCT'S. `rail` is helm-the-repo's own
# shared-checkout law (the ref rail, the lane-discipline venue rung, the
# attribution trailer, the citation and public-prose rungs); `leak` is the two
# legs EVERY repo owes because history is forever and a remote is a
# distribution. A team USING helm must never need to know how helm is made, so
# an undeclared PROJECT repo gets the leak legs and the rail is opt-in there —
# the reverse of what shipped, which armed helm's own process in every adopter
# repo that installed a guard without naming a profile (task/2441).
HELM_REPO_PROFILE = "rail"
PROJECT_REPO_PROFILE = "leak"


class UnknownRepositoryIdentity(ValueError):
    """This repository's identity names no checkout, so no profile follows
    from it. A ValueError SUBCLASS because `install_guard` already REFUSES on
    that type for the other unusable profile input — a declaration it cannot
    parse — and the two belong at the same door: the verb must not write a law
    it had to invent."""


def default_profile(root):
    """The profile a repo that DECLARED NOTHING is judged under: helm's own
    source checkout keeps the rail, every other repo gets the leak legs.
    `selfrepo` is the one module both this and the maker-only tree warning
    ask, so neither can drift into the other's answer — and this is the caller
    that reads the NARROW predicate: a DAMAGED tree (the package root gone from
    a tree still carrying the package's modules) gets the leak legs here while
    the warning still speaks about it, because the two surfaces' worse failures
    are opposite. AN UNANSWERABLE PREDICATE TAKES THE NARROW LEGS: when the
    filesystem can neither confirm nor rule out the package root, arming
    helm's own shared-checkout process in a repo that may well be an
    adopter's is the worse failure, so this caller fails toward the two legs
    every repo owes. The tree warning fails the other way, toward speaking,
    for its own reason. Either way an explicit `--profile` decides, and this
    default is only ever consulted for the profile a repo is moving TO.

    AN IDENTITY WITH NO REACHABLE CHECKOUT IS DIFFERENT FROM A STAT THAT
    FAILED, and it REFUSES here. A failed stat is a tree this repo HAS, read
    through an accident (a mode, a mount) that a retry or a chmod resolves, so
    the narrow legs are a safe posture for it. A separate gitdir that records
    nothing pointing back at a checkout, or a bare mirror, will never have a
    tree to read — MEASURED, and the same measurement `obligation._root_for_repo`
    carries — so "leak" there is not caution, it is an invented law: it arms an
    adopter's legs over whatever is really installed and tells the drift reader
    that a live rail is STALE. Neither profile follows from an unknown
    identity, so this raises and the caller says so: `install_guard` refuses
    before any write and `--profile` decides it, `stale_guard_hooks` reports
    UNKNOWN (which no land close treats as drift), and `doctor` warns."""
    from .. import selfrepo
    try:
        own = selfrepo.is_helm_source_tree(root)
    # THE SUBCLASS CLAUSE COMES FIRST or this whole distinction disappears:
    # `UnreachableCheckout` IS an OSError, so an `except OSError` above it
    # would fold "there is no tree" back into the narrow legs silently.
    except selfrepo.UnreachableCheckout as exc:
        raise UnknownRepositoryIdentity(
            "this repo declares no %s and its identity names no checkout: %s. "
            "Neither the rail nor the leak legs follow from a repository whose "
            "tree nobody can read, so name one with --profile rail|leak, or "
            "hand this verb the checkout itself." % (PROFILE_KEY, exc)) from exc
    except OSError:
        own = False                             # the narrow legs; see above
    return HELM_REPO_PROFILE if own else PROJECT_REPO_PROFILE


# THE RAIL'S OWN HOOK SLOTS — the names the rail plans and the leak profile
# does not. An installed hook at one of these, carrying helm's marker, is
# evidence that THIS repo was put under the rail, whatever it declares.
RAIL_ONLY_HOOKS = tuple(name for name, _t in GUARD_PROFILES["rail"]
                        if name not in {n for n, _t in GUARD_PROFILES["leak"]})


def _rail_only_assets(root, hooks=None):
    """The scanner snapshots the rail runs and the leak profile does not —
    derived from the profile tables, never a list to keep in step with them."""
    hooks = hooks or Hooks(root)
    return sorted(set(_scanner_assets(root, "rail", hooks))
                  - set(_scanner_assets(root, "leak", hooks)))


def rail_only_artifacts(root, hooks=None):
    """Every artifact ON DISK that only a rail install puts there.

    Three kinds, and the shared pre-commit slot is the one that is easy to
    miss: the rail composes its rungs into the SAME `pre-commit` name the leak
    profile uses, so the slot's presence says nothing while its BODY says which
    profile wrote it. The body is matched against the rail-only snapshot
    basenames it delegates to — derived from the profile tables, so a rung
    added or dropped cannot leave this reader behind.

    Raises OSError when the answer can be neither read nor ruled out:
    "no rail artifact here" and "cannot look" are different facts, and a caller
    inferring a repo's current law must not be handed the first for the second.

    AN UNREADABLE SLOT IS ONLY UNKNOWN WHILE NOTHING ELSE HAS ANSWERED. The
    first shape raised from the middle of the walk, so ONE unreadable path
    discarded the evidence already in hand: a repo whose reference-transaction
    and post-checkout hooks were readable, owned and unmistakably the rail's
    reported "cannot look" because a FOREIGN `commit-msg` further down the list
    was mode 000 — and the caller then recorded the leak profile over a rail
    that was still armed. A rail this reader has already SEEN cannot be unseen
    by a path it could not open, so an unreadable path is deferred and raised
    only if the walk ends with no rail artifact found — the one case where
    reading it could have changed the answer.
    """
    hooks = hooks or Hooks(root)
    found = []
    unreadable = []

    def snap(path):
        try:
            return _path_snapshot(path)
        except OSError as exc:
            unreadable.append(exc)
            return ("absent", None, None)

    for name in RAIL_ONLY_HOOKS:
        path = hooks.path(name)
        if _owned_hook(snap(path)):
            found.append(path)
    rail_assets = _rail_only_assets(root, hooks)
    for path in rail_assets:
        if snap(path)[0] != "absent":
            found.append(path)
    shared = hooks.path("pre-commit")
    shared_snap = snap(shared)
    if _owned_hook(shared_snap) and shared_snap[0] == "file":
        body = shared_snap[1] or b""
        names = [os.path.basename(p).encode("utf-8") for p in rail_assets]
        if any(name in body for name in names):
            found.append(shared)
    if not found and unreadable:
        raise unreadable[0]
    return found


def installed_profile(root, hooks=None):
    """The profile whose hooks are ACTUALLY ON DISK, or None when this repo
    carries no helm-owned hook at all.

    A REPO'S CURRENT LAW IS A FACT ABOUT ITS DISK, NEVER ABOUT A DEFAULT. The
    install transaction retires the slots the outgoing profile planned and the
    incoming one does not, and it reads the outgoing profile from what this
    repo is under. Resolving that through `default_profile` made a repo's
    INSTALLED history unreadable: a project repo put under the rail before it
    recorded anything answered "leak" — the default for its identity — so a
    flagless install found nothing departing, left the reference-transaction,
    post-checkout and commit-msg hooks and the rail's scanner snapshots
    running, and then recorded `leak` over them. The declaration described
    hooks that were not the ones executing, and every census read the
    declaration.

    Only helm-owned hooks count: a foreign hook at one of these names is not
    ours and says nothing about which profile this repo was put under.

    EVERY ARTIFACT THE RAIL ALONE INSTALLS IS EVIDENCE, not just its own hook
    slots. Keying rail on the three rail-only slots alone read a PARTIALLY
    dismantled rail — those slots gone, the rail's composed pre-commit still
    running and its whole scanner shelf still on disk — as leak, so an explicit
    leak computed no departures at all: the shelf survived, `drift` and `doctor`
    kept judging rungs no installed hook runs, and the note reported leak as the
    profile already installed. `rail_only_artifacts` is the one enumeration, so
    this answer and the departure set below cannot disagree.

    AND WHEN NOTHING ON DISK CAN BE READ, THIS RAISES. The caller's question is
    which law this repo is under, and "leak" is an ANSWER — returning it for a
    hook set nobody could open lets the leak profile speak for the rail. The
    shared slots are walked the way the rail-only ones are: an owned slot
    settles it, an unreadable one is raised only when nothing settled it."""
    hooks = hooks or Hooks(root)
    if rail_only_artifacts(root, hooks):
        return "rail"
    unreadable = []
    shared = []
    for name, _template in GUARD_PROFILES["leak"]:
        try:
            snap = _path_snapshot(hooks.path(name))
        except OSError as exc:
            unreadable.append(exc)
            continue
        if _owned_hook(snap):
            shared.append(name)
    if shared:
        return "leak"
    if unreadable:
        raise unreadable[0]
    return None


def declared_profile(root):
    """The profile the repo's git config DECLARES, or None when nothing is
    declared — the raw fact, for the install note. `guard_profile` is the
    effective one, and the gap is filled by `default_profile`: the rail for
    helm's own source checkout, the leak legs for a project repo. That is the
    profile an undeclared repo is judged UNDER; the profile it is already
    running is `installed_profile`, which reads the disk."""
    rc, out, _err = _git(root, "config", "--get", PROFILE_KEY)
    value = (out or "").strip() if rc == 0 else ""
    return value or None


def checked_declaration(root):
    """The DECLARATION half of `guard_profile` on its own: the profile this
    repo declared, None when it declares nothing, and a REFUSAL (ValueError)
    when what it declares is not a profile.

    This exists because the two halves answer to different callers. Validating
    a declaration is owed by EVERY install — a typo in the key must never
    silently re-guard a repo — while DERIVING a default is owed only by an
    install that was not told which profile to move TO. Calling the combined
    reader for the validation alone derived a default nobody asked for, and an
    identity that names no checkout raises there, so `install_guard --profile
    leak` refused on a repository whose profile the operator had just named —
    the one recovery the UNKNOWN-identity refusal advertises. Keep the two
    apart and the explicit flag reaches the transaction."""
    value = declared_profile(root)
    if value is not None and value not in GUARD_PROFILES:
        raise ValueError("%s=%r is not a guard profile (one of %s)"
                         % (PROFILE_KEY, value, ", ".join(sorted(GUARD_PROFILES))))
    return value


def guard_profile(root, hooks=None):
    """THE PROFILE A FLAGLESS OPERATION IS ABOUT, resolved in three steps and
    in this order: what this repo DECLARED (git config PROFILE_KEY), else what
    it is RUNNING (`installed_profile`, which reads the hook set on disk), else
    the default its own identity carries (`default_profile`: the rail for
    helm's source checkout, the leak legs for a project repo). An unknown
    declared value REFUSES rather than falling back: a typo in the declaration
    must not silently turn a leak-guarded repo into a rail repo or the reverse.

    THE MIDDLE STEP IS THE ONE THAT WAS MISSING, and its absence had this
    reader answering a question nobody asked. Skipping from the declaration to
    the IDENTITY default told every flagless caller what an undeclared repo
    would be judged under if it were FRESH — a fact about the repo's identity —
    while the repo was in fact running a whole other profile's hooks. MEASURED on
    a shared checkout that had never recorded a profile and had been put under
    the rail: `helm work claim` rendered the rail's
    installed hooks against the LEAK plan, called them stale, and printed
    `helm work install-guard --apply` as the remedy; that flagless install
    resolved its target here, got `leak`, and retired the three rail-only hook
    slots and eleven rail scanner snapshots — the repo lost its rungs by
    following the tool's own instruction, and nothing afterwards looked wrong.
    A default is for a repo with NOTHING to read, so it is asked last: a repo
    that is running a profile has already answered.

    The declaration is read through `checked_declaration`, so the validation
    rule lives at ONE door and a caller that owes only the validation can take
    it without the default. `default_profile` stays the reader for the one
    question it answers — which profile a repo with no hooks and no
    declaration is judged under — and the install note names it there.

    RAISES what the step that could not answer raises: ValueError for an
    unparseable declaration, OSError when the disk cannot be read (an
    unreadable hook set is not an absent one, and the leak profile may never
    speak for a rail nobody could look at), UnknownRepositoryIdentity when a
    fresh repo's identity names no checkout."""
    declared = checked_declaration(root)
    if declared is not None:
        return declared
    running = installed_profile(root, hooks)
    if running is not None:
        return running
    return default_profile(root)


def _remedy_resolution(root, hooks=None):
    """(profile, declared, running) behind a printed remedy: the profile the
    remedy names, what the repo declares (None when nothing), and what its
    hooks on disk are running (None when nothing owned, or unreadable under a
    declaration).

    THE REMEDY NAMES WHAT THE FLAGLESS VERB WOULD INSTALL — `guard_profile`'s
    three steps, in its order — EXCEPT WHERE THAT VERB WOULD REFUSE. A
    declaration is authoritative for a flagless operation, but a declaration
    can disagree with the disk: someone recorded leak while the rail is armed.
    There the flagless install refuses as a narrowing, and the remedy that
    resolved through the declaration printed `--profile leak` — the explicit
    flag the refusal deliberately admits — so an operator who pasted the
    tool's own advice retired the rail's three slots and eleven scanner
    snapshots, the exact class task/2504 exists to end. A PRINTED REMEDY MAY
    NEVER NARROW THE REPO, so in that one state it names the profile the repo
    is RUNNING: following it keeps every rung armed and re-records the
    declaration to match, and the note beside it names the narrowing command
    for the operator who means it. Narrowing is decided by the refusal's own
    predicate (`_narrowing_losses`), never by a profile name, so the remedy
    and the refusal cannot disagree about which state is the trap. Where the
    declaration WIDENS what is running (declares rail, runs the leak legs) it
    stands, as it does for the flagless verb: a widening needs nobody's
    permission.

    THE DISK IS READ DEFENSIVELY ONLY UNDER A DECLARATION, the transaction's
    own rule: a declared repo with one unreadable foreign hook keeps its
    declaration, while an undeclared repo whose hook set cannot be read RAISES
    — the leak profile may never speak for a rail nobody could look at, and
    the caller renders the choose-it-yourself form."""
    hooks = hooks or Hooks(root)
    declared = checked_declaration(root)
    if declared is None:
        running = installed_profile(root, hooks)
        return (running or default_profile(root)), None, running
    try:
        running = installed_profile(root, hooks)
    except OSError:
        return declared, declared, None
    if running is None or running == declared:
        return declared, declared, running
    planned = [{"name": name} for name, _template in GUARD_PROFILES[declared]]
    if _narrowing_losses(root, planned,
                         _scanner_assets(root, declared, hooks), running,
                         hooks):
        return running, declared, running
    return declared, declared, running


def _remedy_parts(root, hooks=None):
    """(command, note) — the install command to PRINT at a repo whose guard
    is stale or missing, and the sentence that follows it when the profile it
    names is not the one the repo declares (empty otherwise).

    NEVER RAISES. This is a diagnostic line on a repo already known to be in a
    bad state, and losing the whole "not armed" notice because the profile
    could not be resolved would hide the finding to protect its footnote. When
    the profile is unresolvable the operator is asked for it."""
    try:
        profile, declared, running = _remedy_resolution(root, hooks)
    except Exception:
        return "helm work install-guard --apply --profile rail|leak", ""
    command = "helm work install-guard --apply --profile %s" % profile
    if declared is None or profile == declared:
        return command, ""
    return command, (
        " — this repo declares %s=%s but is RUNNING the %s profile; that "
        "command keeps the %s armed and re-records the declaration as %s. "
        "`helm work install-guard --apply --profile %s` narrows on purpose "
        "and retires the %s's slots and snapshots; a flagless refresh refuses "
        "here" % (PROFILE_KEY, declared, running, running, running, declared,
                  running))


def guard_remedy(root):
    """The install command to PRINT at a repo whose guard is stale or missing,
    with the profile already in it.

    EVERY PRINTED REMEDY CARRIES THE PROFILE, because a remedy is pasted, not
    read. `helm work install-guard --apply` was the whole text of the staleness
    notice, and on an undeclared repo running the rail that exact paste retired
    three hook slots and eleven scanner snapshots on a shared checkout. The
    resolver no longer narrows, so the bare form is now safe — but a notice is
    printed by the tree that is INSTALLED, which can be older than the tree
    that cures this, so the text itself must not be able to narrow anything.

    AND THE PROFILE IT CARRIES IS NEVER THE ONE THAT WOULD NARROW: see
    `_remedy_resolution`. Every consumer prints `guard_remedy_note` after the
    command, so the operator who is handed the running profile over a
    disagreeing declaration is told why, and told the narrowing command by
    name. NEVER RAISES; see `_remedy_parts`."""
    return _remedy_parts(root)[0]


def guard_remedy_note(root):
    """The sentence every consumer prints AFTER `guard_remedy`'s command —
    empty unless the command names a profile other than the one the repo
    declares, in which case it says the declaration disagrees, what following
    the command does, and which command narrows on purpose. Kept outside the
    command so the command stays a paste."""
    return _remedy_parts(root)[1]


def guard_remedy_and_note(root, hooks=None):
    """(`guard_remedy`, `guard_remedy_note`) from ONE resolution. A consumer
    that prints both asked the profile question twice; `hooks` is the
    evaluation the consumer's own staleness check already made."""
    return _remedy_parts(root, hooks)


def declare_profile(root, profile):
    """Record the profile in the repo's local git config — the common dir,
    so every worktree of the repo reads the same declaration."""
    if profile not in GUARD_PROFILES:
        raise ValueError("not a guard profile: %r" % (profile,))
    rc, _out, err = _git(root, "config", "--local", PROFILE_KEY, profile)
    if rc != 0:
        raise RuntimeError("cannot record %s: %s" % (PROFILE_KEY, err))


def _scanner_path():
    """The source nevertrack.py whose bytes the installer snapshots."""
    from .. import nevertrack
    return os.path.abspath(nevertrack.__file__)


def _vacuous_assertion_rung_path():
    """The source staged-test advisory whose bytes the installer snapshots."""
    from .. import vacuous_assertion
    return os.path.abspath(vacuous_assertion.__file__)


def _silent_cap_rung_path():
    """The silent-cap advisory whose bytes the installer snapshots.

    It warns when a staged write persists a value the same function cut and
    left unmarked. WARN-only on a measured rate (`silent_cap.MEASURED`), and
    snapshotted like every sibling so a committing lane cannot edit its own
    judge."""
    from .. import silent_cap
    return os.path.abspath(silent_cap.__file__)


def _orphaned_mock_rung_path():
    """The source orphaned-double census whose bytes the installer snapshots."""
    from .. import orphaned_mock
    return os.path.abspath(orphaned_mock.__file__)


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


def _split_budget_rung_path():
    """The seats-split line budget, asked at commit instead of from a red
    suite. It REFUSES only what the commit makes worse and reports the rest,
    so a standing debt in a module nobody touched cannot wall an unrelated
    lane. The whole-suite arm stays the backstop and imports its constants
    from here, so there is one budget with two readers."""
    from .. import splitbudget
    return os.path.abspath(splitbudget.__file__)


def _conflict_marker_rung_path():
    """The source conflict-marker scanner whose bytes the installer snapshots."""
    from .. import conflict_marker
    return os.path.abspath(conflict_marker.__file__)


def _world_prose_rung_path():
    """The source public-bound prose scanner snapshotted beside the hook."""
    from .. import world_prose_guard
    return os.path.abspath(world_prose_guard.__file__)


def _retired_name_rung_path():
    """The retired-top-level-name rung snapshotted beside the hook."""
    from .. import retired_name_rung
    return os.path.abspath(retired_name_rung.__file__)


def _seatname_authority_notes():
    """Report live seats missing from the seat-name rung's authority file.

    THE INSTALLER CAN READ THE ROSTER; THE SNAPSHOT CANNOT, and that asymmetry
    is the design rather than an accident. A rung that imported the roster is
    inert everywhere it is installed (inflight_gate measured exactly that), so
    the rung reads a FILE — and a file goes stale SILENTLY, which is the one
    failure the request for this rung named by name ("so it cannot drift from
    who actually exists"). Install is the moment both are readable, so drift
    is reported HERE, where somebody is already looking at guard output.

    IT NEVER REWRITES THE FILE. The exclusions are curated — a name that is
    also a provider value is held back on purpose, because arming it would
    refuse honest tests until somebody disabled the rung wholesale — and a
    blind regenerate would silently re-arm every one of them.

    An unreadable roster reports NOTHING rather than implying the file is
    complete: a failed probe is not evidence that no seat exists.
    """
    try:
        from .. import seatname_guard, seats_roster
        path, invalid = seatname_guard.authority_path()
        live, failed = seats_roster.roster_checked()
    except Exception:                                         # noqa: BLE001
        return []
    if invalid:
        # THE LOUDEST CASE THIS FUNCTION HAS. An override nobody can resolve
        # means the rung refuses every tests/ commit and the roster projects
        # nowhere — and the operator set that variable themselves, so this is
        # the one note here that names an action they can take immediately.
        return ["helm work: the seat-name rung's authority is UNRESOLVABLE — "
                "%s Until it is absolute, the rung FAILS CLOSED and commits "
                "touching tests/ will be refused." % invalid]
    if failed or not isinstance(live, dict):
        return []
    # TOTAL BY CONSTRUCTION. These notes run AFTER the install transaction has
    # written four hooks and nine snapshots, outside its rollback, so a
    # diagnostic that can raise turns a successful install into a traceback
    # over persistent mutation: an authority with invalid UTF-8 did exactly
    # that. read_authority never raises.
    authority, malformed = seatname_guard.read_authority(path)
    if malformed:
        return ["helm work: the seat-name rung's authority is UNREADABLE — %s. "
                "The rung FAILS CLOSED on it, so commits touching tests/ will "
                "be refused until the file is repaired." % malformed]
    declared = authority.names(seatname_guard.ARMED, seatname_guard.HELD,
                               seatname_guard.CONFLICT)
    if not declared:
        # INSTALLED AND UNARMED is the loudest case, not the quietest: the
        # hook is in place, every commit passes, and nothing says why. The
        # rung says so at commit time; the installer says so here, when
        # somebody is in a position to fix it.
        names = sorted(n for n in live if isinstance(n, str) and n.strip())
        return ["helm work: the seat-name rung is INSTALLED BUT UNARMED — no "
                "authority at %s, so it will pass every commit. Populate it "
                "(one name per line) to guard the %d live seat identities."
                % (path, len(names))]
    # `covers`, NOT `refuses`, and through the Authority rather than a fourth
    # normalization minted here. The set union this replaced compared RAW
    # spellings, so a live `seat-a` against a held `!Seat-A` was reported as
    # an uncovered gap that no amount of arming could close — the installer
    # nagging about a name the operator had already decided about.
    missing = sorted(n for n in live
                     if isinstance(n, str) and n.strip()
                     and not authority.covers(n))
    if not missing:
        return []
    # A COUNT, NEVER THE NAMES. This module reads the roster to answer a
    # MEMBERSHIP question, and what leaves is a number and a path — printing
    # the identities would make an install summary a roster emission site, and
    # the whole subject of the rung it configures is seat identities reaching
    # places they should not. `helm chat seats --all` is the reader for that.
    return ["helm work: the seat-name rung's authority is STALE — %d live "
            "seat(s) are not covered by %s, so the rung will not catch them. "
            "List them with `helm chat seats --all`; add each name on its own "
            "line, or prefix with '!' to hold it back deliberately (a name "
            "that is also a provider value would refuse honest tests)."
            % (len(missing), path)]


def _seatname_rung_path():
    """The seat-name rung: refuse a REAL seat identity entering public-bound
    tests/ as a fixture value. Its authority is a FILE outside the tree
    (nevertrack's load_private_needles shape), never an import of the roster —
    from the snapshot's own directory `import helm` fails, which would leave
    the rung inert everywhere it is installed while still printing a line."""
    from .. import seatname_guard
    return os.path.abspath(seatname_guard.__file__)


def _trailer_rung_path():
    """The source attribution rung whose bytes the installer snapshots — same
    canonical-scanner law as every other rung: the copy that judges a commit
    must not be the copy the committing lane can edit."""
    from .. import trailer_rung
    return os.path.abspath(trailer_rung.__file__)


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
    """True when this hook is THIS PROJECT'S OWN output, managed or legacy.

    BOTH MARKER FAMILIES ARE PREFIX TESTS, AND THEY MUST STAY SYMMETRIC. A
    whole-line test on either family is unsatisfiable against the hooks this
    project writes: their marker line carries a continuation — "# helm work
    guard — the shared checkout is the integrator's tree (installed" — so it
    equals no marker and matches nothing.

    THE CONSEQUENCE WAS NOT A MISSED LABEL, IT WAS A MISFILED FILE. Install
    reads this answer to decide whether an existing hook is a USER'S work
    worth preserving. A legacy helm guard answering False was preserved into
    the `.helm-user` companion slot — which is kept byte-for-byte by design
    and rewritten by nothing, ever — while the new managed hook runs it FIRST
    on every invocation. So the stalest copy of this project's own code became
    the one preserved forever, under a filename asserting it belongs to
    somebody else, and no source guard can see it because it is untracked.
    """
    if snap[0] != "file":
        return False
    lines = snap[1].decode("utf-8", "replace").splitlines()[:8]
    markers = (MANAGED_HOOK_MARKER,) + LEGACY_HOOK_MARKERS
    return any(line.startswith(m) for line in lines for m in markers)


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


def _scanner_assets(root, profile=None, hooks=None):
    """Stable installed snapshot -> source module for each hook scanner.

    The hook may be installed FROM a lane, but it must never execute FROM that
    lane: an unstaged edit would change every worktree's guard and deleting the
    room would make the estate fail open. Snapshots live beside the shared hook.
    The leak profile snapshots only the three scanners its hooks run, so drift
    detection judges what is armed, not the rail's whole shelf.
    """
    hooks = hooks or Hooks(root)
    d = os.path.join(os.path.dirname(hooks.path("pre-commit")),
                     ".helm-scanners")
    profile = profile or guard_profile(root, hooks)
    if profile == "leak":
        return {os.path.join(d, "nevertrack.py"): _scanner_path(),
                os.path.join(d, "conflict_marker.py"):
                    _conflict_marker_rung_path(),
                os.path.join(d, "hostpath_guard.py"): _hostpath_scanner_path()}
    return {os.path.join(d, "nevertrack.py"): _scanner_path(),
            os.path.join(d, "vacuous_assertion.py"):
                _vacuous_assertion_rung_path(),
            os.path.join(d, "orphaned_mock.py"): _orphaned_mock_rung_path(),
            os.path.join(d, "silent_cap.py"): _silent_cap_rung_path(),
            os.path.join(d, "hardcode.py"): _hardcode_rung_path(),
            os.path.join(d, "inflight_gate.py"): _inflight_rung_path(),
            os.path.join(d, "conflict_marker.py"): _conflict_marker_rung_path(),
            os.path.join(d, "splitbudget.py"): _split_budget_rung_path(),
            os.path.join(d, "lane_discipline.py"): _lane_discipline_rung_path(),
            os.path.join(d, "docref_guard.py"): _docref_rung_path(),
            os.path.join(d, "world_prose_guard.py"):
                _world_prose_rung_path(),
            os.path.join(d, "retired_name_rung.py"):
                _retired_name_rung_path(),
            os.path.join(d, "seatname_guard.py"): _seatname_rung_path(),
            os.path.join(d, "trailer_rung.py"): _trailer_rung_path(),
            os.path.join(d, "hostpath_guard.py"): _hostpath_scanner_path()}


def _guard_plan(root, profile=None, hooks=None):
    hooks = hooks or Hooks(root)
    profile = profile or guard_profile(root, hooks)
    base = _base(root)
    if profile == "leak":
        return base, _leak_plan(root, hooks)
    assets = _scanner_assets(root, profile, hooks)
    scanner = next(p for p in assets if p.endswith("/nevertrack.py"))
    vacuous = next(p for p in assets if p.endswith("/vacuous_assertion.py"))
    orphaned = next(p for p in assets if p.endswith("/orphaned_mock.py"))
    silent_cap = next(p for p in assets if p.endswith("/silent_cap.py"))
    hardcode_rung = next(p for p in assets if p.endswith("/hardcode.py"))
    inflight_rung = next(p for p in assets if p.endswith("/inflight_gate.py"))
    conflict = next(p for p in assets if p.endswith("/conflict_marker.py"))
    split_budget = next(p for p in assets if p.endswith("/splitbudget.py"))
    lane_rung = next(p for p in assets if p.endswith("/lane_discipline.py"))
    hostpath = next(p for p in assets if p.endswith("/hostpath_guard.py"))
    docref = next(p for p in assets if p.endswith("/docref_guard.py"))
    world_prose = next(p for p in assets
                       if p.endswith("/world_prose_guard.py"))
    retired_name = next(p for p in assets
                        if p.endswith("/retired_name_rung.py"))
    seatname = next(p for p in assets if p.endswith("/seatname_guard.py"))
    trailer = next(p for p in assets if p.endswith("/trailer_rung.py"))
    plan = []
    for name, template in GUARD_HOOKS:
        target = hooks.path(name)
        user = target + ".helm-user"
        sc = hostpath if name == "pre-push" else scanner
        subs = {"base": shlex.quote(base), "user_hook": shlex.quote(user),
                "profile": profile,
                "scanner": shlex.quote(sc),
                # THE SNAPSHOT->SOURCE PAIRING IS THE INSTALLER'S OWN MAP, so
                # the hook cannot compare a snapshot against the wrong source.
                "stale_check": _stale_scanner_check(shlex.quote(assets[sc]),
                                                    profile),
                "docref": shlex.quote(docref),
                "world_prose": shlex.quote(world_prose),
                "retired_name": shlex.quote(retired_name),
                "split_budget": shlex.quote(split_budget),
                "seatname": shlex.quote(seatname),
                "vacuous": shlex.quote(vacuous),
                "orphaned": shlex.quote(orphaned),
                "silent_cap": shlex.quote(silent_cap),
                "conflict": shlex.quote(conflict),
                "hardcode_rung": shlex.quote(hardcode_rung),
                "inflight_rung": shlex.quote(inflight_rung),
                "trailer": shlex.quote(trailer),
                "lane_rung": shlex.quote(lane_rung)}
        script = template % subs
        plan.append({"name": name, "target": target, "user": user,
                     "retired": target + RETIRED_HOOK_SUFFIX,
                     "script": script})
    return base, plan


def _leak_plan(root, hooks=None):
    """The leak profile's hooks, rendered from the same templates and
    snapshot paths the rail uses, so drift detection is one predicate."""
    hooks = hooks or Hooks(root)
    assets = _scanner_assets(root, "leak", hooks)
    scanner = next(p for p in assets if p.endswith("/nevertrack.py"))
    conflict = next(p for p in assets if p.endswith("/conflict_marker.py"))
    hostpath = next(p for p in assets if p.endswith("/hostpath_guard.py"))
    plan = []
    for name, template in GUARD_PROFILES["leak"]:
        target = hooks.path(name)
        user = target + ".helm-user"
        sc = hostpath if name == "pre-push" else scanner
        subs = {"user_hook": shlex.quote(user),
                "profile": "leak",
                "scanner": shlex.quote(sc),
                "conflict": shlex.quote(conflict),
                "stale_check": _stale_scanner_check(shlex.quote(assets[sc]),
                                                    "leak")}
        plan.append({"name": name, "target": target, "user": user,
                     "retired": target + RETIRED_HOOK_SUFFIX,
                     "script": template % subs})
    return plan


def stale_guard_hooks(root, hooks=None):
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
    hooks = hooks or Hooks(root)
    # ONE PROFILE FOR THE WHOLE CHECK (task/3039): the plan and the scanner
    # snapshots below are judged under one resolution, so the two halves of
    # the check cannot disagree about the profile, and git is asked once.
    try:
        profile = guard_profile(root, hooks)
        _base_, plan = _guard_plan(root, profile, hooks)
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
            continue
        # THE BYTES ARE NOT THE HOOK; git runs a hook only if it is
        # executable. Identical bytes at mode 0644 are an inert file that
        # reads as current to a byte compare — so mode is part of identity
        # here, as it already is in check_work_guard's snapshot compare.
        if _path_snapshot(p["target"])[2] != 0o755:
            out.append(("STALE", p["name"],
                        "installed hook is not executable (mode is not 0755) — "
                        "git will not run it"))
    for snap, source in sorted(_scanner_assets(root, profile, hooks).items()):
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


def _installed_drift(plan):
    """[lines] naming every installed hook that DIFFERS from what would be written.

    THE INSTALLED TIER IS ONE NO SOURCE RUNG REACHES. Every content guard in
    this repo scans the TRACKED tree, so it proves things about what the NEXT
    install will write and nothing at all about what is installed NOW. The gap
    is invisible from both directions: the source reads cured, and the running
    box reads fine because nothing asks it. It is the mirror of landed-but-not-
    deployed, and the deployed-but-not-landed half has no instrument.

    THIS IS THAT INSTRUMENT AND IT IS A READ. It renders in the DRY branch,
    writes nothing, and answers the one question the install path never asks
    out loud: does the hook that will FIRE match the template it was rendered
    from?

    TWO SHAPES, AND THE SECOND IS THE ONE THAT NEVER HEALS. A MANAGED target
    that drifts is cured by `--apply`, so naming it is a nudge. A `.helm-user`
    COMPANION carrying a helm marker is a demoted helm guard that was moved
    into the user slot when a newer managed hook installed over it — and
    companions are preserved byte-for-byte BY DESIGN, so nothing rewrites it
    ever, while the managed hook runs it FIRST on every invocation. The
    stalest copy of this project's own code is the one preserved forever under
    a filename asserting it belongs to somebody else. Reporting it is not the
    cure; deciding what to do about a file the preservation rule protects is a
    policy question that wants its own review. What this removes is the part
    that made it undiscoverable.
    """
    out = []
    for p in plan:
        target, user = p["target"], p["user"]
        want = ("file", p["script"].encode("utf-8"), 0o755)
        prior = _path_snapshot(target)
        if prior[0] == "absent":
            out.append("# DRIFT %s — NOT INSTALLED; the guard that would run "
                       "here does not exist" % target)
        elif prior[0] != want[0]:
            out.append("# DRIFT %s — installed node is a %s, not a file"
                       % (target, prior[0]))
        elif prior[1] != want[1]:
            out.append("# DRIFT %s — installed CONTENT differs from the "
                       "template below; `--apply` rewrites it" % target)
        elif prior[2] != want[2]:
            out.append("# DRIFT %s — installed mode is %o, not %o; a hook that "
                       "is not executable does not run"
                       % (target, prior[2] or 0, want[2]))
        companion = _path_snapshot(user)
        if companion[0] == "file" and _owned_hook(companion):
            # NAMED BY THE SAME PREFIX TEST `_owned_hook` USES. Asking a
            # different question here than the one that admitted the file
            # answers about a different file: a whole-line lookup falls
            # through to the managed marker and reports the wrong provenance
            # for exactly the legacy hooks this line exists to name.
            head = companion[1].decode("utf-8", "replace").splitlines()[:8]
            marker = next((m for m in (MANAGED_HOOK_MARKER,) + LEGACY_HOOK_MARKERS
                           if any(line.startswith(m) for line in head)),
                          "a helm marker")
            occupied = _path_snapshot(p["retired"])
            if occupied[0] != "absent" and occupied != _retired_form(companion):
                out.append("# UNHEALABLE %s — this companion carries %r, so it "
                           "is THIS PROJECT'S OWN superseded hook in the user "
                           "slot, and the managed hook RUNS IT FIRST on every "
                           "invocation. `--apply` cannot retire it because %s "
                           "already holds different bytes; move that aside by "
                           "hand" % (user, marker, p["retired"]))
            else:
                out.append("# DRIFT %s — this companion carries %r, so it is "
                           "THIS PROJECT'S OWN superseded hook in the user "
                           "slot, not a user's file, and the managed hook RUNS "
                           "IT FIRST on every invocation: its body reaches the "
                           "shared checkout ahead of the template below and "
                           "can answer first. `--apply` retires it to %s "
                           "non-executable, bytes intact"
                           % (user, marker, p["retired"]))
    return out


def _retired_form(companion):
    """The retired node for a superseded helm hook: same bytes, never runs.

    RETIRING IS NOT DELETING. The bytes are this project's own output and the
    only copy of what the box was executing, so they are kept verbatim under a
    name that is not a git hook and is not the `.helm-user` path the managed
    hook invokes. Dropping the executable bit is the second, independent
    reason it cannot run.
    """
    return ("file", companion[1], 0o644)


def _unreadable_running_refusal(root, exc, profile):
    """The refusal for an install that cannot read the profile this repo is
    RUNNING — one text for both doors that ask the disk, the target resolver
    and the transaction's outgoing-profile observation.

    AND THE ADVICE NAMES THE RECOVERY THAT WORKS. The first refusal offered
    "--profile rail|leak", which cannot recover this state: that flag names the
    profile this repo is moving TO, and the observation that failed is of the
    profile it is moving OFF — read whenever the CONFIG declares nothing,
    whatever the flag says. So every retry hit the same unreadable path and
    refused again while the operator believed they had been handed a way
    through. Two things do settle it: making that exact path readable, and
    RECORDING the declaration, which short-circuits the observation."""
    slot = getattr(exc, "filename", None) or "the path above"
    target = ("Recording %s here" % profile if profile
              else "Choosing a profile with no --profile")
    return ("helm work: REFUSED guard install — this repo declares no %s and "
            "the profile it is RUNNING is UNKNOWN: %s: %s. %s would certify a "
            "profile over hooks nobody could read and retire nothing that is "
            "departing. MAKE THIS PATH READABLE: %s — every retry refuses "
            "until it is, including one that passes --profile, which names the "
            "INCOMING profile and settles nothing about the history this repo "
            "is leaving. The other door is to record the outgoing profile "
            "yourself (git config --local %s rail|leak), which this install "
            "then believes. Nothing changed."
            % (PROFILE_KEY, type(exc).__name__, exc, target, slot, PROFILE_KEY))


def _narrowing_losses(root, plan, assets, running, hooks=None):
    """What a target would retire that the RUNNING profile is still using:
    hook slots the running profile plans and this plan does not, plus scanner
    snapshots it runs and this one does not.

    Derived from the profile tables and the install's own plan, never a list of
    names to keep in step with them — the same rule `_rail_only_assets` and the
    transaction's own retirement set follow, so this refusal and the retirement
    it prevents cannot disagree about what would have gone."""
    planned = {p["name"] for p in plan}
    losses = [name for name, _t in GUARD_PROFILES[running]
              if name not in planned]
    losses += sorted(os.path.basename(a) for a in
                     set(_scanner_assets(root, running, hooks)) - set(assets))
    return losses


def _narrowing_refusal(root, plan, assets, declared, running, profile,
                       hooks=None):
    """The refusal text for a flagless install that would narrow, or None when
    the target retires nothing the running profile uses (leak → rail widens,
    and a widening never needs the operator's permission)."""
    losses = _narrowing_losses(root, plan, assets, running, hooks)
    if not losses:
        return None
    said = ("declares %s=%s" % (PROFILE_KEY, declared) if declared
            else "declares no %s" % PROFILE_KEY)
    return ("helm work: REFUSED guard install — this repo %s and is RUNNING "
            "the %s profile, and no --profile was given. Installing %s here "
            "would retire %d artifact(s) the %s profile is running (%s), and a "
            "refresh nobody asked to narrow must never do that: this verb is "
            "what the staleness notice tells an operator to run, so a silent "
            "narrowing here costs a repo its rungs and looks correct "
            "afterwards. DECIDE IT: `helm work install-guard --apply --profile "
            "%s` narrows on purpose and names everything it retires; `helm "
            "work install-guard --apply --profile %s` keeps this repo under "
            "what it is running. Nothing changed."
            % (said, running, profile, len(losses), running,
               ", ".join(losses), profile, running))


def install_guard(root, apply=False, profile=None):
    """Print or transactionally install the deterministic shared-tree rail.

    reference-transaction refuses branch creation/HEAD departure in the main
    checkout and mutations of branches occupied by another worktree. The
    post-checkout hook is a last-resort pointer-only heal. pre-commit runs the
    refusing lane-discipline VENUE check first (an originated commit on the
    shared checkout's base branch), then the warn-only vacuous-assertion
    advisory and the refusing content rungs, including citation and public-prose
    checks, before never-track closes the staged set. pre-merge-commit runs
    the conflict-marker and never-track rungs over a merge git commits itself,
    under both profiles, because a merge commit never reaches pre-commit.
    Every shared worktree gets
    the add-to-history timing seam that a suite-run guard cannot supply. Existing
    hooks are
    preserved byte-for-byte as executable-mode-aware `.helm-user` companions
    and composed before Helm. Concurrent installers serialize on the hook dir;
    any write failure rolls every changed path back to its exact prior node.

    WITHOUT `--profile` THE TARGET RESOLVES IN THREE STEPS (`guard_profile`):
    the repo's declaration, else the profile its installed hooks are RUNNING,
    else the default its identity carries — leak in a project repo, rail only
    in a checkout of helm's own source, which is the FRESH-repo answer. And a
    flagless install that would still retire artifacts the running profile uses
    REFUSES, naming the `--profile` command that does it on purpose; an
    explicit `--profile` is the operator's decision and is never refused for
    narrowing."""
    # WHETHER A PROFILE WAS TYPED IS A DIFFERENT FACT FROM WHICH ONE RESOLVED,
    # and the narrowing guard below needs the first one: `--profile leak` on a
    # rail repo is the operator's decision and stays open, while the same
    # target arrived at by a flagless refresh is the accident this verb must
    # never commit.
    explicit = profile is not None
    hooks = Hooks(root)
    try:
        profile = profile or guard_profile(root, hooks)
    except ValueError as exc:
        return 1, ["helm work: REFUSED guard install — %s" % exc]
    except OSError as exc:
        # RESOLVING THE TARGET NOW READS THE DISK, so the unreadable-history
        # refusal is owed here too — before, this call could only fail on a
        # declaration, and an unreadable hook set surfaced as a traceback out
        # of a verb that had written nothing. One text for both doors.
        return 1, [_unreadable_running_refusal(root, exc, None)]
    base, plan = _guard_plan(root, profile, hooks)
    assets = _scanner_assets(root, profile, hooks)
    targets = [p["target"] for p in plan]
    safe, scope = _hook_scope(root, targets)
    if not safe:
        return 1, ["helm work: REFUSED guard install — " + scope]
    if not apply:
        # THE DRIFT REPORT LEADS, because it is the only line here that is
        # about the box rather than about the template. Everything below
        # renders what WOULD be written; these say what IS installed.
        lines = _installed_drift(plan)
        lines += ["# scanner snapshot %s <- %s" % (dest, src)
                  for dest, src in sorted(assets.items())]
        for p in plan:
            prior = _path_snapshot(p["target"])
            if prior[0] != "absent" and not _owned_hook(prior):
                lines.append("# preserves existing hook as %s" % p["user"])
            lines += ["# ---- %s ----" % p["target"],
                      p["script"].rstrip("\n"), ""]
        if profile == "leak":
            return 0, lines + [
                "helm work: DRY — would atomically install %d leak-profile "
                "hooks and record %s=leak (--apply installs; pre-commit runs "
                "the conflict-marker scan and the never-track staged-set scan "
                "with its bulk-data leg, "
                "pre-merge-commit runs the conflict-marker and never-track "
                "scans over a merge result git commits itself, pre-push the "
                "host-path scan; HELM_NEVER_TRACK_SKIP=1, "
                "HELM_CONFLICT_MARKER_SKIP=1 and HELM_HOSTPATH_SKIP=1 are the "
                "one-commit owner overrides)"
                % (len(plan), PROFILE_KEY)]
        return 0, lines + [
            "helm work: DRY — would atomically install %d composed hooks "
            "(--apply installs; HELM_WORK_INTEGRATOR=1 clears the "
            "shared-tree ref rail and the lane-discipline venue rung, "
            "nothing else — every other pre-commit rung names its own "
            "one-commit skip in its refusal)"
            % len(plan)]

    # THE BASE-BRANCH PRECONDITION BELONGS TO THE RAIL: its ref hook guards
    # the shared checkout's HEAD, so installing it from any other branch would
    # arm a rule against the very state the installer stands in. The leak
    # profile's two hooks are branch-independent, and an ordinary project repo
    # with a feature branch checked out must not have to switch to install it.
    if profile == "rail":
        rc, current, _err = _git(root, "symbolic-ref", "--short", "HEAD")
        if rc != 0 or current != base:
            return 1, ["helm work: REFUSED guard install — shared checkout HEAD "
                       "is %s, expected %s" % (current or "detached", base)]

    # THE PROFILE IS PART OF THE TRANSACTION. It is read under the lock, the
    # prior value is remembered, and it is written LAST — after every hook
    # and snapshot landed — so a failed or rolled-back install leaves the
    # declaration describing the hooks that are actually there. (Declaring
    # it first would let leak→rail with an unreadable scanner end as a rail
    # declaration over leak hooks, and a flagless reinstall would then
    # change behaviour.)
    os.makedirs(scope, exist_ok=True)
    lockfd = os.open(scope, os.O_RDONLY)
    try:
        fcntl.flock(lockfd, fcntl.LOCK_EX)
        # THE DECLARATION IS RE-READ UNDER THE LOCK and an unparseable one
        # REFUSES — but only the DECLARATION is read here. Asking the combined
        # reader derived a default as well, which is a question about the
        # TARGET, and this install may already have been TOLD its target: an
        # undeclared repository whose identity names no checkout then refused
        # `--profile leak` here, inside the lock, after passing every earlier
        # check — the exact flag the UNKNOWN-identity refusal offers as the
        # recovery. An explicit profile needs no default, so it is not derived,
        # and everything the transaction owes about the profile this repo is
        # LEAVING (the observation below, the retirements, the rollback) is
        # unchanged: an unreadable history still refuses.
        try:
            declared = checked_declaration(root)
        except ValueError as exc:
            return 1, ["helm work: REFUSED guard install — %s" % exc]
        # THE PROFILE THIS REPO IS LEAVING IS WHAT IT IS UNDER RIGHT NOW: what
        # it RECORDED, and when it recorded nothing, the hook set actually
        # installed. A default answers "what should an undeclared repo be
        # judged under", which is a question about the TARGET only; asking it
        # here made a project repo that had been put under the rail before it
        # ever recorded a profile answer "leak", so nothing was departing and
        # the rail's hooks kept running under a leak declaration. When neither
        # the config nor the disk says anything, there is no outgoing profile
        # and nothing to retire.
        # AND THE DECLARATION IS AUTHORITATIVE, so it SHORT-CIRCUITS the
        # observation rather than being preferred to it afterwards. Reading the
        # disk unconditionally made a declared repo's install depend on slots
        # its profile does not own: a declared-leak repo with healthy leak
        # hooks and an unrelated UNREADABLE foreign commit-msg hook raised
        # PermissionError out of the rail-only inference — before the
        # transaction's rollback handler existed, so the verb died with a
        # traceback over a repo it had no reason to look at.
        #
        # AND AN UNKNOWN OUTGOING PROFILE REFUSES. When nothing is declared the
        # disk is the only witness, and an install that cannot read it does not
        # know what it is moving this repo OFF. Classifying that as "no
        # departures, record the target" certified the leak profile over a rail
        # that was still armed: the reference-transaction, post-checkout and
        # commit-msg hooks kept executing outside every census while the
        # declaration said leak, and the note that admitted the observation had
        # failed changed nothing about the recording. AN UNREADABLE INPUT IS
        # NEVER A PASS and the leak profile never speaks for the rail, so this
        # refuses BEFORE any write and leaves the repo exactly as armed as it
        # was. The owner decides it with --profile, or makes the path readable.
        #
        # AND THE ADVICE NAMES THE RECOVERY THAT WORKS. The first refusal
        # offered "--profile rail|leak", which cannot recover this state: that
        # flag names the profile this repo is moving TO, and the observation
        # that failed is of the profile it is moving OFF — read here whenever
        # the CONFIG declares nothing, whatever the flag says. So every retry
        # hit the same unreadable path and refused again, while the operator
        # believed they had been handed a way through. Two things do settle it:
        # making that exact path readable, and RECORDING the declaration, which
        # short-circuits the observation at the line above.
        installed = None
        if declared is None:
            try:
                installed = installed_profile(root, hooks)
            except OSError as exc:
                return 1, [_unreadable_running_refusal(root, exc, profile)]
        # A FLAGLESS INSTALL MAY NOT NARROW THIS REPO. The resolver above keeps
        # an undeclared repo under what it is running, so the only way a target
        # can still retire live artifacts with no flag typed is a DECLARATION
        # that disagrees with the disk — someone recorded leak while the rail
        # is armed. Retiring rungs there is the same accident by another route:
        # nobody asked for a narrowing, the staleness notice's own remedy is a
        # flagless refresh, and a repo that silently loses its lane-discipline,
        # prose and conflict-marker rungs looks correct afterwards. So it
        # refuses and names the command that does it ON PURPOSE. An explicit
        # --profile is untouched: that is the operator deciding, and it keeps
        # its retirement notes.
        #
        # THE DISK IS READ DEFENSIVELY WHEN A DECLARATION EXISTS. Reading it
        # unconditionally is the killed design two blocks up — a declared repo
        # with one unreadable FOREIGN hook died in the rail-only inference — so
        # an unreadable disk here means only that this check cannot speak, and
        # the declaration stands.
        running = installed
        if running is None and declared is not None:
            try:
                running = installed_profile(root, hooks)
            except OSError:
                running = None
        if not explicit and running is not None and running != profile:
            refusal = _narrowing_refusal(root, plan, assets, declared,
                                         running, profile, hooks)
            if refusal:
                return 1, [refusal]
        previous = declared or installed or profile
        # HOOKS THE OLD PROFILE PLANNED AND THE NEW ONE DOES NOT are retired
        # in the same transaction: a rail→leak switch that left the
        # reference-transaction, post-checkout and commit-msg hooks running
        # would enforce rules the declaration no longer names, outside every
        # census. Only helm-owned hooks are touched; a foreign hook at one of
        # those names is not ours to remove.
        #
        # AND THE DISK IS A SECOND WITNESS TO WHAT IS DEPARTING, not only the
        # declaration. A repo can DECLARE one profile while RUNNING another —
        # that is precisely the state the flagless refusal above names — and
        # retiring only what the declaration planned left an explicit
        # `--profile leak` there with the rail's three slots and eleven
        # snapshots still armed under a leak declaration: the same
        # declaration-describes-hooks-nobody-runs disease this transaction's
        # outgoing-profile rule exists to end, entered from the other side. So
        # both witnesses are asked and the retirement is their union. The read
        # is the defensive one above, so an unreadable disk still costs a
        # declared repo nothing.
        outgoing = {previous} | ({running} if running else set())
        # THE NOTE NAMES THE PROFILE THE RETIRED PATH ACTUALLY CAME FROM.
        # Rendering `previous` was true only while the declaration was the
        # sole witness; with the disk asked too, a repo declaring leak while
        # running the rail would have read "planned by the leak profile, not
        # by leak". Only two profiles exist, so removing the target from the
        # outgoing set always leaves exactly the one that is departing.
        departing = " / ".join(sorted(outgoing - {profile})) or previous
        leaving = []
        planned = {p["name"] for p in plan}
        for was in sorted(outgoing - {profile}):
            for name, _template in GUARD_PROFILES[was]:
                if name not in planned:
                    path = hooks.path(name)
                    if path not in leaving:
                        leaving.append(path)
        # ARCHIVES BEFORE THE SLOTS THEY EMPTY — the same rule as the retired
        # paths below: the archive of a helm-owned companion is written
        # before the companion is unlinked, so no window exists (not even a
        # KeyboardInterrupt, which the rollback's `except Exception` does
        # not see) in which its only bytes live nowhere.
        leaving_archives = [path + RETIRED_HOOK_SUFFIX for path in leaving]
        leaving_users = [path + ".helm-user" for path in leaving]
        leaving_companions = leaving_archives + leaving_users
        orphan_assets = sorted(set().union(
            *(set(_scanner_assets(root, was, hooks))
              for was in outgoing)) - set(assets))
        asset_paths = sorted(assets)
        retired_paths = [p["retired"] for p in plan]
        paths = (asset_paths + targets + [p["user"] for p in plan]
                 + retired_paths + leaving + leaving_companions + orphan_assets)
        # A PATH THIS TRANSACTION CANNOT READ IS A PATH IT CANNOT RESTORE, and
        # an install that cannot roll back is not a transaction. This snapshot
        # is the rollback record, so an unreadable member of it refuses here —
        # before the first byte is written — instead of raising out of the verb
        # after the walk above decided what to retire.
        try:
            before = {path: _path_snapshot(path) for path in paths}
        except OSError as exc:
            return 1, ["helm work: REFUSED guard install — %s. This install "
                       "moves %s → %s and cannot snapshot a path it would have "
                       "to restore, so nothing was changed."
                       % (exc, previous, profile)]
        desired = {}
        notes = []
        # A MANAGED WRAPPER IS RETIRED BY GIVING THE HOOK NAME BACK. The rail
        # composes a user's original hook in as `<name>.helm-user` and runs
        # it from the wrapper; deleting the wrapper alone would leave that
        # hook on disk under a name git never invokes — the user's own
        # enforcement silently off. So a foreign companion is restored to
        # the hook name exactly as it was (bytes, mode, symlink-ness), and
        # only a helm-owned companion (a superseded helm hook) is retired.
        for path in leaving:
            companion = path + ".helm-user"
            kept = before[companion]
            if _owned_hook(before[path]):
                if kept[0] != "absent" and not _owned_hook(kept):
                    desired[path] = kept
                    desired[companion] = ("absent", None, None)
                    notes.append("helm work: retired the managed %s and restored "
                                 "the user's own hook to that name (from %s)"
                                 % (path, companion))
                else:
                    desired[path] = ("absent", None, None)
                    if kept[0] != "absent":
                        # a HELM-OWNED companion is a superseded helm hook
                        # this installer preserves — archived to the same
                        # non-executable .helm-superseded form the retained
                        # plan uses, never deleted
                        archive = path + RETIRED_HOOK_SUFFIX
                        want = _retired_form(kept)
                        if (before[archive][0] != "absent"
                                and before[archive] != want):
                            return 1, ["helm work: REFUSED guard install — %s "
                                       "holds bytes that are not this "
                                       "companion's; nothing changed" % archive]
                        desired[archive] = want
                        desired[companion] = ("absent", None, None)
                        notes.append("helm work: archived superseded helm hook "
                                     "%s to %s (non-executable, bytes intact)"
                                     % (companion, archive))
                    notes.append("helm work: retired %s — planned by the %s "
                                 "profile, not by %s"
                                 % (path, departing, profile))
            elif before[path][0] != "absent":
                notes.append("helm work: left %s in place — not a helm hook"
                             % path)
        stale_assets = [path for path in orphan_assets
                        if before[path][0] != "absent"]
        for path in stale_assets:
            desired[path] = ("absent", None, None)
        if stale_assets:
            # SAYING SO IS PART OF RETIRING IT. A snapshot the incoming
            # profile's hooks never run is dead weight that `drift` and
            # `doctor` would keep judging, and a silent deletion leaves the
            # reader believing the rail's shelf is still there.
            notes.append("helm work: retired %d scanner snapshot(s) the %s "
                         "profile ran and %s does not: %s"
                         % (len(stale_assets), departing, profile,
                            ", ".join(os.path.basename(p)
                                      for p in stale_assets)))
        for dest, source in assets.items():
            try:
                with open(source, "rb") as f:
                    desired[dest] = ("file", f.read(), 0o644)
            except OSError as exc:
                return 1, ["helm work: REFUSED guard install — scanner source "
                           "%s unreadable: %s" % (source, exc)]
        for p in plan:
            target, user, retired = p["target"], p["user"], p["retired"]
            prior, preserved = before[target], before[user]
            if prior[0] == "other" or preserved[0] == "other":
                return 1, ["helm work: REFUSED guard install — unsupported hook "
                           "node at %s" % (target if prior[0] == "other" else user)]
            # THE RETIRE DECISION IS FIRST, AND IT DECIDES WHETHER THE USER
            # SLOT IS FREE. A companion carrying a helm marker is this
            # project's own superseded hook, and the managed hook runs it
            # ahead of the template on every invocation — so its body can
            # answer before the installed rules do. Emptying the slot here is
            # what lets the preserve branch below see a genuine user hook
            # arrive into an unoccupied companion path.
            if _owned_hook(preserved):
                want_retired = _retired_form(preserved)
                if before[retired][0] != "absent" and before[retired] != want_retired:
                    return 1, ["helm work: REFUSED guard install — %s holds "
                               "bytes that are not this companion's; nothing "
                               "changed" % retired]
                desired[retired] = want_retired
                desired[user] = ("absent", None, None)
                preserved = ("absent", None, None)
                notes.append("helm work: retired superseded helm hook %s to %s "
                             "(non-executable, bytes intact)" % (user, retired))
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
            # RETIRED PATHS ARE WRITTEN BEFORE THE USER SLOT THEY EMPTY, so
            # no window exists in which the bytes live nowhere.
            # a restored user hook is written at its name (in `leaving`) and
            # an archived companion at its archive (`leaving_archives`)
            # BEFORE the companion slot (`leaving_users`) is emptied, so the
            # bytes never live nowhere
            for path in (asset_paths + retired_paths
                         + [p["user"] for p in plan] + targets
                         + leaving + leaving_archives + leaving_users
                         + orphan_assets):
                want = desired.get(path)
                if want is None or before[path] == want:
                    continue
                changed.append(path)
                _put_snapshot(path, want)
            if previous != profile or declared is None:
                declare_profile(root, profile)
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
        if previous != profile or declared is None:
            # THE NOTE NAMES THE DEFAULT THIS REPO GOT, never the product's.
            # "undeclared (rail by default)" was true of the constant and wrong
            # about the reader's repo: it told an adopter installing the leak
            # legs that helm's shared-checkout rail was their baseline
            # (task/2441, measured from a project repo).
            if declared:
                was = declared
            elif installed is not None:
                # AN UNDECLARED REPO WITH HOOKS ON DISK IS NOT A FRESH ONE.
                # Naming the default here would report the profile this repo
                # would have been judged under while the transaction was in
                # fact moving it off the one it was RUNNING.
                was = "undeclared (%s installed)" % installed
            else:
                # THESE NOTES RUN AFTER THE TRANSACTION, outside its rollback,
                # so nothing here may raise over persistent mutation. An
                # explicit --profile reaches this line without the identity
                # ever being classified, and an identity that names no checkout
                # REFUSES in `default_profile` — which is the right answer to
                # "which profile would this repo have been judged under" and
                # the wrong thing to do to a successful install.
                try:
                    was = "undeclared (%s by default)" % default_profile(root)
                except (UnknownRepositoryIdentity, OSError):
                    was = "undeclared (identity names no readable checkout)"
            notes.append("helm work: profile %s → %s recorded as %s"
                         % (was, profile, PROFILE_KEY))
        if profile == "leak":
            return 0, notes + [
                "helm work: leak guard %s in %s (profile leak)" % (state, scope),
                "helm work: pre-commit now enforces the never-track law on the "
                "staged set — bulk data (exports, dumps, snapshots, archives, "
                "address lists, blobs over the ceiling) and private needles — "
                "and refuses conflict-marker lines, before any commit becomes "
                "history (one-commit skips: HELM_CONFLICT_MARKER_SKIP=1, "
                "HELM_NEVER_TRACK_SKIP=1); pre-merge-commit runs the "
                "conflict-marker and never-track scans over a merge git "
                "commits itself (`git merge --no-ff`), against what the "
                "target branch already tracks. A blob over the ceiling is "
                "judged against HEAD at both doors; needles, markers and "
                "addresses a merged parent already carries are refused at "
                "pre-merge-commit and admitted when the merge is concluded "
                "with `git commit` (one-merge skips: "
                "HELM_CONFLICT_MARKER_SKIP=1, "
                "HELM_NEVER_TRACK_SKIP=1); pre-push REFUSES host paths bound "
                "for a public remote (one-commit skip: HELM_HOSTPATH_SKIP=1). "
                "No branch, lane, citation or prose rung is installed under "
                "this profile — `--profile rail` is the shared-checkout rail."]
        return 0, notes + _seatname_authority_notes() + [
                           "helm work: guard rail %s in %s" % (state, scope),
                           "helm work: shared checkout branch creation/switch now "
                           "FAILS before mutation; occupied worktree branches are "
                           "protected (override: HELM_WORK_INTEGRATOR=1)",
                           "helm work: pre-commit now REFUSES a commit that "
                           "ORIGINATES work on '%s' in the shared checkout — no "
                           "lane, no gate, no verdict (a FOLD is untouched by "
                           "that rung: merges build through pre-merge-commit, "
                           "which runs only the conflict-marker and never-track "
                           "scans over the merge result, and a conflicted "
                           "fold's MERGE_HEAD is admitted). "
                           "Integrator's own direct commits: "
                           "HELM_WORK_INTEGRATOR=1; one-commit skip for that "
                           "rung alone: HELM_LANE_DISCIPLINE_SKIP=1" % base,
                           "helm work: pre-commit now WARNS on staged vacuous tests, "
                           "REFUSES staged merge-conflict marker lines (one-commit "
                           "skip: HELM_CONFLICT_MARKER_SKIP=1), REFUSES a real seat "
                           "identity staged into public-bound tests/ (one-commit "
                           "skip: HELM_SEATNAME_SKIP=1; the rung is a documented "
                           "no-op until seat-names.txt is populated, and says so), "
                           "REFUSES internal chronology added to public-bound "
                           "source prose (one-commit skip: "
                           "HELM_WORLD_PROSE_SKIP=1), REFUSES a retired "
                           "top-level name still spelled in the tree the "
                           "commit produces (one-commit skip: "
                           "HELM_RETIRED_NAME_SKIP=1), then enforces the "
                           "never-track law before any commit becomes history "
                           "(never-track one-commit skip: HELM_NEVER_TRACK_SKIP=1)"]
    finally:
        os.close(lockfd)
