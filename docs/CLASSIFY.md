# helm classify — the local classifier stream

`helm classify` is helm's thin, **optional** client for a classifier stream
that runs on the operator's own LAN: one small HTTP service in front of local
model servers that answers "which of these labels fits this text", with a
probability for every label and an honest "unsure". helm never needs it. Every
consumer falls back to the deterministic rule it had before, so a host without
the stream loses nothing.

Source: `helm/classify.py`. Consumers: the private-name pre-commit rung
(`helm/private_names.py`) and the per-turn injection trim, in shadow.

## The contract

| call | what it does |
|---|---|
| `POST /v1/classify` | in: `{"text", "labels", "task"?, "prefix"?, "backend"?}`; out: `{"label", "forced", "said", "scores", "mass", "top", "backend", "ms"}` |
| `GET /health` | each backend's health and its running and waiting requests |
| `GET /v1/contract` | the contract as JSON, including the text limit |

- `labels` is 2 to 16 ids (`[A-Za-z0-9_.-]`, up to 40 characters) mapped to a
  definition, or a plain list of ids.
- `label` is the model's own answer when that answer is a label, else
  `"unsure"`, and `forced` is the label a grammar-constrained second call
  chose. `scores` are the first-token probabilities over the labels.
- Errors: **400** for bad input, **413** for a text over the limit (60,000
  characters; the stream never cuts a text silently), **503** `{"tried": [...]}`
  when no backend answers.
- Measured: about 130 ms per call on average. Advised threshold **0.75**: below
  it, a consumer defers to its deterministic rule. A confidently wrong answer
  (0.98) exists, so the rule wins above the threshold too.

## The backend is a setting

Which classifier answers is a host setting, so the same helm works on any
install: `HELM_CLASSIFY_BACKEND`, else the `backend` key of
`<helm home>/_global/classify.json` (read on every call):

| backend | what answers |
|---|---|
| `none` | nothing. **A fresh install's default**: no call is made and no socket is opened; every answer is `unconfigured` |
| `openai-compatible` | any server that speaks the contract above at a base URL, such as the operator's own stream |
| `jev` | TypeSafe AI's Jev evaluator through the operator's own AI gateway |

```json
{"backend": "openai-compatible", "url": "http://classify-host:8095",
 "timeout_ms": 2000, "inject_ms": 300}
```

With no backend named, the older discovery still answers, so a host set up
before the setting existed keeps working: a discovered URL means
`openai-compatible`, and none means `none`. A settings file that does not
read, names an unknown backend, or carries a key itself (`key`, `api_key`,
`token`, `secret`) is `none`, and `helm classify where` says why: a broken
file never turns a backend on.

**The URL** of `openai-compatible` is found at run time and never written in
helm's source:

1. `HELM_CLASSIFY_URL` — the stream's base URL, for example
   `http://classify-host:8095`. `off` pins the client off whatever else says.
2. else the setting's `url`;
3. else the `classify_url` key of the `host` block in the authored registry
   (`<helm home>/_global/registry-authored.json`, read through
   `registry.authored_host`);
4. else nothing: every call answers `unconfigured`.

**The key** is never written in a file. `key_env` in the setting (or
`HELM_CLASSIFY_KEY_ENV`) names the environment variable that holds it; its
value is sent as `Authorization: Bearer …` and nowhere else — to the backend URL this host configured (for `jev` with no URL set, the built-in gateway, which `helm classify where` names as `default`), so point a key only at a URL you trust. It is optional
for `openai-compatible` and required for `jev` (default
`AI_GATEWAY_API_KEY`): a `jev` backend whose variable is unset sends nothing.

