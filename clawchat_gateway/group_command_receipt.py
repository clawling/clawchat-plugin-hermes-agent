"""The owner's group command: its reply goes to the owner's direct chat.

When the agent's owner runs a command in a group (``/new``, ``/model``,
``@agent /compress`` …), Hermes answers into the group, because the group is
the event's source and the source is what decides which session the command
acts on. The receipt is for the owner alone, so the adapter delivers it to the
owner's direct chat instead, headed with the group and the command, and only
reacts ✅ on the command message in the group.

The reply is recognised by turn, not by time: the adapter sets a context
variable around handing the command to Hermes. Hermes runs the command in a
task it creates from that call (or inline, when the session is busy), and a
task copies the caller's context, so every send the command makes carries the
marker. Any other event Hermes starts processing from a context that copied
the marker (a queued follow-up drained after a busy-session command, the
sediment turn before ``/new``) drops it for its own task; and the marker is
closed when the command's own processing completes, unless it failed — the
error notice Hermes sends next is the command's reply too.
"""

from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass

# Commands whose output is an agent turn — work done in the group, not a
# receipt about the session — keep answering in the group.
AGENT_TURN_COMMANDS = frozenset(
    {
        "plan",
        "learn",
        "init",
        "blueprint",
        "btw",
        "background",
        "bg",
        "queue",
        "q",
        "steer",
        "moa",
        "goal",
        "retry",
    }
)

RECEIPT_REACTION = "✅"


@dataclass
class GroupCommandReceipt:
    group_id: str
    group_title: str
    command_message_id: str
    command_text: str
    closed: bool = False
    reacted: bool = False

    def header(self) -> str:
        name = self.group_title or self.group_id
        return f'From group "{name}": {self.command_text}'


_receipt: contextvars.ContextVar[GroupCommandReceipt | None] = contextvars.ContextVar(
    "clawchat_group_command_receipt",
    default=None,
)


@contextlib.contextmanager
def group_command_receipt(receipt: GroupCommandReceipt):
    token = _receipt.set(receipt)
    try:
        yield receipt
    finally:
        _receipt.reset(token)


def active_receipt(chat_id: str) -> GroupCommandReceipt | None:
    """The open receipt of the command whose turn this is, for *chat_id* only."""
    receipt = _receipt.get()
    if receipt is None or receipt.closed or receipt.group_id != chat_id:
        return None
    return receipt


def current_receipt() -> GroupCommandReceipt | None:
    return _receipt.get()


def detach_receipt() -> None:
    """Drop the marker for the current task's context only: a task copies its
    creator's context, so this never reaches the command's own task."""
    _receipt.set(None)
