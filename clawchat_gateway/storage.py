from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

DB_FILENAME = "clawchat.sqlite"
BOOTSTRAP_CLAIM_STALE_AFTER_MS = 10 * 60 * 1000

_T = TypeVar("_T")
_UNSET = object()


INITIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  applied_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS clawchat_messages (
  id INTEGER PRIMARY KEY,
  platform TEXT NOT NULL,
  account_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  direction TEXT NOT NULL,
  event_type TEXT NOT NULL,
  trace_id TEXT,
  chat_id TEXT,
  message_id TEXT,
  text TEXT,
  raw_json TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS activations (
  platform TEXT NOT NULL,
  account_id TEXT NOT NULL,
  user_id TEXT,
  access_token TEXT,
  refresh_token TEXT,
  activated_at INTEGER NOT NULL,
  login_method TEXT,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (platform, account_id)
);

CREATE TABLE IF NOT EXISTS connections (
  id INTEGER PRIMARY KEY,
  platform TEXT NOT NULL,
  account_id TEXT NOT NULL,
  attempt INTEGER,
  reconnect_count INTEGER,
  state TEXT NOT NULL,
  connect_started_at INTEGER,
  connect_sent_at INTEGER,
  ready_at INTEGER,
  disconnected_at INTEGER,
  close_code INTEGER,
  close_reason TEXT,
  error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_calls (
  id INTEGER PRIMARY KEY,
  platform TEXT NOT NULL,
  account_id TEXT,
  tool_name TEXT NOT NULL,
  args_json TEXT,
  result_json TEXT,
  error TEXT,
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  duration_ms INTEGER,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clawchat_messages_chat_created
  ON clawchat_messages(chat_id, created_at);
CREATE INDEX IF NOT EXISTS idx_clawchat_messages_message_id
  ON clawchat_messages(message_id);
CREATE INDEX IF NOT EXISTS idx_connections_account_created
  ON connections(platform, account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tool_calls_name_created
  ON tool_calls(tool_name, created_at);
"""

MESSAGE_ID_DEDUP_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_clawchat_messages_message_once
  ON clawchat_messages(account_id, direction, kind, message_id)
  WHERE kind = 'message' AND message_id IS NOT NULL;
"""

ACTIVATION_BOOTSTRAP_SCHEMA = """
ALTER TABLE activations ADD COLUMN conversation_id TEXT;
ALTER TABLE activations ADD COLUMN owner_id TEXT;
ALTER TABLE activations ADD COLUMN bootstrap_sent INTEGER NOT NULL DEFAULT 0;
ALTER TABLE activations ADD COLUMN bootstrap_claimed_at INTEGER;
"""

CONVERSATION_CACHE_SCHEMA = """
ALTER TABLE connections ADD COLUMN resolved_device_id TEXT;
ALTER TABLE connections ADD COLUMN delivery_mode TEXT;

CREATE TABLE IF NOT EXISTS clawchat_conversations (
  platform TEXT NOT NULL,
  account_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  conversation_type TEXT,
  metadata_version INTEGER,
  last_seen_at INTEGER,
  last_refreshed_at INTEGER,
  raw_json TEXT,
  PRIMARY KEY (platform, account_id, conversation_id)
);

CREATE TABLE IF NOT EXISTS clawchat_user_profiles (
  platform TEXT NOT NULL,
  account_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  nickname TEXT,
  avatar_url TEXT,
  bio TEXT,
  raw_json TEXT,
  last_refreshed_at INTEGER,
  PRIMARY KEY (platform, account_id, user_id)
);

CREATE TABLE IF NOT EXISTS clawchat_group_profiles (
  platform TEXT NOT NULL,
  account_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  title TEXT,
  description TEXT,
  metadata_version INTEGER,
  raw_json TEXT,
  last_refreshed_at INTEGER,
  PRIMARY KEY (platform, account_id, conversation_id)
);

CREATE TABLE IF NOT EXISTS clawchat_conversation_members (
  platform TEXT NOT NULL,
  account_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  role TEXT,
  raw_json TEXT,
  last_seen_at INTEGER,
  PRIMARY KEY (platform, account_id, conversation_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_clawchat_conversations_seen
  ON clawchat_conversations(platform, account_id, last_seen_at);
"""

UNIFIED_PROFILES_SCHEMA = """
ALTER TABLE activations ADD COLUMN owner_user_id TEXT;
UPDATE activations
SET owner_user_id = owner_id
WHERE owner_user_id IS NULL AND owner_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS clawchat_profiles (
  platform TEXT NOT NULL,
  account_id TEXT NOT NULL,
  profile_kind TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  relation TEXT,
  profile_type TEXT,
  title TEXT,
  description TEXT,
  behavior TEXT,
  nickname TEXT,
  avatar_url TEXT,
  bio TEXT,
  metadata_version INTEGER,
  raw_json TEXT,
  created_at INTEGER NOT NULL,
  last_seen_at INTEGER,
  last_refreshed_at INTEGER,
  PRIMARY KEY(platform, account_id, profile_kind, profile_id)
);

CREATE INDEX IF NOT EXISTS idx_clawchat_profiles_seen
  ON clawchat_profiles(platform, account_id, profile_kind, last_seen_at);
"""

MIGRATIONS = [
    (1, "initial_schema", INITIAL_SCHEMA),
    (2, "message_id_dedup", MESSAGE_ID_DEDUP_SCHEMA),
    (3, "activation_bootstrap", ACTIVATION_BOOTSTRAP_SCHEMA),
    (4, "conversation_cache", CONVERSATION_CACHE_SCHEMA),
    (5, "unified_profiles", UNIFIED_PROFILES_SCHEMA),
]

_store: ClawChatStore | None = None
_store_lock = threading.Lock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def default_db_path() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / DB_FILENAME


def json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class ActivationBootstrapClaim:
    conversation_id: str
    owner_user_id: str | None
    claimed_at: int


class ClawChatStore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self._initialized = False
        self._disabled = False
        self._lock = threading.Lock()

    def initialize(self) -> None:
        with self._lock:
            if self._initialized or self._disabled:
                return
            try:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(self.db_path)
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    applied = self._applied_migrations(conn)
                    for version, name, sql in MIGRATIONS:
                        if version in applied:
                            continue
                        applied_at = _now_ms()
                        escaped_name = name.replace("'", "''")
                        conn.executescript(
                            "BEGIN;\n"
                            f"{sql}\n"
                            "INSERT INTO schema_migrations(version, name, applied_at) "
                            f"VALUES ({version}, '{escaped_name}', {applied_at});\n"
                            "COMMIT;"
                        )
                    self._chmod_private()
                    self._initialized = True
                finally:
                    conn.close()
            except Exception:  # noqa: BLE001
                self._disabled = True
                logger.warning(
                    "clawchat database initialization failed; disabling writes",
                    exc_info=True,
                )

    def upsert_activation(
        self,
        *,
        platform: str,
        account_id: str,
        user_id: str | None,
        conversation_id: str,
        owner_user_id: str | None,
        access_token: str | None = None,
        refresh_token: str | None = None,
        activated_at: int | None = None,
        login_method: str | None = None,
        updated_at: int | None = None,
    ) -> None:
        if not conversation_id:
            raise ValueError("conversation_id is required")
        now = _now_ms()
        activated = activated_at if activated_at is not None else now
        updated = updated_at if updated_at is not None else activated

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO activations(
                  platform, account_id, user_id, access_token, refresh_token,
                  activated_at, login_method, updated_at, conversation_id, owner_user_id,
                  bootstrap_sent, bootstrap_claimed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
                ON CONFLICT(platform, account_id) DO UPDATE SET
                  user_id = excluded.user_id,
                  access_token = excluded.access_token,
                  refresh_token = excluded.refresh_token,
                  activated_at = excluded.activated_at,
                  login_method = excluded.login_method,
                  updated_at = excluded.updated_at,
                  conversation_id = excluded.conversation_id,
                  owner_user_id = excluded.owner_user_id,
                  bootstrap_sent = 0,
                  bootstrap_claimed_at = NULL
                """,
                (
                    platform,
                    account_id,
                    user_id,
                    None,
                    None,
                    activated,
                    None,
                    updated,
                    conversation_id,
                    owner_user_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO clawchat_conversations(platform, account_id, conversation_id)
                VALUES (?, ?, ?)
                ON CONFLICT(platform, account_id, conversation_id) DO NOTHING
                """,
                (platform, account_id, conversation_id),
            )

        self._write("upsert_activation", write)

    def upsert_conversation_summary(
        self,
        *,
        platform: str,
        account_id: str,
        conversation_id: str,
        conversation_type: str | None = None,
        last_seen_at: int | None = None,
        raw: Any = None,
    ) -> bool | None:
        def write(conn: sqlite3.Connection) -> bool:
            exists = conn.execute(
                """
                SELECT 1
                FROM clawchat_conversations
                WHERE platform = ? AND account_id = ? AND conversation_id = ?
                """,
                (platform, account_id, conversation_id),
            ).fetchone() is not None
            conn.execute(
                """
                INSERT INTO clawchat_conversations(
                  platform, account_id, conversation_id, conversation_type, last_seen_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, account_id, conversation_id) DO UPDATE SET
                  conversation_type = COALESCE(excluded.conversation_type, conversation_type),
                  last_seen_at = COALESCE(excluded.last_seen_at, last_seen_at),
                  raw_json = COALESCE(excluded.raw_json, raw_json)
                """,
                (
                    platform,
                    account_id,
                    conversation_id,
                    conversation_type,
                    last_seen_at,
                    json_dumps(raw),
                ),
            )
            return not exists

        return self._write("upsert_conversation_summary", write)

    def upsert_profile(
        self,
        *,
        platform: str,
        account_id: str,
        profile_kind: str,
        profile_id: str,
        relation: Any = _UNSET,
        profile_type: Any = _UNSET,
        title: Any = _UNSET,
        description: Any = _UNSET,
        behavior: Any = _UNSET,
        nickname: Any = _UNSET,
        avatar_url: Any = _UNSET,
        bio: Any = _UNSET,
        metadata_version: Any = _UNSET,
        raw: Any = _UNSET,
        created_at: int | None = None,
        last_seen_at: Any = _UNSET,
        last_refreshed_at: Any = _UNSET,
    ) -> bool | None:
        if not profile_kind:
            raise ValueError("profile_kind is required")
        if not profile_id:
            raise ValueError("profile_id is required")

        def write(conn: sqlite3.Connection) -> bool:
            return self._upsert_profile_row(
                conn,
                platform=platform,
                account_id=account_id,
                profile_kind=profile_kind,
                profile_id=profile_id,
                relation=relation,
                profile_type=profile_type,
                title=title,
                description=description,
                behavior=behavior,
                nickname=nickname,
                avatar_url=avatar_url,
                bio=bio,
                metadata_version=metadata_version,
                raw=raw,
                created_at=created_at,
                last_seen_at=last_seen_at,
                last_refreshed_at=last_refreshed_at,
            )

        return self._write("upsert_profile", write)

    def upsert_minimal_profile(
        self,
        *,
        platform: str = "clawchat",
        account_id: str,
        profile_kind: str,
        profile_id: str,
        relation: str | None = None,
        profile_type: str | None = None,
        nickname: str | None = None,
        now_ms: int | None = None,
    ) -> bool | None:
        return self.upsert_profile(
            platform=platform,
            account_id=account_id,
            profile_kind=profile_kind,
            profile_id=profile_id,
            relation=relation,
            profile_type=profile_type,
            nickname=nickname,
            created_at=now_ms,
            last_seen_at=now_ms if now_ms is not None else _UNSET,
        )

    def upsert_agent_profile(
        self,
        *,
        platform: str = "clawchat",
        account_id: str,
        profile_id: str,
        behavior: Any = _UNSET,
        raw: Any = _UNSET,
        now_ms: int | None = None,
    ) -> bool | None:
        return self.upsert_profile(
            platform=platform,
            account_id=account_id,
            profile_kind="agent",
            profile_id=profile_id,
            profile_type="agent",
            behavior=behavior,
            raw=raw,
            created_at=now_ms,
            last_refreshed_at=now_ms if now_ms is not None else _UNSET,
        )

    def upsert_group_profile(
        self,
        *,
        platform: str = "clawchat",
        account_id: str,
        profile_id: str,
        title: Any = _UNSET,
        description: Any = _UNSET,
        metadata_version: Any = _UNSET,
        raw: Any = _UNSET,
        now_ms: int | None = None,
    ) -> bool | None:
        return self.upsert_profile(
            platform=platform,
            account_id=account_id,
            profile_kind="group",
            profile_id=profile_id,
            title=title,
            description=description,
            metadata_version=metadata_version,
            raw=raw,
            created_at=now_ms,
            last_seen_at=now_ms if now_ms is not None else _UNSET,
            last_refreshed_at=now_ms if now_ms is not None else _UNSET,
        )

    def upsert_user_profile(
        self,
        *,
        platform: str = "clawchat",
        account_id: str,
        profile_id: str,
        relation: Any = _UNSET,
        profile_type: Any = _UNSET,
        nickname: Any = _UNSET,
        avatar_url: Any = _UNSET,
        bio: Any = _UNSET,
        raw: Any = _UNSET,
        now_ms: int | None = None,
    ) -> bool | None:
        return self.upsert_profile(
            platform=platform,
            account_id=account_id,
            profile_kind="user",
            profile_id=profile_id,
            relation=relation,
            profile_type=profile_type,
            nickname=nickname,
            avatar_url=avatar_url,
            bio=bio,
            raw=raw,
            created_at=now_ms,
            last_seen_at=now_ms if now_ms is not None else _UNSET,
            last_refreshed_at=now_ms if now_ms is not None else _UNSET,
        )

    def profile_exists(
        self,
        *,
        platform: str,
        account_id: str,
        profile_kind: str,
        profile_id: str,
    ) -> bool:
        return self.get_profile(
            platform=platform,
            account_id=account_id,
            profile_kind=profile_kind,
            profile_id=profile_id,
        ) is not None

    def get_profile(
        self,
        *,
        platform: str,
        account_id: str,
        profile_kind: str,
        profile_id: str,
    ) -> dict[str, Any] | None:
        self.initialize()
        if self._disabled:
            return None
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT platform, account_id, profile_kind, profile_id, relation,
                       profile_type, title, description, behavior, nickname,
                       avatar_url, bio, metadata_version, raw_json, created_at,
                       last_seen_at, last_refreshed_at
                FROM clawchat_profiles
                WHERE platform = ?
                  AND account_id = ?
                  AND profile_kind = ?
                  AND profile_id = ?
                """,
                (platform, account_id, profile_kind, profile_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        keys = (
            "platform",
            "account_id",
            "profile_kind",
            "profile_id",
            "relation",
            "profile_type",
            "title",
            "description",
            "behavior",
            "nickname",
            "avatar_url",
            "bio",
            "metadata_version",
            "raw_json",
            "created_at",
            "last_seen_at",
            "last_refreshed_at",
        )
        return dict(zip(keys, row, strict=True))

    def _upsert_profile_row(
        self,
        conn: sqlite3.Connection,
        *,
        platform: str,
        account_id: str,
        profile_kind: str,
        profile_id: str,
        relation: Any = _UNSET,
        profile_type: Any = _UNSET,
        title: Any = _UNSET,
        description: Any = _UNSET,
        behavior: Any = _UNSET,
        nickname: Any = _UNSET,
        avatar_url: Any = _UNSET,
        bio: Any = _UNSET,
        metadata_version: Any = _UNSET,
        raw: Any = _UNSET,
        created_at: int | None = None,
        last_seen_at: Any = _UNSET,
        last_refreshed_at: Any = _UNSET,
    ) -> bool:
        exists = conn.execute(
            """
            SELECT 1
            FROM clawchat_profiles
            WHERE platform = ?
              AND account_id = ?
              AND profile_kind = ?
              AND profile_id = ?
            """,
            (platform, account_id, profile_kind, profile_id),
        ).fetchone() is not None
        values = {
            "relation": relation,
            "profile_type": profile_type,
            "title": title,
            "description": description,
            "behavior": behavior,
            "nickname": nickname,
            "avatar_url": avatar_url,
            "bio": bio,
            "metadata_version": metadata_version,
            "raw_json": _UNSET if raw is _UNSET else json_dumps(raw),
            "last_seen_at": last_seen_at,
            "last_refreshed_at": last_refreshed_at,
        }
        if not exists:
            conn.execute(
                """
                INSERT INTO clawchat_profiles(
                  platform, account_id, profile_kind, profile_id, relation, profile_type,
                  title, description, behavior, nickname, avatar_url, bio,
                  metadata_version, raw_json, created_at, last_seen_at, last_refreshed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    account_id,
                    profile_kind,
                    profile_id,
                    None if relation is _UNSET else relation,
                    None if profile_type is _UNSET else profile_type,
                    None if title is _UNSET else title,
                    None if description is _UNSET else description,
                    None if behavior is _UNSET else behavior,
                    None if nickname is _UNSET else nickname,
                    None if avatar_url is _UNSET else avatar_url,
                    None if bio is _UNSET else bio,
                    None if metadata_version is _UNSET else metadata_version,
                    None if raw is _UNSET else json_dumps(raw),
                    created_at if created_at is not None else _now_ms(),
                    None if last_seen_at is _UNSET else last_seen_at,
                    None if last_refreshed_at is _UNSET else last_refreshed_at,
                ),
            )
            return True
        updates = [(column, value) for column, value in values.items() if value is not _UNSET]
        if updates:
            assignments = ", ".join(f"{column} = ?" for column, _value in updates)
            conn.execute(
                f"""
                UPDATE clawchat_profiles
                SET {assignments}
                WHERE platform = ?
                  AND account_id = ?
                  AND profile_kind = ?
                  AND profile_id = ?
                """,
                (
                    *(value for _column, value in updates),
                    platform,
                    account_id,
                    profile_kind,
                    profile_id,
                ),
            )
        return not exists

    def upsert_conversation_details(
        self,
        *,
        platform: str,
        account_id: str,
        conversation_id: str,
        conversation_type: str | None = None,
        metadata_version: int | None = None,
        last_seen_at: int | None = None,
        last_refreshed_at: int | None = None,
        raw: Any = None,
        group_profile: dict[str, Any] | None = None,
        user_profiles: list[dict[str, Any]] | None = None,
        members: list[dict[str, Any]] | None = None,
        members_complete: bool = False,
    ) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO clawchat_conversations(
                  platform, account_id, conversation_id, conversation_type, metadata_version,
                  last_seen_at, last_refreshed_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, account_id, conversation_id) DO UPDATE SET
                  conversation_type = COALESCE(excluded.conversation_type, conversation_type),
                  metadata_version = COALESCE(excluded.metadata_version, metadata_version),
                  last_seen_at = COALESCE(excluded.last_seen_at, last_seen_at),
                  last_refreshed_at = COALESCE(excluded.last_refreshed_at, last_refreshed_at),
                  raw_json = COALESCE(excluded.raw_json, raw_json)
                """,
                (
                    platform,
                    account_id,
                    conversation_id,
                    conversation_type,
                    metadata_version,
                    last_seen_at,
                    last_refreshed_at,
                    json_dumps(raw),
                ),
            )
            if group_profile is not None:
                self._upsert_profile_row(
                    conn,
                    platform=platform,
                    account_id=account_id,
                    profile_kind="group",
                    profile_id=conversation_id,
                    title=group_profile["title"] if "title" in group_profile else _UNSET,
                    description=(
                        group_profile["description"]
                        if "description" in group_profile
                        else _UNSET
                    ),
                    metadata_version=(
                        group_profile["metadata_version"]
                        if "metadata_version" in group_profile
                        else metadata_version if metadata_version is not None else _UNSET
                    ),
                    raw=group_profile["raw"] if "raw" in group_profile else group_profile,
                    last_seen_at=last_seen_at if last_seen_at is not None else _UNSET,
                    last_refreshed_at=(
                        group_profile["last_refreshed_at"]
                        if "last_refreshed_at" in group_profile
                        else last_refreshed_at if last_refreshed_at is not None else _UNSET
                    ),
                )
            for profile in user_profiles or []:
                user_id = profile.get("user_id")
                if not user_id:
                    continue
                self._upsert_profile_row(
                    conn,
                    platform=platform,
                    account_id=account_id,
                    profile_kind="user",
                    profile_id=str(user_id),
                    relation=profile["relation"] if "relation" in profile else _UNSET,
                    profile_type=(
                        profile["profile_type"] if "profile_type" in profile else _UNSET
                    ),
                    nickname=profile["nickname"] if "nickname" in profile else _UNSET,
                    avatar_url=(
                        profile["avatar_url"] if "avatar_url" in profile else _UNSET
                    ),
                    bio=profile["bio"] if "bio" in profile else _UNSET,
                    raw=profile["raw"] if "raw" in profile else profile,
                    last_seen_at=(
                        profile["last_seen_at"]
                        if "last_seen_at" in profile
                        else last_seen_at if last_seen_at is not None else _UNSET
                    ),
                    last_refreshed_at=(
                        profile["last_refreshed_at"]
                        if "last_refreshed_at" in profile
                        else last_refreshed_at if last_refreshed_at is not None else _UNSET
                    ),
                )
            if members_complete:
                conn.execute(
                    """
                    DELETE FROM clawchat_conversation_members
                    WHERE platform = ? AND account_id = ? AND conversation_id = ?
                    """,
                    (platform, account_id, conversation_id),
                )
                for member in members or []:
                    user_id = member.get("user_id")
                    if not user_id:
                        continue
                    conn.execute(
                        """
                        INSERT INTO clawchat_conversation_members(
                          platform, account_id, conversation_id, user_id, role, raw_json, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            platform,
                            account_id,
                            conversation_id,
                            user_id,
                            member.get("role"),
                            json_dumps(member.get("raw", member)),
                            member.get("last_seen_at"),
                        ),
                    )

        self._write("upsert_conversation_details", write)

    def delete_conversation_cache(
        self,
        *,
        platform: str,
        account_id: str,
        conversation_id: str,
    ) -> None:
        def write(conn: sqlite3.Connection) -> None:
            params = (platform, account_id, conversation_id)
            conn.execute(
                """
                DELETE FROM clawchat_conversation_members
                WHERE platform = ? AND account_id = ? AND conversation_id = ?
                """,
                params,
            )
            conn.execute(
                """
                DELETE FROM clawchat_profiles
                WHERE platform = ?
                  AND account_id = ?
                  AND profile_kind = 'group'
                  AND profile_id = ?
                """,
                params,
            )
            conn.execute(
                """
                DELETE FROM clawchat_conversations
                WHERE platform = ? AND account_id = ? AND conversation_id = ?
                """,
                params,
            )

        self._write("delete_conversation_cache", write)

    def list_cached_conversation_ids(
        self,
        *,
        platform: str,
        account_id: str,
        limit: int,
    ) -> list[str]:
        self.initialize()
        if self._disabled:
            return []
        conn = sqlite3.connect(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT conversation_id
                FROM clawchat_conversations
                WHERE platform = ? AND account_id = ?
                ORDER BY last_seen_at DESC, conversation_id ASC
                LIMIT ?
                """,
                (platform, account_id, max(0, limit)),
            ).fetchall()
            return [str(row[0]) for row in rows]
        finally:
            conn.close()

    def get_activation_conversation(
        self,
        *,
        platform: str,
        account_id: str,
    ) -> str | None:
        self.initialize()
        if self._disabled:
            return None
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT conversation_id
                FROM activations
                WHERE platform = ? AND account_id = ?
                """,
                (platform, account_id),
            ).fetchone()
            if row is None or row[0] is None:
                return None
            return str(row[0])
        finally:
            conn.close()

    def claim_pending_activation_bootstrap(
        self,
        *,
        platform: str,
        account_id: str,
        claimed_at: int | None = None,
        stale_after_ms: int = BOOTSTRAP_CLAIM_STALE_AFTER_MS,
    ) -> ActivationBootstrapClaim | None:
        claimed = claimed_at if claimed_at is not None else _now_ms()
        stale_before = claimed - max(0, stale_after_ms)
        self.initialize()
        if self._disabled:
            return None
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """
                    SELECT conversation_id, owner_user_id
                    FROM activations
                    WHERE platform = ?
                      AND account_id = ?
                      AND conversation_id IS NOT NULL
                      AND conversation_id != ''
                      AND bootstrap_sent = 0
                      AND (
                        bootstrap_claimed_at IS NULL
                        OR bootstrap_claimed_at < ?
                      )
                    """,
                    (platform, account_id, stale_before),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                conversation_id, owner_user_id = row
                cursor = conn.execute(
                    """
                    UPDATE activations
                    SET bootstrap_claimed_at = ?, updated_at = ?
                    WHERE platform = ?
                      AND account_id = ?
                      AND conversation_id = ?
                      AND bootstrap_sent = 0
                      AND (
                        bootstrap_claimed_at IS NULL
                        OR bootstrap_claimed_at < ?
                      )
                    """,
                    (claimed, claimed, platform, account_id, conversation_id, stale_before),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return None
                conn.commit()
                return ActivationBootstrapClaim(
                    conversation_id=str(conversation_id),
                    owner_user_id=str(owner_user_id) if owner_user_id is not None else None,
                    claimed_at=claimed,
                )
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            logger.warning(
                "clawchat database write failed operation=%s",
                "claim_pending_activation_bootstrap",
                exc_info=True,
            )
            return None

    def release_activation_bootstrap_claim(
        self,
        *,
        platform: str,
        account_id: str,
        conversation_id: str,
        claimed_at: int,
        released_at: int | None = None,
    ) -> bool | None:
        if not conversation_id:
            return False
        released = released_at if released_at is not None else _now_ms()

        def write(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                """
                UPDATE activations
                SET bootstrap_claimed_at = NULL, updated_at = ?
                WHERE platform = ?
                  AND account_id = ?
                  AND conversation_id = ?
                  AND bootstrap_sent = 0
                  AND bootstrap_claimed_at = ?
                """,
                (released, platform, account_id, conversation_id, claimed_at),
            )
            return cursor.rowcount == 1

        return self._write("release_activation_bootstrap_claim", write)

    def mark_activation_bootstrap_sent(
        self,
        *,
        platform: str,
        account_id: str,
        conversation_id: str,
        claimed_at: int | None = None,
        sent_at: int | None = None,
    ) -> bool | None:
        if not conversation_id:
            return False
        sent = sent_at if sent_at is not None else _now_ms()

        def write(conn: sqlite3.Connection) -> bool:
            claim_filter = (
                "AND bootstrap_claimed_at IS NOT NULL"
                if claimed_at is None
                else "AND bootstrap_claimed_at = ?"
            )
            params = [sent, platform, account_id, conversation_id]
            if claimed_at is not None:
                params.append(claimed_at)
            cursor = conn.execute(
                f"""
                UPDATE activations
                SET bootstrap_sent = 1, updated_at = ?
                WHERE platform = ?
                  AND account_id = ?
                  AND conversation_id = ?
                  AND bootstrap_sent = 0
                  {claim_filter}
                """,
                tuple(params),
            )
            return cursor.rowcount == 1

        return self._write("mark_activation_bootstrap_sent", write)

    def insert_message(
        self,
        *,
        platform: str,
        account_id: str,
        kind: str,
        direction: str,
        event_type: str,
        trace_id: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        text: str | None = None,
        raw: Any = None,
        created_at: int | None = None,
    ) -> int | None:
        created = created_at if created_at is not None else _now_ms()

        def write(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """
                INSERT INTO clawchat_messages(
                  platform, account_id, kind, direction, event_type, trace_id,
                  chat_id, message_id, text, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    account_id,
                    kind,
                    direction,
                    event_type,
                    trace_id,
                    chat_id,
                    message_id,
                    text,
                    json_dumps(raw),
                    created,
                ),
            )
            return int(cursor.lastrowid)

        return self._write("insert_message", write)

    def claim_message_once(
        self,
        *,
        platform: str,
        account_id: str,
        kind: str,
        direction: str,
        event_type: str,
        trace_id: str | None = None,
        chat_id: str | None = None,
        message_id: str | None = None,
        text: str | None = None,
        raw: Any = None,
        created_at: int | None = None,
    ) -> bool | None:
        if not message_id:
            return None
        created = created_at if created_at is not None else _now_ms()
        self.initialize()
        if self._disabled:
            return None
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(
                    """
                    INSERT INTO clawchat_messages(
                      platform, account_id, kind, direction, event_type, trace_id,
                      chat_id, message_id, text, raw_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        platform,
                        account_id,
                        kind,
                        direction,
                        event_type,
                        trace_id,
                        chat_id,
                        message_id,
                        text,
                        json_dumps(raw),
                        created,
                    ),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                conn.rollback()
                return False
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            logger.warning(
                "clawchat database write failed operation=%s",
                "claim_message_once",
                exc_info=True,
            )
            return None

    def update_message_by_identity(
        self,
        *,
        account_id: str,
        kind: str,
        direction: str,
        message_id: str,
        event_type: str,
        trace_id: str | None = None,
        chat_id: str | None = None,
        text: str | None = None,
        raw: Any = None,
    ) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE clawchat_messages
                SET event_type = ?, trace_id = ?, chat_id = ?, text = ?, raw_json = ?
                WHERE account_id = ?
                  AND kind = ?
                  AND direction = ?
                  AND message_id = ?
                """,
                (
                    event_type,
                    trace_id,
                    chat_id,
                    text,
                    json_dumps(raw),
                    account_id,
                    kind,
                    direction,
                    message_id,
                ),
            )

        self._write("update_message_by_identity", write)

    def start_connection(
        self,
        *,
        platform: str,
        account_id: str,
        attempt: int | None,
        reconnect_count: int | None,
        connect_started_at: int | None = None,
    ) -> int | None:
        started = connect_started_at if connect_started_at is not None else _now_ms()

        def write(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """
                INSERT INTO connections(
                  platform, account_id, attempt, reconnect_count, state,
                  connect_started_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    account_id,
                    attempt,
                    reconnect_count,
                    "connecting",
                    started,
                    started,
                    started,
                ),
            )
            return int(cursor.lastrowid)

        return self._write("start_connection", write)

    def mark_connect_sent(
        self,
        connection_id: int | None,
        *,
        connect_sent_at: int | None = None,
    ) -> None:
        if connection_id is None:
            return
        sent = connect_sent_at if connect_sent_at is not None else _now_ms()

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE connections
                SET state = ?, connect_sent_at = ?, updated_at = ?
                WHERE id = ?
                """,
                ("handshaking", sent, sent, connection_id),
            )

        self._write("mark_connect_sent", write)

    def mark_connection_ready(
        self,
        connection_id: int | None,
        *,
        ready_at: int | None = None,
        resolved_device_id: str | None = None,
        delivery_mode: str | None = None,
    ) -> None:
        if connection_id is None:
            return
        ready = ready_at if ready_at is not None else _now_ms()

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE connections
                SET state = ?, ready_at = ?, resolved_device_id = ?, delivery_mode = ?, updated_at = ?
                WHERE id = ?
                """,
                ("ready", ready, resolved_device_id, delivery_mode, ready, connection_id),
            )

        self._write("mark_connection_ready", write)

    def finish_connection(
        self,
        connection_id: int | None,
        *,
        state: str,
        disconnected_at: int | None = None,
        close_code: int | None = None,
        close_reason: str | None = None,
        error: str | None = None,
    ) -> None:
        if connection_id is None:
            return
        ended = disconnected_at if disconnected_at is not None else _now_ms()

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE connections
                SET state = ?, disconnected_at = ?, close_code = ?, close_reason = ?,
                    error = ?, updated_at = ?
                WHERE id = ?
                """,
                (state, ended, close_code, close_reason, error, ended, connection_id),
            )

        self._write("finish_connection", write)

    def record_tool_call(
        self,
        *,
        platform: str,
        account_id: str | None,
        tool_name: str,
        args: Any = None,
        result: Any = None,
        error: str | None = None,
        started_at: int | None = None,
        ended_at: int | None = None,
    ) -> int | None:
        started = started_at if started_at is not None else _now_ms()
        duration_ms = ended_at - started if ended_at is not None else None

        def write(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """
                INSERT INTO tool_calls(
                  platform, account_id, tool_name, args_json, result_json, error,
                  started_at, ended_at, duration_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    platform,
                    account_id,
                    tool_name,
                    json_dumps(args),
                    json_dumps(result),
                    error,
                    started,
                    ended_at,
                    duration_ms,
                    started,
                ),
            )
            return int(cursor.lastrowid)

        return self._write("record_tool_call", write)

    def _applied_migrations(self, conn: sqlite3.Connection) -> set[int]:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        if row is None:
            return set()
        return {int(version) for (version,) in conn.execute("SELECT version FROM schema_migrations")}

    def _chmod_private(self) -> None:
        try:
            self.db_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            logger.debug("clawchat database chmod failed", exc_info=True)

    def _write(
        self,
        operation: str,
        callback: Callable[[sqlite3.Connection], _T],
    ) -> _T | None:
        self.initialize()
        if self._disabled:
            return None
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                result = callback(conn)
                conn.commit()
                return result
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            logger.warning(
                "clawchat database write failed operation=%s",
                operation,
                exc_info=True,
            )
            return None


def get_clawchat_store() -> ClawChatStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = ClawChatStore(default_db_path())
        return _store
