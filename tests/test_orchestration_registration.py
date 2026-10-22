import unittest
from unittest.mock import AsyncMock, patch

from clawchat_gateway import plugin_tools, tools

EXPECTED = [
    "clawchat_orchestrate_list_agents",
    "clawchat_orchestrate_get_agent",
    "clawchat_orchestrate_set_agent_behavior",
    "clawchat_orchestrate_list_groups",
    "clawchat_orchestrate_get_group",
    "clawchat_orchestrate_set_group_prompt",
    "clawchat_orchestrate_create_group",
    "clawchat_orchestrate_add_group_member",
    "clawchat_orchestrate_remove_group_member",
    "clawchat_orchestrate_set_group_agent_settings",
    "clawchat_orchestrate_create_connect_code",
    "clawchat_orchestrate_get_connect_code",
]


class _RecordingCtx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, "kw": kw}


class OrchestrationRegistrationTest(unittest.TestCase):
    def setUp(self):
        self.ctx = _RecordingCtx()
        plugin_tools.register_tools(self.ctx)

    def test_all_twelve_are_registered_in_the_clawchat_toolset(self):
        for name in EXPECTED:
            self.assertIn(name, self.ctx.tools, name)
            self.assertEqual(self.ctx.tools[name]["toolset"], "clawchat")

    def test_schema_name_matches_the_registered_name(self):
        for name in EXPECTED:
            schema = self.ctx.tools[name]["schema"]
            self.assertEqual(schema["name"], name)
            self.assertEqual(schema["parameters"]["required"][-1], "purpose")
            self.assertEqual(schema["parameters"]["properties"]["purpose"]["maxLength"], 60)
            self.assertTrue(schema["description"].startswith("OWNER-ONLY. "))

    def test_every_orchestration_tool_is_async(self):
        for name in EXPECTED:
            self.assertTrue(self.ctx.tools[name]["kw"].get("is_async"), name)

    def test_required_params_match_the_route_table(self):
        req = lambda n: self.ctx.tools[n]["schema"]["parameters"].get("required", [])[:-1]
        self.assertEqual(req("clawchat_orchestrate_list_agents"), [])
        self.assertEqual(req("clawchat_orchestrate_get_agent"), ["agentId"])
        self.assertEqual(req("clawchat_orchestrate_set_agent_behavior"), ["agentId", "behavior"])
        self.assertEqual(req("clawchat_orchestrate_set_group_prompt"), ["conversationId", "description"])
        self.assertEqual(req("clawchat_orchestrate_create_group"), ["title", "agentIds"])
        self.assertEqual(req("clawchat_orchestrate_add_group_member"), ["conversationId", "agentId"])
        self.assertEqual(req("clawchat_orchestrate_remove_group_member"), ["conversationId", "agentId"])
        self.assertEqual(req("clawchat_orchestrate_set_group_agent_settings"), ["conversationId", "agentId"])
        self.assertEqual(req("clawchat_orchestrate_create_connect_code"), [])
        self.assertEqual(req("clawchat_orchestrate_get_connect_code"), ["code"])

    def test_reply_mode_enum_is_exactly_all_and_mention(self):
        props = self.ctx.tools["clawchat_orchestrate_set_group_agent_settings"]["schema"]["parameters"]["properties"]
        self.assertEqual(props["replyMode"]["enum"], ["all", "mention"])
        self.assertEqual(props["batchDelaySeconds"]["minimum"], 1)
        self.assertEqual(props["batchDelaySeconds"]["maximum"], 3600)

    def test_no_tool_exposes_a_nickname_bio_or_title_write(self):
        # The backend accepts only `behavior` and only `description`; offering
        # the other fields would produce a call that succeeds and changes
        # nothing, which is worse than no tool at all.
        forbidden = {"nickname", "bio", "title"}
        for name in ("clawchat_orchestrate_set_agent_behavior", "clawchat_orchestrate_set_group_prompt"):
            props = set(self.ctx.tools[name]["schema"]["parameters"]["properties"])
            self.assertEqual(props & forbidden, set(), name)

    def test_stage_guidance_says_where_room_rules_go(self):
        # The stage sentence is quoted verbatim by the app's orchestrator texts
        # and the OpenClaw plugin; an edited copy would teach a different line.
        stage_line = ("In a group whose description makes it a stage, you are one of the "
                      "players: pick up the other characters' lines without waiting to be called.")
        behavior = self.ctx.tools["clawchat_orchestrate_set_agent_behavior"]["schema"]["description"]
        group = self.ctx.tools["clawchat_orchestrate_set_group_prompt"]["schema"]["description"]
        self.assertIn(f'"{stage_line}"', behavior)
        self.assertIn(f'"{stage_line}"', group)
        self.assertIn("its own rules go nowhere else", group)

    def test_stage_descriptions_keep_the_pointer_after_the_owner_only_prefix(self):
        # The owner-only notice precedes the existing short stage pointer.
        for name in ("clawchat_orchestrate_set_agent_behavior", "clawchat_orchestrate_set_group_prompt"):
            description = self.ctx.tools[name]["schema"]["description"]
            prefix = ("OWNER-ONLY. Raises an approval card on the owner's phone. "
                      "Never call it in a group or for someone else. ")
            self.assertTrue(description.startswith(prefix), name)
            description = description[len(prefix):]
            self.assertTrue(description.startswith("Stage/room speaking rules go "), name)
            first_sentence = description[: description.index(". ") + 1]
            self.assertLessEqual(len(first_sentence), 200, name)
            self.assertIn("group description", first_sentence, name)
            self.assertIn("stage sentence", first_sentence, name)


    def test_group_prompt_carries_the_three_team_patterns(self):
        # Verbatim from the app's orchestration skill (Team patterns); the
        # app side checks the plugin text against its own copy, so a reworded
        # template line would teach a different group.
        group = self.ctx.tools["clawchat_orchestrate_set_group_prompt"]["schema"]["description"]
        lines = [
            'Members: <hub> — splits the job, assigns each piece, sends each delivery to <checker>, reports to <owner>; <executor> — does the piece it was given; <checker> — checks a delivery against the brief and answers with the verdict alone: "pass" or numbered fixes, never the fix itself',
            "Flow (each hand-off @-mentions the next seat): <owner> → <hub> → <executor> → <hub> → <checker> → <hub> → <owner>",
            "Stop: <hub> reports to <owner> once every piece has passed; a piece that fails twice goes to <owner> as it stands",
            'Members: <writer> — drafts, revises, posts the final to <owner>; <reviewer> — reads each draft against the brief and answers with the verdict alone: "pass" or at most five numbered notes, never a rewrite',
            "Flow (each hand-off @-mentions the next seat): <owner> → <writer> → <reviewer> → <writer>, at most two rounds of notes",
            'Stop: on "pass" or after the second round, <writer> posts the final and any open notes to <owner>',
            "Members: <hub> — gives each researcher one angle, waits for every answer, writes one synthesis for <owner>; <researcher> — answers its own angle once, with sources",
            "Flow (each hand-off @-mentions the next seat): <owner> → <hub> → each <researcher> → <hub> → <owner>",
            "Stop: each researcher delivers once and never answers another; the synthesis ends the run, and a second pass happens only when <hub> or <owner> asks",
        ]
        description_lines = group.split("\n")
        for line in lines:
            self.assertIn(line, description_lines)

    def test_team_patterns_keep_the_four_rules(self):
        # The four rules are the part that stops a working group from talking
        # forever; a shorter wording may not drop any of them.
        group = self.ctx.tools["clawchat_orchestrate_set_group_prompt"]["schema"]["description"]
        # 1. one seat replies to everything, the rest only on a mention, set
        #    with the hard control
        self.assertIn("exactly one seat replies to everything (replyMode all)", group)
        self.assertIn("every other seat only when @-mentioned (replyMode mention)", group)
        self.assertIn("clawchat_orchestrate_set_group_agent_settings", group)
        # 2. real @-mentions, not typed ones
        self.assertIn('every hand-off is a real @-mention, never a typed "@name"', group)
        # 3. verdict alone, no "received" first
        self.assertIn('never a "received" first', group)
        # 4. a judgeable Stop line
        self.assertIn("the Stop line stays", group)
        # Seats live in the group description, not in a member's behavior.
        self.assertIn("never in a member's behavior", group)
        self.assertIn("once the owner agrees", group)

