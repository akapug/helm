# Wiring `helm inject` into your harness

`helm inject` is the one active-fire surface: a hook calls it once per turn
with the prompt text; helm returns the context worth injecting — the pinned
lane (budget-capped), just-in-time typed-store matches, and live reflex
steers. No match, no output, no cost. The same call works from every harness,
which is what makes your knowledge fire wherever you work.

## The self-closing path: `helm hooks install`

For Claude Code you never hand-wire this — helm installs its own hook:

```console
$ helm hooks install            # every claude home + every seat, incl. ~/.claude
helm hooks: inject (UserPromptSubmit): timeout 10 /path/to/helm/bin/helm inject --hook-json || true
helm hooks: deliver (PostToolUse): timeout 2 /path/to/helm/bin/helm chat deliver --hook-json || true
helm hooks: join (SessionStart): timeout 5 /path/to/helm/bin/helm chat join --hook-json || true
  you-example-com              add    backup: none — new file
  (default-claude)             update backup: ~/.cache/helm/config-backups/…
helm hooks: seats (full hook contract — inject + delivery + handoff + resume):
  codex                        add    backup: none — new file
helm hooks: 2 of 2 claude homes covered
helm hooks: 1 of 1 seats covered (full hook contract)
$ helm hooks status             # per-home + per-seat coverage table, read-only
$ helm hooks install --dry      # the would-be diff per home/seat, nothing written
```

The installer merges the whole hook estate — 12 entries per credential home
and per family seat (one per `hooks.SPECS`/`hooks.SEAT_SPECS` row; the count is
test-pinned against that canonical tuple) — into each surface's
`settings.json`. Together they close the loop: turn start + tool boundary +
shell-argv gate + local-suite gate + session start + idle gate + compaction
continuity + compaction resume.

**ONE ROW IS AN EXTERNAL GUARD** (`external` in its spec): `suite-guard` runs an
executable helm does not ship. It is resolved BY NAME — `shutil.which`, or
`HELM_SUITE_GUARD` to pin a path — never as a literal host path, because helm's
own pre-push `hostpath_guard` refuses a `/home/<user>/` literal in a pushed
blob. It is OPTIONAL until configured: a host with nothing by that name on
`PATH` and no pin installs every other hook, and `helm hooks install` and
`helm hooks status` report it as "not configured (optional)". Once the
executable is on `PATH` or `HELM_SUITE_GUARD` is set, it is REQUIRED: a
configured guard that stops resolving gets the spec left OUT of what is
written (`hooks.resolved_specs`) rather than a hook pointing at nothing, every
install door returns a SHORTENED result, and `helm doctor` says so in one loud
row. Being in this table is the whole point of
task/1006: the guard existed for six days wired into `~/.claude` alone, was
hand-copied into 8 seat configs on 2026-08-11, and the two seats minted after
that patch were born unguarded while `helm hooks status` reported *"10 of 10
seats covered (full hook contract)"* — a guard named in no list is a guard no
census can count.

