"""Synthetic reasoning-turn builder for ``friend.request`` notify.signal events.

When the server fires a ``friend.request`` signal the adapter dispatches one
deduped synthetic inbound message so the agent can reason about the pending
request. The prompt text varies by the current "friend.add" permission policy
state: a deny policy yields a decline/inform prompt; ask or allow yields a
prompt that instructs the agent to review the request via the
``clawchat_list_friend_requests`` tool.

Mirrors the OpenClaw plugin's ``friend-request-turn.ts`` (``friendRequestPromptFor``
/ ``buildFriendRequestEnvelope``). Keep prompt wording semantically identical so
that the two adapters produce equivalent reasoning turns for the same state.
"""

from __future__ import annotations

import time

from clawchat_gateway.inbound import InboundMessage


def friend_request_prompt_for(state: str) -> str:
    """Return canned prompt text for a friend-request reasoning turn.

    - ``deny``  → inform the agent it should decline; no accept instruction.
    - ``ask`` / ``allow`` → instruct the agent to review the pending request.
    """
    if state == "deny":
        return (
            "A new friend request has arrived."
            " Your current friend-add policy is set to deny."
            " Do not add this contact."
            " You may inform the requester that you cannot add them at this time."
        )
    return (
        "A new friend request has arrived."
        " Please review the pending request by calling `clawchat_list_friend_requests`"
        " and decide whether to accept it."
    )


def build_friend_request_inbound(
    *,
    owner_user_id: str,
    owner_chat_id: str,
    state: str,
    entity_id: str,
) -> InboundMessage:
    """Build a synthetic inbound message that triggers one agent reasoning turn.

    The inbound targets the owner's direct conversation so the agent has context
    about who it is reasoning for. The ``raw_message`` carries ``"synthetic": True``
    so downstream guards can distinguish it from real protocol frames.
    """
    text = friend_request_prompt_for(state)
    now_ms = int(time.time() * 1000)
    return InboundMessage(
        chat_id=owner_chat_id,
        chat_type="direct",
        sender_id="clawchat-friend-request",
        sender_name="ClawChat",
        text=text,
        raw_message={
            "synthetic": True,
            "friend_request": True,
            "entity_id": entity_id,
            "trace_id": f"clawchat-hermes-friend-request-{entity_id}-{now_ms}",
            "owner_user_id": owner_user_id,
        },
    )
