#!/usr/bin/env python3
"""`helm pi` — the eval's arm-pinning generator, and why it exists early.

0.3's §E-8 converged on shipping the EVIDENCE plus the thin seat adapter and
DEFERRING the pi extension "until the eval proves pi earns the surface." Right
instinct, wrong order, and measuring the host settled it:

  §E-5 pins the two arms as the same codex model, same tier, same task suite,
  under two harnesses. On this host `cc-codex` reaches codex through helm's
  CLIProxyAPI on the owner's SUBSCRIPTION OAuth, and pi has exactly ONE
  provider configured: openrouter. Run as-is, the eval compares
  Claude-Code-over-subscription with pi-over-OpenRouter and credits the whole
  difference to the harness — different provider, different billing, different
  rate limits, possibly a different model build. That is precisely the confound
  §E-5 was written to forbid.

pi's supported way to point at another endpoint is an extension calling
`pi.registerProvider(...)`, so a minimal extension is a PREREQUISITE of the
eval rather than a reward for it. This module generates only the provider
override; §B.2's reflex/chat bridge stays deferred as converged.

TWO INVARIANTS THESE PIN, and the second is the one that would hurt:

  1. The generated provider points at the SAME proxy cc-codex uses, so the
     comparison isolates the harness.
  2. NO CREDENTIAL MATERIAL IS EVER WRITTEN. pi interpolates `$VAR` in config
     values, so the shim carries an env REFERENCE and the token stays in the
     seat's 0600 proxy config. A generator that helpfully inlined the key would
     produce a world-readable .ts holding a live credential, and it would look
     completely fine in review.
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import os as _os, sys as _sys  # noqa: E402
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

# helm.seat first: seat_catalog is one of its impl modules, and the seat
# facade seeds them (tests/test_seat_facade_injection.py).
from helm import pi, seat  # noqa: E402


class SourceTest(unittest.TestCase):
    def src(self, **kw):
        kw.setdefault("seat", "codex")
        kw.setdefault("port", 8317)
        kw.setdefault("model", "gpt-5.6-sol")
        return pi.extension_source(**kw)

    def test_it_points_at_the_seats_own_proxy(self):
        """The whole reason the file exists: same endpoint as cc-codex, so the
        eval measures the harness rather than the provider."""
        self.assertIn('baseUrl: "http://127.0.0.1:8317/v1"', self.src())

    def test_the_key_is_read_from_child_env_never_inlined(self):
        """THE ONE THAT WOULD HURT. A generator that inlined the token would
        write a live credential into a plain .ts under the user's home, and the
        diff would look entirely reasonable."""
        s = self.src()
        self.assertIn('apiKey: process.env["%s"] || ""' % pi.KEY_ENV, s)
        self.assertNotIn("sk-", s)

    def test_NO_credential_shaped_string_survives_generation(self):
        """A blunt negative control against every future edit: nothing in the
        output may look like key material, whatever the template does."""
        import re
        s = self.src()
        self.assertEqual(re.findall(r"[A-Za-z0-9_\-]{40,}", s), [])

    def test_the_openai_compatible_route_is_declared(self):
        """CLIProxyAPI speaks openai-completions on the route this targets;
        picking the wrong `api` fails at request time, not at generation."""
        self.assertIn('api: "openai-completions"', self.src())

    def test_an_upstream_alias_overrides_the_claude_side_name(self):
        """Families whose provider-side id differs from the claude-side alias
        (ds4pro: ds4-pro -> deepseek/deepseek-v4-pro) must send the UPSTREAM
        id, or the proxy rejects a model it has never heard of."""
        s = self.src(model="ds4-pro", upstream="deepseek/deepseek-v4-pro")
        self.assertIn('id: "deepseek/deepseek-v4-pro"', s)

    def test_the_provider_name_is_namespaced_to_helm(self):
        """A provider called `codex` would collide with pi's own built-ins and
        silently override them for every session on the box."""
        self.assertEqual(pi.provider_name("codex"), "helm-codex")
        self.assertIn('registerProvider("helm-codex"', self.src())


class WriteTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-pi-ext-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_dry_run_writes_NOTHING(self):
        out = os.path.join(self.d, "helm-codex.ts")
        path, src, err = pi.write_extension("codex", out=out)
        self.assertIsNone(err)
        self.assertTrue(src)
        self.assertFalse(os.path.exists(out), "a preview must not install")

    def test_apply_writes_the_file(self):
        out = os.path.join(self.d, "nested", "helm-codex.ts")
        path, src, err = pi.write_extension("codex", out=out, apply=True)
        self.assertIsNone(err)
        self.assertTrue(os.path.exists(out))
        with open(out, encoding="utf-8") as fh:
            self.assertIn("registerProvider", fh.read())

    def test_an_unknown_seat_is_refused_not_generated(self):
        """Generating a provider for a seat that does not exist would produce a
        shim pointing at a port nothing listens on — a failure that surfaces
        only mid-eval, as noise in the numbers."""
        path, src, err = pi.write_extension("not-a-seat")
        self.assertIsNone(src)
        self.assertIn("unknown seat", err)


class PortTest(unittest.TestCase):
    def test_the_port_comes_from_the_family_table_not_the_0600_config(self):
        """There is no reason to open a file holding a credential to learn a
        port number, and every reason not to."""
        port, err = pi.seat_port("codex")
        self.assertIsNone(err)
        self.assertTrue(isinstance(port, int) and port > 0)

    def test_an_unknown_family_reports_the_reason(self):
        port, err = pi.seat_port("nope-9")
        self.assertIsNone(port)
        self.assertIn("unknown seat", err)


def _declared(src):
    """(contextWindow, maxTokens) as the generated provider declares them.
    Exactly one of each, or the arm fails: a second model entry would be a
    second answer nobody asked for."""
    import re
    ctx = re.findall(r"^\s*contextWindow: (\d+),$", src, re.M)
    out = re.findall(r"^\s*maxTokens: (\d+),$", src, re.M)
    if len(ctx) != 1 or len(out) != 1:
        raise AssertionError("want one contextWindow and one maxTokens, got "
                             "%r and %r" % (ctx, out))
    return int(ctx[0]), int(out[0])


class WindowFromCatalogTest(unittest.TestCase):
    """task/3380: pi's provider takes its window and output cap from the seat's
    family catalog row, through the resolver the Claude Code launch line uses.

    MEASURED on a local seat's host: every generated provider declared
    contextWindow 400000 and maxTokens 64000. pi reads contextWindow as the
    TOTAL window (it compacts at contextWindow - reserveTokens and clamps
    max_tokens to contextWindow - input), so on a local seat it held context
    far past the server slot (qwen27 212,992, bonsai 131,072) and requests
    failed on context overflow.

    Every expected number below is READ FROM THE CATALOG, never typed here, so
    a re-probe that moves a slot moves the expectation with it. The writes go
    through write_extension with --apply into a temp dir (the write path, not
    only the preview), and seat_port is stubbed so no ledger or seat config is
    read."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="helm-test-pi-window-")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def write(self, seat, model=None):
        """(file text, warnings, err) from the real write path."""
        out = os.path.join(self.d, "helm-%s.ts" % seat)
        if os.path.exists(out):          # subtests share a seat's path
            os.remove(out)
        warnings = []
        with mock.patch.object(pi, "seat_port", return_value=(8345, None)):
            path, src, err = pi.write_extension(seat, out=out, model=model,
                                                apply=True,
                                                warn=warnings.append)
        if err:
            self.assertFalse(os.path.exists(out),
                             "a refused provider must not be written")
            return None, warnings, err
        with open(out, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text, src, "the preview and the file must agree")
        return text, warnings, None

    def declared(self, seat, model=None):
        """(contextWindow, maxTokens, warnings) of one WRITTEN provider; a
        refusal fails the arm with its reason."""
        text, warnings, err = self.write(seat, model=model)
        if err:
            self.fail("%s was refused: %s" % (seat, err))
        return _declared(text) + (warnings,)

    def planted(self, **row):
        """A throwaway family `pifake` in the live table, removed on exit."""
        from helm import seat_catalog
        row.setdefault("port", 8999)
        row.setdefault("model", "pifake-model")
        return mock.patch.dict(seat_catalog.FAMILIES, {"pifake": row})

    # --- a family that declares max_context -----------------------------

    def test_a_max_context_family_is_told_exactly_that_window(self):
        """No budget, no model_context entry for the launch model: the window
        is max_context itself. qwenlocal is the local family the defect names."""
        from helm.seat_catalog import FAMILIES
        plain = [f for f, fam in FAMILIES.items() if fam.get("max_context")
                 and not fam.get("context_budget")
                 and fam.get("model") not in (fam.get("model_context") or {})]
        self.assertIn("qwenlocal", plain)
        got = {f: self.declared(f)[::2] for f in plain}
        self.assertEqual(got, {f: (FAMILIES[f]["max_context"], [])
                               for f in plain})

    # noqa: VACUOUS_ASSERTION — the control is FAMILIES["bonsai"]["context_budget"] which is the source of truth; if pi tells CC a different window than what the catalog says, that is the real defect the test catches
    def test_a_budgeted_family_is_told_its_budget(self):
        """bonsai has context_budget: pi tells the budget, not the raw max_context.
        If this FAILS, pi tells bonsai a different window than Claude Code does."""
        from helm.seat_catalog import FAMILIES
        self.assertEqual(self.declared("bonsai")[0], FAMILIES["bonsai"]["context_budget"])

    def test_a_planted_max_context_is_the_window(self):
        with self.planted(max_context=123456, max_output_tokens=4096):
            limits, err = pi.provider_limits("pifake", "pifake-model")
        self.assertIsNone(err)
        self.assertEqual(limits["context_window"], 123456)
        self.assertEqual(limits["max_tokens"], 4096)

    # --- a per-model model_context entry --------------------------------

    def test_a_model_context_entry_wins_for_its_model(self):
        """codex keys spark at 76,000 under a 220,000 family ceiling: a pi run
        on spark is told spark's window, as the launch line tells CC."""
        from helm.seat_catalog import FAMILIES, launch_window
        fam = FAMILIES["codex"]
        spark = "gpt-5.3-codex-spark"
        self.assertIn(spark, fam["model_context"])
        self.assertNotEqual(fam["model_context"][spark], fam["max_context"],
                            "the control needs an entry that differs")
        text, _w, err = self.write("codex", model=spark)
        self.assertIsNone(err)
        self.assertIn("contextWindow: %d," % fam["model_context"][spark],
                      text)
        self.assertIn("contextWindow: %d," % launch_window(fam, spark), text)

    def test_every_model_context_entry_reads_through_launch_window(self):
        from helm.seat_catalog import FAMILIES, launch_window
        pairs = [(f, m) for f, fam in FAMILIES.items()
                 for m in (fam.get("model_context") or {})]
        self.assertIn(("codex", "gpt-5.3-codex-spark"), pairs)
        got = {pair: pi.provider_limits(*pair) for pair in pairs}
        self.assertIn(("codex", "gpt-5.3-codex-spark"), got)
        self.assertEqual(
            {pair: (limits and limits["context_window"], err)
             for pair, (limits, err) in got.items()},
            {(f, m): (launch_window(FAMILIES[f], m), None) for f, m in pairs})

    def test_a_planted_model_entry_wins_and_other_models_fall_to_the_family(self):
        with self.planted(max_context=100000,
                          model_context={"pifake-small": 50000}):
            small, err1 = pi.provider_limits("pifake", "pifake-small")
            other, err2 = pi.provider_limits("pifake", "pifake-other")
        self.assertIsNone(err1)
        self.assertIsNone(err2)
        self.assertEqual(small["context_window"], 50000)
        self.assertEqual(other["context_window"], 100000)

    def test_a_context_budget_narrows_the_window_as_the_launch_line_does(self):
        """qwen27's budget (163,840) is what its seat holds; its compaction
        margin comes FROM the budget. pi must not be told the wider
        max_context, or it holds what the Claude Code seat is kept from."""
        from helm.seat_catalog import FAMILIES
        budgeted = [f for f, fam in FAMILIES.items()
                    if fam.get("context_budget")]
        self.assertIn("qwen27", budgeted)
        qwen27, err = pi.provider_limits("qwen27", FAMILIES["qwen27"]["model"])
        self.assertIsNone(err)
        self.assertIn("context_budget", qwen27["window_source"])
        self.assertEqual(qwen27["context_window"],
                         FAMILIES["qwen27"]["context_budget"])
        got = {f: pi.provider_limits(f, FAMILIES[f]["model"])[0]
               for f in budgeted}
        self.assertIn("qwen27", got)
        self.assertEqual(
            {f: (limits["context_window"], limits["window_source"])
             for f, limits in got.items()},
            {f: (FAMILIES[f]["context_budget"], "%s's context_budget" % f)
             for f in budgeted})
        self.assertEqual([f for f in budgeted
                          if not got[f]["context_window"]
                          < FAMILIES[f]["max_context"]], [])

    # --- the output cap -------------------------------------------------

    def test_max_output_tokens_is_maxTokens(self):
        from helm.seat_catalog import FAMILIES
        capped = [f for f, fam in FAMILIES.items()
                  if fam.get("max_output_tokens")]
        self.assertIn("bonsai", capped)
        got = {f: self.declared(f)[1] for f in capped}
        self.assertIn("bonsai", got)
        self.assertEqual(got, {f: FAMILIES[f]["max_output_tokens"]
                               for f in capped})

    def test_a_family_with_no_output_cap_gets_the_named_fallback(self):
        from helm.seat_catalog import FAMILIES
        self.assertIn("max_context", FAMILIES["codex"],
                      "a pinned window, so the output is the only fallback")
        self.assertNotIn("max_output_tokens", FAMILIES["codex"])
        text, _w, err = self.write("codex")
        self.assertIsNone(err)
        self.assertIn("maxTokens: %d," % pi.UNDECLARED_OUTPUT_TOKENS, text)
        self.assertIn("codex declares no max_output_tokens", text,
                      "the file says why maxTokens is what it is")

    # --- a family that declares no window -------------------------------

    def test_a_family_with_no_window_falls_back_to_the_cc_default_and_warns(self):
        """ds4flash pins no window, on purpose (the catalog records why: its
        route publishes none). Its Claude Code seats would run under Claude
        Code's own 200k default, so the pi arm is told the same number, named
        in the file and warned on stderr, never a silent 400000."""
        from helm.seat_catalog import FAMILIES, _CC_ASSUMED_WINDOW_MIRROR
        fam = FAMILIES["ds4flash"]  # noqa: SEAT_NAME — the catalog FAMILY key whose window is this arm's subject
        self.assertIn("model", fam, "the warning names the model it read")
        self.assertFalse(fam.get("max_context"))
        self.assertFalse(fam.get("model_context"))
        text, warnings, err = self.write("ds4flash")  # noqa: SEAT_NAME — the catalog FAMILY key whose window is this arm's subject
        self.assertIsNone(err)
        self.assertIn("contextWindow: %d," % _CC_ASSUMED_WINDOW_MIRROR, text)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("ds4flash", warnings[0])  # noqa: SEAT_NAME — the catalog FAMILY key whose window is this arm's subject
        self.assertIn("pins no context window", warnings[0])
        self.assertIn("pins no context window", text)
        self.assertNotIn("400000", text)

    def test_a_planted_unpinned_family_warns_too(self):
        from helm.seat_catalog import _CC_ASSUMED_WINDOW_MIRROR
        with self.planted(max_output_tokens=8000):
            limits, err = pi.provider_limits("pifake", "pifake-model")
        self.assertIsNone(err)
        self.assertEqual(limits["context_window"], _CC_ASSUMED_WINDOW_MIRROR)
        self.assertEqual(limits["max_tokens"], 8000)
        self.assertTrue(limits["warnings"])

    # --- maxTokens at or above the window -------------------------------

    def test_an_output_cap_at_or_above_the_window_is_refused(self):
        with self.planted(max_context=32000, max_output_tokens=32000), \
                mock.patch.object(seat, "_seat_family",
                                  return_value=("pifake", None)):
            equal, _w, err = self.write("pifake")
        self.assertIn("maxTokens 32000", err)
        self.assertIn("contextWindow 32000", err)
        self.assertIsNone(equal)
        with self.planted(max_context=32000, max_output_tokens=40000), \
                mock.patch.object(seat, "_seat_family",
                                  return_value=("pifake", None)):
            above, _w, err = self.write("pifake")
        self.assertIn("maxTokens 40000", err)
        self.assertIn("pifake", err)
        self.assertIsNone(above)

    def test_an_output_cap_one_under_the_window_is_written(self):
        """The boundary's other side, so the refusal is not a blanket one."""
        with self.planted(max_context=32000, max_output_tokens=31999), \
                mock.patch.object(seat, "_seat_family",
                           return_value=("pifake", None)):
            text, _w, err = self.write("pifake")
        self.assertIsNone(err)
        self.assertEqual(_declared(text), (32000, 31999))

    def test_the_fallbacks_are_held_to_the_same_rule(self):
        """A fallback window with a declared cap above it, and a small window
        with no cap under the fallback cap, both refuse."""
        from helm.seat_catalog import _CC_ASSUMED_WINDOW_MIRROR
        with self.planted(max_output_tokens=_CC_ASSUMED_WINDOW_MIRROR):
            limits, err = pi.provider_limits("pifake", "pifake-model")
        self.assertIsNone(limits)
        self.assertIn("pifake", err)
        with self.planted(max_context=pi.UNDECLARED_OUTPUT_TOKENS):
            limits, err = pi.provider_limits("pifake", "pifake-model")
        self.assertIsNone(limits)
        self.assertIn("pifake", err)

    # --- today's real families ------------------------------------------

    def test_todays_local_families_write_their_catalog_numbers(self):
        """The defect's own seats. Each written provider declares the taught
        window and the declared cap, and the two fit the server slot the
        catalog records, so no request pi sends can overflow it."""
        from helm.seat_catalog import FAMILIES, launch_window, own_box
        local = [f for f, fam in FAMILIES.items() if own_box(fam)]
        self.assertTrue({"qwen27", "qwenlocal", "bonsai"} <= set(local), local)
        got = {f: self.declared(f) for f in local}
        self.assertIn("bonsai", got)
        self.assertEqual(got, {f: (launch_window(FAMILIES[f],
                                                 FAMILIES[f]["model"]),
                                   FAMILIES[f]["max_output_tokens"], [])
                               for f in local})
        self.assertEqual([f for f, (ctx, out, _w) in got.items()
                          if ctx + out > FAMILIES[f]["probed_context_length"]],
                         [], "a written pair overflows its server slot")

    def test_every_family_pi_can_target_writes_a_coherent_pair(self):
        """Every family with a proxy port: the window is the resolver's
        answer (or the named fallback), the cap is the declared one (or the
        named fallback), and the cap is below the window."""
        from helm.seat_catalog import (FAMILIES, _CC_ASSUMED_WINDOW_MIRROR,
                                       launch_window)
        targets = [f for f, fam in FAMILIES.items() if fam.get("port")]
        self.assertIn("codex", targets)
        got = {f: self.declared(f)[:2] for f in targets}
        self.assertIn("codex", got)
        self.assertEqual(got, {
            f: (launch_window(FAMILIES[f], FAMILIES[f]["model"])
                or _CC_ASSUMED_WINDOW_MIRROR,
                FAMILIES[f].get("max_output_tokens")
                or pi.UNDECLARED_OUTPUT_TOKENS)
            for f in targets})
        self.assertEqual([f for f, (ctx, out) in got.items() if out >= ctx],
                         [], "maxTokens must sit below contextWindow")

    # --- the source generator and the verb ------------------------------

    def test_extension_source_without_limits_reads_the_catalog(self):
        """evalpin's callers pass only (seat, port, model): they get the
        catalog's numbers for that model, not a constant."""
        from helm.seat_catalog import FAMILIES
        src = pi.extension_source("codex", 8317, "gpt-5.6-sol")
        self.assertIn("contextWindow: %d,"
                      % FAMILIES["codex"]["model_context"]["gpt-5.6-sol"], src)

    def test_extension_source_refuses_what_provider_limits_refuses(self):
        with self.planted(max_context=1000, max_output_tokens=2000), \
                mock.patch.object(seat, "_seat_family",
                           return_value=("pifake", None)):
            with self.assertRaises(ValueError):
                pi.extension_source("pifake", 8999, "pifake-model")

    def test_the_verb_warns_on_stderr_for_an_unpinned_family(self):
        import io
        err = io.StringIO()
        with mock.patch.object(pi, "seat_port", return_value=(8330, None)), \
                mock.patch("sys.stdout", new_callable=io.StringIO), \
                mock.patch("sys.stderr", err):
            rc = pi.cmd_pi(["extension", "--seat", "ds4flash"])  # noqa: SEAT_NAME — the catalog FAMILY key of the one unpinned family
        self.assertEqual(rc, 0)
        self.assertIn("warning", err.getvalue())
        self.assertIn("pins no context window", err.getvalue())

    def test_the_verb_refuses_an_incoherent_family(self):
        import io
        err = io.StringIO()
        with self.planted(max_context=1000, max_output_tokens=2000), \
                mock.patch.object(seat, "_seat_family",
                           return_value=("pifake", None)), \
                mock.patch.object(pi, "seat_port", return_value=(8999, None)), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", err):
            rc = pi.cmd_pi(["extension", "--seat", "pifake"])
        self.assertEqual(rc, 1)
        self.assertIn("pifake", err.getvalue())
        self.assertNotIn("registerProvider", out.getvalue())


class CmdTest(unittest.TestCase):
    def test_status_never_fails_when_pi_is_absent_from_PATH(self):
        """Most machines have no pi. status must report, not raise."""
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(pi.cmd_pi(["status"]), 1)

    def test_a_bare_verb_prints_usage_and_refuses(self):
        self.assertEqual(pi.cmd_pi([]), 2)

    def test_help_exits_zero(self):
        self.assertEqual(pi.cmd_pi(["--help"]), 0)

    def test_an_unknown_flag_is_NAMED_not_ignored(self):
        """helm's law. A verb that silently drops `--sate codex` generates a
        shim for the DEFAULT seat while the operator believes otherwise."""
        self.assertNotEqual(pi.cmd_pi(["extension", "--sate", "codex"]), 0)

    def test_a_valued_flag_keeps_its_value(self):
        """Regression: the first draft pre-filtered the tail to dashed tokens
        before guarding it, which strips a valued flag's value — `--seat codex`
        arrived as a bare `--seat` and the verb refused its own valid input."""
        self.assertEqual(pi.cmd_pi(["extension", "--seat", "codex"]), 0)


if __name__ == "__main__":
    unittest.main()
