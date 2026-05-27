from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any


__all__ = [
    "resolve_clawchat_memory_path",
    "ensure_clawchat_memory_target_safe",
    "delete_clawchat_memory_file",
    "read_clawchat_memory_file",
    "search_clawchat_memory",
    "write_clawchat_memory_body",
    "edit_clawchat_memory_body",
    "write_clawchat_metadata",
]

METADATA_START = "<!-- clawchat:metadata:start -->"
METADATA_END = "<!-- clawchat:metadata:end -->"
_LINE_ENDING_RE = re.compile(r"\r\n?|\n")
_METADATA_VALUE_LINE_BREAK_RE = re.compile(r"[\r\n]+")
_METADATA_FIELDS_BY_TARGET = {
    "owner": {
        "updated_at",
        "agent_id",
        "agent_owner_id",
        "agent_nickname",
        "agent_avatar_url",
        "agent_bio",
        "agent_owner_nickname",
        "agent_owner_avatar_url",
        "agent_owner_bio",
        "agent_behavior",
    },
    "user": {
        "updated_at",
        "id",
        "nickname",
        "avatar_url",
        "bio",
        "profile_type",
    },
    "group": {
        "updated_at",
        "group_id",
        "group_type",
        "group_title",
        "group_description",
        "group_owner_id",
        "group_owner_nickname",
        "group_owner_profile_type",
        "group_created_at",
        "participant_ids",
    },
}


def _validate_file_id(target_id: str) -> None:
    if not target_id:
        raise ValueError("target_id is required")
    if target_id in {".", ".."}:
        raise ValueError("target_id must be a single safe file id")
    if "/" in target_id or "\\" in target_id or "\x00" in target_id:
        raise ValueError("target_id must not contain path separators or NUL")
    if any(ord(char) < 32 or ord(char) == 127 for char in target_id):
        raise ValueError("target_id must not contain control characters")


def _relative_target_path(target_type: str, target_id: str) -> Path:
    _validate_file_id(target_id)
    if target_type == "owner":
        if target_id != "owner":
            raise ValueError("owner target requires target_id='owner'")
        return Path("owner.md")
    if target_type == "user":
        return Path("users") / f"{target_id}.md"
    if target_type == "group":
        return Path("groups") / f"{target_id}.md"
    raise ValueError(f"unsupported ClawChat memory target_type: {target_type}")


def resolve_clawchat_memory_path(root: str | Path, target_type: str, target_id: str) -> Path:
    root_path = Path(root)
    root_resolved = root_path.resolve()
    candidate = _candidate_path(root_path, target_type, target_id)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("resolved ClawChat memory path is outside the memory root") from exc
    return resolved


def _candidate_path(root: Path, target_type: str, target_id: str) -> Path:
    return root / _relative_target_path(target_type, target_id)


