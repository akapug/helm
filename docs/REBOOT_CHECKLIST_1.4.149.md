# Credhoming reboot checklist — the live claude 1.4.149 switch

Run this the moment the host's `claude` CLI flips to 1.4.149 (or any new
harness build). It re-verifies the owner's twin concern by hand — **credhoming
is correct BOTH when helm hosts standalone AND when a metaharness (orca/herdr)
hosts the seat** — so a harness upgrade that silently changed env handling,
config-home resolution, or the child-session stamp is caught at the switch, not
days later in a leaked session.

Each row is one command with its EXPECTED result. A mismatch is a STOP: do not
mint new seats until it reads as written. The automated backstop for these same
invariants is `tests/test_credhoming_parity.py` — run it first:

    python3 -m pytest tests/test_credhoming_parity.py -q
    # EXPECT: all pass. If red here, the switch broke a credhoming invariant —
    #         fix before any live seat mint.

---

## 1. Accounts still enumerate, no secret leaks
    helm codex list
    # EXPECT: one row per account (name · email · tier · pooled column).
    #         NO refresh_token / access_token / JWT bytes (no "eyJ…") on screen.

## 2. Cred-% gate still reads live usage before a mint
    helm codex launch            # no --force
    # EXPECT: a verdict — either "gate ok" and a delegated mint, or "REFUSE:
    #         no pooled cred reads ok" naming the concrete `helm codex pool <x>`
    #         fix. Never a silent mint on an exhausted account.

## 3. LAUNCH.SH AUTHORITATIVE — the seat's launch.sh beats a polluted parent
This is the orca-can't-clobber-us row. Mint, then run the launch.sh under a
DELIBERATELY polluted parent env (what an orca host launched from an owner shell
can carry) and confirm the seat's own pin wins.

    helm seat launch codex
    # EXPECT: a line starting `env -u ANTHROPIC_API_KEY …` that PINS
    #         `CLAUDE_CONFIG_DIR=<…/seats/codex/claude>` and ends
    #         `claude --dangerously-skip-permissions --model …`.

    SEAT=$(helm home)/…/seats/codex          # the seat dir (see `helm seat status`)
    CLAUDE_CONFIG_DIR=/tmp/ORCA-POLLUTION \
      ANTHROPIC_API_KEY=sk-OWNER-KEY \
      CLAUDE_CODE_CHILD_SESSION=stamp \
      sh "$SEAT/launch.sh" --version    # or any harmless claude flag
    # EXPECT: claude 1.4.149 boots against the SEAT's config home
    #         (…/seats/codex/claude), NOT /tmp/ORCA-POLLUTION; the owner key is
    #         absent (env -u stripped it); transcript persistence is ON (the
    #         CLAUDE_CODE_CHILD_SESSION stamp was unset, so the session is
    #         top-level, not a subprocess child).
    # HOW TO CONFIRM the config home: after the seat runs one turn, its
    #   transcript lands under $SEAT/claude/projects/…, never under
    #   /tmp/ORCA-POLLUTION/projects.

## 4. PER-ACCOUNT ISOLATION — two accounts never cross-pollinate
    helm seat launch codex      # account 1
    helm seat launch kimi       # account 2 (or a second codex account)
    # EXPECT: two DIFFERENT `CLAUDE_CONFIG_DIR=` homes and two DIFFERENT
    #         `ANTHROPIC_AUTH_TOKEN=` values. Neither launch line carries the
    #         other seat's token or home. A seat pinned to account A can never
    #         read account B's session store or creds.
    helm codex pooled
    # EXPECT: one pool file per ACCOUNT (keyed on account_id, never on email) —
    #         two accounts that share an email stay TWO rows, each carrying only
    #         its own token.

## 5. HARNESS CRED-BLIND — the metaharness only ever sees a PATH
    helm seat resume codex
    # EXPECT: the pane command the metaharness (orca/herdr) is handed is the
    #         launch.sh PATH plus `--continue`/`--resume <sid>` — NEVER the
    #         expanded line. No `ANTHROPIC_AUTH_TOKEN=` and no token bytes in
    #         the spawn command, pane title, or any error echo.

