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

CONNECTION_METADATA_SCHEMA = """
ALTER TABLE connections ADD COLUMN resolved_device_id TEXT;
ALTER TABLE connections ADD COLUMN delivery_mode TEXT;
"""

ACTIVATION_OWNER_USER_ID_SCHEMA = """
ALTER TABLE activations ADD COLUMN owner_user_id TEXT;
UPDATE activations
SET owner_user_id = owner_id
WHERE owner_user_id IS NULL AND owner_id IS NOT NULL;
"""

ACTIVATION_DEVICE_ID_SCHEMA = """
ALTER TABLE activations ADD COLUMN device_id TEXT;
"""

LIVEWARE_SAMPLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS liveware_sample (
  platform TEXT NOT NULL,
  account_id TEXT NOT NULL,
  app_id TEXT NOT NULL,
  app_name TEXT NOT NULL,
  port INTEGER NOT NULL,
  public_url TEXT,
  sample_version TEXT NOT NULL,
  status TEXT NOT NULL,
  last_error TEXT,
  intro_sent INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (platform, account_id)
);
"""

OWNER_PROFILE_SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_profile (
  platform TEXT NOT NULL,
  account_id TEXT NOT NULL,
  owner_user_id TEXT,
  nickname TEXT,
  avatar_url TEXT,
  bio TEXT,
  locale TEXT,
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (platform, account_id)
);
"""

