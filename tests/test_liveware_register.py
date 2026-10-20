import email.parser
import email.policy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from clawchat_gateway import plugin_tools, tools
from clawchat_gateway.api_client import ClawChatApiClient, ClawChatApiError

PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0, 0, 0, 0])
JPEG = bytes([0xFF, 0xD8, 0xFF, 0xE0, 0, 0])
WEBP = b"RIFF\x01\x02\x03\x04WEBPVP8 "

VIEW = {
    "id": "lw_1",
    "liveware_id": "lw1",
    "name": "Dash",
    "subtitle": "",
    "icon_url": "",
    "url": "https://x.example.com",
}


def _client() -> ClawChatApiClient:
    return ClawChatApiClient(base_url="https://example.invalid", token="t", user_id="usr_x", device_id="dev_x")


def _parse_multipart(body: bytes, content_type: str) -> dict:
    raw = b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(raw)
    parts = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        parts[name] = {
            "filename": part.get_filename(),
            "content_type": part.get_content_type(),
            "payload": part.get_payload(decode=True),
        }
    return parts


class LivewareApiClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_register_posts_multipart_to_liveware_and_keeps_app_shape(self):
        spy = AsyncMock(return_value={"liveware": dict(VIEW)})
        with patch.object(ClawChatApiClient, "_call_json", new=spy):
            result = await _client().register_app(name="Dash", app_id="lw1", url="https://x.example.com")
        args = spy.await_args
        self.assertEqual(args.args, ("POST", "/v1/agents/me/liveware"))
        ctype = args.kwargs["extra_headers"]["content-type"]
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        parts = _parse_multipart(args.kwargs["body"], ctype)
        self.assertEqual(parts["name"]["payload"], b"Dash")
        self.assertEqual(parts["liveware_id"]["payload"], b"lw1")
        self.assertEqual(parts["url"]["payload"], b"https://x.example.com")
        self.assertNotIn("subtitle", parts)
        self.assertNotIn("icon", parts)
        self.assertEqual(
            result,
            {
                "app": {
                    "id": "lw_1",
                    "app_id": "lw1",
                    "liveware_id": "lw1",
                    "name": "Dash",
                    "subtitle": "",
                    "icon_url": "",
                    "url": "https://x.example.com",
                }
            },
        )

    async def test_register_sends_subtitle_and_icon_parts(self):
        spy = AsyncMock(return_value={"liveware": dict(VIEW, subtitle="Sub", icon_url="https://cdn.example.com/i.png")})
        with patch.object(ClawChatApiClient, "_call_json", new=spy):
            result = await _client().register_app(
                name="Dash",
                app_id="lw1",
                url="https://x.example.com",
                subtitle="Sub",
                icon=(PNG, "icon.png", "image/png"),
            )
        ctype = spy.await_args.kwargs["extra_headers"]["content-type"]
        parts = _parse_multipart(spy.await_args.kwargs["body"], ctype)
        self.assertEqual(parts["subtitle"]["payload"], b"Sub")
        self.assertEqual(parts["icon"]["filename"], "icon.png")
        self.assertEqual(parts["icon"]["content_type"], "image/png")
        self.assertEqual(parts["icon"]["payload"], PNG)
        self.assertEqual(result["app"]["icon_url"], "https://cdn.example.com/i.png")
        self.assertEqual(result["app"]["subtitle"], "Sub")

    async def test_list_maps_liveware_to_apps(self):
        spy = AsyncMock(return_value={"liveware": [dict(VIEW)]})
        with patch.object(ClawChatApiClient, "_call_json", new=spy):
            result = await _client().list_apps()
        self.assertEqual(spy.await_args.args, ("GET", "/v1/agents/me/liveware"))
        self.assertEqual(result["apps"][0]["app_id"], "lw1")
        self.assertEqual(result["apps"][0]["liveware_id"], "lw1")
        self.assertEqual(result["apps"][0]["url"], "https://x.example.com")

    async def test_list_accepts_entries_without_optional_fields(self):
        data = {"liveware": [{"id": "lw_1", "liveware_id": "lw1", "name": "D", "url": "https://x"}]}
        with patch.object(ClawChatApiClient, "_call_json", new=AsyncMock(return_value=data)):
            app = (await _client().list_apps())["apps"][0]
        self.assertEqual((app["app_id"], app["subtitle"], app["icon_url"]), ("lw1", "", ""))

    async def test_list_returns_empty_only_when_server_says_so(self):
        with patch.object(ClawChatApiClient, "_call_json", new=AsyncMock(return_value={"liveware": []})):
            self.assertEqual(await _client().list_apps(), {"apps": []})

    async def test_list_rejects_malformed_responses(self):
        cases = {
            "missing list": {},
            "non-list": {"liveware": {"id": "lw_1"}},
            "non-object entry": {"liveware": ["lw1"]},
            "entry without liveware_id": {"liveware": [{"id": "lw_1", "name": "Dash", "url": "https://x"}]},
            "entry without id": {"liveware": [{"liveware_id": "lw1", "name": "Dash", "url": "https://x"}]},
            "entry without name": {"liveware": [{"id": "lw_1", "liveware_id": "lw1", "url": "https://x"}]},
            "entry without url": {"liveware": [{"id": "lw_1", "liveware_id": "lw1", "name": "Dash"}]},
            "non-string name": {"liveware": [{"id": "lw_1", "liveware_id": "lw1", "name": 7, "url": "https://x"}]},
            "non-string subtitle": {
                "liveware": [{"id": "lw_1", "liveware_id": "lw1", "name": "D", "url": "https://x", "subtitle": 1}]
            },
            "non-string icon_url": {
                "liveware": [{"id": "lw_1", "liveware_id": "lw1", "name": "D", "url": "https://x", "icon_url": None}]
            },
        }
        for label, data in cases.items():
            with self.subTest(label):
                with patch.object(ClawChatApiClient, "_call_json", new=AsyncMock(return_value=data)):
                    with self.assertRaises(ClawChatApiError) as ctx:
                        await _client().list_apps()
                self.assertEqual(ctx.exception.kind, "transport")

    async def test_register_rejects_response_without_liveware_entry(self):
        for data in ({}, {"liveware": {"id": "lw_1"}}):
            with self.subTest(data=data):
                with patch.object(ClawChatApiClient, "_call_json", new=AsyncMock(return_value=data)):
                    with self.assertRaises(ClawChatApiError) as ctx:
                        await _client().register_app(name="D", app_id="lw1", url="https://x")
                self.assertEqual(ctx.exception.kind, "transport")

    async def test_register_trims_subtitle_and_omits_blank(self):
        for given, expected in (("  Sub  ", b"Sub"), ("", None), ("   ", None)):
            with self.subTest(given=given):
                spy = AsyncMock(return_value={"liveware": dict(VIEW)})
                with patch.object(ClawChatApiClient, "_call_json", new=spy):
                    await _client().register_app(name="D", app_id="lw1", url="https://x", subtitle=given)
                ctype = spy.await_args.kwargs["extra_headers"]["content-type"]
                parts = _parse_multipart(spy.await_args.kwargs["body"], ctype)
                self.assertEqual(parts.get("subtitle", {}).get("payload"), expected)

    async def test_unregister_deletes_on_liveware_and_encodes_id(self):
        spy = AsyncMock(return_value={"deleted": True})
        with patch.object(ClawChatApiClient, "_call_json", new=spy):
            result = await _client().unregister_app("lw/1")
        self.assertEqual(spy.await_args.args, ("DELETE", "/v1/agents/me/liveware/lw%2F1"))
        self.assertEqual(result, {"deleted": True})


