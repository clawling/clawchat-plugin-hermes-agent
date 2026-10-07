"""Permission receipts tell the agent what happened, including the result.

A gated call answers ``21001`` (pending); the owner's verdict arrives later as
a ``permission_result`` system message, and only that receipt closes the loop.
Hermes used to:

* ignore the receipt's ``result`` object, so what an approved replay produced
  (an orchestration's ``conversation_id``, a connect ``code``, and — once the
  server returns read results — the data a single-use approved read fetched)
  never reached the agent, whose own call only got ``21001``;
* render ``approved_retry`` as "has been approved_retry. No further action was
  taken.", hiding the one outcome where the agent MUST call again.

Ported from the ClawChat app's local-agent handling: ``approved`` /
``auto_allowed`` = the server already did it, do not call again;
``approved_retry`` = nothing done yet, call the same tool once more; refusals
and failures = do not retry; an unknown outcome is never read as a refusal; a
receipt settled by a live allow window (``reason=window_allow``) gets no turn,
since the agent's own call already returned. ``result`` is passed through
generically (keys as the server sent them), bounded and marked as data.
"""

from __future__ import annotations

import json

import pytest

from clawchat_gateway import permission_result as pr


@pytest.fixture(autouse=True)
def _fresh_dedupe():
    pr._seen_request_ids.clear()
    yield
    pr._seen_request_ids.clear()


def _frame(outcome, *, request_id="prq_1", operation="group.manage", reason="owner_allowed", result=None):
    metadata = {
        "kind": "permission_result",
        "operation": operation,
        "outcome": outcome,
        "reason": reason,
        "request_id": request_id,
    }
    if result is not None:
        metadata["result"] = result
    return {
        "event": "message.send",
        "chat_id": "cnv_owner",
        "chat_type": "direct",
        "sender": {"id": "system"},
        "payload": {"metadata": metadata},
    }


def _text(frame):
    inbound = pr.handle_permission_result(frame)
    assert inbound is not None
    return inbound.text, inbound


def test_approved_says_done_and_do_not_call_again():
    text, _ = _text(_frame("approved"))
    assert "already carried it out" in text
    assert "Do not call that tool again" in text


def test_approved_retry_says_call_again_not_done():
    text, inbound = _text(_frame("approved_retry", operation="orchestrate.read"))
    assert "approved_retry" not in text.split("outcome:")[0]  # not "has been approved_retry"
    assert "has been approved_retry" not in text
    assert "Nothing has been done yet" in text
    assert "call the same tool again" in text
    assert "Do not call that tool again" not in text
    assert inbound.raw_message["outcome"] == "approved_retry"


def test_result_object_is_passed_through_generically():
    result = {"code": "ABC123", "qr_content": "clawchat://join?c=ABC123", "expires_at": 1700000000}
    text, inbound = _text(_frame("approved", operation="group.invite.read", result=result))
    assert inbound.raw_message["result"] == result
    assert json.dumps(result, ensure_ascii=False, sort_keys=True) in text
    assert "data" in text.lower()


def test_orchestration_result_reaches_the_agent():
    text, _ = _text(_frame("approved", operation="orchestrate.group.create",
                           result={"conversation_id": "cnv_new"}))
    assert '"conversation_id": "cnv_new"' in text


def test_a_huge_result_is_bounded():
    result = {"items": ["x" * 100] * 500}
    text, inbound = _text(_frame("approved", result=result))
    assert len(text) < pr.RESULT_TEXT_LIMIT + 2000
    assert "truncated" in text
    assert inbound.raw_message["result"] == result


def test_no_result_line_without_a_result():
    text, _ = _text(_frame("approved"))
    assert "result:" not in text


@pytest.mark.parametrize("outcome, phrase", [
    ("denied", "did not approve"),
    ("auto_denied", "standing permission rule refused"),
    ("expired", "Nobody answered"),
    ("failed", "did not go through"),
])
def test_refusals_and_failures_say_do_not_retry(outcome, phrase):
    text, _ = _text(_frame(outcome))
    assert phrase in text
    assert "Do not retry" in text


def test_failed_batch_that_stopped_partway():
    text, _ = _text(_frame("failed", result={"conversation_id": "cnv_g", "applied": 2, "total": 5}))
    assert "2 of 5" in text


def test_unknown_outcome_is_not_a_refusal():
    text, _ = _text(_frame("something_new"))
    assert "not known" in text
    assert "Do not retry" in text


def test_window_settled_receipt_gets_no_turn():
    assert pr.handle_permission_result(_frame("auto_allowed", reason="window_allow")) is None
    assert pr.handle_permission_result(_frame("auto_denied", reason="window_allow", request_id="prq_2")) is None


def test_auto_allowed_by_rule_is_done():
    text, _ = _text(_frame("auto_allowed", reason="policy_allow"))
    assert "already carried it out" in text


def test_redelivered_receipt_is_deduped():
    assert pr.handle_permission_result(_frame("approved")) is not None
    assert pr.handle_permission_result(_frame("approved")) is None


def test_pending_tool_result_states_the_receipt_rule():
    from clawchat_gateway import tools
    from clawchat_gateway.api_client import ClawChatApiError

    err = ClawChatApiError(kind="api", message="pending", code=21001, data={"request_id": "prq_9"})
    out = tools._api_error(err)
    assert "approved_retry" in out["message"]
    assert "do not call" in out["message"].lower()
