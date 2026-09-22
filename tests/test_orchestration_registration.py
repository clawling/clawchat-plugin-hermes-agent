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
            self.assertEqual(self.ctx.tools[name]["schema"]["name"], name)

    def test_every_orchestration_tool_is_async(self):
        for name in EXPECTED:
            self.assertTrue(self.ctx.tools[name]["kw"].get("is_async"), name)

    def test_required_params_match_the_route_table(self):
        req = lambda n: self.ctx.tools[n]["schema"]["parameters"].get("required", [])
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
        return await handler(args)

    async def test_each_tool_calls_its_own_orchestrate_function_with_the_right_arguments(self):
        for tool_name, func_name, args, expect_args, expect_kwargs in WIRING_CASES:
            with self.subTest(tool_name):
                mock = AsyncMock(return_value={"code": 0, "data": {}})
                with patch.object(tools, func_name, mock):
                    await self._invoke(tool_name, dict(args))
                mock.assert_awaited_once_with(*expect_args, **expect_kwargs)

    async def test_muted_false_is_distinguishable_from_muted_omitted(self):
        mock = AsyncMock(return_value={"code": 0, "data": {}})
        with patch.object(tools, "orchestrate_set_group_agent_settings", mock):
            await self._invoke(
                "clawchat_orchestrate_set_group_agent_settings",
                {"conversationId": "conv-1", "agentId": "agent-1", "muted": False},
            )
        mock.assert_awaited_once_with(
            "conv-1", "agent-1", muted=False, reply_mode=None, batch_delay_seconds=None
        )

    async def test_omitted_settings_fields_arrive_as_none_not_false(self):
        mock = AsyncMock(return_value={"code": 0, "data": {}})
        with patch.object(tools, "orchestrate_set_group_agent_settings", mock):
            await self._invoke(
                "clawchat_orchestrate_set_group_agent_settings",
                {"conversationId": "conv-1", "agentId": "agent-1"},
            )
        mock.assert_awaited_once_with(
            "conv-1", "agent-1", muted=None, reply_mode=None, batch_delay_seconds=None
        )

    async def test_non_list_agent_ids_reaches_the_function_as_an_empty_list(self):
        mock = AsyncMock(return_value={"code": 0, "data": {}})
        with patch.object(tools, "orchestrate_create_group", mock):
            await self._invoke(
                "clawchat_orchestrate_create_group",
                {"title": "Squad", "agentIds": "not-a-list"},
            )
        mock.assert_awaited_once_with("Squad", [])

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