class _KeySpyDict(dict):
    """A dict that records every key looked up via `.get()`.

    Used to check what the dispatch table actually reads out of `args`,
    without hand-listing those keys a second time in the test — the handler
    itself is the source of truth for what it reads.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.accessed: set[str] = set()

    def get(self, key, default=None):
        self.accessed.add(key)
        return super().get(key, default)


# Each row: (tool name, name of the `tools.orchestrate_*` function it must
# call, a representative `args` dict a model would send, the exact
# positional args, and the exact keyword args that must reach the
# underlying function). This is the ground truth for the camelCase-args ->
# snake_case-params wiring in `plugin_tools._ORCHESTRATION_HANDLERS`.
WIRING_CASES = [
    ("clawchat_orchestrate_list_agents", "orchestrate_list_agents", {}, (), {}),
    ("clawchat_orchestrate_get_agent", "orchestrate_get_agent",
     {"agentId": "agent-1"}, ("agent-1",), {}),
    ("clawchat_orchestrate_set_agent_behavior", "orchestrate_set_agent_behavior",
     {"agentId": "agent-1", "behavior": "be kind"}, ("agent-1", "be kind"), {}),
    ("clawchat_orchestrate_list_groups", "orchestrate_list_groups", {}, (), {}),
    ("clawchat_orchestrate_get_group", "orchestrate_get_group",
     {"conversationId": "conv-1"}, ("conv-1",), {}),
    ("clawchat_orchestrate_set_group_prompt", "orchestrate_set_group_prompt",
     {"conversationId": "conv-1", "description": "be helpful"}, ("conv-1", "be helpful"), {}),
    ("clawchat_orchestrate_create_group", "orchestrate_create_group",
     {"title": "Squad", "agentIds": ["agent-1", "agent-2"]}, ("Squad", ["agent-1", "agent-2"]), {}),
    ("clawchat_orchestrate_add_group_member", "orchestrate_add_group_member",
     {"conversationId": "conv-1", "agentId": "agent-1"}, ("conv-1", "agent-1"), {}),
    ("clawchat_orchestrate_remove_group_member", "orchestrate_remove_group_member",
     {"conversationId": "conv-1", "agentId": "agent-1"}, ("conv-1", "agent-1"), {}),
    ("clawchat_orchestrate_set_group_agent_settings", "orchestrate_set_group_agent_settings",
     {"conversationId": "conv-1", "agentId": "agent-1", "muted": False,
      "replyMode": "mention", "batchDelaySeconds": 15},
     ("conv-1", "agent-1"),
     {"muted": False, "reply_mode": "mention", "batch_delay_seconds": 15}),
    ("clawchat_orchestrate_create_connect_code", "orchestrate_create_connect_code", {}, (), {}),
    ("clawchat_orchestrate_get_connect_code", "orchestrate_get_connect_code",
     {"code": "CODE123"}, ("CODE123",), {}),
]


class OrchestrationHandlerWiringTest(unittest.IsolatedAsyncioTestCase):
    """Guards the highest-risk surface of the diff: the dispatch table's
    free-standing camelCase key literals actually reaching the right
    `tools.orchestrate_*` function under the right snake_case parameter
    names. The schema tests above only check what the model is offered;
    these check what the backend-facing function actually receives.
    """

    def setUp(self):
        self.ctx = _RecordingCtx()
        plugin_tools.register_tools(self.ctx)

    async def _invoke(self, tool_name, args):
        handler = self.ctx.tools[tool_name]["handler"]
        args["purpose"] = "test"
        return await handler(args)

    async def test_each_tool_calls_its_own_orchestrate_function_with_the_right_arguments(self):
        for tool_name, func_name, args, expect_args, expect_kwargs in WIRING_CASES:
            with self.subTest(tool_name):
                mock = AsyncMock(return_value={"code": 0, "data": {}})
                with patch.object(tools, func_name, mock):
                    await self._invoke(tool_name, dict(args))
                mock.assert_awaited_once_with(*expect_args, purpose="test", **expect_kwargs)

    async def test_muted_false_is_distinguishable_from_muted_omitted(self):
        mock = AsyncMock(return_value={"code": 0, "data": {}})
        with patch.object(tools, "orchestrate_set_group_agent_settings", mock):
            await self._invoke(
                "clawchat_orchestrate_set_group_agent_settings",
                {"conversationId": "conv-1", "agentId": "agent-1", "muted": False},
            )
        mock.assert_awaited_once_with(
            "conv-1", "agent-1", muted=False, reply_mode=None, batch_delay_seconds=None, purpose="test"
        )

    async def test_omitted_settings_fields_arrive_as_none_not_false(self):
        mock = AsyncMock(return_value={"code": 0, "data": {}})
        with patch.object(tools, "orchestrate_set_group_agent_settings", mock):
            await self._invoke(
                "clawchat_orchestrate_set_group_agent_settings",
                {"conversationId": "conv-1", "agentId": "agent-1"},
            )
        mock.assert_awaited_once_with(
            "conv-1", "agent-1", muted=None, reply_mode=None, batch_delay_seconds=None, purpose="test"
        )

    async def test_non_list_agent_ids_reaches_the_function_as_an_empty_list(self):
        mock = AsyncMock(return_value={"code": 0, "data": {}})
        with patch.object(tools, "orchestrate_create_group", mock):
            await self._invoke(
                "clawchat_orchestrate_create_group",
                {"title": "Squad", "agentIds": "not-a-list"},
            )
        mock.assert_awaited_once_with("Squad", [], purpose="test")

    async def test_handler_only_reads_args_keys_declared_in_its_own_schema(self):
        # This is the check that makes a silent rename impossible to
        # reintroduce: whatever keys a handler actually pulls out of `args`
        # must be a subset of the keys the schema tells the model it can
        # send. A key present on only one side (e.g. one renamed to
        # `agentIds` while the other still reads `agent_ids`) fails here.
        for tool_name, func_name, sample_args, _, _ in WIRING_CASES:
            with self.subTest(tool_name):
                props = set(self.ctx.tools[tool_name]["schema"]["parameters"]["properties"])
                spy_args = _KeySpyDict(sample_args)
                mock = AsyncMock(return_value={"code": 0, "data": {}})
                with patch.object(tools, func_name, mock):
                    await self._invoke(tool_name, spy_args)
                self.assertTrue(spy_args.accessed <= props, (tool_name, spy_args.accessed, props))
