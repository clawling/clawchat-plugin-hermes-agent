"""Opt-in durable receipts and connection-local dseq acknowledgements."""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import FAKE_SECRETS

from clawchat_gateway import connection, config as config_module, reliable_delivery as delivery
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import ClawChatConnection, ConnectionState, build_connect_capabilities
from clawchat_gateway.storage import ClawChatStore


BASE_CAPS = {"multi_device": False, "device_replay": True, "chat_meta_events": True,
             "notify_signals": True, "permission_events": True}
GRANT = {"ack_mode": "dseq", "ack_epoch": "test-epoch"}


async def until(predicate, timeout=2):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.002)
    await asyncio.wait_for(wait(), timeout)


class FakeWS:
    """A queue-backed peer driving the real handshake and socket read loop."""
    def __init__(self, hello, hello_form="event"):
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait(json.dumps({"event": "connect.challenge", "payload": {"nonce": "test-nonce"}}))
        self.hello = hello
        self.hello_form = hello_form
        self.sent = []
        self.closed = False
        self.close_code = None
        self.close_reason = None
        self.at_close = []
        self.send_error = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.incoming.get()
        if frame is None:
            raise StopAsyncIteration
        return frame

    async def send(self, raw):
        frame = json.loads(raw)
        if frame["event"] == "message.sync_ack" and self.send_error:
            raise self.send_error
        self.sent.append(frame)
        if frame["event"] == "connect":
            if self.hello_form == "event":
                hello = {"event": "hello-ok", "trace_id": frame["trace_id"], "payload": self.hello}
            else:
                hello = {"type": "res", "requestId": frame["trace_id"],
                         "payload": {"type": "hello-ok", **self.hello}}
            self.feed(hello)

    def feed(self, frame):
        self.incoming.put_nowait(json.dumps(frame))

    async def close(self):
        if not self.closed:
            self.at_close = list(self.acks)
            self.closed = True
            self.incoming.put_nowait(None)

    @property
    def acks(self):
        return [f for f in self.sent if f["event"] == "message.sync_ack"]


def message(dseq=1, message_id="test-message", text="hello", event="message.send"):
    return {"version": "2", "event": event, "dseq": dseq, "trace_id": "test-trace",
            "chat_id": "cnv_test", "chat_type": "direct",
            "sender": {"id": "usr_test_human", "nick_name": "Example"},
            "payload": {"message_id": message_id,
                        "message": {"fragments": [{"kind": "text", "text": text}]}}}


@pytest.fixture
def store(monkeypatch, tmp_path):
    store = ClawChatStore(tmp_path / "receipts.sqlite")
    store.initialize()
    monkeypatch.setattr(connection, "get_clawchat_store", lambda: store)
    monkeypatch.setattr(connection, "hermes_home", lambda: tmp_path)
    monkeypatch.setattr(connection, "hermes_root", lambda: tmp_path)
    monkeypatch.setattr(connection, "get_device_id", lambda: "test-device")
    return store


def rows(store):
    with sqlite3.connect(store.db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute("SELECT * FROM inbound_receipts ORDER BY received_at, rowid")]


def has_receipts_table(store):
    with sqlite3.connect(store.db_path) as db:
        return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name = 'inbound_receipts'").fetchone())


def seed_pending(store, frame, *, epoch="old-epoch"):
    receipt = ClawChatConnection._receipt_identity(frame, epoch, frame.get("dseq", 1))
    assert store.upsert_inbound_receipt(
        account_id="default", event=frame["event"], raw=frame,
        write_order=delivery.next_write_order(), **receipt,
    )


@asynccontextmanager
async def peer(monkeypatch, store, *, enabled=True, hello=None, hello_form="event", on_message=None,
               on_signal=None, wait_for_recovery=True, cleanup_workers=True):
    ws = FakeWS(GRANT if hello is None else hello, hello_form)
    monkeypatch.setattr(connection, "_ws_connect", AsyncMock(return_value=ws))
    cfg = ClawChatConfig(websocket_url="wss://example.invalid/ws", token="t",
                        refresh_token="r", user_id="usr_test_agent",
                        owner_user_id="usr_test_owner", reliable_delivery_v2=enabled)
    conn = ClawChatConnection(cfg, on_message=on_message or AsyncMock(), on_notify_signal=on_signal)
    async def watch():
        await asyncio.Event().wait()
    monkeypatch.setattr(conn, "_watch_activation_credentials", watch)
    conn._supervisor_task = asyncio.create_task(conn._run_one_connection())
    try:
        await until(lambda: conn._supervisor_task.done() or (
            conn._credential_watch_task is not None if wait_for_recovery else
            conn._hello_wait is not None and conn._hello_wait.done()))
        assert not conn._supervisor_task.done(), conn._supervisor_task.exception()
        assert conn._state == ConnectionState.READY
        yield SimpleNamespace(conn=conn, ws=ws)
    finally:
        await asyncio.wait_for(conn.stop(), 3)
        # Finish storage workers before pytest tears down this function's loop.
        if cleanup_workers:
            shutdown = asyncio.create_task(asyncio.get_running_loop().shutdown_default_executor())
            await until(shutdown.done)
            await shutdown


@pytest.mark.parametrize("conflict", [False, True])
def test_disabled_capabilities_exact_baseline(conflict):
    expected = {**({"install_conflict": True} if conflict else {}), **BASE_CAPS}
    assert build_connect_capabilities(install_conflict=conflict) == expected
    assert build_connect_capabilities(install_conflict=conflict, reliable_delivery_v2=False) == expected
    enabled = build_connect_capabilities(install_conflict=conflict, reliable_delivery_v2=True)
    assert enabled == {**expected, "reliable_delivery_v2": True}
    assert "reliable_delivery" not in enabled


async def test_disabled_dseq_ignored_no_table_or_ack(monkeypatch, store):
    handler = AsyncMock()
    forbidden = lambda **kwargs: pytest.fail("disabled profile accessed receipts")
    for name in ("upsert_inbound_receipt", "finish_inbound_receipt", "recover_inbound_receipts"):
        monkeypatch.setattr(store, name, forbidden)
    async with peer(monkeypatch, store, enabled=False, on_message=handler) as h:
        h.ws.feed(message(99))
        await until(lambda: handler.await_count == 1)
        await asyncio.sleep(0.23)
        assert h.conn._delivery_tracker is None
        assert not h.ws.acks and not has_receipts_table(store)
        assert h.conn._receipt_recovery_ready is None and h.conn._receipt_retry_task is None
        assert not h.conn.dseq_density_violations
        assert "reliable_delivery_v2" not in h.ws.sent[0]["payload"]["capabilities"]


@pytest.mark.parametrize("hello", [{}, {"ack_mode": "seq", "ack_epoch": "test-epoch"},
                                   {"ack_mode": "dseq", "ack_epoch": ""},
                                   {"ack_mode": "dseq", "ack_epoch": None},
                                   {"ack_mode": "dseq", "ack_epoch": 7}])
