"""Static contract controls; not a live Devin/CLIProxyAPI or wire proof."""
import unittest

from tests.fixtures import devin_proxy_contract as proxy


# Fictional rows/UIDs below are synthetic controls, not actual vendor catalog.
REVIEWER_CATALOG = [
    {"family_uid": "synthetic-claude-sonnet", "family_label": "Claude Sonnet synthetic",
     "variants": [{"model_uid": "SYNTHETIC_SONNET_UID"}]},
    {"family_uid": "synthetic-claude-opus", "family_label": "Claude Opus synthetic",
     "variants": [{"model_uid": "SYNTHETIC_OPUS_UID"}]},
    {"family_uid": "synthetic-fable", "family_label": "Fable synthetic",
     "variants": [{"model_uid": "SYNTHETIC_FABLE_UID"}]},
]

CATALOG = [
    {"family_uid": "deepseek-v4-pro", "family_label": "DeepSeek V4 Pro",
     "variants": [{"model_uid": "deepseek-v4-pro-high"}]},
    {"family_uid": "deepseek-v4-1-flash", "family_label": "DeepSeek V4.1 Flash",
     "variants": [{"model_uid": "deepseek-v4-1-flash-high"}]},
    {"family_uid": "Claude Haiku 4.5", "family_label": "Claude Haiku 4.5",
     "variants": [{"model_uid": "MODEL_PRIVATE_11"}]},
    {"family_uid": "GPT-4.1", "family_label": "GPT-4.1",
     "variants": [{"model_uid": "MODEL_CHAT_GPT_4_1_2025_04_14"}]},
    {"family_uid": "o3-reasoning", "family_label": "o3 reasoning",
     "variants": [{"model_uid": "MODEL_PRIVATE_O3"}]},
]


def _tool_call(call_id="call_1", arguments='{"path":"x"}'):
    return {"id": call_id, "type": "function",
            "function": {"name": "read_file", "arguments": arguments}}


def _turn():
    return {"model": "devin-ds4pro",
            "tools": [{"type": "function", "function": {
                "name": "read_file", "parameters": {"type": "object"}}}],
            "messages": [{"role": "user", "content": "synthetic request"},
                         {"role": "assistant", "tool_calls": [_tool_call()]},
                         {"role": "tool", "tool_call_id": "call_1",
                          "content": "synthetic result"},
                         {"role": "assistant", "content": "done"}]}