| event | command | what it carries |
|---|---|---|
| `UserPromptSubmit` | `helm inject --hook-json` (timeout 10) | the per-turn context lane (pinned + JIT + reflexes) |
| `PostToolUse` (`*`) | `helm chat deliver --hook-json` (timeout 2) | the delivery lane plus silent delegation producer: rows addressed to the seat (@mentions, replies, DMs, @all) and the home room's plain rows from people and seats nudge between tools, one per boundary, on the lead's main thread only; a plain row from a subsystem label (`helm/machine_senders.py`: proxywatch, beacons, autocompact, gc summaries and the like) is pulled with `helm chat read`, and the stop-guard and pending counts skip it too. Outside helm's project the leg runs only for a pane Orca opened (`hooks.orca_admits`), and only for that seat's own mail: DMs, @mentions, replies and reactions in any room, and plain rows of its own project's room; an @all in #main, #helm or another project's room is not delivered there unless the seat's own beacon already rang for it, and the leg records no delegation evidence and speaks no whisper, because the hooks that consume those stay scoped. A subagent's payload (`agent_id`) and a payload that does not parse deliver nothing and move no cursor. A documented subagent `agent_id` event at an exactly claimed lane records lease/session/holder-bound interval evidence |
| `SubagentStop` (`*`) | `helm chat delegation-stop --hook-json` (timeout 2) | tombstones that exact full-session/`agent_id` independently of claim-lock contention; the next claim-locked read rejects and prunes it, so teardown never races another agent's producer |
| `SubagentStart` (`*`) | `helm saguide --hook-json` (timeout 5) | THE SUBAGENT'S INITIAL PHYSICS, and the one home of the rules every build-capable subagent must obey (the test route, the beacon, the sibling sweep a fix or a review owes, the row's REVIEW FIX MODE: PATCH commits its mechanical cure, MELD-DIFF posts an exact diff for the author to apply). `UserPromptSubmit` fires for a real user turn and NOT for the prompt handed to a subagent, so before this every SA ran with no premises and no guidance. It emits `hookSpecificOutput` with `hookEventName: SubagentStart` MINTED AT THE SOURCE — the harness discards a payload whose event name is not the firing event, silently, at exit 0, so reusing inject's envelope here installs green and delivers nothing. Scope is decided from the payload `cwd`: a foreign or unresolvable project gets NOTHING rather than helm's rules. Two harness-side limits are outside helm's reach: isolated-context subagents have hook context dropped before it arrives, and a `managedHooksOnly` filter skips user hooks for the built-in `web-fetch` agent. **Nested-spawn reflexes ride here too** (task/2971): each live `nested-spawn` reflex that reaches the payload's project (`reflex.spawn_steers`, fleet entries plus the project's own) follows the brief for helm and comes alone for any other project. This is the seam because the harness pushes a SubagentStart `additionalContext` into the subagent's opening messages, before its first step; a PreToolUse line on its Agent call would reach it only after that call had run. The hook runs in every project (task/2987), and its payload is scoped. **Build-capable children only**: a read-only `agent_type` (`saguide.READ_ONLY_AGENT_TYPES`: Explore, Plan, claude-code-guide, statusline-setup) gets nothing, and an unknown or missing type still gets the brief. The brief is at most 560 bytes (`tests/test_hook_budgets.py`) |
| `SessionStart` (`*`) | `helm chat join --hook-json` (timeout 5) | the autojoin: roster presence row + the seat's identity as session context (incl. the mandatory beacon-arm directive) |
| `SessionStart` (`*`) | `helm seat resume-turn --hook-json` (timeout 5) | the RESUME leg: on `source == "compact"` only, fork a detached child that waits out the composer settle and injects the seat's own handoff `NEXT:` back into its pane. If identity or delivery cannot be proven, the alert reports the measured recovery route: armed inbox beacon, pane-input fallback, or UNKNOWN — never an unconditional instruction. **A native autocompaction gets none of that**: the payload's `source` is `compact` for both kinds, so the verb reads the PreCompact record `handoff check` wrote for the same session — a recent `trigger: "auto"` (inside `HELM_RESUME_TURN_SPIRAL_S`), written by the same thread (the record's `agent` equals the payload's `agent_id`, both absent on the main thread) and not yet consumed, means the harness continues the interrupted turn itself, and the verb injects nothing, alerts nobody, and leaves one stderr line, a `native-auto` state entry and a ledger event. **One record vouches for exactly one SessionStart**: the verb marks it consumed in the same locked write; a record with a present-invalid field (a null record, a non-finite `at`, a missing or mistyped `trigger`, `session` or `agent`) never vouches; and a record stamped at or before the entry's last recorded resume-turn decision (`last_at`) is obsolete and never vouches. `trigger: "manual"` (a typed or helm-injected `/compact`), a consumed or obsolete record, a subagent's record, or NO record is the resume leg above, unchanged — absence of the record is not proof of a native compaction. BOUNDARY: a PreCompact whose record could not be written inside the producer's bounded wait leaves the previous record standing, and that record vouches only if it is unconsumed, same session and thread, inside the window and newer than the entry's last decision; reaching it takes the consumer refused at the auto SessionStart AND no later decision stamped AND the producer refused past its bound at a later manual PreCompact inside the window. Source-gated inside the verb, not by the matcher — the wildcard group is the shape proven live across the estate |
| `SessionStart` (`compact`) | `helm now show --hook-json` (timeout 3) | the WORKING SET after a compaction (task/4054): from the session's recorder ring (`working-set.jsonl`, one row per main-thread call, written by `helm record`), hand the seat back, newest first and in at most 7,000 characters, the paths that still exist, the verb spellings that worked and the ones that failed with a usage error (marked DON'T), the task and dispatch-row ids it touched with how many calls ago, its background tasks and whether each has an output file (a Monitor has none), its scratch dirs, and the PreCompact handoff nag, which no longer goes to PreCompact stdout (Claude Code gives that to the summarizer as custom instructions). Names only: never command text, flag values, file contents or output, and no env or credential-shaped path (`*.env`, `.env*`, `credentials*`, `*secret*`, `*.pem`, `*.key`, `~/.aws`). A subagent's compaction gets nothing (its `agent_id`, the PreCompact record's agent, or `resumeturn.compacting_thread`); an unproven thread gets the set with one line saying it is the main thread's. Gated on `source == "compact"` inside the verb as well as by the matcher. Fail-open: any fault prints nothing but the nag. `HELM_WORKING_SET=0` turns the set off and still hands over the nag; until this hook is installed (`helm hooks install`, then a restart), `inject` hands a fresh pending nag to the seat on its next prompt, and whichever reader comes first takes it, so it arrives once (task/4070) |
| `Stop` | `helm chat stop-guard --hook-json` (timeout 20, re-derived from measurement — see `hooks.SPECS`) | the idle gate: BLOCKS a stop on undelivered mentions/owner rows (once per pending-fingerprint — never an infinite loop), on claim leases held by the stopping session (except an exact live child or recent active-subagent event bound to the same room/lease/holder incarnation), or on **NO ARMED BEACON** when a launched fleet seat sends/receives canonically owed dispatch work (an unreadable obligation ledger fails closed; a measured zero makes the beacon optional and re-arms the block when work later appears; the block names the exact `Monitor(...)` call), or on a **REVIEW SPIRAL** — one lane review-dispatched at 3+ distinct tips in 12h is round three, which the store forbids by name, so the block quotes the literal `helm chat meld invite <peer> "<lane>: …"` cure (latched per lane+round-count; two rounds warn instead), or on a **PAIR-MELD TURN** — a round of a task's pair meld whose floor is this seat's, owed until it is taken even once its row is delivered (latched per set of owed turns), or on **OWED ROWS** — dispatch rows addressed TO the stopping seat that it owes and is not working, each named with `helm dispatch triage <id>` at every idle turn (a seat leasing other work, or running a Workflow or background agent, hears each owed set once; `stop_hook_active` keeps it from looping; a `dispatch:` lease on a row not proven discharged is the IN PROGRESS mark and refuses nothing until it nears expiry); WARNs when the outgoing count/SHA/proof/landed claim lacks this turn's matching measurement (one transcript snapshot binds the finding and latch identity; typed transcript metadata is ignored, malformed message envelopes surface a latched `CLAIM-EVIDENCE SKIPPED` rather than clean), and separately WARNs on a claim indexed to the PRESENT INSTANT inside text the turn made durable — the chat and dispatch door bodies — posts, DMs, replies, sends, and verdict evidence — which ride in a tool input and are absent from every assistant message, so the four claim shapes above never saw it (a turn can publish a fleet-facing body while emitting no assistant text at all). Publications carry that ONE shape deliberately: the four older shapes fire on more than a third of published bodies, and a present-indexed claim has no honest tool anchor, so the rung asks only whether the body discloses its own measurement instant — undisclosed, >20m old, or ahead of the clock, and to arm the beacon on a clean stop. `stop_hook_active` suppresses blocks but still surfaces inbox + claim-evidence WARNs. Folds no ledger and asks git nothing per lease: the gate/approved exemption, the room advice, the dispatch-claim advice, the owed frontier the beacon and whisper rungs read, and the review rounds all come from the **stop facts** the `helm web` resident writes (see "Stop facts" below); an exemption needs EXACT facts, and STALE or ABSENT facts keep the block and say why. The memory-index cap and the scratch reaper no longer run here — the resident runs them every minute. Kill: `HELM_STOP_GUARD=0`, the 17 per-check `HELM_STOP_GUARD_*=0` switches (the complete register is the ENVIRONMENT.md table: `INBOX/NOACT/CLAIMS/LEASE_TTL/DELEGATION/IN_PROGRESS/BEACON/SPIRAL/PAIR/SEAM/NDP/PUNT/OWED/WIRING/CLAIME/WHISPER/INDEX`), reaper `HELM_SCRATCH_GC=0` |
| `PreToolUse` (`Bash\|Monitor\|Write\|Edit\|NotebookEdit\|Agent\|Workflow`) | `helm chat argv-guard --hook-json` (timeout 2) | the shell-substitution GATE on every Bash call: a chat/dispatch body composed as a double-quoted shell argument lets the SHELL execute backticked content before helm exists — measured three times in two days, once running `git clean` in the shared checkout from inside the message warning about it. Helm cannot see consumed backticks; this hook reads the Bash command BEFORE any shell runs, the one place they are visible. Also covers `git commit`/`tag -m` messages — the same hazard one surface out. **The SIDECHAIN rung, Bash and Monitor:** a call whose payload carries `agent_id` (a subagent) and whose command TEXT names a beacon arm — the word `chat`, the word `wait` and one of `--follow`/`--replace`, anywhere in the folded command (the fold is described under the GitHub-Actions rung below; both deny rungs read the same text) — exits 2. A Monitor armed in a subagent delivers its wake lines to that subagent, not to the seat, so when the subagent ends the seat is deaf while `helm beacons` reads covered; the subagent inherits the seat's `HELM_CHAT_NAME` and session, so no identity door in the beacon process can see this. **It reads PRESENCE, not shell.** It walked the shell once — heredoc openers, delimiter words, wrapper scripts, pipeline stages that own a document — and the walk is what hid a real arm twice: a quoted FAKE opener (`echo "<<'EOF'"`) handed every line below it to a document the shell never opens and the excision cut a real arm standing among them, and a backslash-newline inside a literal document destroyed its terminator and swallowed the arm beneath it. A guard whose miss costs the seat its wake route does not get to be the more precise reader, so every spelling those walks argued about — bare `helm`, any `…/bin/helm`, `python3 -m helm`, `helm chat --room R wait`, a continuation, `sh -c`/`bash -lc` after any shell options, a heredoc of either tag — is one refusal now. A wait with NEITHER flag registers no beacon and is never refused (`--any` room reads, one-shot deliveries), which is the spelling a subagent actually needs. **What it costs:** a MENTION is refused with the act — a post quoting the arm, a grep for it, a non-beacon wait sharing a command with an unrelated `--follow` — and the refusal says so and names the cure: say it without the flag word, or write it with a tool that is not a shell. The words are whole words and the flags whole flags, so `HELM_CHAT_NAME=s1` is not `chat` and `--follow-tags` is not the flag. They SPAN the fold's expansion mark, the way an Actions piece does and for the same reason: the mark stands where an expansion was deleted from inside a word, the hidden text is unknown, and the word bash runs is the word it assembles to — `helm ch$(echo at) wait --follow`, `helm chat wa${x}it --follow` and `helm chat wait --fol$(echo low)` are refusals. They did not span it while the Actions pieces did, and that gap ARMED A REAL BEACON from a subagent (measured): one rung reading the mark and its sibling ignoring it is two readers of one fold. Their EDGES stay their own (`[\w-]`, no dot), so `bin/helm.chat wait --follow` still reads as the word. A mark may FINISH a piece begun in the text and never start one, so a word supplied WHOLE by a runtime value is outside this rung as it is outside its sibling (`helm $V wait --follow` passes), while a substitution that SPELLS the words is refused by the plainest reading (`$(echo helm chat wait) --follow`). A payload without `agent_id` takes the unchanged path. **A heredoc DELIMITER is read by the SUBSTITUTION rung as bash reads one — ONE WORD, then quote removal:** after `<<` (or `<<-`) and any blanks the word runs to unquoted whitespace or an unquoted operator character, honouring quotes and backslash escapes inside it, and the terminator line is that word after quote removal, so `cat <<'DATA'-END` closes on the line `DATA-END`, `cat <<\EOF` on `EOF` and `cat <<"E"OF` on `EOF`; a quote character or a backslash anywhere in the word makes the document LITERAL, so its body substitutes nothing and is data. A `<<` INSIDE A QUOTED WORD opens nothing, because the shell reads no redirection there: `echo "<<'EOF'"` is text. A backslash-newline is removed BEFORE that word is read, as bash removes one before it reads anything. A body whose delimiter line never arrives runs to the END of the command, because that is where bash ends it: the shell warns and reads the rest of its input as the document, and never runs a line of it. That reading now serves only the rungs that ALLOW on what it finds — this substitution gate and the spawn steer; neither deny rung reaches it, which is why a document body is data to one rung and text to the other.  A seat installed before `Monitor` joined the matcher runs no guard on Monitor calls until `helm hooks install` widens its group. **The GITHUB-ACTIONS rung, Bash and Monitor commands plus Write and Edit (task/2566):** the owner rule that CI, builds and releases for local projects run on the local fabric (`fab test --repo <tree> -- <gate>`), never GitHub Actions or any GitHub-hosted runner — the owner does not want to pay for GitHub runners — was a store premise and a whisper, and a seat ran `gh api -X PUT repos/<org>/<repo>/actions/permissions -F enabled=true` with nothing firing. Cheap-to-detect owner rules belong in guards, so this rung refuses (exit 2, one paragraph naming the rule, the premise `ci-runs-on-the-local-fabric-never-github-actions` and the override). **THE REFUSAL SAYS WHEN THE MATCH LANDED IN A DOCUMENT** — twenty characters, `(in a heredoc body)` — because that is the one thing the reader cannot derive: the offset counts in the FOLDED command and the author never typed that text, so a number alone was counted in the raw command instead and the rung was reported as matching `run` inside the word `trunk`, which it cannot, since every piece carries the edge that stops it running into a longer word. Quoting the folded bytes around the match would say more and does not FIT: this line's length is pinned (`PreToolUse/refusal-ci-runner`, tests/test_hook_budgets.py) and raising that number is what the pin exists to refuse, so for a match on the command line the offset is still fold-relative and still has to be re-derived by hand. That is the residual, and it is stated rather than left for the next reader to find. **THE DECISION IS PRESENCE OVER THE FOLDED WHOLE COMMAND.** It had a shell parser once — segments, prefixes, operand roles, heredoc ownership, delimiter words, substitution spans — and nine review rounds each measured one more false ALLOW out of it: an override boundary read one way and spelled another, a backslash or quote inside a token, gh's global options standing past a fixed gap, a heredoc excision that hid a real arm, an Actions API spelling the table had not learned, a path that reached the directory by traversal, a substitution whose interior was quoted, a grant a receiver stage inherited. Every cure was right about its own case and said nothing about the next, because a rule whose answer is never does not need to know which word runs, only whether the text holds the act at all. So the whole decision is: fold the INVOCATION TEXT once — the command less its DATA — and refuse if any protected spelling stands ANYWHERE in it, quoted or not, inside a substitution or not. **THE INVOCATION TEXT (task/2973, the integrator's ruling): an Actions verb is an invocation only where a shell RUNS it.** Three rungs cut with this one data predicate (`chat._command_data`): this one, the owner-posture rung (which reads python's text and stdin as code) and the sidechain authority rung. What a shell hands to a program that runs none of it is cut before the table reads anything: a QUOTED argument holding a blank, and not holding `.github` (a write through a quoted output path costs a runner, a mention costs one refusal), given to `echo`, `printf` (not with `-v`, which assigns its output to a variable a later word can run), a helm verb that posts or records text (`chat`, `task`, `dispatch`, `store`, `asks`, `handoff`, `lr`, `note`, `premise`), a git subcommand that takes a message or a pattern (`commit`, `tag`, `merge`, `stash`, `notes`, `log`, `show`, `diff`, `status`, `blame`, `shortlog`, after `-C`, `--no-pager`, `-P` or a `-c` that sets only `user.name`, `user.email`, `commit.gpgsign`, `core.quotepath` or `color.ui`, none of which names a program git runs; not with `-e`, which hands the message to an editor) or a gh command that is not Actions (`pr`, `issue`, `search`, `release`, `gist`, `label`, `repo`; not with `-O`, `--output`, `-D` or `--dir`, and git not with `--output`, each of which writes a file); EVERY argument of `grep`, `egrep`, `fgrep`, `rg` and `git grep` (not with `rg --pre`, `rg --hostname-bin` or `git grep -O`, which run a program); EVERY argument of `pgrep`, `pkill`, `pidof` and `ps`, which run and write nothing they are given (task/3060); python's `-c` program text, unless it holds a substitution, names `.github`, or starts a process (`subprocess`, `os.system`, `os.popen`, `os.exec*`, `os.spawn*`, `pty.spawn`, `Popen`, `pexpect`, `runpy`) (task/3060); EVERY argument of a `gh api` READ — a GET, because no `-X` or `--method` other than GET or HEAD and no `-f`, `-F`, `--field`, `--raw-field` or `--input` stands in it; and a QUOTED heredoc body read by `cat`, `tee`, `python`, `grep`, `rg` or one of those recording programs; an UNQUOTED body one of those reads, python excepted, is cut too, LESS its `$(…)`, `$((…))` and backtick spans, which run and stay (task/3064) — unless a body line ends in a backslash (bash joins the next line on before it looks for the delimiter, so `E\` then `OF` ends the body at `EOF`), a span does not close or opens a heredoc, or the command runs a file it writes, and then the body is read whole; a `${NAME:=…}` or `${NAME=…}` that assigns a name that picks a program (`PATH`, `EDITOR`, …) reads the whole command whole. The program is read by NAME after the prefixes bash strips (assignments, `env`, `timeout`, `nice`, `nohup`, `command`, `stdbuf`, `setsid`, `sudo`), a `/usr/bin/` or `/bin/` path reading as its name. Each cut word reads as one `_`, and each cut body is gone. **What stays:** every UNQUOTED word given to any program (`python3 x.py workflow <13 words> enable` hands a wrapper the words as argv), and every word after a python script path or `-m`; every argument of gh's Actions nouns, of a gh api write, of a shell, `eval`, `ssh`, `xargs`, `curl` and any program not named above; any word holding a command or process substitution, which runs whoever's argument it is; an UNQUOTED body python, a shell or an unknown program reads, and the live spans of one a sink reads; and a body a SHELL reads — `bash`, `sh`, `zsh`, `dash`, `ksh`, `ssh`, `xargs`, or a data program piped into one — which is its script, so EVERY row in it counts, the three anchorless ones included; and so is the body of a program the reader does not know when a shell, an evaluator or a value-run stands anywhere in the command (`cat <<'EOF' | (sh)`, `while read l; do $l; done <<'EOF'`, `sudo -s <<'EOF'`), and every body beside an evaluator. **It fails closed twice.** A data program's words are cut only while EVERY pipe in the command leads into a filter that runs nothing it reads (`base64`, `cat`, `column`, `cut`, `fold`, `grep`, `head`, `jq`, `nl`, `rg`, `sort`, `tail`, `tee`, `tr`, `uniq`, `wc`) and no process substitution takes output, because `echo '<act>' | bash` and a loop piped into a shell run their data. And the reader follows bash's reading of quotes, escapes, comments, continuations, substitutions and heredocs, and must agree with the heredoc walker about every opener and with bash about every terminator (the walker trims a line before it compares and bash does not); where it cannot — an unclosed quote, a `case` or `coproc`, a heredoc opened inside a quote or inside a `$(…)` within double quotes, an escaped `\<<`, a `$'…\'` quote held open across a line, an indented terminator — or where the command defines a function or an alias, runs `hash` or `enable`, runs a CURRENT-shell evaluator (`eval`, `source`, `.`, `trap`, `mapfile`), which can rebind a name or re-read what a data program recorded (`git commit -m '<act>' && eval "$(git log -1 --format=%s)"`), or runs a program word that is a VALUE (`$c`, `$(…)`), or assigns `PATH`, `LD_PRELOAD`, `LD_LIBRARY_PATH`, `PYTHONPATH`, `PYTHONHOME`, `BASH_ENV` or an editor variable, NOTHING is cut, and an error inside the reader reads the command whole as well, because the hook fails open on an exception. **A quoted body still standing** — its program neither a data program nor a shell — is read as task/2870 set it: an ANCHORLESS row standing ONLY there is not evidence, because the body of a program this rung does not know is more often English than an act, and an ANCHORED row there IS evidence. **A written file that is run is code (task/3430):** within ONE command, a data program whose OUTPUT reaches a file the command runs keeps its words and bodies as code — `echo '…' > x.sh; bash x.sh`, `cat > a.sh <<'EOF'` then `bash a.sh`, `bash -c "$(< x)"`, a copy (`cp`, `mv`) or a rewrite (`cat a > b.sh`, `sed … a > b.sh`: a program that reads a written file and writes one passes it on) then a run, a stdin redirect into a shell, an interpreter or exec (`bash < x`, `exec 3< x`), or into a group, loop, conditional or subshell only where a command inside it runs text (`{ bash; } < x`, a `done < x` whose body evals; `while read -r l; do echo "$l"; done < x` only reads; into any other program, `helm dispatch send < brief.txt`, the file is data), a program run by path, `.`/`source`, a glob or value script operand (`bash *.sh` counts the written files it matches, `bash "$f"` every one), an interpreter's `-c`/`-e` text naming a written file or, for python, importing a written module (`python3 -c 'import x'`, `python3 -m x`), and a program that runs what it is handed or finds (`xargs` feeding a shell, `find -exec`, an `awk` that spawns, `make` with a makefile, a git alias); a shell's `-c` text is read as the commands it is. Only that program: a commit message, a post or a grep pattern whose output reaches no run file stays data, and a written file handed to a program as an ARGUMENT (`bash report.sh hits`, `bash -c 'helm task add x "$(cat f)"'`), `bash -n` and `python3 -m py_compile` run nothing. Python counts as running a file only where its text is not inert, and python holding data text is kept where a string in it names a file the command runs as a path, because `open()` writes with no redirect. A body a SHELL runs is code, where every row counts; one only an interpreter runs is read as a non-inert `python3 - <<'EOF'` body is. Measured over 14,938 real commands from ten days of transcripts: 2 newly refused (both a script really written and run: a python one holding an anchored spelling, a shell one holding `rerun` beside expansions), none newly admitted; the review round after it (F3 narrows, F4 and F5 widen) was not re-measured, because that corpus was not kept. **What the data cut gives up:** a script written by an EARLIER call, or by the Write tool (which this rung has never read), and run by path in this one; a program the list does not name that finds a written file by itself (a git hook, `sed e`) or writes one through its own option or text (`sort -o`, `sed w`); a module a runner loads from its arguments (`python3 -m unittest tests.test_x`); and a file written through a name the text does not settle (`> "$f"`) and run by its literal name. The reading is linear in the command and compiles at most one pattern per call: a PreToolUse hook that times out (2 s) admits. **A spelling standing only in python text that imports helm** stays refused — python that imports helm can reach a denied primitive — and that refusal names the move that works: spell the test string in pieces. The reader trusts a NAME: an alias, a function, or a `PATH` inherited from an earlier call that makes `echo`, `helm` or `git` run its arguments is outside this rung. **Measured, and expiring with the corpus:** over one seat's 212 labelled refusals of this rung (none of them a real Actions act), the trunk the cut was built on refused 141; with the cut it refuses 38 and newly refuses none, and the one row that wrote a workflow file (a Write-tool path) is still refused. The bodies are removed from the RAW lines and BEFORE the fold joins continuations, which is the order that matters — a backslash ending a line inside a literal document is not a continuation to bash, and an excision that ran after the join lost the terminator and swallowed everything below it — and the walker masks quotes before it looks for the `<<`, so `echo "<<'EOF'"` opens nothing and no real command below a FAKE opener is cut away. Those are the two misses the sidechain rung above still carries in its own comment, and they are why this rung reaches that walker and not a parser. **The FOLD**, which is a deletion and not a parse (every step rewrites characters unconditionally and none asks which word owns one): continuations joined; the numeric escapes decoded, because `$'\x67\x68 workflow enable'` runs gh and spells no gh — **with bash's digit counts, not a fixed width**: `\x` takes one or two hex digits, `\u` one to FOUR, `\U` one to EIGHT, `\OOO` one to three octal ones, each greedy, and an octal value above 255 is masked to a byte the way bash masks it (`$'\547\550'` is `gh`, `$'\777'` is `\xff`, `$'\400'` is the empty string, all measured against bash 5.3, and so are `$'\u67\u68'` and `$'\U67\U68'`, which are `gh` too). The introducer letter is case-SENSITIVE, because bash's is (`$'\X67'` is four literal characters there) and because one IGNORECASE flag over the whole alternation let the `u` branch eat `\U`'s first four digits and kill the `U` branch outright; every quote MARK and every backslash deleted — a mark being `'`, `"`, and the two-character ANSI-C and locale marks `$'` and `$"` with the `$` that opens them, because `g\h`, `gh "workflow" enable`, `actions/'permissions'` and `acti$''ons` reach the same act and deleting only the quote of a two-character mark leaves the `$` standing inside the word (`acti$ons`, which spells no row) while the shell hands gh `actions`; whitespace runs collapsed to one space and case folded; and the `./`, `//` and `x/..` segments of every slash-bearing word collapsed, so `.github/x/../workflows` is the directory it resolves to. Three steps disagree with themselves and are therefore read MORE THAN ONE WAY, with a refusal if the readings hold a spelling: a CONTROL escape is a character inside `$'…'` and a deleted backslash everywhere else (`gh work\flow enable` needs the deletion, `$'workflow\nenable'` the decode) — and bash has TWO spellings for one, `\n` with its seven siblings and `\cX`, whose arithmetic is `toupper(c) & 0x1f` (`\cI` a TAB, `\c[` \x1b, `\c0` \x10, `\cx` \x18, and `\c?` the one exception, DEL — all measured against bash 5.3): the second was undecoded, so `eval $'gh workflow\cIenable ci.yml'` folded to one glued word and ran the row, with both protected words standing in the command TEXT. `\c@` is a zero byte and is DROPPED, because the expansion mark below is a NUL and a decode that made one would let a command forge a mark; a path collapse can only REMOVE text, so `.github/workflows/../ISSUE_TEMPLATE/x` still names the directory in the text that was typed; and an UNRESOLVED EXPANSION — `$name`, `${…}`, `$(…)`, a backtick span, nested by counting — is a hole in the text, so one reading deletes every one of them and, where the deleted span stood INSIDE a word, leaves a mark that a row piece may span, because the text removed is unknown and could be the rest of that piece (`repos/o/r/acti${x}ons/permissions` holds the GitHub-owned REST row, and `gh work$(echo flow) enable` holds the workflow row). A QUOTE MARK BESIDE THE EXPANSION IS NOT A WORD BREAK — reading it as one deleted the mark outright and `gh work"$(echo flow)" enable ci.yml` was ALLOWED while bash joined the quoted halves into one word and handed gh the row (measured, eight spellings, the Write door among them) — so the neighbour that decides is the first character on either side that is not part of a quote mark, and WHITESPACE is never read past, because whitespace is bash's word break (`gh "work" "$(echo flow)" enable` is two words of argv and leaves no mark). All three disagreements resolve toward more refusals, the only direction a never rule may resolve them in. **And the readings are scanned JOINED, never one at a time**: a row is a SET of pieces, and pieces of one row stood in different readings while each was scanned alone, so the evidence was split and thrown away (`gh work\flow ci$'\n'enable` holds the noun only where the backslash is deleted and the verb only where the escape is decoded, and was ALLOWED). They are joined on a newline, which no folded reading holds and no piece can span. **The protected spellings are ONE TABLE** in the module, so the deny set is read in one place, and **a row is a SET OF PIECES, all present, in any order and at any distance, each on a span of its own** (a span that reads as two pieces of one row is neither: `r<mark>n` holds both halves of the re-execute row, because the mark may stand for one letter or for three, and a Python raw-string regex holding a backtick, written to a file through a quoted heredoc, was refused as that row twice in one session, task/2855; a real act spelled that way still holds the anchor beside an expansion and the scoped rule refuses it) — the shape the sidechain rung above already uses for `chat` + `wait` + a flag. A row is not a phrase: gh's Actions families are the NOUN and the VERB — `workflow` with `enable`, `disable`, `run`, `view` or `list`, and `run` with `rerun`, `watch`, `cancel` or `download` — and never `gh` plus them as a PHRASE, because gh strips flags before it resolves a subcommand and takes them in TWO gaps: before the noun (`gh -R o/r workflow enable`) and between the noun and the verb (`gh workflow --repo o/r enable ci.yml`, `gh workflow -R o/r enable 1234`, `gh workflow --repo=o/r enable`, all of which gh resolves to `gh workflow enable`). An adjacent-pair table closed the first gap and left the second open, which is the position reading twice; a set of pieces asks about neither. **The re-execute row names the GitHub head as one more piece of its set, `gh` with `run` and `rerun`** (task/3696): `run` and `rerun` are two English words, and a scratch-file state note saying "rerun release.py 0.3.2 dry run" was refused as that row with no gh, no workflow and nothing GitHub's in it. A piece of a set has no position, so both flag gaps stay closed (`gh -R o/r run rerun`, `gh run --repo o/r rerun`), and so is a path in front of gh, its Windows name `gh.exe` (what a seat in WSL or Git Bash types), a wrapper's string, a program's argv list and a body a shell reads. What it gives up: a program handed the two words with no gh WORD in the text passes — a wrapper the rung does not read (`./wrap run rerun 1`, `./gh-retry run rerun 1`), or a head the fold cannot rejoin (a glob, `/usr/bin/g? run rerun 1`, or a printf format piped into a shell, `printf '%sh run rerun 1' g | sh`), where the cancel row, which names no head, still refuses the same spelling. The other rows: the GitHub-owned REST shapes `repos/<o>/<r>/actions`, `orgs/<o>/actions`, `organizations/<id>/actions` (the numeric alias GitHub's pagination links use), `enterprises/<e>/actions` and `repositories/<id>/actions` — an owner slot is any run of non-slash characters, empty where the expansion reading deleted a substitution, and one slot holding an expansion may stand for owner and repository together (`repos/$GITHUB_REPOSITORY/actions`) — plus the separate `.github/actions` composite-action directory; the bare `/actions` segment where the command itself NAMES GitHub (task/3312, the integrator's ruling) — `gh api` with it, because gh talks to no other host (`gh api -X PUT "$P/actions/permissions"`, a gh api GET still cut first), or GitHub's API host `api.github.com`, a GHE `/api/v3` base, or a host under `ghe.com` (GitHub Enterprise Cloud with data residency serves its REST API as `api.<subdomain>.ghe.com`; the piece begins at the typed dot, so `ghe.example.com` and `api.notghe.com` are not it) with it (`API=https://api.github.com/repos/o/r; curl -X PUT "$API/actions/permissions"`), as four rows with NO anchor, because `api.github.com` beside a token variable is an ordinary read and an anchor beside any expansion is a row; they count in every body an anchored row does. **These four are sets too, not phrases about one call:** the segment is refused wherever the WHOLE command also holds `gh` and `api`, or a GitHub API host, so a gh api READ elsewhere on the line (`gh api user --jq .login; git mv src/actions src/handlers`: the read's arguments are cut, the words `gh` and `api` stay), a `gh` command beside a DigitalOcean act whose root variable is spelled `$API` (`gh pr list; curl -X POST "$API/droplets/$ID/actions"`) and a token read of the API host beside a DigitalOcean act (`curl -H "Authorization: token $T" https://api.github.com/user; curl -X POST "$DO/droplets/1/actions"`) are refused, as the trunk before task/3312 refused the same lines (measured on a cross-model read of task/3312); `/dispatches`, the one documented trigger that spells no Actions REST owner path (`repos/<o>/<r>/dispatches` fires wherever a workflow subscribes to `on: repository_dispatch`); and `.github` with `workflows`, as TWO pieces, because a `cd` names the directory across two words (`cd .github && printf 'on: push' > workflows/evil.yml` writes a workflow file and spells no `.github/workflows` anywhere). A piece that could run on into a longer word carries the edge that stops it, so `run` is neither `rerun` nor `runs` and `workflow` is not `workflows`. The client is not read (a `curl` spends on the same runner), the method is read only to find a `gh api` GET, which is data (above), and the operand's role is not read. **What that costs, plainly:** a command that MENTIONS a spelling anywhere but in data is refused too — as an unquoted word to any program, as a quoted argument to a program the reader does not name, in a substitution, or in a body a shell reads. **One mention passes: a READ of the workflow directory (task/2973).** Reading a file runs nothing, so a command whose only row is `.github` + `workflows` passes when every simple command in it is `cat`, `ls`, `grep`, `rg`, `git grep`, `git show`, a `sed -n` print, `head`, `tail`, `cd` or `echo`. It is an allowlist that fails closed: an expansion outside single quotes, a heredoc, a subshell or group, an assignment prefix, a wrapper, any output redirect but into `/dev/null` or onto a descriptor (a `cd` in the same command can stand inside the directory), `sed` with `-i` in any position or a script that is not `[address[,address]]p`, `git` with a global option but `-C`/`--no-pager`, a subcommand but `grep`/`show`, or `-O`/`--output`, and `rg --pre` are each a refusal as before. Writing into the directory — a redirect, `cp`/`mv` into it, `git add`, `tee`, an editor, `sed -i` — and every Actions act stay refused, and a read beside another row is refused with it. It trusts a NAME, as the invocation text does: an alias or function that makes `cat` a writer is outside this rung. A two-piece row costs it wider, because the pieces need not touch: prose saying `the run finished` and `the workflow is local` in one command holds both pieces of a row and is refused. **What it still cannot see. This rung is a superset of the acts it is meant to refuse, not of everything that could perform one, and there are THREE things it does not read** — stated as limits rather than as conservatism, because a guard described as refusing everything unknown is a guard nobody checks. FIRST, it reads ONE command's text, and the shell's working directory is not in it: a `cd` and a relative write inside one command are both text and refused, but a `cd` in an EARLIER call, whose directory the next call inherits, leaves a relative `workflows/x.yml` holding one piece, and that passes. A token supplied WHOLE by a runtime value is the second, and it is NARROWED rather than closed: **an ANCHOR standing beside an unresolved expansion is refused** (the SCOPED rule), where the operator typed MORE of the anchor than its first letter (task/3696): a word that is one letter and then an expansion — `r$(date)`, or a python raw-string regex's `r` before a backtick pair, which a report heredoc was refused for as `rerun` — is the expansion alone. The row rule still reads that letter beside the rest of its row, so `gh run r$(echo erun) 1` is refused and `gh $W r$(echo erun) 1`, whose row is not whole either, passes. A ONE-PIECE row has no other piece to be that evidence, so its piece takes the same reading: `cp a "$D/.$N"` is not the composite-action directory (over this project's own 295,026 distinct commands, all 18 composite-directory refusals left after that cure stood only on a dot and a mark, and none was an act), while `.$X/actions` still is, and a value that supplies the whole path after the dot (`mkdir -p .$X`) passes. `gh workflow $V ci.yml` and `gh $W rerun 123` perform the act with a word the text does not hold, and no reading can hold a piece nobody wrote — so where the command already names the half of the act that means nothing outside GitHub Actions, a hole in it is read as the other half. **The same limit reaches the REST root** (task/3312, accepted by the integrator's ruling). The common condition is an API base from a runtime value in a command that names no GitHub API host literally (not `api.github.com`, a GHE `/api/v3` base or a host under `ghe.com`), and under it four shapes pass, each of them refused on the trunk task/3312 started from (measured on its re-read): a client that is not gh — curl, wget, python — whose WHOLE owner root is the runtime value (`curl -X PUT "$API/actions/permissions"`); gh reached through a runtime WORD with a runtime endpoint (`gh $SUB -X PUT "$P/actions/permissions"`, `$GHBIN api -X PUT "$P/actions/permissions"`), which spell no `gh api` for the rows that name GitHub to find (the whole-token limit above); a root spelled as a brace or a curl glob after the runtime base (`curl -X PUT $H/{repos,x}/o/r/actions/permissions`, `"$H/repo[r-s]/o/r/actions/..."`, `"$H/repos/{o/r,x/y}/actions/..."`), because the fold has no brace or glob reading and bash or curl expands each into the real path; and a root word an expansion supplies WHOLE or BEGINS before a literal owner path (`"$H/$ROOT/o/r/actions/..."`, `"$H/${X}s/o/r/actions/..."`), the whole-token limit, and a mark that may finish a piece but never start one. The first is the text of `curl -X POST "$DO_API/droplets/$ID/actions"`, the DigitalOcean act this rung exists to allow, and no reading tells a value's host apart; the other three are the whole-token, brace and word-start limits meeting that same runtime base. **Outside the table altogether, and unchanged by task/3312**, is an act that spells no protected path at all. Two are known: the check-suite and check-run REREQUEST endpoints (`repos/<o>/<r>/check-suites/<id>/rerequest`, `.../check-runs/<id>/rerequest`) ask GitHub to run checks again with no Actions segment in the path (whether that reruns an Actions workflow was not measured), and an SDK METHOD CALL — Octokit's `rest.actions.*` — names a method and no path. Trunk passed both and still does. **Beside means in ONE REGION**: the executed text (the command less its quoted-tag bodies; an unquoted body substitutes and stays in it) or one quoted body TOGETHER WITH THE LINE THAT OPENS IT (continuation lines included, and shared by both documents when one line opens two). The outer shell never expands a quoted body, so an expansion on another line supplies nothing inside it — `rerun` as prose in a posted document and a later `echo "rc=$?"` are not a row, while `bash <<'EOF'` whose body holds both is — but the program consuming the body stands on the opener line and takes that line's words as its argv and environment prefix, so `node - "$V" <<'EOF'`, `V=$X node - <<'EOF'` and `bash -s workflow <<'EOF'` with `$1` in the body are refused (a `python3 -` body is data and never read). The opener line is read whole, so `echo $? ; node - <<'EOF'` joins its `$?` to the body as well: a refusal, never a miss. What that gives up: a body that reads the missing word from state an EARLIER line left for it (`export V=$X`, or a file written from `$X`, then a later heredoc whose body names the anchor and reads the environment or the file) passes. The scoped refusal names the anchor and its position and no row, because the row a description would name is a guess made by table order and its forty characters do not fit the pinned line. The blanket rule (`refuse every command carrying a $name, ${, $( or a backtick`) is what the scope replaces: `echo $HOME`, `ls $(pwd)` and `fab test --repo $WT -- …` carry no piece and are untouched. **WHICH piece is the ANCHOR**: the noun or path fragment that means nothing outside GitHub Actions — `workflow`, `rerun` (an English word too, so its row also names `gh`), `.github`, the five GitHub-owned Actions REST shapes, `.github/actions`, and `/dispatches`, written as the `+` of each row in the module's one table — and a row marks AT MOST ONE, or none at all. `run`, `list`, `view`, `watch`, `cancel`, `download`, `enable` and `disable` are ordinary English words this fleet types every day and are deliberately NOT anchors, so the rows `run watch`, `run cancel` and `run download` carry no scoped rule and `gh $W enable ci.yml` PASSES. **THOSE SAME THREE ROWS ARE THE THIRD LIMIT**, and it is the whole of what the document rule costs: written inside a QUOTED heredoc whose program this rung does not know (`docker exec -i c sh <<'EOF'` … `EOF`), `gh run cancel|watch|download 123` passes. A body a SHELL reads is not that: `bash <<'EOF'` and `cat <<'EOF' | sh` run their body, and every row there is refused (task/2973). Nothing anchored passes by that rule, no unquoted body is touched by it — it substitutes, so its live spans are still read — and nothing outside a heredoc body changes at all. So does the same shape with the verb that actually SPENDS — a hosted runner is billed by the minute the moment one starts, and that spelling sits in the identical allowed class as the one this sentence used to lead with. Naming only the cheap verb read as a narrower hole than the rung has, so the limit is stated on its costliest member: stated as a limit, not as coverage. The directory row anchors on `.github` and not on `workflows`, on both readings of the word: this fleet's own agents ARE workflows and it says that word in 341 of its commands against 64, and the half of that path that is GitHub's is the dotted directory. **A BASE-RATE THRESHOLD STOOD HERE AND ITS OWN MEASUREMENT REFUTED IT**: before task/3312 replaced the bare segment with GitHub-owned REST shapes, below one in two thousand stood only `/dispatches` (one command in 2,673), `enable` (2,934), that bare segment (10,023) and `download` (30,069) — the pieces task/3312 added (the five REST shapes, `.github/actions` and the four rows that name GitHub) have no transcript rate: it was NOT measured, and no number here describes them — so that rule ALLOWED `gh workflow $V ci.yml` and REFUSED `gh $W enable ci.yml` — exactly backwards from what protects the repository, because the NOUN is what makes a command an Actions command and the verb is an ordinary English word. The rates now say what an anchor COSTS and no longer choose one, and **they expire**, because the corpus is this project's own transcripts and it grows. An expansion standing INSIDE a word is refused by the ROW rule as before — that is what the mark is for, and `gh work$(echo flow) enable` and `.git${h}ub/workflows/x.yml` are refusals — but a mark may only FINISH a piece that has begun in the text, never start one, which is measured rather than tasteful: letting a mark OPEN a piece refused three lines of ordinary prose, every one a backticked code span pluralised by the letter after it (``git worktree``s, where the mark stood for `workflow` and the `s` finished `workflows`). **WHAT THIS RUNG COSTS, measured over the fleet's OWN commands and not estimated, with every baseline named — because a round of this lane called one of them “before the lane” and it was not one:** across 120,275 distinct Bash and Monitor commands this project's session transcripts hold, the rung DOES NOT EXIST before the lane and refuses 0; it refuses **757 now, one command in 159 (0.63%)**, and that is what the whole rung costs the fleet. The ROW rule alone refuses 581 (0.483%). Against the build before the scoped rule (572) this one newly refuses 185 (0.154%) and loses NONE; against the build before this round (603) it newly refuses 176 (0.146%) and gives back 22, every one of them an `enable` or `download` allow this design intends and not one of them a row. For task/3312, the tracked `*.sh` and `*.md` corpus was re-measured on its re-read (the owner slots, `organizations/<id>`, the four rows that name GitHub and the rule that a piece the text holds clearly claims no ambiguous span included), against trunk 436424d9a8c: over trunk's 26,945 non-blank lines trunk's rung had 34 refusals and this one has 33, with no new refusal (over the lane's own 26,964 lines, 33). The only newly allowed line was the old CHANGELOG statement that an ordinary `src/actions/` directory was refused; that statement is rewritten in this lane to describe the scoped rule. **The anchor set was NOT trimmed, and the numbers are why**: a budget of 0.5% newly refused measured from the pre-lane zero cannot bind, because the ROW rule alone spends 0.483% of it and the cheapest anchor that decides anything (`.github`, 22 commands) does not fit the 20 that are left; dropping in cost order reads 651 (0.541%) without `workflow`, 603 (0.501%) without `rerun` too, and 581 (0.483%) without `.github` — the budget reached only by deleting the scoped rule itself. What the anchors cost is those 176 commands — 107 naming `workflow`, 47 `rerun`, 22 `.github` — most of them chat posts and notes that MENTION a workflow beside a `$` or a backtick and perform nothing, which is the price of refusing `gh workflow $V ci.yml`. **The blanket spelling of that rule cost 13,495 and 258** — 12,914 of the commands by the scoped rule alone, ONE COMMAND IN NINE, because the pieces it read as evidence are ordinary English: `run` 6,113, `list` 5,714, `cancel` 387, `view` 252, `watch` 122, `workflows` 120, `workflow` 92, `rerun` 41, `disable` 36, `.github` 21, `enable` 16. Every `helm gate run` and `helm chat read` with a `$VAR` in the line was refused, which is what the anchor bought back. The cure for a deliberate one is the same word at the front; the cure for an accidental one is that this rung was never the only thing standing between the fleet and a paid runner. **The cure is exact: put `HELM_ALLOW_GITHUB_ACTIONS=1` at the FRONT of that one command** (leading whitespace allowed, nothing else in front of it) **and a space, then the command, on that same line** — the word ends where bash ends an assignment word, at a BLANK, and bash's blanks are the space and the TAB only. Everything past that blank is the command, redirections and operators included, so `…=1 gh workflow enable ci.yml < input` is granted whole; but an operator GLUED to the value is not the same word to bash and is not a grant here — `…=1;gh workflow enable` is a bare assignment and THEN a separate command, which does not carry the variable at all, and granting that whole line was a hole. **A NEWLINE IS NOT A BLANK TO BASH, it is that same terminator**, and matching the blank as Python's `\s` (which is ` \t\n\r\f\v`) reopened the hole in the commonest shape there is: `…=1<newline>gh workflow enable ci.yml` ran the act with the variable UNSET in gh's environment (measured; the space-separated spelling of the same command assigned `1`). So the rule is one rule — the word, a blank, and A COMMAND after it on that line — which refuses `…=1 ; gh …`, `…=1 > out<newline>gh …` and `…=1 # note<newline>gh …` as the same bare assignment spelled three more ways. **And `a command` is not one character.** Bash's COMMAND PREFIX stands between the blank and the command word — more assignment words and redirections, any number, any order — so a rule that asked only whether the next CHARACTER could open a command word took the prefix itself for the command and granted the line it stands alone on: `…=1 2>/dev/null<newline>gh workflow enable` and `…=1 X=2<newline>gh workflow enable` ran the act with the name UNSET (measured, `<unset>` in gh's environment), the bare-assignment hole one token further along, and the file descriptor's digit was admitted although the bare `>` beside it was not. The rule WALKS that prefix and asks for a word that is no part of it before the line ends, so `…=1 X=2 gh …`, `…=1 2>/dev/null gh …`, `…=1 <in gh …` and `…=1 >out.txt gh …` — all of which assign exactly 1 (measured) — are grants, and the same prefixes with the command on the NEXT line are not. A BACKSLASH-NEWLINE after the value IS a grant, because bash removes a continuation before it reads anything (`…=1\<newline> gh workflow enable` assigns exactly 1, measured), so the grant is matched against the continuation-joined command, the join the fold does first. A redirection GLUED to the value (`…=1<input gh …`), which bash does assign 1 for, is still refused, because the word did not end at a blank, and the cure is the space. That is the ONLY position that grants: the word anywhere else grants nothing, there is no nested grant, no `bash -c` inheritance and no revocation grammar — a grant read loosely is a hole, and the loose readings this rung shipped grew three. The grant must be a WORD there, so `HELM_ALLOW_GITHUB_ACTIONS=1x`, `…=1-fake` and `XHELM_ALLOW_GITHUB_ACTIONS=1` grant nothing; the ambient environment never counts, because an exported variable would silence the guard for every act that follows; and a command granted at the front is granted whole, including a second act after a `;`. A command whose text holds none of the spellings (`git status`, `cat docs/notes.md`) passes, and so does one that mentions a SINGLE spelling (`gh pr list`, `gh issue view`, `git add .github`, `helm gate run --repo $WT`) — but an ANCHOR and a `$name`, `${…}`, `$(…)` or backtick in the same REGION of one command is the scoped refusal above, which is why `cat <<OUT` + `$(cat .github/CODEOWNERS)` is refused while `cat .github/CODEOWNERS` alone is not, and why `cat <<OUT` + `$(gh release download v1)` is not refused at all. The deny rungs run BEFORE the substitution gate, so a message body holding a spelling and a backtick gets this refusal and not that gate's heredoc cure — a cost the anchor shrank to the acts: while the scoped rule read every piece, the gate's own incident body (`helm chat post "never run `git clean`"`) took the Actions refusal and lost the heredoc cure it exists to hand out, and it has that cure back. And a `Write` or `Edit` whose `file_path` holds one of the same spellings is refused — which is why `Write` and `Edit` are in the matcher. **A file_path goes through the SAME fold and the SAME table**, because two guards for one property disagree sooner or later and these two did: the file door had a private matcher that knew only `.github/workflows` and never folded case, so a Write of `.github/actions/build/action.yml` or `.GITHUB/WORKFLOWS/ci.yml` was admitted while the identical act through Bash was refused. One table costs one thing: a path whose TEXT names the directory is refused even where its `..` leaves it, so `.github/workflows/../ISSUE_TEMPLATE/x` is refused and the cure is to spell the path it resolves to. A Write cannot carry the override word, so the refusal says to write the file through Bash with the word first on that command. Fail-open like every rung: a spelling the table does not know is a miss, never a wedge. A seat installed before `Write` and `Edit` joined the matcher runs no guard on those tools until `helm hooks install` widens its group **The TREE rung, an ADVISORY on all four tools (task/2838):** a lead read another project's commit and wrote a probe file into that project's checkout while that project's own lead was live in its own pane, and nothing in helm noticed. A dispatch-door rung was measured first and would almost never fire, because the harm is a tool call and not a row, so the rung rides the hook every Bash, Monitor, Write and Edit call already passes. It NEVER blocks (exit 0 always; reading an upstream, a reference tree or a fork is ordinary work) and speaks only when ALL of these hold: the command or `file_path` names a path inside a REGISTERED project's checkout (the registry's longest-prefix answer, from the resolver the injection layer already owns, lane rooms at `<repo>-wt` included); that project is not the caller's own, nor a tree the caller's own project contains; and a seat other than the caller is homed in that project's registered checkout, VERIFIED native by the delivery lane's runtime reader, and still seated by the presence beat. The roster carries no role, so that is what a lead IS here: a proxy seat homed in the checkout is somebody's reviewer and a native seat homed in a lane room is a worker, and neither is named. One line per foreign project, naming the project, the freshest such seat and its home room, LATCHED once per (session, foreign project) in the same RAM-side latch the steers use — the same line on every call is wallpaper. **It rides the exit-0 JSON envelope on STDOUT** (`hookSpecificOutput.additionalContext`, `hookEventName` minted as `PreToolUse`), because stderr from a hook that exits 0 reaches the debug log and no agent; `bin/helm-hook` passes the child's stdout through untouched, and every line of one call rides ONE document. THE ARGV STEERS RIDE THE SAME ENVELOPE: the pass-path `[helm steer]` lines (a two-dot lane diff, a typed ledger id, `fab gate` being a wrapper, a pipeline's exit status, `pkill -f` matching its own shell, a live seat of the family being spawned, a full `git clone` of a LOCAL repository into a tmpfs path — shared memory charged to the seat's own swapless cgroup, the copy that froze a seat — which only a command naming `git` and `clone` pays a mount-table read for, and which `--shared` silences) are said through it too, each latched once per session; printed to stderr alone they spent that latch on telling no one. ONE CALL IS HANDED AT MOST `ENVELOPE_BUDGET` CHARACTERS (`helm/chat.py`, pinned as `PreToolUse/envelope` in `tests/test_hook_budgets.py`): the budget is decided BEFORE a latch is spent, so a line that does not fit is neither said nor latched and is said on the next call that trips it, and the first line always rides. The caller is the validated seat name plus its roster row; a caller helm cannot place (no name, no row) hears nothing, and a delegate inherits its seat's identity, which is right because the advisory is about the tree. **What it costs** is the design constraint, since this runs on every tool call in the fleet, so the reads are ordered by price and each can end the rung: first PURE STRING work — a path under a scan root (`HELM_SCAN_ROOTS`, read once in `helm/home.py`) that is not under the payload's own `cwd` or that cwd's lane rooms, and with no such path nothing is read or imported at all; then the ROSTER through `seats_common` alone, where a path no other seat is homed over ends the rung without opening the registry (the arm a reference-tree read ends on; an umbrella seat homed at or above a scan root hosts nothing, or this exit would be unreachable); then a STRICT registry load (no lock, no migration, no write) and the runtime reader for the few calls left. **What it cannot see:** it reads one call's text, so a relative path is not resolved, and the string step trusts the payload's `cwd`, so a session whose cwd already stands inside a foreign checkout reads that checkout as its own room — the `cd` that got it there named the path, which is the call this rung is for. A registered project outside every scan root is never seen. Each miss withholds a hint; none asserts a falsehood. Fail-open and SILENT on an unreadable registry, roster or payload, and every name in the line is inert as written or is not named. **The AGENT-MODEL rung, Agent only:** an Agent call whose `tool_input.model` is a non-empty string exits 2, on every seat and in every project (argv-guard carries no scope). The owner's ruling: the Agent tool's `model` argument never changes the model a subagent runs on; the subagent runs as the launching seat's model whatever the flag says. A flag ignored in silence is the expensive failure, because the caller then routes, budgets and reports as if another model did the work. The refusal names three doors, in this order: drop the flag; the Workflow tool with `agent(prompt, {model})`; a seat launched on that model (`helm launch --seat S --model M`). It points at `helm store get heuristic:fable-via-workflow-not-agent-tool` for the why. It echoes the flag's value cut at 32 characters, with every character outside a model name's alphabet shown as `?`, so no value can carry the line past its budget (`PreToolUse/refusal-agent-model` in `tests/test_hook_budgets.py`). No `model` key, an empty string and a null are admitted unchanged; a value that is not a string is malformed input and is admitted too, the fail-open law every other payload this hook cannot read already follows. **The NESTED-SPAWN rung, Agent only (task/1775):** an Agent call whose payload carries `agent_id` (`actors.sidechain_agent`, the signal every sidechain rung reads) exits 2 after the agent-model rung, naming the bounded-delegate contract: a subagent or Workflow agent does its own slice and spawns nothing, and the review or the next slice is its parent's to spawn, so it finishes and reports to its parent what should run next. It is a deny, not a steer, because the measured case was a bounded writer re-spawning eight to ten nested review agents through its coordinator's stop messages while TaskStop killed only what it re-minted. The payload does not tell a Workflow agent from an Agent-tool subagent (both carry `agent_id`), so an Agent call from inside a Workflow agent is refused the same way; a Workflow's own fan-out is its script's `agent()`, which is no tool call and is untouched, and there is no grant or variable that admits a nested spawn. A missing, empty or non-string `agent_id` is the main thread, as on every sidechain rung. **The FABLE-BUILD steer, Agent only (task/2574):** a main-thread Agent call whose `tool_input.prompt` is build-shaped (a word `claim`, `commit`, `cure`, `implement…`, `arms` or `fab test`) on a seat whose newest turn answered as Fable exits 0 with one `[helm steer]` line in the PreToolUse envelope, latched once per session: the subagent runs as Fable, and the build belongs in an opus Workflow (`agent(prompt, {model: 'opus'})`), with `helm store get fable-is-the-orchestrator-not-the-subagent-opus-two-to-one` for the why. The model is read the way the approval tier reads a native seat's (`native_turn.native_turn_model` on the payload's session, under the transcript root the payload's `transcript_path` names); no transcript, an unreadable one, or any other model says nothing, and so does a prompt that is not a string. The prompt is read first, so a read-only delegation imports nothing. An Agent call meets no other rung: its prompt is prose for a subagent, not a shell command, so the substitution scan, the Bash and Monitor sidechain rungs, the GitHub-Actions rung and the tree rung never read it, and a read-only main-thread call imports nothing and reads no file. A `model` key in any other tool's input is not read at all. **The Workflow tool is not matched.** Claude Code's hook reference (<https://code.claude.com/docs/en/hooks>, "Matcher patterns") says a PreToolUse matcher filters on the tool name, and a matcher made only of letters, digits and `|` is a list of exact names, so this group fires for a tool named `Agent` and for no other. A Workflow call carries its own tool name, and its script spawns agents through its own `agent()`, never through an Agent tool call, so a workflow's per-agent model is never refused here. A workflow agent's own tool calls do fire PreToolUse (the same reference, "Common input fields": hooks run inside subagents and carry `agent_id`), so an Agent call made from inside a workflow agent meets this rung like any other. The rung is seeded from a project lead's standalone `block-agent-model-flag.sh`, which refused the same flag from one home. **The ENV-DUMP rung, Bash and Monitor (task/3037):** a command whose output would carry the environment, the value of a secret-looking variable or a credentials file into the transcript exits 2, because the transcript goes to the model provider. It reads PROGRAMS through the shell reader the GitHub-Actions rung uses, so the words in a quoted string, a commit message, a grep pattern or a heredoc body written to a file are never read as a dump. Refused: `env`, `printenv`, `export`, `set`, `declare -p` and their kin alone or piped into a filter; `export $(…)` or an `export` whose arguments can expand to nothing; the same dumps through `ssh`, `docker exec`, `kubectl exec`, `podman exec`, `bash -c`, `su -c` or a body a shell reads; `docker inspect` without a `--format` that leaves out `Config.Env`; `/proc/<pid>/environ` read by a reader; a reader printing a file named like `.env`, `*.env`, `credentials*`, `*secret*`, `*.pem`, `*.key`, `id_rsa*` or `*token*`; and `printenv NAME` or `echo $NAME` for a NAME with KEY, TOKEN, SECRET, PASS, CRED, PRIVATE, AUTH or MASTER in it, or a database URL or DSN. A print leaks only where it reaches the terminal, so `env VAR=x cmd`, a presence check (`[ -n "$NAME" ] && echo set`, `${#NAME}`), `grep -c`, `wc -l`, a names-only filter (`cut -d= -f1`), a grep of exact names that do not look secret (`tr '\0' '\n' < /proc/PID/environ \| grep '^HELM_CHAT_NAME='`, which a census of the fleet's commands found is most of its reads of a process environment), a redirect to a file and a pipe into the command that needs the value all pass. An output option or operand (`sort -o F`, `iconv -o F`, BSD `base64 -o F`, `uniq IN OUT`, `xxd IN OUT`) names a file the reader WRITES, which it never reads, and a reader writing to a file prints nothing, so `iconv` is a reader now that its `-o` is read that way; `jq --rawfile` and `--slurpfile` READ their file into a variable the program can print. A process substitution is followed both ways: a reader prints what `<(…)` hands it as an operand, a redirect or a here-string (`cat < <(env)`), and the command in `>(…)` writes to this shell's stdout, so a dump run in one (`true >(env)`) or written into one that passes its input on (`env > >(cat)`) is refused, while `env > >(wc -l)` passes. A `$(…)` in an unquoted heredoc body a reader prints and `$(< FILE)` in a printer's argument are read too. The refusal names the shape and its cure, within `PreToolUse/refusal-env-dump` in `tests/test_hook_budgets.py`. It cannot see a script run by its path, an interpreter's own dump, `set -x` tracing a secret, or a sourced file that is not valid shell (its parse errors echo values): the argv-guard never reads file content. **The SHARED-CHECKOUT rung, Bash and Monitor (task/3057):** a working-tree git verb whose EFFECTIVE directory is the shared checkout of a registered repository exits 2, for a seat and a subagent alike. A `git stash pop` meant for a lane once ran in helm's shared checkout and left a conflict marker in a module every helm verb imports, so every verb and hook raised SyntaxError: git's stash is one list for every worktree, and the Bash tool returns a seat to the shared checkout after every call. The effective directory is the payload `cwd`, moved by `cd X &&`/`cd X;`/`pushd` in its scope (a subshell closes the scope; a `cd` inside a pipeline moves nothing) and then by each `git -C X`, resolved in turn; the walk follows `bash -c`, `eval`, `$(…)` and a heredoc a shell reads. The shared checkout is a PRIMARY worktree (`.git` is a directory) with at least one linked lane, in a repository that declares the rail guard profile (`helm.guard.profile=rail`, which is opt-in for a project repository), whose top the registry holds; a repository with no lane, or one that declares no rail, is not one, so a project lead's branch work in its own checkout passes. Refused: `stash pop\|apply\|drop\|branch`, `checkout` and `restore` with any operand, `reset --hard\|--merge\|--keep`, `merge` and `pull` without `--ff-only`, `rebase`, `cherry-pick`, `revert`, `am`, `switch`, and `clean` without `-n`. Everything else passes, and so does any verb when the seat's environment holds `HELM_WORK_INTEGRATOR=1`; text in the command does not count. A directory the text does not settle (`cd "$d"`, `git -C $d`, `cd -`, a `cd` after `\|\|`, `env -C`), a repository chosen by `--git-dir`, `--work-tree` or `GIT_DIR`, and text the shell reader cannot settle pass. The refusal names the checkout and spells the same verb as `git -C <checkout>-wt/<lane> …`, within `PreToolUse/refusal-shared-checkout`. It reads only the directory tree until a primary checkout with lanes is found, then one `git config` read, and the registry last. **The WRITE rung (task/3301), Bash and Monitor for `git init`, and Write, Edit and NotebookEdit for a path:** a file tool whose path resolves inside the shared checkout exits 2, symlinks and `..` resolved first; a gitignored path and `<checkout>/.git/info/exclude` stay writable, and `git check-ignore` that does not answer fails closed. `git init` whose directory is the shared checkout, inside it, or at or inside the lane parent `<checkout>-wt` exits 2. Shell writes (a redirect, `sed -i`, `tee`, `cp`, `mv`) are not parsed; `helm work checkout-watch` reports those. The exemption is `HELM_WORK_INTEGRATOR=1` in the seat's environment; text in the command does not count. **The RECURSIVE-GREP rung, Bash and Monitor (task/3384):** a grep that walks a tree with no bound exits 2, for a seat and a subagent alike. In a seat's shell `grep` is a shell function the harness installs, which runs the harness's own ugrep, and a recursive grep with no path walks the whole working directory: eight orphaned ugrep processes were found on the hub, up to 9.75 days old, two of them near 1h49m of CPU each, and this box has crashed from one before. It reads PROGRAMS through the walk the shared-checkout rung uses (`_commands_run`), so a grep in an `echo`, a commit message, a post, a quoted argument to another program or a heredoc body no shell reads is data, while `bash -c`, `sh -lc`, `eval`, `$(…)` and a heredoc a shell reads are walked; text the reader cannot settle passes, as it does for the shared-checkout rung (a crude reading of it refused 14 lines of emberian/dregg's docs, every one a line bash cannot parse, and found no real one). RECURSIVE, measured from ugrep's `--help` and in a scratch tree against both engines: `grep`, `egrep`, `fgrep`, `ugrep` or `ug` with `-r`, `-R`, `--recursive` or `--dereference-recursive` in any cluster and any position, `-d`/`--directories` `recurse`, GNU's long prefixes (`--rec`, `--der`), and `rgrep`, which is `grep -r`; under the harness grep also `--depth`, `-NUM` (a directory depth there, where GNU reads context lines), `--index`, and a `-g`/`--glob`/`--include`/`--include-dir` glob holding a `/`. A pipe on stdin stops no walk: under ugrep `cmd \| grep -r x` read the pipe AND walked the working directory, and GNU grep -r ignored the pipe. UNBOUNDED: no path operand, or one that resolves (against the payload `cwd` and any `cd` in scope) to `/`, `/home`, `/home/<user>`, `/home/<user>/dev`, the home and its `dev` in any spelling (`~`, `$HOME`, `${HOME}`), `/tmp`, or an org directory directly under `dev` that is not itself a checkout (the last two from the PATH grep shim's own list, which the harness function never reaches), and any directory one level below `/` or below a home (`/usr`, `/proc`, `~/.helm`, `~/.claude`), a lane parent `<checkout>-wt` and every directory between it and a worktree, and helm's own home, global dir and seat store; a trailing glob segment reads as its parent. The refusal names the grep, how it recurses, where it walks, and both routes of the /dev search law — `git grep -n PATTERN -- PATH` inside a repo, `/usr/bin/rg -n PATTERN <narrow path>` outside one — within `PreToolUse/refusal-recursive-grep`. Passes: `git grep`; `rg` with any path; a narrow path (a checkout, a subdirectory, a file, `-`); a grep that is not recursive, every stdin filter included; one given no pattern at all, which both engines refuse with a usage line (measured); and an operand that is an unquoted expansion or a directory the text does not settle. It does not read a grep that `xargs`, `find -exec`, `watch` or a script runs, a body piped into a shell (`cat <<EOF \| bash`), a command on another host, or an abbreviated GNU long option that takes a value; and it refuses a child shell's `grep -3` as the harness function's depth, though a child shell runs GNU grep, where `-3` is context. **The SIDECHAIN AUTHORITY rung, Bash and Monitor (task/3060), the one delegate rung:** a call whose payload carries `agent_id` (a subagent or a Workflow agent) and whose folded command text runs a verdict-class write from `delegate_grant.REFUSED` exits 2 unless a live grant on the payload's session admits it (`helm delegate allow`, VERBS.md): `helm dispatch verdict\|retract\|hold\|release\|cancel\|rebind\|retip`, `helm review done`, `helm lr close\|land\|abandon\|retire\|expired` with close's aliases `discharge`, `withdraw`, `close-landed`, `helm store confirm\|supersede\|revise\|retire\|reject`, `helm handoff write`, and `helm delegate allow\|revoke`, which no grant admits. A delegate inherits its seat's name, session and environment, so every identity door records its write as the seat's, and no declaration clears this rung; a delegated reader once wrote an APPROVE the ledger never takes back. **It reads what the shell RUNS, never what a command mentions (the integrator's ruling):** the beacon rung's fold, as ONE invocation (the word `helm` in any spelling, the group, at most three options, the verb), over the command LESS THE DATA the shell reader proves, cut by the same predicate as the Actions rung's INVOCATION TEXT below: a grep or rg pattern, a pgrep or ps argument, a quoted heredoc body read by cat, tee, python or a recording helm verb, an unquoted one read by any of those but python less its `$(…)` and backtick spans, a quoted echo or printf argument, python's `-c` text, a commit message, a helm post's prose. Everything the reader cannot prove is data stays and counts: a program it does not know (awk, node, a for-list, xargs, ssh, find -exec, `/usr/bin/time`), a stage that could run its input (a pipe into python or sed), a program given as a value, an unquoted body a sink does not read and the substitutions of one it does, a body a shell reads, python text that starts a process (the whole command's python is then code), a file the command writes and also runs (with a shell, node, perl or ruby, or with python where the command names a way to start a process), and text the reader cannot settle (unknown refuses). Reading only the reader's own helm calls would pass `xargs helm ...`, `/usr/bin/time ./bin/helm ...` and `$HELM dispatch verdict`, all measured in the census; cutting only proven data keeps them. A spelling that writes nothing passes, read PER INVOCATION from the shell reader's words (`_commands_run`), because the fold deletes the quote marks and newlines that say which invocation a flag belongs to: a dispatch or lr verb given no arguments (its usage) or `-h`/`--help`, an lr `--dry-run`, `lr expired` and `lr retire --off-frontier` without `--apply`, and a call whose OWN prefix assigns HELM_HOME a literal path resolving to neither the home helm resolves nor `~/.helm`. A HELM_HOME holding another variable or a substitution is not a test home, and neither is one set by an earlier statement: the shell reader does not model shell state, and an `export` read as reaching the call exempted live-ledger writes one spelling at a time (a subshell, a pipeline element, a background job or `bash -c` drops the export; `unset`, `export -n` and env's clearing options undo it), so no export counts. For the same reason any option word after `env` in the prefix (`-`, `-i`, `-u X`, `-uX`, `--unset=X`, `-C`, `-S`), or `sudo`, voids the assignment; refusing `env -u OTHER HELM_HOME=v helm ...` is the accepted cost. The verb passes only when the reader reads as many invocations of it as the fold found and every one writes nothing; text the reader cannot settle lets nothing through. task/1388 built a second delegate rung from the same incident; its verbs and passes are folded into this one. **What it costs:** a mention the reader cannot prove is data is refused like a run (the list above); the cure is the same words without `helm`, or a file written with a tool that is not a shell. The refusal names the parent, the advisory route (`--reviewer-model`/`--reviewer-run`) and the grant, within `PreToolUse/refusal-sidechain-authority`. **The delegate mark** (`helm/pull_delivery.py`): every Bash or Monitor call whose payload carries `agent_id` and whose command RUNS `helm chat read` or `helm chat ack` (`chat.runs_a_seat_read`: helm by any path or `python3 -m helm`, through a wrapper, `bash -c` or a substitution, and text the reader cannot settle that names `chat`) marks its session for 15 minutes, and a `helm chat read` inside the mark delivers nothing and says so on stderr, because a delegate reads as its seat and what it prints reaches only the delegate. A call that only names chat (a path to `helm/chat.py`), or runs helm with `--help` or `-h` (a verb a runtime value supplies included), marks nothing (task/3696); a program a runtime value names (`$H chat read --help`) still marks, since nothing proves it is helm. ACCEPTED: a read through a program the reader does not read as helm (a script, `python3 -c`, `python3 bin/helm`, `/usr/bin/env helm`, `watch`, text piped into a shell) is not marked, and it delivers to the seat. **The BEACON-TIMEOUT rung, Monitor only (task/3404):** a Monitor whose command runs an inbox beacon arm (`helm chat wait` with `--follow` or `--replace`) with a `timeout_ms` under 1800000, or with none, exits 2, and the refusal carries the corrected call, a complete Monitor input: the seat's own command, unchanged, its own `description` (the tool requires one; a call without one gets the fixed `inbox beacon`, task/3435) and `timeout_ms: 1800000`, rendered by `seats_advice.monitor_call`, the one shape `beacon_monitor` renders too, with each value one json literal carrying the characters as typed, never a json escape of them, and the command whole at any width (a cut command, or a name for one, is a call that no longer runs). The harness kills a Monitor at its deadline, 300000 when the call omits it, and a local seat armed 48 of its 152 beacons at 300000 while every helm surface advised the cap (`seats_advice.beacon_monitor`); each lapse is a chance to end a turn unarmed, the class the stop guard refuses once per fresh stop for a local seat (task/3382). It reads what the shell RUNS, token by token, the opposite choice from the SIDECHAIN rung and for the opposite asymmetry: a miss here costs a short lease, today's state, while a false hit refuses a Monitor that is no beacon (presence refused `tail --follow=name /tmp/chat/wait.log`, and a one-shot wait chained to an unrelated `tail --follow`). So a beacon is ONE simple command as the argv-guard's shell reader splits the text (`_commands_run`: pipeline stages, `&&`/`||`/`;` links, and what a substitution, a `-c` string, `eval` or a shell's heredoc runs): helm by any path or `python -m helm` (`_helm_argv`), past assignments, `env` and wrappers, whose first two arguments are `chat` and `wait`, carrying `--follow` or `--replace` as a word of its own. A Monitor that greps a log FOR the beacon's words, or echoes them, passes. A log tail, a gate waiter and a one-shot `helm chat wait` pass at any timeout; a timeout at or over the cap passes without the command being read; every Bash call passes. Everything it cannot read passes: a tool_input that is not an object, a command that is not a string (a WebSocket Monitor has none), a timeout_ms that is not a finite number, and command text the shell reader cannot settle (an unclosed quote, a `case`). It stands after every other rung, so a subagent's arm meets the sidechain refusal first and is never told how to arm one. It needs no install: the matcher already names Monitor. The refusal's fixed text, measured less the command it carries, is pinned (`PreToolUse/refusal-beacon-timeout`). **THE DOORS (task/1135, helm/doors.py):** on the pass path of Bash and Agent, the act a call stands in is read off its argv, the way the CLI dispatcher reads it: `act.helm.<verb>`, `act.helm.<verb>.<sub>`, `act.helm.<verb>.<flag>` per long flag, `act.helm.<verb>.<value>` for `--kind`, and `act.spawn` for an Agent call. A verb inside a quoted body or a heredoc is data. Every store entry with a matching `route:` cell and every `signal: act` reflex with a matching `route:` is said once per context (the steer latch, cleared at a context boundary), at most two per call, each one line of at most 250 bytes worded "you just ran X", after every other steer on the same envelope. A door never refuses. With `HELM_DOOR_SHADOW=1` (the shadow is off by default), a `helm chat post|reply|dm` body is matched against the store's PHRASE probes (never a lone word), one rule, once per context, in SHADOW: the would-fire is a moment-ledger row (route `act.chat.post`, outcome `shadow`) and nothing is said. Each named act route writes one moment row per detection (`delivered`, `in-context`, `deferred` or `silent`), which `helm inject --moment-report` counts, with the door bytes per inject turn and the shadow would-fire rate. **What it costs:** a command that never names helm does not import the door; a helm verb nobody bound reads one small file, `<global>/.state/door-table.json` (the route ids any rule binds, the store and act-reflex directories each project's one index was built from, each cwd's project with the registry mtime it was answered under), and stats the registry, MEASURED at about 1 ms of CPU a call in a fresh hook process on the loaded hub; a bound act also reads the `<global>/.state/door-index-<key>.json` its project's record names, with no store import while the record is younger than 300 s and every directory it lists keeps its mtime (about 1.2-2.2 ms a call in a fresh process, against 10-13 ms when that check imported the store); a rebuild, through inject's parsed-entry cache, costs about 20-27 ms and runs when one of those directories moves, 300 s pass, or `helm reflex add --signal act` marks every index stale; with the shadow switched on, a post reads `door-phrases-<key>.marshal` (the store's phrase probes packed by first word) for 6-11 ms and writes a ledger row and a latch; with it off (the default) a post pays what any unbound helm verb pays. **Reaching the installed homes:** a matcher change reaches a Claude home only when its hooks are rendered again. `helm hooks install` (no `--home`) rewrites every Claude home and every seat config dir, and `--dry` previews it. Until then the old group is not counted: `hooks._matcher_ok` counts a group only when its matcher equals the spec's exactly, so the guard audit in `helm doctor` names argv-guard for that home or seat, and `hooks.stale_specs` names it as an entry install would rewrite, which `helm hooks status` and doctor's drift row report. **The NARROW-GOAL rung, last on the pass path of Bash, Write, Edit, NotebookEdit, Agent and Workflow** (`helm/narrow_goal.py`): the turn's first BUILD ACT (an edit; `helm work claim`, `helm dispatch send`, `helm task add`; an Agent spawn other than Explore, Plan or claude-code-guide; a Workflow; a Bash command that redirects stdout into a file, or runs `tee`/`cp`/`mv`/`install`/`touch`/`ln`/`patch` onto one, `sed -i`/`perl -i`, `git apply` or `systemctl enable|link|edit`; a destination under `/tmp/`, `/dev/` or a scratchpad is not work) exits 2 with one line of at most 250 characters that asks the owner's question, "am I optimizing on too narrow a goal?", and names the line that answers it: `Wider goal: <goal> · for: <what follows> · waits: <bottleneck> · exists: <prior art> · obvious: <expert's first question>`. It asks while the seat is PLANNING, because at report time the narrow thing is already built. It passes free when the turn's visible assistant text already holds a line beginning `Wider goal:` (thinking is not read: claude seats store it as an empty signature), or when the seat is in an open meld (`active` or `invited`) whose room is the task room `<scope>-<N>` of the task the call builds (a `task/N` the call names, or the task recorded for the lane worktree it writes in): a meld is planning before building. ONCE PER TURN: the turn is the recorder's `turn-opened-at` (counters.json, `record.turn_open`), the latch `narrow-goal.turn` in the session's reflex-state dir is written under that dir's lock before the line is printed, so the retry and every later build act in the turn pass, and two parallel build calls refuse one of them. No turn edge, an unwritable latch, a subagent call (`agent_id`), a cwd in another registered project, or any error is no block. The transcript is read backwards in 64 KiB blocks, at most 512 KiB, and a line is parsed only when it names `wider goal` or may be the prompt that began the turn. `HELM_NARROW_GOAL=0` turns the rung off. Workflow joined the matcher for this rung: its script spawns agents with no Agent call. |
| `PreCompact` | `helm handoff check --hook-json` (timeout 5) | the continuity lane: capture the now-snapshot + nag when no handoff artifact exists, so the window that survives compaction never starts blind (see VERBS.md, the handoff contract). The nag never goes to stdout here: Claude Code gives PreCompact stdout to the summarizer as custom instructions, so it goes to the session's nag file, and the `working-set` hook hands it to the seat after the compaction (task/4054); a seat whose settings predate that hook gets it from `inject` on its next prompt, and the kill switch or a builder fault still hands it over alone (task/4070) |
| `SessionEnd` | `helm handoff check --hook-json` (timeout 5) | the same continuity check on the last exit a session gets — the safety net for a session that ends without ever compacting |
| `PreToolUse` (`Bash`) | `fab-suite-pretooluse` (timeout 3) — EXTERNAL, not a `helm …` command | the local-compute GATE on every Bash call: refuses a local test SUITE however python is spelled. A PATH shim (`~/.local/bin/python3`) cannot close this class — an absolute interpreter path like `/home/linuxbrew/.linuxbrew/bin/python3 -m unittest …` never resolves through PATH — so this reads the command STRING before any interpreter runs, the one layer the shim cannot reach. ~10 measured incidents of agent-launched local compute made the owner's daily driver unusable (24 cores at 100%, load 34.96); `helm/roguescan.py` is the box-layer complement that measures what actually EXECUTES |