@pytest.mark.parametrize("hello_form", ["event", "res"])
async def test_missing_grant_stays_legacy(monkeypatch, store, hello, hello_form, caplog):
    handler = AsyncMock()
    with caplog.at_level(logging.INFO):
        async with peer(monkeypatch, store, hello=hello, hello_form=hello_form, on_message=handler) as h:
            h.ws.feed(message(9))
            await until(lambda: handler.await_count == 1)
            assert h.conn._delivery_tracker is None
            assert "using legacy delivery" in caplog.text
            assert not h.ws.acks


@pytest.mark.parametrize("hello_form", ["event", "res"])
async def test_granted_debounce_durable_before_processing(monkeypatch, store, hello_form):
    release = asyncio.Event()
    seen = []
    async def handler(frame):
        seen.append(frame)
        await release.wait()
    async with peer(monkeypatch, store, on_message=handler, hello_form=hello_form) as h:
        h.ws.feed(message(1))
        h.ws.feed(message(2, "test-second"))
        await until(lambda: h.conn._delivery_tracker.acked == 2 and bool(seen))
        assert [r["status"] for r in rows(store)] == ["pending", "pending"]
        assert not h.ws.acks
        await until(lambda: len(h.ws.acks) == 1)
        assert h.ws.acks == [{"version": "2", "event": "message.sync_ack", "trace_id": "sync-ack",
                              "payload": {"dseq": 2, "epoch": "test-epoch"}}]
        assert h.ws.sent[0]["payload"]["capabilities"]["reliable_delivery_v2"] is True
        assert not h.conn._pending_acks
        release.set()
        await until(lambda: not h.conn._inbound_lanes)
        assert [r["status"] for r in rows(store)] == ["done", "done"]
        assert all("_clawchat_receipt" not in f for f in seen)


async def test_controls_unknown_transit_and_truncation(monkeypatch, store):
    monkeypatch.setattr(delivery, "ACK_DEBOUNCE_SECONDS", 10)
    async with peer(monkeypatch, store) as h:
        h.ws.feed({"event": "history.truncated", "dseq": 999, "payload": {}})
        h.ws.feed({"event": "sync.mark", "dseq": 1, "payload": {}})
        h.ws.feed({"event": "future.event", "dseq": 2, "payload": {}})
        h.ws.feed({"event": "history.transit", "dseq": 3, "payload": {"new": "content"}})
        h.ws.feed({"event": "replay.done", "dseq": 4, "payload": {"ack_epoch": "test-epoch"}})
        await until(lambda: bool(h.ws.acks))
        assert h.ws.acks[0]["payload"]["dseq"] == 4
        assert [r["event"] for r in rows(store)] == ["future.event", "history.transit"]
        assert all(r["status"] == "done" and r["error"] is None for r in rows(store))
        assert not h.conn.dseq_density_violations


async def test_wrong_replay_epoch_recorded_without_flush_or_dispatch(monkeypatch, store, caplog):
    monkeypatch.setattr(delivery, "ACK_DEBOUNCE_SECONDS", 10)
    async with peer(monkeypatch, store) as h:
        dispatch = AsyncMock(wraps=h.conn._dispatch_inbound)
        monkeypatch.setattr(h.conn, "_dispatch_inbound", dispatch)
        tracker = h.conn._delivery_tracker
        flush = AsyncMock(wraps=tracker.flush_now)
        monkeypatch.setattr(tracker, "flush_now", flush)
        h.ws.feed({"event": "replay.done", "dseq": 1, "payload": {"ack_epoch": "old-epoch"}})
        h.ws.feed({"event": "sync.mark", "dseq": 2, "payload": {}})
        await until(lambda: tracker.acked == 2)
        assert tracker.last_read_dseq == 2
        assert not h.ws.acks and flush.await_count == 0
        assert any(r.levelno == logging.WARNING and "ack_epoch mismatch" in r.message for r in caplog.records)
        assert [c.args[0]["event"] for c in dispatch.call_args_list] == ["sync.mark"]
        assert rows(store) == []
        await tracker.flush_now()
        assert h.ws.acks[0]["payload"] == {"dseq": 2, "epoch": "test-epoch"}


async def test_sustained_frames_do_not_postpone_first_debounce(monkeypatch, store):
    async with peer(monkeypatch, store) as h:
        loop = asyncio.get_running_loop()
        sent_at = []
        original = h.ws.send
        async def send(raw):
            if json.loads(raw)["event"] == "message.sync_ack":
                sent_at.append(loop.time())
            await original(raw)
        monkeypatch.setattr(h.ws, "send", send)
        started = loop.time()
        for dseq in range(1, 11):
            h.ws.feed({"event": "sync.mark", "dseq": dseq})
            await asyncio.sleep(0.1)
        assert sent_at, "continuous progress must send an ack during the one-second stream"
        assert 0.18 <= sent_at[0] - started <= 0.4
        assert h.ws.acks[0]["payload"]["dseq"] >= 2
        await until(lambda: h.ws.acks[-1]["payload"]["dseq"] == 10)