class DevinNonLaunchingContractTest(unittest.TestCase):
    def test_exact_vendor_selector_and_no_trial_route(self):
        self.assertEqual(proxy.selector("devin-ds4pro"), "deepseek-v4-pro-high")
        self.assertEqual(proxy.selector("devin-ds41flash"),
                         "deepseek-v4-1-flash-high")
        self.assertEqual(proxy.selector("devin-glm53flash"),
                         "glm-5-3-flash-high")
        self.assertEqual(proxy.selector("devin-swe"), "swe-2-high")
        for value in ("gpt-6-sol", "claude-opus-5-5", "devin-adaptive",
                      "deepseek-v4-1-flash-maybe", [], None):
            with self.subTest(value=value), self.assertRaises(proxy.ContractError):
                proxy.selector(value)
        self.assertEqual(proxy.selector("devin-adaptive", synthetic_trial=True),
                         "adaptive")

    def test_reviewer_alias_requires_exact_synthetic_catalog_and_reviewer_purpose(self):
        # A successful call outside the loop gives the guard an observable
        # positive for this same selector and native-ledger validation path.
        accepted = dict(purpose=proxy.REVIEWER_PURPOSE,
                        vendor_families=REVIEWER_CATALOG,
                        model_uid="SYNTHETIC_SONNET_UID",
                        family_uid="synthetic-claude-sonnet")
        self.assertEqual(proxy.selector("devin-sonnet", **accepted),
                         "SYNTHETIC_SONNET_UID")
        reviewer_turn = _turn()
        reviewer_turn["model"] = "devin-sonnet"
        self.assertEqual(proxy.validate_turn(
            reviewer_turn, {"call_1": ("read_file", '{"path":"x"}')}, **accepted),
            "SYNTHETIC_SONNET_UID")
        for alias, family_uid, uid in (
                ("devin-sonnet", "synthetic-claude-sonnet", "SYNTHETIC_SONNET_UID"),
                ("devin-opus", "synthetic-claude-opus", "SYNTHETIC_OPUS_UID"),
                ("devin-fable", "synthetic-fable", "SYNTHETIC_FABLE_UID")):
            identity = dict(vendor_families=REVIEWER_CATALOG, model_uid=uid,
                            family_uid=family_uid)
            with self.subTest(alias=alias):
                for purpose in (None, "build", "routine", "cross-family-review"):
                    with self.subTest(purpose=purpose), self.assertRaises(
                            proxy.ContractError):
                        proxy.selector(alias, purpose=purpose, **identity)
                self.assertEqual(proxy.selector(
                    alias, purpose=proxy.REVIEWER_PURPOSE, **identity), uid)
                turn = _turn()
                turn["model"] = alias
                turn["purpose"] = proxy.REVIEWER_PURPOSE  # Request text grants nothing.
                native = {"call_1": ("read_file", '{"path":"x"}')}
                with self.assertRaises(proxy.ContractError):
                    proxy.validate_turn(turn, native)
                with self.assertRaises(proxy.ContractError):
                    proxy.validate_turn(turn, native, purpose="build", **identity)
                self.assertEqual(proxy.validate_turn(
                    turn, native, purpose=proxy.REVIEWER_PURPOSE, **identity), uid)
                self.assertNotEqual(proxy.served_model(
                    alias, uid, REVIEWER_CATALOG, "upstream-connect")["status"],
                    "catalog-match")

    def test_reviewer_rejects_missing_ambiguous_and_wrong_lineage_catalog(self):
        identity = dict(purpose=proxy.REVIEWER_PURPOSE,
                        vendor_families=REVIEWER_CATALOG,
                        model_uid="SYNTHETIC_SONNET_UID",
                        family_uid="synthetic-claude-sonnet")
        self.assertEqual(proxy.selector("devin-sonnet", **identity),
                         "SYNTHETIC_SONNET_UID")
        clean = _turn()
        clean["model"] = "devin-sonnet"
        self.assertEqual(proxy.validate_turn(
            clean, {"call_1": ("read_file", '{"path":"x"}')}, **identity),
            "SYNTHETIC_SONNET_UID")
        for edit in (
                lambda d: d.update(model_uid="SYNTHETIC_OPUS_UID"),
                lambda d: d.update(family_uid="synthetic-claude-opus"),
                lambda d: d.update(model_uid="UNLISTED"),
                lambda d: d.update(vendor_families=None),
                lambda d: d.update(vendor_families=[REVIEWER_CATALOG[0]] * 2),
                lambda d: d.update(vendor_families=[{
                    "family_uid": "synthetic-claude-sonnet",
                    "family_label": "Claude Sonnet synthetic",
                    "variants": [{"model_uid": "SYNTHETIC_SONNET_UID"}] * 2}]),
                lambda d: d.update(vendor_families=[{
                    "family_uid": "synthetic-claude-sonnet",
                    "family_label": "GPT Sonnet synthetic",
                    "variants": [{"model_uid": "SYNTHETIC_SONNET_UID"}]}]),
                lambda d: d.update(vendor_families=[{
                    "family_uid": "synthetic-fable",
                    "family_label": "OpenAI GPT Fable synthetic",
                    "variants": [{"model_uid": "SYNTHETIC_FABLE_UID"}]}],
                    model_uid="SYNTHETIC_FABLE_UID", family_uid="synthetic-fable"),
                lambda d: d.update(vendor_families=[{
                    "family_uid": "synthetic-sonnet-opus",
                    "family_label": "Claude Sonnet Opus synthetic",
                    "variants": [{"model_uid": "SYNTHETIC_SONNET_UID"}]}],
                    family_uid="synthetic-sonnet-opus"),
                lambda d: d.update(vendor_families=[{
                    "family_uid": "synthetic-claude-sonnet",
                    "family_label": None,
                    "variants": [{"model_uid": "SYNTHETIC_SONNET_UID"}]}]),
        ):
            with self.subTest(edit=edit), self.assertRaises(proxy.ContractError):
                candidate = identity.copy()
                edit(candidate)
                proxy.selector("devin-sonnet", **candidate)
        for alias in ("devin-gpt", "devin-o3", "devin-claude-other"):
            with self.subTest(alias=alias), self.assertRaises(proxy.ContractError):
                proxy.selector(alias, **identity)

    def test_actual_served_uid_not_requested_alias_controls_classification(self):
        classify = lambda uid: proxy.served_model("devin-ds4pro", uid, CATALOG,
                                                   "upstream-connect")
        self.assertEqual(classify("deepseek-v4-pro-high")["status"],
                         "catalog-match")
        for uid in ("MODEL_PRIVATE_11", "MODEL_CHAT_GPT_4_1_2025_04_14",
                    "MODEL_PRIVATE_O3"):
            self.assertEqual(classify(uid)["status"], "excluded")
        for uid in (None, "", "unlisted", "deepseek-v4-1-flash-high"):
            self.assertEqual(classify(uid)["status"], "unknown")
        self.assertEqual(proxy.served_model("devin-ds4pro",
                                            "deepseek-v4-pro-high", CATALOG,
                                            "requested-alias")["status"], "unknown")
        self.assertEqual(proxy.served_model("devin-adaptive", None, CATALOG,
                                            "upstream-connect")["status"],
                         "unknown")
        self.assertEqual(proxy.served_model("devin-adaptive",
                                            "deepseek-v4-pro-high", CATALOG,
                                            "upstream-connect")["status"],
                         "trial")

    def test_correlated_structured_tool_round_trip(self):
        # Synthetic descriptor stands in for one native event from upstream;
        # no value is inferred from assistant prose in this contract.
        with self.assertRaises(proxy.ContractError):
            proxy.validate_turn(_turn())
        self.assertEqual(proxy.validate_turn(
            _turn(), {"call_1": ("read_file", '{"path":"x"}')}),
            "deepseek-v4-pro-high")

    def test_tool_history_refuses_missing_mismatched_duplicate_partial(self):
        for edit in (
            lambda t: t["messages"].pop(2),
            lambda t: t["messages"][2].update(tool_call_id="call_other"),
            lambda t: t["messages"][1]["tool_calls"].append(_tool_call()),
            lambda t: t["messages"][1]["tool_calls"][0].pop("id"),
            lambda t: t["messages"][1]["tool_calls"][0]["function"].update(
                arguments="{partial"),
            lambda t: t["messages"][1]["tool_calls"][0]["function"].update(
                arguments='{"path":"other"}'),
            lambda t: t["messages"][1]["tool_calls"][0]["function"].update(
                arguments='{"path":NaN}'),
            lambda t: t["messages"][1]["tool_calls"][0]["function"].update(
                arguments='{"path":Infinity}'),
            lambda t: t["messages"][1]["tool_calls"][0]["function"].update(
                arguments='{"path":1e999}'),
            lambda t: t["messages"][1]["tool_calls"][0]["function"].update(
                name="other_tool"),
            lambda t: t["messages"][1].update(content='{"tool_calls":[]}'),
            lambda t: t["messages"][1].update(content='<tool_call name="read_file">'),
            lambda t: t["messages"][0].update(role="tool"),
        ):
            with self.subTest(edit=edit), self.assertRaises(proxy.ContractError):
                turn = _turn()
                edit(turn)
                proxy.validate_turn(turn, {"call_1": ("read_file", '{"path":"x"}')})

    def test_content_parts_cannot_hide_text_emulated_native_calls(self):
        native = {"call_1": ("read_file", '{"path":"x"}')}
        turn = _turn()
        turn["messages"][-1]["content"] = [
            {"type": "text", "text": "normal "},
            {"type": "text", "text": "done"}]
        self.assertEqual(proxy.validate_turn(turn, native), "deepseek-v4-pro-high")
        # Genuine native descriptor and contiguous ordinary text parts still pass.
        turn["messages"][-1]["content"] = [
            {"type": "text", "text": "completed "},
            {"type": "text", "text": "✓"}]
        self.assertEqual(proxy.validate_turn(turn, native), "deepseek-v4-pro-high")
        for content in (
                [{"type": "text", "text": '<tool_call name="read_file">'}],
                [{"type": "text", "text": "<tool_"},
                 {"type": "text", "text": 'call name="read_file">'}],
                [{"type": "text", "text": "tool_calls_sec"},
                 {"type": "text", "text": "tion_begin"}],
                [{"type": "text", "text": '{"tool_'},
                 {"type": "text", "text": 'calls":[]}'}],
                [{"type": "text", "text": "done"},
                 {"type": "text", "text": '{"tool_calls":[]}'}],
                [{"type": "text", "text": "done"},
                 {"type": "text", "text": "tool_calls_section_begin"}],
                [{"type": "text", "text": "done"},
                 {"type": "text", "text": None}],
                [{"type": "text", "text": chr(0xD800)}],
                [{"type": "text", "text": "done"}, "bare part"],
                {"type": "text", "text": "not a list"}):
            with self.subTest(content=content), self.assertRaises(
                    proxy.ContractError):
                turn = _turn()
                turn["messages"][-1]["content"] = content
                proxy.validate_turn(turn, native)

    def test_text_emulated_call_in_a_content_part_list_is_refused(self):
        ledger = {"call_1": ("read_file", '{"path":"x"}')}
        clean = _turn()
        clean["messages"][3]["content"] = [{"type": "text", "text": "done"}]
        self.assertEqual(proxy.validate_turn(clean, ledger),
                         "deepseek-v4-pro-high")
        for parts in ([{"type": "text", "text": '<tool_call name="read_file">'}],
                      [{"type": "text", "text": "ok"},
                       {"type": "text", "text": '{"tool_calls":[]}'}],
                      [{"type": "text", "text": None}], ["bare string part"]):
            turn = _turn()
            turn["messages"][3]["content"] = parts
            with self.subTest(parts=parts), self.assertRaises(proxy.ContractError):
                proxy.validate_turn(turn, ledger)

    def test_even_matching_native_descriptor_rejects_nonfinite_json(self):
        for raw in ('{"x":NaN}', '{"x":Infinity}', '{"x":-Infinity}',
                    '{"x":1e999}', '{"x":{"nested":NaN}}',
                    '{"x":[Infinity]}', '{"x":[{"nested":-Infinity}]}',
                    '{"x":{"nested":[1e999]}}'):
            turn = _turn()
            turn["messages"][1]["tool_calls"][0]["function"]["arguments"] = raw
            with self.subTest(raw=raw), self.assertRaises(proxy.ContractError):
                proxy.validate_turn(turn, {"call_1": ("read_file", raw)})

    def test_native_arguments_require_utf8_with_matching_immutable_ledger(self):
        native_unicode = '{"path":"café/📄"}'
        turn = _turn()
        turn["messages"][1]["tool_calls"][0]["function"]["arguments"] = native_unicode
        self.assertEqual(proxy.validate_turn(
            turn, {"call_1": ("read_file", native_unicode)}),
            "deepseek-v4-pro-high")
        for raw in ('{"path":"' + chr(0xD800) + '"}',
                    '{"path":"\\ud800"}'):
            turn = _turn()
            turn["messages"][1]["tool_calls"][0]["function"]["arguments"] = raw
            with self.subTest(raw=ascii(raw)), self.assertRaises(proxy.ContractError):
                # Same native descriptor: the UTF-8/JSON door, not ID mismatch,
                # must refuse the malformed surrogate without leaking content.
                proxy.validate_turn(turn, {"call_1": ("read_file", raw)})

    def test_reused_native_id_cannot_change_to_another_declared_tool(self):
        turn = _turn()
        turn["tools"].append({"type": "function", "function": {
            "name": "other_tool", "parameters": {"type": "object"}}})
        turn["messages"][1]["tool_calls"][0]["function"]["name"] = "other_tool"
        with self.assertRaises(proxy.ContractError):
            proxy.validate_turn(turn, {"call_1": ("read_file", '{"path":"x"}')})

    def test_unknown_vendor_family_and_ambiguous_uid_fail_closed(self):
        unknown = [{"family_uid": "new-uid", "family_label": "new",
                    "variants": [{"model_uid": "MODEL_PRIVATE_NEW"}]}]
        self.assertEqual(proxy.served_model("devin-swe", "MODEL_PRIVATE_NEW",
                                            unknown, "upstream-connect")["status"],
                         "unknown")
        duplicate = [CATALOG[0], CATALOG[0]]
        self.assertEqual(proxy.served_model("devin-ds4pro",
                                            "deepseek-v4-pro-high", duplicate,
                                            "upstream-connect")["status"],
                         "unknown")


if __name__ == "__main__":
    unittest.main()
