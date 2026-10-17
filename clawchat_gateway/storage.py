from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from clawchat_gateway.hermes_home import hermes_home
from clawchat_gateway.tool_grants import (
    PENDING_TTL_MS, _finite, is_pending_tool_grant_payload,
)

from clawchat_gateway.tool_capabilities import (
    GrantSelector, parse_grant_selector, normalize_grant_selectors, selector_string, plan_deny_removal,
)

logger = logging.getLogger(__name__)

DB_FILENAME = "clawchat.sqlite"
CLAWCHAT_DIRNAME = "clawchat"
_PROFILE_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
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

RECALLED_MESSAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS recalled_messages (
  account_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  recalled_at INTEGER NOT NULL,
  PRIMARY KEY (account_id, message_id)
);
"""

# One row per group whose shared Hermes session the plugin has started: the
# first turn of that session is seeded with recent group history, once.
GROUP_SHARED_SESSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_shared_sessions (
  account_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  started_at INTEGER NOT NULL,
  PRIMARY KEY (account_id, chat_id)
);
"""

# Explicit message ids from the model (a react target, a mention's reply-to) and
# the chat ids beside them are matched case-insensitively against the ledger
# (find_seen_message_in_chat / find_stored_chat_id). The BINARY indexes above
# cannot serve a COLLATE NOCASE comparison, so without these every such tool
# call would scan the whole unpruned ledger.
MESSAGE_TARGET_NOCASE_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_clawchat_messages_message_id_nocase
  ON clawchat_messages(message_id COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_clawchat_messages_chat_id_nocase
  ON clawchat_messages(chat_id COLLATE NOCASE);
"""

REVOKED_REFRESH_SCHEMA = """
CREATE TABLE IF NOT EXISTS revoked_refresh_tokens (
  platform TEXT NOT NULL,
  account_id TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  rejected_at INTEGER NOT NULL,
  PRIMARY KEY (platform, account_id)
);
"""

TOOL_GRANTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_grants (
  account_id TEXT NOT NULL,
  grantee_user_id TEXT NOT NULL,
  grantee_name TEXT NOT NULL,
  tool TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('direct', 'ask')),
  label TEXT,
  expires_at INTEGER,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (account_id, grantee_user_id, tool)
);
CREATE TABLE IF NOT EXISTS tool_grant_pending (
  account_id TEXT NOT NULL,
  code TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  consumed_at INTEGER,
  PRIMARY KEY (account_id, code)
);
"""


TOOL_GRANTS_V2_SCHEMA = """
CREATE TABLE tool_grants_v2 (
  account_id TEXT NOT NULL, grantee_user_id TEXT NOT NULL,
  grantee_name TEXT NOT NULL,
  what_kind TEXT NOT NULL CHECK (what_kind IN ('tool','capability','all')),
  what TEXT NOT NULL CHECK (length(what) > 0),
  mode TEXT NOT NULL CHECK (mode IN ('direct','ask','deny')),
  label TEXT, expires_at INTEGER, created_at INTEGER NOT NULL,
  CHECK (what_kind <> 'all' OR what = 'all'),
  CHECK (what_kind <> 'tool' OR what NOT IN
    ('clawchat_tool_grant','clawchat_tool_revoke','clawchat_tool_grants_list','clawchat_tool_request')),
  PRIMARY KEY (account_id, grantee_user_id, what_kind, what)
);
INSERT INTO tool_grants_v2
  (account_id, grantee_user_id, grantee_name, what_kind, what, mode, label, expires_at, created_at)
  SELECT account_id, grantee_user_id, grantee_name, 'tool', tool, mode, label, expires_at, created_at
  FROM tool_grants;
CREATE TEMP TABLE tool_grants_copy_check (valid INTEGER NOT NULL CHECK (valid = 1));
INSERT INTO tool_grants_copy_check VALUES
  ((SELECT count(*) FROM tool_grants_v2) = (SELECT count(*) FROM tool_grants));
DROP TABLE tool_grants_copy_check;
DROP TABLE tool_grants;
ALTER TABLE tool_grants_v2 RENAME TO tool_grants;
ALTER TABLE tool_grant_pending ADD COLUMN purpose TEXT NOT NULL DEFAULT 'grant'
  CHECK (purpose IN ('grant', 'request'));
ALTER TABLE tool_grant_pending ADD COLUMN payload_version INTEGER NOT NULL DEFAULT 1
  CHECK (payload_version IN (1, 2));
"""


TOOL_GRANT_REQUESTS_SCHEMA = """
CREATE TABLE tool_grant_requests (
  account_id TEXT NOT NULL, request_id TEXT NOT NULL,
  code TEXT NOT NULL CHECK(length(code) = 6 AND code NOT GLOB '*[^0-9]*'),
  requester_user_id TEXT NOT NULL, requester_name TEXT NOT NULL, capability_id TEXT NOT NULL,
  source_chat_id TEXT NOT NULL, source_chat_type TEXT NOT NULL CHECK(source_chat_type IN ('direct','group')),
  source_session_key TEXT NOT NULL, source_message_id TEXT NOT NULL,
  reason TEXT NOT NULL, request_summary TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('sending','pending','allowed','asked','denied','expired','failed')),
  created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  sent_at INTEGER, decided_at INTEGER,
  decision_mode TEXT CHECK(decision_mode IN ('direct','ask','deny')), decided_by_user_id TEXT,
  wake_state TEXT NOT NULL CHECK(wake_state IN ('none','pending','dispatching','delivered','failed')),
  wake_key TEXT NOT NULL UNIQUE CHECK(length(wake_key) > 0), wake_attempted_at INTEGER, wake_delivered_at INTEGER,
  last_error_code TEXT, wake_notified_at INTEGER,
  PRIMARY KEY(account_id,request_id), UNIQUE(account_id,code),
  CHECK(status NOT IN ('allowed','asked','denied') OR
    (decision_mode IS NOT NULL AND decided_by_user_id IS NOT NULL AND decided_at IS NOT NULL)),
  FOREIGN KEY(account_id,code) REFERENCES tool_grant_pending(account_id,code)
);
CREATE INDEX tool_request_person_cap_created ON tool_grant_requests(account_id,requester_user_id,capability_id,created_at);
CREATE INDEX tool_request_account_created ON tool_grant_requests(account_id,created_at);
CREATE INDEX tool_request_status_expiry ON tool_grant_requests(account_id,status,expires_at);
"""


TOOL_REQUEST_NOTICE_CLAIMS_SCHEMA = """
ALTER TABLE tool_grant_requests ADD COLUMN notice_claim_key TEXT;
ALTER TABLE tool_grant_requests ADD COLUMN notice_claimed_at INTEGER;
CREATE INDEX tool_request_notice_claim ON tool_grant_requests(account_id, notice_claim_key);
"""


def refresh_token_fingerprint(refresh_token: str) -> str:
    """Fingerprint the credential as submitted to the refresh endpoint."""
    return hashlib.sha256(refresh_token.strip().encode("utf-8")).hexdigest()


MIGRATIONS = [
    (1, "initial_schema", INITIAL_SCHEMA),
    (2, "message_id_dedup", MESSAGE_ID_DEDUP_SCHEMA),
    (3, "activation_bootstrap", ACTIVATION_BOOTSTRAP_SCHEMA),
    (4, "connection_metadata", CONNECTION_METADATA_SCHEMA),
    (5, "activation_owner_user_id", ACTIVATION_OWNER_USER_ID_SCHEMA),
    (6, "activation_device_id", ACTIVATION_DEVICE_ID_SCHEMA),
    (7, "liveware_sample", LIVEWARE_SAMPLE_SCHEMA),
    (8, "owner_profile", OWNER_PROFILE_SCHEMA),
    (9, "recalled_messages", RECALLED_MESSAGES_SCHEMA),
    (10, "group_shared_sessions", GROUP_SHARED_SESSIONS_SCHEMA),
    (11, "message_target_nocase_indexes", MESSAGE_TARGET_NOCASE_SCHEMA),
    (12, "revoked_refresh_tokens", REVOKED_REFRESH_SCHEMA),
    (13, "tool_grants", TOOL_GRANTS_SCHEMA),
    (14, "tool_grants_v2", TOOL_GRANTS_V2_SCHEMA),
    (15, "tool_grant_requests", TOOL_GRANT_REQUESTS_SCHEMA),
    (16, "tool_request_notice_claims", TOOL_REQUEST_NOTICE_CLAIMS_SCHEMA),
]

# Lookup behind ClawChatStore.find_seen_message_in_chat: a real chat message
# (message.send / message.reply, either direction; a failed send is stored as
# message.error and never reached anyone) with this id in this chat. One seek
# on the NOCASE message-id index; account and chat are post-filters. INDEXED BY
# pins the seek: without table statistics the planner may pick the chat-id
# index and walk the whole chat. Module-level so a test can pin the plan.
FIND_SEEN_MESSAGE_SQL = """
SELECT chat_id, message_id
FROM clawchat_messages INDEXED BY idx_clawchat_messages_message_id_nocase
WHERE message_id = ? COLLATE NOCASE
  AND account_id = ?
  AND chat_id = ? COLLATE NOCASE
  AND kind = 'message'
  AND event_type IN ('message.send', 'message.reply')
ORDER BY direction = 'inbound' DESC, id DESC
LIMIT 1
"""

# Lookup behind ClawChatStore.find_stored_chat_id: an inbound row first (its
# case is the server's), then any row. Each half is one seek on the NOCASE
# chat-id index with LIMIT 1, so a long chat is never walked.
FIND_STORED_CHAT_ID_SQL = """
SELECT chat_id FROM (
  SELECT chat_id, 0 AS rank FROM (
    SELECT chat_id FROM clawchat_messages
    WHERE chat_id = ? COLLATE NOCASE AND account_id = ? AND direction = 'inbound'
    LIMIT 1
  )
  UNION ALL
  SELECT chat_id, 1 AS rank FROM (
    SELECT chat_id FROM clawchat_messages
    WHERE chat_id = ? COLLATE NOCASE AND account_id = ?
    LIMIT 1
  )
)
ORDER BY rank
LIMIT 1
"""

_stores: dict[Path, ClawChatStore] = {}
_store_lock = threading.Lock()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _hermes_home() -> Path:
    return hermes_home()


def _active_profile_name() -> str:
    """Resolve the active Hermes profile name.

    Prefers the host's own resolver (importable at runtime inside the Hermes
    process); falls back to deriving it from the HERMES_HOME layout when
    hermes_cli is not importable (unit tests, standalone CLI use).
    """
    try:
        from hermes_cli.profiles import get_active_profile_name  # type: ignore

        name = get_active_profile_name()
        if name:
            return name
    except Exception:  # noqa: BLE001
        pass
    home = _hermes_home()
    if home.parent.name == "profiles":
        return home.name
    return "default"


def is_default_profile() -> bool:
    """True when running as the Hermes **default** profile (the primary agent).

    Hermes runs one gateway process per profile, but some ClawChat features own
    *host-global* singletons that co-located profiles cannot share — notably the
    Liveware Sample demo, which binds a fixed TCP port. (Its ``liveware`` CLI
    login store under ``$HOME/.clawling`` is host-wide as well, but each call
    names the agent's own ``--account``.) Such features gate on this so only
    the primary agent owns the shared resource; named profiles skip it. Also
    decides whether the legacy ``~/clawchat/`` agent files are still read
    (``agent_files``): only the default profile may fall back to them.
    """
    return _active_profile_name() == "default"


def _sanitize_profile(name: str) -> str:
    cleaned = _PROFILE_SAFE.sub("-", name).strip("-")
    return cleaned or "default"


def _db_filename(profile: str) -> str:
    safe = _sanitize_profile(profile)
    if safe == "default":
        return DB_FILENAME
    return f"clawchat-{safe}.sqlite"


def clawchat_data_dir() -> Path:
    """Dedicated ClawChat data directory: ``$HERMES_HOME/clawchat``."""
    return _hermes_home() / CLAWCHAT_DIRNAME


def default_db_path() -> Path:
    return clawchat_data_dir() / _db_filename(_active_profile_name())


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



# Conversation rows for history context: real messages only (message.send /
# message.reply), and only ordinary ones — a stored frame whose
# payload.message_mode is a non-empty string other than "normal" (the agent's
# own tool progress, notices, reasoning; docs/client-integration.md §7.5) is
# not part of the conversation.
_CONVERSATION_ROWS_SQL = """
    SELECT {columns}
    FROM clawchat_messages
    WHERE account_id = ? AND chat_id = ?
      AND event_type IN ('message.send', 'message.reply')
      {mode_filter}
    ORDER BY created_at DESC, rowid DESC
    LIMIT ?
"""
_NORMAL_MODE_FILTER_SQL = """
      AND NOT COALESCE(
        json_valid(raw_json)
        AND json_type(raw_json, '$.payload.message_mode') = 'text'
        AND trim(json_extract(raw_json, '$.payload.message_mode')) NOT IN ('', 'normal'),
        0
      )
"""


def _select_conversation_rows(
    conn: sqlite3.Connection, columns: str, account_id: str, chat_id: str, limit: int
) -> list[tuple]:
    params = (account_id, chat_id, limit)
    try:
        return conn.execute(
            _CONVERSATION_ROWS_SQL.format(columns=columns, mode_filter=_NORMAL_MODE_FILTER_SQL),
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        # An SQLite built without the JSON functions: keep the history,
        # unfiltered, rather than lose it.
        return conn.execute(
            _CONVERSATION_ROWS_SQL.format(columns=columns, mode_filter=""), params
        ).fetchall()


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
                self._relocate_legacy_db()
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

    def _tool_grants_connection(self) -> sqlite3.Connection:
        # Grant APIs propagate errors; a failed write must never look successful.
        self.initialize()
        if self._disabled:
            raise RuntimeError("clawchat sqlite database is unavailable")
        return sqlite3.connect(self.db_path)

    def _tool_grants_transaction(self, callback: Callable[[sqlite3.Connection], _T]) -> _T:
        conn = self._tool_grants_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = callback(conn)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _request_row(conn, account_id, request_id):
        cursor = conn.execute('SELECT * FROM tool_grant_requests WHERE account_id = ? AND request_id = ?',
                              (account_id, request_id))
        row = cursor.fetchone()
        return dict(zip((col[0] for col in cursor.description), row)) if row else None

    def get_request(self, *, account_id, request_id):
        return self._tool_grants_transaction(lambda conn: self._request_row(conn, account_id, request_id))

    def reserve_request(self, *, account_id, owner_user_id, origin, requester_name, capability_id, reason, now=None):
        """Rate check, quota, shared code reservation and request write serialize together."""
        import secrets
        from dataclasses import asdict
        from clawchat_gateway.tool_capabilities import TOOL_CAPABILITIES
        from clawchat_gateway.tool_grants import generate_grant_code, resolve_turn_grants
        from clawchat_gateway.tool_requests import (
            REQUEST_CARD_TTL_MS, REQUEST_COOLDOWN_MS, REQUEST_DAILY_LIMIT,
            resolve_request_daily_window, request_grant_block, sanitize_request_text,
        )
        now = _now_ms() if now is None else now
        if (not _finite(now) or not account_id or not owner_user_id or capability_id not in TOOL_CAPABILITIES
                or not sanitize_request_text(requester_name, 120) or not re.fullmatch(r'usr_[^\s]+', origin.requester_user_id)
                or origin.requester_user_id == owner_user_id
                or not origin.source_chat_id or not origin.source_session_key or not origin.source_message_id
                or origin.source_chat_type not in ('direct', 'group')):
            raise ValueError('Invalid request')

        def write(conn):
            self._expire_requests(conn, account_id, now)
            cursor = conn.execute('SELECT * FROM tool_grants WHERE account_id = ? AND grantee_user_id = ?',
                                  (account_id, origin.requester_user_id))
            rows = [dict(zip((col[0] for col in cursor.description), row)) for row in cursor.fetchall()]
            grants = resolve_turn_grants([origin.requester_user_id], owner_user_id, rows, now)
            block = request_grant_block(grants, capability_id, now=now)
            if block:
                return dict(status='blocked', reason=block)
            existing = conn.execute('''SELECT status FROM tool_grant_requests WHERE account_id = ? AND requester_user_id = ?
                AND capability_id = ? AND ((status IN ('sending','pending') AND expires_at > ?) OR (status <> 'failed' AND created_at > ?)) LIMIT 1''',
                (account_id, origin.requester_user_id, capability_id, now, now - REQUEST_COOLDOWN_MS)).fetchone()
            if existing:
                return dict(status='waiting')
            window = resolve_request_daily_window(now)
            count = conn.execute('SELECT count(*) FROM tool_grant_requests WHERE account_id = ? AND created_at > ? AND created_at <= ?',
                                 (account_id, window['start'], window['end'])).fetchone()[0]
            if count >= REQUEST_DAILY_LIMIT:
                return dict(status='blocked', reason='rate-limited')
            request_id, wake_key = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
            for attempt in range(6):
                code = generate_grant_code()
                try:
                    conn.execute('''INSERT INTO tool_grant_pending
                        (account_id, code, payload_json, created_at, expires_at, purpose, payload_version)
                        VALUES (?, ?, ?, ?, ?, 'request', 2)''',
                        (account_id, code, json.dumps({'request_id': request_id}), now, now + REQUEST_CARD_TTL_MS))
                    break
                except sqlite3.IntegrityError:
                    if attempt == 5:
                        raise
            values = dict(account_id=account_id, request_id=request_id, code=code, **asdict(origin),
                          requester_name=sanitize_request_text(requester_name, 120), capability_id=capability_id,
                          reason=sanitize_request_text(reason), created_at=now, updated_at=now,
                          expires_at=now + REQUEST_CARD_TTL_MS, status='sending', wake_state='none', wake_key=wake_key)
            values['request_summary'] = sanitize_request_text(values['request_summary'], 500)
            columns = ','.join(values)
            conn.execute(f'INSERT INTO tool_grant_requests ({columns}) VALUES ({",".join("?" for _ in values)})', tuple(values.values()))
            return dict(status='sending', request=self._request_row(conn, account_id, request_id))
        return self._tool_grants_transaction(write)

    def mark_sent(self, *, account_id, request_id, now=None):
        now = _now_ms() if now is None else now
        return self._tool_grants_transaction(lambda conn: conn.execute('''UPDATE tool_grant_requests
            SET status = 'pending', sent_at = ?, updated_at = ? WHERE account_id = ? AND request_id = ? AND status = 'sending' ''',
            (now, now, account_id, request_id)).rowcount)

    def mark_failed(self, *, account_id, request_id, now=None):
        now = _now_ms() if now is None else now
        return self._tool_grants_transaction(lambda conn: conn.execute('''UPDATE tool_grant_requests
            SET status = 'failed', last_error_code = 'send-failed', updated_at = ?
            WHERE account_id = ? AND request_id = ? AND status = 'sending' ''', (now, account_id, request_id)).rowcount)

    @staticmethod
    def _expire_requests(conn, account_id, now):
        conn.execute("UPDATE tool_grant_requests SET status = 'expired', updated_at = ? WHERE account_id = ? AND status IN ('sending','pending') AND expires_at <= ?",
                     (now, account_id, now))

    def confirm_request(self, *, account_id, code, mode, decided_by_user_id, ask_available=True, now=None):
        now = _now_ms() if now is None else now
        if not _finite(now) or mode not in ('direct', 'ask', 'deny') or not decided_by_user_id:
            raise ValueError('Invalid request decision')
        def write(conn):
            self._expire_requests(conn, account_id, now)
            pending = conn.execute('''SELECT request_id FROM tool_grant_requests WHERE account_id = ? AND code = ?
                AND status IN ('sending','pending') AND expires_at > ?''', (account_id, code, now)).fetchone()
            if not pending:
                return None
            request = self._request_row(conn, account_id, pending[0])
            registered = conn.execute("SELECT 1 FROM tool_grant_pending WHERE account_id = ? AND code = ? AND purpose = 'request' AND consumed_at IS NULL AND expires_at > ?",
                                      (account_id, code, now)).fetchone()
            if not registered:
                return None
            if mode == 'ask' and not ask_available:
                raise ValueError('ask-unavailable')
            payload = dict(grantees=[dict(id=request['requester_user_id'], name=request['requester_name'])],
                           tools=['cap:' + request['capability_id']], mode=mode)
            rows = self._write_grant_payload(conn, account_id, payload, now)
            conn.execute('UPDATE tool_grant_pending SET consumed_at = ? WHERE account_id = ? AND code = ?', (now, account_id, code))
            conn.execute('''UPDATE tool_grant_requests SET status = ?, decision_mode = ?, decided_by_user_id = ?,
                decided_at = ?, updated_at = ?, wake_state = 'pending' WHERE account_id = ? AND request_id = ?''',
                ({'direct': 'allowed', 'ask': 'asked', 'deny': 'denied'}[mode], mode, decided_by_user_id, now, now, account_id, pending[0]))
            return dict(request=self._request_row(conn, account_id, pending[0]), rows=rows)
        return self._tool_grants_transaction(write)

    def claim_wake(self, *, account_id, wake_key, now=None):
        now = _now_ms() if now is None else now
        def write(conn):
            cursor = conn.execute('''UPDATE tool_grant_requests SET wake_state = 'dispatching', wake_attempted_at = ?, updated_at = ?
                WHERE account_id = ? AND wake_key = ? AND wake_state = 'pending'
                AND status IN ('allowed','asked','denied') AND expires_at > ?''', (now, now, account_id, wake_key, now))
            if cursor.rowcount != 1:
                return None
            request_id = conn.execute('SELECT request_id FROM tool_grant_requests WHERE account_id = ? AND wake_key = ?', (account_id, wake_key)).fetchone()[0]
            return self._request_row(conn, account_id, request_id)
        return self._tool_grants_transaction(write)

    def finish_wake(self, *, account_id, wake_key, now=None):
        return self._complete_wake(account_id, wake_key, None, now)

    def fail_wake(self, *, account_id, wake_key, error='dispatch-failed', now=None):
        if error not in ('dispatch-failed', 'session-unavailable', 'host-rejected', 'dispatch-timeout'):
            raise ValueError('Invalid wake error')
        return self._complete_wake(account_id, wake_key, error, now)

    def _complete_wake(self, account_id, wake_key, error, now):
        now = _now_ms() if now is None else now
        return self._tool_grants_transaction(lambda conn: conn.execute('''UPDATE tool_grant_requests SET wake_state = ?,
            wake_delivered_at = ?, updated_at = ?, last_error_code = ?
            WHERE account_id = ? AND wake_key = ? AND wake_state = 'dispatching' ''',
            ('failed' if error else 'delivered', None if error else now, now, error, account_id, wake_key)).rowcount)

    def _recover_stale(self, conn, account_id, now):
        from clawchat_gateway.tool_requests import REQUEST_WAKE_STALE_MS, REQUEST_NOTICE_CLAIM_TTL_MS
        self._expire_requests(conn, account_id, now)
        conn.execute('''UPDATE tool_grant_requests SET wake_state = 'failed', last_error_code = 'dispatch-timeout', updated_at = ?
            WHERE account_id = ? AND wake_state = 'dispatching' AND (wake_attempted_at IS NULL OR wake_attempted_at <= ?)''',
            (now, account_id, now - REQUEST_WAKE_STALE_MS))
        conn.execute('''UPDATE tool_grant_requests SET notice_claim_key = NULL, notice_claimed_at = NULL
            WHERE account_id = ? AND notice_claim_key IS NOT NULL
            AND (notice_claimed_at IS NULL OR notice_claimed_at <= ? OR expires_at <= ?)''',
            (account_id, now - REQUEST_NOTICE_CLAIM_TTL_MS, now))

    def recover_stale(self, *, account_id, now=None):
        now = _now_ms() if now is None else now
        def write(conn):
            self._recover_stale(conn, account_id, now)
            return [row[0] for row in conn.execute('''SELECT wake_key FROM tool_grant_requests WHERE account_id = ?
                AND wake_state = 'pending' AND status IN ('allowed','asked','denied') AND expires_at > ?''', (account_id, now))]
        return self._tool_grants_transaction(write)

    def take_pending_notice(self, *, account_id, requester_ids, session_key, chat_id, chat_type, now=None):
        """Claim notices on the original route; host acceptance must confirm consumption."""
        import secrets
        now = _now_ms() if now is None else now
        def write(conn):
            self._recover_stale(conn, account_id, now)
            ids = [row[0] for row in conn.execute('''SELECT request_id FROM tool_grant_requests WHERE account_id = ?
                AND source_session_key = ? AND source_chat_id = ? AND source_chat_type = ? AND expires_at > ?
                AND wake_state = 'failed' AND wake_notified_at IS NULL AND notice_claim_key IS NULL
                AND status IN ('allowed','asked','denied')''',
                (account_id, session_key, chat_id, chat_type, now))]
            requests = [self._request_row(conn, account_id, id_) for id_ in ids]
            requests = [request for request in requests if request['requester_user_id'] in requester_ids]
            claim_key = secrets.token_urlsafe(24)
            for request in requests:
                conn.execute('''UPDATE tool_grant_requests SET notice_claim_key = ?, notice_claimed_at = ?, updated_at = ?
                    WHERE account_id = ? AND wake_key = ?''', (claim_key, now, now, account_id, request['wake_key']))
                request.update(notice_claim_key=claim_key, notice_claimed_at=now)
            return requests
        return self._tool_grants_transaction(write)

    def finish_pending_notice(self, *, account_id, claim_key, accepted, now=None):
        """Acknowledge or release this claim only; late acknowledgments cannot steal a new claim."""
        now = _now_ms() if now is None else now
        if not claim_key or not isinstance(accepted, bool) or not _finite(now):
            raise ValueError('Invalid notice acknowledgement')
        return self._tool_grants_transaction(lambda conn: conn.execute('''UPDATE tool_grant_requests
            SET wake_notified_at = CASE WHEN ? AND expires_at > ? THEN ? ELSE wake_notified_at END,
                notice_claim_key = NULL, notice_claimed_at = NULL, updated_at = ?
            WHERE account_id = ? AND notice_claim_key = ? AND wake_notified_at IS NULL''',
            (accepted, now, now, now, account_id, claim_key)).rowcount)

    def create_pending_grant(
        self, *, account_id: str, code: str, payload: dict[str, Any], now: int | None = None, payload_version: int = 2,
    ) -> None:
        now = _now_ms() if now is None else now
        if (not isinstance(account_id, str) or not account_id.strip()
                or not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code)
                or not _finite(now) or not is_pending_tool_grant_payload(payload, payload_version)):
            raise ValueError("Invalid pending tool grant")
        if payload_version == 2:
            payload = {**payload, "tools": normalize_grant_selectors(payload["tools"])}

        def write(conn: sqlite3.Connection) -> None:
            # A collision must not replace an existing or already consumed code.
            conn.execute(
                """INSERT INTO tool_grant_pending
                (account_id, code, payload_json, created_at, expires_at, consumed_at, purpose, payload_version)
                VALUES (?, ?, ?, ?, ?, NULL, 'grant', ?)""",
                (account_id, code, json.dumps(payload, ensure_ascii=False), now, now + PENDING_TTL_MS, payload_version),
            )

        self._tool_grants_transaction(write)

    @staticmethod
    def _upsert_tool_grant(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
        conn.execute(
            """INSERT INTO tool_grants
            (account_id, grantee_user_id, grantee_name, what_kind, what, mode, label, expires_at, created_at)
            VALUES (:account_id, :grantee_user_id, :grantee_name, :what_kind, :what, :mode, :label, :expires_at, :created_at)
            ON CONFLICT(account_id, grantee_user_id, what_kind, what) DO UPDATE SET
              grantee_name = excluded.grantee_name, mode = excluded.mode, label = excluded.label,
              expires_at = excluded.expires_at, created_at = excluded.created_at""",
            row,
        )

    def confirm_pending_grant(
        self, *, account_id: str, code: str, now: int | None = None, ask_available: bool = True,
    ) -> list[dict[str, Any]] | None:
        """Consume once atomically; invalid/expired codes return None, write errors raise."""
        now = _now_ms() if now is None else now
        if not _finite(now):
            raise ValueError("Invalid tool grant time")

        def write(conn: sqlite3.Connection) -> list[dict[str, Any]] | None:
            pending = conn.execute(
                """SELECT payload_json, payload_version FROM tool_grant_pending
                WHERE account_id = ? AND code = ? AND consumed_at IS NULL AND expires_at > ? AND purpose = 'grant'""",
                (account_id, code, now),
            ).fetchone()
            try:
                payload = json.loads(pending[0]) if pending else None
            except (TypeError, ValueError):
                payload = None
            if (not is_pending_tool_grant_payload(payload, pending[1] if pending else 2)
                    or (payload.get("expires_at") is not None and payload["expires_at"] <= now)):
                # Explicit rollback also covers malformed or expired grant payloads.
                conn.rollback()
                return None
            if payload["mode"] == "ask" and not ask_available:
                raise ValueError("ask-unavailable")
            rows = self._write_grant_payload(conn, account_id, payload, now, pending[1])
            conn.execute(
                "UPDATE tool_grant_pending SET consumed_at = ? WHERE account_id = ? AND code = ?",
                (now, account_id, code),
            )
            return rows

        return self._tool_grants_transaction(write)

    def _write_grant_payload(self, conn, account_id, payload, now, version=2):
        rows = []
        selectors = ([GrantSelector("tool", value) for value in dict.fromkeys(payload["tools"])] if version == 1 else
                     [parse_grant_selector(value) for value in normalize_grant_selectors(payload["tools"])])
        for grantee in payload["grantees"]:
            for selector in selectors:
                row = dict(account_id=account_id, grantee_user_id=grantee["id"],
                           grantee_name=grantee["name"], what_kind=selector.what_kind, what=selector.what,
                           mode=payload["mode"], label=payload.get("label"),
                           expires_at=payload.get("expires_at"), created_at=now)
                self._upsert_tool_grant(conn, row)
                rows.append(row)
        return rows

    def apply_grant(self, *, account_id: str, payload: dict, now: int | None = None) -> int:
        """Owner's explicit deny is immediate; direct/ask require a confirmation code."""
        now = _now_ms() if now is None else now
        if (not account_id or not _finite(now) or not is_pending_tool_grant_payload(payload)
                or payload["mode"] != "deny" or payload.get("expires_at") is not None and payload["expires_at"] <= now):
            raise ValueError("Invalid deny grant")
        return self._tool_grants_transaction(lambda conn: len(self._write_grant_payload(conn, account_id, payload, now)))

    def revoke_grants(self, *, account_id: str, user_id: str, tools: list[str] | None = None) -> int:
        if tools == []:
            return 0
        selectors = [parse_grant_selector(tool) for tool in normalize_grant_selectors(tools)] if tools is not None else []
        clause = " AND (" + " OR ".join("(what_kind = ? AND what = ?)" for _ in selectors) + ")" if selectors else ""
        values = (account_id, user_id, *(value for selector in selectors for value in selector))
        def write(conn):
            conn.row_factory = sqlite3.Row
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM tool_grants WHERE account_id = ? AND grantee_user_id = ?" + clause, values)]
            if any(row["mode"] == "deny" for row in rows) and plan_deny_removal(rows) != "immediate":
                raise ValueError("confirmation-required")
            return conn.execute("DELETE FROM tool_grants WHERE account_id = ? AND grantee_user_id = ?" + clause, values).rowcount
        return self._tool_grants_transaction(write)

    def list_grants(self, *, account_id: str, now: int | None = None, include_expired: bool = False) -> list[dict[str, Any]]:
        return self._select_tool_grants(account_id=account_id, now=now, include_expired=include_expired)

    def load_grants_for_users(
        self, *, account_id: str, user_ids: list[str], now: int | None = None,
    ) -> list[dict[str, Any]]:
        return self._select_tool_grants(account_id=account_id, user_ids=user_ids, now=now)

    def _select_tool_grants(
        self, *, account_id: str, now: int | None, user_ids: list[str] | None = None, include_expired: bool = False,
    ) -> list[dict[str, Any]]:
        now = _now_ms() if now is None else now
        if not _finite(now):
            raise ValueError("Invalid tool grant time")
        if user_ids == []:
            return []
        clause = " AND grantee_user_id IN (" + ", ".join("?" for _ in user_ids) + ")" if user_ids is not None else ""
        conn = self._tool_grants_connection()
        try:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(
                "SELECT * FROM tool_grants WHERE account_id = ?"
                + ("" if include_expired else " AND (expires_at IS NULL OR expires_at > ?)")
                + clause + " ORDER BY grantee_user_id, what_kind, what",
                (account_id, *((now,) if not include_expired else ()), *(user_ids or [])),
            ).fetchall()]
        finally:
            conn.close()

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
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        seed_if_missing: bool = False,
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
        seeded. With an expected access/refresh pair, update only the matching row;
        a mismatch or missing row returns False without seeding unless bootstrap
        explicitly permits ``seed_if_missing``. That INSERT uses DO NOTHING on
        conflict, preserving an existing or concurrently inserted row.
        """
        updated = updated_at if updated_at is not None else _now_ms()
        access = access_token.strip() if access_token and access_token.strip() else None
        refresh = refresh_token.strip() if refresh_token and refresh_token.strip() else None
        device = device_id.strip() if isinstance(device_id, str) and device_id.strip() else None

        def write(conn: sqlite3.Connection) -> bool:
            sql = """
                UPDATE activations
                SET access_token = ?,
                    refresh_token = ?,
                    device_id = COALESCE(?, device_id),
                    updated_at = ?
                WHERE platform = ? AND account_id = ?
                """
            params = (access, refresh, device, updated, platform, account_id)
            if expected_access_token is not None:
                sql += " AND access_token = ?"
                params += (expected_access_token,)
            if expected_refresh_token is not None:
                sql += " AND COALESCE(refresh_token, '') = ?"
                params += (expected_refresh_token,)
            cursor = conn.execute(sql, params)
            if cursor.rowcount == 1:
                return True
            if (
                expected_access_token is not None or expected_refresh_token is not None
            ) and not seed_if_missing:
                return False
            # Bootstrap may seed only an absent row: DO NOTHING preserves any
            # existing pair, including a row inserted before this write (§C.2).
            # conversation_id may be empty (env derives the home channel).
            seed_user = seed_user_id.strip() if isinstance(seed_user_id, str) and seed_user_id.strip() else None
            seed_owner = (
                seed_owner_user_id.strip()
                if isinstance(seed_owner_user_id, str) and seed_owner_user_id.strip()
                else None
            )
            if not seed_user or not seed_owner:
                return False
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
                ON CONFLICT(platform, account_id) DO NOTHING
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

    def mark_revoked_refresh_token(
        self, *, platform: str, account_id: str, refresh_token: str,
    ) -> bool:
        fingerprint = refresh_token_fingerprint(refresh_token)

        def write(conn: sqlite3.Connection) -> bool:
            conn.execute(
                """INSERT INTO revoked_refresh_tokens
                   (platform, account_id, fingerprint, rejected_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(platform, account_id) DO UPDATE SET
                   fingerprint = excluded.fingerprint, rejected_at = excluded.rejected_at""",
                (platform, account_id, fingerprint, _now_ms()),
            )
            return True

        return self._write("mark_revoked_refresh_token", write)

    def get_revoked_refresh_fingerprint(self, *, platform: str, account_id: str) -> str | None:
        self.initialize()
        if self._disabled:
            raise RuntimeError("ClawChat revoked-refresh guard unavailable")
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT fingerprint FROM revoked_refresh_tokens WHERE platform = ? AND account_id = ?",
                (platform, account_id),
            ).fetchone()
            return str(row[0]) if row else None
        finally:
            conn.close()

    def clear_revoked_refresh_token(self, *, platform: str, account_id: str) -> bool:
        def write(conn: sqlite3.Connection) -> bool:
            conn.execute(
                "DELETE FROM revoked_refresh_tokens WHERE platform = ? AND account_id = ?",
                (platform, account_id),
            )
            return True

        return self._write("clear_revoked_refresh_token", write)

    def clear_activation_credentials(
        self,
        *,
        platform: str,
        account_id: str,
        updated_at: int | None = None,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
    ) -> bool | None:
        """Blank the token columns but KEEP identity (re-pair mode).

        Token-refresh spec §C.1: on permanent logout, clear access_token /
        refresh_token but keep user_id / owner_user_id / conversation_id /
        device_id so a fresh connect code re-pairs the same identity. An expected
        access/refresh pair restricts the clear to the row that owned the request.
        """
        updated = updated_at if updated_at is not None else _now_ms()

        def write(conn: sqlite3.Connection) -> bool:
            sql = """
                UPDATE activations
                SET access_token = NULL, refresh_token = NULL, updated_at = ?
                WHERE platform = ? AND account_id = ?
                """
            params = (updated, platform, account_id)
            if expected_access_token is not None:
                sql += " AND access_token = ?"
                params += (expected_access_token,)
            if expected_refresh_token is not None:
                sql += " AND COALESCE(refresh_token, '') = ?"
                params += (expected_refresh_token,)
            cursor = conn.execute(sql, params)
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
                # §9.8: consult the recall tombstone on every ingest path.
                # BEGIN IMMEDIATE takes the write lock before the lookup, so a
                # recall cannot commit between this check and the INSERT below.
                # That is what makes the two frames order-independent: whichever
                # the server delivers first, a recalled message_id never ends up
                # in the ledger.
                conn.execute("BEGIN IMMEDIATE")
                recalled = conn.execute(
                    "SELECT 1 FROM recalled_messages WHERE account_id = ? AND message_id = ?",
                    (account_id, message_id),
                ).fetchone()
                if recalled is not None:
                    conn.rollback()
                    return False
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

    def recent_outbound_messages(
        self,
        *,
        account_id: str,
        chat_id: str,
        since_ms: int,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Newest-first outbound ``message`` rows for one chat since ``since_ms``.

        Each row carries ``message_id``, ``event_type``, ``text`` and the decoded
        ``raw`` frame (a failed send's ``text`` holds the error, its ``raw`` still
        holds the frame that was sent).
        """

        self.initialize()
        if self._disabled:
            return []
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                rows = conn.execute(
                """
                SELECT message_id, event_type, text, raw_json
                FROM clawchat_messages
                WHERE account_id = ?
                  AND direction = 'outbound'
                  AND kind = 'message'
                  AND chat_id = ?
                  AND message_id IS NOT NULL
                  AND created_at >= ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                    (account_id, chat_id, since_ms, limit),
                ).fetchall()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            logger.warning(
                "clawchat database read failed operation=recent_outbound_messages",
                exc_info=True,
            )
            return []
        out: list[dict[str, Any]] = []
        for message_id, event_type, text, raw_json in rows:
            try:
                raw = json.loads(raw_json) if raw_json else None
            except (TypeError, ValueError):
                raw = None
            out.append({"message_id": message_id, "event_type": event_type, "text": text, "raw": raw})
        return out

    def find_seen_message_in_chat(
        self,
        *,
        account_id: str,
        chat_id: str,
        message_id: str,
    ) -> tuple[str, str] | bool | None:
        """The stored ``(chat_id, message_id)`` of a message seen in this chat.

        Matches inbound messages and the agent's own sent ones, chat id and
        message id compared case-insensitively; the ids come back in their
        stored case so callers put the real ids on the wire. ``False`` when no
        row matches, ``None`` when the store cannot answer — callers must tell
        "not seen" from "cannot say".
        """
        if not chat_id or not message_id:
            return False
        self.initialize()
        if self._disabled:
            return None
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                row = conn.execute(
                    FIND_SEEN_MESSAGE_SQL, (message_id, account_id, chat_id)
                ).fetchone()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            logger.warning(
                "clawchat database read failed operation=find_seen_message_in_chat",
                exc_info=True,
            )
            return None
        if row is None or not isinstance(row[0], str) or not isinstance(row[1], str):
            return False
        return (row[0], row[1])

    def find_stored_chat_id(self, *, account_id: str, chat_id: str) -> str | bool | None:
        """``chat_id`` in the case the ledger already stores it (inbound first).

        ``False`` when the ledger has no row for that chat, ``None`` when the
        store cannot answer.
        """
        if not chat_id:
            return False
        self.initialize()
        if self._disabled:
            return None
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                row = conn.execute(
                    FIND_STORED_CHAT_ID_SQL, (chat_id, account_id, chat_id, account_id)
                ).fetchone()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            logger.warning(
                "clawchat database read failed operation=find_stored_chat_id",
                exc_info=True,
            )
            return None
        if row is None or not isinstance(row[0], str) or not row[0]:
            return False
        return row[0]

    def is_message_recalled(self, *, account_id: str, message_id: str | None) -> bool:
        """True when ``message_id`` carries a recall tombstone for this account."""
        if not message_id:
            return False
        self.initialize()
        if self._disabled:
            return False
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                row = conn.execute(
                    "SELECT 1 FROM recalled_messages WHERE account_id = ? AND message_id = ?",
                    (account_id, message_id),
                ).fetchone()
                return row is not None
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            logger.warning(
                "clawchat database read failed operation=is_message_recalled",
                exc_info=True,
            )
            return False

    def recall_message(
        self,
        *,
        account_id: str,
        message_id: str,
        recalled_at: int | None = None,
    ) -> int | None:
        """Record the permanent recall tombstone and purge the ledger row.

        The local half of a server-authorised recall (``message.recall``,
        ``docs/client-integration.md`` §9.8), and the only path that should be
        used for one. Both halves happen in a single transaction.

        The delete alone is not enough. Live delivery and inbox replay can both
        carry a recall, and a replay can carry the original and its recall
        back-to-back — so the purge can run before the message is ingested, and
        a Kafka redelivery can resurrect the row afterwards. The tombstone is
        what makes the arrival order irrelevant: :meth:`claim_message_once`
        refuses a tombstoned ``message_id`` whenever it turns up.

        Scoped to ``account_id`` — the same scope as the purge, never narrower,
        or a row could slip past the tombstone that the delete would have taken.

        Returns the number of ledger rows removed. ``0`` is a normal outcome,
        not a failure: the row may never have been stored, or the recall may
        have arrived first — the tombstone stands either way. ``None`` means the
        store is unavailable, which is NOT a successful purge.
        """
        # An empty id would tombstone and delete every row whose message_id is
        # '' — everything the store never managed to key. Refuse rather than
        # guess.
        if not message_id:
            return 0

        def write(conn: sqlite3.Connection) -> int:
            conn.execute(
                """
                INSERT OR IGNORE INTO recalled_messages(account_id, message_id, recalled_at)
                VALUES (?, ?, ?)
                """,
                (account_id, message_id, recalled_at if recalled_at is not None else _now_ms()),
            )
            cursor = conn.execute(
                """
                DELETE FROM clawchat_messages
                WHERE account_id = ? AND message_id = ?
                """,
                (account_id, message_id),
            )
            return int(cursor.rowcount)

        return self._write("recall_message", write)

    def delete_messages_by_message_id(
        self,
        *,
        account_id: str,
        message_id: str,
    ) -> int | None:
        """Erase every ledger row for one ``message_id``.

        **Not the recall path.** This writes no tombstone, so a recall routed
        through here loses to a redelivery, and to a replay that carries the
        recall before the message it withdraws. Use :meth:`recall_message` for
        ``message.recall`` (``docs/client-integration.md`` §9.8).

        **The only DELETE in this store, and deliberately so.** Nothing prunes
        ``clawchat_messages`` and it has no retention policy, so without this a
        message recalled ninety seconds after it was sent stays one @-mention
        away from being read back aloud by
        :meth:`list_recent_group_messages` for as long as the group is quiet.

        Scoped to ``account_id`` so one account's recall cannot reach another's
        rows, and matched on ``message_id`` — indexed by
        ``idx_clawchat_messages_message_id``, so this is a lookup, not a scan.

        Returns the number of rows removed. ``0`` is a normal outcome, not a
        failure: the row may never have been stored. ``None`` means the store
        is unavailable.
        """
        # An empty id would match every row whose message_id is '' — deleting
        # everything the store never managed to key. Refuse rather than guess.
        if not message_id:
            return 0

        def write(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                """
                DELETE FROM clawchat_messages
                WHERE account_id = ? AND message_id = ?
                """,
                (account_id, message_id),
            )
            return int(cursor.rowcount)

        return self._write("delete_messages_by_message_id", write)

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
        sibling OpenClaw query filtering to real message kinds.) Process messages
        (``payload.message_mode`` other than normal, e.g. the agent's own tool
        progress) are left out the same way.
        """
        self.initialize()
        if self._disabled:
            return []
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                rows = _select_conversation_rows(
                    conn, "message_id, text, created_at", account_id, chat_id, limit
                )
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

    def list_recent_group_transcript(
        self,
        account_id: str,
        chat_id: str,
        limit: int,
    ) -> list[dict]:
        """Last *limit* real messages of one group, oldest-first, with speaker.

        Like ``list_recent_group_messages`` (same filter, same index) but each
        row also carries ``direction`` and, for inbound rows, ``sender_id`` /
        ``sender_name`` taken from the stored frame, plus ``text_after_mentions``:
        the text with the @-mentions it opens with removed (the text itself when
        it opens with none), so a caller can tell ``@agent /new`` is a command.
        Empty list on any error.
        """
        from clawchat_gateway.inbound import split_leading_mentions

        self.initialize()
        if self._disabled or limit <= 0:
            return []
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                rows = _select_conversation_rows(
                    conn,
                    "message_id, text, created_at, direction, raw_json",
                    account_id,
                    chat_id,
                    limit,
                )
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            logger.warning(
                "clawchat database read failed operation=list_recent_group_transcript",
                exc_info=True,
            )
            return []
        result: list[dict] = []
        for message_id, text, created_at, direction, raw_json in reversed(rows):
            sender_id = sender_name = ""
            text_after_mentions = str(text or "")
            if direction == "inbound" and raw_json:
                try:
                    raw = json.loads(raw_json)
                except (TypeError, ValueError):
                    raw = None
                sender = raw.get("sender") if isinstance(raw, dict) else None
                if isinstance(sender, dict):
                    sender_id = str(sender.get("id") or "")
                    sender_name = str(sender.get("nick_name") or "")
                if isinstance(raw, dict):
                    try:
                        split = split_leading_mentions(text_after_mentions, raw)
                    except Exception:  # noqa: BLE001 - malformed stored frame
                        split = None
                    if split is not None:
                        text_after_mentions = split[1]
            result.append(
                {
                    "message_id": message_id,
                    "text": text,
                    "created_at": created_at,
                    "direction": direction,
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "text_after_mentions": text_after_mentions,
                }
            )
        return result

    def has_group_shared_session(self, account_id: str, chat_id: str) -> bool | None:
        """Whether the shared session of this group was started; None if unknown."""
        self.initialize()
        if self._disabled:
            return None
        try:
            conn = sqlite3.connect(self.db_path)
            try:
                row = conn.execute(
                    "SELECT 1 FROM group_shared_sessions WHERE account_id = ? AND chat_id = ?",
                    (account_id, chat_id),
                ).fetchone()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            logger.warning(
                "clawchat database read failed operation=has_group_shared_session",
                exc_info=True,
            )
            return None
        return row is not None

    def mark_group_shared_session(self, account_id: str, chat_id: str) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR IGNORE INTO group_shared_sessions(account_id, chat_id, started_at) "
                "VALUES (?, ?, ?)",
                (account_id, chat_id, _now_ms()),
            )

        self._write("mark_group_shared_session", write)

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
        """Upsert the single liveware-sample row for (platform, account_id).

        ``status`` is one of ``pending`` | ``active`` | ``failed`` | ``disabled``
        (see docs/liveware-sample.md). ``pending`` means ``liveware app create``
        succeeded but the app is not registered with ClawChat yet; it is written
        mid-bootstrap so a failure after create resumes with the same app id
        instead of minting (and leaking) another one. The column is a plain TEXT
        with no CHECK constraint, so adding the value needs no migration.
        """
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

    def _relocate_legacy_db(self) -> None:
        """One-time move of the pre-``clawchat/``-dir default DB into the data dir.

        Older installs stored the default profile's DB at
        ``$HERMES_HOME/clawchat.sqlite``; new installs use
        ``$HERMES_HOME/clawchat/clawchat.sqlite``. Only the default profile has a
        legacy location — named profiles are always fresh. Idempotent and
        fail-safe: on any move error we open the legacy file in place rather than
        creating an empty database.
        """
        target = self.db_path
        if target.name != DB_FILENAME or target.parent.name != CLAWCHAT_DIRNAME:
            return
        if target.exists():
            return
        legacy = target.parent.parent / DB_FILENAME
        if legacy == target or not legacy.exists():
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                src = legacy.with_name(legacy.name + suffix)
                if src.exists():
                    src.replace(target.with_name(target.name + suffix))
        except Exception:  # noqa: BLE001
            logger.warning(
                "clawchat db relocation failed; opening legacy path in place",
                exc_info=True,
            )
            if legacy.exists():
                self.db_path = legacy

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
    path = default_db_path().expanduser().resolve()
    with _store_lock:
        if path not in _stores:
            _stores[path] = ClawChatStore(path)
        return _stores[path]


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
