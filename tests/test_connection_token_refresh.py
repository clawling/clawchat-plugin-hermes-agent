from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

from clawchat_gateway import connection as conn_mod
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import (
    AUTO_LOGOUT_STATUS_MESSAGE,
    ClawChatConnection,
    ConnectionState,
)
from clawchat_gateway.token_refresh import RefreshOutcome


def _jwt(payload: dict) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'HS256'})}.{seg(payload)}.sig"


def _near_expiry_token() -> str:
    now = int(time.time())
    return _jwt({"exp": now + 300, "iat": now - 24 * 3600, "aid": "agt_x"})


def _fresh_token() -> str:
    now = int(time.time())
    return _jwt({"exp": now + 24 * 3600, "iat": now, "aid": "agt_x"})


def _jwt_with_did(did: str) -> str:
    now = int(time.time())
    return _jwt({"exp": now + 24 * 3600, "iat": now, "aid": "agt_x", "did": did})


# --- _refresh_device_id resolution (§E): stored → token `did` → get_device_id ---


def test_refresh_device_id_prefers_stored_value():
    store = _FakeStore(_FakeCreds("acc", "r0"))  # device_id="hermes-dev-1"
    c, _ = _make_connection(_jwt_with_did("hermes-token-did"), store=store)
    # A connect-code activation persisted its device id; it wins (and equals did).
    assert c._refresh_device_id() == "hermes-dev-1"


def test_refresh_device_id_falls_back_to_token_did_when_unstored():
    # Env-booted: no stored activations row, but the access token carries the
    # `did` the backend baked at login — the exact X-Device-Id refresh expects.
    c, _ = _make_connection(_jwt_with_did("hermes-host-frozen"))
    # The store (if any) has no row for this account → resolution uses token did.
    assert c._refresh_device_id() == "hermes-host-frozen"


def test_refresh_device_id_token_did_when_stored_device_empty():
    creds = _FakeCreds("acc", "r0")
    creds.device_id = None  # legacy/env row with a NULL device id
    c, _ = _make_connection(_jwt_with_did("hermes-host-frozen"), store=_FakeStore(creds))
    assert c._refresh_device_id() == "hermes-host-frozen"


def test_refresh_device_id_falls_back_to_get_device_id_without_did(monkeypatch):
    monkeypatch.setattr(conn_mod, "get_device_id", lambda: "hermes-host-fallback")
    c, _ = _make_connection(_fresh_token())  # no `did` claim, no store
    assert c._refresh_device_id() == "hermes-host-fallback"


def test_session_device_id_matches_refresh_device_id():
    c, _ = _make_connection(_jwt_with_did("hermes-host-frozen"))
    assert c.session_device_id() == c._refresh_device_id() == "hermes-host-frozen"


