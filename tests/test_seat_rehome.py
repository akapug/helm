#!/usr/bin/env python3
"""task/2573 — `helm seat rehome` moves a live seat onto a credhome in one verb.

HERMETIC AND REAL. Every arm drives the SHIPPED producer over real fixture
files: a real credhome under a temp claude-homes root, a real Orca account
store under a temp ORCA_USER_DATA_PATH, a real chat roster under a temp
HELM_HOME, a real beacon registry row, a real /proc tree under a temp
HELM_PROC (so `beacons.proc_env` and `beacons.proc_argv` do the reading), and
a real ledger file. The only substitutions are the two things a unit test must
never have: the /proc census (`orcaadopt.claude_processes`, whose rows ARE the
fixture) and the metaharness adapter — and the adapter double SUBCLASSES
`harness._CLIAdapter`, so `send_to_pane` runs the REAL split-send-plus-read-
back `submit` rather than a second implementation of it. No live pane, seat,
home or process is touched.

THE CENSUS IS A LIVE LIST, NOT A SNAPSHOT, and the apply fixture's whole
causality depends on it: the /exit REMOVES the seat's row and the launch
keystroke ADDS the new process, rewrites the roster row and arms the beacon.
Nothing the verification reads is planted before the launch line is typed, so
an arm that never typed it has nothing to be believed by. `pid_alive` and
`authorized_handle` are wrapped, never reimplemented — the spy records what
production passed and hands it to the shipped door.

THE FIXTURE LAYOUT IS THE STORE'S OWN, taken from tests/test_cred_orca.py so
there is one description of Orca's shape in this tree rather than two:
  <orca>/claude-accounts/<uuid>/auth/{.credentials.json, oauth-account.json,
                                      .orca-managed-claude-auth}
  <claude-homes>/<folded-email>/{.claude.json, .credentials.json}
Every token is a synthetic FAKE string and every email is at example.test.
"""
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helm import (beacons, chat, cred, harness, homes,  # noqa: E402
                  orcaadopt, panetail, seat_rehome, seat_resume_all, seats)
# ONE description of the measured exit-confirm screen in this tree: the
# recogniser's own suite owns the fixture and its pointer mover.
from tests.test_panetail import exit_dialog_focused_on  # noqa: E402

EMAIL = "seat@example.test"
UUID = "0a1b2c3d-0000-4000-8000-00000000cafe"
SEAT = "example-seat"        # noqa: SEAT_NAME — a fixture identity; no fleet seat wears it
SID = "11111111-2222-4333-8444-555555555555"
PANE_KEY = "pk-1"
HANDLE = "handle-of-" + PANE_KEY
SEAT_PID, SEAT_START = 4242, "77"
BEACON_PID = 4243
# The process a rehome's OWN launch line starts: a different identity, and a
# LARGER birth stamp, because /proc field 22 orders births against each other
# and "younger than the session it replaced" is the whole claim.
NEW_PID, NEW_START = 5151, "990"
# THE LOOK-ALIKE NEIGHBOUR: another claude that comes up in the seat's pane on
# the planned home, session and model, ALSO younger than the exited session.
# Every ordering test passes on it, which is why the marker exists.
NEIGHBOUR_PID, NEIGHBOUR_START = 6262, "880"
# A marker minted by SOMEBODY ELSE'S attempt — a real uuid4, so the only thing
# that makes it wrong is that it is not the one this apply minted.
FOREIGN_MARK = "beefbeef-1111-4222-8333-444444444444"
OTHER_PANE_KEY = "pk-2"
OTHER_HANDLE = "handle-of-" + OTHER_PANE_KEY


def _marker_in(line):
    """The attempt marker a composed pane line assigns, read the way a shell
    reads it: a NAME=value word standing in front of the command. Never a
    substring search — the point of the assertion is WHERE the assignment sits,
    and `in` cannot tell a prefix from an export."""
    for word in shlex.split(line):
        if word.startswith(seat_rehome.ATTEMPT_ENV + "="):
            return word.split("=", 1)[1]
    return None

# A pane frame whose composer is EMPTY — one that took its turn. Synthetic, but
# a real frame's shape, because `submit` proves delivery by READING THE COMPOSER
# BACK: a double returning anything without a composer models an UNREADABLE pane
# (UNKNOWN), which is a different arm from the one every test below writes.
ADVANCED_PANE = "\n".join(("─" * 40, "❯", "─" * 40,
                           "  opus-5 | ~/dev/example/repo",
                           "  ⏵⏵ bypass permissions on"))


def _framed(*rows):
    """A pane tail with an input box, its statusline and `rows` below it: the
    chrome a real footer sits in."""
    return "\n".join(("─" * 40, "❯", "─" * 40,
                      "  opus-5 | ~/dev/example/repo") + rows)


#: The permission-mode footers, measured on throwaway panes of Claude Code
#: 2.1.284 (task/3515); `default` draws the manual row.
PLAN_FOOTER = "  ⏸ plan mode on (shift+tab to cycle) · ← for agents"
ACCEPT_FOOTER = "  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents"
MANUAL_FOOTER = "  ⏸ manual mode on · ? for shortcuts · ← for agents"


def _perm_in(line):
    """The permission words a composed launch line hands claude: what follows
    the `--` of its `helm launch` invocation, read the way a shell reads it.
    The launched double inherits exactly these, so a line that dropped the
    mode starts a process that runs without it."""
    words = shlex.split(line)
    rest = words[words.index("--") + 1:] if "--" in words else []
    out = []
    for i, word in enumerate(rest):
        if word == "--dangerously-skip-permissions":
            out.append(word)
        elif word == "--permission-mode" and i + 1 < len(rest):
            out += [word, rest[i + 1]]
    return out


def _now_ms(hours=0):
    return int((time.time() + hours * 3600) * 1000)


def _creds(token, hours, refresh_hours=24 * 20):
    return {"claudeAiOauth": {
        "accessToken": token + "-ACCESS", "refreshToken": token + "-REFRESH",
        "expiresAt": _now_ms(hours),
        "refreshTokenExpiresAt": _now_ms(refresh_hours),
        "scopes": ["user:inference"], "subscriptionType": "max"}}


def _proc(pid=SEAT_PID, start=SEAT_START, seat=SEAT, pane_key=PANE_KEY):
    """A row shaped like `claude_processes()` produces one. `start` is never
    None by default: a real /proc row always has a birth stamp, and a fixture
    without one exercises the UNSTAMPED refusal in every arm instead of the
    behaviour under test."""
    return {"pid": pid, "start": start, "seat": seat, "pane_key": pane_key,
            "resume_sid": SID, "worktree_id": None}


class _PaneAd(harness._CLIAdapter):
    """An orca adapter that turns pane keys into handles and RECORDS its sends.

    Subclasses the REAL base so the turn verb under test is helm's own.
    """

    name = "orca"

    def __init__(self):
        self.sent, self.typed = [], {}
        # A pane-key REMAP an arm can inject between two resolutions, which is
        # the world-change the one-binding guard exists to refuse.
        self.remap = {}
        # WHAT THE LAUNCH LINE ACTUALLY STARTS. The surfaces a rehome verifies
        # are produced HERE, by the keystroke under test, and by nothing else.
        self.on_launch = None
        # THE PERMISSION-MODE FOOTER the pane draws (task/3515): None keeps
        # the frame's own bypass row, "" draws none, text replaces it.
        self.footer = None

    def resolve_pane(self, key):
        return {"handle": self.remap.get(key, "handle-of-" + key)}

    def list(self):
        return [{"handle": HANDLE, "status": "connected"}]

    def read(self, handle, limit=3000, timeout=60):
        pane = ADVANCED_PANE
        if self.footer is not None:
            pane = pane.replace("  ⏵⏵ bypass permissions on", self.footer)
        text = self.typed.get(handle)
        if text is not None:
            return pane.replace("\n❯\n", "\n❯\xa0%s\n" % text)
        return pane

    def send(self, handle, text, enter=True, timeout=60):
        if enter:
            self.typed.pop(handle, None)
        else:
            self.typed[handle] = text
        if text:
            self.sent.append((handle, text, enter))
        if enter and text and self.on_launch and "helm launch" in text:
            self.on_launch(handle, text)


#: What the pane shows once the session has exited: the resume hint and the
#: shell's prompt, with no composer and no dialog.
SHELL_PANE = "\n".join(("Resume this session with:", "claude --resume " + SID,
                        "~/dev/example/repo $"))
#: A session that took /exit and never finished leaving: no composer, no
#: dialog, and a process that is still alive.
HUNG_PANE = "\n".join(("\u25cf Goodbye!", "\u273b Flushing the transcript\u2026"))


class _ExitDialogAd(_PaneAd):
    """A pane whose claude holds BACKGROUND TASKS — every seat with an armed
    inbox beacon does — so `/exit` opens Claude Code's exit-confirm dialog
    (task/3201, the measured screen in tests/fixtures) instead of exiting.

    THE DIALOG IS CAUSED, NEVER PLANTED: it opens only when a bare Enter
    submits a composer holding `/exit`, with the pointer where the arm put it
    (`focus`; Claude Code opens it on option 1). It answers keys the way the
    measured dialog does (task/3209, a throwaway pane): an option's DIGIT
    selects that option with no Enter, and a bare Enter selects the option
    under the pointer. Options 1 and 2 end the process; 3 ("Stay") closes the
    dialog and keeps the session. `dialog=False` is the plain composer exit,
    the positive control: the Enter that submits /exit is the whole exit.

    `plain` is a session with NO background tasks whose pane cannot show the
    exit: "shell" exits on the /exit's own Enter and leaves the shell prompt
    (no composer to read back), "hang" keeps the process alive behind a frame
    with no composer. Either way the wake reads UNVERIFIED, and only the pid
    can say what happened.

    `keys` records EVERY send, bare Enters included, with the screen it landed
    on, so an arm can say which keystroke reached the dialog and which did not.
    `exited` is the process: None defers to the arm's own spy (the plain exit),
    False/True is this pane's truth once a dialog or a `plain` exit is in play.
    """

    def __init__(self, focus=1, dialog=True, showing=False, plain=None):
        super().__init__()
        self.focus, self.dialog, self.plain = focus, dialog and not plain, plain
        self.showing = HANDLE if showing else None
        self.exited = False if (dialog or plain) else None
        self.hung = False
        self.keys = []

    def read(self, handle, limit=3000, timeout=60):
        if self.exited:
            return SHELL_PANE
        if self.hung:
            return HUNG_PANE
        if self.showing == handle:
            return exit_dialog_focused_on(self.focus)
        return super().read(handle, limit=limit, timeout=timeout)

    def send(self, handle, text, enter=True, timeout=60):
        on = ("dialog" if self.showing == handle else
              "shell" if self.exited else "composer")
        self.keys.append((handle, text, enter, on))
        if on == "dialog":
            chosen = (int(text) if (text.isdigit() and not enter) else
                      self.focus if (enter and not text) else None)
            if chosen in (1, 2):
                self.showing, self.exited = None, True
            elif chosen == 3:
                self.showing = None
            return
        held = self.typed.get(handle)
        super().send(handle, text, enter=enter, timeout=timeout)
        if not (enter and not text and held == "/exit"):
            return
        if self.plain == "shell":
            self.exited = True
        elif self.plain == "hang":
            self.hung = True
        elif self.dialog:
            self.showing = handle


