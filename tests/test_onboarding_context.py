"""Onboarding telemetry attached to /v1/agents/connect and /connect/check.

Values mirror the connection wiki manifest vocabulary (agent_kind = wiki
`match.agent`, lane = "self", matched_install = the official page slug).
"""

from __future__ import annotations

from clawchat_gateway.onboarding import (
    AGENT_KIND,
    LANE,
    MATCHED_INSTALL,
    RECONNECT_GUIDE_URL,
    START_GUIDE_URL,
    host_os,
    onboarding_context,
    wiki_version,
)


def test_constants_match_the_wiki_vocabulary() -> None:
    assert AGENT_KIND == "hermes"
    assert LANE == "self"
    assert MATCHED_INSTALL == "install/official-hermes"
    assert START_GUIDE_URL == "https://agent-connection.clawling.com/start.md"
    assert RECONNECT_GUIDE_URL == "https://agent-connection.clawling.com/reconnect.md"


def test_host_os_maps_platform_system_onto_the_wiki_enum() -> None:
    assert host_os("Windows") == "windows"
    assert host_os("Darwin") == "macos"
    assert host_os("Linux") == "linux"
    assert host_os("FreeBSD") == ""


def test_wiki_version_is_trimmed_and_capped() -> None:
    assert wiki_version(lambda _name: " 2026.09.16 ") == "2026.09.16"
    assert wiki_version(lambda _name: "") == ""
    assert len(wiki_version(lambda _name: "x" * 100)) == 40


def test_onboarding_context_omits_empty_fields() -> None:
    ctx = onboarding_context(system="Darwin", env_get=lambda _name: "")
    assert ctx == {
        "agent_kind": "hermes",
        "os": "macos",
        "lane": "self",
        "matched_install": "install/official-hermes",
    }
    ctx = onboarding_context(system="Plan9", env_get=lambda _name: "2026.09.16")
    assert "os" not in ctx
    assert ctx["wiki_version"] == "2026.09.16"