## 6. HELM WITHOUT ORCA — standalone credhoming still works
    HELM_METAHARNESS=none helm seat resume codex
    # EXPECT: rc 1 with the recommendation line (orca/herdr are OPTIONAL
    #         companions) AND a pasteable `<launch.sh> --continue` line — helm
    #         degrades to a copy-paste, never crashes, and the launch.sh it
    #         hands back carries the SAME authoritative pins as rows 3–4.
    helm doctor
    # EXPECT: a metaharness row: OK naming orca/herdr when installed, or a WARN
    #         recommending them as OPTIONAL — a missing metaharness is never an
    #         error, because helm hosts credhoming on its own.

---

*Green on all six + the pytest backstop = credhoming parity holds across the
1.4.149 switch, orca-hosted and standalone alike.*

---

# EXPERIMENT — orca 1.4.149 credhoming: what actually happens

Rows 1-6 above verify helm's *existing* guarantees survive the switch. This
section is different: it is an OPEN EXPERIMENT the owner asked for. We stopped
theorizing about orca's internals — run these after the upgrade and record the
answers, because they decide whether helm should keep pinning
`CLAUDE_CONFIG_DIR` per seat or DEFER to orca's per-account credhoming.

## Why it matters (the two cases)

A `/login` writes credentials INTO the config home the session is currently
using — it never moves the session elsewhere (MEASURED 2026-07-21: the dir named
`cto-example` ended up holding `david@mv`, creds overwritten, no backup).
That plays out two ways:

* **Case A — launched WITH a credhome** (`CLAUDE_CONFIG_DIR` pinned; every helm
  seat). Pollution is CONTAINED to that one home. The pin is a blast-radius
  container.
* **Case B — launched WITHOUT one** (the shared default `~/.claude`). One
  `/login` rewrites the home that EVERY un-pinned session reads — they all
  silently change account on their next token refresh. Shared-fate.

And the physics neither side can beat: a RUNNING process's environment is
immutable, so no switcher — orca's included — can repoint a live session's
config home. The only non-destructive move is re-resuming the SAME session
against a different home (`helm session port --cred <home> <sid>`).

## The matrix — run each, record the result

| # | Case | Action | Question to answer |
|---|------|--------|--------------------|
| E1 | A (helm-pinned seat) | orca "switch account" on that pane | Does orca repoint it at all, or does helm's exec-line pin win? (expected: pin wins, orca is a no-op) |
| E2 | A (helm-pinned seat) | `/login` in the pane | Does it still overwrite the PINNED home in place? Any new orca-side backup/versioning? |
| E3 | B (un-pinned pane) | orca "switch account" | Does orca swap the pane to `claude-accounts/<uuid>/auth` cleanly, leaving `~/.claude` untouched? (the non-polluting pointer-swap) |
| E4 | B (un-pinned pane) | `/login` | Confirm the shared-default blast radius: do OTHER un-pinned sessions change account on refresh? |
| E5 | A or B | orca switch on a LIVE vs a NEW pane | Does orca apply the switch to the running pane (impossible per the physics — expect NEW panes only) or relaunch it? |
| E6 | — | inspect | After a switch, is the previous account's cred still intact in its own `claude-accounts/<uuid>/auth` dir (recoverable), unlike `/login`? |

## The decision the answers drive