class RehomeBase(unittest.TestCase):
    """One temp world per arm: HELM_HOME, the claude/codex homes roots, the
    default homes, Orca's store, the cred backup root and the cache."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="helm-test-seat-rehome-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        j = lambda *p: os.path.join(self.tmp, *p)          # noqa: E731
        self.orca = j("orca")
        self.roots = {"claude": j("claude-homes"), "codex": j("codex-homes")}
        self.defaults = {"claude": j("default-claude"),
                         "codex": j("default-codex")}
        for path in list(self.roots.values()) + list(self.defaults.values()):
            os.makedirs(path)
        os.makedirs(j("helm-home"))
        # A REAL /proc under helm's own standing indirection, so the shipped
        # readers (`beacons.proc_env`, `beacons.proc_argv`) do the reading and
        # no arm has to substitute them.
        self.proc = j("proc")
        os.makedirs(self.proc)
        for patch in (
                mock.patch.dict(os.environ, {
                    "HELM_HOME": j("helm-home"),
                    "ORCA_USER_DATA_PATH": self.orca,
                    "HELM_CRED_BACKUP_ROOT": j("cred-backups"),
                    "HELM_PROC": self.proc,
                    # PINNED, not inherited: the launch-time sync switch is a
                    # gate this verb reads, so an arm must never inherit the
                    # developer's own setting for it.
                    "HELM_CREDHOME_ORCA_SYNC": "",
                    "HELM_CACHE_DIR": j("cache")}),
                mock.patch.dict(homes.ROOTS, self.roots),
                mock.patch.dict(homes.DEFAULTS, self.defaults),
                mock.patch.object(cred, "holders_of",
                                  lambda p, default=False: [])):
            patch.start()
            self.addCleanup(patch.stop)
        cred.cache_clear()
        self.addCleanup(cred.cache_clear)
        self.ad = _PaneAd()

    # -- fixture planting ---------------------------------------------------

    def plant_home(self, email=EMAIL, creds=None):
        """A real credhome named by the FOLDED email, so its identity AGREES
        with its directory name (a DRIFTED home is a different refusal)."""
        name = homes.canonical_name(email)
        d = os.path.join(self.roots["claude"], name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, ".claude.json"), "w") as f:
            json.dump({"numStartups": 1,
                       "oauthAccount": {"emailAddress": email,
                                        "accountUuid": "acct-1"}}, f)
        with open(os.path.join(d, ".credentials.json"), "w") as f:
            json.dump(creds if creds is not None else _creds("FAKE-HOME", 8), f)
        os.chmod(os.path.join(d, ".credentials.json"), 0o600)
        cred.cache_clear()
        return name, d

    def plant_orca(self, email=EMAIL, creds=None):
        auth = os.path.join(self.orca, "claude-accounts", UUID, "auth")
        os.makedirs(auth, exist_ok=True)
        with open(os.path.join(auth, "oauth-account.json"), "w") as f:
            json.dump({"accountUuid": "acct-1", "emailAddress": email,
                       "organizationUuid": "org-1"}, f)
        with open(os.path.join(auth, ".orca-managed-claude-auth"), "w") as f:
            f.write(UUID)
        with open(os.path.join(auth, ".credentials.json"), "w") as f:
            json.dump(creds if creds is not None else _creds("FAKE-ORCA", 8), f)
        os.chmod(os.path.join(auth, ".credentials.json"), 0o600)
        cred.cache_clear()
        return auth

    def plant_roster(self, session=SID, seat=SEAT):
        os.makedirs(chat.chat_dir(), exist_ok=True)
        row = {"room": "main"}
        if session:
            row["session"] = session
        with open(seats.roster_path(), "w") as f:
            json.dump({seat: row}, f)

    def plant_default_home(self, creds=None):
        with open(os.path.join(self.defaults["claude"], ".claude.json"),
                  "w") as f:
            json.dump({"numStartups": 1,
                       "oauthAccount": {"emailAddress": "default@example.test",
                                        "accountUuid": "acct-default"}}, f)
        if creds is not None:
            path = os.path.join(self.defaults["claude"], ".credentials.json")
            with open(path, "w") as f:
                json.dump(creds, f)
            os.chmod(path, 0o600)
        cred.cache_clear()
        return self.defaults["claude"]

    def live_home(self):
        """A credhome carrying Orca's exact live bytes: the accepted pole."""
        blob = _creds("FAKE-LIVE", 8)
        name, d = self.plant_home(creds=blob)
        self.plant_orca(creds=blob)
        self.home_dir = d
        return name

    def stale_home(self, email=EMAIL):
        """A credhome BEHIND Orca's copy on the SAME refresh family: STALE, and
        launchable only because a launch copies Orca's live token into it
        first. This is the `would-sync` pole, and the only pole on which the
        launch-time sync switch can decide anything."""
        name, d = self.plant_home(email=email, creds=_creds("FAKE-PAIR", -10))
        self.plant_orca(email=email, creds=_creds("FAKE-PAIR", 8))
        self.home_dir = d
        return name

    def plant_proc(self, pid, home_dir, model="opus", session=SID,
                   attempt=None, flag="--resume", perm=()):
        """A REAL /proc entry for a claude process under the fixture HELM_PROC.

        The environ and argv are what a `helm launch --home H -- --model M
        --resume SID` leaves behind, and they are the only evidence that says
        WHICH home and WHICH session a process that answers for the seat is
        actually on. `attempt` is the marker the process INHERITED from the
        line that started it — absent for any claude that came up by another
        route, which is exactly the process this fixture must be able to
        model."""
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        # `home_dir` None is a process on the DEFAULT home: the default is the
        # ABSENCE of the variable, never ~/.claude spelled into it (task/3515).
        env = {"HELM_CHAT_NAME": SEAT}
        if home_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = home_dir
        if attempt:
            env[seat_rehome.ATTEMPT_ENV] = attempt
        with open(os.path.join(d, "environ"), "wb") as f:
            f.write(b"".join(("%s=%s\0" % kv).encode() for kv in env.items()))
        argv = ["claude"] + (["--model", model] if model else [])
        argv += list(perm)
        argv += [flag, session] if session else []
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"".join((a + "\0").encode() for a in argv))
        return d

    def _seat_presence(self, transcript=True, **fields):
        """The vendor's presence record for the SEAT's own process, where
        `harness.presence_witness` reads it: <config>/sessions/<pid>.json
        under the CLAUDE_CONFIG_DIR its /proc environ names, bound to its
        birth stamp. `seat_rehome.plan` reads the same record for the
        session the seat's live process holds NOW (task/3208), and asks
        whether that session has a transcript in the same config root: a
        seat that has worked has one, so `transcript` plants it, and False
        models a session that has taken no message yet."""
        root = os.path.join(self.tmp, "seat-config")
        os.makedirs(os.path.join(root, "sessions"), mode=0o700, exist_ok=True)
        rec = {"pid": SEAT_PID, "procStart": SEAT_START, "sessionId": SID,
               "kind": "interactive", "entrypoint": "cli",
               "statusUpdatedAt": 1790383066237}
        rec.update(fields)
        path = os.path.join(root, "sessions", "%d.json" % SEAT_PID)
        with open(path, "w") as f:
            json.dump(rec, f)
        os.chmod(path, 0o600)
        if transcript:
            proj = os.path.join(root, "projects", "-p")
            os.makedirs(proj, exist_ok=True)
            with open(os.path.join(proj, rec["sessionId"] + ".jsonl"),
                      "w") as f:
                f.write('{"type":"user","message":{"role":"user",'
                        '"content":"x"}}\n')
        self.plant_proc(SEAT_PID, root)
        return root

    def plant_cwd(self, pid, name="seat-worktree"):
        """A REAL /proc/<pid>/cwd symlink, so the SHIPPED reader
        (`beacons.proc_cwd`) gives the plan a directory and the composed pane
        line carries the `cd` a reused pane needs. A seat with no readable cwd
        is the OTHER shape, and the composer's own branch for it."""
        d = os.path.join(self.tmp, name)
        os.makedirs(d, exist_ok=True)
        entry = os.path.join(self.proc, str(pid))
        os.makedirs(entry, exist_ok=True)
        link = os.path.join(entry, "cwd")
        if not os.path.islink(link):
            os.symlink(d, link)
        return d

    def census(self, rows=(), unreadable=()):
        """The /proc census as a LIVE list the arm can change mid-run.

        A launch STARTS a process and an exit removes one, so a census frozen
        at its first reading can only ever prove the surfaces the arm planted
        itself — which is what round 1 did, and why its apply arm could not
        say the launch produced anything."""
        self.procs, self.unreadable = list(rows), list(unreadable)
        return mock.patch.object(
            orcaadopt, "claude_processes",
            side_effect=lambda: (list(self.procs), list(self.unreadable)))


class HomeGateTest(RehomeBase):
    """The gate the hand procedure did not have: the home is decided BEFORE the
    live pane is touched, so every refusal here costs a running seat nothing."""

    def test_a_credhome_carrying_orcas_live_bytes_is_accepted(self):
        """THE POSITIVE, and the control for every refusal below: the same
        producer, over a real home that is simply fine, returns a note and NO
        refusal — so a check_home that refused everything could not pass this."""
        name = self.live_home()
        note, refusal = seat_rehome.check_home(name)
        self.assertIsNone(refusal, "a live credhome was refused: %s"
                          % (refusal and refusal.line()))
        self.assertIn(cred.FRESH, note)
        self.assertIn(EMAIL, note)

    def test_a_refresh_expired_home_refuses_naming_the_check_and_the_remedy(self):
        """THE OWNER'S MEASURED DEFECT. A home on its OWN login chain whose
        refreshTokenExpiresAt has PASSED, with no Orca copy of that account to
        sync from, is a dead token wearing a live identity — `helm launch`
        starts on it and the pane prints Not logged in after the old session is
        already gone.

        ITS CONTROL IS THE SAME HOME. The second half rewrites only the refresh
        lifetime and the identical call is ACCEPTED, so this arm cannot pass
        because of the account, the directory, the missing Orca copy, or any
        other property of the fixture — only because the chain is spent.
        """
        spent = "spent@example.test"
        name, d = self.plant_home(email=spent,
                                  creds=_creds("FAKE-SPENT", -4,
                                               refresh_hours=-1))
        note, refusal = seat_rehome.check_home(name)
        self.assertIsNone(note)
        self.assertIsNotNone(refusal, "a spent refresh chain was accepted")
        self.assertEqual(refusal.check, "home refresh chain is live")
        self.assertIn("EXPIRED", refusal.reason)
        self.assertIn("log in fresh in it", refusal.remedy)
        self.assertIn("Orca-synced default home", refusal.remedy)

        with open(os.path.join(d, ".credentials.json"), "w") as f:
            json.dump(_creds("FAKE-SPENT", -4, refresh_hours=24 * 5), f)
        cred.cache_clear()
        note, refusal = seat_rehome.check_home(name)
        self.assertIsNone(refusal, "the CONTROL home was refused too, so the "
                                   "arm above proves nothing about expiry: %s"
                          % (refusal and refusal.line()))
        self.assertIn("own login chain", note)

    def test_a_would_sync_home_refuses_while_the_launch_time_sync_is_off(self):
        """THE PREFLIGHT MUST AGREE WITH THE LAUNCH IT IS A PREFLIGHT FOR. A
        STALE home is admitted only because `helm launch` copies Orca's live
        token into it before exec — and `cred.launch_sync` performs no sync at
        all when HELM_CREDHOME_ORCA_SYNC is switched off, and starts claude on
        the home's own stale token instead. Admitting the home in that world
        walks the verb into its own defect the long way round.

        ITS CONTROL IS THE SAME HOME AND THE SAME CALL, one line down, with
        only the switch changed — so this arm cannot pass because of the
        fixture's account, its staleness, or its Orca copy, only because of
        the switch. The control also proves the fixture really is a
        `would-sync` home: if it were not, the control would refuse too and
        the refusal above would be about something else.
        """
        name = self.stale_home()
        with mock.patch.dict(os.environ, {cred.SYNC_ENV: "0"}):
            note, refusal = seat_rehome.check_home(name)
        self.assertIsNone(note)
        self.assertIsNotNone(refusal, "a would-sync home was admitted while "
                                      "the launch-time sync was switched off")
        self.assertEqual(refusal.check,
                         "the launch will perform the sync this home needs")
        self.assertIn(cred.SYNC_ENV, refusal.reason)
        self.assertIn("stale token", refusal.reason)
        self.assertIn(cred.SYNC_ENV, refusal.remedy)

        with mock.patch.dict(os.environ, {cred.SYNC_ENV: "1"}):
            note, refusal = seat_rehome.check_home(name)
        self.assertIsNone(refusal, "the CONTROL was refused too, so the arm "
                                   "above proves nothing about the switch: %s"
                          % (refusal and refusal.line()))
        self.assertIn("syncs it from Orca", note)

    def test_a_home_with_no_refresh_lifetime_refuses_naming_the_remedy(self):
        """AN UNPROVEN CHAIN IS NOT A PROOF. A named credhome with no Orca copy
        runs on its OWN login chain, and a token carrying no
        refreshTokenExpiresAt is a chain nothing can place — cred's own measure
        calls a missing lifetime UNPROVEN rather than live
        (`test_a_missing_refresh_lifetime_proves_no_chain`, tests/
        test_cred_orca.py). Unproven is not dead; it is also not the proof this
        verb owes, because the LIVE session is exited before the target is ever
        used.

        ITS CONTROL IS THE SAME HOME with only `refreshTokenExpiresAt` put
        back, so nothing about the account, the directory or the absent Orca
        copy can be what this arm measures.
        """
        blob = _creds("FAKE-OWN", 8)
        del blob["claudeAiOauth"]["refreshTokenExpiresAt"]
        own = "own@example.test"
        name, d = self.plant_home(email=own, creds=blob)
        note, refusal = seat_rehome.check_home(name)
        self.assertIsNone(note)
        self.assertIsNotNone(refusal, "an unproven refresh chain was accepted")
        self.assertEqual(refusal.check, "home refresh chain is live")
        self.assertIn("refreshTokenExpiresAt", refusal.reason)
        self.assertIn("UNPROVEN", refusal.reason)
        self.assertIn("log in fresh in it", refusal.remedy)
        self.assertIn("Orca-synced default home", refusal.remedy)

        with open(os.path.join(d, ".credentials.json"), "w") as f:
            json.dump(_creds("FAKE-OWN", 8), f)
        cred.cache_clear()
        note, refusal = seat_rehome.check_home(name)
        self.assertIsNone(refusal, "the CONTROL home was refused too, so the "
                                   "arm above proves nothing about the missing "
                                   "lifetime: %s"
                          % (refusal and refusal.line()))
        self.assertIn("own login chain", note)

    def test_a_home_that_is_not_a_named_credhome_refuses(self):
        """The default home is Orca's own to rewrite on an account switch, so a
        seat pinned there follows the whole fleet — the sledgehammer, not the
        scalpel. Its CONTROL is the accepted arm above, which resolves a home
        under the very same roots through the very same resolver."""
        path = self.plant_default_home()
        note, refusal = seat_rehome.check_home(path)
        self.assertIsNone(note)
        self.assertEqual(refusal.check, "home is a credhome")
        self.assertIn("follows the whole fleet", refusal.reason)