The Stop command's cooperative budget is **17.5s inside the immutable 20s wrapper**. It follows the recorded 2x observed-sample policy (8.7s × 2, rounded), not the wrapper number. The budget is measured from the WRAPPER's start (`HELM_HOOK_T0`, read from `/proc/uptime` by `bin/helm-hook`), so interpreter startup is charged to it rather than discovered by the outer `timeout`. Before claims or seam starts, Helm requires its fitted cost plus a successor reserve to fit beneath the ambient deadline (the dispatch-ledger rung and its pin are gone: the fold runs in the resident). The 1.25× factor is an **inferred engineering margin**, selected after a fresh seam measured 6.942s—6.8% above the pinned 6.500s maximum—and rounded upward to 0.1s; it is not itself a measured fact. Since the claims and seam rungs read the resident's stop facts (task/3042) both were re-measured on the lane tip — 40 stops, fresh and post-commit, at loads 11.6-13.2 and 21.8-27.8, claims at most 0.140s and seam at most 0.050s, both on the busier run — so claims is admitted at a fitted 0.2s and seam at 0.1s, and claims reserves 2.7s for its successors instead of 8.0s (the seam fat tail that reserve covered is gone). A rung exceeding its fitted cost, or the outer 20s wrapper firing before fallback publication, falsifies the fit and requires remeasurement. An admission miss makes that rung explicitly `COVERAGE UNKNOWN` and still runs the later ladder, so one fat-tail read cannot erase whisper, claim-evidence, mechanical work, or response. **`COVERAGE UNKNOWN` is a WARN and never refuses the stop.** It used to be filed as a block, and that refusal could not be discharged by anything the seat could do: the next stop re-measured the same rung on the same board, overran again, and refused again — measured 2026-09-11, a seam rung taking 11.4s and 12.2s against its fitted 8.7s while every other rung finished under 3s, refusing one seat's stops in a row with no finding in any of them. An unexamined rung is an absence of looking, not a finding; blocks from rungs that DID finish still refuse on their own evidence, and on such a stop the advisory channel is suppressed as it always is beside a block. The current evidence is a provisional 339-run transcript-retention sample from three Claude Code seats; no native Stop timing ledger or non-Claude family sample exists yet. The pinned provenance, replay, falsifier, and re-measurement limitation live in `tests/fixtures/stop-guard-ladder-2026-09-10.txt`.

