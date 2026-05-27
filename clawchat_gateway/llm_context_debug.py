from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

Visibility = str


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_id(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "unknown"))
    return (text or "unknown")[:80]


class ClawChatLlmContextDebug:
    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        default_snapshot_root: str | Path = ".clawchat-llm-context-debug",
    ) -> None:
        self._env = env if env is not None else os.environ
        self.enabled = _enabled(self._env.get("CLAWCHAT_LLM_CONTEXT_DEBUG"))
        self.capture_full_input = _enabled(self._env.get("CLAWCHAT_LLM_CONTEXT_CAPTURE_FULL_INPUT"))
        self.capture_output = _enabled(self._env.get("CLAWCHAT_LLM_CONTEXT_CAPTURE_OUTPUT"))
        self.reload_prompts = _enabled(self._env.get("CLAWCHAT_LLM_CONTEXT_RELOAD_PROMPTS"))
        self.snapshot_root = Path(
            self._env.get("CLAWCHAT_LLM_CONTEXT_SNAPSHOT_DIR") or default_snapshot_root,
        ).resolve()

    def write_snapshot(
        self,
        *,
        visibility: Visibility,
        trace: Mapping[str, Any],
        input: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
        output: Mapping[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> str | None:
        if not self.enabled:
            return None
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        target_root = self.snapshot_root / "hermes"
        runs_root = target_root / "runs"
        runs_root.mkdir(parents=True, exist_ok=True)
        file_path = runs_root / (
            f"{created_at.replace(':', '-').replace('.', '-')}-"
            f"{_safe_id(trace.get('messageId'))}-{_safe_id(trace.get('traceId'))}.json"
        )
        body = {
            "schemaVersion": 1,
            "source": "hermes",
            "visibility": visibility,
            "createdAt": created_at,
            "trace": dict(trace),
            "context": {
                "promptParts": list((context or {}).get("promptParts") or []),
                "tools": list((context or {}).get("tools") or []),
                "skills": list((context or {}).get("skills") or []),
            },
            "input": {
                "injectedPrompt": str(input.get("injectedPrompt") or ""),
                "eventText": str(input.get("eventText") or ""),
                "fullLlmInput": input.get("fullLlmInput"),
                "sections": list(input.get("sections") or []),
            },
            "output": {
                "rawModelOutput": (output or {}).get("rawModelOutput"),
                "streamChunks": list((output or {}).get("streamChunks") or []),
                "toolCalls": list((output or {}).get("toolCalls") or []),
                "toolResults": list((output or {}).get("toolResults") or []),
                "finalAssistantText": (output or {}).get("finalAssistantText"),
                "adapterFilteredText": (output or {}).get("adapterFilteredText"),
                "outboundClawChatMessage": (output or {}).get("outboundClawChatMessage"),
                "suppressed": bool((output or {}).get("suppressed") or False),
                "suppressionReason": (output or {}).get("suppressionReason"),
            },
            "warnings": warnings or [],
        }
        text = json.dumps(body, ensure_ascii=False, indent=2) + "\n"
        file_path.write_text(text, encoding="utf-8")
        (target_root / "latest.json").write_text(text, encoding="utf-8")
        return str(file_path)


llm_context_debug = ClawChatLlmContextDebug()


def write_llm_context_snapshot(
    *,
    visibility: Visibility,
    trace: Mapping[str, Any],
    input: Mapping[str, Any],
    context: Mapping[str, Any] | None = None,
    output: Mapping[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> str | None:
    return ClawChatLlmContextDebug().write_snapshot(
        visibility=visibility,
        trace=trace,
        input=input,
        context=context,
        output=output,
        warnings=warnings,
    )
