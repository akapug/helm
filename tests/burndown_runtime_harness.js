/* Client-runtime harness for the burn-down console renderers.
 *
 * task/986. Four renderers draw three sections of the owner's console and
 * nothing referenced them. The defect that prompted the row was a COUPLING —
 * calling ocbRender after obdRender's unavailable early return, so an owed
 * source that could not answer silently blanked a cured bucket that could.
 *
 * REVISION AFTER A REVIEW, and its four findings were one defect:
 * the first version spliced the renderers and then HAND-WROTE THEIR ENTIRE
 * ENVIRONMENT. esc, the selector and the DOM were all fakes, so the harness
 * tested my escaper against my selector over my auto-created elements. Every
 * finding was a place that invented world diverged from production:
 *   - a reimplemented esc that dropped production's apostrophe escaping, so
 *     ledger-controlled apostrophes took a different path here than in the
 *     browser;
 *   - a selector that AUTO-CREATED every id, so renaming or removing a mount
 *     point threw in production and passed here;
 *   - no execution of obdInit, so removing its obdRender call left the
 *     hand-built calls green while the console rendered nothing;
 *   - undeclared data seeded but never read, so oubRender could be handed the
 *     WRONG BUCKET and every arm stayed green.
 *
 * So production's esc and selector semantics are used, the DOM contains
 * EXACTLY the ids the view declares, and obdInit runs for real against a
 * stubbed fetch. The renderer sources are spliced verbatim at the marker.
 *
 * Date.now is PINNED: obdAge turns a timestamp into an age, and an arm whose
 * expected value drifts with the wall clock fails at midnight for reasons
 * unrelated to the code. Plain CommonJS so the spliced declarations share
 * scope. */

const NOW = Date.parse("2026-08-11T12:00:00Z");
Date.now = () => NOW;

/* THE MOUNT POINTS THE VIEW ACTUALLY DECLARES, spliced from the .html.part at
 * build time so this list cannot drift from the markup. A selector for
 * anything else returns NULL, exactly as document.querySelector does — that is
 * what makes a renamed or deleted mount id FAIL here instead of passing. */
const DECLARED = /*__IDS__*/[];

const DOM = {};
for (const id of DECLARED) DOM[id] = {id: id, textContent: "", innerHTML: ""};