### Stop facts: what a Stop reads instead of rebuilding the world

Every Stop is a new interpreter on a shared, loaded box, and it used to fold the whole dispatch ledger (p50 7.4s on the live fleet) and ask git eight to twelve questions per held lane. Those facts are the same for every stop until an input moves, so the supervised `helm web` resident computes them — with the guard's own functions, `_gate_pending`, `_room_unfinished`, `_dispatch_advice`, `review_spiral` and `dispatches.owed` — whenever an input moves, and writes them behind itself to `_global/web-cache/stop-facts.json` (helm/stopfacts_resident.py; one writer, under a LOCK_NB lock).

A Stop reads that file (helm/stopfacts.py) and checks, with no subprocess, whether each fact is still exact:

| witness | how | what a mismatch does |
|---|---|---|
| dispatch ledger | one `os.stat`; if it grew, only the appended bytes (at most 64 KB) are read | only the leases and seats those bytes NAME go STALE |
| code | a digest of the package's source stats against the snapshot's | every fact is STALE (the resident runs other code) |
| lane HEAD | the worktree's HEAD file and branch ref against the ones recorded before the resident read the lane | that lease is STALE ("HEAD moved since") |
| trunk | the trunk ref file (or the packed-refs stat) | advice only: landedness is marked as aged |

