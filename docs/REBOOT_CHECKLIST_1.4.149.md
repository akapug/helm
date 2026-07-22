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