/* PRODUCTION SEMANTICS: null for an unknown selector, never an invented node. */
function $(sel) {
  const id = String(sel).replace(/^#/, "");
  return Object.prototype.hasOwnProperty.call(DOM, id) ? DOM[id] : null;
}

/* esc is SPLICED FROM PRODUCTION at the marker below, not reimplemented. */
/*__CORE__*/

/* The fetch helper obdInit calls. Each case sets NEXT before running it. */
let NEXT = null;
let THROW = null;
const FETCHED = [];          /* every URL obdInit asked for, in order */
async function j(url, ms) {
  FETCHED.push({url: url, ms: ms});
  if (THROW) throw new Error(THROW);
  return NEXT;
}

const TIMERS = [];           /* every setInterval the wiring installs */
function setInterval(fn, ms) { TIMERS.push({fn: fn, ms: ms}); return TIMERS.length; }

/* CONSOLE CAPTURE (task/1033). "Neither failure is swallowed" is only an
 * assertable property if the developer-facing half of the report is recorded
 * too. The Error OBJECT is what must reach console.error — String(err) keeps
 * "TypeError: ..." so the KIND of throw is visible, and `error` records that
 * a real Error (and therefore its stack) was passed rather than a flattened
 * string. This is also the ONLY report available when the failing mount is
 * the reporter's own. */
const ERRORS = [];
console.error = (...a) => {
  ERRORS.push({
    text: a.map(x => String(x)).join(" "),
    error: a.some(x => x instanceof Error),
  });
};

/*__INJECT__*/

/*__WIRING__*/

/* ---------------------------------------------------------------- cases --- */

function reset() {
  /* CLEARS IN PLACE, NEVER REPLACES. Replacing DOM[id] with a fresh object
   * discards anything the page assigned to that element at load time — which
   * is exactly where the top-level wiring lives: `$("#obdReload").onclick =
   * ...` runs ONCE at splice, and four earlier cases each called reset()
   * before the reload case ran. The click then reached the endpoint 0 times
   * and read as a DEAD BUTTON rather than as a fixture that had erased it.
   * A reset that destroys page state cannot be used to test page state. */
  for (const id of DECLARED) {
    const el = DOM[id];
    el.textContent = "";
    el.innerHTML = "";
  }
  THROW = null;
  ERRORS.length = 0;
}
/* AN ESCAPING THROW IS AN OUTCOME, NOT A HARNESS CRASH. obdInit is driven by
 * an interval that ignores the returned promise, so a throw out of it means
 * the browser logs an unhandled rejection and the console silently keeps its
 * STALE text. Recording it lets an arm tell that apart from a drawn failure
 * line — and keeps one broken case from taking the whole run's output with
 * it, which would turn every unrelated arm into an error too. */
let ESCAPED = null;
async function run() {
  ESCAPED = null;
  try { await obdInit(); } catch (e) { ESCAPED = String(e); }
}
function snapshot() {
  const out = {};
  for (const [k, v] of Object.entries(DOM)) {
    out[k] = {text: v.textContent, html: v.innerHTML};
  }
  return out;
}

const CURED_TWO = {
  total: 2, rows: [
    {row: "a1b2c3d4e5f6", lane: "lane/one", seat: "seat-one",
     reviewer: "rev-one", since: "2026-08-08T11:00:00Z",
     branch: "lane/one", tip: "0123456789ab", ahead: 3},
    {row: "ffffffffffff", lane: "lane/two", seat: "seat-two",
     reviewer: "rev-two", since: null, branch: "", tip: "", ahead: 0},
  ],
};
/* THE UNDECLARED BUCKET IS DISTINGUISHABLE FROM THE CURED ONE ON PURPOSE.
 * The old fixture seeded undeclared data nothing ever read, so
 * oubRender(d && d.cured) — the WRONG bucket — passed every arm. These two
 * carry different totals and different lane strings, so a swap is visible. */
const UNDECLARED_FOUR = {
  total: 4, rows: [
    {row: "111111111111", lane: "lane/undeclared-a", seat: "seat-u1",
     since: "2026-08-09T11:00:00Z"},
  ],
};

const results = {};

results.age = {
  missing: obdAge(undefined),
  empty: obdAge(""),
  unparseable: obdAge("not-a-date"),
  future: obdAge("2026-09-01T00:00:00Z"),
  today: obdAge("2026-08-11T11:00:00Z"),
  three_days: obdAge("2026-08-08T11:00:00Z"),
};

/* PRODUCTION'S esc, run for real on the character class the ledger supplies.
 * A lane name with an apostrophe is ordinary, not exotic. */
results.esc = {
  apostrophe: esc("lane/o'brien"),
  angle: esc("<script>"),
  amp: esc("a&b"),
  quote: esc('say "hi"'),
  nullish: esc(null),
};

(async () => {
  /* THE COUPLING CASE, DRIVEN THROUGH obdInit rather than by hand, so the
   * production wiring (fetch -> generation check -> obdRender) is what runs. */
  reset();
  NEXT = {unavailable: true, why: "the ledger did not open",
          cured: CURED_TWO, undeclared: UNDECLARED_FOUR};
  await obdInit();
  results.owed_blind = snapshot();

  /* THE ORDINARY CASE. Undated sorts LAST and prints "?" — the task/872
   * defect one layer down, re-implemented here in obdAge. */
  reset();
  NEXT = {
    unavailable: false, total: 3, rows: [
      {row: "undated00000", lane: "lane/undated", seat: "seat-undated",
       since: null},
      {row: "old000000000", lane: "lane/old", seat: "seat-old",
       since: "2026-08-01T11:00:00Z"},
      {row: "recent000000", lane: "lane/recent", seat: "seat-recent",
       since: "2026-08-10T11:00:00Z"},
    ],
    cured: {total: 0, rows: []}, undeclared: UNDECLARED_FOUR,
  };
  await obdInit();
  results.populated = snapshot();

  /* INDEPENDENCE THE OTHER WAY: a healthy owed half must not lend its health
   * to a cured walk that broke. */
  reset();
  NEXT = {unavailable: false, total: 1,
          rows: [{row: "r00000000000", lane: "lane/live", seat: "seat-live",
                  since: "2026-08-10T11:00:00Z"}],
          cured: {unavailable: true, why: "git could not answer"},
          undeclared: UNDECLARED_FOUR};
  await obdInit();
  results.cured_blind = snapshot();

  /* THE FETCH ITSELF FAILING is obdInit's own catch arm, which no direct
   * obdRender call can reach. */
  reset();
  THROW = "network is down";
  await obdInit();
  results.fetch_failed = snapshot();
  results.fetch_failed_errors = ERRORS.slice();

  /* ------------------------------------------- SERVE-STALE-WHILE-REBUILD ---
   * The endpoint costs a full fold of the dispatch ledger, so it is served
   * through the same serve-stale cache the land card uses: a COLD read answers
   * {warming:true} and a warm one carries the age of the body being served.
   * Both states are drawn by this console and neither existed before, so both
   * get a case here rather than a string assertion about the source. */

  /* WARMING IS NOT UNREADABLE. The old console had no such branch, so this
   * body fell into `!d || d.unavailable` and printed "✗ CANNOT SEE the
   * burn-down" — an alarm about a record that is perfectly readable and merely
   * uncomputed. The sibling buckets are absent from a warming body, so they
   * would have printed their own CANNOT-SEE lines beside it. */
  reset();
  NEXT = {warming: true};
  await obdInit();
  results.warming = snapshot();

  /* A WARM BODY CARRIES ITS AGE, and the age is the SERVER's — this cache may
   * serve a body minutes old while one rebuild runs, so a list printed with no
   * age passes as current. The number is deliberately not round and not small:
   * 372s renders through lrAgo as minutes, which a "0s"-shaped bug cannot
   * imitate. */
  const AGED = {
    unavailable: false, total: 1,
    rows: [{row: "a00000000000", lane: "lane/aged", seat: "seat-aged",
            since: "2026-08-10T11:00:00Z"}],
    cured: CURED_TWO, undeclared: UNDECLARED_FOUR, read_age_s: 372,
  };
  reset();
  NEXT = AGED;
  await obdInit();
  results.aged = snapshot();

  /* AND A BODY WITH NO AGE SAYS "unknown", never "0s ago" — an older server,
   * or any path that could not date what it served. A fabricated zero on a
   * freshness line is the worst available lie. */
  reset();
  NEXT = Object.assign({}, AGED);
  delete NEXT.read_age_s;
  await obdInit();
  results.ageless = snapshot();

  /* ------------------------------------------------ task/1033: THE SPLIT ---
   * A TRANSPORT failure and a DEFECT IN THIS PAGE are different failures with
   * different readers. Before the split, obdInit wrapped its fetch and its
   * render in ONE try/catch and printed one sentence for both, so a renamed
   * mount id was indistinguishable from a dead network on the owner's screen.
   * The two cases below are that pair, run through the SAME entry point with
   * the SAME healthy payload, so the ONLY difference is where the throw came
   * from — which is exactly what the surface has to reveal. */
  const HEALTHY = {
    unavailable: false, total: 1,
    rows: [{row: "h00000000000", lane: "lane/healthy", seat: "seat-h",
            since: "2026-08-10T11:00:00Z"}],
    cured: CURED_TWO, undeclared: UNDECLARED_FOUR,
  };

  /* A CASE THAT REMOVES A MOUNT OWNS PUTTING IT BACK. reset() CLEARS IN PLACE
   * and deliberately cannot rebuild a deleted element — rebuilding is what
   * destroyed the page's load-time wiring. So the removal is scoped here, and
   * the snapshot is taken WHILE the mount is gone (an arm asserting the id is
   * absent is how we know the rename was real). The restore is in `finally`:
   * a throw that skipped it would leave every later case — the reload click,
   * the interval, the generation race — running against a DOM this case
   * broke, and they would fail for a reason that is not theirs. */
  async function withMountRemoved(id, label) {
    const saved = DOM[id];
    delete DOM[id];
    try {
      await run();
      results[label] = {dom: snapshot(), errors: ERRORS.slice(),
                        escaped: ESCAPED};
    } finally {
      DOM[id] = saved;
    }
  }

  /* A SIBLING SECTION'S MOUNT IS RENAMED. ocbRender runs FIRST (above the
   * owed guard, by design), so it throws before the burn-down half draws
   * anything — the fetch was perfect and the console is still blank. */
  reset();
  NEXT = HEALTHY;
  await withMountRemoved("ocbmeta", "render_defect");

  /* THE REPORTER'S OWN MOUNT IS THE MISSING ONE. The old catch wrote through
   * $("#obdmeta") unguarded, so this case threw a SECOND TypeError from
   * inside the catch and the original cause was destroyed — the page went
   * silent with nothing anywhere naming why. */
  reset();
  NEXT = HEALTHY;
  await withMountRemoved("obdmeta", "defect_at_the_reporter");

  results.fetched = FETCHED;
  results.timers = TIMERS.map(t => ({ms: t.ms}));
  /* THE WIRING IS EXECUTED, NOT GREPPED. Fire what production installs and
   * assert obdInit actually ran — a no-op onclick or a dropped interval keeps
   * its source text and fails HERE instead of passing a string check. */
  // NO reset() HERE ON PURPOSE. reset() rebuilds every element from DECLARED,
  // which DESTROYS the onclick the spliced wiring assigned at load time — the
  // fixture would erase the exact wiring this case exists to prove. Measured:
  // with the reset in place the click reached the endpoint 0 times and read as
  // a dead button rather than as a clobbered fixture.
  const before = FETCHED.length;
  NEXT = {unavailable: false, total: 0, rows: [],
          cured: {total: 0, rows: []}, undeclared: {total: 0, rows: []}};
  const btn = $("#obdReload");
  if (btn && typeof btn.onclick === "function") { await btn.onclick(); }
  results.reload_fetched = FETCHED.length - before;
  const tick = TIMERS.length ? TIMERS[0].fn : null;
  const beforeTick = FETCHED.length;
  if (tick) { await tick(); }
  results.interval_fetched = FETCHED.length - beforeTick;

  /* THE GENERATION RACE, RUN. Counting `gen !== OBD_GEN` in the source passes
   * on a no-op that keeps the string. This starts a slow read, starts a fast
   * one, and asserts the STALE answer never lands. */
  reset();
  let release = null;
  const slow = new Promise(r => { release = r; });
  NEXT = null;
  const origJ = j;
  let call = 0;
  // eslint-disable-next-line no-func-assign
  j = async (url, ms) => {
    FETCHED.push({url: url, ms: ms});
    call += 1;
    if (call === 1) { await slow; return {unavailable: false, total: 111,
        rows: [], cured: {total: 0, rows: []}, undeclared: {total: 0, rows: []}}; }
    return {unavailable: false, total: 222, rows: [],
            cured: {total: 0, rows: []}, undeclared: {total: 0, rows: []}};
  };
  const first = obdInit();
  const second = obdInit();
  await second;
  release();
  await first;
  results.race_meta = DOM["obdmeta"].textContent;
  j = origJ;
  process.stdout.write(JSON.stringify(results));
})();
