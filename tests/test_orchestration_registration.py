import unittest

from clawchat_gateway import plugin_tools

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