async def test_periodic_unconditional_resend(monkeypatch, store):
    monkeypatch.setattr(delivery, "ACK_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr(delivery, "ACK_RESEND_SECONDS", 0.06)
    async with peer(monkeypatch, store) as h:
        h.ws.feed({"event": "sync.mark", "dseq": 1})
        await until(lambda: len(h.ws.acks) >= 3)
        assert all(f["payload"] == {"dseq": 1, "epoch": "test-epoch"} for f in h.ws.acks)


async def test_stop_flushes_before_close_and_disposes(monkeypatch, store):
    monkeypatch.setattr(delivery, "ACK_DEBOUNCE_SECONDS", 10)
    async with peer(monkeypatch, store) as h:
        h.ws.feed({"event": "sync.mark", "dseq": 1})
        await until(lambda: h.conn._delivery_tracker.acked == 1)
        tracker = h.conn._delivery_tracker
        await h.conn.stop()
        assert h.ws.at_close[0]["payload"]["dseq"] == 1
        assert tracker.disposed and h.conn._delivery_tracker is None


async def test_density_violation_reconnect_resets_baseline_and_epoch(monkeypatch, store):
    async with peer(monkeypatch, store) as h:
        old_ws = h.ws
        old_tracker = h.conn._delivery_tracker
        h.ws.feed({"event": "sync.mark", "dseq": 1})
        h.ws.feed(message(3))
        await until(lambda: h.ws.closed)
        await until(lambda: h.conn._supervisor_task.done())
        assert h.conn.dseq_density_violations == 1 and old_tracker.disposed
        new_ws = FakeWS({"ack_mode": "dseq", "ack_epoch": "new-epoch"})
        monkeypatch.setattr(connection, "_ws_connect", AsyncMock(return_value=new_ws))
        h.conn._supervisor_task = asyncio.create_task(h.conn._run_one_connection())
        await until(lambda: h.conn._credential_watch_task is not None)
        new_ws.feed({"event": "sync.mark", "dseq": 1})
        await until(lambda: bool(new_ws.acks))
        assert new_ws.acks[0]["payload"] == {"dseq": 1, "epoch": "new-epoch"}
        assert h.conn.dseq_density_violations == 1
        old_tracker.record(2)
        await old_tracker.flush_now()
        assert not old_ws.acks


@pytest.mark.parametrize("raises", [False, True])
@pytest.mark.parametrize("failed_dseq", [1, 2])
async def test_receipt_failure_closes_and_reconnects_with_new_epoch(monkeypatch, store, raises, failed_dseq, caplog):
    monkeypatch.setattr(delivery, "ACK_DEBOUNCE_SECONDS", 10)
    original = store.upsert_inbound_receipt
    def upsert(**kwargs):
        if kwargs["dseq"] == failed_dseq:
            if raises:
                raise OSError("placeholder-access-secret")
            return False
        return original(**kwargs)
    monkeypatch.setattr(store, "upsert_inbound_receipt", upsert)
    handler = AsyncMock()
    async with peer(monkeypatch, store, on_message=handler) as h:
        tracker = h.conn._delivery_tracker
        if failed_dseq == 2:
            h.ws.feed(message(1, "test-earlier"))
            await until(lambda: tracker.acked == 1 and not h.conn._inbound_lanes)
            await tracker.flush_now()
        earlier_acks = list(h.ws.acks)
        h.ws.feed(message(failed_dseq))
        h.ws.feed(message(failed_dseq + 1, "test-later"))
        await until(lambda: h.conn._supervisor_task.done())
        assert h.ws.closed and tracker.disposed
        assert h.conn.receipt_write_failures == 1 and h.conn.dseq_density_violations == 0
        assert h.ws.acks == earlier_acks
        assert all(f["payload"]["dseq"] < failed_dseq for f in h.ws.acks)
        assert handler.await_count == failed_dseq - 1
        assert any(r.levelno == logging.ERROR and "durable receipt unavailable" in r.message for r in caplog.records)
        assert "placeholder-access-secret" not in caplog.text

        monkeypatch.setattr(store, "upsert_inbound_receipt", original)
        ws = FakeWS({"ack_mode": "dseq", "ack_epoch": "retry-epoch"})
        monkeypatch.setattr(connection, "_ws_connect", AsyncMock(return_value=ws))
        h.conn._supervisor_task = asyncio.create_task(h.conn._run_one_connection())
        await until(lambda: h.conn._credential_watch_task is not None)
        ws.feed(message(1))
        ws.feed({"event": "replay.done", "dseq": 2, "payload": {"ack_epoch": "retry-epoch"}})
        await until(lambda: bool(ws.acks) and not h.conn._inbound_lanes)
        assert ws.acks[0]["payload"] == {"dseq": 2, "epoch": "retry-epoch"}
        assert handler.await_count == failed_dseq
        assert h.conn.receipt_write_failures == 1 and h.conn.dseq_density_violations == 0


@pytest.mark.parametrize("event", ["message.send", "notify.signal"])
async def test_poison_acked_and_failed_error_redacted(monkeypatch, store, event, caplog):
    error = "placeholder-access-secret placeholder-refresh-secret " + "bad " * 200
    handler = AsyncMock(side_effect=ValueError(error))
    async with peer(monkeypatch, store, on_message=handler, on_signal=handler) as h:
        frame = message()
        if event == "notify.signal":
            frame = {"event": event, "dseq": 1, "payload": {"event_id": "test-signal", "type": "test.changed"}}
        h.ws.feed(frame)
        await until(lambda: bool(rows(store)) and rows(store)[0]["status"] == "failed")
        await until(lambda: bool(h.ws.acks))
        row = rows(store)[0]
        assert row["error"] and len(row["error"]) <= 500
        assert h.ws.acks[0]["payload"]["dseq"] == 1
        for secret in ("placeholder-access-secret", "placeholder-refresh-secret"):
            assert secret not in row["error"] and secret not in caplog.text


@pytest.mark.parametrize("event", ["message.send", "notify.signal"])
async def test_receipt_errors_redact_display_credentials_and_unconfigured_long_secret(monkeypatch, store, event, caplog):
    opaque = secrets.token_hex(20)
    error = ("Authorization: Bearer abc123short https://h/x?access_token=shortq&sig=zz "
             "password=hunter2 " + opaque)
    handler = AsyncMock(side_effect=ValueError(error))
    async with peer(monkeypatch, store, on_message=handler, on_signal=handler) as h:
        h.conn._cfg = replace(h.conn.config, token=FAKE_SECRETS["access"],
                              refresh_token=FAKE_SECRETS["refresh"])
        frame = message()
        if event == "notify.signal":
            frame = {"event": event, "dseq": 1,
                     "payload": {"event_id": "test-signal", "type": "test.changed"}}
        h.ws.feed(frame)
        await until(lambda: bool(rows(store)) and rows(store)[0]["status"] == "failed")
        await until(lambda: bool(h.ws.acks))
        for secret in ("abc123short", "shortq", "hunter2", opaque):
            assert secret not in rows(store)[0]["error"]
            assert secret not in caplog.text
        assert "raised" in caplog.text and "***" in rows(store)[0]["error"]


@pytest.mark.parametrize("field", ["base_url", "websocket_url"])
def test_receipt_error_with_userinfo_remains_withheld(monkeypatch, store, field):
    cfg = ClawChatConfig(websocket_url="wss://example.invalid/ws")
    cfg = replace(cfg, **{field: FAKE_SECRETS["userinfo_url"]})
    conn = ClawChatConnection(cfg, on_message=AsyncMock())
    assert conn._receipt_error_text(ValueError("placeholder-error-secret")) == connection.DIAGNOSTIC_WITHHELD


def test_receipt_error_redaction_failure_remains_closed(monkeypatch, store):
    conn = ClawChatConnection(ClawChatConfig(websocket_url="wss://example.invalid/ws"), on_message=AsyncMock())
    def fail(*args):
        raise ValueError("placeholder-redaction-secret")
    monkeypatch.setattr(connection, "redact_display_text", fail)
    assert conn._receipt_error_text(ValueError("placeholder-error-secret")) == connection.DIAGNOSTIC_REDACTION_FAILED


@pytest.mark.parametrize("final_first", [False, True])
async def test_provisional_merged_reply_never_replaces_final_and_both_are_acked(monkeypatch, store, final_first):
    final = message(1 if final_first else 2, "test-merged-reply", text="final", event="message.reply")
    provisional = message(2 if final_first else 1, "test-merged-reply", text="provisional", event="message.reply")
    provisional["payload"]["stream_merged"] = True
    async with peer(monkeypatch, store) as h:
        first, second = (final, provisional) if final_first else (provisional, final)
        h.ws.feed(first)
        await until(lambda: h.conn._delivery_tracker.acked == 1 and not h.conn._inbound_lanes)
        assert rows(store)[0]["provisional"] == (0 if final_first else 1)
        h.ws.feed(second)
        await until(lambda: bool(h.ws.acks) and h.ws.acks[-1]["payload"]["dseq"] == 2
                    and not h.conn._inbound_lanes)
        row = rows(store)[0]
        assert json.loads(row["raw_json"]) == final
        assert row["provisional"] == 0 and row["dseq"] == 2
        assert row["status"] == "done"
        assert h.ws.acks[-1]["payload"]["epoch"] == "test-epoch"


async def test_replayed_message_upserts_copy_but_adapter_claims_once(monkeypatch, store):
    from clawchat_gateway.adapter import ClawChatAdapter
    adapter = ClawChatAdapter({})
    adapter._store = store
    dispatched = AsyncMock()
    monkeypatch.setattr(adapter, "_handle_inbound", dispatched)
    monkeypatch.setattr(adapter, "_handle_owner_forwarded_approval", AsyncMock(return_value=False))
    monkeypatch.setattr(adapter, "_schedule_profile_sync", lambda coro: coro.close())
    monkeypatch.setattr(adapter, "_resolve_inbound_sender_context", lambda inbound: inbound)
    async with peer(monkeypatch, store, on_message=adapter._on_message) as h:
        h.ws.feed(message(1, text="initial"))
        await until(lambda: dispatched.await_count == 1 and not h.conn._inbound_lanes)
        h.ws.feed(message(2, text="coalesced"))
        await until(lambda: h.conn._delivery_tracker.acked == 2 and not h.conn._inbound_lanes)
        row = rows(store)[0]
        assert dispatched.await_count == 1 and len(rows(store)) == 1
        assert row["dseq"] == 2 and row["status"] == "done"
        assert "coalesced" in row["raw_json"]


@pytest.mark.parametrize("hello", [GRANT, {}])
async def test_ready_recovers_all_pending_kinds_and_ages_without_ack(monkeypatch, store, hello):
    now = int(time.time() * 1000)
    def insert(frame, kind, key):
        assert store.upsert_inbound_receipt(account_id="default", kind=kind, key=key,
                                            dseq=1, epoch="old-epoch", event=frame["event"], raw=frame,
                                            write_order=delivery.next_write_order())
    insert(message(), "message", "test-message")
    signal = {"event": "notify.signal", "dseq": 2, "payload": {"event_id": "test-signal", "type": "test.changed"}}
    insert(signal, "signal", "test-signal")
    insert(message(message_id="test-new"), "message", "test-new")
    insert(message(message_id="test-done"), "message", "test-done")
    insert(message(message_id="test-failed"), "message", "test-failed")
    insert(message(event="message.recall"), "other", "old-epoch:recall")
    insert({"event": "future.event", "dseq": 3}, "other", "old-epoch:unknown")
    insert({"event": "history.transit", "dseq": 4}, "other", "old-epoch:transit")
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE inbound_receipts SET received_at = ? WHERE key != 'test-new'",
                   (now - 10000,))
        db.execute("UPDATE inbound_receipts SET received_at = ? WHERE key = 'test-new'", (now + 10000,))
        db.execute("UPDATE inbound_receipts SET status = 'done', updated_at = ? WHERE key = 'test-done'",
                   (now - 9 * 86400000,))
        db.execute("UPDATE inbound_receipts SET status = 'failed', updated_at = ? WHERE key = 'test-failed'",
                   (now - 9 * 86400000,))
    handler, signal_handler = AsyncMock(), AsyncMock()
    async with peer(monkeypatch, store, on_message=handler, on_signal=signal_handler, hello=hello) as h:
        await until(lambda: not h.conn._inbound_lanes)
        assert handler.await_count == 3 and signal_handler.await_count == 1
        assert not h.ws.acks
        if h.conn._delivery_tracker is not None:
            assert h.conn._delivery_tracker.acked == 0
        assert len(rows(store)) == 6
        assert all(row["status"] == "done" for row in rows(store))
        assert await h.conn._recover_inbound_receipts(h.ws, h.conn._delivery_generation) is True
        assert handler.await_count == 3 and signal_handler.await_count == 1


