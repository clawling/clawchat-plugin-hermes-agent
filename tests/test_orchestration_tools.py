import unittest
from unittest.mock import AsyncMock, patch

from clawchat_gateway import tools


ORIGIN = dict(conversation_id="cnv_owner", initiator_id="usr_owner", purpose="test")


class _FakeClient:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def _record(*a, **kw):
            self.calls.append((name, a, kw))
            return {"code": 0, "data": {}}
        return _record


class OrchestrationToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = _FakeClient()
        self.patcher = patch.object(tools, "_build_client", return_value=(self.client, None))
        self.patcher.start()
        origin = patch("clawchat_gateway.host_tools_guard.orchestrate_handler_origin",
                       return_value={key: value for key, value in ORIGIN.items() if key != "purpose"})
        origin.start()
        self.addCleanup(origin.stop)
        self.addCleanup(self.patcher.stop)

    async def test_list_agents_passes_the_envelope_through_unchanged(self):
        result = await tools.orchestrate_list_agents(purpose="test")
        self.assertEqual(result, {"code": 0, "data": {}})
        self.assertEqual(self.client.calls[0][0], "orch_list_agents")

    async def test_behavior_over_3000_runes_is_rejected_locally(self):
        result = await tools.orchestrate_set_agent_behavior("agt_x", "x" * 3001, purpose="test")
        self.assertNotEqual(result.get("code"), 0)
        self.assertEqual(self.client.calls, [])

    async def test_behavior_at_exactly_3000_runes_is_sent(self):
        await tools.orchestrate_set_agent_behavior("agt_x", "x" * 3000, purpose="test")
        self.assertEqual(self.client.calls[0][0], "orch_set_agent_behavior")

    async def test_wrong_prefix_agent_id_is_sent_to_the_server(self):
        # 29002 folds "not yours" and "does not exist" on purpose; a local
        # prefix check would leak the distinction back to the model.
        await tools.orchestrate_get_agent("cnv_oops", purpose="test")
        self.assertEqual(self.client.calls[0][0], "orch_get_agent")

    async def test_empty_agent_ids_is_rejected_locally(self):
        result = await tools.orchestrate_create_group("Crew", [], purpose="test")
        self.assertNotEqual(result.get("code"), 0)
        self.assertEqual(self.client.calls, [])

    async def test_title_over_60_runes_is_rejected_locally(self):
        result = await tools.orchestrate_create_group("t" * 61, ["agt_a"], purpose="test")
        self.assertNotEqual(result.get("code"), 0)
        self.assertEqual(self.client.calls, [])

    async def test_settings_with_no_field_set_is_rejected_locally(self):
        result = await tools.orchestrate_set_group_agent_settings("cnv_x", "agt_a", purpose="test")
        self.assertNotEqual(result.get("code"), 0)
        self.assertEqual(self.client.calls, [])

    async def test_muted_false_alone_counts_as_a_field(self):
        await tools.orchestrate_set_group_agent_settings("cnv_x", "agt_a", muted=False, purpose="test")
        self.assertEqual(self.client.calls[0][2]["muted"], False)

    async def test_bad_reply_mode_is_rejected_locally(self):
        result = await tools.orchestrate_set_group_agent_settings("cnv_x", "agt_a", reply_mode="sometimes", purpose="test")
        self.assertNotEqual(result.get("code"), 0)
        self.assertEqual(self.client.calls, [])

    async def test_batch_delay_out_of_range_is_rejected_locally(self):
        self.assertNotEqual(
            (await tools.orchestrate_set_group_agent_settings("cnv_x", "agt_a", batch_delay_seconds=0, purpose="test")).get("code"), 0
        )
        self.assertNotEqual(
            (await tools.orchestrate_set_group_agent_settings("cnv_x", "agt_a", batch_delay_seconds=3601, purpose="test")).get("code"), 0
        )
        self.assertEqual(self.client.calls, [])

    async def test_a_config_error_short_circuits_before_any_call(self):
        with patch.object(tools, "_build_client", return_value=(None, {"code": 1, "message": "not activated"})):
            self.assertEqual(await tools.orchestrate_list_agents(purpose="test"), {"code": 1, "message": "not activated"})

    async def test_list_groups_reaches_the_client(self):
        await tools.orchestrate_list_groups(purpose="test")
        self.assertEqual(self.client.calls[0], ("orch_list_groups", (), {"origin": ORIGIN}))

    async def test_get_group_reaches_the_client_with_the_conversation_id(self):
        await tools.orchestrate_get_group("cnv_x", purpose="test")
        self.assertEqual(self.client.calls[0], ("orch_get_group", ("cnv_x",), {"origin": ORIGIN}))

    async def test_set_group_prompt_reaches_the_client_with_the_description(self):
        await tools.orchestrate_set_group_prompt("cnv_x", "be helpful", purpose="test")
        self.assertEqual(self.client.calls[0], ("orch_set_group_prompt", ("cnv_x", "be helpful"), {"origin": ORIGIN}))

    async def test_description_over_3000_runes_is_rejected_locally(self):
        result = await tools.orchestrate_set_group_prompt("cnv_x", "x" * 3001, purpose="test")
        self.assertNotEqual(result.get("code"), 0)
        self.assertEqual(self.client.calls, [])

    async def test_description_at_exactly_3000_runes_is_sent(self):
        await tools.orchestrate_set_group_prompt("cnv_x", "x" * 3000, purpose="test")
        self.assertEqual(self.client.calls[0][0], "orch_set_group_prompt")

    async def test_title_at_1_rune_is_sent(self):
        await tools.orchestrate_create_group("t", ["agt_a"], purpose="test")
        self.assertEqual(self.client.calls[0], ("orch_create_group", ("t", ["agt_a"]), {"origin": ORIGIN}))

    async def test_title_at_60_runes_is_sent(self):
        await tools.orchestrate_create_group("t" * 60, ["agt_a"], purpose="test")
        self.assertEqual(self.client.calls[0], ("orch_create_group", ("t" * 60, ["agt_a"]), {"origin": ORIGIN}))

    async def test_add_group_member_reaches_the_client_with_both_ids(self):
        await tools.orchestrate_add_group_member("cnv_x", "agt_a", purpose="test")
        self.assertEqual(self.client.calls[0], ("orch_add_group_member", ("cnv_x", "agt_a"), {"origin": ORIGIN}))

    async def test_remove_group_member_reaches_the_client_with_both_ids(self):
        await tools.orchestrate_remove_group_member("cnv_x", "agt_a", purpose="test")
        self.assertEqual(self.client.calls[0], ("orch_remove_group_member", ("cnv_x", "agt_a"), {"origin": ORIGIN}))

    async def test_batch_delay_seconds_at_boundaries_is_sent(self):
        await tools.orchestrate_set_group_agent_settings("cnv_x", "agt_a", batch_delay_seconds=1, purpose="test")
        await tools.orchestrate_set_group_agent_settings("cnv_x", "agt_a", batch_delay_seconds=3600, purpose="test")
        self.assertEqual(
            [c[0] for c in self.client.calls],
            ["orch_set_group_agent_settings", "orch_set_group_agent_settings"],
        )
        self.assertEqual(self.client.calls[0][2]["batch_delay_seconds"], 1)
        self.assertEqual(self.client.calls[1][2]["batch_delay_seconds"], 3600)

    async def test_create_connect_code_reaches_the_client(self):
        await tools.orchestrate_create_connect_code(purpose="test")
        self.assertEqual(self.client.calls[0], ("orch_create_connect_code", (), {"origin": ORIGIN}))

    async def test_get_connect_code_reaches_the_client_with_the_code(self):
        await tools.orchestrate_get_connect_code("cnv_code", purpose="test")
        self.assertEqual(self.client.calls[0], ("orch_get_connect_code", ("cnv_code",), {"origin": ORIGIN}))