class PlanTest(RehomeBase):
    """The dry run IS the plan: it resolves, it prints, and it writes nothing."""

    def test_the_dry_run_prints_the_launch_line_and_writes_nothing(self):
        """THE POSITIVE. A whole resolution over real fixtures — roster session,
        census-bound pane, live home — rendered as the plan, with the exact
        `helm launch` line an apply would type. The ledger and the adapter are
        the controls on "writes nothing": both are inspected AFTER the call."""
        name = self.live_home()
        self.plant_roster()
        with self.census([_proc()]):
            rc = self._run(["rehome", SEAT, "--home", name, "--model", "opus"])
        self.assertEqual(rc, 0, self.err)
        self.assertIn("DRY RUN", self.out)
        self.assertIn(SID, self.out)
        self.assertIn(HANDLE, self.out)
        # the pane's footer proves the live mode (task/3515): bypass here
        self.assertIn("helm launch --seat %s --home %s -- --model opus "
                      "--permission-mode bypassPermissions --resume %s"
                      % (SEAT, name, SID), self.out)
        self.assertEqual(self.ad.sent, [], "a dry run typed into a pane")
        self.assertFalse(os.path.exists(seat_rehome.ledger_path()),
                         "a dry run wrote a ledger row")
        # THE POSITIVE CONTROL ON THE SAME TWO OBSERVABLES. An absence proves
        # nothing about restraint when the observable could not have become
        # non-empty anyway — a mis-resolved ledger path and a recording double
        # nothing reaches look exactly like a well-behaved dry run. Both are
        # made non-empty HERE, unconditionally, after the assertions above.
        self.assertEqual(seat_rehome.record({"seat": SEAT, "home": name}),
                         (True, None))
        self.assertTrue(os.path.exists(seat_rehome.ledger_path()),
                        "the ledger path this arm proved empty cannot be "
                        "written at all, so the emptiness said nothing")
        self.ad.send(HANDLE, "a control line", enter=True)
        self.assertEqual(len(self.ad.sent), 1,
                         "the adapter this arm proved silent does not record "
                         "sends, so the silence said nothing")

    def test_it_refuses_when_the_pane_cannot_be_proven(self):
        """A census whose row carries NO pane key is a live seat with no
        address — `resolve` publishes an EMPTY candidate set, the addressing
        primitive authorizes nothing, and helm refuses rather than typing into
        whatever pane answers.

        MEASURED, NOT ASSUMED: the refusal that fires is
        `authorized_handle`'s empty-authorization one, because a row without a
        pane key never reaches `pane_pids` at all — the no-ORCA_PANE_KEY branch
        one rung lower is unreachable from this caller.

        ITS CONTROL is the identical call one line down, where the ONLY change
        is that the same process carries its ORCA_PANE_KEY; that one plans.
        """
        name = self.live_home()
        self.plant_roster()
        with self.census([_proc(pane_key=None)]):
            p, refusal = seat_rehome.plan(SEAT, name, adapter=self.ad)
        self.assertIsNone(p)
        self.assertEqual(refusal.check, "pane proof")
        self.assertIn("no process to bind a pane to", refusal.reason)

        with self.census([_proc()]):
            p, refusal = seat_rehome.plan(SEAT, name, adapter=self.ad)
        self.assertIsNone(refusal, "the CONTROL census was refused too: %s"
                          % (refusal and refusal.line()))
        self.assertEqual(p["handle"], HANDLE)

    def test_it_refuses_when_the_roster_records_no_current_session(self):
        """Resuming with no session id would mint a NEW conversation on the new
        home and silently abandon the old one. The control is the same roster
        with the session restored."""
        name = self.live_home()
        self.plant_roster(session=None)
        with self.census([_proc()]):
            p, refusal = seat_rehome.plan(SEAT, name, adapter=self.ad)
        self.assertIsNone(p)
        self.assertEqual(refusal.check, "seat session")

        self.plant_roster()
        with self.census([_proc()]):
            p, refusal = seat_rehome.plan(SEAT, name, adapter=self.ad)
        self.assertIsNone(refusal, "the CONTROL roster was refused too: %s"
                          % (refusal and refusal.line()))
        self.assertEqual(p["session"], SID)

    # -- task/3208: the session the pane's LIVE process holds ---------------
    # The roster's CURRENT session is whatever the last SessionStart joined.
    # The pane's process names the session it holds NOW in its own presence
    # record (<config>/sessions/<pid>.json), and MEASURED on claude 2.1.283
    # that record names a /clear's new session the instant it lands.
    NEW = "66666666-7777-4888-8999-aaaaaaaaaaaa"

    def test_the_plan_resumes_the_session_the_panes_process_holds(self):
        """A /clear the roster never saw leaves the roster naming the
        pre-clear session while the pane runs a new one; resuming the
        roster's would bring the old conversation back on the new home.
        THE CONTROL is the same plan before the presence record exists: with
        nothing to read, the roster decides, exactly as before."""
        name = self.live_home()
        self.plant_roster()                       # the roster names SID
        with self.census([_proc()]):
            p, refusal = seat_rehome.plan(SEAT, name, adapter=self.ad)
        self.assertIsNone(refusal, refusal and refusal.line())
        self.assertEqual(p["session"], SID)
        self._seat_presence(sessionId=self.NEW)
        with self.census([_proc()]):
            p, refusal = seat_rehome.plan(SEAT, name, adapter=self.ad)
        self.assertIsNone(refusal, refusal and refusal.line())
        self.assertEqual(p["session"], self.NEW)
        self.assertIn("--resume %s" % self.NEW, p["command"])

    def test_a_helper_claude_in_the_pane_never_names_the_session(self):
        """The same plan, but the pane process's record is a `claude -p`
        helper's (entrypoint sdk-cli, MEASURED on 2.1.283) — a child of the
        seat inherits its pane key and name, so it is in the candidate set —
        and names a session that is not the roster's. It does not speak for
        the seat, so the roster decides, as before task/3208. The control is
        the record above, identical but for entrypoint "cli", which does."""
        name = self.live_home()
        self.plant_roster()                       # the roster names SID
        self._seat_presence(sessionId=self.NEW, entrypoint="sdk-cli")
        with self.census([_proc()]):
            p, refusal = seat_rehome.plan(SEAT, name, adapter=self.ad)
        self.assertIsNone(refusal, refusal and refusal.line())
        self.assertEqual(p["session"], SID)
        self.assertIn("--resume %s" % SID, p["command"])
        self.assertIn("not the pane's interactive claude", p["session_note"])

    def test_a_session_with_no_transcript_starts_fresh_under_its_own_id(self):
        """claude writes a session's transcript at its first message, and
        refuses `--resume` for one with none (MEASURED: "No conversation
        found", exit 1) — AFTER this verb has already exited the seat. It
        accepts `--session-id` for exactly that id (MEASURED), so the plan
        launches with it and says so."""
        name = self.live_home()
        self.plant_roster(session=self.NEW)
        self._seat_presence(transcript=False, sessionId=self.NEW)
        with self.census([_proc()]):
            p, refusal = seat_rehome.plan(SEAT, name, adapter=self.ad)
        self.assertIsNone(refusal, refusal and refusal.line())
        self.assertIn("--session-id %s" % self.NEW, p["command"])
        self.assertNotIn("--resume", p["command"])

    def test_the_verification_reads_the_flag_the_plan_launched_with(self):
        """A fresh launch's process carries `--session-id`, never
        `--resume`; a verification that only read `--resume` would call the
        process this apply started a stranger and fail a rehome that
        worked."""
        name = self.live_home()
        self.plant_roster(session=self.NEW)
        self._seat_presence(transcript=False, sessionId=self.NEW)
        with self.census([_proc()]):
            p, refusal = seat_rehome.plan(SEAT, name, adapter=self.ad)
        self.assertIsNone(refusal, refusal and refusal.line())
        self.assertEqual(p["flag"], "--session-id")
        p = dict(p, attempt=FOREIGN_MARK)
        self.plant_proc(NEW_PID, self.home_dir, model=None, session=self.NEW,
                        attempt=FOREIGN_MARK, flag="--session-id",
                        perm=p["permission"])
        ident, why = seat_rehome._planned_process(
            p, {"pane_pids": [orcaadopt.ProcIdent(NEW_PID, NEW_START)]})
        self.assertEqual(ident, NEW_PID)
        self.assertIsNone(why, why)

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        from helm import seat
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(harness, "detect", return_value=self.ad):
            rc = seat.cmd_seat(argv)
        self.out, self.err = out.getvalue(), err.getvalue()
        return rc


