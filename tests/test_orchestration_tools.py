import unittest
from unittest.mock import AsyncMock, patch

from clawchat_gateway import tools


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
        self.addCleanup(self.patcher.stop)

    async def test_list_agents_passes_the_envelope_through_unchanged(self):
        result = await tools.orchestrate_list_agents()
        self.assertEqual(result, {"code": 0, "data": {}})
        self.assertEqual(self.client.calls[0][0], "orch_list_agents")

    async def test_a_non_zero_business_code_is_not_an_error(self):
        # 21003 (owner has not enabled 云端编排) arrives as HTTP 200. The tool
        # must hand it to the model verbatim, not raise and not rewrite it.
        client = _FakeClient()

        async def denied(*a, **kw):
            return {"code": 21003, "message": "orchestration disabled"}

        client.orch_list_agents = denied
        with patch.object(tools, "_build_client", return_value=(client, None)):
            self.assertEqual(await tools.orchestrate_list_agents(), {"code": 21003, "message": "orchestration disabled"})

    async def test_behavior_over_3000_runes_is_rejected_locally(self):
        result = await tools.orchestrate_set_agent_behavior("agt_x", "x" * 3001)
        self.assertNotEqual(result.get("code"), 0)
        self.assertEqual(self.client.calls, [])

    async def test_behavior_at_exactly_3000_runes_is_sent(self):
        await tools.orchestrate_set_agent_behavior("agt_x", "x" * 3000)
        self.assertEqual(self.client.calls[0][0], "orch_set_agent_behavior")

    async def test_wrong_prefix_agent_id_is_sent_to_the_server(self):
        # 29002 folds "not yours" and "does not exist" on purpose; a local
        # prefix check would leak the distinction back to the model.
        await tools.orchestrate_get_agent("cnv_oops")
        self.assertEqual(self.client.calls[0][0], "orch_get_agent")

    async def test_empty_agent_ids_is_rejected_locally(self):
        result = await tools.orchestrate_create_group("Crew", [])
        self.assertNotEqual(result.get("code"), 0)
        self.assertEqual(self.client.calls, [])

    async def test_title_over_60_runes_is_rejected_locally(self):
        result = await tools.orchestrate_create_group("t" * 61, ["agt_a"])
        self.assertNotEqual(result.get("code"), 0)
        self.assertEqual(self.client.calls, [])

    async def test_settings_with_no_field_set_is_rejected_locally(self):
        result = await tools.orchestrate_set_group_agent_settings("cnv_x", "agt_a")
        self.assertNotEqual(result.get("code"), 0)
        self.assertEqual(self.client.calls, [])

    async def test_muted_false_alone_counts_as_a_field(self):
        await tools.orchestrate_set_group_agent_settings("cnv_x", "agt_a", muted=False)
        self.assertEqual(self.client.calls[0][2]["muted"], False)

    async def test_bad_reply_mode_is_rejected_locally(self):
        result = await tools.orchestrate_set_group_agent_settings("cnv_x", "agt_a", reply_mode="sometimes")
        self.assertNotEqual(result.get("code"), 0)
        self.assertEqual(self.client.calls, [])

    async def test_batch_delay_out_of_range_is_rejected_locally(self):
        self.assertNotEqual(
            (await tools.orchestrate_set_group_agent_settings("cnv_x", "agt_a", batch_delay_seconds=0)).get("code"), 0
        )
        self.assertNotEqual(
            (await tools.orchestrate_set_group_agent_settings("cnv_x", "agt_a", batch_delay_seconds=3601)).get("code"), 0
        )
        self.assertEqual(self.client.calls, [])

    async def test_a_config_error_short_circuits_before_any_call(self):
        with patch.object(tools, "_build_client", return_value=(None, {"code": 1, "message": "not activated"})):
            self.assertEqual(await tools.orchestrate_list_agents(), {"code": 1, "message": "not activated"})
