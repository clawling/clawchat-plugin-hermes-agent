"""Conversation-local history maintenance and its narrow Hermes compatibility bridge.

Only the ledger and generation-CAS state are authoritative. A host reset is never
inferred from a returned banner or an in-memory session entry.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import inspect
import json
import logging
from pathlib import Path
import time
import weakref

from clawchat_gateway.config import effective_history_settings
from clawchat_gateway.history_budget import (
    Entry, _entry_time, estimate_entry_tokens, history_tokens, plan_compaction,
    recent_window_for_injection, split_blocks,
)
from clawchat_gateway.history_transcript import (
    UNTRUSTED_HISTORY, quoted_history_summary, render_entry, rendered_entry_line,
    rendered_summary_messages, safe_history_summary,
)
from clawchat_gateway.rolling_summary import (
    cleanup_summaries, publish_summary, read_summary, summary_dir,
)

logger = logging.getLogger(__name__)
MAINTENANCE_SECONDS = 300
RESET_WARNING_SECONDS = 120
POLL_SECONDS = .1
_internal_reset = ContextVar("clawchat_internal_history_reset", default=None)
_llm = None
_instances = weakref.WeakSet()


@dataclass
class _ActivityTurn:
    chat_id: str
    trigger: str
    real: bool
    closed: bool = False


_activity_turn = ContextVar("clawchat_history_activity_turn", default=None)


@contextmanager
def activity_turn(event, inbound, *, control=False):
    """Host tasks inherit provenance; unrelated notices have no answer marker."""
    raw = inbound.raw_message if isinstance(inbound.raw_message, dict) else {}
    if raw.get("clawchat_group_batch"):
        raw = (raw.get("messages") or [{}])[-1]
    payload = raw.get("payload") or {}
    trigger = payload.get("message_id") or raw.get("message_id") or getattr(event, "message_id", "") or ""
    marker = _ActivityTurn(inbound.chat_id, trigger,
                           bool(trigger) and not inbound.is_internal_synthetic and not control
                           and not inbound.text.lstrip().startswith("/"))
    event._clawchat_history_activity = marker
    context = _activity_turn.set(marker)
    try:
        yield marker
    finally:
        _activity_turn.reset(context)


def bind_activity(event, marker=None):
    # Durable host admission may restore a new event object in another task.
    # The plugin-owned nonce map preserves provenance across that boundary.
    existing = getattr(event, "_clawchat_history_activity", None)
    if existing is not None and marker is not None and existing is not marker:
        existing.closed = True
    marker = existing or marker
    if marker is not None:
        event._clawchat_history_activity = marker
    _activity_turn.set(marker)


def restore_activity(adapter, event):
    """Recover provenance for host-admitted work resumed after a gateway restart."""
    if getattr(event, "internal", False):
        return None
    chat_id = str(getattr(getattr(event, "source", None), "chat_id", "") or "")
    trigger = str(getattr(event, "message_id", "") or adapter._turn_trigger_message_id(event))
    store = getattr(adapter, "_store", None)
    if not chat_id or not trigger or store is None:
        return None
    try:
        rows = store.list_chat_history_messages("default", chat_id)
        for row in rows or []:
            if row["direction"] != "inbound" or row["message_id"] != trigger:
                continue
            raw = json.loads(row.get("raw_json") or "{}")
            if not isinstance(raw, dict) or raw.get("clawchat_history_control") is True:
                return None
            text = str(row.get("text") or "").lstrip()
            if text.startswith("/") or raw.get("sender", {}).get("id") == "system":
                return None
            return _ActivityTurn(chat_id, trigger, True)
    except Exception:
        # Missing optional provenance must never break delivery or invent activity.
        return None
    return None


def finish_activity(event, marker=None):
    marker = getattr(event, "_clawchat_history_activity", None) or marker
    if marker is not None:
        marker.closed = True
    _activity_turn.set(None)


def outbound_activity(chat_id, metadata=None):
    marker = _activity_turn.get()
    interim = isinstance(metadata, dict) and metadata.get("_interim_send")
    if marker and marker.real and not marker.closed and marker.chat_id == chat_id and not interim:
        return {"kind": "answer", "trigger": marker.trigger}
    return {"kind": "notice"}


def original_message_time(raw, created_at):
    from clawchat_gateway.chat_history import _stamp
    for value in (raw.get("emitted_at"), raw.get("timestamp"), created_at):
        if value is not None:
            stamp = _stamp(value)
            if _entry_time(stamp) is not None:
                return stamp
    return None


def configure(llm):
    global _llm
    _llm = llm


def reset_signature_supported(fn):
    if not callable(fn) or not inspect.iscoroutinefunction(fn):
        return False
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    return (len(params) == 1 and params[0].name == "event"
            and params[0].kind in (inspect.Parameter.POSITIONAL_ONLY,
                                   inspect.Parameter.POSITIONAL_OR_KEYWORD))


def routing_reader(store):
    """Select the authority once. Never fall back to a stale legacy mirror."""
    db = getattr(store, "_routing_db", None)
    if db is not None:
        load = getattr(db, "load_gateway_routing_entries", None)
        scope = getattr(store, "_routing_scope", None)
        if not callable(load) or not callable(scope):
            raise RuntimeError("persistent database routing reader unavailable")
        def read(key):
            value = load(scope=scope()).get(key)
            return str(json.loads(value)["session_id"]) if value else None
        return read
    directory = getattr(store, "sessions_dir", None)
    if directory is None:
        raise RuntimeError("persistent legacy routing reader unavailable")
    def read(key):
        values = json.loads((Path(directory) / "sessions.json").read_text(encoding="utf-8"))
        value = values.get(key)
        return str(value["session_id"]) if value else None
    return read


def suppress_reset_output(chat_id):
    operation = _internal_reset.get()
    return bool(operation and operation[1] == chat_id)


def _session_env(name):
    try:
        from gateway.session_context import get_session_env
        return str(get_session_env(name, "") or "")
    except Exception:
        return ""


def post_api_request(**kwargs):
    if kwargs.get("platform") != "clawchat":
        return
    nonce = _session_env("HERMES_SESSION_MESSAGE_ID")
    key = _session_env("HERMES_SESSION_KEY")
    sid = str(kwargs.get("session_id") or "")
    from clawchat_gateway.sediment import _prompt_tokens
    tokens = _prompt_tokens(kwargs.get("usage"))
    for instance in list(_instances):
        if instance.loop and not instance.loop.is_closed() and not instance.closed:
            instance.loop.call_soon_threadsafe(instance.observe_request, nonce, key, sid, tokens)


def on_session_reset(**kwargs):
    if kwargs.get("platform") != "clawchat":
        return
    operation = _internal_reset.get()
    for instance in list(_instances):
        if instance.loop and not instance.loop.is_closed() and not instance.closed:
            instance.loop.call_soon_threadsafe(
                instance.observe_reset, kwargs.get("old_session_id"),
                kwargs.get("new_session_id") or kwargs.get("session_id"), operation)


class Superseded(Exception):
    """Another generation committed; discard this attempt's result."""