class ApplyTest(RehomeBase):
    """--apply drives the REAL doors. The spies wrap the shipped functions and
    record the arguments the production call made — never a reconstruction.

    AND NOTHING THE VERIFICATION READS IS PLANTED IN ADVANCE. The new claude
    process, the roster row it rejoins and its inbox beacon all come into
    existence inside the adapter's `send`, at the moment the launch line is
    typed — so a run that never typed it, or typed it and got nothing back,
    has no surfaces to be believed by. Round 1 planted all three before the
    launch and could therefore only demonstrate that pre-existing observations
    satisfy a post-launch check, which is the opposite of what it claimed.
    """

    def _plant_neighbour(self, mark, model="opus"):
        """A DIFFERENT CLAUDE IN THE SEAT'S PANE — the counterexample time
        ordering cannot rule out. It runs on the planned home, resumes the
        planned session, carries the planned model and permission mode, is
        born AFTER the process the exit removed, and arms an inbox beacon for
        the seat since the act. The only thing it does not carry is THIS
        apply's marker: `mark` None is a process started by any other route,
        and a foreign value is another attempt's process.
        """
        self.plant_proc(NEIGHBOUR_PID, self.home_dir, model=model,
                        session=SID, attempt=mark,
                        perm=["--permission-mode", "bypassPermissions"])
        self.procs.append(_proc(pid=NEIGHBOUR_PID, start=NEIGHBOUR_START))
        beacons.register(SEAT, session=SID, pid=BEACON_PID, proc_dir=None)

    def _apply(self, name=None, model="opus", launch=None, stale_beacon=False,
               remap=False, break_the_ledger=False, verify_s=None, env=None,
               marker=None, neighbour=None, mode=None):
        """Plan, then apply, over the shipped doors. -> (rc, refusal).

        `launch` is WHAT THE LAUNCH LINE STARTS: None for exactly what the plan
        asked for, a dict overriding any of home/model/session/pid/start for a
        launch that came up somewhere else, and False for a line that was typed
        and produced nothing.

        `marker` is what the started process INHERITS: None means it inherits
        the assignment read out of the line that was actually typed, which is
        the causal reading and the only default a child can honestly have.

        `neighbour` is ("exit"|"launch", mark) and plants the look-alike
        described above at that moment — after the exit and before the launch
        line, or after it.
        """
        name = self.live_home() if name is None else name
        self.plant_roster()
        # THE SEAT HAS A WORKING DIRECTORY, as every real seat does, so the
        # composed line carries the `cd` and the assignment is asserted in the
        # place production puts it.
        self.cwd = self.plant_cwd(SEAT_PID)
        if stale_beacon:
            # THE SEAT'S PREVIOUS INCARNATION: armed BEFORE this verb ran, and
            # therefore no evidence about this rehome at all.
            beacons.register(SEAT, session=SID, pid=BEACON_PID, proc_dir=None)
        started = {"home": self.home_dir, "model": model, "session": SID,
                   "pid": NEW_PID, "start": NEW_START}
        if isinstance(launch, dict):
            started.update(launch)

        def on_launch(_handle, text):
            if neighbour and neighbour[0] == "launch":
                self._plant_neighbour(neighbour[1], model=model)
            if launch is False:
                return
            # THE CHILD INHERITS WHAT THE LINE ASSIGNED. Read out of the text
            # the adapter was handed, never reconstructed from the plan: a
            # fixture that mints the marker itself would pass over a producer
            # that never put one on the line at all.
            mark = _marker_in(text) if marker is None else marker
            perm = (_perm_in(text) if started.get("perm") is None
                    else started["perm"])
            self.plant_proc(started["pid"], started["home"],
                            model=started["model"], session=started["session"],
                            attempt=mark or None, perm=perm)
            self.procs.append(_proc(pid=started["pid"], start=started["start"]))
            self.plant_roster(session=started["session"])
            beacons.register(SEAT, session=started["session"], pid=BEACON_PID,
                             proc_dir=None)

        self.ad.on_launch = on_launch
        if break_the_ledger:
            # THE REAL FAILURE DOOR, not a patched writer: a DIRECTORY where the
            # ledger file belongs. `eventledger.append` opens that path O_RDWR
            # and the kernel refuses, so the shipped append genuinely fails and
            # the shipped reason is what the report carries.
            os.makedirs(seat_rehome.ledger_path(), exist_ok=True)
        exited = {"yes": False}
        real_send = orcaadopt.send_to_pane
        self.calls = []

        def spy(seat, text, expect_pids=None, adapter=None, **kw):
            # SPY THE REAL CALL: record what production passed, then let the
            # shipped door run it. Reconstructing the arguments here would only
            # prove this test can build them.
            self.calls.append((seat, text,
                               [(int(x), getattr(x, "start", None))
                                for x in (expect_pids or ())]))
            out = real_send(seat, text, expect_pids=expect_pids,
                            adapter=adapter, **kw)
            if out[0] == "resumed" or getattr(self.ad, "exited", None):
                # A pane that owns its process says so itself: an exit whose
                # wake read UNVERIFIED still removed the /proc row.
                exited["yes"] = True
                # THE CENSUS FOLLOWS THE WORLD: an exited process stops being a
                # /proc row, so a verification that still finds it is finding
                # something the arm put there.
                self.procs[:] = [r for r in self.procs if r["pid"] != SEAT_PID]
                if neighbour and neighbour[0] == "exit":
                    self._plant_neighbour(neighbour[1], model=model)
            return out

        def alive(pid, starttime=None, proc_dir=None):
            """The seat's process is gone once /exit lands; the beacon's is
            not. A blanket answer would make the beacon arm vacuous. A pane
            that models its own process (`exited` not None — the exit-confirm
            dialog double) is the truth instead: there the process ends at the
            keystroke that answers the dialog, never at the wake's verdict."""
            if int(pid) == SEAT_PID:
                own = getattr(self.ad, "exited", None)
                return not (exited["yes"] if own is None else own)
            return True

        real_auth = orcaadopt.authorized_handle
        self.bindings = []

        def auth_spy(expect_pids, adapter):
            got = real_auth(expect_pids, adapter)
            self.bindings.append(got[0])
            if remap and len(self.bindings) == 1:
                # THE WORLD MOVES BETWEEN THE TWO RESOLUTIONS — the apply's own
                # binding is taken, and the pane key is remapped before
                # `send_to_pane` takes its send-time one. This is the only
                # place the drift the guard refuses can appear.
                self.ad.remap[PANE_KEY] = OTHER_HANDLE
            return got

        with self.census([_proc()]), \
                mock.patch.object(orcaadopt, "send_to_pane", spy), \
                mock.patch.object(beacons, "pid_alive", alive):
            p, refusal = seat_rehome.plan(SEAT, name, model=model,
                                          adapter=self.ad, mode=mode)
            self.assertIsNone(refusal, refusal and refusal.line())
            buf = io.StringIO()
            with contextlib.ExitStack() as es:
                es.enter_context(mock.patch.object(
                    orcaadopt, "authorized_handle", auth_spy))
                if verify_s is not None:
                    es.enter_context(mock.patch.object(
                        seat_rehome, "VERIFY_WAIT_S", verify_s))
                if env:
                    es.enter_context(mock.patch.dict(os.environ, env))
                rc, refusal = seat_rehome.apply_rehome(p, out=buf,
                                                       sleep=lambda s: None)
        self.name, self.text, self.p = name, buf.getvalue(), p
        return rc, refusal

    def _launched(self):
        """Every send carrying the launch. NOT `startswith`: the composed line
        begins with the pane's `cd` and carries the attempt assignment in front
        of the command, and a filter anchored at the start of the line silently
        matched nothing once both were there."""
        return [t for t in self.ad.sent if "helm launch" in t[1]]

    def test_apply_exits_through_the_wake_door_then_types_the_launch_line(self):
        """THE POSITIVE, end to end: ONE pane-input wake carrying `/exit` and
        the AUTHORIZED pid identity, then the launch line — carrying the same
        session id and the model — typed into the same pane, and never a
        signal.

        AND INTO THE SAME PANE IS ASSERTED, not assumed: the handle the `/exit`
        was typed into and the handle the launch line went to are compared,
        and both are the ONE binding this apply took. The remap arm below is
        this arm's negative twin on exactly that comparison.
        """
        rc, refusal = self._apply()
        self.assertEqual(rc, 0, refusal and refusal.line())
        self.assertEqual(len(self.calls), 1, "more than ONE pane-input wake")
        seat_arg, text, pids = self.calls[0]
        self.assertEqual((seat_arg, text), (SEAT, "/exit"))
        self.assertEqual(pids, [(SEAT_PID, SEAT_START)],
                         "the exit was not bound to the authorized process")
        exits = [t for t in self.ad.sent if t[1] == "/exit"]
        self.assertEqual(len(exits), 1, "the /exit was not typed once: %r"
                         % (self.ad.sent,))
        launch = self._launched()
        self.assertEqual(len(launch), 1, "the launch line was not typed once: "
                                         "%r" % (self.ad.sent,))
        handle, line, enter = launch[0]
        self.assertEqual(exits[0][0], handle,
                         "the exit and the relaunch went to DIFFERENT panes")
        self.assertEqual(handle, HANDLE)
        self.assertTrue(enter, "the launch line was typed without Enter")
        self.assertIn("--resume " + SID, line)
        self.assertIn("--model opus", line)
        self.assertIn("--home " + self.name, line)
        self.assertIn("PROVEN gone", self.text)
        for surface in ("pane", "register", "beacon"):
            self.assertIn(surface, self.text)
        self.assertNotIn("UNPROVEN", self.text)

    def test_the_surfaces_exist_only_because_the_launch_line_was_typed(self):
        """THE CAUSAL CONTROL on the arm above, and the one round 1 could not
        write. Same world, same exit, same keystroke — and the launch line
        starts NOTHING. The pane surface and the beacon surface, which the arm
        above reads PROVEN, both read UNPROVEN here, so what that arm measured
        was produced by the launch and not by the fixture.

        AND THE BEACON HERE IS NOT ABSENT, IT IS OLD: a live inbox beacon for
        this seat on this session is standing the whole time, armed before the
        act. A verification that asked only whether a beacon exists would read
        this run PROVEN.
        """
        rc, refusal = self._apply(launch=False, stale_beacon=True,
                                  verify_s=0.05)
        self.assertEqual(rc, 1)
        self.assertEqual(refusal.check, "post-launch verification")
        self.assertEqual(len(self._launched()), 1,
                         "the launch line was not typed, so this arm proves "
                         "nothing about what typing it produces")
        self.assertIn("pane", refusal.reason)
        self.assertIn("beacon", refusal.reason)
        self.assertIn("no pane answers for the seat", self.text)
        # The beacon EXISTS and is live — and it is the seat's previous
        # incarnation, armed before this verb ran, so it says nothing about
        # this rehome and the surface says exactly that.
        self.assertIn("armed before the exit", self.text)

    def test_the_apply_re_asks_the_home_gate_before_it_touches_the_pane(self):
        """PREFLIGHT AND ACT ARE ONE PREDICATE. The plan admitted a would-sync
        home while the launch-time sync was on; the switch goes off between the
        plan and the act, and the apply — which re-runs `check_home`, not a
        second implementation of its rules — refuses.

        THE LIVE SEAT IS UNTOUCHED, and that is the assertion that matters: the
        send_to_pane spy recorded NO call and the adapter recorded NO
        keystroke. Its CONTROL is the positive arm above, where the same spy
        records exactly one wake — an empty spy proves restraint only when the
        spy demonstrably records.
        """
        rc, refusal = self._apply(name=self.stale_home(),
                                  env={cred.SYNC_ENV: "0"})
        self.assertEqual(rc, 1)
        self.assertEqual(refusal.check,
                         "the launch will perform the sync this home needs")
        self.assertIn("REFUSED now", refusal.reason)
        self.assertIn(cred.SYNC_ENV, refusal.reason)
        self.assertEqual(self.calls, [], "a refused apply woke the pane")
        self.assertEqual(self.ad.sent, [], "a refused apply typed into a pane")
        self.assertFalse(os.path.exists(seat_rehome.ledger_path()),
                         "a refused apply wrote a ledger row")
        # THE POSITIVE CONTROL ON THE SAME OBSERVABLES, unconditional and
        # taken AFTER the absence assertions above. A recording double nothing
        # ever reaches and a ledger path that cannot be written at all look
        # exactly like restraint, so each is made non-empty here and asserted.
        self.ad.send(HANDLE, "a control line", enter=True)
        self.assertEqual(len(self.ad.sent), 1,
                         "the adapter this arm proved silent does not record "
                         "sends, so the silence said nothing")
        self.assertEqual(seat_rehome.record({"seat": SEAT}), (True, None))
        self.assertTrue(os.path.exists(seat_rehome.ledger_path()),
                        "the ledger path this arm proved empty cannot be "
                        "written at all, so the emptiness said nothing")

    def test_a_pane_remap_between_the_two_bindings_refuses_before_the_exit(self):
        """ONE PANE ACROSS THE EXIT, THE PROOF AND THE RELAUNCH. `send_to_pane`
        takes its OWN binding at send time — the re-proof that may never be
        skipped — so this verb compares that binding with the one it took a
        moment earlier instead of resolving twice and hoping. A pane-key remap
        injected between the two would otherwise exit the process behind one
        handle and type the launch line into another.

        NOTHING IS TYPED, which is the point: the refusal lands before the
        actuation, so the seat is exactly as it was. Its CONTROL is the
        positive arm, identical but for the remap, where both bindings agree
        and the exit and the launch reach the same handle.
        """
        rc, refusal = self._apply(remap=True)
        self.assertEqual(rc, 1)
        self.assertEqual(refusal.check,
                         "one pane across the exit and the relaunch")
        self.assertEqual(len(self.bindings), 2,
                         "the two bindings this arm compares were not both "
                         "taken: %r" % (self.bindings,))
        self.assertNotEqual(self.bindings[0], self.bindings[1],
                            "the remap did not land, so nothing was compared")
        self.assertIn(OTHER_HANDLE, refusal.reason)
        self.assertIn(HANDLE, refusal.reason)
        self.assertEqual(self.ad.sent, [],
                         "a refused exit typed into a pane")
        self.assertFalse(os.path.exists(seat_rehome.ledger_path()),
                         "a refused exit wrote a ledger row")
        # THE POSITIVE CONTROL ON THE SAME OBSERVABLES, unconditional and
        # taken AFTER the absence assertions above. A recording double nothing
        # ever reaches and a ledger path that cannot be written at all look
        # exactly like restraint, so each is made non-empty here and asserted.
        self.ad.send(HANDLE, "a control line", enter=True)
        self.assertEqual(len(self.ad.sent), 1,
                         "the adapter this arm proved silent does not record "
                         "sends, so the silence said nothing")
        self.assertEqual(seat_rehome.record({"seat": SEAT}), (True, None))
        self.assertTrue(os.path.exists(seat_rehome.ledger_path()),
                        "the ledger path this arm proved empty cannot be "
                        "written at all, so the emptiness said nothing")

    def test_verification_refuses_the_process_the_exit_removed(self):
        """A SURFACE THAT EXISTS IS NOT THE REHOME THAT WAS ASKED FOR. Here the
        launch line is typed and what answers for the seat afterwards is the
        OLD process identity — the pane answers, the roster names the seat on
        the planned session and a beacon is armed, and round 1 would have read
        all three PROVEN and returned 0. The pane surface must read UNPROVEN
        naming the mismatch, because a recycled or surviving predecessor is
        exactly what a rehome has to be able to tell apart from its own new
        session.

        ITS CONTROL is the positive arm, whose ONLY difference is the identity
        the launch brings up.
        """
        rc, refusal = self._apply(launch={"pid": SEAT_PID, "start": SEAT_START},
                                  verify_s=0.05)
        self.assertEqual(rc, 1)
        self.assertEqual(refusal.check, "post-launch verification")
        self.assertIn("pane", refusal.reason)
        self.assertIn("SAME process the exit removed", self.text)
        self.assertIn("register  PROVEN", self.text,
                      "the OTHER surfaces did not come back, so this arm is "
                      "not measuring the pane's correlation: %r" % self.text)
        self.assertIn("beacon    PROVEN", self.text)

    def test_verification_refuses_a_new_process_on_another_home(self):
        """THE SAME QUESTION ABOUT THE OTHER HALF OF THE PLAN. A brand new
        process, younger than the one the exit removed, answering for the seat
        on the planned session — and running on a DIFFERENT credhome, which is
        precisely the outcome this verb exists to prevent and the one a
        seat-named surface cannot see. The home is read from the process's own
        CLAUDE_CONFIG_DIR through the shipped reader.

        ITS CONTROL is the positive arm, whose launch comes up on the planned
        home and reads PROVEN on the same surface.
        """
        _other, other_dir = self.plant_home(
            email="elsewhere@example.test", creds=_creds("FAKE-ELSE", 8))
        rc, refusal = self._apply(launch={"home": other_dir}, verify_s=0.05)
        self.assertEqual(rc, 1)
        self.assertEqual(refusal.check, "post-launch verification")
        self.assertIn("pane", refusal.reason)
        self.assertIn("and this rehome planned", self.text)
        self.assertIn("runs on home", self.text)

    def test_apply_records_one_ledger_row_carrying_what_was_proven(self):
        """The ledger is the durable half: one row per rehome, readable back
        through the CHECKED reader — an append that cannot be read back is the
        worst shape a ledger has. The control is the dry-run arm, which leaves
        no file at all; and this arm is in turn the control for the refused-
        append arm below, which uses the same writer over a path the kernel
        will not open."""
        rc, refusal = self._apply()
        self.assertEqual(rc, 0, refusal and refusal.line())
        rows, unavailable = seat_rehome.events()
        self.assertIsNone(unavailable)
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["event"], "seat-rehome")
        self.assertEqual(row["seat"], SEAT)
        self.assertEqual(row["home"], self.name)
        self.assertEqual(row["session"], SID)
        self.assertEqual(row["model"], "opus")
        self.assertEqual(row["process"], [NEW_PID, NEW_START],
                         "the row does not name the process it proved")
        self.assertTrue(row["verified"]["pane"])
        self.assertTrue(row["verified"]["register"])
        self.assertTrue(row["verified"]["beacon"])

    def test_a_rehome_whose_launch_never_started_writes_no_launch_row(self):  # noqa: VACUOUS_ASSERTION — no row is the contract: the row is the relaunched process's own SessionStart hook's, and the rc 1 is the positive control that the launch line produced nothing
        """The launch record holds only launches that really started: the
        relaunched claude's own SessionStart hook writes the row, so a
        launch line typed into the pane that produced nothing leaves none,
        and the capture of the process the rehome stopped still describes
        the session's last launch. MUTATION: write the row before typing
        the line — a relaunch that never started unbinds that capture."""
        from helm import eventledger, home
        rc, _refusal = self._apply(launch=False)
        self.assertEqual(rc, 1)
        rows, unread = eventledger.checked_events(
            os.path.join(home.global_dir(), "seat-launches.jsonl"))
        self.assertIsNone(unread)
        self.assertEqual(rows, [])

    def test_a_refused_ledger_row_exits_non_zero_with_the_partial_effect(self):
        """A MISSING DURABLE ROW IS NOT A SUCCESS. The rehome itself goes
        perfectly — every surface comes back — and the ledger append is refused
        by the real writer. The verb has to say what actually happened (the
        seat WAS moved, the row is missing, here is the line that ran and where
        the ledger lives) and exit non-zero, because a caller reading only the
        exit status must never be told a rehome nobody recorded went cleanly.

        AND NOTHING IS ROLLED BACK OR RETRIED: re-exiting a session that is
        coming up is a second actuation on a pane nobody has read. The spies
        prove it — exactly ONE wake and exactly ONE launch line, the same
        counts the positive arm asserts.

        ITS CONTROL is the arm above: the identical run with a writable ledger
        exits 0 and the row reads back.
        """
        rc, refusal = self._apply(break_the_ledger=True)
        self.assertEqual(rc, 1, "a rehome with no durable row returned success")
        self.assertEqual(refusal.check, "rehome ledger row")
        self.assertIn("the seat WAS moved", refusal.reason)
        self.assertIn("Nothing was rolled back", refusal.reason)
        self.assertIn(SID, refusal.reason)
        self.assertIn(seat_rehome.ledger_path(), refusal.remedy)
        self.assertIn("helm launch --seat", refusal.remedy)
        self.assertEqual(len(self.calls), 1, "the rehome was retried")
        self.assertEqual(len(self._launched()), 1, "the rehome was retried")
        self.assertIn("PROVEN", self.text)
    def test_a_first_message_before_the_exit_turns_fresh_into_resume(self):
        """task/3208. The plan found the session with no transcript and
        planned `--session-id`; the session then took its first message
        before the exit, so a transcript exists by the time the launch line
        is typed, and `--session-id` on it would be refused ("already in
        use", MEASURED). The apply asks again once the exit is PROVEN — no
        process is left to write it — and types `--resume`, which the
        verification then reads."""
        root = self._seat_presence(transcript=False)
        real_wait = seat_rehome._wait_gone

        def wait_after_a_first_message(*a, **k):
            proj = os.path.join(root, "projects", "-p")
            os.makedirs(proj, exist_ok=True)
            with open(os.path.join(proj, SID + ".jsonl"), "w") as f:
                f.write('{"type":"user"}\n')
            return real_wait(*a, **k)

        with mock.patch.object(seat_rehome, "_wait_gone",
                               wait_after_a_first_message):
            rc, refusal = self._apply()
        self.assertEqual(self.p["flag"], "--session-id")   # the plan's
        self.assertEqual(rc, 0, refusal and refusal.line())
        _handle, line, _enter = self._launched()[0]
        self.assertIn("--resume %s" % SID, line)
        self.assertNotIn("--session-id", line)
        self.assertIn("resuming it instead", self.text)

    def test_the_launch_carries_a_fresh_marker_the_pane_proves_it_by(self):
        """THE POSITIVE, and the CONTROL for every neighbour arm below.

        One chain, each link asserted against the next: the apply mints a
        uuid4, the composed line ASSIGNS it in front of the launch, the process
        the line started carries it in its OWN environ (read back through the
        shipped `beacons.proc_env` over the fixture /proc), the pane surface
        reads PROVEN quoting it, and the ledger row records it. A producer that
        minted a marker and never put it on the line, or put it on the line and
        never asked the process for it, breaks a link here.
        """
        rc, refusal = self._apply()
        self.assertEqual(rc, 0, refusal and refusal.line())
        _handle, line, _enter = self._launched()[0]
        mark = _marker_in(line)
        self.assertIsNotNone(mark, "the launch line assigns no %s: %r"
                             % (seat_rehome.ATTEMPT_ENV, line))
        self.assertEqual(uuid.UUID(mark).version, 4,
                         "the attempt marker is not a uuid4: %r" % mark)
        env = beacons.proc_env(NEW_PID)
        self.assertEqual(env.get(seat_rehome.ATTEMPT_ENV), mark,
                         "the process the launch started does not carry the "
                         "marker the line assigned")
        self.assertIn("pane      PROVEN", self.text)
        self.assertIn("%s=%s" % (seat_rehome.ATTEMPT_ENV, mark), self.text)
        rows, unavailable = seat_rehome.events()
        self.assertTrue(rows, "the rehome wrote no readable ledger row")
        self.assertIsNone(unavailable)
        self.assertEqual(rows[0]["attempt"], mark,
                         "the ledger row does not carry the marker this "
                         "attempt launched with")
        # PER-ATTEMPT, NOT PER-VERB. A constant would satisfy every assertion
        # above and correlate nothing, because the neighbour arms below are
        # only refused by a value the NEXT rehome will not reuse. Each mint is
        # SIZED first, on itself: two empty strings differ from nothing, so an
        # inequality alone would read a producer that mints nothing as fresh.
        first, second = seat_rehome.new_attempt(), seat_rehome.new_attempt()
        self.assertTrue(first, "the mint produced nothing")
        self.assertTrue(second, "the mint produced nothing")
        self.assertNotEqual(first, second)

    def test_an_unmarked_look_alike_standing_before_the_launch_is_refused(self):
        """THE FINDING, EXACTLY. Another claude comes up in this pane AFTER the
        exit and BEFORE the launch line is typed: same home, same session, same
        model, born after the process the exit removed. Our launch line is then
        typed and starts nothing at all, and every ordering test this
        verification can write is true of the neighbour.

        THE OTHER TWO SURFACES COME BACK PROVEN, which is what makes this arm
        about attribution and not about a dead seat: the roster names the seat
        on the planned session and the neighbour arms a live inbox beacon since
        the act. Only the pane refuses, and it refuses naming the marker.

        ITS CONTROL is the positive arm above: the same fixture, where the
        launch DOES start the process and the same pane reads PROVEN.
        """
        rc, refusal = self._apply(launch=False, neighbour=("exit", None),
                                  verify_s=0.05)
        self.assertEqual(rc, 1)
        self.assertEqual(refusal.check, "post-launch verification")
        self.assertEqual(len(self._launched()), 1,
                         "the launch line was not typed, so this arm proves "
                         "nothing about what the verification credits")
        self.assertIn("pane", refusal.reason)
        self.assertNotIn("beacon", refusal.reason)
        self.assertIn("register  PROVEN", self.text)
        self.assertIn("beacon    PROVEN", self.text,
                      "the neighbour did not satisfy the other surfaces, so "
                      "this arm is not measuring the pane's attribution: %r"
                      % self.text)
        self.assertIn("pid %d matches the home, the session and the model but "
                      "carries no %s" % (NEIGHBOUR_PID,
                                         seat_rehome.ATTEMPT_ENV),
                      self.text)

    def test_a_look_alike_carrying_another_attempts_marker_is_refused(self):
        """THE SAME NEIGHBOUR, ARRIVING AFTER THE LAUNCH, and carrying a marker
        from somebody else's rehome. A verification that merely looked for the
        variable — rather than for THIS value — would credit it, and the whole
        point of a per-apply value is that the previous rehome's marker is not
        this one's.

        ITS CONTROL is the positive arm: same producer, same surfaces, and the
        only difference is which value the process carries.
        """
        rc, refusal = self._apply(launch=False,
                                  neighbour=("launch", FOREIGN_MARK),
                                  verify_s=0.05)
        self.assertEqual(rc, 1)
        self.assertEqual(refusal.check, "post-launch verification")
        _handle, line, _enter = self._launched()[0]
        mine = _marker_in(line)
        # THE CONTROL ON THE COMPARISON ITSELF: an apply that assigned no
        # marker at all also differs from the foreign one, and would read this
        # arm green while proving nothing.
        self.assertTrue(mine, "the launch line assigned no marker, so every "
                              "comparison in this arm is vacuous")
        self.assertNotEqual(mine, FOREIGN_MARK,
                            "the fixture handed the neighbour this apply's "
                            "own marker, so nothing foreign was measured")
        self.assertIn("pid %d carries %s=%s"
                      % (NEIGHBOUR_PID, seat_rehome.ATTEMPT_ENV, FOREIGN_MARK),
                      self.text)
        self.assertIn("another attempt's process", self.text)
        self.assertIn(mine, self.text)

    def test_the_ledger_and_the_report_carry_the_whole_line_that_was_typed(self):
        """P3. What is reported is the EXACT string the adapter was handed —
        the `cd`, the assignment and the launch — not the launch fragment the
        plan printed. An operator re-running a rehome by hand from the ledger
        has to get the line that ran, and a line missing its assignment starts
        a process the next verification cannot attribute.

        THE ASSERTION IS AGAINST THE SPY, not against a re-composition: the
        recorded send is the ground truth, and the row and the printed report
        are compared to it.
        """
        rc, refusal = self._apply()
        self.assertEqual(rc, 0, refusal and refusal.line())
        _handle, line, _enter = self._launched()[0]
        self.assertTrue(line.startswith("cd "),
                        "the composed line carries no cd, so this arm cannot "
                        "say where the assignment sits: %r" % line)
        self.assertIn(os.path.realpath(self.cwd), line)
        self.assertIn(" && %s=" % seat_rehome.ATTEMPT_ENV, line)
        self.assertIn("  launched  %s" % line, self.text)
        row = seat_rehome.events()[0][0]
        self.assertEqual(row["pane_line"], line,
                         "the ledger row is not the line that was typed")
        self.assertIn(row["command"], row["pane_line"])
        self.assertNotEqual(row["command"], row["pane_line"],
                            "the row reports the launch fragment alone")

    # -- task/3201: the exit-confirm dialog ---------------------------------

    def _apply_through(self, ad, exit_wait=None, **kw):
        """`_apply` over a pane double that owns its process, with the turn
        verb's settle and read-back interval at zero so the arm measures the
        decision and not the clock. `exit_wait` shortens the wait for the old
        pid, for an arm whose pid is meant to outlive it."""
        self.ad = ad
        with contextlib.ExitStack() as es:
            es.enter_context(mock.patch.object(
                harness, "SUBMIT_VERIFY_INTERVAL_S", 0))
            if exit_wait is not None:
                es.enter_context(mock.patch.object(
                    seat_rehome, "EXIT_WAIT_S", exit_wait))
            return self._apply(env={"HELM_SUBMIT_SETTLE_S": "0"}, **kw)

    def _on(self, where):
        """[(text, enter)] for every keystroke that landed on `where`."""
        return [(t, e) for _h, t, e, on in self.ad.keys if on == where]

    def test_an_exit_dialog_on_option_1_is_confirmed_and_the_rehome_proceeds(self):
        """THE MEASURED DEFECT, as an arm (task/3201): the
        seat holds background tasks, so its /exit opens Claude Code's
        exit-confirm dialog with the pointer on "1. Exit and stop tasks". The verb read "no
        readable composer" three times and refused a pane its own /exit had
        already moved. Now it recognises the dialog, confirms option 1 through
        the dialog door with ONE key (the option's digit, task/3209), and runs
        the rest of its plan unchanged: the process is
        PROVEN gone, the launch line is typed into the same pane, and all
        three surfaces come back.

        CAUSAL: the double ends the process only at the keystroke that answers
        the dialog, so "PROVEN gone" below is evidence the key was pressed."""
        rc, refusal = self._apply_through(_ExitDialogAd(focus=1))
        self.assertEqual(rc, 0, refusal and refusal.line())
        self.assertEqual(self._on("dialog"), [("1", False)],
                         "the dialog was not answered by exactly ONE key, "
                         "option 1's digit with no Enter: %r" % (self.ad.keys,))
        self.assertEqual([k[1:3] for k in self.ad.keys
                          if k[3] == "composer"], [("/exit", False),
                                                   ("", True)],
                         "the /exit was not typed and submitted once")
        launch = self._on("shell")
        self.assertEqual(len(launch), 1, "the launch line was not typed once "
                         "into the pane the dialog left: %r" % (self.ad.keys,))
        self.assertIn("helm launch", launch[0][0])
        self.assertTrue(self.ad.exited)
        self.assertIn("exit-confirm dialog", self.text)
        self.assertIn("PROVEN gone", self.text)
        self.assertNotIn("UNPROVEN", self.text)
        # The pane showed this exit (the dialog closed), so the pane proves it.
        self.assertEqual(seat_rehome.events()[0][0]["exit_proven_by"], "pane")

    def test_the_dialog_is_confirmed_when_the_presence_record_agrees(self):
        """THE DOOR'S SECOND WITNESS, end to end: the seat's own process has a
        presence record, and it reads waiting for 'dialog open' — the state a
        throwaway pane measured while this dialog stood. The confirm goes
        through and the report says both witnesses agreed."""
        self._seat_presence(status="waiting", waitingFor="dialog open")
        rc, refusal = self._apply_through(_ExitDialogAd(focus=1))
        self.assertEqual(rc, 0, refusal and refusal.line())
        self.assertEqual(self._on("dialog"), [("1", False)])
        self.assertIn("presence record for pid %d says it waits for "
                      "'dialog open'" % SEAT_PID, self.text)

    def test_a_presence_record_that_contradicts_the_dialog_presses_nothing(self):  # noqa: VACUOUS_ASSERTION — no key on the dialog IS the contract; the same keystroke record carries the /exit that opened it, and the refusal carries the record's own words
        """The dialog stands on screen, and the vendor's own record for the
        seat's process says the session is NOT waiting at a dialog. Two
        witnesses disagree about whether this dialog awaits input now, and
        the door refuses: a contradiction is never a licence to press."""
        self._seat_presence(status="shell")
        rc, refusal = self._apply_through(_ExitDialogAd(focus=1),
                                          exit_wait=0.05)
        self.assertEqual(rc, 1)
        self.assertEqual(refusal.check, "clean exit")
        self.assertIn("/exit -> manual", self.text)
        self.assertEqual(self._on("dialog"), [],
                         "a key was pressed over a contradicting record: %r"
                         % (self.ad.keys,))
        self.assertEqual(self._on("composer"), [("/exit", False), ("", True)])
        line = refusal.line()
        self.assertIn("status 'shell'", line)
        self.assertIn("its witness refuses", line)
        self.assertFalse(self.ad.exited)

    def test_a_plain_composer_exit_spends_no_confirm_keystroke(self):
        """THE POSITIVE CONTROL, through the same double: a seat with no
        background tasks exits on the /exit's own Enter, and the dialog door
        adds NOTHING — no second Enter, no digit."""
        rc, refusal = self._apply_through(_ExitDialogAd(dialog=False))
        self.assertEqual(rc, 0, refusal and refusal.line())
        self.assertEqual([k[1:] for k in self.ad.keys
                          if "helm launch" not in k[1]],
                         [("/exit", False, "composer"),
                          ("", True, "composer")],
                         "the plain exit spent a keystroke beyond its own")
        self.assertEqual(len(self._launched()), 1)
        self.assertIn("PROVEN gone", self.text)
        self.assertEqual(seat_rehome.events()[0][0]["exit_proven_by"], "pane")

    def test_an_unverified_exit_is_proven_by_the_pid_and_the_rehome_proceeds(self):
        """THE PID OUTRANKS THE PANE. A session with no background tasks exits
        on the /exit's own Enter and takes its composer with it, so the
        read-back finds none and the wake answers UNVERIFIED about an exit
        that worked. The old pid is then PROVEN gone inside the existing wait,
        and that is the fact the rest of the plan needs: the launch line is
        typed once with its marker, all three surfaces come back, and the
        ledger row says the exit was proven by the pid, not by the pane.

        CAUSAL: the double ends the process at the /exit's Enter and nowhere
        else, and the pane it leaves has no composer to read."""
        rc, refusal = self._apply_through(_ExitDialogAd(plain="shell"))
        self.assertEqual(rc, 0, refusal and refusal.line())
        self.assertIn("/exit -> unverified", self.text,
                      "the pane proved this exit, so the arm no longer "
                      "measures the pid standing in for it")
        self.assertEqual(self._on("composer"), [("/exit", False), ("", True)])
        self.assertEqual(self._on("dialog"), [])
        launch = self._launched()
        self.assertEqual(len(launch), 1, "the launch line was not typed once: "
                         "%r" % (self.ad.keys,))
        self.assertEqual(_marker_in(launch[0][1]),
                         seat_rehome.events()[0][0]["attempt"])
        self.assertIn("proven by the pid, not by the pane", self.text)
        for surface in ("pane", "register", "beacon"):
            self.assertIn("  %-9s PROVEN" % surface, self.text)
        self.assertNotIn("UNPROVEN", self.text)
        row = seat_rehome.events()[0][0]
        self.assertEqual((row["exit_wake"], row["exit_proven_by"]),
                         ("unverified", "pid"))
        self.assertEqual(row["verified"],
                         {"pane": True, "register": True, "beacon": True})

    def test_an_unverified_exit_whose_pid_outlives_the_wait_refuses(self):  # noqa: VACUOUS_ASSERTION — no launch over a live process IS the contract; the same keystroke record's composer rows are asserted non-empty and the refusal asserted to carry the wait's own reason, so the verb reached the wait
        """THE OVERRIDE'S NEGATIVE: the same UNVERIFIED wake, and the old
        process is still alive when the wait ends. Nothing proved the exit, so
        the verb refuses as before, says the pid did not prove it either, and
        names what the pane shows and that the session still runs."""
        rc, refusal = self._apply_through(_ExitDialogAd(plain="hang"),
                                          exit_wait=0.05)
        self.assertEqual(rc, 1)
        self.assertEqual(refusal.check, "clean exit")
        self.assertEqual(self._on("composer"), [("/exit", False), ("", True)])
        self.assertEqual(self._launched(), [],
                         "a launch line was typed over a process still alive")
        self.assertIn("/exit -> unverified", self.text)
        line = refusal.line()
        self.assertIn("the pid did not prove it either", line)
        self.assertIn("still alive after", line)
        self.assertIn("no composer helm can read and no dialog it recognises",
                      line)
        self.assertIn("still running", line)
        self.assertNotIn("untouched", line)

    def test_a_pointer_on_stay_is_never_pressed_and_the_refusal_names_the_dialog(self):  # noqa: VACUOUS_ASSERTION — no keystroke on the dialog and no launch IS the contract; the same keystroke record's composer rows are asserted non-empty, so the double demonstrably recorded the /exit that opened it
        """THE NEGATIVE. Claude Code opens the dialog on option 1, so a pointer
        on "3. Stay" means something answered it after it opened — a person,
        most likely, choosing to keep the session. Helm does not move the
        pointer back and does not press anything: it REFUSES, and the refusal
        says the pane is sitting in an open exit-confirm dialog with the
        pointer on Stay. It never says "untouched" — the /exit landed.

        A PROVEN NON-EXIT NEVER REACHES THE PID WAIT: the wake is `manual`,
        an answer, so there is nothing for the pid to prove. The wait is
        shortened only so a regression that sent this arm to it fails fast."""
        rc, refusal = self._apply_through(_ExitDialogAd(focus=3),
                                          exit_wait=0.05)
        self.assertEqual(rc, 1)
        self.assertIn("/exit -> manual", self.text)
        self.assertEqual(refusal.check, "clean exit")
        self.assertEqual(self._on("dialog"), [],
                         "a keystroke reached a dialog whose pointer is on "
                         "Stay: %r" % (self.ad.keys,))
        # CONTROL on the same keystroke record: the /exit DID land, which is
        # what opened the dialog this arm refuses to answer.
        self.assertEqual(self._on("composer"), [("/exit", False), ("", True)])
        self.assertEqual(self._launched(), [])
        self.assertFalse(self.ad.exited)
        line = refusal.line()
        self.assertIn("exit-confirm dialog", line)
        self.assertIn("3. Stay", line)
        self.assertIn("still running", line)
        self.assertNotIn("untouched", line)
        self.assertNotIn("the pid did not prove it either", line,
                         "a proven non-exit went on to the pid wait")

    def test_a_dialog_helm_did_not_open_is_never_answered(self):  # noqa: VACUOUS_ASSERTION — an EMPTY keystroke record is the contract; the double's own dialog is asserted still standing and the refusal text asserted to name it, so the pane was read and was a dialog
        """NEVER BLIND. The pane is ALREADY in the exit-confirm dialog when the
        verb arrives, pointer on option 1 — somebody else's /exit. The turn
        verb's pre-read finds no composer and types nothing, so the dialog door
        has no /exit of its own to follow up and presses nothing either. The
        refusal names what the pane shows.

        AND THE PID IS NEVER ASKED. The pre-read's refusal is UNVERIFIED, the
        same wake the pid override keys on; what keeps it out of the wait is
        that this apply typed no /exit. Were it to wait, the person confirming
        their own dialog inside the window would read as this verb's exit and
        get a launch line. The wait is shortened only so that regression
        fails fast."""
        # the dialog hides the mode footer, so the operator names the mode
        # (task/3515) and the arm reaches the exit door it measures
        rc, refusal = self._apply_through(_ExitDialogAd(focus=1, showing=True),
                                          exit_wait=0.05,
                                          mode="bypassPermissions")
        self.assertEqual(rc, 1)
        self.assertEqual(refusal.check, "clean exit")
        self.assertIn("/exit -> unverified", self.text)
        self.assertEqual(self.ad.keys, [],
                         "a keystroke reached a pane helm had not typed /exit "
                         "into: %r" % (self.ad.keys,))
        self.assertEqual(self.ad.showing, HANDLE,
                         "the dialog closed, so this arm no longer measures "
                         "a dialog left standing")
        line = refusal.line()
        self.assertIn("exit-confirm dialog", line)
        self.assertIn("1. Exit and stop tasks", line)
        self.assertNotIn("the pid did not prove it either", line,
                         "a wake over a /exit this apply never typed went on "
                         "to the pid wait")

    def test_a_dialog_that_never_closes_gets_ONE_enter_and_a_loud_refusal(self):  # noqa: VACUOUS_ASSERTION — no launch over a live process IS the contract; the same keystroke record carries exactly one Enter on the dialog and the /exit on the composer, so the verb acted and was recorded
        """THE BOUNDED READ-BACK. The confirm key is pressed ONCE and the
        pane is read back a bounded number of times; a dialog still standing
        after them is UNKNOWN, which the rehome refuses, and no second key is
        ever spent on it. That one key's send carries the act bound every
        other act-door keystroke gets, not the transport's 60s default.

        CAUSAL: this double swallows the confirm key, so the dialog stands
        on every read-back and the process never ends.

        THE PID IS ASKED TOO, and does not rescue it. The wake reads
        UNVERIFIED and this apply typed the /exit, so the verb goes on to the
        pid wait (shortened here); the process outlives it, and the refusal
        says the pid did not prove the exit either."""
        rc, refusal = self._apply_through(_StuckExitDialogAd(), exit_wait=0.05)
        self.assertEqual(rc, 1)
        self.assertEqual(refusal.check, "clean exit")
        self.assertEqual(self._on("dialog"), [("1", False)],
                         "the dialog was not pressed exactly ONCE, with option "
                         "1's digit: %r" % (self.ad.keys,))
        self.assertEqual(self.ad.timeouts, [harness.act_send_s()])
        self.assertEqual(self._on("composer"), [("/exit", False), ("", True)])
        self.assertEqual(self._launched(), [])
        self.assertFalse(self.ad.exited)
        line = refusal.line()
        self.assertIn("has not been seen to close", line)
        self.assertIn("the pid did not prove it either", line)
        self.assertIn("still running", line)