* If **E3 + E6 are clean** (orca swaps per-account dirs non-destructively and
  keeps each account's creds intact), then orca's model is strictly better for
  SWITCHING, and helm should be able to target orca's account dirs as valid
  homes — one shared cred pool instead of two (`helm session port --cred`
  accepting `~/.config/orca/claude-accounts/<uuid>/auth`).
* But helm should KEEP per-seat pinning unless orca 1.4.149 also sub-isolates
  panes WITHIN one account: helm homes per-SEAT (codex and codex-2 isolated on
  one account), orca homes per-ACCOUNT. Deferring without sub-isolation would
  force one-seat-per-account and collide with the single-open session law.
* Either way `/login` stays the fallback, made safe by `helm cred`
  (backup-before-switch, identity-read-from-CONTENT-not-dir-name, drift heal).

*Record the answers inline here at the switch — this file is the durable home
for what we learn.*

## VENDOR ANSWERS (orca team, Brennan — 2026-07-21, in response to the owner)

Several matrix rows are answered upstream; recorded verbatim-in-substance so we
only spend the live test on what is genuinely still open.

**The build we are ON (pre-rc.2):** all managed Codex accounts share ONE runtime
home, and switching **hot-swaps the credentials in place**, so every running
codex follows the currently selected account — one account at a time. The team
confirms this is a real limitation and that "a custom multi-home setup was a
reasonable workaround" (that workaround is helm's credhomes). Note this is the
SAME root cause we measured independently via `/login`:
**credential-overwrite-in-place**, on a different surface.

**Starting 1.4.149-rc.2** each managed account gets its **own self-contained
home** — credentials, config, and session history fully isolated per account:

* Sessions launched under different accounts **run concurrently**, each against
  its own cred home. Launch under A, switch the picker to B, launch more → A's
  sessions keep running as A.
* **Automatic resume pins each session to the account that created it**, even if
  the global selection changed since. No more sessions silently hopping accounts.
* Usage/history are tracked per-account.
* The picker chooses which account NEW terminals get: the flow is
  **switch → launch**, not a per-terminal dropdown.

### What that resolves

* **E3, E5, E6 → answered.** Per-account dirs are the model; a switch applies to
  NEW terminals while running sessions keep their account (which matches the
  immutable-env physics above); each account's creds stay in its own home rather
  than being overwritten.
* **Case B's shared-fate problem is fixed for orca-managed panes**: switching the
  picker no longer reaccounts running sessions.
* **helm's per-ACCOUNT credhoming becomes redundant with orca rc.2+** — orca now
  provides the guarantee helm's credhomes were built for (isolated per-account
  creds + sessions pinned to their creating account).

### RESOLVED by ground truth — no live test needed

The earlier framing here asked whether two helm SEATS sharing one ACCOUNT would
collide in one shared orca home. Measured on this host, that question is moot:

* **codex / kimi seats hold NO credentials.** Their `CLAUDE_CONFIG_DIR`
  (`seats/codex/claude`, `seats/codex/instances/codex-2/claude`, …) is a SESSION
  dir; the actual credential lives in the local CLIProxyAPI auth dir
  (`ANTHROPIC_BASE_URL=127.0.0.1:8317`). codex, codex-2 and codex-3 are three
  seats on ONE codex account, and their dirs exist to keep SESSIONS apart, not
  creds. Orca's credhoming — any version — neither helps nor hurts them.
* **For claude-direct sessions the credhome IS the account store, and multiple
  sessions already share one home cleanly.** Proven live, not theorised:
  `opus-integrator` and `helm-claude` both ran for hours against
  `~/.claude-homes/cto-example` — two sessions, one account, one home, zero
  collisions.

**Therefore:** orca rc.2's per-ACCOUNT isolation is SUFFICIENT — there is no
sub-account isolation requirement to satisfy. helm can defer credhoming for
claude-direct sessions to orca rc.2+, while proxy seats keep their own session
dirs regardless (they were never cred homes).

### What is STILL open

* **E8:** does a `/login` INSIDE an orca-managed pane still overwrite that
  account's home in place (leaving a home named for A holding B)? If yes, the
  `credhome-name-account-drift` class survives rc.2 and `helm cred`
  (backup + identity-from-content + heal) remains necessary regardless of who
  owns the home. This is the one row worth running live.

*Owner feedback loop: the orca team explicitly asked for feedback from exactly
this multi-account power-user case. The measured `/login` drift and the
proxy-seat decoupling above are the feedback worth sending back.*