Each fact is EXACT, STALE (with its age and reason) or ABSENT (no snapshot, older than 600s, or not yet computed). A witness only ever DOWNGRADES. An exemption (LANE IN GATE, APPROVED) comes only from EXACT facts, and its proof (`seats_delegation._gate_pending`) takes a dispatch row as this lane's only when the row was written in the lease's own repository (`repo_id`) and its label or its bound `ref_branch` names the lane; an owed frontier it cannot measure, or a snapshot it cannot index, grants none (task/2387); a STALE or ABSENT lease keeps its block, its line carries the age and reason, and one line names the snapshot's standing. **A reading is never waited for** (owner rule: fail-closed rungs never wait on state). The commonest STALE reading is this seat's own commit a moment before its stop; the lane is unfinished anyway, so the lease is held at once with the line printed for unproven idleness and the snapshot's age, and the resident's next poll is what the next stop reads. `helm doctor` reads the same header (`check_stop_facts`).

**After a land the resident re-execs itself onto the new tree.** Every hook compares the snapshot's code digest with its own, so a land makes every fact STALE ("computed by other code") until facts computed by the new code exist. Nothing else restarts the resident — no path unit, land step or supervisor is relied on — so it does it: once the tree's digest has read the same for one poll (a checkout still writing files is waited out) and the new tree imports in a child interpreter (a tree that does not import is never exec'd onto, and the console keeps serving), it lets any snapshot write in flight finish, releases the writer lock and `os.execv`s its own command line — same interpreter, argv (so port) and environment. The new image marks the facts it finds as being refolded (ABSENT, never exempting) until its first write, so a land costs about one full compute of exemptions. A refused import check or a failed exec is not retried until the tree moves again, and says why in the server's log. `helm doctor` names a resident whose loaded code is older than the tree: a WARN for the first `REEXEC_WITHIN_S` (60s) after the change, a FAIL after it, and a separate FAIL for a resident running another checkout.

**The untested-composition (seam) rung reads the same file.** It used to list every worktree of the repository, walk /proc, read the whole gate-receipt ledger and ask git about every live pair on the stop (1.4-4.8s per stop). The resident's seam leg does that per repository (`stopfacts_resident.compute_seam`: `work._gc.seam_rooms`, `green_receipts`, and `seam_candidates` once per LIVE room), on its own read-behind key so a slow seam refresh never holds back the lease facts, and writes `seam.roots[<repo>]` into the snapshot. The stop finds its repository and its room from the checkout's own files and the recorded registry, adds its leased rooms, and assembles its answer from the per-room rows (`work._gc.seam_assemble`: the rows whose peer is outside its own rooms and not provably its own — an arm pins that this equals the one-call answer). Its witnesses:

| witness | what a mismatch means |
|---|---|
| worktree registry (`<common>/worktrees` stat) | a room was added or removed: STALE |
| each seat's recorded cwd (a digest of the roster's `cwd` fields, not its stat) | attribution moved: STALE |
| the lane-lease projection, re-derived from the claims file | a room gained or lost its lease: STALE |
| the trunk ref, the gate-receipt, import and binding ledger stats, and the HEAD of the stop's own rooms and every live peer | a row may have moved: STALE — asked only when the stop has a room and a live peer, since with no pair there is no row for them to move |