@pytest.mark.parametrize("poison", [False, True])
async def test_other_recall_receipt_finishes_only_after_lane_processing(monkeypatch, store, poison):
    release = asyncio.Event()
    async def handler(frame):
        if frame["event"] == "message.send":
            await release.wait()
        elif poison:
            raise ValueError("test recall failure")
    async with peer(monkeypatch, store, on_message=handler) as h:
        h.ws.feed(message(1))
        h.ws.feed(message(2, event="message.recall"))
        await until(lambda: h.conn._delivery_tracker.acked == 2)
        recall = next(r for r in rows(store) if r["event"] == "message.recall")
        assert recall["kind"] == "other" and recall["status"] == "pending"
        assert ("other", recall["key"]) in h.conn._receipts_in_flight
        release.set()
        await until(lambda: not h.conn._inbound_lanes)
        recall = next(r for r in rows(store) if r["event"] == "message.recall")
        assert recall["status"] == ("failed" if poison else "done")
        assert bool(recall["error"]) is poison
        assert not h.conn._receipts_in_flight


async def test_new_instance_recovers_pending_left_by_stopped_instance(monkeypatch, store):
    entered = asyncio.Event()
    async def blocked(frame):
        entered.set()
        await asyncio.Event().wait()
    async with peer(monkeypatch, store, on_message=blocked, cleanup_workers=False) as old:
        old.ws.feed(message())
        await until(entered.is_set)
        await old.conn.stop()
        assert rows(store)[0]["status"] == "pending"
        assert not old.conn._receipts_in_flight
    handler = AsyncMock()
    async with peer(monkeypatch, store, on_message=handler) as new:
        await until(lambda: not new.conn._inbound_lanes)
        assert handler.await_count == 1 and rows(store)[0]["status"] == "done"
        assert new.conn._delivery_tracker.acked == 0 and not new.ws.acks


async def test_reconnect_recovers_pending_but_skips_work_still_in_flight(monkeypatch, store):
    release = asyncio.Event()
    handler = AsyncMock(side_effect=lambda frame: None)
    async def blocked(frame):
        await handler(frame)
        await release.wait()
    async with peer(monkeypatch, store, on_message=blocked) as h:
        h.ws.feed(message())
        await until(lambda: handler.await_count == 1)
        old_gate = h.conn._receipt_recovery_ready
        await h.ws.close()
        await until(lambda: h.conn._supervisor_task.done())
        seed_pending(store, message(2, "test-between-connections"))
        ws = FakeWS({})  # Recovery also runs when the new connection has no v2 grant.
        monkeypatch.setattr(connection, "_ws_connect", AsyncMock(return_value=ws))
        h.conn._supervisor_task = asyncio.create_task(h.conn._run_one_connection())
        await until(lambda: h.conn._credential_watch_task is not None)
        assert h.conn._receipt_recovery_ready is not old_gate
        assert h.conn._receipt_recovery_ready.is_set()
        assert handler.await_count == 1
        assert len(h.conn._inbound_lanes["cnv_test"].frames) == 1
        assert ("message", "test-message") in h.conn._receipts_in_flight
        release.set()
        await until(lambda: not h.conn._inbound_lanes)
        assert [c.args[0]["payload"]["message_id"] for c in handler.call_args_list] == [
            "test-message", "test-between-connections",
        ]
        assert all(row["status"] == "done" for row in rows(store))
        assert not h.conn._receipts_in_flight