def _lstat_if_exists(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def ensure_clawchat_memory_target_safe(root: str | Path, target_type: str, target_id: str) -> Path:
    root_path = Path(root)
    candidate = _candidate_path(root_path, target_type, target_id)

    if target_type in {"user", "group"}:
        parent_name = "users" if target_type == "user" else "groups"
        parent = root_path / parent_name
        parent_stat = _lstat_if_exists(parent)
        if parent_stat is not None:
            if stat.S_ISLNK(parent_stat.st_mode):
                raise ValueError(f"{parent_name}/ must not be a symlink")
            if not stat.S_ISDIR(parent_stat.st_mode):
                raise ValueError(f"{parent_name}/ must be a directory")

    target_stat = _lstat_if_exists(candidate)
    if target_stat is not None:
        if stat.S_ISLNK(target_stat.st_mode):
            raise ValueError("ClawChat memory target must not be a symlink")
        if not stat.S_ISREG(target_stat.st_mode):
            raise ValueError("ClawChat memory target must be a regular file")

    path = resolve_clawchat_memory_path(root, target_type, target_id)
    root_resolved = root_path.resolve()
    try:
        path.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("resolved ClawChat memory path is outside the memory root") from exc
    return path


def _normalize_metadata_value(value: str) -> str:
    return _METADATA_VALUE_LINE_BREAK_RE.sub(" ", value)


def _normalize_line_endings(value: str) -> str:
    return _LINE_ENDING_RE.sub("\n", value)


def _find_metadata_block(content: str) -> tuple[int, int, list[str]] | None:
    offset = 0
    start_offset: int | None = None
    metadata_lines: list[str] = []
    in_block = False

    for line in content.splitlines(keepends=True):
        stripped = line[:-1] if line.endswith("\n") else line
        next_offset = offset + len(line)
        if not in_block:
            if stripped == METADATA_START:
                start_offset = offset
                in_block = True
                metadata_lines = []
        elif stripped == METADATA_END:
            return start_offset or 0, next_offset, metadata_lines
        else:
            metadata_lines.append(stripped)
        offset = next_offset

    return None


def _parse_metadata(lines: list[str]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value[1:] if value.startswith(" ") else value
    return metadata


def _strip_metadata_separator(body: str) -> str:
    if body.startswith("\n"):
        return body[1:]
    return body


def _format_metadata_block(metadata: dict[str, str]) -> str:
    lines = [METADATA_START]
    for key, value in metadata.items():
        lines.append(f"{key}: {_normalize_metadata_value(value)}")
    lines.append(METADATA_END)
    return "\n".join(lines)


def _filter_metadata_for_target(target_type: str, metadata: dict[str, str]) -> dict[str, str]:
    allowed = _METADATA_FIELDS_BY_TARGET.get(target_type)
    if allowed is None:
        raise ValueError(f"unsupported ClawChat memory target_type: {target_type}")
    return {key: value for key, value in metadata.items() if key in allowed}


def _parse_clawchat_memory_content(content: str) -> dict[str, Any]:
    normalized = _normalize_line_endings(content)
    block = _find_metadata_block(normalized)
    if block is None:
        return {"metadata": {}, "body": normalized}

    start, end, metadata_lines = block
    body = normalized[:start] + _strip_metadata_separator(normalized[end:])
    return {"metadata": _parse_metadata(metadata_lines), "body": body}


def _replace_metadata_block(content: str, metadata: dict[str, str]) -> str:
    normalized = _normalize_line_endings(content)
    block_text = _format_metadata_block(metadata)
    block = _find_metadata_block(normalized)
    if block is None:
        if not normalized:
            return block_text
        return f"{block_text}\n\n{normalized}"

    start, end, _metadata_lines = block
    suffix = normalized[end:]
    if suffix and not suffix.startswith("\n"):
        suffix = "\n" + suffix
    return normalized[:start] + block_text + suffix


def _replace_body(content: str, body: str) -> str:
    normalized = _normalize_line_endings(content)
    body = _normalize_line_endings(body)
    block = _find_metadata_block(normalized)
    if block is None:
        return body

    start, end, _metadata_lines = block
    block_text = normalized[start:end].rstrip("\n")
    if body:
        return f"{block_text}\n\n{body}"
    return block_text


def _read_existing_content(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as file:
            file.write(content)
        tmp_path.replace(path)
    except BaseException:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def read_clawchat_memory_file(root: str | Path, target_type: str, target_id: str) -> dict:
    path = ensure_clawchat_memory_target_safe(root, target_type, target_id)
    target = {"target_type": target_type, "target_id": target_id, "path": str(path)}
    if not path.exists():
        return {**target, "exists": False, "content": "", "metadata": {}, "body": ""}

    content = _normalize_line_endings(path.read_text(encoding="utf-8"))
    parsed = _parse_clawchat_memory_content(content)
    return {**target, "exists": True, "content": content, **parsed}


def _normalize_search_targets(target_types: list[str] | tuple[str, ...] | None) -> list[str]:
    if target_types is None:
        return ["owner", "user", "group"]
    if not target_types:
        raise ValueError("target_types must not be empty")
    invalid = [target_type for target_type in target_types if target_type not in {"owner", "user", "group"}]
    if invalid:
        raise ValueError(f"unsupported ClawChat memory target_type: {', '.join(invalid)}")
    return list(target_types)


def _safe_search_targets(root: Path, target_type: str) -> list[tuple[str, str]]:
    if target_type == "owner":
        return [("owner", "owner")]
    dirname = "users" if target_type == "user" else "groups"
    directory = root / dirname
    directory_stat = _lstat_if_exists(directory)
    if directory_stat is None:
        return []
    if stat.S_ISLNK(directory_stat.st_mode):
        raise ValueError(f"{dirname}/ must not be a symlink")
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise ValueError(f"{dirname}/ must be a directory")
    targets: list[tuple[str, str]] = []
    for child in sorted(directory.iterdir(), key=lambda item: item.name):
        if child.suffix != ".md":
            continue
        target_id = child.stem
        _validate_file_id(target_id)
        ensure_clawchat_memory_target_safe(root, target_type, target_id)
        targets.append((target_type, target_id))
    return targets


def _first_matching_line(value: str, query_lower: str) -> str | None:
    for line in _normalize_line_endings(value).split("\n"):
        if query_lower in line.lower():
            return line if len(line) <= 300 else f"{line[:297]}..."
    return None


def _build_search_match(target_type: str, target_id: str, memory: dict, query_lower: str) -> dict | None:
    matched_fields: list[str] = []
    snippets: list[str] = []
    metadata_text = "\n".join(f"{key}: {value}" for key, value in memory["metadata"].items())
    metadata_snippet = _first_matching_line(metadata_text, query_lower)
    if metadata_snippet is not None:
        matched_fields.append("metadata")
        snippets.append(metadata_snippet)
    body_snippet = _first_matching_line(memory["body"], query_lower)
    if body_snippet is not None:
        matched_fields.append("body")
        if body_snippet not in snippets:
            snippets.append(body_snippet)
    if not matched_fields:
        return None
    return {
        "targetType": target_type,
        "targetId": target_id,
        "matchedFields": matched_fields,
        "snippets": snippets[:3],
    }


def search_clawchat_memory(
    root: str | Path,
    query: str,
    *,
    target_types: list[str] | tuple[str, ...] | None = None,
    max_results: int = 10,
) -> dict:
    query = query.strip()
    if not query:
        raise ValueError("query is required")
    if not isinstance(max_results, int) or max_results < 1 or max_results > 50:
        raise ValueError("max_results must be between 1 and 50")
    root_path = Path(root)
    query_lower = query.lower()
    matches: list[dict] = []
    for target_type in _normalize_search_targets(target_types):
        for candidate_type, candidate_id in _safe_search_targets(root_path, target_type):
            memory = read_clawchat_memory_file(root_path, candidate_type, candidate_id)
            if not memory["exists"]:
                continue
            match = _build_search_match(candidate_type, candidate_id, memory, query_lower)
            if match is not None:
                matches.append(match)
    return {"query": query, "matches": matches[:max_results], "truncated": len(matches) > max_results}


def delete_clawchat_memory_file(root: str | Path, target_type: str, target_id: str) -> None:
    path = ensure_clawchat_memory_target_safe(root, target_type, target_id)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _append_body(existing_body: str, content: str) -> str:
    if not existing_body:
        return content
    if existing_body.endswith("\n") or content.startswith("\n"):
        return existing_body + content
    return f"{existing_body}\n{content}"


def write_clawchat_memory_body(
    root: str | Path,
    target_type: str,
    target_id: str,
    mode: str,
    content: str,
) -> None:
    path = ensure_clawchat_memory_target_safe(root, target_type, target_id)
    content = _normalize_line_endings(content)
    existing = _normalize_line_endings(_read_existing_content(path))
    if mode == "append":
        if not content:
            raise ValueError("append content must be non-empty")
        body = _append_body(_parse_clawchat_memory_content(existing)["body"], content)
    elif mode == "replace":
        body = content
    else:
        raise ValueError("mode must be 'append' or 'replace'")
    _atomic_write(path, _replace_body(existing, body))


def edit_clawchat_memory_body(
    root: str | Path,
    target_type: str,
    target_id: str,
    old_text: str,
    new_text: str,
) -> None:
    if not old_text:
        raise ValueError("old_text must be non-empty")

    path = ensure_clawchat_memory_target_safe(root, target_type, target_id)
    existing = _normalize_line_endings(_read_existing_content(path))
    parsed = _parse_clawchat_memory_content(existing)
    body = parsed["body"]
    old_text = _normalize_line_endings(old_text)
    new_text = _normalize_line_endings(new_text)
    if body.count(old_text) != 1:
        raise ValueError("old_text must match exactly one body occurrence")
    _atomic_write(path, _replace_body(existing, body.replace(old_text, new_text, 1)))


def write_clawchat_metadata(
    root: str | Path,
    target_type: str,
    target_id: str,
    metadata: dict[str, str],
) -> None:
    path = ensure_clawchat_memory_target_safe(root, target_type, target_id)
    existing = _normalize_line_endings(_read_existing_content(path))
    filtered_metadata = _filter_metadata_for_target(target_type, metadata)
    _atomic_write(path, _replace_metadata_block(existing, filtered_metadata))