Occupancy (/proc) has no witness a stop could check; the resident re-reads it on its minute refresh and the reading carries its age. The seam rung is advisory about facts it does not have: a STALE or ABSENT reading, or one computed by other code, is one `composition-seam rung has no exact reading` WARN naming why, and never a block. An EXACT reading decides exactly as the rung always did (blocks, latches, the blind-spot disclosure). A room that is neither occupied nor leased is not computed, so a seat standing in one is told UNKNOWN. The resident finishes the legacy import-placement scan the stop used to cut off at 0.25s, so the seat gets the complete answer.

### Act denies and steers on the argv-guard pass path (task/2980)

Guidance that belongs to an ACT is said at the act, and the prompt-lane trigger it replaces is retired in the same change (`helm/actsteer.py`):

- **Denies** (exit 2, one line of at most 300 characters that carries the cure): a `pkill -f`, or a `pgrep -f` that feeds a kill, whose pattern matches its own command text (the kill ends the seat's own turn at exit 144; the refusal prints the bracketed pattern that does not match itself); and a `gh pr|issue create|edit|comment|review|merge|close|reopen` body (from `--body`, a heredoc or `--body-file`), or a `gh api` body on an issues or pulls endpoint (a `body=` field or an `--input` JSON file), that carries an AI authoring line as `helm/trailer_rung.py` reads one (owner canon: nothing on GitHub names a model as its author). Not read, and not claimed: a body gh takes from a pipe, a body the shell computes (`"$(cat f)"`), `gh release --notes` and `gh api graphql`. A command named inside quoted text is data to both denies, so a body that QUOTES `pkill -f` passes.
- **Steers** (one line of at most 250 characters, once per context): `git --stat` piped to a search; a scrub or untrack; a pane send; timing a command under `/usr/bin/timeout`; a session transcript read by hand; a chat claim post with no CL%; a long foreground command on a non-claude seat; an outward push, PR or issue on a repo origin does not own, or a repo made public; a lane claim or a new source module (sweep first); an edit to a shell script a live shell is running; a read-only `pgrep -f` that matched its own shell. A PreToolUse line reaches the model with the call's result, so each is worded for an act that has already run.
- **The narrow-goal question, once per turn** (`helm/narrow_goal.py`): the turn's first build act (an edit; `helm work claim`, `helm dispatch send` or `helm task add`; a non-read-only Agent spawn or a Workflow; a Bash command that writes a file, unit or script outside `/tmp/`, `/dev/` and the scratchpad) is refused with one line of at most 250 characters: "am I optimizing on too narrow a goal?" plus the `Wider goal: <goal> · for: … · waits: … · exists: … · obvious: …` line that answers it. A turn whose visible text already holds a line beginning `Wider goal:` pays nothing, and so does a seat in an open meld for the task the call builds, because the meld is the plan. The question is asked while planning, not at report time, because by then the narrow thing is built. The latch is keyed on the recorder's `turn-opened-at`, so the retry passes; a subagent call, a session with no turn edge, an unwritable latch or any error never blocks. `HELM_NARROW_GOAL=0` turns it off.
- **Once per context:** the SessionStart reset that re-arms inject's pinned lane (`resumeturn`) also calls `chat.forget_steers`, so a steer latched before a compaction or `/clear` may speak once more. A subagent latches apart from its seat (the payload's `agent_id` is part of the latch), so its call never spends the seat's line; the reset clears both. A session that ended has no next boundary, so `helm gc`'s `chat-steer-latches` row reaps the latches of a dead session (the liveness answer `chat-cursors` reads).
- **Retired:** `helm sync` trims the keyword cells listed in `actsteer.MOVED` from their store entries (once, and only while every listed cell is still there), and re-keys the `publication-boundary` and `sweep-before-you-build` reflexes to the `act` signal and `cv-first-lookup` to a narrower pattern (`reflex.REKEYED`). It also retags the entries in `doors.ROUTED` with their door route cells (task/1135). `helm sync` takes no arguments and has no dry run, so each of these is a write to the live store.

  (task/3858) A doors index/rebuild on the argv-guard path (for bound acts) is now live-sized: with HELM_DOOR_SHADOW off (default) it writes only the routes index; the phrase marshal cost is paid only on the shadow path. Old "20-27 ms" claims were pre-gate.

## The per-tool-call hook budget

**Two of these hooks fire on EVERY tool call** — `PreToolUse` argv-guard and the
`PostToolUse` pair — so their cost is not paid per turn, it is paid per Bash
call, per seat, on every project on the box. That is the whole reason this
section exists. Read as a seat this morning, every one of them was printing its
own timeout banner several times a turn:

    [helm argv-guard] THE GUARD TIMED OUT after 2s — this tool call is ALLOWED and UNCHECKED
    [helm posttoolrun] delivery: handler timed out after 2s — event ALLOWED and UNCHECKED

**Both halves of that were real problems and they are different problems.** The
guard really was failing OPEN — the tool call ran unchecked — and delivery
really was being dropped; that is not noise, it is the fail-open law working as
designed and saying so. What was broken was that it said so a dozen times
before anything else could be read, which is how a loud guard becomes an
ignored one.

### Where the time went, measured

Measured on the hub between load 15 and 30 on 8 cores, interleaved so both arms
share one load window (a sequential before/after on this box measures the load,
not the change):

| | wall p50 | CPU p50 |
|---|---|---|
| argv-guard, before | 0.54 s | 0.35 s |
| argv-guard, after | 0.11 s | 0.10 s |
| `python3 -c "import helm.cli"`, before | 0.28 s | 0.26 s |

**The single largest item was not helm's at all.** `python -X importtime`
priced a bare interpreter start on this box at a 488 ms `site` stage, 250 ms of
it `usercustomize` — the machine-local local-suite guard, which imported
`unittest`, `unittest.suite` and `doctest` (and through doctest, `pdb`) at
EVERY interpreter start. With about four hook processes alive at any instant
fleet-wide, that is roughly 2.4 cores spent permanently on interpreter
startups. That guard is not in this repo; it was cured at its source by moving
its doors onto the `sys.meta_path` finder it already used for pytest, so the
wrappers are installed only if a test engine is actually imported. The site
stage measures **14 ms** after, and all seven refusal doors were re-probed
individually against the old file to prove the guard is unchanged.

**helm cannot ship that file, so it MEASURES it.** Half of this cure lives
outside the repository, and every arm in this tree stays green whether or not
it holds — so a rebuilt box would lose the hook budget with nothing saying why.
`helm doctor`'s `check_startup_doors` rung starts a child interpreter and asks
the two questions the file's bytes cannot answer: does a fresh start already
carry `unittest`, `doctest` or `pdb`, and is that same child still refused a
one-case suite. Lazy plus refused is one OK row carrying the measured
site-stage cost; an eager site stage is a WARN naming the cost, the artifact (a
`usercustomize.py` in the interpreter's own per-version user site directory)
and the cure; a measurement that could not be taken is a WARN that says
UNKNOWN. The refusal half is what stops an ABSENT guard reading like a lazy
one — a box rebuilt without the file imports nothing at startup either.

The two items that ARE helm's, both import-time and both cured here:

* `chat.cmd_chat` derived a default ROOM before dispatching any verb, which
  imported `helm.seats` and the fifteen modules behind it — **93 ms of CPU on
  every Bash and Monitor call** for a gate that reads one command string and
  never touches a room (its advisory rungs read a roster only behind a string
  gate of their own). `argv-guard --hook-json` is now answered before that
  prologue. `tests/test_chat_argv_guard.py` binds it structurally rather than
  by a clock: the hook verb must not IMPORT `helm.seats`, with a refusing
  payload as the control that the probe reached the guard at all.
* `cli` imported `registry` (45 ms) and `hooks` imported `configs` (27 ms) at
  module scope, for verbs no hook runs. Both are imported where they are used.
  `wiring.graph` reads function-level imports, so the module graph and the
  reachability law see the same edges they saw before.

**An interpreter flag was measured and REFUSED.** Launching the hook entry with
`-S -E -s` was the obvious next step while `site` cost 488 ms. After the
`usercustomize` cure it buys **7 ms**, and it would move the executable out of
word 3 of the command — which `envtidy._helm_args`, `hooks._executed`,
`posttool.recognize` and `cred._retired_guard_command` all read to recognise
helm's own hooks. Seven milliseconds is not worth four identification parsers.

### One line per hook class per window

The alarm now says three things and stops: which hook, what budget, what the
seat lost.

    [helm argv-guard] TIMED OUT at 2s — this tool call is UNCHECKED
    [helm deliver] TIMED OUT at 2s — pending chat is deferred to the next call

**UNCHECKED survives the trim on purpose.** A silent fail-open is worse than a
loud one and 2026-08-04 is what it costs; the sentence keeps the fact and drops
the rest.

**A timeout carries its likeliest cause.** On 2026-09-29 every seat's hooks
timed out together because `agents.slice` itself was stalled, and each line
said only TIMED OUT. The line now ends with the fleet's stall as the kernel
counted it at the kill (task/3714):

    [helm argv-guard] TIMED OUT at 2s — this tool call is UNCHECKED; fleet stall (agents.slice PSI some avg10): cpu 62.53%, memory 41.00%

or `; fleet stall UNKNOWN: <why>` when it cannot be read. The rc-124 arm of
`bin/helm-hook` reads it with shell builtins only, and the in-process handler
timeout in `helm/hookrun.py` reads the same files (`seatceiling.stall_clause`)
and puts the clause in the outcome's `why` as well. With
`HELM_SEAT_PRESSURE=off` and no `HELM_FLEET_CGROUP` nothing is read and the
clause is absent. `helm pressure-watch` is the watcher that alarms on a
sustained stall.

**A repeat inside the window prints nothing and is counted.** The first timeout
of a class speaks in full; every repeat appends to a counter, and the next line
that speaks carries the arrears — `(+4 suppressed since 18:30Z)` — so the count
is deferred, never dropped. The machine-readable half never moves:
`hookoutcome.declare` runs whether or not this event is the one that says it
out loud, so no consumer of an outcome sees a suppressed failure as an answer.

**BOTH HALVES READ THE COUNTER, and for one release the shell half did not.**
It wrote a byte per repeat and drained nothing, so for the two banners the
owner actually reads — which come from the sh ladder BY CONSTRUCTION, because a
timeout means the helm process that would have printed from Python was already
killed — the count was dropped rather than deferred, while this page and the
module docstring both promised otherwise. Each half now drains the keys it
writes, and `tests/test_hookalarm.py` compares the two renderings byte for byte
against one seeded state.

**The keys are per CLASS and the two halves do not share them**, which is
correct and reads like a bug: the sh ladder keys on the spec name
(`deliver`), `hookrun` on `handler-<name>` and `posttoolrun` on
`posttoolrun-<phase>`. Those are three different failures with three different
sentences — the whole wrapper killed, one in-process handler over budget, one
phase of the composite — and a shared key would let one silence the others.
What has to be shared is the FORMAT, and that is what is tested.

**The since-stamp is measured, not assumed.** The clause used to read "in the
last 10 min" while the drain summed every bucket it could see with no age
limit: seeded six hours back, it still said "in the last 10 min". The oldest
bucket that contributed is in the filename on both sides, so the clause names
it.

**Every failure path of the alarm exits 0 and still prints.** The 124 arm
creates its marker with `printf '' > "$f"` and never with `: > "$f"`: `:` is a
POSIX SPECIAL BUILT-IN, and a redirection error on one makes a non-interactive
shell exit immediately. Measured under `/bin/sh` (dash — what these wrappers
actually run under) with the alarm directory uncreatable, read-only, absent
under an unwritable parent, or a non-directory: **rc 2, silent, on all four**,
where bash printed and exited 0. rc 2 out of a PreToolUse or Stop wrapper is
BLOCK, so a hook that merely TIMED OUT became a refused tool call — or a turn
the agent cannot end — with no sentence naming the hook. An internal failure of
the diagnostic must never become a refusal the owner cannot diagnose. The other
dash-safe spelling, `{ : ; } > "$f"`, is unusable for a different reason:
`hooks._segments_ex` reports `brace group`, callers that abstain DISOWN the
entry, and helm would stop recognising the hooks it wrote across the estate.

`hooks.hookalarm` owns both halves, and it has to own both because **a hook
that timed out is dead**: the process helm would print from has been killed by
its own `timeout`, so that line comes from the `case` ladder in the generated
wrapper, which is POSIX sh with no helm in it. The two agree on files and
nothing else — `<dir>/<hook>.<window>`, where the window is a UTC clock stamp
with its last digit dropped, existence means "already spoke", and size is how
many repeats were swallowed. `tests/test_hookalarm.py` EXECUTES the sh half and
reads it back with the Python half in both directions, because two
implementations agreeing in prose and disagreeing in a path is a failure
nobody would notice: the state splits and every line simply prints twice.

The window is ten minutes rather than the five first asked for, and the shell
is why: five needs an integer division, `$(( … ))` is on helm's own
`_UNMODELLED` list, and generating it would make `_segments_ex` abstain on
every hook command helm writes — and the callers that abstain DISOWN the entry,
so helm would stop recognising its own hooks across the estate. Ten is what
the clock can spell without arithmetic, it is strictly quieter, and the arrears
carry everything the wider window swallows.

`HELM_HOOK_ALARM_DIR` names a private window. A gate-suite child that names no
directory always speaks and writes nothing — a suite is not a fleet, and
durable cross-process suppression there would silence later arms and read as a
missing diagnostic rather than as a leaked channel.

### Presence is not currency

`helm hooks status` and `helm doctor` used to report a whole estate as un-wired
the first time a hook template changed. `_lane_live` matches the installed
command EXACTLY, so every entry helm wrote before the change read as `missing`,
and every count built on `missing` said **`inject coverage 0 of 7`**, **`delivery
lane 0 of 7`**, **`continuity lane 0 of 7`** — about hooks that were firing on
every tool call, on the same screen as a table printing `deliver yes`.

`stale_specs` already drew the line — an `update` action means the entry EXISTS
and would be re-rendered, an `add` means it is absent — and the MISSING *row*
already subtracted it. Only the COUNTS did not. `hooks.carried_names`,
`hooks.missing_names` and `hooks.lane_live` are now the one door the table, the
counts and the rows all read, so they cannot disagree again, and the seven
identical per-home drift lines collapse into one that names the count and the
homes.

Coverage is now PRESENCE. The vintage keeps its own row in `hooks status` and
gained one in `doctor` that it never had — loosening the count without giving
the fact a row of its own would have retired the only place the estate could
learn a re-render is owed, so the loosening and the new row are one change.
STALE and DRIFTED stay opposite failures: STALE means the config names
something this host cannot run, DRIFTED means it names something that runs and
is not the current spelling.

**`scripts/deploy.py` is deliberately NOT the place this heals.** It publishes
one immutable, digest-verified artifact under a lock on the releases directory
and writes nothing outside its own root; teaching it to rewrite settings.json
across every credential home would make a hermetic publisher a fleet mutator.
The estate already self-heals where a session begins: `hooks.preflight` calls
`install_home` on the way in, so a seat that starts a new session re-renders its
own home. What was missing was an honest reading in between, which is what the
above restores.

It does, however, now REFUSE an artifact that would arrive unguarded.
`bin/helm-hook` is the second entry point — every generated hook command execs
it — so it sits in `REQUIRED_FILES` beside `bin/helm` and in the executable-mode
check both the archive and the staged tree pass through. Shipping it absent or
non-executable would make every hook in the estate exit 127 or 126 before its
first line, which the harness reads as ALLOW and nothing in helm's voice can
report; that is a publication the deployer declines to make.

### A blocking lock inside a bounded budget

`PostToolUse delivery: handler timed out after 2s — event ALLOWED and
UNCHECKED` was the owner's live symptom, and the cause was neither the size of
his chat backlog nor the dispatch-ledger walk. **Every delivery timeout
captured from the latency ledger on this host named the same frame**:

    hooklatency.flock < proxywatch.delivery_state_guard < seats_identity._delivery_guard
      < seats_delivery.deliver_any < seats_cli.cmd < chat.cmd_chat

Five consecutive timeouts in one minute, each having waited **1.6-1.9 s of its
2 s budget** for a fleet-wide `LOCK_EX` and then been killed before reading a
single room. The holds are short (`lock-proxywatch` p50 0.2 ms, p95 0.3 ms);
the QUEUE is not, because every seat takes that lock on every tool call.

An unbounded blocking acquisition inside a bounded budget has exactly one
ending. The delivery boundary now waits with a deadline — **half the budget
left on its own `ITIMER_REAL`**, read through `hooklatency.remaining_budget()`,
so the arm and the read cannot drift and no number has to be re-measured when a
budget changes. Half guarantees the delivery keeps at least as much time as the
wait spent. If the deadline passes, the boundary yields `seats_identity.BUSY`,
declares `hookoutcome.SKIPPED`, delivers nothing and changes nothing; the next
tool call retries against the same cursors. Delivery is idempotent and
cursor-driven, so a skip costs one boundary of latency where a kill costs the
whole event plus a line the owner cannot act on.

Measured, interleaved, six pairs, one contending holder, against a copy of the
live 332 MB chat store:

| tree | delivery stage | outcome | owner banner |
|---|---|---|---|
| before | 2001-2034 ms | `timeout` ×6 | `TIMED OUT at 2s … UNCHECKED` |
| after | 1007-1035 ms | `skipped` ×6 | none |

**The bounded wait is OPT-IN and nothing else learned to skip.** `record()`
writing proxywatch state, and the chat writer that replaces it, are not retried
by a next tool call — a silent skip there would lose the write the lock exists
to order. They pass no deadline and block exactly as before.

`hooklatency.flock` takes the deadline from its CALLER and never invents one:
the instrument still replaces no flags and no deadline of its own. A skipped
acquisition records `skipped`, which is a word already in `OUTCOMES` — the
first cut wrote `busy`, `_valid` rejected the row, and the probe read twelve
span STARTs against six ENDs, losing exactly the acquisitions the change exists
to show.

### The measured envelope

Through the shipped `bin/helm` entry, interleaved against the trunk binary,
neutral cwd, `nice -n 19`, on an 8-core box:

| load | argv-guard CPU p50 | CPU p95 | wall p50 | wall p95 |
|---|---|---|---|---|
| ~18 | 85 ms | 112 ms | 86 ms | 112 ms |
| 27-30 | 154 ms | 230 ms | 280 ms | 429 ms |

**argv-guard meets the 150 ms CPU target at load at or below 20 and misses it
at load 27 to 30; the 2 s wall budget is met with margin in both windows** —
at load 30 the wall is still 4.6x inside it. The CPU target is the one that
moves with the box, and it is a target rather than the contract: nothing fails
when it is missed, and the wall budget is what the wrapper enforces. The
mechanism behind the margin is structural rather than a lucky clock: on the
guard path the lane imports NONE of `helm.seats`/`registry`/`configs`/`vcs`
where trunk imported forty such modules.

### The interpreter floor (task/3040)

Every hook is a new process, so every hook pays the cost of STARTING one
before any helm code runs. On the agents box that cost was most of the hook:
`#!/usr/bin/env python3` reaches a bash shim on `PATH`, and the interpreter it
chooses runs a site stage whose machine-local `usercustomize.py` imports
`unittest` and `doctest` eagerly (0.27-0.72 s of CPU per start, measured
before this change).

`bin/helm-hook` now starts helm's own child as `<interpreter> -S -- <checkout>/bin/helm …`:

- **Which interpreter.** The one the shebang path actually ran, recorded by
  helm itself. On a miss the wrapper takes the old shebang path and passes the
  child the `python3` it found on `PATH` (`HELM_HOOK_INTERP_KEY`) and the
  record file (`HELM_HOOK_INTERP_RECORD`). `cli._record_hook_interpreter`
  writes `<that python3> TAB <sys.executable>` to
  `_global/.state/hook-interp` in the helm home. The next hook whose `PATH`
  finds the same `python3` execs the recorded interpreter directly. The
  interpreter is never a hardcoded host path.
- **Why `-S` is safe.** helm is stdlib-only. No import anywhere in `helm/` or
  `bin/helm` (guarded or nested) names a module outside the standard library,
  helm ships no `.pth`, and no helm source calls a builtin that only the site
  stage defines. `tests/test_site_stage_precondition.py` pins all three
  facts.
- **When the old path is kept.** For every state that is not proven: a child
  that is not the `helm` beside the wrapper, a first line that is not exactly
  `#!/usr/bin/env python3`, a child that is not an executable file (126 and
  127 keep their meaning), no `python3` on `PATH`, no record, a recorded
  interpreter that is gone, or a `PATH` python3 newer than the record.
- **What it does not follow.** The record is keyed on the `PATH` python3 file,
  not on what a shim behind it would choose today. If the host changes which
  interpreter the shim picks while the recorded one stays installed, hooks
  keep running the recorded one until it is removed or the shim file changes.
  `helm doctor`'s interpreter-startup rung prints the interpreter hooks run.

One behaviour changes with it, in the direction the code was written for.
`hookalarm._under_a_test_runner` reads "`unittest` is in `sys.modules`" as
"this process is a test runner", and a test runner gets no rate limit on the
Python-side timeout alarm. On a box whose site stage imports `unittest` at
every start, every production hook matched that test, so the alarm printed
every line and never used its one-line-per-window limit. A hook started with
`-S` imports no `unittest`, so the limit now applies as its comment says it
does. The sh-side `124)` arm always had the limit.

The budget had a floor as well. The uutils `timeout` (0.8, the default
`timeout` on a rust-coreutils host) waits for its child in 100 ms polls, so
every hook's wall time rounded up to the next 100 ms. The wrapper uses GNU
`timeout` when the host ships it as `gnutimeout`. The exit codes this file
reads (124, 125, 126, 127 and 128+N) and the process-group kill were measured
identical for both.

Measured on the agents box, interleaved against trunk, hermetic helm home,
from a cwd outside helm, 25 runs per arm at 1-minute load 12.5-16.8 on the
8-CPU agents slice:

| surface | trunk p50 / p90 | this change p50 / p90 |
|---|---|---|
| argv-guard | 207 / 310 ms | 110 / 144 ms |
| deliver | 207 / 309 ms | 101 / 130 ms |
| PostToolUse composite | 207 / 312 ms | 101 / 150 ms |
| stop-guard, non-helm project | 207 / 307 ms | 106 / 149 ms |

`helm doctor`'s interpreter-startup rung now also launches a child through a
copy of the wrapper. An eager site stage that no hook pays is OK. An eager site
stage that hooks still pay is a WARN, and the WARN names the reason.

### The hook resident (task/1825)

The interpreter floor makes a cold start cheaper; it is still a cold start. On
2026-09-30 the fleet stalled twice on CPU: hook processes held 4.5 of the
agents slice's 8.7 cores at about 15 births a second, and a cold argv-guard
still cost about 133 ms of CPU after the box's site stage was fixed. argv-guard
runs on every Bash, Monitor, Write, Edit, NotebookEdit and Agent call of every
seat.

So argv-guard is now answered by a warm process when one is serving:

```
bin/helm-hook (sh)  --served argv, recorded interpreter, resident dir-->
bin/helm-hookres (bash, starts no python)  --TCP 127.0.0.1 + token-->
helm hooks resident: accept, fork; the child checks the call, says
  `ready PROOF`; on the client's `go` it runs helm's own `chat argv-guard`
  as the caller and says `rc N`.
```

- **The answer is the cold answer, by construction.** The child takes the
  calling process's environment (`/proc/PID/environ`), cwd and umask, and its
  stdin, stdout and stderr as the same open files (`pidfd_getfd`). The hook
  reads the harness's payload from the harness's pipe and writes to the
  harness's pipes. Nothing is copied back through the socket, so a pass and a
  refusal are the bytes a cold run writes. tests/test_hookres.py compares
  them, byte for byte, for a pass, a pass that prints a steer, and two
  refusals.
- **Fork per call.** The parent imports helm once and never runs a hook, so
  each call starts from the state a cold interpreter reaches after its
  imports: no cache, latch or memo carries from one call to the next.
- **What a fork cannot change is checked.** A module that reads the
  environment while it imports keeps the resident's value, so the resident
  records every key its modules read while they import and refuses a caller
  whose value differs. The record starts at the first import: the modules
  loaded before the resident runs (helm itself reads `HELM_GATE_LOADS_DIR`)
  are imported again by a fresh interpreter with the recorder installed
  first. Every `PYTHON*` variable, `TZ`, the locale's character set, the
  interpreter and the checkout are always compared.
- **Exactly once, warm or cold, never neither.** Every refusal comes before
  stdin is read. The client's `go` is the commit point: the child holds the
  caller's stdin and stdout from admission, but reads and writes them only
  after it reads `go`. A resident too slow to say `ready` inside
  `HELM_HOOK_RESIDENT_WAIT_S` (0.5 s) can therefore never run the hook beside
  the cold run the client falls back to. The child takes the caller's stderr
  only after `go`, because the client's `go` write moves only its stdout (to
  the socket, for the length of the builtin), so a client stalled there is
  still served. A failure between `go` and the hook is `refuse PROOF WHY`,
  and the client runs the call cold. After the hook starts, a resident that
  hangs up is an UNKNOWN outcome: the client says so and exits 70, which the
  wrapper reports as a failed guard (the call is unchecked), never as a
  pass.