async def test_retryable_claim_stays_pending_and_retries_without_another_delivery(monkeypatch, store):
    from clawchat_gateway.adapter import ClawChatAdapter
    monkeypatch.setattr(delivery, "RECEIPT_RETRY_SECONDS", 0.05)
    adapter = ClawChatAdapter({})
    adapter._store = store
    dispatched = AsyncMock()
    monkeypatch.setattr(adapter, "_handle_inbound", dispatched)
    monkeypatch.setattr(adapter, "_handle_owner_forwarded_approval", AsyncMock(return_value=False))
    monkeypatch.setattr(adapter, "_schedule_profile_sync", lambda coro: coro.close())
    monkeypatch.setattr(adapter, "_resolve_inbound_sender_context", lambda inbound: inbound)
    available = False
    original = store.claim_message_once
    def claim(**kwargs):
        return original(**kwargs) if available else None
    monkeypatch.setattr(store, "claim_message_once", claim)
    async with peer(monkeypatch, store, on_message=adapter._on_message) as h:
        h.ws.feed(message())
        await until(lambda: bool(h.ws.acks) and not h.conn._inbound_lanes)
        assert dispatched.await_count == 0
        assert rows(store)[0]["status"] == "pending"
        assert not h.conn._receipts_in_flight
        assert h.ws.acks[0]["payload"]["dseq"] == 1
        timer = h.conn._receipt_retry_task
        assert timer is not None
        h.conn._schedule_receipt_retry()
        assert h.conn._receipt_retry_task is timer
        available = True
        await until(lambda: dispatched.await_count == 1 and not h.conn._inbound_lanes)
        await until(lambda: h.conn._receipt_retry_task is None)
        assert rows(store)[0]["status"] == "done"
        await asyncio.sleep(0.12)
        assert dispatched.await_count == 1 and not h.conn._receipts_in_flight


async def test_stop_cancels_retry_timer_and_disabled_lane_ignores_sentinel(monkeypatch, store):
    monkeypatch.setattr(delivery, "RECEIPT_RETRY_SECONDS", 10)
    retryable = AsyncMock(return_value=delivery.INBOUND_RETRYABLE)
    async with peer(monkeypatch, store, on_message=retryable, cleanup_workers=False) as h:
        h.ws.feed(message())
        await until(lambda: h.conn._receipt_retry_task is not None)
        timer = h.conn._receipt_retry_task
        await h.conn.stop()
        await until(timer.done)
        assert timer.cancelled() and h.conn._receipt_retry_task is None
        assert rows(store)[0]["status"] == "pending"
    async with peer(monkeypatch, store, enabled=False, on_message=retryable) as disabled:
        disabled.ws.feed(message(99))
        await until(lambda: retryable.await_count == 2)
        assert disabled.conn._receipt_retry_task is None
        assert disabled.conn._receipt_recovery_ready is None


@pytest.mark.parametrize("hello", [GRANT, {}])
async def test_recovery_queues_old_message_before_new_business_frame(monkeypatch, store, hello):
    seed_pending(store, message(message_id="test-old", text="old"))
    entered, release = threading.Event(), threading.Event()
    original = store.recover_inbound_receipts
    def recover(**kwargs):
        entered.set()
        assert release.wait(3)
        return original(**kwargs)
    monkeypatch.setattr(store, "recover_inbound_receipts", recover)
    handler = AsyncMock()
    async def on_message(frame):
        # Recovery may also see the new frame persisted at the gate. Model the
        # adapter's durable claim so this assertion counts turns, not raw dispatches.
        claimed = await asyncio.to_thread(
            store.claim_message_once, platform="hermes", account_id="default",
            kind="message", direction="inbound", event_type=frame["event"],
            message_id=frame["payload"]["message_id"], chat_id=frame["chat_id"], raw=frame,
        )
        if claimed is True:
            await handler(frame)
    try:
        async with peer(monkeypatch, store, hello=hello, on_message=on_message, wait_for_recovery=False) as h:
            await until(entered.is_set)
            h.ws.feed(message(1, "test-new", text="new"))
            if h.conn._delivery_tracker is not None:
                await until(lambda: h.conn._delivery_tracker.acked == 1)
                await until(lambda: bool(h.ws.acks))
            else:
                await asyncio.sleep(0.03)
            assert handler.await_count == 0 and not h.conn._receipt_recovery_ready.is_set()
            release.set()
            await until(lambda: handler.await_count == 2 and not h.conn._inbound_lanes)
            assert [c.args[0]["payload"]["message_id"] for c in handler.call_args_list] == [
                "test-old", "test-new",
            ]
            assert h.conn._receipt_recovery_ready.is_set()
    finally:
        release.set()


async def test_replayed_pending_message_keeps_recovery_order_and_claims_once(monkeypatch, store):
    from clawchat_gateway.adapter import ClawChatAdapter
    seed_pending(store, message(1, "test-A", text="old A"))
    seed_pending(store, message(2, "test-B", text="B"))
    adapter = ClawChatAdapter({})
    adapter._store = store
    dispatched = AsyncMock()
    monkeypatch.setattr(adapter, "_handle_inbound", dispatched)
    monkeypatch.setattr(adapter, "_handle_owner_forwarded_approval", AsyncMock(return_value=False))
    monkeypatch.setattr(adapter, "_schedule_profile_sync", lambda coro: coro.close())
    monkeypatch.setattr(adapter, "_resolve_inbound_sender_context", lambda inbound: inbound)
    entered, release = threading.Event(), threading.Event()
    original_recover = store.recover_inbound_receipts
    def recover(**kwargs):
        entered.set()
        assert release.wait(3)
        return original_recover(**kwargs)
    monkeypatch.setattr(store, "recover_inbound_receipts", recover)
    finishes = []
    original_finish = store.finish_inbound_receipt
    def finish(**kwargs):
        result = original_finish(**kwargs)
        finishes.append((kwargs["key"], kwargs["dseq"], kwargs["epoch"], kwargs["error"]))
        return result
    monkeypatch.setattr(store, "finish_inbound_receipt", finish)
    async with peer(monkeypatch, store, on_message=adapter._on_message, wait_for_recovery=False) as h:
        try:
            await until(entered.is_set)
            h.ws.feed(message(1, "test-A", text="replayed A"))
            await until(lambda: h.conn._delivery_tracker.acked == 1)
            await until(lambda: bool(h.ws.acks))
            assert not h.conn._receipt_recovery_ready.is_set()
            assert ("message", "test-A") not in h.conn._receipts_in_flight
            assert len(h.conn._inbound_receipts) == 1
            release.set()
            await until(lambda: dispatched.await_count == 2 and not h.conn._inbound_lanes)
            assert [c.args[0].raw_message["payload"]["message_id"] for c in dispatched.call_args_list] == ["test-A", "test-B"]
            assert [r["status"] for r in rows(store)] == ["done", "done"]
            assert [f for f in finishes if f[0] == "test-A"] == [
                ("test-A", 1, "test-epoch", None), ("test-A", 1, "test-epoch", None),
            ]
            assert not h.conn._receipts_in_flight and not h.conn._inbound_receipts
        finally:
            release.set()


