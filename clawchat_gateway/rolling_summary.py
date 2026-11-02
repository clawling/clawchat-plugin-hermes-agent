"""Rolling summary prompts, parsing and immutable local publication.

Publishing a file does not update a conversation: the caller must separately CAS
its history state. Only state-referenced files are authoritative.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import secrets
import time
from urllib.parse import unquote

from clawchat_gateway import atomic_file
from clawchat_gateway.history_budget import BlockItem, _entry_time
from clawchat_gateway.state_lock import state_file_lock


ROLLING_SUMMARY_MAX_CHARS = 10000
_INDEX_EXAMPLE = "(e.g. 10-30 · owner · server address and login · id m123)"
SUMMARY_SYSTEM_PROMPT = """Reply with the summary of this conversation, in exactly these four sections:

## Now
What is in progress, and how far it got.
## Decided
Agreements, decisions and your owner's instructions that still stand. Keep the specifics needed to carry on: times, places, names, numbers, each step of a plan that was accepted.
## Open
What someone said they will do and has not done yet: not what is under Decided, not advice that was only given.
## Index
One line per thing someone may need again: an address, account, link, file or number, someone's plan or method, steps your owner gave. Date, who, what, message id:
(e.g. 10-30 · owner · server address and login · id m123)
Clues only: never copy a password, a link or a long instruction itself.

If a previous summary is given, start from it and fold in the new messages.
Write "(none)" under an empty section. Facts only, not your own view or what you did in this turn; under 800 words.
"""


def build_summary_messages(previous_summary: str | None, block: list[BlockItem]) -> list[dict]:
    parts = []
    if previous_summary:
        parts.extend(["## ClawChat Conversation Summary", previous_summary, ""])
    parts.append("Messages, oldest first:")
    for entry in block:
        ts = _entry_time(entry.ts)
        stamp = f", {ts:%m-%d %H:%M}" if ts is not None else ""
        # Names remain on one transcript line; bodies retain all their text.
        name = re.sub(r"[\r\n]", " ", entry.name)
        parts.append(f"- id {entry.id}{stamp}, {name}: {entry.text}")
    return [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


def parse_rolling_summary(reply: str | None) -> str | None:
    if reply is None:
        return None
    lines = []
    for line in reply.strip().split("\n"):
        bare = re.sub(r"^[-*+]\s+", "", line.strip()).strip()
        if bare != _INDEX_EXAMPLE:
            lines.append(line)
    positions = []
    previous = -1
    for heading in ("## Now", "## Decided", "## Open", "## Index"):
        at = next((i for i in range(previous + 1, len(lines))
                   if lines[i].rstrip() == heading), -1)
        if at < 0:
            return None
        positions.append(at)
        previous = at
    kept = lines[positions[0]:]
    index = positions[-1] - positions[0]
    while len("\n".join(kept)) > ROLLING_SUMMARY_MAX_CHARS and len(kept) > index + 1:
        del kept[index + 1]
    text = "\n".join(kept).strip()
    if len(text) <= ROLLING_SUMMARY_MAX_CHARS:
        return text
    return text[:ROLLING_SUMMARY_MAX_CHARS - 2].rstrip() + " …"


def _escape_segment(value: str) -> str:
    if not value:
        raise ValueError("summary path segment must not be empty")
    safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
    if value in {".", ".."}:
        return "%2E" * len(value)
    return "".join(chr(unit) if unit in safe else f"%{unit:02X}"
                   for unit in value.encode("utf-8"))


def summary_dir(root: Path | str, account_id: str, conv_key: str) -> Path:
    """Root already includes any profile prefix; both appended parts are escaped."""
    return Path(root) / _escape_segment(account_id) / _escape_segment(conv_key)


_REF = re.compile(r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{32}\.md\Z")
_HEADER = re.compile(
    r"<!-- clawchat-summary as_of=(\S*) through=(\S*) boundary=(\S*) -->\Z"
)


def _header_value(value: str | datetime | None) -> str:
    if isinstance(value, datetime):
        value = value.astimezone(timezone.utc).isoformat()
    # Escape delimiters, whitespace and non-ASCII bytes so the comment cannot
    # be terminated by an id. Reads return the original field values.
    if value is None:
        return ""
    safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-+"
    return "".join(chr(unit) if unit in safe else f"%{unit:02X}"
                   for unit in value.encode("utf-8"))


def publish_summary(
    directory: Path | str, text: str, *, through: str,
    boundary: str | None, as_of: str | datetime,
) -> str:
    directory = Path(directory)
    ref = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + secrets.token_hex(16) + ".md"
    path = directory / ref
    header = (f"<!-- clawchat-summary as_of={_header_value(as_of)} "
              f"through={_header_value(through)} boundary={_header_value(boundary)} -->")
    # All publishers use the same cross-process lock around exists + atomic
    # rename. A collision fails rather than replacing an immutable version.
    with state_file_lock(directory / "publication"):
        if path.exists() or path.is_symlink():
            raise FileExistsError("summary version already exists")
        try:
            atomic_file.atomic_write_text(path, header + "\n" + text, mode=0o600)
        except BaseException:
            # atomic_write_text can fail during directory fsync, after rename.
            # No caller may publish this failed version through its state CAS.
            if path.exists():
                path.unlink()
                atomic_file.fsync_dir(directory)
            raise
    return ref


def read_summary(directory: Path | str, ref: str) -> tuple[dict[str, str], str] | None:
    if not _REF.fullmatch(ref):
        return None
    path = Path(directory) / ref
    try:
        if path.is_symlink():
            return None
        first, separator, body = path.read_text(encoding="utf-8").partition("\n")
    except (OSError, UnicodeError):
        return None
    match = _HEADER.fullmatch(first)
    if not separator or match is None:
        return None
    header = dict(zip(("as_of", "through", "boundary"), map(unquote, match.groups())))
    return header, body


def cleanup_summaries(
    directory: Path | str, keep_refs: set[str], older_than_s: float = 3600,
) -> None:
    directory = Path(directory)
    if not directory.is_dir():
        return
    cutoff = time.time() - older_than_s
    with state_file_lock(directory / "publication"):
        removed = False
        for path in directory.iterdir():
            if path.name in keep_refs or not _REF.fullmatch(path.name) or path.is_symlink():
                continue
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed = True
            except FileNotFoundError:
                continue
        if removed:
            atomic_file.fsync_dir(directory)
