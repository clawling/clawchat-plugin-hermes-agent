import json
import unittest
from unittest.mock import AsyncMock, patch

from clawchat_gateway.api_client import ClawChatApiClient

BASE = "/v1/agents/me/orchestration"


def _client() -> ClawChatApiClient:
    return ClawChatApiClient(base_url="https://example.invalid", token="t", user_id="usr_x", device_id="dev_x")


class OrchestrationRoutesTest(unittest.IsolatedAsyncioTestCase):
    async def _capture(self, call):
        with patch.object(ClawChatApiClient, "_call_json", new=AsyncMock(return_value={"code": 0})) as spy:
            await call(_client())
        return spy.await_args

    async def test_list_agents_is_a_bare_get(self):
        args = await self._capture(lambda c: c.orch_list_agents())
        self.assertEqual(args.args, ("GET", f"{BASE}/agents"))

    async def test_get_agent_puts_the_id_in_the_path(self):
        args = await self._capture(lambda c: c.orch_get_agent("agt_x"))
        self.assertEqual(args.args, ("GET", f"{BASE}/agents/agt_x"))

    async def test_set_agent_behavior_sends_only_behavior(self):
        args = await self._capture(lambda c: c.orch_set_agent_behavior("agt_x", "be terse"))
        self.assertEqual(args.args, ("PATCH", f"{BASE}/agents/agt_x"))
        self.assertEqual(json.loads(args.kwargs["body"]), {"behavior": "be terse"})

    async def test_set_group_prompt_sends_only_description(self):
        args = await self._capture(lambda c: c.orch_set_group_prompt("cnv_x", "stay in character"))
        self.assertEqual(args.args, ("PATCH", f"{BASE}/groups/cnv_x"))
        self.assertEqual(json.loads(args.kwargs["body"]), {"description": "stay in character"})

    async def test_create_group_sends_title_and_agent_ids(self):
        args = await self._capture(lambda c: c.orch_create_group("Crew", ["agt_a", "agt_b"]))
        self.assertEqual(args.args, ("POST", f"{BASE}/groups"))
        self.assertEqual(json.loads(args.kwargs["body"]), {"title": "Crew", "agent_ids": ["agt_a", "agt_b"]})

    async def test_remove_group_member_is_a_delete_with_no_body(self):
        args = await self._capture(lambda c: c.orch_remove_group_member("cnv_x", "agt_a"))
        self.assertEqual(args.args, ("DELETE", f"{BASE}/groups/cnv_x/members/agt_a"))
        self.assertIsNone(args.kwargs.get("body"))

    async def test_group_agent_settings_omits_unset_fields(self):
        args = await self._capture(lambda c: c.orch_set_group_agent_settings("cnv_x", "agt_a", muted=True))
        self.assertEqual(args.args, ("PATCH", f"{BASE}/groups/cnv_x/agents/agt_a"))
        self.assertEqual(json.loads(args.kwargs["body"]), {"muted": True})

    async def test_group_agent_settings_keeps_explicit_false(self):
        args = await self._capture(
            lambda c: c.orch_set_group_agent_settings("cnv_x", "agt_a", muted=False, batch_delay_seconds=30)
        )
        self.assertEqual(json.loads(args.kwargs["body"]), {"muted": False, "batch_delay_seconds": 30})

    async def test_create_connect_code_sends_no_body(self):
        args = await self._capture(lambda c: c.orch_create_connect_code())
        self.assertEqual(args.args, ("POST", f"{BASE}/connect-codes"))
        self.assertIsNone(args.kwargs.get("body"))

    async def test_get_connect_code_url_encodes_the_code(self):
        args = await self._capture(lambda c: c.orch_get_connect_code("a/b"))
        self.assertEqual(args.args, ("GET", f"{BASE}/connect-codes/a%2Fb"))