@pytest.mark.parametrize("failure", ["return", "raise", "timeout"])
async def test_retryable_receipt_retries_again_after_recovery_failure(monkeypatch, store, failure, caplog):
    monkeypatch.setattr(delivery, "RECEIPT_RETRY_SECONDS", 0.03)
    monkeypatch.setattr(delivery, "RECEIPT_RECOVERY_TIMEOUT_SECONDS", 0.02)
    failed_read, release = threading.Event(), threading.Event()
    original = store.recover_inbound_receipts
    reads = []
    def recover(**kwargs):
        reads.append(True)
        if len(reads) == 2:
            failed_read.set()
            if failure == "raise":
                raise OSError("test transient recovery failure")
            if failure == "timeout":
                assert release.wait(3)
            return None
        return original(**kwargs)
    monkeypatch.setattr(store, "recover_inbound_receipts", recover)
    handler = AsyncMock(side_effect=[delivery.INBOUND_RETRYABLE, None])
    async with peer(monkeypatch, store, on_message=handler) as h:
        try:
            h.ws.feed(message())
            await until(failed_read.is_set)
            assert handler.await_count == 1 and rows(store)[0]["status"] == "pending"
            assert h.conn._receipt_retry_task is not None
            await until(lambda: handler.await_count == 2 and not h.conn._inbound_lanes)
            await until(lambda: h.conn._receipt_retry_task is None)
            assert rows(store)[0]["status"] == "done" and len(reads) >= 3
            if failure == "timeout":
                assert "recovery timed out" in caplog.text
            await asyncio.sleep(0.07)
            assert handler.await_count == 2
        finally:
            release.set()


