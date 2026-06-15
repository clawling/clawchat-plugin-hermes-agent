from __future__ import annotations

from clawchat_gateway.api_client import ClawChatApiClient, build_plugin_report_payload


def test_build_plugin_report_payload_includes_all_fields() -> None:
    payload = build_plugin_report_payload(
        device_id="hermes-abc",
        platform="hermes",
        plugin_version="0.14.0-24",
        runtime_name="python",
        runtime_version="3.12.3",
    )
    assert payload == {
        "device_id": "hermes-abc",
        "platform": "hermes",
        "plugin_version": "0.14.0-24",
        "runtime_name": "python",
        "runtime_version": "3.12.3",
    }


async def test_report_plugin_posts_to_expected_paths(monkeypatch) -> None:
    client = ClawChatApiClient(base_url="http://x", token="", device_id="hermes-abc")
    calls: list[tuple[str, str]] = []

    async def fake_call(method, path, *, body=None, extra_headers=None):
        calls.append((method, path))
        return {}

    monkeypatch.setattr(client, "_call_json", fake_call)
    await client.report_plugin(
        device_id="hermes-abc", platform="hermes", plugin_version="1",
        runtime_name="python", runtime_version="3.12", authenticated=False,
    )
    await client.report_plugin(
        device_id="hermes-abc", platform="hermes", plugin_version="1",
        runtime_name="python", runtime_version="3.12", authenticated=True,
    )
    assert calls == [
        ("POST", "/v1/agents/plugin-report"),
        ("POST", "/v1/agents/me/plugin-report"),
    ]