- **It fails open and says so.** Down, gone, stopped, not beating, slow,
  stale or an environment it cannot stand in for: one line on stderr,
  `[helm argv-guard] hook resident <why>; this call runs cold`, then the exact
  cold command the wrapper would have run, under the same budget. Three
  states are silent: no endpoint file (no resident serves this checkout for
  this helm home), an answer without the resident's proof (see Auth), and a
  busy resident (at its limit of calls in flight, or unable to fork), which
  refuses before it reads the token and so without the proof.
- **A resident that cannot accept is never dialled.** Before it connects,
  the client reads, with builtins, the endpoint pid's `/proc/PID/stat` and
  the resident's heartbeat. A pid that is gone (or now names another
  process, by its start time) or stopped (state `T`) runs cold. So does a
  heartbeat that is missing or more than 3 s from now: every poll (once a
  second) writes the epoch second to `<endpoint>.beat`, so a stale beat
  means the serve loop is not running. That covers a resident that is alive
  and reads as running but does not accept (a frozen cgroup, a loop stuck in
  the kernel). Without it, its accept queue fills, each connect blocks until
  the wrapper's budget, and a gate's timeout ALLOWS the call unchecked, on
  every seat at once.
- **Auth, both ways.** TCP on 127.0.0.1 is open to every local user. The
  endpoint file is `0600` in a `0700` directory (`_global/.state/hookres/`
  in the helm home) and holds two secrets. The token proves the caller: a
  request without it is refused as soon as its token field arrives. The
  proof proves the resident: it rides `ready PROOF` and every refusal to a
  caller that sent the token, and never an answer to one that did not. An
  endpoint outlives a resident killed without cleanup, and anyone may then
  listen on its port, so the client compares the proof with a builtin and
  trusts no answer without it: the call runs cold, and none of that
  answer's text is printed. The request names the calling process's pid, and
  the child refuses it unless that process holds this very connection on its
  fd 3 (the kernel's socket table gives the connection's inode), so a token
  holder cannot name another process. One pidfd, opened first, backs every
  read of that process for the rest of the call, so a pid reused after the
  client dies is never read.
- **It follows its code.** It uses the `helm web` follower
  (`stopfacts_resident.Follower`): when the tree's digest moves and settles,
  it removes its endpoint, checks that the new tree imports, and re-execs in
  place. Between a land and that poll, every call compares the checkout's
  HEAD with the HEAD it loaded, so a trunk change is refused at once.
- **Who runs it.** The console `helm web` keeps one alive
  (`hookres.supervise`), started with the interpreter the hooks run. It lives
  in the `helm-web` unit's cgroup and stops with it; a call it has said
  `ready` to ignores the unit's SIGTERM and finishes, so a web restart never
  turns an in-flight call into an unchecked one. `helm hooks resident
  --status` says whether one is serving. `HELM_HOOK_RESIDENT=off` turns off
  both the starting and the asking.
- **It does not start where it cannot take a socket.** Claude Code gives a
  hook sockets for its stdin, stdout and stderr, and only `pidfd_getfd` can
  take a socket from another process. Where the kernel refuses that for a
  process that is not the caller's descendant (`kernel.yama.ptrace_scope` 1
  or more), a resident would refuse every call and only slow it. So it
  probes `pidfd_getfd` against its parent when it starts (a probe against
  itself always passes), and on a refusal it exits (status 4) with one line
  that names the reason, writes no endpoint, and `helm web` does not restart
  it. Every call then runs cold in silence, as on a host with no resident,
  and `helm hooks resident --status` says why.

Measured on the agents box on 2026-09-30, argv-guard through `bin/helm-hook`,
30 calls per arm, arms interleaved, in six rounds at one-minute load 14-36 on
the 8-CPU agents slice. CPU is everything the call costs: the wrapper,
`timeout`, the client or the cold interpreter, and the resident with its
forked children. The last two rounds, at load 15-18, are the quietest box:

| arm | CPU per call | wall p50 / p90 |
|---|---|---|
| cold (`HELM_HOOK_RESIDENT=off`), load 15-18 | 159-200 ms | 153-213 / 266-295 ms |
| resident, load 15-18 | 19-20 ms (client 6-7, resident 13) | 16-17 / 19-22 ms |
| cold, all rounds (load 14-36) | 159-273 ms | 153-440 / 266-693 ms |
| resident, all rounds | 19-32 ms (client 6-10, resident 13-22) | 16-46 / 19-122 ms |

A serving resident also polls its tree once a second (the digest walk), about
0.7 ms of CPU per call at the rate these rounds ran.