@pytest.mark.parametrize("failure", ["return", "raise", "timeout"])
async def test_ready_recovery_failure_schedules_retry(monkeypatch, store, failure):
    seed_pending(store, message(message_id="test-pending"))
    monkeypatch.setattr(delivery, "RECEIPT_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(delivery, "RECEIPT_RECOVERY_TIMEOUT_SECONDS", 0.02)
    release = threading.Event()
    original = store.recover_inbound_receipts
    reads = []
    def recover(**kwargs):
        reads.append(True)
        if len(reads) == 1:
            if failure == "raise":
                raise OSError("test initial recovery failure")
            if failure == "timeout":
                assert release.wait(3)
            return None
        return original(**kwargs)
    monkeypatch.setattr(store, "recover_inbound_receipts", recover)
    handler = AsyncMock()
    async with peer(monkeypatch, store, on_message=handler) as h:
        try:
            assert h.conn._receipt_retry_task is not None
            await until(lambda: handler.await_count == 1 and not h.conn._inbound_lanes)
            await until(lambda: h.conn._receipt_retry_task is None)
            assert rows(store)[0]["status"] == "done"
            assert not h.ws.acks and h.conn._delivery_tracker.acked == 0
            # A stale recovery must report success so an old retry loop can exit.
            assert await h.conn._recover_inbound_receipts(h.ws, h.conn._delivery_generation - 1) is True
        finally:
            release.set()


async def test_recovered_inline_signal_can_wait_for_control_ack_without_deadlock(monkeypatch, store):
    seed_pending(store, {"event": "notify.signal", "dseq": 1,
                         "payload": {"event_id": "test-signal", "type": "test.changed"}})
    seed_pending(store, message(message_id="test-old"))
    entered, ack_seen = asyncio.Event(), asyncio.Event()
    async def signal(frame):
        entered.set()
        await ack_seen.wait()
    async with peer(monkeypatch, store, on_signal=signal, wait_for_recovery=False) as h:
        await until(entered.is_set)
        assert h.conn._receipt_recovery_ready.is_set()
        monkeypatch.setattr(h.conn, "_handle_ack", lambda frame: ack_seen.set())
        h.ws.feed({"event": "message.ack", "payload": {}})
        await until(lambda: h.conn._credential_watch_task is not None and not h.conn._inbound_lanes)
        assert all(row["status"] == "done" for row in rows(store))


async def test_transport_controls_bypass_blocked_recovery_gate(monkeypatch, store):
    entered, release = threading.Event(), threading.Event()
    original = store.recover_inbound_receipts
    def recover(**kwargs):
        entered.set()
        assert release.wait(3)
        return original(**kwargs)
    monkeypatch.setattr(store, "recover_inbound_receipts", recover)
    try:
        async with peer(monkeypatch, store, wait_for_recovery=False) as h:
            await until(entered.is_set)
            dispatch = AsyncMock(wraps=h.conn._dispatch_inbound)
            monkeypatch.setattr(h.conn, "_dispatch_inbound", dispatch)
            monkeypatch.setattr(h.conn, "_handle_ack", lambda frame: None)
            monkeypatch.setattr(h.conn, "_handle_message_error", lambda frame: None)
            for event in ("message.ack", "message.error", "pong", "history.truncated"):
                h.ws.feed({"event": event, "payload": {}})
            h.ws.feed({"event": "sync.mark", "dseq": 1})
            h.ws.feed({"event": "replay.done", "dseq": 2, "payload": {"ack_epoch": "test-epoch"}})
            await until(lambda: dispatch.await_count == 6 and bool(h.ws.acks))
            assert not h.conn._receipt_recovery_ready.is_set()
            assert h.ws.acks[0]["payload"]["dseq"] == 2
            release.set()
            await until(lambda: h.conn._credential_watch_task is not None)
    finally:
        release.set()


@pytest.mark.parametrize("failure", ["return", "raise", "timeout"])
async def test_recovery_failure_releases_order_gate_without_finishing_pending(monkeypatch, store, failure, caplog):
    seed_pending(store, message(message_id="test-old"))
    release = threading.Event()
    def recover(**kwargs):
        if failure == "raise":
            raise OSError("test recovery failure")
        if failure == "timeout":
            assert release.wait(3)
        return None
    monkeypatch.setattr(store, "recover_inbound_receipts", recover)
    monkeypatch.setattr(delivery, "RECEIPT_RECOVERY_TIMEOUT_SECONDS", 0.03)
    handler = AsyncMock()
    try:
        async with peer(monkeypatch, store, on_message=handler) as h:
            assert h.conn._receipt_recovery_ready.is_set()
            h.ws.feed(message(1, "test-new"))
            await until(lambda: handler.await_count == 1 and not h.conn._inbound_lanes)
            release.set()
            await asyncio.sleep(0.04)
            assert next(r for r in rows(store) if r["key"] == "test-old")["status"] == "pending"
            assert handler.await_count == 1
            if failure == "timeout":
                assert "recovery timed out" in caplog.text
    finally:
        release.set()


@pytest.mark.parametrize("env,configured,expected", [("TRUE", False, True), ("false", True, False),
    ("1", False, True), ("0", True, False), ("YeS", False, True), ("NO", True, False),
    ("on", False, True), ("OFF", True, False), ("invalid", True, False),
    (None, True, True), (None, False, False), (None, "true", False), (None, 1, False), (None, None, False)])
def test_config_env_precedence_and_invalid_values(monkeypatch, store, env, configured, expected, caplog):
    monkeypatch.setattr(config_module, "_get_env", lambda *names: env or "" if names[0] == "CLAWCHAT_RELIABLE_DELIVERY_V2" else "")
    cfg = ClawChatConfig.from_platform_config(SimpleNamespace(extra={"reliable_delivery_v2": configured}))
    assert cfg.reliable_delivery_v2 is expected
    if env == "invalid" or not isinstance(configured, bool):
        assert any(r.levelno == logging.WARNING and "reliable_delivery_v2" in r.message for r in caplog.records)


def test_config_default_disabled_and_profile_dotenv(monkeypatch, tmp_path, store):
    monkeypatch.delenv("CLAWCHAT_RELIABLE_DELIVERY_V2", raising=False)
    assert ClawChatConfig.from_platform_config(SimpleNamespace(extra={})).reliable_delivery_v2 is False
    monkeypatch.setenv("CLAWCHAT_RELIABLE_DELIVERY_V2", "false")
    (tmp_path / ".env").write_text("CLAWCHAT_RELIABLE_DELIVERY_V2=yes\n")
    monkeypatch.setattr(config_module, "hermes_home", lambda: tmp_path)
    assert ClawChatConfig.from_platform_config(SimpleNamespace(extra={})).reliable_delivery_v2 is True


async def test_tracker_out_of_order_and_dispose(monkeypatch):
    monkeypatch.setattr(delivery, "ACK_DEBOUNCE_SECONDS", 0.01)
    ws = FakeWS({})
    tracker = delivery.DseqAckTracker(ws, "test-epoch", lambda: True)
    try:
        tracker.record(2)
        assert tracker.acked == 0
        tracker.record(1)
        await until(lambda: bool(ws.acks))
        assert ws.acks[0]["payload"]["dseq"] == 2
        tracker.dispose()
        tracker.record(3)
        await tracker.flush_now()
        assert len(ws.acks) == 1
    finally:
        tracker.dispose()


async def test_receipt_io_is_off_event_loop(monkeypatch, store):
    main_thread = threading.get_ident()
    original = store.upsert_inbound_receipt
    threads = []
    def write(**kwargs):
        threads.append(threading.get_ident())
        return original(**kwargs)
    monkeypatch.setattr(store, "upsert_inbound_receipt", write)
    async with peer(monkeypatch, store) as h:
        h.ws.feed(message())
        await until(lambda: h.conn._delivery_tracker.acked == 1)
        assert threads and main_thread not in threads


async def test_old_receipt_completion_cannot_record_after_dispose(monkeypatch, store):
    entered, release = threading.Event(), threading.Event()
    original = store.upsert_inbound_receipt
    def write(**kwargs):
        entered.set()
        assert release.wait(2)
        return original(**kwargs)
    monkeypatch.setattr(store, "upsert_inbound_receipt", write)
    handler = AsyncMock()
    async with peer(monkeypatch, store, on_message=handler) as h:
        tracker = h.conn._delivery_tracker
        try:
            h.ws.feed(message())
            await until(entered.is_set)
            h.conn._dispose_delivery_tracker()
            h.conn._delivery_generation += 1
        finally:
            release.set()
        await until(lambda: h.conn._supervisor_task.done())
        assert tracker.acked == 0 and not h.ws.acks
        assert handler.await_count == 0 and rows(store)[0]["status"] == "pending"


async def test_old_instance_blocked_write_cannot_replace_new_instance_finished_receipt(monkeypatch, store):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = store.upsert_inbound_receipt
    old_orders = []
    def write(**kwargs):
        if kwargs["epoch"] == "test-epoch":
            old_orders.append(kwargs["write_order"])
            entered.set()
            assert release.wait(4)
            result = original(**kwargs)
            finished.set()
            return result
        return original(**kwargs)
    monkeypatch.setattr(store, "upsert_inbound_receipt", write)
    old_handler, new_handler = AsyncMock(), AsyncMock()
    try:
        async with peer(monkeypatch, store, on_message=old_handler, cleanup_workers=False) as old:
            old.ws.feed(message(text="old connection"))
            await until(entered.is_set)
            old_tracker = old.conn._delivery_tracker
            await old.conn.stop()
            async with peer(monkeypatch, store, on_message=new_handler, cleanup_workers=False,
                            hello={"ack_mode": "dseq", "ack_epoch": "new-epoch"}) as new:
                new.ws.feed(message(text="new connection"))
                new.ws.feed({"event": "replay.done", "dseq": 2, "payload": {"ack_epoch": "new-epoch"}})
                await until(lambda: new_handler.await_count == 1 and not new.conn._inbound_lanes and bool(new.ws.acks))
                snapshot = rows(store)[0]
                assert snapshot["epoch"] == "new-epoch" and snapshot["dseq"] == 1
                assert snapshot["write_order"] > old_orders[0] and snapshot["status"] == "done"
                assert "new connection" in snapshot["raw_json"]
                release.set()
                await until(finished.is_set)
                assert rows(store)[0] == snapshot
                assert new.ws.acks[-1]["payload"] == {"dseq": 2, "epoch": "new-epoch"}
                assert old_tracker.disposed and old_tracker.acked == 0
                assert old_handler.await_count == 0 and not old.ws.acks
    finally:
        release.set()
        shutdown = asyncio.create_task(asyncio.get_running_loop().shutdown_default_executor())
        await until(shutdown.done)
        await shutdown


async def test_stop_waits_for_sync_ack_send_before_closing(monkeypatch, store):
    monkeypatch.setattr(delivery, "ACK_DEBOUNCE_SECONDS", 10)
    async with peer(monkeypatch, store) as h:
        h.ws.feed({"event": "sync.mark", "dseq": 1})
        await until(lambda: h.conn._delivery_tracker.acked == 1)
        entered, release = asyncio.Event(), asyncio.Event()
        original = h.ws.send
        async def send(raw):
            entered.set()
            await release.wait()
            await original(raw)
        monkeypatch.setattr(h.ws, "send", send)
        stopping = asyncio.create_task(h.conn.stop())
        await asyncio.wait_for(entered.wait(), 1)
        assert not stopping.done() and not h.ws.closed
        release.set()
        await stopping
        assert h.ws.at_close[0]["payload"]["dseq"] == 1


async def test_stop_flush_timeout_is_bounded(monkeypatch, store):
    monkeypatch.setattr(delivery, "ACK_DEBOUNCE_SECONDS", 10)
    monkeypatch.setattr(delivery, "ACK_FLUSH_TIMEOUT_SECONDS", 0.02)
    async with peer(monkeypatch, store) as h:
        h.ws.feed({"event": "sync.mark", "dseq": 1})
        await until(lambda: h.conn._delivery_tracker.acked == 1)
        async def blocked_send(raw):
            await asyncio.Event().wait()
        monkeypatch.setattr(h.ws, "send", blocked_send)
        await asyncio.wait_for(h.conn.stop(), 0.5)
        assert h.ws.closed and h.conn._delivery_tracker is None


async def test_cancelled_signal_keeps_pending_receipt_without_frame_mapping(monkeypatch, store):
    entered = asyncio.Event()
    async def handler(frame):
        entered.set()
        await asyncio.Event().wait()
    async with peer(monkeypatch, store, on_signal=handler) as h:
        h.ws.feed({"event": "notify.signal", "dseq": 1,
                   "payload": {"event_id": "test-signal", "type": "test.changed"}})
        await until(entered.is_set)
        await h.conn.stop()
        assert rows(store)[0]["status"] == "pending"
        assert h.conn._inbound_receipts == {}


async def test_reconnect_can_withdraw_v2_grant(monkeypatch, store):
    async with peer(monkeypatch, store) as h:
        old_tracker = h.conn._delivery_tracker
        await h.ws.close()
        await until(lambda: h.conn._supervisor_task.done())
        ws = FakeWS({})
        monkeypatch.setattr(connection, "_ws_connect", AsyncMock(return_value=ws))
        h.conn._supervisor_task = asyncio.create_task(h.conn._run_one_connection())
        await until(lambda: h.conn._credential_watch_task is not None)
        ws.feed(message(100))
        await until(lambda: not ws.incoming.qsize())
        assert old_tracker.disposed and h.conn._delivery_tracker is None
        assert not ws.acks and not h.conn.dseq_density_violations


@pytest.mark.parametrize("dseq", [True, False, "1", 1.0, None])
async def test_non_integer_dseq_ignored(monkeypatch, store, dseq):
    async with peer(monkeypatch, store) as h:
        h.ws.feed({"event": "future.event", "dseq": dseq})
        h.ws.feed({"event": "sync.mark", "dseq": 1})
        await until(lambda: h.conn._delivery_tracker.acked == 1)
        assert rows(store) == [] and not h.conn.dseq_density_violations


async def test_tracker_generation_guard_and_send_error_redaction(monkeypatch, caplog):
    monkeypatch.setattr(delivery, "ACK_DEBOUNCE_SECONDS", 10)
    ws = FakeWS({})
    current = True
    tracker = delivery.DseqAckTracker(ws, "test-epoch", lambda: current)
    try:
        tracker.record(1)
        ws.send_error = RuntimeError("placeholder-send-secret")
        await tracker.flush_now()
        assert "placeholder-send-secret" not in caplog.text
        assert "sync_ack send failed" in caplog.text
        current = False
        ws.send_error = None
        tracker.record(2)
        await tracker.flush_now()
        assert tracker.acked == 1 and not ws.acks
    finally:
        tracker.dispose()


@pytest.mark.parametrize("event,kind", [("message.send", "message"), ("message.reply", "message"),
                                        ("notify.signal", "signal"), ("future.event", "other")])
async def test_missing_identity_falls_back_to_epoch_dseq(monkeypatch, store, event, kind):
    async with peer(monkeypatch, store) as h:
        frame = message(event=event)
        frame["payload"] = {}
        h.ws.feed(frame)
        await until(lambda: h.conn._delivery_tracker.acked == 1 and not h.conn._inbound_lanes)
        row = rows(store)[0]
        assert row["kind"] == kind and row["key"] == "test-epoch:1"


def test_storage_preserves_done_upserts_pending_and_isolates_accounts(store):
    kwargs = dict(account_id="default", kind="message", key="test-message", dseq=1,
                  epoch="test-epoch", event="message.send", raw=message(),
                  write_order=delivery.next_write_order())
    assert store.upsert_inbound_receipt(**kwargs) is True
    finish = {k: kwargs[k] for k in ("account_id", "kind", "key", "dseq", "epoch")}
    assert store.finish_inbound_receipt(**finish, error="failed") is True
    kwargs.update(dseq=2, epoch="replay-epoch", raw=message(2, text="new"),
                  write_order=delivery.next_write_order())
    assert store.upsert_inbound_receipt(**kwargs) is True
    assert rows(store)[0]["status"] == "pending" and rows(store)[0]["error"] is None
    # Finishing an old generation cannot finish the refreshed pending row.
    assert store.finish_inbound_receipt(**finish) is True
    assert rows(store)[0]["status"] == "pending"
    finish.update(dseq=2, epoch="replay-epoch")
    assert store.finish_inbound_receipt(**finish) is True
    kwargs.update(dseq=3, raw=message(3, text="latest"), write_order=delivery.next_write_order())
    assert store.upsert_inbound_receipt(**kwargs) is True
    assert rows(store)[0]["status"] == "done"
    assert "latest" in rows(store)[0]["raw_json"]
    kwargs["account_id"] = "test-other-account"
    assert store.upsert_inbound_receipt(**kwargs) is True
    assert len(rows(store)) == 2


def test_storage_reports_real_failure_without_exception_text(monkeypatch, store, caplog):
    def fail(callback):
        raise OSError("placeholder-storage-secret")
    monkeypatch.setattr(store, "_inbound_receipt_transaction", fail)
    assert store.upsert_inbound_receipt(account_id="default", kind="message", key="test-message",
        dseq=1, epoch="test-epoch", event="message.send", raw=message(),
        write_order=delivery.next_write_order()) is False
    assert "placeholder-storage-secret" not in caplog.text


@pytest.mark.parametrize("equal_order", [False, True])
def test_stale_receipt_upsert_preserves_every_newer_column(store, equal_order):
    old_order, new_order = delivery.next_write_order(), delivery.next_write_order()
    newer = dict(account_id="default", kind="message", key="test-message", dseq=9,
                 epoch="new-epoch", event="message.reply", raw=message(9, text="newer", event="message.reply"),
                 write_order=new_order)
    assert store.upsert_inbound_receipt(**newer) is True
    finish = {k: newer[k] for k in ("account_id", "kind", "key", "dseq", "epoch")}
    assert store.finish_inbound_receipt(**finish, error="test newer failure") is True
    snapshot = rows(store)[0]
    assert store.upsert_inbound_receipt(
        account_id="default", kind="message", key="test-message", dseq=1, epoch="old-epoch",
        event="message.send", raw=message(text="older"), write_order=new_order if equal_order else old_order,
    ) is True
    assert rows(store)[0] == snapshot


def test_receipt_write_order_is_process_wide_monotonic_when_clock_moves_back(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setattr(delivery.time, "time_ns", lambda: 1)
    start = delivery.next_write_order()
    with ThreadPoolExecutor(max_workers=4) as pool:
        orders = list(pool.map(lambda _: delivery.next_write_order(), range(64)))
    assert sorted(orders) == list(range(start + 1, start + 65))
