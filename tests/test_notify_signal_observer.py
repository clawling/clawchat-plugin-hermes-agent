from __future__ import annotations

from clawchat_gateway.notify_signal import NotifySignalObserver


def _frame(**over: object) -> dict[str, object]:
    payload = {
        "type": "friend.added",
        "entity_id": "usr_bob",
        "version": 1776162700000,
        "event_id": "ntf_01",
        "message_id": "notify:friend.added:usr_bob",
    }
    payload.update(over)
    return {"event": "notify.signal", "trace_id": "notif-1", "payload": payload}


def test_observes_a_well_formed_signal() -> None:
    observer = NotifySignalObserver()
    assert observer.observe(_frame()) == "observed"


def test_dedups_live_frame_and_reliable_inbox_replay_by_event_id() -> None:
    observer = NotifySignalObserver()
    assert observer.observe(_frame(event_id="ntf_dup")) == "observed"
    assert observer.observe(_frame(event_id="ntf_dup")) == "duplicate"


def test_missing_event_id_or_type_is_invalid_without_raising() -> None:
    observer = NotifySignalObserver()
    assert observer.observe(_frame(event_id="")) == "invalid"
    assert observer.observe({"event": "notify.signal", "payload": {}}) == "invalid"
    assert observer.observe({"event": "notify.signal"}) == "invalid"


def test_evicts_oldest_event_ids_past_max_seen() -> None:
    observer = NotifySignalObserver(max_seen=2)
    assert observer.observe(_frame(event_id="a")) == "observed"
    assert observer.observe(_frame(event_id="b")) == "observed"
    assert observer.observe(_frame(event_id="c")) == "observed"  # evicts "a"
    assert observer.observe(_frame(event_id="a")) == "observed"  # "a" forgotten
    assert observer.observe(_frame(event_id="c")) == "duplicate"  # "c" still tracked
