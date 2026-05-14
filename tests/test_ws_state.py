from clawchat_gateway.ws_state import ReconnectTracker


def test_reconnect_tracker_counts_attempts_and_reconnects():
    tracker = ReconnectTracker()

    assert tracker.next_connect() == (1, 0)
    tracker.mark_reconnect_scheduled()
    assert tracker.next_connect() == (2, 1)
    tracker.mark_reconnect_scheduled()
    assert tracker.next_connect() == (3, 2)


def test_reconnect_tracker_resets_after_stable_ready():
    tracker = ReconnectTracker()
    tracker.next_connect()
    tracker.mark_reconnect_scheduled()
    tracker.next_connect()

    tracker.reset_reconnect_count()

    assert tracker.snapshot().reconnect_count == 0