MIGRATIONS = [
    (1, "initial_schema", INITIAL_SCHEMA),
    (2, "message_id_dedup", MESSAGE_ID_DEDUP_SCHEMA),
    (3, "activation_bootstrap", ACTIVATION_BOOTSTRAP_SCHEMA),
    (4, "connection_metadata", CONNECTION_METADATA_SCHEMA),
    (5, "activation_owner_user_id", ACTIVATION_OWNER_USER_ID_SCHEMA),
    (6, "activation_device_id", ACTIVATION_DEVICE_ID_SCHEMA),
    (7, "liveware_sample", LIVEWARE_SAMPLE_SCHEMA),
    (8, "owner_profile", OWNER_PROFILE_SCHEMA),
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


@dataclass(frozen=True)
class ActivationCredentials:
    user_id: str
    owner_user_id: str
    access_token: str
    refresh_token: str | None
    device_id: str | None = None
    activated_at: int | None = None


@dataclass(frozen=True)
class OwnerProfileRow:
    platform: str
    account_id: str
    owner_user_id: str | None
    nickname: str | None
    avatar_url: str | None
    bio: str | None
    locale: str | None
    updated_at: int


@dataclass(frozen=True)
class LivewareSampleRow:
    platform: str
    account_id: str
    app_id: str
    app_name: str
    port: int
    public_url: str | None
    sample_version: str
    status: str
    last_error: str | None
    intro_sent: int
    created_at: int
    updated_at: int


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
        device_id: str | None = None,
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
                  device_id, bootstrap_sent, bootstrap_claimed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
                ON CONFLICT(platform, account_id) DO UPDATE SET
                  user_id = excluded.user_id,
                  access_token = excluded.access_token,
                  refresh_token = excluded.refresh_token,
                  activated_at = excluded.activated_at,
                  login_method = excluded.login_method,
                  updated_at = excluded.updated_at,
                  conversation_id = excluded.conversation_id,
                  owner_user_id = excluded.owner_user_id,
                  device_id = excluded.device_id,
                  bootstrap_sent = 0,
                  bootstrap_claimed_at = NULL
                """,
                (
                    platform,
                    account_id,
                    user_id,
                    access_token.strip() if isinstance(access_token, str) and access_token.strip() else None,
                    refresh_token.strip() if isinstance(refresh_token, str) and refresh_token.strip() else None,
                    activated,
                    None,
                    updated,
                    conversation_id,
                    owner_user_id,
                    device_id.strip() if isinstance(device_id, str) and device_id.strip() else None,
                ),
            )

        self._write("upsert_activation", write)

    def update_activation_tokens(
        self,
        *,
        platform: str,
        account_id: str,
        access_token: str,
        refresh_token: str,
        device_id: str | None = None,
        updated_at: int | None = None,
        seed_user_id: str | None = None,
        seed_owner_user_id: str | None = None,
        seed_conversation_id: str | None = None,
    ) -> bool | None:
        """Rotate just the token columns of an existing activation row.

        Used by the refresh routine: it must NOT touch identity columns
        (user_id / owner_user_id / conversation_id) or reset the bootstrap flags
        (a refresh is not a re-pair). ``device_id`` is backfilled if provided.

        Token-refresh spec §C.2 (env-only deployment): when NO activations row
        exists yet (an ``.env``-booted process that never activated in-pod), fall
        back to seeding the row from the supplied identity so the first refresh
        does not return rowcount==0 and brick the agent. The seed INSERT keeps
        ``bootstrap_sent=0`` (the env row was never bootstrapped) and does not
        reset any activation flags. Returns True when the row was updated OR
        seeded.
        """
        updated = updated_at if updated_at is not None else _now_ms()
        access = access_token.strip() if access_token and access_token.strip() else None
        refresh = refresh_token.strip() if refresh_token and refresh_token.strip() else None
        device = device_id.strip() if isinstance(device_id, str) and device_id.strip() else None

        def write(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                """
                UPDATE activations
                SET access_token = ?,
                    refresh_token = ?,
                    device_id = COALESCE(?, device_id),
                    updated_at = ?
                WHERE platform = ? AND account_id = ?
                """,
                (
                    access,
                    refresh,
                    device,
                    updated,
                    platform,
                    account_id,
                ),
            )
            if cursor.rowcount == 1:
                return True
            # No pre-existing row → env-only deployment. Seed an identity row so
            # the rotated tokens are durably stored and future refreshes/restart
            # recovery work (§C.2). conversation_id may be empty (env path derives
            # the home channel from env vars, not this row).
            seed_user = seed_user_id.strip() if isinstance(seed_user_id, str) and seed_user_id.strip() else None
            seed_owner = (
                seed_owner_user_id.strip()
                if isinstance(seed_owner_user_id, str) and seed_owner_user_id.strip()
                else None
            )
            seed_conversation = (
                seed_conversation_id.strip()
                if isinstance(seed_conversation_id, str) and seed_conversation_id.strip()
                else None
            )
            insert_cursor = conn.execute(
                """
                INSERT INTO activations(
                  platform, account_id, user_id, access_token, refresh_token,
                  activated_at, login_method, updated_at, conversation_id,
                  owner_user_id, device_id, bootstrap_sent, bootstrap_claimed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
                ON CONFLICT(platform, account_id) DO UPDATE SET
                  access_token = excluded.access_token,
                  refresh_token = excluded.refresh_token,
                  device_id = COALESCE(excluded.device_id, activations.device_id),
                  updated_at = excluded.updated_at
                """,
                (
                    platform,
                    account_id,
                    seed_user,
                    access,
                    refresh,
                    updated,
                    None,
                    updated,
                    seed_conversation,
                    seed_owner,
                    device,
                ),
            )
            return insert_cursor.rowcount >= 1

        return self._write("update_activation_tokens", write)

    def set_activation_device_id(
        self,
        *,
        platform: str,
        account_id: str,
        device_id: str,
        updated_at: int | None = None,
    ) -> None:
        """Backfill ``device_id`` on an existing activations row, only if empty.

        Token-refresh spec §E: env-booted agents have a NULL ``device_id`` (they
        never ran the connect-code activation that persists it). The connection
        backfills the resolved id (the token's ``did``) at connect so the durable
        value lives in the DB and survives container recreation. The
        ``device_id IS NULL OR device_id = ''`` guard makes this idempotent and
        ensures it NEVER clobbers a value a connect-code activation already set.
        """
        device = device_id.strip() if isinstance(device_id, str) and device_id.strip() else None
        if not device:
            return
        updated = updated_at if updated_at is not None else _now_ms()

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                UPDATE activations
                SET device_id = ?, updated_at = ?
                WHERE platform = ? AND account_id = ?
                  AND (device_id IS NULL OR device_id = '')
                """,
                (device, updated, platform, account_id),
            )

        self._write("set_activation_device_id", write)

    def clear_activation_credentials(
        self,
        *,
        platform: str,
        account_id: str,
        updated_at: int | None = None,
    ) -> bool | None:
        """Blank the token columns but KEEP identity (re-pair mode).

        Token-refresh spec §C.1: on permanent logout, clear access_token /
        refresh_token but keep user_id / owner_user_id / conversation_id /
        device_id so a fresh connect code re-pairs the same identity.
        """
        updated = updated_at if updated_at is not None else _now_ms()

        def write(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                """
                UPDATE activations
                SET access_token = NULL, refresh_token = NULL, updated_at = ?
                WHERE platform = ? AND account_id = ?
                """,
                (updated, platform, account_id),
            )
            return cursor.rowcount == 1

        return self._write("clear_activation_credentials", write)

    def get_activation_credentials(
        self,
        *,
        platform: str,
        account_id: str,
    ) -> ActivationCredentials | None:
        self.initialize()
        if self._disabled:
            return None
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT user_id, owner_user_id, access_token, refresh_token,
                       device_id, activated_at
                FROM activations
                WHERE platform = ? AND account_id = ?
                """,
                (platform, account_id),
            ).fetchone()
            if row is None:
                return None
            user_id = str(row[0] or "").strip()
            owner_user_id = str(row[1] or "").strip()
            access_token = str(row[2] or "").strip()
            refresh_token = str(row[3] or "").strip() or None
            device_id = str(row[4] or "").strip() or None
            try:
                activated_at = int(row[5]) if row[5] is not None else None
            except (TypeError, ValueError):
                activated_at = None
            if not user_id or not owner_user_id or not access_token:
                return None
            return ActivationCredentials(
                user_id=user_id,
                owner_user_id=owner_user_id,
                access_token=access_token,
                refresh_token=refresh_token,
                device_id=device_id,
                activated_at=activated_at,
            )
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

    def get_activation_owner_user_id(
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
                SELECT owner_user_id
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

    def list_recent_group_messages(
        self,
        account_id: str,
        chat_id: str,
        limit: int,
    ) -> list[dict]:
        """Return the last *limit* messages for (account_id, chat_id), oldest-first.

        Uses the ``idx_clawchat_messages_chat_created`` index.  The query
        selects DESC to get the most-recent rows, then the caller-visible result
        is reversed so the list is oldest-first (chronological order for context
        injection).  Returns an empty list when the store is disabled or on any
        error.

        Only real conversation rows are returned: rows are restricted to
        ``event_type`` in {``message.send``, ``message.reply``}, which excludes
        ``message.error`` / internal records (issue #2 item 4). Those persist in
        ``clawchat_messages`` for audit/dedup but must NOT be prepended into the
        @-mention prior-context prompt as if they were group history. (Mirrors the
        sibling OpenClaw query filtering to real message kinds.)
        """
        self.initialize()
        if self._disabled:
            return []
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                rows = conn.execute(
                    """
                    SELECT message_id, text, created_at
                    FROM clawchat_messages
                    WHERE account_id = ? AND chat_id = ?
                      AND event_type IN ('message.send', 'message.reply')
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (account_id, chat_id, limit),
                ).fetchall()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            logger.warning(
                "clawchat database read failed operation=list_recent_group_messages",
                exc_info=True,
            )
            return []
        # Reverse so result is oldest-first.
        return [
            {"message_id": row[0], "text": row[1], "created_at": row[2]}
            for row in reversed(rows)
        ]

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

    def get_owner_profile(
        self, *, platform: str, account_id: str
    ) -> "OwnerProfileRow | None":
        self.initialize()
        if self._disabled:
            return None
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT platform, account_id, owner_user_id, nickname,
                       avatar_url, bio, locale, updated_at
                FROM owner_profile WHERE platform = ? AND account_id = ?
                """,
                (platform, account_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return OwnerProfileRow(
            platform=str(row[0]),
            account_id=str(row[1]),
            owner_user_id=(str(row[2]) if row[2] is not None else None),
            nickname=(str(row[3]) if row[3] is not None else None),
            avatar_url=(str(row[4]) if row[4] is not None else None),
            bio=(str(row[5]) if row[5] is not None else None),
            locale=(str(row[6]) if row[6] is not None else None),
            updated_at=int(row[7]),
        )

    def upsert_owner_profile(
        self,
        *,
        platform: str,
        account_id: str,
        owner_user_id: str | None,
        nickname: str | None,
        avatar_url: str | None,
        bio: str | None,
        locale: str | None,
    ) -> None:
        now = _now_ms()

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO owner_profile
                  (platform, account_id, owner_user_id, nickname,
                   avatar_url, bio, locale, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(platform, account_id) DO UPDATE SET
                  owner_user_id = excluded.owner_user_id,
                  nickname = excluded.nickname,
                  avatar_url = excluded.avatar_url,
                  bio = excluded.bio,
                  locale = excluded.locale,
                  updated_at = excluded.updated_at
                """,
                (platform, account_id, owner_user_id, nickname,
                 avatar_url, bio, locale, now),
            )

        self._write("upsert_owner_profile", _op)

    def get_liveware_sample(
        self, *, platform: str, account_id: str
    ) -> "LivewareSampleRow | None":
        self.initialize()
        if self._disabled:
            return None
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                """
                SELECT platform, account_id, app_id, app_name, port, public_url,
                       sample_version, status, last_error, intro_sent,
                       created_at, updated_at
                FROM liveware_sample WHERE platform = ? AND account_id = ?
                """,
                (platform, account_id),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return LivewareSampleRow(
            platform=str(row[0]), account_id=str(row[1]), app_id=str(row[2]),
            app_name=str(row[3]), port=int(row[4]),
            public_url=(str(row[5]) if row[5] is not None else None),
            sample_version=str(row[6]), status=str(row[7]),
            last_error=(str(row[8]) if row[8] is not None else None),
            intro_sent=int(row[9]), created_at=int(row[10]), updated_at=int(row[11]),
        )

    def upsert_liveware_sample(
        self, *, platform: str, account_id: str, app_id: str, app_name: str,
        port: int, public_url: str | None, sample_version: str, status: str,
        last_error: str | None = None,
    ) -> None:
        now = _now_ms()

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO liveware_sample
                  (platform, account_id, app_id, app_name, port, public_url,
                   sample_version, status, last_error, intro_sent,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(platform, account_id) DO UPDATE SET
                  app_id = excluded.app_id,
                  app_name = excluded.app_name,
                  port = excluded.port,
                  public_url = excluded.public_url,
                  sample_version = excluded.sample_version,
                  status = excluded.status,
                  last_error = excluded.last_error,
                  updated_at = excluded.updated_at
                """,
                (platform, account_id, app_id, app_name, port, public_url,
                 sample_version, status, last_error, now, now),
            )

        self._write("upsert_liveware_sample", _op)

    def update_liveware_sample_status(
        self, *, platform: str, account_id: str, status: str,
        last_error: str | None = None,
    ) -> None:
        now = _now_ms()

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE liveware_sample SET status = ?, last_error = ?, updated_at = ? "
                "WHERE platform = ? AND account_id = ?",
                (status, last_error, now, platform, account_id),
            )

        self._write("update_liveware_sample_status", _op)

    def mark_liveware_sample_intro_sent(
        self, *, platform: str, account_id: str
    ) -> None:
        now = _now_ms()

        def _op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE liveware_sample SET intro_sent = 1, updated_at = ? "
                "WHERE platform = ? AND account_id = ?",
                (now, platform, account_id),
            )

        self._write("mark_liveware_sample_intro_sent", _op)

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


def make_owner_profile_persister(
    store: ClawChatStore,
    *,
    platform: str = "hermes",
    account_id: str = "default",
):
    """Bind a persist callback for pull_owner_metadata.

    Maps the flat GET /v1/agents/me/owner payload onto the owner_profile
    cache row. Missing fields are stored as NULL (never empty strings).
    """

    def _text(value: object) -> str | None:
        if value is None:
            return None
        return value if isinstance(value, str) else str(value)

    def _persist(owner: dict) -> None:
        store.upsert_owner_profile(
            platform=platform,
            account_id=account_id,
            owner_user_id=_text(owner.get("id")),
            nickname=_text(owner.get("nickname")),
            avatar_url=_text(owner.get("avatar_url")),
            bio=_text(owner.get("bio")),
            locale=_text(owner.get("locale")),
        )

    return _persist