class HistoryMaintenance:
    def __init__(self, adapter, llm=None):
        self.adapter = adapter
        self.store = adapter._store
        self.account = adapter._account_id()
        self.llm = llm if llm is not None else _llm
        self.gateway = None
        self.read_route = None
        self.available = False
        self.loop = None
        self.closed = False
        self.epoch = 0
        self.locks = {}
        self.tasks = {}
        self.watchdogs = {}
        self.blocked = {}
        self.reset_tasks = {}
        self.candidates = {}
        self.injections = {}
        self.usage = {}
        self.turns = {}
        self.inflight = set()
        self.releasing = set()
        self.releasing_messages = set()
        self.drain_waiting = set()
        self.drains = set()
        self.replays = {}
        self.delivered = {}
        self.confirmed = {}
        self.progress = {}
        self.running_tasks = set()
        _instances.add(self)

    def connect(self):
        self.loop = asyncio.get_running_loop()
        self.closed = False
        self.epoch += 1
        missing = []
        try:
            from clawchat_gateway.adapter import _host_supports_channel_context, _HOST_AGENT_VERSION
            import re
            version = re.match(r"v?(\d+)\.(\d+)\.(\d+)", _HOST_AGENT_VERSION or "")
            if not version or tuple(map(int, version.groups())) < (0, 14, 0):
                missing.append("unknown or old host version")
        except Exception:
            missing.append("unknown host version")
        try:
            fn = getattr(self.llm, "acomplete", None)
            if not callable(fn):
                raise ValueError
            inspect.signature(fn).bind([], timeout=120, max_tokens=4000)
        except Exception:
            missing.append("ctx.llm.acomplete")
        try:
            if not _host_supports_channel_context():
                missing.append("channel_context")
        except Exception:
            missing.append("channel_context")
        self.gateway = getattr(self.adapter, "gateway_runner", None)
        if not self.gateway:
            missing.append("gateway_runner")
        if not reset_signature_supported(getattr(self.gateway, "_handle_reset_command", None)):
            missing.append("async _handle_reset_command(event)")
        self.read_route = None
        try:
            host_store = self.gateway.session_store
            read_route = routing_reader(host_store)
            if not callable(getattr(host_store, "peek_session_id", None)):
                raise ValueError
            if not callable(getattr(self.gateway, "_session_key_for_source", None)):
                raise ValueError
            self.read_route = read_route
        except Exception:
            missing.append("canonical key / persistent routing")
        if not callable(getattr(self.gateway, "_is_session_running", None)):
            missing.append("host running predicate")
        # Newer hosts expose _queued_events as a dict-like view, not a dict.
        if not callable(getattr(getattr(self.gateway, "_queued_events", None), "get", None)):
            missing.append("_queued_events")
        if not isinstance(getattr(self.gateway, "_startup_restore_queue", None), list):
            missing.append("_startup_restore_queue")
        for name in ("_pending_messages", "_text_debounce"):
            if not isinstance(getattr(self.adapter, name, None), dict):
                missing.append(name)
        if self.store is None:
            missing.append("ledger")
        self.available = not missing
        if missing:
            logger.warning("clawchat budget unavailable; host compaction and legacy notes retained: %s",
                           ", ".join(missing))
        else:
            config = self.adapter._clawchat_config
            logger.info("ClawChat budget dm/group %s (%s)/%s (%s), "
                        "host threshold %s is the backstop only; enabled=%s; conversation overrides take precedence",
                        config.history_budget_dm, config.history_sources.get("history-budget-dm", "factory"),
                        config.history_budget_group, config.history_sources.get("history-budget-group", "factory"),
                        self.adapter._host_compression_cap, config.budget_compaction)
            if version and tuple(map(int, version.groups())) < (0, 21, 1):
                policies = getattr(getattr(self.gateway, "config", None), "reset_by_platform", {})
                default = getattr(getattr(self.gateway, "config", None), "default_reset_policy", None)
                policy = next((p for platform, p in policies.items()
                               if str(getattr(platform, "value", platform)) == "clawchat"), default)
                if policy is None or getattr(policy, "mode", "both") != "none":
                    logger.warning("clawchat host time resets may clear context before answering; "
                                   "disable them or upgrade to 0.21.1+ for answer-before-idle-maintenance")

    def enabled(self, inbound):
        return bool(self.available and self.journal_ready() and self.account
                    and self.settings(inbound).budget_compaction)

    def journal_ready(self):
        """Accepted inputs must drain even after summary capability is disabled."""
        return bool(not self.closed and self.store is not None
                    and self.account == self.adapter._account_id()
                    and callable(self.read_route)
                    and callable(getattr(getattr(self.gateway, "session_store", None), "peek_session_id", None))
                    and callable(getattr(self.gateway, "_session_key_for_source", None)))

    def settings(self, inbound):
        return effective_history_settings(self.adapter._clawchat_config,
                                          inbound.chat_id, inbound.chat_type == "group")

    def source(self, inbound):
        return self.adapter.build_source(
            chat_id=inbound.chat_id,
            user_id=self.adapter._session_user_id_for_inbound(inbound),
            chat_type=self.adapter._map_source_chat_type(inbound.chat_type))

    def key(self, inbound):
        return self.gateway._session_key_for_source(self.source(inbound))

    def state(self, key):
        return self.store.get_history_state(self.account, key) or {"gen": 0}

    def cas(self, key, state, **fields):
        for marker in ("summary_through", "boundary"):
            if marker in fields and marker + "_seq" not in fields:
                fields[marker + "_seq"] = self.position(fields[marker]) if fields[marker] else None
        if self.closed or self.account != self.adapter._account_id() or not self.store.cas_history_state(self.account, key, state["gen"], **fields):
            raise Superseded
        return self.state(key)

    def directory(self, key):
        # DB names encode the active profile; keep summaries under the same home.
        db = self.store.db_path
        prefix = db.stem.removeprefix("clawchat").lstrip("-")
        root = db.parent / "summaries"
        if prefix:
            root /= prefix
        return summary_dir(root, self.account, key)

    def entries(self, inbound):
        from clawchat_gateway.chat_history import _entry, _speaker
        from clawchat_gateway.inbound import split_leading_mentions
        rows = self.store.list_chat_history_messages("default", inbound.chat_id)
        if rows is None:
            raise RuntimeError("history ledger unavailable")
        scope = {"self_user_id": self.adapter._clawchat_config.user_id,
                 "owner_user_id": self.adapter._owner_user_id(), "self_name": "you"}
        # History storage returns only normal messages, excludes recall tombstones.
        result = []
        positions = self.store.history_positions("default", inbound.chat_id)
        for row in sorted(rows, key=lambda r: positions[r.get("protocol_message_id") or r["message_id"]]):
            e = _entry(row, scope)
            if not e.self:
                command_text = row.get("text") or ""
                if inbound.chat_type == "group":
                    try:
                        raw = json.loads(row.get("raw_json") or "{}")
                        split = split_leading_mentions(command_text, raw)
                    except (TypeError, ValueError):
                        split = None
                    if split:
                        command_text = split[1]
                if command_text.lstrip().startswith("/"):
                    continue
            result.append(Entry(e.id, e.ts, "self" if e.self else "owner" if e.owner else "other",
                                _speaker(e), e.text, sender_id=e.sender))
        return result

    def activity_entries(self, inbound):
        from clawchat_gateway.chat_history import _object
        rows = self.store.list_chat_history_messages("default", inbound.chat_id)
        if rows is None:
            raise RuntimeError("history activity ledger unavailable")
        entries = {entry.id: entry for entry in self.entries(inbound)}
        human = {row.get("protocol_message_id") or row["message_id"] for row in rows
                 if row["direction"] == "inbound" and (row.get("protocol_message_id") or row["message_id"]) in entries}
        controls = set()
        for row in rows:
            try:
                raw = json.loads(row.get("raw_json") or "{}")
                if isinstance(raw, dict) and raw.get("clawchat_history_control") is True:
                    controls.add(row.get("protocol_message_id") or row["message_id"])
            except (TypeError, ValueError):
                pass
        human -= controls
        result = []
        for row in rows:
            mid = row.get("protocol_message_id") or row["message_id"]
            entry = entries.get(mid)
            if entry is None:
                continue
            try:
                raw = json.loads(row.get("raw_json") or "{}")
            except (TypeError, ValueError):
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            if row["direction"] == "outbound":
                activity = raw.get("clawchat_history_activity")
                if activity is not None:
                    if not isinstance(activity, dict) or activity.get("kind") != "answer":
                        continue
                else:
                    message = _object(_object(raw.get("payload")).get("message"))
                    reply = _object(_object(message.get("context")).get("reply"))
                    if reply.get("reply_to_msg_id") not in human:
                        continue
            elif (raw.get("clawchat_history_control") is True
                  or _object(raw.get("sender")).get("id") in ("system", self.adapter._clawchat_config.user_id)):
                continue
            stamp = original_message_time(raw, row["created_at"])
            if stamp is not None:
                result.append(replace(entry, ts=stamp))
        order = self.store.history_positions("default", inbound.chat_id)
        return sorted(result, key=lambda entry: order[entry.id])

    def position(self, marker, saved=None):
        # An older writer can advance an ID while leaving an additive *_seq
        # field untouched. The immutable ID mapping resolves that mixed-version case.
        value = self.store.history_positions("default").get(marker)
        if value is None:
            raise RuntimeError("history marker has no recoverable position")
        return saved if saved == value else value

    def interval(self, inbound, state, *, cut=None, cut_seq=None, boundary=True):
        entries = self.entries(inbound)
        markers = ["summary_through"] + (["boundary"] if boundary else [])
        start = max((self.position(state[m], state.get(m + "_seq"))
                     for m in markers if state.get(m)), default=0)
        end = self.position(cut, cut_seq) if cut else float("inf")
        order = self.store.history_positions("default", inbound.chat_id)
        return [e for e in entries if start < order[e.id] <= end]

    def control_input(self, inbound):
        from clawchat_gateway.adapter import _slash_command_name, _slash_confirm_choice, HERMES_CONFIRM_SLASH_COMMANDS
        text = inbound.text
        if inbound.chat_type == "group":
            text = self.adapter._group_command_text(inbound) or text
        if _slash_command_name(text) in {"new", "reset", *HERMES_CONFIRM_SLASH_COMMANDS}:
            return True
        if self.journal_ready():
            row = self.held_row(self.key(inbound), self.adapter._inbound_trigger_id(inbound.raw_message))
            if row and "control" in row["payload"]:
                return row["payload"]["control"] is True
        choice = _slash_confirm_choice(text)
        host_choices = getattr(self.gateway, "_slash_confirm_text_choices", None)
        if choice is None and callable(host_choices):
            choice = host_choices().get(text.strip().lstrip("!/").lower())
        if choice is None:
            return False
        if (inbound.chat_id in self.adapter._non_owner_slash_confirms
                or inbound.chat_id in self.adapter._owner_group_slash_confirms):
            return True
        return self.journal_ready() and self.key(inbound) in self.candidates

    def command_prompted(self, key):
        if not self.journal_ready():
            return
        candidate = self.candidates.get(key)
        if candidate:
            self.store.finish_held_batch(self.account, key, {candidate["mid"]})
            if key in self.drain_waiting:
                self.wake_drain(key)

    def finish_control(self, inbound):
        if not self.journal_ready():
            return
        key = self.key(inbound)
        row = self.held_row(key, self.adapter._inbound_trigger_id(inbound.raw_message))
        if self.control_input(inbound) or (row and row["payload"].get("control")):
            self.no_reply(inbound)

    def retire_control_replay(self, key, row):
        if row["state"] != "dispatching":
            return False
        payload = row["payload"]
        if "control" in payload and payload["control"] is not True:
            return False
        from clawchat_gateway.inbound import parse_inbound_message
        inbound = parse_inbound_message(payload["frame"], self.adapter._clawchat_config)
        control = payload.get("control") is True or (inbound and self.control_input(inbound))
        if not control and inbound:
            from clawchat_gateway.adapter import _slash_confirm_choice, _slash_command_name
            if _slash_confirm_choice(inbound.text) is not None:
                previous = [item for item in self.store.list_held(
                    self.account, key, states=("held", "dispatching", "dispatched")) if item["seq"] < row["seq"]]
                if previous:
                    last = previous[-1]
                    prior = parse_inbound_message(last["payload"]["frame"], self.adapter._clawchat_config)
                    control = last["payload"].get("control") is True or bool(
                        "control" not in last["payload"] and prior
                        and _slash_command_name(prior.text) in {"new", "reset"})
        if not control:
            return False
        self.store.finish_held_batch(self.account, key, {row["message_id"]})
        logger.info("clawchat history control not replayed conv=%s", key)
        return True

    def claim_options(self, inbound):
        if not self.enabled(inbound) or inbound.is_internal_synthetic:
            return {}
        return {"held_key": self.key(inbound), "held_account_id": self.account, "held_payload": {"frame": inbound.raw_message, "control": self.control_input(inbound)}}

    def held_row(self, key, mid):
        return next((row for row in self.store.list_held(self.account, key)
                     if row["message_id"] == mid), None)

    def wake_drain(self, key):
        if self.closed:
            return
        task = asyncio.create_task(self.release(key))
        self.drains.add(task)
        task.add_done_callback(self.drains.discard)

    def recalled_inbound(self, inbound):
        if inbound.is_internal_synthetic or self.store is None:
            return False
        ids = self.adapter._batch_message_ids(inbound)
        if not ids:
            return False
        ready = self.journal_ready()
        if ready:
            keys = {self.key(inbound)}
        else:
            # Recall remains authoritative if capabilities fail during an await.
            # Use durable journal keys; the legacy path must not call host routing.
            keys = {row["conv_key"] for row in self.store.list_held(self.account)
                    if row["message_id"] in ids}
        recalled = set()
        for key in keys or {""}:
            checked = set(ids)
            for mid in tuple(ids):
                row = self.held_row(key, mid)
                snapshot = row["payload"].get("snapshot", {}) if row else {}
                checked.update(snapshot.get("history", {}).get("ids", []))
            found = self.store.retire_recalled_held(self.account, key, checked)
            recalled.update(found)
            if found:
                for mid in checked:
                    self.replays.pop(mid, None)
                if ready:
                    self.wake_drain(key)
        return bool(recalled)

    def recalled_row(self, key, row):
        primary = row["payload"].get("batch_primary", row["message_id"])
        parent = self.held_row(key, primary) or row
        snapshot = parent["payload"].get("snapshot", {})
        raw = snapshot.get("raw_message", {}).get("clawchat_raw", {})
        frames = raw.get("messages", []) if raw.get("clawchat_group_batch") else []
        ids = {row["message_id"], primary}
        ids.update(snapshot.get("history", {}).get("ids", []))
        ids.update(self.adapter._inbound_trigger_id(frame) for frame in frames)
        recalled = self.store.retire_recalled_held(self.account, key, ids - {""})
        if recalled:
            for mid in ids:
                self.replays.pop(mid, None)
        return row["message_id"] in recalled

    @asynccontextmanager
    async def dispatch_scope(self, inbound):
        if inbound.is_internal_synthetic or not self.journal_ready():
            yield
            return
        key = self.key(inbound)
        owned = {(key, mid) for mid in self.adapter._batch_message_ids(inbound)} - self.inflight
        self.inflight.update(owned)
        try:
            yield
        finally:
            self.inflight.difference_update(owned)
            if key in self.drain_waiting:
                self.wake_drain(key)

    def defer(self, inbound):
        if inbound.is_internal_synthetic or not self.enabled(inbound):
            return False
        key = self.key(inbound)
        mid = self.adapter._inbound_trigger_id(inbound.raw_message)
        draining = key in self.releasing or key in self.drain_waiting
        admitted = (key, mid) in self.releasing_messages or (key, mid) in self.inflight
        if key not in self.blocked and (not draining or admitted):
            return False
        for mid in self.adapter._batch_message_ids(inbound):
            row = self.held_row(key, mid)
            if row is None:
                raise RuntimeError("maintenance inbound has no durable claim")
            if row["state"] == "dispatching":
                self.store.mark_held_state(self.account, key, mid, "dispatching", "held")
        return True

    async def continue_claimed(self, inbound, key=None):
        if not self.journal_ready():
            return await self.adapter._continue_claimed_inbound(inbound)
        key = key or self.key(inbound)
        if self.recalled_inbound(inbound):
            return
        mid = self.adapter._inbound_trigger_id(inbound.raw_message)
        if self.defer(inbound):
            return
        row = self.held_row(key, mid)
        if row:
            if row["state"] == "held":
                if not self.store.mark_held_state(self.account, key, mid, "held", "dispatching"):
                    return
            self.inflight.add((key, mid))
            if row["payload"].get("snapshot"):
                self.replays[mid] = row["payload"]["snapshot"]
        try:
            await self.adapter._continue_claimed_inbound(inbound)
        finally:
            self.inflight.discard((key, mid))
            self.replays.pop(mid, None)
            if key in self.drain_waiting:
                self.wake_drain(key)

    async def release(self, key):
        if key in self.releasing or not self.journal_ready():
            return
        self.releasing.add(key)
        self.drain_waiting.discard(key)
        try:
            attempted = set()
            while not self.closed and key not in self.blocked:
                waiting = [row for row in self.store.list_held(self.account, key)
                           if row["message_id"] not in attempted]
                if not waiting:
                    break
                for candidate in waiting:
                    if self.closed or key in self.blocked:
                        break
                    mid = candidate["message_id"]
                    attempted.add(mid)
                    row = self.held_row(key, mid)
                    if row is None:
                        continue
                    if self.retire_control_replay(key, row):
                        continue
                    if self.recalled_row(key, row):
                        continue
                    row = self.held_row(key, mid)
                    if row is None:
                        continue
                    if (key, mid) in self.inflight:
                        self.drain_waiting.add(key)
                        return
                    if row["payload"].get("terminal"):
                        self.store.mark_held_state(self.account, key, mid, row["state"], "dispatched")
                        continue
                    if row["payload"].get("batch_primary", mid) != mid:
                        continue
                    inbound = await self.restore_inbound(row)
                    if inbound is None:
                        continue
                    logger.info("clawchat history replay conv=%s state=%s", key, row["state"])
                    self.releasing_messages.add((key, mid))
                    try:
                        await self.continue_claimed(inbound, key)
                    finally:
                        self.releasing_messages.discard((key, mid))
        except Exception:
            logger.warning("clawchat history held release failed conv=%s", key)
        finally:
            self.releasing.discard(key)

    async def restore_inbound(self, row):
        from clawchat_gateway.inbound import parse_inbound_message
        if not self.journal_ready() or self.recalled_row(row["conv_key"], row):
            return None
        # A recall may have invalidated the batch snapshot above.
        row = self.held_row(row["conv_key"], row["message_id"]) or row
        inbound = parse_inbound_message(row["payload"]["frame"], self.adapter._clawchat_config)
        if inbound is None or self.adapter._is_chat_dead(inbound.chat_id):
            return None
        snapshot = row["payload"].get("snapshot", {})
        raw = snapshot.get("raw_message", {}).get("clawchat_raw", {})
        if raw.get("clawchat_group_batch"):
            from clawchat_gateway.group_message_coalescer import GroupMessageCoalescer
            messages = [parse_inbound_message(frame, self.adapter._clawchat_config)
                        for frame in raw.get("messages", [])]
            if not messages or any(message is None for message in messages):
                return None
            rebuilt = []
            async def capture(message):
                rebuilt.append(message)
            batch = GroupMessageCoalescer(idle_seconds=0, max_wait_seconds=0, dispatch=capture)
            batch._pending[inbound.chat_id] = [self.adapter._resolve_inbound_sender_context(message)
                                             for message in messages]
            await batch.flush(inbound.chat_id, force=True)
            inbound = rebuilt[0]
        return self.adapter._resolve_inbound_sender_context(inbound)

    def begin_recovery(self):
        """Do not block the transport's READY callback or its receive loop."""
        if not self.journal_ready():
            return
        epoch = self.epoch
        async def run():
            try:
                await self.recover()
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self.closed and self.epoch == epoch:
                    self.available = False
                    logger.warning("clawchat history recovery unavailable; retaining journal and host backstop")
        task = asyncio.create_task(run())
        self.drains.add(task)
        task.add_done_callback(self.drains.discard)

    async def recover(self):
        if not self.journal_ready():
            return
        # Recovery is required even when the budget switch was subsequently disabled.
        for key in dict.fromkeys(row["conv_key"] for row in self.store.list_held(self.account)):
            await self.release(key)
        # A completed handoff can leave pending summary/reset work across restart.
        # Resume it from its journaled source without inventing a new trigger.
        latest = {}
        for row in self.store.list_held(self.account, states=("held", "dispatching", "dispatched")):
            latest[row["conv_key"]] = row
        for key, row in latest.items():
            state = self.state(key)
            if state.get("pending") and not state.get("retry_after_msgs"):
                inbound = await self.restore_inbound(row)
                if inbound and self.enabled(inbound):
                    self.schedule(inbound)

    def no_reply(self, inbound):
        if inbound.is_internal_synthetic or not self.journal_ready():
            return
        key = self.key(inbound)
        self.store.finish_held_batch(self.account, key, self.adapter._batch_message_ids(inbound))
        if key in self.drain_waiting:
            self.wake_drain(key)

    def replay_nonce(self, inbound):
        if not self.journal_ready():
            return None
        mid = self.adapter._inbound_trigger_id(inbound.raw_message)
        snapshot = self.replays.get(mid)
        if not snapshot and self.gateway:
            row = self.held_row(self.key(inbound), mid)
            snapshot = row["payload"].get("snapshot") if row else None
        return (snapshot or {}).get("nonce")

    def save_snapshot(self, inbound, event, history_text=None):
        if inbound.is_internal_synthetic or not self.journal_ready():
            return
        key = self.key(inbound)
        mid = self.adapter._inbound_trigger_id(inbound.raw_message)
        snapshot = self.replays.get(mid)
        if not snapshot:
            existing = self.held_row(key, mid)
            snapshot = existing["payload"].get("snapshot") if existing else None
        fields = ("text", "channel_context", "channel_prompt", "raw_message", "media_urls", "media_types",
                  "reply_to_message_id", "reply_to_text", "allow_gateway_control")
        if snapshot:
            # Authorization is rebuilt by the ordinary continuation path. The
            # immutable event content keeps the host admission's payload digest.
            for name in fields:
                if name in snapshot and name != "allow_gateway_control":
                    setattr(event, name, snapshot[name])
            if snapshot.get("timestamp"):
                event.timestamp = datetime.fromisoformat(snapshot["timestamp"])
            for name, value in snapshot.get("source_labels", {}).items():
                setattr(event.source, name, value)
            history = snapshot.get("history", {})
            snapshot["history"] = dict(self.opening_history(inbound, history.get("ids", [])),
                                       injected=history.get("injected", False))
        else:
            snapshot = {name: getattr(event, name) for name in fields if hasattr(event, name)}
            snapshot["source_labels"] = {name: getattr(event.source, name) for name in
                                         ("chat_name", "user_name", "chat_topic") if hasattr(event.source, name)}
            snapshot["history"] = dict(self.opening_history(inbound, self.delivered.get(key, set())),
                                       injected=bool(history_text))
            snapshot["nonce"] = str(getattr(event.source, "message_id", ""))
            if hasattr(event, "timestamp"):
                snapshot["timestamp"] = event.timestamp.isoformat()
        row = self.held_row(key, mid)
        if row:
            self.store.snapshot_held_batch(self.account, key, mid,
                                           self.adapter._batch_message_ids(inbound), snapshot)
        return snapshot.get("history", {"injected": False, "ids": [], "tokens": 0})

    def seen_ids(self, inbound):
        return self.confirmed.get(self.key(inbound), set()) if self.enabled(inbound) else set()

    def dispatched(self, event):
        """Only the actual host handoff may finish a durable dispatch."""
        if not self.journal_ready():
            return
        key = self.gateway._session_key_for_source(event.source)
        self.store.finish_held_batch(self.account, key, set(self.event_ids(event)))
        if key in self.drain_waiting:
            self.wake_drain(key)

    def replaying_batch(self, inbound):
        if not self.journal_ready():
            return False
        mid = self.adapter._inbound_trigger_id(inbound.raw_message)
        return bool(self.replays.get(mid, {}).get("raw_message", {}).get("clawchat_raw", {}).get("clawchat_group_batch"))

    async def reset_notes(self, inbound):
        async with self.locks.setdefault(self.key(inbound), asyncio.Lock()):
            return await self.adapter._run_sediment_turn(chat_id=inbound.chat_id,
                chat_type=inbound.chat_type, sender_id=inbound.sender_id, reason="reset")

    def event_ids(self, event):
        raw = getattr(event, "raw_message", {}) or {}
        inner = raw.get("clawchat_raw", {})
        if inner.get("clawchat_group_batch"):
            return [self.adapter._inbound_trigger_id(frame) for frame in inner.get("messages", [])]
        return [self.adapter._inbound_trigger_id(inner)]

    def completed(self, event, outcome):
        if not self.journal_ready():
            return
        if getattr(outcome, "name", str(outcome)).upper() != "SUCCESS":
            return
        # SUCCESS means confirmed final delivery OR the host's explicit no-reply
        # terminal. Process output and unconfirmed outbound ledger rows never count.
        key = self.gateway._session_key_for_source(event.source)
        for mid in self.event_ids(event):
            row = self.held_row(key, mid)
            if row:
                self.store.update_held_payload(self.account, key, mid, {"terminal": True})

    def queue_pending(self, inbound, state, why, cut=None, boundary=None):
        key = self.key(inbound)
        if state.get("pending") and why != "renewed":
            return state
        cut_seq = self.position(cut) if cut else None
        entries = self.interval(inbound, state, boundary=why != "renewed", cut=cut, cut_seq=cut_seq)
        settings = self.settings(inbound)
        fold = entries if why == "renewed" else plan_compaction(
            entries, datetime.now(timezone.utc), settings.recent_window,
            settings.keep_recent_turns, settings.history_budget_tokens // 2, False)[0]
        if why != "budget" and (sum(e.role == "other" for e in fold) < 2
                                and not any(e.role == "owner" for e in fold) and not state.get("pending")):
            if boundary:
                return self.cas(key, state, boundary=boundary, inject_next=1)
            return state
        pending = {"why": why, "cut_id": cut if why == "renewed" else (fold[-1].id if fold else None),
                   "cut_seq": cut_seq if why == "renewed" else (self.position(fold[-1].id) if fold else None),
                   "boundary": boundary or state.get("boundary"), "stage": "summary",
                   "host_session": state.get("host_session")}
        return self.cas(key, state, pending=pending, **({"boundary": boundary} if boundary else {}))

    def observe_reset(self, old, new, operation):
        if self.closed or operation:
            return
        for key, candidate in list(self.candidates.items()):
            if (old and candidate["old"] != old) or not new or candidate["old"] == new:
                continue
            try:
                # Even a hook cannot prove that a failed persistence operation committed.
                if self.read_route(key) != new:
                    continue
                self.confirm_renewed(key, candidate, new)
            except Exception:
                logger.warning("clawchat history user reset confirmation unavailable conv=%s", key)

    def confirm_renewed(self, key, candidate, new):
        state = self.state(key)
        state = self.cas(key, state, host_session=new)
        self.queue_pending(candidate["inbound"], state, "renewed",
                           cut=candidate["mid"], boundary=candidate["mid"])
        self.candidates.pop(key, None)

    async def prepare(self, inbound):
        try:
            return await self.before(inbound)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.available = False
            logger.warning("clawchat history preparation unavailable; retaining original session and host backstop")
            return ""

    async def before(self, inbound):
        if inbound.is_internal_synthetic or not self.enabled(inbound):
            return ""
        key = self.key(inbound)
        async with self.locks.setdefault(key, asyncio.Lock()):
            sid = self.gateway.session_store.peek_session_id(key)
            state = self.state(key)
            candidate = self.candidates.get(key)
            from clawchat_gateway.adapter import _slash_confirm_choice
            choice = _slash_confirm_choice(inbound.text) if candidate else None
            host_choices = getattr(self.gateway, "_slash_confirm_text_choices", None)
            if candidate and choice is None and callable(host_choices):
                choice = host_choices().get(inbound.text.strip().lstrip("!/").lower())
            if candidate and sid and sid != candidate["old"] and self.read_route(key) == sid:
                self.confirm_renewed(key, candidate, sid)
                state = self.state(key)
            elif candidate and (choice == "cancel" or (
                    choice not in ("once", "always") and not inbound.text.lstrip().startswith("/"))):
                # Confirmation text such as "approve once" still belongs to the pending
                # command. Only cancellation or a normal subsequent turn abandons it.
                self.candidates.pop(key, None)
            pending = state.get("pending") or {}
            if (sid and pending.get("stage") == "reset" and pending.get("host_session")
                    and pending["host_session"] != sid and self.read_route(key) == sid):
                # A late reset may commit after a released input advanced gen.
                # Verify the durable route again and reconcile the completed cut
                # without overwriting that input's injection confirmation.
                state = self.cas(key, state, host_session=sid, inject_next=int(state.get("injected_for") != sid),
                                 pending=None, failures=0, retry_after_msgs=0)
            if sid and state.get("host_session") != sid:
                external = bool(state.get("host_session"))
                state = self.cas(key, state, host_session=sid)
                if external:
                    state = self.queue_pending(inbound, state, "external")
            if inbound.text.lstrip().split(" ", 1)[0].lower() in ("/new", "/reset"):
                self.candidates[key] = {"old": sid, "mid": self.adapter._inbound_trigger_id(inbound.raw_message),
                                        "inbound": inbound}
                return ""
            if choice in ("once", "always", "cancel"):
                return ""
        state = self.state(key)
        if state.get("pending", {}) and state["pending"]["why"] == "renewed":
            if not state.get("retry_after_msgs"):
                await self.start(inbound, renewed=True)
        async with self.locks.setdefault(key, asyncio.Lock()):
            state = self.state(key)
            sid = self.gateway.session_store.peek_session_id(key)
            ids = self.adapter._batch_message_ids(inbound)
            entries = self.interval(inbound, state)
            # Capture the prior *original* timestamp before this turn; arrivals
            # after the trigger cannot erase an idle interval.
            all_entries = self.entries(inbound)
            activity = self.activity_entries(inbound)
            activity_start = next((i for i, e in enumerate(activity) if e.id in ids), len(activity))
            first_activity = next((e.ts for e in activity if e.id in ids), None)
            first = next((i for i, e in enumerate(all_entries) if e.id in ids), len(all_entries))
            previous = activity[activity_start - 1] if activity_start else None
            self.turns[self.adapter._inbound_trigger_id(inbound.raw_message)] = (
                inbound, previous.ts if previous else None, first_activity)
            needs = state.get("inject_next") or not sid or sid != state.get("injected_for")
            if not needs:
                return ""
            summary = None
            if state.get("summary_ref"):
                loaded = read_summary(self.directory(key), state["summary_ref"])
                if not loaded:
                    raise RuntimeError("published summary unavailable")
                summary = quoted_history_summary(loaded[1])
                if summary is None:
                    raise RuntimeError("published summary invalid")
            settings = self.settings(inbound)
            prior_ids = {e.id for e in all_entries[:first]}
            window = recent_window_for_injection([render_entry(e) for e in entries if e.id in prior_ids and e.id not in ids],
                settings.history_budget_tokens, has_summary=bool(summary), now=datetime.now(timezone.utc),
                recent_window=settings.recent_window, keep_recent_turns=settings.keep_recent_turns)
            text = ("## ClawChat Conversation Summary\n" + UNTRUSTED_HISTORY + "\n" + summary + "\n\n") if summary else ""
            text += "## Recent messages\n" + UNTRUSTED_HISTORY + "\n" + "\n".join(rendered_entry_line(e) for e in window)
            self.delivered[key] = {e.id for e in window}
            return text

    def opening_history(self, inbound, prior_ids):
        ids = set(prior_ids) | self.adapter._batch_message_ids(inbound)
        return {"ids": sorted(ids), "tokens": sum(estimate_entry_tokens(e)
                for e in self.entries(inbound) if e.id in ids)}

    def bind_injection(self, inbound, event, text, metadata=None):
        if inbound.is_internal_synthetic or not self.enabled(inbound):
            return
        key = self.key(inbound)
        nonce = str(getattr(event.source, "message_id", ""))
        while len(self.injections) >= 4096:
            self.injections.pop(next(iter(self.injections)))
        metadata = metadata or dict(self.opening_history(inbound, self.delivered.get(key, set())),
                                    injected=bool(text))
        self.injections[nonce] = {"key": key, "text": metadata["injected"], "state": self.state(key),
                                  "chat": inbound.chat_id, "group": inbound.chat_type == "group",
                                  "ids": set(metadata["ids"]) | self.adapter._batch_message_ids(inbound),
                                  "history": metadata["tokens"]}

    def observe_request(self, nonce, key, sid, tokens):
        if self.closed or not sid:
            return
        candidate = self.injections.pop(nonce, None)
        if not candidate:
            # Only the first observed request confirms injection; later usage
            # still belongs to the exact canonical key bound to this nonce.
            key = self.usage.get(("nonce", nonce))
            if key and tokens is not None:
                self.usage[key] = (sid, tokens)
            return
        key = candidate["key"]
        logger.info("clawchat history request conv=%s prompt_tokens=%s injected=%s", key, tokens, candidate["text"])
        self.usage[("nonce", nonce)] = key
        while len(self.usage) > 8192:
            self.usage.pop(next(iter(self.usage)))
        if tokens is not None:
            self.usage[key] = (sid, tokens)
        try:
            state = candidate["state"]
            current_sid = self.gateway.session_store.peek_session_id(key)
            if current_sid and current_sid != sid:
                return
            if candidate["text"]:
                self.cas(key, state, host_session=sid, injected_for=sid, inject_next=0,
                         open_context=tokens, open_history=candidate["history"])
                self.confirmed[key] = candidate["ids"]
                if candidate["group"]:
                    self.adapter._remember_delivered_group_ids(candidate["chat"], candidate["ids"])
                    self.store.mark_group_shared_session(self.account, candidate["chat"])
            elif state.get("host_session") != sid:
                self.cas(key, state, inject_next=1)
        except Superseded:
            pass
        except Exception:
            logger.warning("clawchat history injection confirmation failed conv=%s", key)

    def _event_budget_enabled(self, event):
        source = getattr(event, "source", None)
        chat = str(getattr(source, "chat_id", "") or "")
        raw = getattr(event, "raw_message", {}) or {}
        group = raw.get("clawchat_chat_type") == "group" or getattr(source, "chat_type", None) == "group"
        return bool(self.available and not self.closed and chat and effective_history_settings(
            self.adapter._clawchat_config, chat, group).budget_compaction)

    def finish_failed_turn(self, event):
        """Discard turn tracking without advancing backoff or starting maintenance."""
        mid = self.adapter._turn_trigger_message_id(event)
        turn = self.turns.pop(mid, None)
        return self.enabled(turn[0]) if turn else self._event_budget_enabled(event)

    def after_turn(self, event):
        mid = self.adapter._turn_trigger_message_id(event)
        turn = self.turns.pop(mid, None)
        if not turn:
            return self._event_budget_enabled(event)
        if not self.enabled(turn[0]):
            return False
        inbound, previous, *started = turn
        key = self.key(inbound)
        state = self.state(key)
        if state.get("retry_after_msgs", 0):
            remaining = state["retry_after_msgs"] - 1
            state = self.cas(key, state, retry_after_msgs=remaining)
            if remaining:
                return True
        if not state.get("pending"):
            entries = self.interval(inbound, state)
            usage = self.usage.get(key)
            last = usage[1] if usage and usage[0] == state.get("host_session") else None
            tokens = history_tokens(state.get("open_history"), state.get("open_context"), last)
            if tokens is None:
                tokens = sum(map(estimate_entry_tokens, entries))
            settings = self.settings(inbound)
            if tokens > settings.history_budget_tokens:
                state = self.queue_pending(inbound, state, "budget")
            elif settings.idle_reset and previous:
                stamp = _entry_time(previous)
                current = next((e for e in self.activity_entries(inbound) if e.id == mid), None)
                now = _entry_time(started[0]) if started and started[0] else (
                    _entry_time(current.ts) if current else None)
                if stamp and now and now - stamp > settings.idle_reset:
                    state = self.queue_pending(inbound, state, "idle")
        if state.get("pending"):
            self.schedule(inbound)
        return True

    def schedule(self, inbound):
        key = self.key(inbound)
        if key in self.tasks or key in self.reset_tasks or self.closed:
            return
        task = asyncio.create_task(self.start(inbound))
        self.tasks[key] = task
        def finished(done):
            if self.tasks.get(key) is done:
                self.tasks.pop(key, None)
            if not done.cancelled() and done.exception():
                logger.warning("clawchat history maintenance worker failed conv=%s", key)
        task.add_done_callback(finished)

    async def idle(self, inbound):
        key = self.key(inbound)
        chat = inbound.chat_id
        a, g = self.adapter, self.gateway
        if a._chat_session_active(chat) or chat in a._group_dispatching or chat in a._sediment_chats:
            return False
        for name in ("_pending_messages", "_text_debounce"):
            buffer = getattr(a, name, None)
            if not isinstance(buffer, dict):
                return False
            if any(chat in str(k).split(":") for k in buffer):
                return False
        if any(chat == k.split(":", 1)[0] and values
               for k, values in a._group_message_coalescer._pending.items()):
            return False
        if not callable(getattr(g, "_is_session_running", None)) or g._is_session_running(key):
            return False
        queues = getattr(g, "_queued_events", None)
        startup = getattr(g, "_startup_restore_queue", None)
        if not callable(getattr(queues, "get", None)) or not isinstance(startup, list):
            return False
        if queues.get(key) or any(getattr(getattr(e, "source", None), "chat_id", None) == chat for e in startup):
            return False
        authority = getattr(g, "session_authority", None)
        if authority is not None:
            pending = getattr(authority, "_pending_rows", None)
            if not callable(pending):
                return False
            from types import SimpleNamespace
            sid = g.session_store.peek_session_id(key)
            if sid and pending(SimpleNamespace(session_id=sid)):
                return False
        return True

    async def wait_idle(self, inbound, token):
        while self.blocked.get(self.key(inbound)) is token and not self.closed:
            if await self.idle(inbound):
                return True
            await asyncio.sleep(POLL_SECONDS)
        return False

    async def watchdog(self, key, token):
        await asyncio.sleep(MAINTENANCE_SECONDS)
        if self.blocked.get(key) is token and not self.closed:
            self.blocked.pop(key, None)
            logger.warning("clawchat history maintenance deadline conv=%s; releasing inbound independently", key)
            drain = asyncio.create_task(self.release(key))
            self.drains.add(drain)
            drain.add_done_callback(self.drains.discard)

    async def start(self, inbound, renewed=False):
        key = self.key(inbound)
        token = object()
        if self.closed or key in self.blocked or key in self.reset_tasks:
            return
        self.blocked[key] = token
        epoch = self.epoch
        watchdog = asyncio.create_task(self.watchdog(key, token))
        self.watchdogs[key] = watchdog
        started = time.monotonic()
        self.progress[key] = (token, 0)
        blocks = 0
        result = "deferred"
        before = self.usage.get(key, (None, None))[1]
        why = "unknown"
        running = asyncio.current_task()
        self.running_tasks.add(running)
        try:
            why = (self.state(key).get("pending") or {}).get("why", "unknown")
            async with asyncio.timeout(MAINTENANCE_SECONDS):
                async with self.locks.setdefault(key, asyncio.Lock()):
                    if self.blocked.get(key) is not token or self.closed:
                        return
                    if not renewed and not await self.wait_idle(inbound, token):
                        return
                    blocks, result = await self.maintain(inbound, token)
        except PermissionError:
            self.available = False
            result = "llm-unavailable"
            logger.warning("clawchat budget unavailable: summary permission denied; host backstop retained")
        except Superseded:
            result = "superseded"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = ("reset-awaiting" if key in self.reset_tasks else
                      "renewed-incomplete" if why == "renewed" and isinstance(exc, TimeoutError) else "failed")
            if epoch == self.epoch and not self.closed:
                state = self.state(key)
                if state.get("pending") and key not in self.reset_tasks:
                    try:
                        failures = state.get("failures", 0) + 1
                        self.cas(key, state, failures=failures, retry_after_msgs=min(8, 2 ** min(failures - 1, 3)))
                    except Superseded:
                        pass
            logger.warning("clawchat history maintenance failed conv=%s; retaining session and pending", key)
        finally:
            if self.blocked.get(key) is token:
                self.blocked.pop(key, None)
            if self.watchdogs.get(key) is watchdog:
                self.watchdogs.pop(key, None)
            watchdog.cancel()
            if self.progress.get(key, (None,))[0] is token:
                blocks = self.progress.pop(key)[1]
            logger.info("clawchat history maintenance conv=%s why=%s result=%s blocks=%d elapsed=%.3f "
                        "prompt_before=%s prompt_after=%s", key, why, result, blocks,
                        time.monotonic() - started, before, self.usage.get(key, (None, None))[1])
            try:
                if epoch == self.epoch and not self.closed:
                    await self.release(key)
            finally:
                self.running_tasks.discard(running)

    async def maintain(self, inbound, token):
        key = self.key(inbound)
        state = self.state(key)
        pending = state.get("pending")
        if self.blocked.get(key) is not token or self.closed:
            return 0, "deadline"
        if not pending:
            return 0, "empty"
        renewed = pending["why"] == "renewed"
        blocks = split_blocks([render_entry(e) for e in self.interval(inbound, state, cut=pending["cut_id"], cut_seq=pending.get("cut_seq"), boundary=not renewed)]) if pending["cut_id"] else []
        count = 0
        previous_ref = pending.get("previous_ref") or state.get("summary_ref")
        for block in blocks if renewed else blocks[:3]:
            if self.blocked.get(key) is not token or self.closed:
                return count, "deadline"
            old = read_summary(self.directory(key), state["summary_ref"]) if state.get("summary_ref") else None
            if state.get("summary_ref") and not old:
                raise RuntimeError("published summary missing")
            reply = await self.llm.acomplete(rendered_summary_messages(old[1] if old else None, block),
                                             timeout=120, max_tokens=4000)
            if self.blocked.get(key) is not token or self.closed:
                return count, "deadline"
            summary = safe_history_summary(getattr(reply, "text", None))
            if summary is None:
                raise ValueError("invalid rolling summary")
            ref = publish_summary(self.directory(key), summary, through=block[-1].id,
                                  boundary=state.get("boundary"), as_of=datetime.now(timezone.utc))
            previous_ref = state.get("summary_ref")
            pending = dict(pending, previous_ref=previous_ref)
            state = self.cas(key, state, summary_ref=ref, summary_through=block[-1].id, pending=pending)
            count += 1
            self.progress[key] = (token, count)
        if len(blocks) > count:
            return count, "partial"
        pending = dict(pending, stage="reset")
        state = self.cas(key, state, pending=pending)
        if renewed:
            self.cas(key, state, inject_next=1, pending=None, failures=0, retry_after_msgs=0)
            return count, "renewed"
        if self.adapter._clawchat_config.sediment_on_compact and pending.get("notes") != "done":
            try:
                await self.adapter._run_sediment_turn(chat_id=inbound.chat_id, chat_type=inbound.chat_type,
                                                     sender_id=inbound.sender_id, reason="compact")
            except Exception:
                logger.warning("clawchat history notes failed conv=%s; continuing renewal", key)
            if self.blocked.get(key) is not token or self.closed:
                return count, "deadline"
            pending = dict(pending, notes="done")
            state = self.cas(key, state, pending=pending)
        if not await self.wait_idle(inbound, token):
            return count, "busy"
        # The pre-reset durable ID must still be the target summarized above.
        old = self.read_route(key)
        if not old or old != state.get("host_session"):
            raise RuntimeError("reset target changed")
        task = asyncio.create_task(self.reset(inbound, state, token, old))
        self.reset_tasks[key] = task
        try:
            done, _ = await asyncio.wait({task}, timeout=RESET_WARNING_SECONDS)
            if not done:
                logger.warning("clawchat history reset exceeds two minutes conv=%s", key)
            success = await asyncio.shield(task)
        finally:
            # A deadline must not cancel or forget an in-progress reset.
            if task.done() and self.reset_tasks.get(key) is task:
                self.reset_tasks.pop(key, None)
        if success:
            cleanup_summaries(self.directory(key), {r for r in (previous_ref, self.state(key).get("summary_ref")) if r})
        return count, "reset" if success else "reset-pending"

    async def reset(self, inbound, state, token, old):
        key = self.key(inbound)
        epoch = self.epoch
        operation = (token, inbound.chat_id)
        context = _internal_reset.set(operation)
        try:
            from clawchat_gateway.adapter import MessageEvent, MessageType
            event = MessageEvent(text="/new", source=self.source(inbound), message_type=MessageType.TEXT)
            try:
                await self.gateway._handle_reset_command(event)
            except Exception:
                logger.warning("clawchat history reset raised conv=%s; checking durable route", key)
            # Always reread authoritative persistence, including on exceptions.
            actual = self.read_route(key)
            if self.closed or epoch != self.epoch:
                return False
            if not actual or actual == old:
                logger.warning("clawchat history reset did not commit conv=%s; host backstop retained", key)
                failures = state.get("failures", 0) + 1
                self.cas(key, state, failures=failures, retry_after_msgs=min(8, 2 ** min(failures - 1, 3)))
                return False
            self.cas(key, state, host_session=actual, inject_next=1, pending=None,
                     failures=0, retry_after_msgs=0)
            return True
        except Superseded:
            return False
        finally:
            _internal_reset.reset(context)
            current = asyncio.current_task()
            if self.reset_tasks.get(key) is current:
                self.reset_tasks.pop(key, None)

    async def close(self):
        self.closed = True
        self.epoch += 1
        tasks = (set(self.tasks.values()) | set(self.watchdogs.values())
                 | set(self.reset_tasks.values()) | self.drains | self.running_tasks)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.blocked.clear()
        self.tasks.clear()
        self.reset_tasks.clear()
        self.injections.clear()
