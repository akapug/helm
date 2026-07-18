# sesh → helm FULL-PARITY matrix (supervisor: meld-maintainer, 2026-07-18)

Owner mandate (David, /afk all day): "make sure all intended UX and AX targets get FULL PARITY
in the merge." Nothing is "done" until every row is ✓ (present + depth-verified against sesh's
:7402 / sesh CLI). This is the supervisor's checklist — I verify each as Fable's slices land.

Status key: ✓ done+verified · ~ partial/present-verify-depth · ✗ missing · ? needs check

## UX targets (sesh web UI views/features — source: sesh/server/ui.html @ :7402)
| # | sesh UX target | what it is | helm status | notes |
|---|---|---|---|---|
| U1 | quota chart | multi-line quota-over-time per account, remaining % | ~ | slice A landed; VERIFY depth (all lines/accounts) |
| U2 | quota: projection-to-reset | dashed projection lines + reset ETAs | ? | verify present in helm quota |
| U3 | quota: weekly/session toggle | 5h vs 7d views, per-model (fable/codex-spark) | ? | verify the toggles + per-model columns |
| U4 | account/battery table | per-account session% + weekly% + home + battery | ? | verify the table under the chart |
| U5 | burn ↔ sessions | temporal join: what was active when quota burned | ~ | helm page shows 'burn' refs; verify the join works |
| U6 | sessions view | full session list, click-to-resume, harness filter | ~ | helm has 'sessions'; verify resume + filters + depth |
| U7 | resume (one-paste) | copy the resume command for a session | ? | sesh's core AX-in-UX; verify |
| U8 | configs tab | list/show config files across cascade | ~ | verify in web (CLI verified ✓) |
| U9 | config-cascade EDITOR | inspect+edit layered config in-browser (backup→validate→atomic) | ✗ | slice C; the highest-value editor UI |
| U10 | homes view | credential-home list + status | ~ | helm page shows 'homes'; verify depth |
| U11 | homes hygiene | "N homes not reporting", "home issue", waste-risk warnings | ~ | helm page shows 'hygiene'; verify the warnings compute |
| U12 | team tray | seats, per-seat account alloc, dup-account warning, presets | ✗ | slice B (Fable's plan); big AX/UX surface |
| U13 | skills enable/disable/delete | (helm ADDED this) | ✓ | already wired (helm's own) |
| U14 | dark/light theme | prefers-color-scheme + toggle | ~ | verify parity |

## AX targets (sesh CLI verbs — source: sesh/server/cli.py)
| # | sesh verb | what it does | helm equivalent | status |
|---|---|---|---|---|
| A1 | configs / configs-show | config cascade inspect/edit | helm configs | ✓ verified (safety law intact) |
| A2 | doctor | health check | helm doctor | ✓ |
| A3 | home | resolve home root | helm home | ✓ |
| A4 | keepalive | session keepalive | helm keepalive | ~ verify |
| A5 | ls | list sessions | helm sessions | ~ verify parity |
| A6 | show | print a session | helm show | ~ verify (helm show is project; sesh show is session?) |
| A7 | mv | re-home a session's cwd | helm rehome | ~ verify |
| A8 | prune | prune sessions | helm prune | ~ verify |
| A9 | serve | run the web server | helm web | ✓ |
| A10 | history | session history | helm transcript? | ? verify or MISSING |
| A11 | creds | credential ops | helm homes? (or MISSING named verb) | ✗? verify |
| A12 | next | account selection by headroom | ??? | ✗? the tokaware-lineage headroom picker |
| A13 | swap | cred rollover/swap | ??? | ✗? verify |
| A14 | physics | the sesh physics/quota compute | helm ??? (backend?) | ? verify |
| A15 | capsule | catalog row × git × mv × cmd (session capsule) | ??? | ✗? verify |
| A16 | cmd | run a command in a session context | ??? | ✗? verify |

## Candidate GAPS to drive to ✓ (flag to Fable)
- UX: U9 config-editor-web, U12 team-tray (both building per Fable's slices B/C) + depth-verify
  U1-U8, U10-U11.
- AX: A10 history, A11 creds, A12 next (headroom picker — tokaware lineage!), A13 swap,
  A14 physics, A15 capsule, A16 cmd — verify each has a helm equivalent or is intentionally
  dropped (with reason). "Everything absorbed" = these need homes or explicit N/A.

*Supervisor updates this as slices land; feeds gaps to Fable. All-green = merge parity done.*