After the cure round for the fresh read (the resident's proof, the sender
check, the client's state read before it connects, the first-import record),
measured the same way on the same box on 2026-09-30, 20 calls per arm in two
interleaved rounds at one-minute load about 11: cold 106-127 ms of CPU per
call (wall p50 105-131 ms), resident 19-22 ms (client 7-9 ms, resident
12-13 ms; wall p50 16-27 ms). The checks add no measurable cost per call.

Most of the resident's share is the fork and the copy-on-write faults of its
child (about 4 ms for a fork and exit of the 40 MB parent, and about 8 ms of
faults while the hook runs, against 2.3 ms for the same hook in-process). That
is the price of a fresh process state per call.

**Only argv-guard, deliberately.** The PostToolUse composite (`helm hooks run
PostToolUse --installed`) runs on every tool call too, but the delivery and
seat-identity code under it reads this process's ancestry and session
(`seats_common`, `pull_delivery`, `beacons`), which a forked child would
answer with the resident's. It stays cold until those reads take the caller's
pid. `hookres.SERVED` and the test in `bin/helm-hook` name the served argv,
and tests/test_hookres.py holds the two to each other.

### What is NOT cured

`chat deliver` measures **~0.35 s of CPU** with its imports warm, and that is
its own work — rooms, roster, cursors — not startup. That cost is not what
blew the budget in production, but it is real and unimproved, and a box under
enough load will still spend it.

The delivery store itself is the standing debt. `chat.list_rooms` getdents the
room directory, and that directory holds **90,987 entries of which 445 are
rooms** — measured **238-302 ms per call under load**. Since task/3848 a
delivery pass no longer pays it on every tool call: it reads the listing the
last pass made (`seats_roomscan.room_names`), which is made again only when a
room log is created (`chat.rooms_generation`) or the listing is 30 s old, and
a beacon waiter whose rooms have not moved since it proved them read skips the
pass entirely (`seats_roomscan.QuietRooms`). `chat.state_path` and
`STATE_FAMILIES` exist precisely to move that exhaust into per-family
subdirectories and only the `deleg` family has moved; the cursors and their
locks, which are ~97% of the entries, have not, so every listing that does run
— that shared one, gc, rename, restore — still reads them all.

The proxywatch delivery-state lock is now waited on with a deadline (below),
which stops a queue from consuming the budget — it does not make the queue
shorter. The lock is fleet-wide and taken by every seat on every tool call;
narrowing what it covers, rather than bounding the wait for it, is the fix that
removes the contention instead of surviving it.


**THREE rows are GATES, not lanes: `Stop` (stop-guard), `PreToolUse` (argv-guard) and `PreToolUse` (suite-guard, the external one).** **The ladder below is a SHIPPED FILE, `bin/helm-hook`, and the installed command is the short invocation of it: `<abs>/bin/helm-hook <gate|lane|posttool> <name> <event> <N> <consequence> <abs>/bin/helm …`.** Claude Code echoes a hook's WHOLE command string whenever that hook blocks or errors, so an inline rc-case ladder is owner-facing output on every blocked stop — measured at 2,039 characters for the Stop gate, printed in front of the one sentence the owner needed, and the owner asked twice why the stop hook was so long. The arms, their exit codes and their sentences are UNCHANGED; only their address moved. Five operands always precede the child, because `hooks._executed` steps over exactly that many to find it and a phrase marker claims an entry only from the child's FIRST ARGUMENT — a variable-width head would make helm read every hook it installed as foreign and append a duplicate beside it. **TWO ABSENCES, AND HELM CAN ONLY SPEAK FOR ONE OF THEM.** A missing CHILD is unchanged and still speaks in helm's own voice: `timeout` returns 127, the `127)` arm runs, and the wrapper prints THE GUARD IS MISSING — naming the path and `helm hooks install` — on stderr AND as the exit-0 JSON the harness surfaces. That is the deleted-lane-room case, and it is pinned by an executed arm on both channels. A missing WRAPPER is the new state and helm has no voice in it at all: the shell exits 127 before any line of `bin/helm-hook` runs, so the hook still fails OPEN (non-zero and not 2) but in the harness's words, on neither of helm's channels, and no code inside that process ever runs to say otherwise. **Only a reader that MEASURES the file can report it**, so the ones that matter do, from ONE function (`hooks._wrapper_state`, rendered by `hooks.wrapper_gone_message`): `helm hooks status` prints it for credential homes and for seat config dirs; `doctor`'s inject-coverage rung prints it and no longer counts such a home as covered; `doctor`'s guard-contract rung prints it per config. So a hook that produced rc 127 and no output at all is the WRAPPER missing, not the guard — ask `helm doctor` or `helm hooks status`, never the hook. The nine other hooks never block either — but they no longer do it in SILENCE. They used to be generated with a bare `|| true`, which is the fail-open law written as an idiom: right about the law, and it also rewrote rc 124 and rc 127 to success. A `timeout` KILL prints NOTHING, so a lane hook that never ran was indistinguishable from one that ran clean — the identical failure this section records for the GATES on 2026-08-04, whose cure stopped at the three gates. On 2026-08-27 the fleet spent hours chasing seats that were simply unreachable, and a silently-dead `chat join` is exactly that: no mentions, no DMs, no wake, indistinguishable from idle. Every advisory hook runs the `lane` kind, whose ladder is `timeout N helm …; rc=$?; case "$rc" in 0) ;; 124) <alarm> ;; 127) <alarm> ;; *) <alarm> ;; esac; exit 0` — the gate ladder minus the `2) exit 2` arm, because an advisory spec that propagated 2 would arm every lane hook into a blocker (`hookrun._stronger` holds the same line in-process). **The exit code never moves: it is 0 on every path.** Each alarm names WHAT THE SEAT LOST rather than which verb failed — the sentences live in `hooks._ADVISORY_LOST`, one per spec, and an advisory spec added without one fails its arm rather than shipping a generic line. A gate cannot be: it returns **rc 2 to BLOCK**, and `|| true` rewrites that to 0 — the harness then lets the agent proceed anyway. Measured 2026-07-26, the stop-guard was returning 2 with **72 undelivered messages** while every turn ended cleanly, and the owner's report was exactly right: *"ive never once seen them actually fire to stop you from ending a turn"*. A gate runs the `gate` kind, whose ladder is `timeout N helm …; rc=$?; case "$rc" in 2) exit 2 ;; 0) ;; 124) <alarm "TIMED OUT at Ns"> ;; 127) <alarm "THE GUARD IS MISSING — nothing executable at <path> … repair with: helm hooks install"> ;; *) <alarm "THE GUARD FAILED rc=$rc"> ;; esac; exit 0`, where every alarm ends "— this <stop|tool call> is UNCHECKED" (it read "ALLOWED and UNCHECKED" until the budget section below trimmed it; UNCHECKED is the half that must survive any trim) — it propagates the refusal, and every other outcome still exits 0 so a helm crash fails open. **The arm set is exhaustive on purpose.** The rc-124 leg exists because a `timeout` kill prints NOTHING, so an unchecked stop was indistinguishable from a clean allow. The rc-127 leg exists because "a crash at least leaves a traceback" was FALSE for the case that mattered: a hook whose helm path no longer exists exits **127**, which was neither 2 nor 124 and so fell straight through to `exit 0`. On 2026-08-04 eight settings files named a deleted lane room's `bin/helm`, both gates among them, and four credential homes ran unguarded in silence (see **helm_bin and the shared checkout** below). A guard that could not run is a strictly worse state than one that timed out, and only the timeout was reported. The `*)` default means the next unhandled code is loud by construction rather than after the next outage. Fail-open is right for a lane and fatal for a gate. The hook entry also gives Claude's outer runner `N+5` seconds: without that explicit grace its default five-second deadline can kill the shell before rc 124 emits the alarm, recreating an allowed unchecked stop outside Helm's wrapper. This does not widen the checked verdict budget; the inner `timeout N` remains authoritative.

**AN ALARM IS TWO CHANNELS, AND STDERR IS NOT THE ONE THAT COUNTS.** Every arm above exits 0 by the fail-open law, and the harness contract is explicit about what that means: *"Stderr from a hook that exits 0 goes to the debug log only, never the transcript, and Claude never sees it."* So from 2026-08-04 until this was fixed, all three warnings — including the ones written to cure that very outage — were announced to nobody. @codex found it on `lane/helm-bin-derives-from-file`, and the arms that were supposed to prove visibility could not: they captured the SUBPROCESS's stderr, which proves the text was written, not that anyone can read it. `_gate_alarm` now emits the same sentence on **both** channels — stderr for the debug log, and JSON on stdout, which is *"only processed on exit 0"* and is therefore available exactly when a gate fails open. `systemMessage` reaches the OWNER; `hookSpecificOutput.additionalContext` reaches CLAUDE, and both gate events carry it (Stop renders at the end of the turn, PreToolUse next to the tool result). **The exit code does not move** — that is the whole point: the guard still fails open, it just stops doing it in silence. Two rules for anyone editing this: the sentence is written ONCE and rendered to both channels (two hand-kept copies is how a hardcoded "this stop" ended up in front of a PreToolUse reader), and rc 0 / rc 2 emit NOTHING on stdout — a pass must stay quiet, and on exit 2 the harness IGNORES stdout and reads stderr instead, so emitting there would drop the refusal's reason entirely.

**A REFUSAL IS COUNTED, AND THE REFUSAL DOES NOT WAIT FOR THE COUNT.** On the `2)` arm the gate kind starts `<abs>/bin/helm friction record <name> --reason <event>` — the helm beside the wrapper, so the external gate is counted too — DETACHED, with stdin, stdout and stderr on `/dev/null`, and exits 2 at once. One line reaches the friction ledger carrying the gate's name and the event, never the payload, which the wrapper does not read. It is detached because a gate that overran the harness deadline while bookkeeping would be read as a hook error, and a hook error ALLOWS. A `dispatch-*` entry is not counted here: the in-process dispatcher counts its own refusal under the handler's name. `helm friction` reads the ledger.

**helm_bin and the shared checkout.** The absolute path baked into every generated hook is the **shared checkout's** `bin/helm`, resolved through `work._lanes.find_root` (git's `--git-common-dir`), never `dirname(dirname(__file__))`. A seat running a hook-touching verb from its lane room would otherwise write that room's path into every credential home and every seat config — `_merge_event` is the one place that reaches all of them — and a lane room is deleted when its lane lands. Two independent rails hold this: `hooks.helm_bin()` **raises** rather than return a room path, and `hooks.refuse_lane_room_commands()` runs on the merged candidate **immediately before any write**, so a caller that composes its own command text (`record.py` does) still cannot persist one. The write guard refuses rather than repairs — silently rewriting an entry the merge law does not own would be a clobber — and it names the file and the offending path.

The two continuity entries OMIT a matcher on purpose: they must fire on EVERY
compaction and EVERY session end, never gated to one trigger. `helm handoff
check --hook-json` is FAIL-OPEN TOTAL — rc 0 always, silent when the contract
is satisfied, and it captures `_global/now.md` on the same trigger.

**Seats are part of the full estate.** A full `install` (no `--home` filter)
wires `hooks.SEAT_SPECS`, the same complete contract as `hooks.SPECS`, into
every multimodel seat's isolated `CLAUDE_CONFIG_DIR`
(`<helm_home>/_global/seats/<family>/claude`). That includes per-turn inject,
the delivery and gate hooks, both `handoff check` producers, and resume-turn.
The shared contract matters compositionally: resume-turn consumes the seat's
own handoff artifact, so installing that consumer without PreCompact and
SessionEnd producers leaves recovery universally armed but unfed. A launched
codex/kimi/… seat receives `@<family>` and owner posts under its family name
(`seat launch` exports `HELM_CHAT_NAME=<family>`), cannot idle past NEW inbox
rows, and writes the continuity artifact its next compacted window reads. See
the `stop-guard` contract in VERBS for the inbox bound: it blocks once per
pending-fingerprint, so a re-stop on the SAME rows passes and a seat CAN idle
past a static inbox it has already been shown once. `helm hooks status` prints
a `seats (full hook contract)` block with inject and paired-handoff columns
plus `seat hooks: N of M seats`; `helm hooks install` reports `N of M seats
covered (full hook contract)`.

Same laws for every entry: MERGE-preserving (existing hooks — `helm record`'s
PostToolUse/PostToolUseFailure legs, a seat's foreign hooks — and settings keys
are never clobbered), idempotent (re-install reports `ok`), on configs.py's
safety rails. Every mutation now uses **content-revision compare-and-swap**:
read exact bytes + opaque revision, re-derive the full domain merge from that
snapshot, write only with `expected_revision`, then re-read and verify the exact
committed revision. A pre-commit conflict or a foreign write immediately after
Helm's commit is preserved and retried from its newer bytes, at most three
attempts. Three conflicts refuse loudly; non-conflict storage/shape failures do
not retry. Backups remain recovery artifacts, but Helm never unconditionally
restores one over a revision another writer may have produced. Semantic no-ops
preserve the file's exact formatting.

`--home NAME` narrows to one home (and skips seats); `helm doctor` reports
`inject coverage: N of M claude homes` so a gap in HELM'S OWN ESTATE can't hide
— and that is the whole of the claim. Orca's `orca agent hooks on|off|status`
edits the same `settings.json` with overlapping UserPromptSubmit/Stop/PostToolUse
entries. A Helm-only advisory lock cannot bind that external writer, so locking
is not the correctness mechanism; exact-revision CAS is. A later intentional
Orca edit can still change the estate after Helm returns, so a green coverage
number is a read of the current file, not a perpetual ownership claim.

`helm hooks status` reports currency as well as presence. Each readable home is
compared with the same canonical hook-spec merge used by install and verify;
installed entries that would be rewritten print `OUT-OF-DATE` instead of also
being labeled missing, while an unreadable or broken-link settings file keeps
the established missing-contract alarm and also prints `currency UNKNOWN`, never
clean. A genuinely absent settings file is known-empty rather than unreadable.
This comparison covers resolved hook specs only —
unrelated estate defaults and lane-room repair
are not hook-rendering drift. The explicitly inject-only coverage count falls
only when inject itself is out of date; drift in another guard remains visible
without changing that narrower number. Status also adds per-home
`deliver`/`join`/`stop`/`handoff`/`resume` columns plus the seat block.

**Scope de-duplication is cross-file.** Claude loads user and project settings
together, so an owned hook in both `<CLAUDE_CONFIG_DIR>/settings.json` and
`<project>/.claude/settings.json` fires twice even though each file is internally
clean. `hooks status`, `hooks install`, and `doctor` scan the current checkout and
every identity-verified live seat's checkout for this pair. A confirmed pair is
printed as `cross-scope DUPLICATE`; unreadable roster/settings evidence is
`UNKNOWN`, never clean. Helm reports but does not delete either entry: a project
hook may be deliberate, and choosing which authored scope to remove is a judgment
rather than an installer merge. The scan is bounded by current/live projects; it
does not walk the historical project registry.

The generated command pipes the hook's FULL JSON to `helm inject --hook-json`,
which extracts the prompt, derives `--project` from the hook's `cwd` (longest
registry-path prefix; global-only when no project claims it), and stamps the
`session_id` onto the fire-ledger row. New rows are schema v2 and carry an exact
UTF-8 sample: normal text-mode stdout bytes (including its terminal newline when
non-empty) plus each lane payload. Their point-in-time context records only
authoritative evidence (`session_id`, absolute hook `cwd`, runtime-stamped
harness, and an explicitly set harness config-home environment variable). An absent config-home variable stays unknown; it is never
turned into `~/.claude`. Historical v1 rows remain readable as approximate Unicode
character counts (`len(str)`, with join separators absent), and are never relabelled
as exact bytes.

The fire ledger uses a durable per-attempt spool. A stable, data-free
`inject-ledger.jsonl.lock` is never replaced or unlinked. Before Helm constructs,
mutates, or returns an injection, it holds the shared lock and durably writes
`inject-ledger.jsonl.queue/<attempt>.intent`; inability to open/lock or sync that
admission suppresses Helm output while the hook still returns rc 0. Seen-state,
reflex, coinage, council, and greeting latch mutations are staged while that
shared lock is held; none is applied unless the completed row first becomes an
immutable, file-and-directory-synced `<attempt>.ready` envelope. Only then is the
shared lock released and the staged plan applied. Exclusive recovery renames READY to a plain
`inflight.<generation>` file, appends with `O_APPEND` plus a write loop and fsync,
then plants a durable per-attempt COMMITTED tombstone before cleanup. The
tombstone stays until every other artifact for that attempt is durably gone, so
failed cleanup and later rotations cannot replay an already appended row.
Intent-only attempts become generation-bound UNKNOWN witnesses;
append/flush/close failures retain inflight evidence for a later writer, and an
inflight attempt never ages into a complete census. Current and `.1` carry ignored
protocol/generation headers, rotation still uses the 5MB threshold with one `.1`
and no `.2`, and legacy raw rows remain dual-format input. The reader captures
both generation fds/sizes/ids and the complete queue census under one exclusive
lock boundary, then preads exactly those bytes. Completeness is tri-state: exact,
known incomplete, or unavailable; missing protocol markers, migration history,
malformed/pending/live-generation evidence never become false zeroes. The outer
hook keeps `timeout` and its exit-0 contract, so missing or failing Helm still injects
nothing instead of blocking.

`helm hooks install --harness codex` reports honestly: the codex notify-hook
recipe below is not yet mechanical, so codex stays hand-wired for now.

### Project scope: `helm hooks install --project DIR`

Not every operator wants the physics machine-wide. `--project` installs the
same full spec set into `DIR/.claude/settings.local.json` instead of any
claude home: sessions launched in that project get inject, the delivery lane,
the stop-guard and the handoff contract; every other session on the machine
stays hook-free. Same CAS pipeline, same merge-preservation laws, same beacon
permits — with one deliberate delta: the scalar estate defaults are **not**
seeded, because a project file is neither a home nor a seat and helm does not
own a project's other settings.

`settings.local.json`, deliberately: the generated commands carry this
machine's absolute helm path, so the file is per-machine and must never ride
a commit into someone else's checkout. Add `.claude/settings.local.json` to
the project's `.gitignore` — the installer reminds you, and never edits a
repo's ignore file itself. Only *new* sessions pick the hooks up (the harness
snapshots hook config at session start). `helm hooks status` reports
project-scoped wiring in its project-scopes lines, and flags a spec loaded at
both project and active-home scope as a cross-scope duplicate.

## The contract

`helm hooks status` reports SA initial-context coverage on its OWN line (`SA initial-context hook INSTALLED for N of M seats (delivery unobserved)`) and in an `sa-ctx` column, separate from `inject`. THE TWO ARE DIFFERENT POPULATIONS: `inject` covers a TLA's per-turn physics and says nothing about the subagents that seat spawns. The figure counts CONFIG, NOT DELIVERY — it is not a claim that every subagent receives physics, because the harness limits above are not observable from a settings file. It IS part of the full-contract total, since a seat missing this hook is not fully covered.

```console
$ echo "<the user's prompt text>" | helm inject [--project <name>]
PREMISE naming-extremes: metaphors live at the extremes only ...
TERM drain: routing raw memory intake to typed homes ...
REFLEX: checkpoint the green slice
```

stdout is the injection payload (may be empty). `--json` returns
`{"pinned": [...], "jit": [...], "reflex": [...]}` for hooks that want
structure. `--hook-json` reads the harness hook's full JSON on stdin instead
(`prompt` / `cwd` / `session_id`, unknown keys tolerated; malformed JSON
injects nothing, rc 0). Without `--hook-json`, resolve `--project` from your
registry name (`helm projects`) yourself.

## Manual recipes (the fallback appendix)

### Claude Code

What `helm hooks install` writes, if you'd rather wire it by hand in one scope
(user **or** project `settings.json`, never both for the same event):

```json
{
  "hooks": {
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "timeout 10 /path/to/helm/bin/helm inject --hook-json || true"
      }]
    }]
  }
}
```

Hook stdout becomes `additionalContext` automatically. The prompt-only
variant (`jq -r .prompt | helm inject --project myproject`) still works but
loses the cwd-derived project scope and the session on the ledger.

### Codex

`UserPromptSubmit`-equivalent notify hook: run `helm inject` with the prompt
text on stdin; return the output via `hookSpecificOutput.additionalContext`.
Keep it sparse — codex renders injected context as a visible developer
message.

### OpenCode

An `@opencode-ai/plugin` with `experimental.chat.system.transform`: shell out
to `helm inject`, append the lines to `output.system`. Feature-check the
experimental namespace and fail open (empty) if it moved.

### Hermes

A `pre_llm_call` plugin hook returning `{'context': <helm inject output>}` —
appended to the current turn, preserving the cached system-prefix.

## Rules of the road

- **Fail open.** A hook that cannot run helm must inject nothing, never block
  the turn. The installer generates the rc-case wrapper (a `timeout`
  guard plus an arm per outcome, always `exit 0`); a hand-wired hook
  may still use a bare `|| true`, which never blocks but also never
  reports a helm that could not run.
- **Budget is helm's job.** The pinned lane is byte-capped and JIT is capped
  at 4 entries; hooks should not add their own truncation.
- **Per-project scoping.** `--hook-json` derives it from the turn's cwd; in
  manual wiring pass `--project` when the session's cwd maps to a registry
  project. Global entries fire everywhere, project entries only where they
  belong.
