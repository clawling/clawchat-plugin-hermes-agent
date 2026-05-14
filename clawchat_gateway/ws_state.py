from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReconnectSnapshot:
    attempt: int
    reconnect_count: int


class ReconnectTracker:
    def __init__(self) -> None:
        self._attempt = 0
        self._reconnect_count = 0
        self._has_connected_once = False

    def next_connect(self) -> tuple[int, int]:
        self._attempt += 1
        if self._has_connected_once:
            self._reconnect_count += 1
        self._has_connected_once = True
        return self._attempt, self._reconnect_count

    def mark_reconnect_scheduled(self) -> None:
        self._has_connected_once = True

    def reset_reconnect_count(self) -> None:
        self._reconnect_count = 0

    def snapshot(self) -> ReconnectSnapshot:
        return ReconnectSnapshot(self._attempt, self._reconnect_count)