class _StuckExitDialogAd(_ExitDialogAd):
    """The exit-confirm double whose confirm key never closes the dialog:
    the keystroke is recorded and swallowed. `timeouts` is the bound each
    keystroke on the dialog was sent with."""

    def __init__(self):
        super().__init__(focus=1)
        self.timeouts = []

    def send(self, handle, text, enter=True, timeout=60):
        if self.showing != handle:
            return super().send(handle, text, enter=enter, timeout=timeout)
        self.keys.append((handle, text, enter, "dialog"))
        self.timeouts.append(timeout)


class LaunchLineScopeTest(RehomeBase):
    """THE ASSIGNMENT'S SCOPE, MEASURED IN A REAL SHELL.

    Every other arm in this file reads the marker out of a /proc entry the
    fixture wrote, which proves what the verification does with a value and
    nothing about whether a shell would ever deliver one. This class runs the
    SHIPPED composer's output through `bash -c`, the way the pane runs it, and
    reads the marker back out of the child's own environment.
    """

    def _run(self, *lines):
        """The composed lines, in ONE shell, in order. The environment is
        minimal ON PURPOSE: a marker inherited from the test runner would make
        every assertion below vacuous.

        THE SHELL HAVING RUN IS THE FIXTURE'S OWN GUARD, asserted here rather
        than in the arm: a broken fixture becomes a readable message, and the
        arm's observables stay the two environment dumps the shell writes.
        """
        bash = shutil.which("bash")
        self.assertTrue(bash, "no bash here, so a pane line cannot be run the "
                              "way a pane runs it")
        done = subprocess.run([bash, "-c", "\n".join(lines)],
                              env={"PATH": os.environ.get("PATH", "")},
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)

    def _dumper(self):
        """A stand-in for the launch executable that writes its OWN environment
        to the file it is given."""
        path = os.path.join(self.tmp, "fixture-launch")
        with open(path, "w") as f:
            f.write("#!/bin/sh\nenv > \"$1\"\n")
        os.chmod(path, 0o755)
        return path

    def _env_of(self, path):
        out = {}
        with open(path) as f:
            for row in f.read().splitlines():
                key, _sep, value = row.partition("=")
                out.setdefault(key, value)
        return out

    def test_the_child_inherits_the_marker_and_a_sibling_launch_does_not(self):
        """TWO QUESTIONS ONE SHELL ANSWERS TOGETHER.

        FORWARD: the process the composed line starts carries the marker. It
        runs after the `cd`, in the seat's directory, with the assignment bound
        to the launch invocation — so a rehome's verification can ask a real
        child for a real value.

        SIDEWAYS, the scoping control: a SECOND launch line, composed WITHOUT
        this apply and run in the SAME shell immediately afterwards, carries no
        marker at all. If the assignment were an `export`, that sibling would
        inherit it and be credited to a rehome that did not start it — which is
        the same defect as the neighbour, minted by us.

        THE SIBLING'S FILE IS ITS OWN CONTROL: it is read for a variable that
        must be absent, so it is also asserted to be a real environment dump
        from a process that really ran in the seat's directory.
        """
        cwd = os.path.join(self.tmp, "seat-worktree")
        os.makedirs(cwd)
        script, root = self._dumper(), os.environ["HELM_HOME"]
        ours = os.path.join(root, "child-env")
        sibling = os.path.join(root, "sibling-env")
        attempt = seat_rehome.new_attempt()
        line = seat_rehome.attempt_launch_line(
            "%s %s" % (shlex.quote(script), shlex.quote(ours)), cwd, attempt)
        beside = seat_resume_all.pane_launch_line(
            "%s %s" % (shlex.quote(script), shlex.quote(sibling)), cwd)
        self._run(line, beside)

        child = self._env_of(ours)
        self.assertIn("PWD", child,
                      "the launched child dumped no environment at all, so "
                      "nothing below reads its inheritance")
        self.assertEqual(child.get(seat_rehome.ATTEMPT_ENV), attempt,
                         "the launched child did not inherit the marker")
        self.assertEqual(os.path.realpath(child.get("PWD", "")),
                         os.path.realpath(cwd),
                         "the child did not run in the seat's directory, so "
                         "the cd and the assignment are not in the order the "
                         "composer claims")
        after = self._env_of(sibling)
        # READ THE SIBLING'S DUMP BEFORE ASKING WHAT IS MISSING FROM IT: a file
        # that was never written, or written by nothing, is missing the marker
        # too.
        self.assertIn("PWD", after,
                      "the sibling dumped no environment at all, so its "
                      "missing marker says nothing")
        self.assertNotIn(seat_rehome.ATTEMPT_ENV, after,
                         "a sibling launch in the same shell inherited this "
                         "rehome's marker, so the assignment is not bound to "
                         "the launch invocation")
        self.assertEqual(os.path.realpath(after.get("PWD", "")),
                         os.path.realpath(cwd),
                         "the sibling wrote no real environment, so its "
                         "missing marker says nothing")
        # AND THE TEXT, ASSERTED LAST ON PURPOSE. The shell above is the
        # measurement; these three say WHERE the composer put the assignment,
        # so a producer that leaked the marker some other way — an export, a
        # prefix on the `cd` — is caught by the run and described by the text
        # rather than only by the text.
        self.assertTrue(line.startswith("cd "), line)
        self.assertIn(" && %s=%s " % (seat_rehome.ATTEMPT_ENV, attempt), line)
        self.assertNotIn("export", line)


