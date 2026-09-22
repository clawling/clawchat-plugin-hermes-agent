import io
import json
import unittest
from unittest.mock import AsyncMock, patch
from urllib.error import URLError

from clawchat_gateway import api_client, tools
from clawchat_gateway.api_client import ClawChatApiClient, ClawChatApiError

BASE = "/v1/agents/me/orchestration"


def _client() -> ClawChatApiClient:
    return ClawChatApiClient(base_url="https://example.invalid", token="t", user_id="usr_x", device_id="dev_x")


class _FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class OrchestrationRoutesTest(unittest.IsolatedAsyncioTestCase):
    async def _capture(self, call):
        # The twelve orch_* methods route through `_call_envelope`, NOT
        # `_call_json` — see C1: `_call_json`'s raise-on-nonzero-code and
        # unwrap-data behavior would swallow the business envelope.
        with patch.object(ClawChatApiClient, "_call_envelope", new=AsyncMock(return_value={"code": 0})) as spy:
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


class OrchestrationEnvelopeReachesTheModelTest(unittest.IsolatedAsyncioTestCase):
    """C1 regression: drives the REAL transport (a stubbed `urlopen`), not a
    patched `_call_json`/`_call_envelope` and not a fake client. Every
    orchestration response is HTTP 200 with the business outcome in the
    envelope `{code, msg, data}`; the envelope must reach the tool-layer
    return value unchanged so the model can read `code` itself.
    """

    def setUp(self):
        self.client = _client()
        self.patcher = patch.object(tools, "_build_client", return_value=(self.client, None))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def test_a_non_zero_business_code_reaches_the_model_at_top_level(self):
        def fake_urlopen(request, timeout=None):
            return _FakeResponse(
                json.dumps(
                    {"code": 21003, "msg": "operation forbidden by owner", "data": None}
                ).encode("utf-8")
            )

        with patch.object(api_client, "urlopen", fake_urlopen):
            result = await tools.orchestrate_list_agents()

        self.assertEqual(result.get("code"), 21003)

    async def test_success_data_is_not_mistaken_for_the_top_level_code(self):
        def fake_urlopen(request, timeout=None):
            return _FakeResponse(
                json.dumps(
                    {"code": 0, "msg": "", "data": {"code": "AAAAAA", "expires_at": "2026-01-01T00:00:00Z"}}
                ).encode("utf-8")
            )

        with patch.object(api_client, "urlopen", fake_urlopen):
            result = await tools.orchestrate_create_connect_code()

        self.assertEqual(result.get("code"), 0)
        self.assertEqual(result["data"]["code"], "AAAAAA")

    async def test_a_genuine_transport_failure_still_raises(self):
        def boom(request, timeout=None):
            raise URLError("connection refused")

        with patch.object(api_client, "urlopen", boom):
            with self.assertRaises(ClawChatApiError):
                await self.client.orch_list_agents()