class _FakeClient:
    def __init__(self):
        self.calls = []

    async def register_app(self, **kw):
        self.calls.append(kw)
        return {"app": {"id": "lw_1", "app_id": kw["app_id"]}}


class LivewareRegisterToolTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = _FakeClient()
        p = patch.object(tools, "_build_client", return_value=(self.client, None))
        p.start()
        self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write(self, name: str, data: bytes) -> str:
        path = Path(self.tmp.name) / name
        path.write_bytes(data)
        return str(path)

    async def test_without_optional_fields_sends_none(self):
        await tools.register_app("Dash", "lw1", "https://x.example.com")
        self.assertEqual(
            self.client.calls[0],
            {"name": "Dash", "app_id": "lw1", "url": "https://x.example.com", "subtitle": None, "icon": None},
        )

    async def test_icon_is_sniffed_from_bytes(self):
        cases = [("icon.bin", PNG, "image/png"), ("a.jpg", JPEG, "image/jpeg"), ("a.webp", WEBP, "image/webp")]
        for name, data, mime in cases:
            with self.subTest(name):
                self.client.calls.clear()
                path = self._write(name, data)
                result = await tools.register_app("Dash", "lw1", "https://x", subtitle="Sub", icon_path=path)
                self.assertNotIn("error", result)
                self.assertEqual(self.client.calls[0]["icon"], (data, name, mime))
                self.assertEqual(self.client.calls[0]["subtitle"], "Sub")

    async def test_relative_icon_path_is_rejected(self):
        result = await tools.register_app("Dash", "lw1", "https://x", icon_path="icon.png")
        self.assertEqual(result["error"], "validation")
        self.assertIn("absolute", result["message"])
        self.assertEqual(self.client.calls, [])

    async def test_missing_icon_file_is_rejected(self):
        result = await tools.register_app("Dash", "lw1", "https://x", icon_path="/nonexistent/clawchat/icon.png")
        self.assertEqual(result["error"], "validation")
        self.assertEqual(self.client.calls, [])

    async def test_non_image_named_png_is_rejected(self):
        path = self._write("fake.png", b"<svg xmlns='http://www.w3.org/2000/svg'/>")
        result = await tools.register_app("Dash", "lw1", "https://x", icon_path=path)
        self.assertEqual(result["error"], "validation")
        self.assertIn("PNG, JPEG or WebP", result["message"])
        self.assertEqual(self.client.calls, [])

    async def test_icon_over_25mb_is_rejected(self):
        path = Path(self.tmp.name) / "big.png"
        with path.open("wb") as fh:
            fh.write(PNG)
            fh.truncate(25 * 1024 * 1024 + 1)
        result = await tools.register_app("Dash", "lw1", "https://x", icon_path=str(path))
        self.assertEqual(result["error"], "validation")
        self.assertIn("25MB", result["message"])
        self.assertEqual(self.client.calls, [])

    async def test_unreadable_icon_returns_validation_error(self):
        path = self._write("locked.png", PNG)
        with patch.object(Path, "read_bytes", side_effect=PermissionError("permission denied")):
            result = await tools.register_app("Dash", "lw1", "https://x", icon_path=path)
        self.assertEqual(result["error"], "validation")
        self.assertIn("cannot read", result["message"])
        self.assertEqual(self.client.calls, [])

    async def test_icon_removed_after_checks_returns_validation_error(self):
        path = self._write("gone.png", PNG)
        with patch.object(Path, "read_bytes", side_effect=FileNotFoundError("gone")):
            result = await tools.register_app("Dash", "lw1", "https://x", icon_path=path)
        self.assertEqual(result["error"], "validation")
        self.assertEqual(self.client.calls, [])

    async def test_icon_limit_reserves_multipart_overhead(self):
        path = Path(self.tmp.name) / "near-cap.png"
        with path.open("wb") as fh:
            fh.write(PNG)
            # Under 25MB on its own, but no room left for the multipart envelope.
            fh.truncate(25 * 1024 * 1024 - 1024)
        result = await tools.register_app("Dash", "lw1", "https://x", icon_path=str(path))
        self.assertEqual(result["error"], "validation")
        self.assertIn("too large", result["message"])
        self.assertIn("multipart overhead", result["message"])
        self.assertEqual(self.client.calls, [])

    async def test_subtitle_is_trimmed_before_validation_and_sending(self):
        await tools.register_app("Dash", "lw1", "https://x", subtitle="  " + "字" * 200 + "  ")
        self.assertEqual(self.client.calls[0]["subtitle"], "字" * 200)

    async def test_multi_line_subtitle_is_rejected(self):
        for subtitle in ("line one\nline two", "a\rb", "status\n", "\r\nstatus", "  status\n  "):
            with self.subTest(subtitle=subtitle):
                result = await tools.register_app("Dash", "lw1", "https://x", subtitle=subtitle)
                self.assertEqual(result["error"], "validation")
                self.assertIn("one line", result["message"])
        self.assertEqual(self.client.calls, [])

    async def test_blank_subtitle_is_not_sent(self):
        await tools.register_app("Dash", "lw1", "https://x", subtitle="   ")
        self.assertIsNone(self.client.calls[0]["subtitle"])

    async def test_subtitle_over_200_chars_is_rejected(self):
        result = await tools.register_app("Dash", "lw1", "https://x", subtitle="字" * 201)
        self.assertEqual(result["error"], "validation")
        self.assertIn("200", result["message"])
        self.assertEqual(self.client.calls, [])

    async def test_subtitle_of_exactly_200_chars_is_sent(self):
        await tools.register_app("Dash", "lw1", "https://x", subtitle="字" * 200)
        self.assertEqual(self.client.calls[0]["subtitle"], "字" * 200)


