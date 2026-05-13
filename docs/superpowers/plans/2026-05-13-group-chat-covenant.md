# Group Chat Covenant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add group-only ClawChat covenant injection through Hermes `MessageEvent.channel_prompt`, and make `group_mode` default to `"all"`.

**Architecture:** A small `clawchat_gateway.group_context` helper owns covenant formatting. `ClawChatAdapter` composes that prompt into `event.channel_prompt` only for inbound group messages, preserving the existing activation prompt. Config defaults and docs are updated together so behavior and references stay aligned.

**Tech Stack:** Python dataclasses/pytest, Hermes gateway `MessageEvent`, ClawChat adapter/config modules.

---

### Task 1: Group Covenant Prompt Helper

**Files:**
- Create: `clawchat_gateway/group_context.py`
- Test: `tests/test_group_context.py`

- [ ] **Step 1: Write failing helper tests**

Add `tests/test_group_context.py`:

```python
from clawchat_gateway.group_context import (
    build_group_channel_prompt,
    format_group_covenant_prompt,
)


def test_format_group_covenant_prompt_ignores_blank_text():
    assert format_group_covenant_prompt("") is None
    assert format_group_covenant_prompt("   ") is None


def test_format_group_covenant_prompt_wraps_text():
    prompt = format_group_covenant_prompt("群里陈平安是落魄山共和国的国王。它的位置在卡拉米星球的洪荒大陆上。")

    assert prompt == (
        "ClawChat group covenant:\n"
        "群里陈平安是落魄山共和国的国王。它的位置在卡拉米星球的洪荒大陆上。"
    )


def test_build_group_channel_prompt_uses_default_covenant(monkeypatch):
    monkeypatch.setattr(
        "clawchat_gateway.group_context.DEFAULT_GROUP_COVENANT",
        "群里陈平安是落魄山共和国的国王。它的位置在卡拉米星球的洪荒大陆上。",
    )

    prompt = build_group_channel_prompt()

    assert prompt == (
        "ClawChat group covenant:\n"
        "群里陈平安是落魄山共和国的国王。它的位置在卡拉米星球的洪荒大陆上。"
    )
```

- [ ] **Step 2: Run helper tests to verify RED**

Run: `pytest tests/test_group_context.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'clawchat_gateway.group_context'`.

- [ ] **Step 3: Add minimal helper implementation**

Create `clawchat_gateway/group_context.py`:

```python
from __future__ import annotations


DEFAULT_GROUP_COVENANT = (
    "When replying in a ClawChat group, treat the conversation as a shared space. "
    "Stay concise, respect the group topic, and avoid exposing hidden runtime context."
)


def format_group_covenant_prompt(covenant: str) -> str | None:
    text = str(covenant or "").strip()
    if not text:
        return None
    return f"ClawChat group covenant:\n{text}"


def build_group_channel_prompt() -> str | None:
    return format_group_covenant_prompt(DEFAULT_GROUP_COVENANT)
```

- [ ] **Step 4: Run helper tests to verify GREEN**

Run: `pytest tests/test_group_context.py -v`

Expected: PASS.

### Task 2: Adapter Group-Only Channel Prompt

**Files:**
- Modify: `clawchat_gateway/adapter.py`
- Test: `tests/test_adapter.py`

- [ ] **Step 1: Write failing adapter tests**

Append tests to `tests/test_adapter.py`:

```python
async def test_group_message_attaches_group_covenant_channel_prompt(monkeypatch):
    monkeypatch.setattr(
        "clawchat_gateway.adapter.build_group_channel_prompt",
        lambda: "ClawChat group covenant:\n群里陈平安是落魄山共和国的国王。它的位置在卡拉米星球的洪荒大陆上。",
    )
    adapter = _make_adapter()
    inbound = InboundMessage(
        chat_id="room1",
        chat_type="group",
        sender_id="u1",
        sender_name="alice",
        text="hello group",
        raw_message={"x": 1},
    )

    await adapter._handle_inbound(inbound)

    event = adapter.handled[0]
    assert event.channel_prompt == (
        "ClawChat group covenant:\n"
        "群里陈平安是落魄山共和国的国王。它的位置在卡拉米星球的洪荒大陆上。"
    )


async def test_direct_message_does_not_attach_group_covenant(monkeypatch):
    monkeypatch.setattr(
        "clawchat_gateway.adapter.build_group_channel_prompt",
        lambda: "ClawChat group covenant:\n群里陈平安是落魄山共和国的国王。它的位置在卡拉米星球的洪荒大陆上。",
    )
    adapter = _make_adapter()
    inbound = InboundMessage(
        chat_id="u1",
        chat_type="direct",
        sender_id="u1",
        sender_name="alice",
        text="hello dm",
        raw_message={"x": 1},
    )

    await adapter._handle_inbound(inbound)

    assert adapter.handled[0].channel_prompt is None


async def test_group_activation_prompt_appends_covenant(monkeypatch):
    monkeypatch.setattr(
        "clawchat_gateway.adapter.build_group_channel_prompt",
        lambda: "ClawChat group covenant:\n群里陈平安是落魄山共和国的国王。它的位置在卡拉米星球的洪荒大陆上。",
    )
    adapter = _make_adapter()
    inbound = InboundMessage(
        chat_id="room1",
        chat_type="group",
        sender_id="u1",
        sender_name="alice",
        text="clawchat 的激活码是 R4E1IW",
        raw_message={"x": 1},
    )

    await adapter._handle_inbound(inbound)

    event = adapter.handled[0]
    assert "ClawChat group covenant:" in event.channel_prompt
    assert "群里陈平安是落魄山共和国的国王" in event.channel_prompt
    assert "python -m clawchat_gateway.activate CODE" in event.channel_prompt
```