**The Jev door.** Its base is `HELM_CLASSIFY_URL`, else the setting's `url`,
else the gateway `helm/relevance.py` already uses. The wire is ASSUMED to be
the gateway's evaluate call that `helm/relevance.py` speaks (`POST
<base>/v1/evaluate` with the Jev model, the text as `state.message`, and
boolean `questions` answered with a `probability` each): every label is asked
as its own yes/no question, so its scores are independent probabilities, not
a distribution over the labels. The gateway is outside the LAN, so a text
over 60,000 characters is `too-long` and a text or label that matches the
relevance lane's secret patterns is `scrubbed`; neither is sent.

```console
$ helm classify where
helm classify: openai-compatible http://classify-host:8095 (from registry)
```

In code, `classify.scores(text, labels, source, consumer=...)` is the
flag-never-decide face: `{label: score}`, or `None` for no opinion (no
backend, a refused source, a backend that is down, slow or refuses the size),
and a caller that gets `None` behaves exactly as it did before it asked.
`classify.classify()` returns the same call as a typed answer with its reason.

## Only helm's own text leaves the box

A caller names what its text is with `--source` (in code, `source=`), from a
closed set:

| source kind | what it is |
|---|---|
| `helm-chat` | helm's own chat rooms |
| `ledger` | helm's ledgers (dispatch, task, land) |
| `lane-diff` | a lane's staged or committed diff |
| `lane-transcript` | a transcript of a helm lane |

Anything else is **refused** before discovery and before a byte is sent, and
the refusal is an answer like any other (`refused-source`). Client data
(client exports, member data, anything a project marks client-owned) has no
source kind, so no caller can name it and none of it goes to the LAN.

## Answers

`classify()` returns a `Result` — `label`, `score` (that label's probability;
the forced label's when the stream was unsure), `scores`, `backend`, `ms` — or
a `FailOpen` with one of these reasons:

| outcome | when |
|---|---|
| `unconfigured` | the backend is `none`, no endpoint was discovered, `HELM_CLASSIFY_URL=off`, or the `jev` key variable is unset |
| `unreachable` | the connection failed, or an HTTP status the contract does not name |
| `timeout` | no answer inside the call's time budget, or no budget left |
| `no-backend` | HTTP 503: no backend answered (the detail names what was tried) |
| `too-long` | HTTP 413: the text is over the limit. It was not cut; trimming is the caller's decision |
| `refused-source` | the source kind is outside the closed set; nothing was sent |
| `rejected` | HTTP 400 |
| `malformed` | the answer carries no label and scores |
| `scrubbed` | `jev` only: the text or a label matched a secret pattern; nothing was sent |

Each call has a time budget: `HELM_CLASSIFY_TIMEOUT_MS` (default 2000) is one
wall-clock limit on the whole call, however slowly a backend answers, and `--budget-ms` on the verb is one budget shared by every
line of `--each-line`. After an `unconfigured`, `unreachable`, `timeout` or
`no-backend` answer the verb sends nothing more in that run.

## The bars

1. **A classifier never decides** a land, a review or a guard. It only flags
   candidates, and a deterministic rule gates.
2. **Every consumer ships with its own metric**, measured before and after.
3. **A ranking may reorder, never hide.**

## The metric journal

Every call appends one row to `<cache>/classify/metrics.jsonl` (`<cache>` is
`HELM_CACHE_DIR`, default `~/.cache/helm`): the consumer, the source kind, the
label, the score, the time, the outcome and the backend — never the text. The
journal keeps one rotated generation and is declared in `helm projections`.

```console
$ helm classify metrics --consumer private-name-advisory
$ printf 'first line\nsecond line\n' | helm classify --labels yes,no --source lane-diff --each-line --json
```

`helm classify label --consumer NAME --label ID` adds labelled examples, one
per stdin line as `HASH [NUMBER]`: a hash of a text the consumer already
judged, never the text. `helm classify eval private-name` asks the stream about
a consumer's labelled set and prints hits and misses at the threshold.

## Consumer: the private-name pre-commit rung

The rail's pre-commit hook runs `helm/private_names.py` (snapshotted beside
the shared hook like every rung):

- **The refusing leg** (`--staged`) reads every line the commit adds under a
  public-bound path (`helm/`, `docs/`, `tests/`, `agents/`, `bin/`,
  `scripts/`, and the top-level README and documentation files) and refuses a
  line that names one of the operator's private projects, hosts or people. It
  uses the same list and scanner as `tests/test_no_private_names.py`: there is
  one list, hex-encoded so it names nothing in the clear. The refusal names
  the path, the line and the list entry's number, never the name. Each refused
  line is journalled as a labelled `private` example (a hash and the entry
  number). One-commit owner override: `HELM_PRIVATE_NAME_SKIP=1`.
- **The advisory leg** (`--advise`) never refuses. It sends added comment and
  document lines (at most `HELM_PRIVATE_NAME_ADVISORY_LINES`, default 12,
  inside `HELM_PRIVATE_NAME_ADVISORY_MS` of classify time, default 2500, plus
  at most 1.5 s for `helm` to start) as `lane-diff` text through one `helm
  classify --each-line` run with the `@private-name` label set, and warns
  about each line whose `private` probability is at or above 0.75, highest
  first, with a pointer to the list. It asks the **internal-names** question:
  does the line name one of the owner's own machines or hosts, internal
  projects, repositories or lanes, or people on their team? Public products,
  companies, vendors, AI models, open-source projects, tools and version
  numbers are **not** private, and the task lists the shipped public names,
  plus the host's `public-names` local-names key, as known public names. It
  ranks by the `private` score, never by the returned label. It is silent when
  the stream is unconfigured, unreachable or slow, and off with
  `HELM_PRIVATE_NAME_ADVISORY=0`.

### Metric

Before this rung, nothing between a staged line and the train audits read a
private name outside `helm/`: three lane comments naming two private projects
reached the audits in one night.

After (measured on the lane's tip):

- **Deterministic leg, the leak shapes.** A project name written in a lane
  comment line, in six spellings each (lower case, title case with a
  possessive, upper case with an underscore suffix, inside a path, inside a
  seat handle, upper case with a colon), for both names from that night:
  **12 of 12 refused**. Every listed name in each of eight public-bound path
  kinds: **96 of 96 refused**. Arms: `tests/test_private_names.py`.
- **Deterministic leg, false positives over the current tree.** Every line of
  every tracked public-bound file read as if it were added: on trunk (1,017
  files, 950,638 lines) **65 lines would refuse, all 65 true occurrences of a
  listed name**, 0 matcher false positives. 60 are in two skills under
  `agents/` that name private tools, 1 is a transcript fixture under
  `tests/fixtures/`, and 4 were prose lines in `tests/`, reworded in this lane,
  so the lane's tree (1,022 files, 952,889 lines) reads **61**. `helm/`,
  `docs/`, `bin/`, `scripts/` and the top-level files: 0. The rung judges
  only ADDED lines, so this debt blocks no unrelated commit.
- **Advisory leg, labelled set** (`helm classify eval private-name`; built at
  run time: every listed name in three comment shapes, plus 18 neutral lines
  of the same register, 6 of them naming public projects and tools), over
  four runs against the live stream: at 0.75, **9 to 10 of 36 positives
  flagged, 0 of 18 neutral lines flagged** in every run; the highest neutral
  score was 0.23 to 0.32, and the mean call took 381 ms with this label set's
  longer definitions. By shape, a capitalised name with a possessive scored
  0.83 or more in 8 to 9 of 12 names, while a lower-case name used as an
  ordinary noun ("the NAME box", "the NAME lane") scored under 0.75 in 23 to
  24 of 24 lines. That split is the reason the list, not the classifier,
  gates: the advisory finds a name the list does not know yet when it reads
  as a name, and says nothing about one that reads as a word.
- **Advisory leg, the question changed.** The first question was the
  allowlist question: does the line name anything **not** in the shipped
  public names? It defined private as "not listed", so a public name the list
  does not carry (a vendor, a model, a version string) read as private. The
  labelled set now carries 15 such lines as negatives beside the 18 neutral
  ones. At 0.75 on the 69-row set, the stream's owner measured the allowlist
  question at **9 of 36 private caught, 0 of 18 neutral and 8 of 15 public
  names flagged**, and the internal-names question at **6 of 36, 0 of 18 and
  0 of 15**. The lane's own eval against the live stream agreed: the
  allowlist question gave tp 10, fn 24, fp 8 (all 8 public-name lines),
  tn 25, 2 timeouts; the internal-names question, in two runs, gave tp 6,
  fn 30, fp 0 and tn 29 to 31, with 2 to 4 timeouts, and no public-name line
  scored above 0.24. The list owns recall, so the advisory keeps the question
  whose flags are right: it gives up about a third of its catches to stop
  flagging public names.

## Consumer: the injection trim, in shadow

The per-turn injection hook (`helm/inject/_whisper.py`) asks the classifier
which of the jit lines it delivered are relevant to the turn, and changes
nothing the agent sees (`classify.inject_shadow`):

- It asks **only on a host that named a backend** (`HELM_CLASSIFY_BACKEND` or
  `backend` in `classify.json`). A `none` host, or one that has only the older
  URL discovery, pays one small settings read per turn and opens no socket.
- It asks **only for a turn in a lane room**: that turn's prompt and the lines
  injected into it are a helm-lane transcript (`lane-transcript`), and nothing
  else is sent.
- **One call per turn.** The text is the turn's substance (what the keyword
  lane read); each delivered jit line, at most 15, is a label whose definition
  is the line, beside a `none` label.
- **Bounded.** `HELM_CLASSIFY_INJECT_MS` (else `inject_ms` in the setting;
  default 300, at most 1000, `0` off) is the whole call's budget, far inside
  the hook's own deadline.
- **Logged, never applied.** The answer lands on the turn's fire-ledger row as
  `classify_shadow`: `outcome`, `ms`, `n`, and on an answer `scores` (aligned
  with the delivered jit lines), `none` and `relevant` (the indexes at or above
  0.75). The sections are final before it runs and it writes only the row.

### Metric

Each call is journalled under the consumer `inject-shadow`; `helm classify
metrics --consumer inject-shadow` reads back how many turns were asked and how
many got a score. Before this consumer, nothing measured whether a delivered
line was relevant to its turn at all; the shadow rows are the before
measurement a live trim would be judged against, and no line is dropped on a
score until they have been read.