class _RecordingCtx:
    def __init__(self):
        self.tools = {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = {"schema": schema, "handler": handler}


class LivewareRegisterSchemaTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctx = _RecordingCtx()
        plugin_tools.register_tools(self.ctx)

    def test_schema_exposes_optional_subtitle_and_icon_path(self):
        schema = self.ctx.tools["clawchat_register_app"]["schema"]
        params = schema["parameters"]
        self.assertEqual(sorted(params["required"]), ["appId", "name", "url"])
        self.assertEqual(params["properties"]["subtitle"]["type"], "string")
        # Length is checked after trimming (_normalize_liveware_subtitle), not on the raw value.
        self.assertNotIn("maxLength", params["properties"]["subtitle"])
        self.assertIn("max 200 characters", params["properties"]["subtitle"]["description"])
        self.assertEqual(params["properties"]["iconPath"]["type"], "string")
        self.assertRegex(params["properties"]["iconPath"]["description"], r"(?i)absolute local path")
        self.assertIn("PNG, JPEG or WebP", params["properties"]["iconPath"]["description"])
        self.assertIn("subtitle", schema["description"])
        self.assertIn("iconPath", schema["description"])
        sub = params["properties"]["subtitle"]["description"]
        self.assertRegex(sub, r"(?i)one line")
        self.assertRegex(sub, r"(?i)omitted or empty subtitle keeps")
        self.assertRegex(sub, r"(?i)cannot clear")

    async def test_handler_forwards_subtitle_and_icon_path(self):
        mock = AsyncMock(return_value={"app": {}})
        with patch.object(tools, "register_app", mock):
            await self.ctx.tools["clawchat_register_app"]["handler"](
                {"name": "Dash", "appId": "lw1", "url": "https://x", "subtitle": "Sub", "iconPath": "/tmp/i.png"}
            )
        mock.assert_awaited_once_with(
            name="Dash", app_id="lw1", url="https://x", subtitle="Sub", icon_path="/tmp/i.png"
        )

    async def test_handler_omitted_optionals_arrive_as_none(self):
        mock = AsyncMock(return_value={"app": {}})
        with patch.object(tools, "register_app", mock):
            await self.ctx.tools["clawchat_register_app"]["handler"](
                {"name": "Dash", "appId": "lw1", "url": "https://x"}
            )
        mock.assert_awaited_once_with(name="Dash", app_id="lw1", url="https://x", subtitle=None, icon_path=None)