class _FakeWS:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeManager:
    """Records refresh() calls and returns scripted outcomes."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = []
        self._latch_reset = 0

    async def refresh(self, *, access_token, refresh_token):
        self.calls.append((access_token, refresh_token))
        return self._outcomes.pop(0)

    def reset_latch(self):
        self._latch_reset += 1


def _make_connection(token, refresh_token="r0", store=None):
    cfg = ClawChatConfig(
        websocket_url="wss://example.test/ws",
        base_url="https://example.test",
        token=token,
        refresh_token=refresh_token,
        user_id="usr_1",
        owner_user_id="usr_owner",
    )
    logout_messages: list = []

    async def on_logout(message):
        logout_messages.append(message)

    c = ClawChatConnection(
        cfg,
        on_message=_noop,
        on_auth_logout=on_logout,
    )
    if store is not None:
        c._store = store
    return c, logout_messages


async def _noop(_frame):
    return None


# --- _attempt_refresh success: persist-before-swap + close WS (§0/§D) ---

async def test_attempt_refresh_success_swaps_and_closes_ws():
    c, _ = _make_connection(_fresh_token())
    c._refresh_manager = _FakeManager([RefreshOutcome("success", "new-acc", "new-ref")])
    ws = _FakeWS()
    c._ws = ws
    outcome = await c._attempt_refresh(close_ws_on_success=True)
    assert outcome.status == "success"
    # In-memory swap happened AFTER the manager (which persisted).
    assert c._cfg.token == "new-acc"
    assert c._cfg.refresh_token == "new-ref"
    # §C.2 recovery unification: env path converted onto the SQLite path.
    assert c._using_activation_db_credentials is True
    # §D: live socket closed so we reconnect with the new token.
    assert ws.closed is True
    assert c._refresh_pending_reconnect is True


async def test_attempt_refresh_permanent_clears_creds():
    c, _ = _make_connection(_fresh_token())
    c._refresh_manager = _FakeManager([RefreshOutcome("permanent", error="invalid")])
    outcome = await c._attempt_refresh(close_ws_on_success=False)
    assert outcome.status == "permanent"
    # In-memory creds dropped, dead token latched.
    assert c._cfg.token == ""
    assert c._cfg.refresh_token == ""
    assert c._rejected_activation_token is not None
    assert c._using_activation_db_credentials is False


# --- proactive trigger (§A.1) ---

async def test_proactive_refresh_fires_near_expiry():
    c, _ = _make_connection(_near_expiry_token())
    mgr = _FakeManager([RefreshOutcome("success", "new-acc", "new-ref")])
    c._refresh_manager = mgr
    c._ws = _FakeWS()
    await c._maybe_proactive_refresh()
    assert len(mgr.calls) == 1


async def test_proactive_refresh_skips_when_fresh():
    c, _ = _make_connection(_fresh_token())
    mgr = _FakeManager([])
    c._refresh_manager = mgr
    await c._maybe_proactive_refresh()
    assert mgr.calls == []


# --- hello-fail gating (§A.2) ---

def _hello_fail_frame(reason, trace_id="t1"):
    return {"event": "hello-fail", "trace_id": trace_id, "payload": {"reason": reason}}


async def _setup_handshake(c):
    c._pending_connect_id = "t1"
    c._hello_wait = asyncio.get_running_loop().create_future()
    c._ws = _FakeWS()
    await c._set_state(ConnectionState.HANDSHAKING)


async def test_hello_fail_auth_unavailable_backoff_no_refresh():
    c, _ = _make_connection(_fresh_token())
    mgr = _FakeManager([])
    c._refresh_manager = mgr
    await _setup_handshake(c)
    await c._maybe_finish_handshake(_hello_fail_frame("auth service unavailable"))
    # §14.1 transient: backoff-reconnect with the SAME token, NO refresh.
    assert mgr.calls == []
    assert c._cfg.token != ""  # creds kept
    assert c._hello_wait.result() is False


async def test_hello_fail_token_rejected_triggers_refresh():
    c, _ = _make_connection(_fresh_token())
    mgr = _FakeManager([RefreshOutcome("success", "new-acc", "new-ref")])
    c._refresh_manager = mgr
    await _setup_handshake(c)
    await c._maybe_finish_handshake(_hello_fail_frame("authentication failed"))
    assert len(mgr.calls) == 1  # token-rejected → refresh attempted
    assert c._cfg.token == "new-acc"  # swapped on success


async def test_hello_fail_generic_token_not_near_no_refresh():
    c, _ = _make_connection(_fresh_token())  # not near expiry
    mgr = _FakeManager([])
    c._refresh_manager = mgr
    await _setup_handshake(c)
    await c._maybe_finish_handshake(_hello_fail_frame("connection rejected"))
    # Generic reason + token not near expiry → no refresh (prevents storm).
    assert mgr.calls == []


async def test_hello_fail_generic_token_expired_triggers_refresh():
    c, _ = _make_connection(_near_expiry_token())  # near expiry
    mgr = _FakeManager([RefreshOutcome("success", "new-acc", "new-ref")])
    c._refresh_manager = mgr
    await _setup_handshake(c)
    await c._maybe_finish_handshake(_hello_fail_frame("connection rejected"))
    # Generic reason but token IS near expiry → refresh.
    assert len(mgr.calls) == 1


async def test_hello_fail_permanent_refresh_logs_out():
    c, logout_messages = _make_connection(_near_expiry_token())
    mgr = _FakeManager([RefreshOutcome("permanent", error="invalid")])
    c._refresh_manager = mgr
    await _setup_handshake(c)
    await c._maybe_finish_handshake(_hello_fail_frame("authentication failed"))
    assert len(mgr.calls) == 1
    assert c._cfg.token == ""  # creds cleared
    assert c._state == ConnectionState.AUTH_FAILED
    # _attempt_refresh permanent → manager.persist_logout emitted; here the
    # FakeManager doesn't call on_auth_logout, so just assert state + cleared.
    assert c._rejected_activation_token is not None


# --- auto-logout user message wording (§C.1) ---

# --- startup refresh-if-near-expiry (§A.4, SQLite path) ---


class _FakeCreds:
    def __init__(self, access_token, refresh_token, activated_at=None):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.user_id = "usr_1"
        self.owner_user_id = "usr_owner"
        self.device_id = "hermes-dev-1"
        self.activated_at = activated_at


class _FakeStore:
    def __init__(self, creds):
        self._creds = creds

    def get_activation_credentials(self, *, platform, account_id):
        return self._creds


async def test_startup_refresh_if_near_expiry_swaps_before_connect():
    near = _near_expiry_token()
    store = _FakeStore(_FakeCreds(near, "r0"))
    c, _ = _make_connection("", refresh_token="", store=store)
    mgr = _FakeManager([RefreshOutcome("success", "new-acc", "new-ref")])
    c._refresh_manager = mgr
    loaded = await c._wait_for_activation_credentials()
    assert loaded is True
    # §A.4: a refresh was attempted on the near-expiry stored token, and the
    # connection now holds the fresh token before the first connect.
    assert len(mgr.calls) == 1
    assert c._cfg.token == "new-acc"


async def test_startup_refresh_permanent_keeps_waiting():
    near = _near_expiry_token()
    store = _FakeStore(_FakeCreds(near, "r0"))
    c, _ = _make_connection("", refresh_token="", store=store)
    mgr = _FakeManager([RefreshOutcome("permanent", error="invalid")])
    c._refresh_manager = mgr

    # After the permanent outcome the loop `continue`s; flip _stopping so the
    # second iteration exits instead of spinning forever.
    real_creds = store.get_activation_credentials

    calls = {"n": 0}

    def stopping_after_first(*, platform, account_id):
        calls["n"] += 1
        if calls["n"] >= 2:
            c._stopping = True
        return real_creds(platform=platform, account_id=account_id)

    store.get_activation_credentials = stopping_after_first
    loaded = await c._wait_for_activation_credentials()
    # §A.4: permanent refresh → auto-logout immediately, skip the doomed connect.
    assert loaded is False
    assert len(mgr.calls) == 1


async def test_no_reconnect_with_dead_token_while_refresh_in_flight():
    # §D: a refresh in-flight must dedupe; the supervisor must not open a socket
    # with the dead token. The single-flight manager guarantees one HTTP refresh
    # even under concurrent proactive + reactive triggers.
    c, _ = _make_connection(_near_expiry_token())
    from clawchat_gateway.token_refresh import RefreshManager

    started = asyncio.Event()
    release = asyncio.Event()
    http_calls = {"n": 0}

    class SlowClient:
        async def auth_refresh(self, *, refresh_token, device_id):
            http_calls["n"] += 1
            started.set()
            await release.wait()
            from clawchat_gateway.api_client import RefreshResult

            return RefreshResult(access_token="new-acc", refresh_token="new-ref")

    async def persist_tokens(a, b):
        return True

    async def persist_logout(r):
        return None

    c._refresh_manager = RefreshManager(
        build_client=lambda: SlowClient(),
        persist_tokens=persist_tokens,
        persist_logout=persist_logout,
        device_id_provider=lambda: "hermes-dev-1",
        min_interval_seconds=0.0,
    )
    c._ws = _FakeWS()
    # Two concurrent refresh attempts (proactive + reactive); only ONE HTTP call.
    t1 = asyncio.create_task(c._attempt_refresh(close_ws_on_success=True))
    await started.wait()
    t2 = asyncio.create_task(c._attempt_refresh(close_ws_on_success=True))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(t1, t2)
    assert http_calls["n"] == 1
    assert c._cfg.token == "new-acc"


async def test_persist_auth_logout_emits_user_message(monkeypatch):
    c, logout_messages = _make_connection(_fresh_token())

    # activate.py imports hermes_cli at module load; stub it so the function-level
    # import inside _persist_auth_logout succeeds in this isolated test env.
    import sys
    from types import ModuleType

    hermes_cli = ModuleType("hermes_cli")
    hermes_config = ModuleType("hermes_cli.config")
    hermes_config.get_config_path = lambda: "/tmp/config.yaml"
    hermes_config.get_env_path = lambda: "/tmp/.env"
    hermes_config.read_raw_config = lambda: {}
    hermes_config.remove_env_value = lambda key: None
    hermes_config.save_config = lambda config: None
    hermes_config.save_env_value = lambda key, value: None
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", hermes_config)

    # Patch persistence to a no-op so we exercise only the user-notification path.
    async def fake_to_thread(fn, *a, **k):
        return None

    monkeypatch.setattr(conn_mod.asyncio, "to_thread", fake_to_thread)
    await c._persist_auth_logout("invalid refresh")
    assert logout_messages == [AUTO_LOGOUT_STATUS_MESSAGE]


# --- §C.2 env-only deployment: first refresh must NOT brick (FINDING 1) ---


async def test_env_only_startup_refresh_seeds_db_and_succeeds(tmp_path, monkeypatch):
    """An env-booted process (no activations row) whose stored access token is
    near expiry must end its startup refresh with status=='success' and the new
    tokens in BOTH stores — not bricked by a rowcount==0 db write."""
    from clawchat_gateway import activate, storage
    from clawchat_gateway.api_client import RefreshResult
    from clawchat_gateway.storage import ClawChatStore
    from clawchat_gateway.token_refresh import RefreshManager

    # Real SQLite store with NO activations row (the env-only precondition).
    store = ClawChatStore(tmp_path / "clawchat.sqlite")
    store.initialize()
    monkeypatch.setattr(storage, "_store", store)
    monkeypatch.setattr(storage, "get_clawchat_store", lambda: store)
    monkeypatch.setattr(activate, "get_clawchat_store", lambda: store)

    # Capture .env writes instead of touching a real env file.
    env_writes: dict[str, str | None] = {}

    def fake_write_env(values):
        env_writes.update(values)
        from pathlib import Path

        return Path("/tmp/.env")

    monkeypatch.setattr(activate, "_write_env_values", fake_write_env)

    near = _near_expiry_token()
    c, _ = _make_connection(near, refresh_token="r0", store=store)

    # Real RefreshManager wired to the connection's real persist path so the
    # seed-on-absent fallback is exercised end to end. The HTTP layer is faked.
    class _Client:
        async def auth_refresh(self, *, refresh_token, device_id):
            return RefreshResult(access_token="new-acc", refresh_token="new-ref")

    c._refresh_manager = RefreshManager(
        build_client=lambda: _Client(),
        persist_tokens=c._persist_rotated_tokens,
        persist_logout=c._persist_auth_logout,
        device_id_provider=lambda: "hermes-dev-1",
        min_interval_seconds=0.0,
    )

    outcome = await c._attempt_refresh(close_ws_on_success=False)
    assert outcome.status == "success"  # NOT transient/bricked
    # In-memory swapped onto the SQLite-credentials recovery path (§C.2).
    assert c._cfg.token == "new-acc"
    assert c._using_activation_db_credentials is True
    # New tokens in BOTH stores.
    assert env_writes["CLAWCHAT_TOKEN"] == "new-acc"
    assert env_writes["CLAWCHAT_REFRESH_TOKEN"] == "new-ref"
    creds = store.get_activation_credentials(platform="hermes", account_id="default")
    assert creds is not None
    assert creds.access_token == "new-acc"
    assert creds.refresh_token == "new-ref"
    assert creds.user_id == "usr_1"
    assert creds.owner_user_id == "usr_owner"


# --- §D: refresh-driven close resets backoff (FINDING 4) ---


async def test_refresh_pending_reconnect_resets_backoff(monkeypatch):
    """A refresh-driven WS close must reset the supervisor backoff immediately
    (retries→0, delay→initial) even if the socket did not stay READY long enough
    for the stable-ready reset — so the new token reconnects without backing off.
    """
    c, _ = _make_connection(_fresh_token())

    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(conn_mod.asyncio, "sleep", fake_sleep)

    # First connection: a normal disconnect (no refresh, no stable-ready reset)
    # so backoff grows. Second: a refresh close that must reset it. Then stop.
    calls = {"n": 0}

    async def fake_run_one_connection():
        calls["n"] += 1
        if calls["n"] == 1:
            # Plain drop: did not stay ready long enough → backoff should grow.
            self_ref._stable_ready_reset_done = False
            self_ref._refresh_pending_reconnect = False
        elif calls["n"] == 2:
            # Refresh-driven close: planned swap, backoff must reset.
            self_ref._stable_ready_reset_done = False
            self_ref._refresh_pending_reconnect = True
        else:
            self_ref._stopping = True
        return False

    self_ref = c
    monkeypatch.setattr(c, "_run_one_connection", fake_run_one_connection)
    # Avoid the activation-wait path: pretend we always hold creds.
    monkeypatch.setattr(c, "_has_connect_credentials", lambda: True)

    await c._supervisor()

    # After iter 1 the scheduled reconnect delay grew above the initial; after
    # iter 2 (refresh close) it reset back to the initial delay.
    initial = c._cfg.reconnect_initial_delay_ms / 1000.0
    assert len(sleeps) >= 2
    assert sleeps[0] >= initial  # first backoff (with jitter)
    # The reconnect scheduled after the refresh close uses the reset initial delay
    # (within the jitter band), proving retries/delay were reset.
    assert sleeps[1] <= initial * (1.0 + c._cfg.reconnect_jitter_ratio) + 1e-6
    assert c._refresh_pending_reconnect is False