SKIP = "--dangerously-skip-permissions"


class DefaultHomeTest(RehomeBase):
    """task/3515 — `--home default` moves a seat PINNED to a named credhome
    onto the Orca-synced default home, and every rehome keeps the permission
    mode the old process ran with and names what its exit stopped.

    The seat's OLD process is planted as a real /proc row on a named credhome,
    so the plan reads its mode through the shipped readers
    (`beacons.proc_argv`, `beacons.proc_env`) and the old home's own
    settings.json. The apply arms reuse ApplyTest's causal fixture: the new
    process exists only because the launch line was typed, and it inherits the
    permission words the line carried and nothing else."""

    _apply = ApplyTest._apply
    _apply_through = ApplyTest._apply_through
    _launched = ApplyTest._launched
    _plant_neighbour = ApplyTest._plant_neighbour

    def _old_seat(self, perm=(), settings=None):
        """The seat as it runs today: pinned to a named credhome by
        CLAUDE_CONFIG_DIR, started with `perm` on its argv. `settings` is the
        old home's settings.json body."""
        name, d = self.plant_home(email="pinned@example.test",
                                  creds=_creds("FAKE-PINNED", 8))
        if settings is not None:
            with open(os.path.join(d, "settings.json"), "w") as f:
                json.dump(settings, f)
        self.plant_proc(SEAT_PID, d, perm=perm)
        self.old_dir = d
        return name, d

    def _default_live(self, **kw):
        self.plant_default_home(creds=_creds("FAKE-DEFAULT", 8, **kw))
        self.home_dir = None
        return self.defaults["claude"]

    def _plan(self, target="default"):
        self.plant_roster()
        with self.census([_proc()]):
            return seat_rehome.plan(SEAT, target, model="opus",
                                    adapter=self.ad)

    def test_the_default_plan_unsets_the_config_dir_and_carries_the_flag(self):
        """THE POSITIVE. The launch line runs with CLAUDE_CONFIG_DIR REMOVED
        (the default home is the absence of the variable, never ~/.claude
        spelled into it), names no --home, and carries the old process's own
        --dangerously-skip-permissions, because each home's settings.json
        carries its own defaultMode and a relaunch with no flag takes the new
        home's (measured: a bypass seat came back in auto mode)."""
        self._old_seat(perm=[SKIP])
        self._default_live()
        p, refusal = self._plan()
        self.assertIsNone(refusal, refusal and refusal.line())
        self.assertEqual(
            p["command"],
            "env -u CLAUDE_CONFIG_DIR helm launch --seat %s -- --model opus "
            "%s --resume %s" % (SEAT, SKIP, SID))
        self.assertNotIn("--home", p["pane_line"])
        self.assertEqual(p["permission"], [SKIP])

    def test_a_named_rehome_carries_the_old_permission_mode_too(self):
        """The mode rule is not the default target's alone: a named credhome
        has its own settings.json default as well."""
        self._old_seat(perm=["--permission-mode", "acceptEdits"])
        self.ad.footer = ACCEPT_FOOTER
        name = self.live_home()
        p, refusal = self._plan(target=name)
        self.assertIsNone(refusal, refusal and refusal.line())
        self.assertIn("--home %s -- --model opus --permission-mode acceptEdits "
                      "--resume %s" % (name, SID), p["command"])
        self.assertFalse(p["command"].startswith("env -u"))

    def test_argv_without_a_flag_falls_back_to_the_old_homes_settings(self):
        """No flag on the old argv: the mode the session ran with was the OLD
        home's settings.json permissions.defaultMode, and the relaunch passes
        it explicitly."""
        self._old_seat(settings={"permissions":
                                 {"defaultMode": "bypassPermissions"}})
        self._default_live()
        p, refusal = self._plan()
        self.assertIsNone(refusal, refusal and refusal.line())
        self.assertIn("-- --model opus --permission-mode bypassPermissions "
                      "--resume %s" % SID, p["command"])

    def test_a_settings_mode_outside_the_clis_choices_is_never_passed(self):
        """CURE P2-1: a settings default the CLI does not take would exit the
        relaunch and leave the seat on no home, so it names no mode: it is
        never passed, and with no footer the mode is UNKNOWN (CURE3)."""
        self._old_seat(settings={"permissions": {"defaultMode": "yolo"}})
        self._default_live()
        self.ad.footer = ""
        p, refusal = self._plan()
        self.assertIsNone(refusal, refusal and refusal.line())
        self.assertIsNone(p["permission"])
        self.assertNotIn("yolo", p["command"])
        self.assertIn("UNKNOWN", p["permission_note"])
        for mode in sorted(seat_rehome.PERMISSION_MODES):
            self.assertEqual(seat_rehome._valid_mode(mode), mode)

    def test_a_project_layer_is_a_cross_check_on_the_footer(self):
        """The cwd's project layers name a startup mode; the footer that
        agrees with them is passed explicitly, and one that disagrees is
        UNKNOWN (CURE3: startup layers are never proof, only a check)."""
        for layer in ("settings.local.json", "settings.json"):
            with self.subTest(layer=layer):
                self._old_seat(settings={"permissions":
                                         {"defaultMode": "bypassPermissions"}})
                cwd = self.plant_cwd(SEAT_PID, name="wt-" + layer)
                os.makedirs(os.path.join(cwd, ".claude"), exist_ok=True)
                with open(os.path.join(cwd, ".claude", layer), "w") as f:
                    json.dump({"permissions": {"defaultMode": "plan"}}, f)
                self._default_live()
                self.ad.footer = PLAN_FOOTER
                p, refusal = self._plan()
                self.assertIsNone(refusal, refusal and refusal.line())
                self.assertEqual(p["permission"], ["--permission-mode", "plan"])
                self.assertIn(layer, p["permission_note"])
                self.ad.footer = None            # the bypass row
                p, _refusal = self._plan()
                self.assertIsNone(p["permission"])
                self.assertIn("UNKNOWN", p["permission_note"])
                os.unlink(os.path.join(self.proc, str(SEAT_PID), "cwd"))

    def test_a_footer_showing_plan_relaunches_in_plan(self):
        """CURE3: the old argv carries no flag and no settings layer names a
        mode, and the pane's footer says plan mode: the relaunch passes
        --permission-mode plan, even onto a home whose settings say bypass,
        and verification asks the new argv for it."""
        self._old_seat()
        name = self.live_home()
        with open(os.path.join(self.home_dir, "settings.json"), "w") as f:
            json.dump({"permissions": {"defaultMode": "bypassPermissions"}}, f)
        self.ad.footer = PLAN_FOOTER
        p, refusal = self._plan(target=name)
        self.assertIsNone(refusal, refusal and refusal.line())
        self.assertEqual(p["permission"], ["--permission-mode", "plan"])
        self.assertIn("-- --model opus --permission-mode plan --resume %s"
                      % SID, p["command"])
        self.assertIn("footer", p["permission_note"])
        p = dict(p, attempt=FOREIGN_MARK)
        for perm, ok in ((["--permission-mode", "plan"], True), ([], False)):
            self.plant_proc(NEW_PID, self.home_dir, session=SID,
                            attempt=FOREIGN_MARK, perm=perm)
            ident, why = seat_rehome._planned_process(
                p, {"pane_pids": [orcaadopt.ProcIdent(NEW_PID, NEW_START)]})
            if ok:
                self.assertEqual(ident, NEW_PID, why)
            else:
                self.assertIsNone(ident)
                self.assertIn("--permission-mode plan", why)

    def test_a_footer_that_disagrees_with_argv_is_unknown(self):
        self._old_seat(perm=["--permission-mode", "acceptEdits"])
        self._default_live()
        self.ad.footer = PLAN_FOOTER
        p, refusal = self._plan()
        self.assertIsNone(refusal, refusal and refusal.line())
        self.assertIsNone(p["permission"])
        self.assertIn("says plan and acceptEdits", p["permission_note"])

    def test_the_footer_names_every_measured_mode(self):
        for row, mode in ((PLAN_FOOTER, "plan"), (ACCEPT_FOOTER, "acceptEdits"),
                          (MANUAL_FOOTER, "manual"),
                          ("  ⏵⏵ auto mode on (shift+tab to cycle) · ← for "
                           "agents", "auto"),
                          ("  ⏵⏵ don't ask on (shift+tab to cycle) · ← for "
                           "agents", "dontAsk"),
                          ("  ⏵⏵ bypass permissions on", "bypassPermissions")):
            self.assertEqual(panetail.permission_footer(_framed(row))[0], mode)
        self.assertIsNone(panetail.permission_footer(_framed())[0])
        self.assertIsNone(panetail.permission_footer(
            _framed(PLAN_FOOTER, ACCEPT_FOOTER))[0])
        # a quoted footer high in the transcript is not the footer
        self.assertIsNone(panetail.permission_footer(
            "\n".join([PLAN_FOOTER] + ["line %d" % n for n in range(8)]))[0])

    def test_a_footer_quoted_above_the_input_box_is_never_the_footer(self):
        """helm-codex's QC of CURE3: the last six rows were read whatever they
        were, so a transcript line quoting a footer just above the input box
        proved a mode the pane never drew once the real footer was hidden
        (a dialog, a statusline that wraps). The footer is chrome BELOW the
        input box's bottom rule, so only rows under the last rule count."""
        quoted = "\n".join(("assistant: the pane said", PLAN_FOOTER,
                            "─" * 40, "❯", "─" * 40,
                            "  opus-5 | ~/dev/example/repo"))
        self.assertIsNone(panetail.permission_footer(quoted)[0])
        # must-hit: the same frame with the real footer under the rule
        self.assertEqual(panetail.permission_footer(
            quoted + "\n" + ACCEPT_FOOTER)[0], "acceptEdits")
        # no input box at all proves nothing
        self.assertIsNone(panetail.permission_footer(ACCEPT_FOOTER)[0])
        # helm-codex's QC of CURE4: a transcript that prints a rule of its
        # own and a mode line, while a dialog hides the real box, is no input
        # box: the rule must close a box whose composer row (❯) sits under
        # an opening rule
        forged = "\n".join(("assistant: done", "─" * 40, PLAN_FOOTER))
        self.assertIsNone(panetail.permission_footer(forged)[0])
        self.assertIsNone(panetail.permission_footer("\n".join((
            "─" * 40, "some output", "─" * 40, PLAN_FOOTER)))[0])

    def test_default_is_ambiguous_while_a_credhome_is_named_default(self):
        """CURE P3-1: a credhome literally named `default` makes the reserved
        word ambiguous, so it is refused; the credhome is still reachable by
        its path."""
        self._default_live()
        d = os.path.join(self.roots["claude"], "default")
        os.makedirs(d)
        note, refusal = seat_rehome.check_home("default")
        self.assertIsNone(note)
        self.assertIsNotNone(refusal, "an ambiguous `default` was admitted")
        self.assertIn("ambiguous", refusal.reason)
        os.rmdir(d)
        note, refusal = seat_rehome.check_home("default")
        self.assertIsNone(refusal, refusal and refusal.line())

    def test_an_arbitrary_non_credhome_path_is_still_refused(self):
        """The default is a RESERVED WORD, never an accepted path: the default
        home's own path and any other directory keep rule 1's refusal. The
        control is the reserved word itself, accepted over the same home."""
        self._default_live()
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        for path in (self.defaults["claude"], elsewhere):
            note, refusal = seat_rehome.check_home(path)
            self.assertIsNone(note, path)
            self.assertIsNotNone(refusal, "%s was accepted" % path)
        note, refusal = seat_rehome.check_home("default")
        self.assertIsNone(refusal, refusal and refusal.line())
        self.assertIn("default", note)

    def test_a_default_home_with_an_expired_token_is_refused(self):
        """Orca, not helm, refreshes the default home's token, so there is no
        sync leg to rescue a dead one: an expired refresh chain refuses before
        the pane is touched, and so does a default home with no credentials.
        The control is the same home with a live chain."""
        self._default_live(refresh_hours=-1)
        note, refusal = seat_rehome.check_home("default")
        self.assertIsNone(note)
        self.assertIsNotNone(refusal, "an expired default home was admitted")
        self.assertIn("EXPIRED", refusal.reason)
        os.unlink(os.path.join(self.defaults["claude"], ".credentials.json"))
        cred.cache_clear()
        note, refusal = seat_rehome.check_home("default")
        self.assertIsNotNone(refusal, "a default home with no token was "
                                      "admitted")
        self._default_live()
        note, refusal = seat_rehome.check_home("default")
        self.assertIsNone(refusal, refusal and refusal.line())

    def test_an_unproved_mode_refuses_the_apply_before_the_exit(self):
        """CURE3: the old argv is unreadable and the pane draws no mode
        footer, so the live mode is UNKNOWN: --apply refuses before the
        /exit, naming it, and nothing is typed."""
        self._old_seat()
        os.unlink(os.path.join(self.proc, str(SEAT_PID), "cmdline"))
        self._default_live()
        self.ad.footer = ""
        rc, refusal = self._apply(name="default", launch={"home": None})
        self.assertEqual(rc, 1)
        self.assertEqual(refusal.check, "permission mode is proved")
        self.assertIn("UNKNOWN", refusal.reason)
        self.assertIn("--mode", refusal.remedy)
        self.assertEqual(self.ad.sent, [], "an UNKNOWN mode typed into the pane")

    def test_an_operator_mode_applies_and_is_verified(self):
        """The way out: --mode plan over the same unproved pane is recorded as
        operator-chosen, rides the launch line, and the new process is
        verified to carry it."""
        self._old_seat()
        os.unlink(os.path.join(self.proc, str(SEAT_PID), "cmdline"))
        self._default_live()
        self.ad.footer = ""
        rc, refusal = self._apply(name="default", launch={"home": None},
                                  mode="plan")
        self.assertEqual(rc, 0, refusal and refusal.line())
        line = self._launched()[0][1]
        self.assertIn("--permission-mode plan", line)
        self.assertNotIn("UNPROVEN", self.text)
        row = seat_rehome.events()[0][0]
        self.assertEqual(row["permission"], ["--permission-mode", "plan"])
        self.assertIn("operator-chosen", row["permission_note"])

    def test_the_dry_run_shows_an_unknown_mode_and_the_refusal(self):
        self._old_seat()
        self._default_live()
        self.ad.footer = ""
        self.plant_roster()
        out = io.StringIO()
        with self.census([_proc()]):
            p, refusal = seat_rehome.plan(SEAT, "default", model="opus",
                                          adapter=self.ad)
        self.assertIsNone(refusal, refusal and refusal.line())
        seat_rehome.print_plan(p, out=out)
        self.assertIn("mode      UNKNOWN", out.getvalue())
        self.assertIn("--apply refuses before the /exit", out.getvalue())
        with self.census([_proc()]):
            _p, refusal = seat_rehome.plan(SEAT, "default", adapter=self.ad,
                                           mode="yolo")
        self.assertIn("not a --permission-mode", refusal.reason)
        # `default` is not in the choice set claude 2.1.284 lists (it only
        # draws manual), so an operator names manual, never the hidden word
        with self.census([_proc()]):
            _p, refusal = seat_rehome.plan(SEAT, "default", adapter=self.ad,
                                           mode="default")
        self.assertIsNotNone(refusal, "--mode default was admitted")
        self.assertIn("manual", refusal.remedy)

    def test_the_default_apply_proves_the_new_process_left_the_pin(self):
        """THE POSITIVE APPLY: the new process has NO CLAUDE_CONFIG_DIR, is
        named for the seat, and its argv carries the session, the model and
        the kept permission flag."""
        self._old_seat(perm=[SKIP])
        self._default_live()
        rc, refusal = self._apply(name="default", launch={"home": None})
        self.assertEqual(rc, 0, refusal and refusal.line())
        line = self._launched()[0][1]
        self.assertIn("env -u CLAUDE_CONFIG_DIR helm launch", line)
        self.assertIn(SKIP, line)
        self.assertNotIn("UNPROVEN", self.text)
        row = seat_rehome.events()[0][0]
        self.assertEqual(row["home"], "default")
        self.assertEqual(row["permission"], [SKIP])

    def test_verification_fails_when_the_new_process_keeps_the_old_pin(self):
        """The launch came up still carrying the OLD CLAUDE_CONFIG_DIR: every
        other property matches, and it is not on the default home."""
        _name, old = self._old_seat(perm=[SKIP])
        self._default_live()
        rc, refusal = self._apply(name="default", launch={"home": old},
                                  verify_s=0.05)
        self.assertEqual(rc, 1)
        self.assertEqual(refusal.check, "post-launch verification")
        self.assertIn("pane", refusal.reason)
        self.assertIn(cred._display_path(old), self.text)

    def test_verification_fails_when_the_new_process_lost_the_mode(self):
        """A process that came up without the kept flag runs on the new
        home's settings default, which is the measured defect."""
        self._old_seat(perm=[SKIP])
        self._default_live()
        rc, refusal = self._apply(name="default",
                                  launch={"home": None, "perm": []},
                                  verify_s=0.05)
        self.assertEqual(rc, 1)
        self.assertEqual(refusal.check, "post-launch verification")
        self.assertIn(SKIP, self.text)

    def test_the_ledger_and_the_wake_name_what_the_exit_stopped(self):
        """The exit dialog lists what it will stop, and none of it survives a
        relaunch: the rows this /exit's dialog listed land in the ledger row,
        and the seat's post-relaunch wake carries them as a re-arm line."""
        self._old_seat(perm=[SKIP])
        self._default_live()
        # The suite's chat dir is one per PROCESS (tests/__init__.py), so the
        # lane already holds earlier arms' wakes: only rows after this apply
        # are this apply's.
        before = chat.read(chat.dm_room(SEAT))[1]
        rc, refusal = self._apply_through(_ExitDialogAd(focus=1),
                                          name="default",
                                          launch={"home": None})
        self.assertEqual(rc, 0, refusal and refusal.line())
        row = seat_rehome.events()[0][0]
        self.assertEqual(row["stopped"][0], "monitor · example-seat inbox "
                                            "beacon")
        self.assertEqual(len(row["stopped"]), 4, row["stopped"])
        self.assertTrue(row["stopped"][1].startswith("shell · "))
        self.assertTrue(row["wake"]["posted"], row["wake"])
        texts = [r.get("text") or "" for r in
                 chat.read(chat.dm_room(SEAT), since=before)[0]]
        wake = [t for t in texts if "re-arm" in t]
        self.assertEqual(len(wake), 1, texts)
        self.assertIn("monitor · example-seat inbox beacon", wake[0])


if __name__ == "__main__":
    unittest.main()