- [ ] **Step 2: Run adapter tests to verify RED**

Run: `pytest tests/test_adapter.py::test_group_message_attaches_group_covenant_channel_prompt tests/test_adapter.py::test_direct_message_does_not_attach_group_covenant tests/test_adapter.py::test_group_activation_prompt_appends_covenant -v`

Expected: FAIL because `clawchat_gateway.adapter` has no `build_group_channel_prompt` attribute or group messages do not set `channel_prompt`.

- [ ] **Step 3: Implement adapter composition**

Modify `clawchat_gateway/adapter.py`:

```python
from clawchat_gateway.group_context import build_group_channel_prompt
```

Add a helper method inside `ClawChatAdapter`:

```python
    def _compose_channel_prompt(self, inbound: InboundMessage) -> str | None:
        prompts: list[str] = []
        if inbound.chat_type == "group":
            group_prompt = build_group_channel_prompt()
            if group_prompt:
                prompts.append(group_prompt)
        if self._should_attach_activation_skill(inbound.text):
            prompts.append(_CLAWCHAT_SKILL_PROMPT)
        return "\n\n".join(prompts) or None
```

Replace the existing activation-only assignment in `_handle_inbound` with:

```python
        channel_prompt = self._compose_channel_prompt(inbound)
        if self._should_attach_activation_skill(inbound.text):
            event.auto_skill = "clawchat"
        if channel_prompt:
            event.channel_prompt = channel_prompt
```

- [ ] **Step 4: Run adapter tests to verify GREEN**

Run: `pytest tests/test_adapter.py::test_group_message_attaches_group_covenant_channel_prompt tests/test_adapter.py::test_direct_message_does_not_attach_group_covenant tests/test_adapter.py::test_group_activation_prompt_appends_covenant -v`

Expected: PASS.

### Task 3: Default Group Mode Is All

**Files:**
- Modify: `clawchat_gateway/config.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_inbound.py`

- [ ] **Step 1: Write failing config/default tests**

Update expected defaults in `tests/test_config.py` so `test_config_defaults` asserts:

```python
assert cfg.group_mode == "all"
```

Add to `tests/test_inbound.py`:

```python
def test_default_group_mode_accepts_group_message_without_mention():
    cfg = ClawChatConfig(websocket_url="wss://x", token="t", user_id="bot")
    envelope = {
        "chat_id": "room1",
        "chat_type": "group",
        "sender": {"id": "u1", "nick_name": "alice"},
        "payload": {
            "message": {
                "context": {"mentions": []},
                "fragments": [{"kind": "text", "text": "hello"}],
            }
        },
    }

    inbound = parse_inbound_message(envelope, cfg)

    assert inbound is not None
    assert inbound.chat_type == "group"
```

- [ ] **Step 2: Run default tests to verify RED**

Run: `pytest tests/test_config.py::test_config_defaults tests/test_inbound.py::test_default_group_mode_accepts_group_message_without_mention -v`

Expected: FAIL because `group_mode` defaults to `"mention"` and unmentioned group messages are filtered.

- [ ] **Step 3: Change config default**

Modify `clawchat_gateway/config.py`:

```python
group_mode: str = "all"
```

and in `from_platform_config`:

```python
group_mode=_get_env("CLAWCHAT_GROUP_MODE")
or _get_config_value(extra, "group_mode", "all"),
```

- [ ] **Step 4: Run default tests to verify GREEN**

Run: `pytest tests/test_config.py::test_config_defaults tests/test_inbound.py::test_default_group_mode_accepts_group_message_without_mention -v`

Expected: PASS.

### Task 4: Documentation Sync

**Files:**
- Modify: `docs/config.md`
- Modify: `docs/adapter.md`
- Modify: `docs/inbound.md`
- Modify: `docs/architecture.md`
- Modify: `docs/tests.md`
- Modify: `README.md`
- Modify if needed: `install.md`

- [ ] **Step 1: Update docs for channel prompt and group mode**

Update docs to state:

- `group_mode` default is `"all"`;
- `"mention"` remains available as opt-in filtering;
- group covenant is injected through `MessageEvent.channel_prompt`;
- `channel_prompt` is an ephemeral system prompt path and is not the WebSocket protocol;
- direct messages do not receive group covenant text.

- [ ] **Step 2: Search for stale default references**

Run: `rg -n "group_mode.*mention|groupMode.*mention|default.*mention|channel_prompt|group covenant|group_mode.*all|groupMode.*all" README.md install.md docs tests clawchat_gateway`

Expected: No stale statement says the default group mode is mention-only.

### Task 5: Full Verification

**Files:**
- No new files.

- [ ] **Step 1: Run focused tests**

Run: `pytest tests/test_group_context.py tests/test_adapter.py tests/test_config.py tests/test_inbound.py -v`

Expected: PASS.

- [ ] **Step 2: Run full test suite**

Run: `pytest`

Expected: PASS.

- [ ] **Step 3: Review worktree**

Run: `git status --short`

Expected: only intended implementation/docs files changed, plus pre-existing untracked `AGENTS.md`.
