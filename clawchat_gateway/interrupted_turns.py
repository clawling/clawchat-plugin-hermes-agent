"""At-most-once restart notices for journaled host handoffs; never replay work."""
from __future__ import annotations

import asyncio
import logging
import math
import time

from clawchat_gateway.owner_language import resolve_owner_language
from clawchat_gateway.protocol import build_message_reply_event, new_message_id

logger = logging.getLogger(__name__)
# Import happens with history maintenance, before the first accepted handoff.
# Reconnects and replacement maintenance instances retain this process boundary.
PROCESS_STARTED_AT = time.time()
NOTICE_DELAY_SECONDS = 120
NOTICE_WINDOW_SECONDS = 24 * 60 * 60
NOTICE_TEXT = {
    "en": "The service restarted while processing your message and may not have finished. If you still need help, please send it again.",
    "es": "El servicio se reinició mientras procesaba tu mensaje y puede que no haya terminado. Si aún necesitas ayuda, envíalo de nuevo.",
    "ja": "メッセージの処理中にサービスが再起動し、処理が完了していない可能性があります。まだ必要な場合は、もう一度送信してください。",
    "ko": "메시지를 처리하는 중 서비스가 다시 시작되어 처리가 완료되지 않았을 수 있습니다. 아직 도움이 필요하시면 다시 보내 주세요.",
    "zh": "刚才处理你的消息时服务重启了，可能没有完成。如果还需要，请重新发送。",
    "zh_Hant": "剛才處理你的訊息時服務重新啟動了，可能沒有完成。如果還需要，請重新傳送。",
}


def notice_candidate(row, *, started_at, now):
    payload = row["payload"]
    handoff = payload.get("handoff_at")
    frame = payload.get("frame") or {}
    return (row["state"] == "dispatched"
            and isinstance(handoff, (int, float)) and not isinstance(handoff, bool)
            and math.isfinite(handoff) and now - NOTICE_WINDOW_SECONDS <= handoff < started_at
            and handoff <= now and "terminal" not in payload and not payload.get("notified")
            and not payload.get("control") and not payload.get("local_consumed")
            and not payload.get("internal_synthetic") and not frame.get("synthetic")
            and bool(frame.get("chat_id")) and frame.get("chat_type") in {"direct", "group"})


class InterruptedTurnNotices:
    def __init__(self, history, *, delay=NOTICE_DELAY_SECONDS, started_at=PROCESS_STARTED_AT):
        self.history = history
        self.delay = delay
        self.started_at = started_at
        self.task = None

    def current(self, epoch):
        history = self.history
        return (history.epoch == epoch and history.journal_ready()
                and history.adapter._history is history
                and history.adapter._clawchat_config.interrupted_turn_notice)

    def schedule(self):
        history = self.history
        epoch = history.epoch
        if self.task is not None or not self.current(epoch):
            return
        self.task = asyncio.create_task(self.run(epoch))
        history.drains.add(self.task)
        self.task.add_done_callback(history.drains.discard)

    async def run(self, epoch):
        try:
            await asyncio.sleep(self.delay)
            if self.current(epoch):
                await self.check(epoch)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("clawchat interrupted turn check unavailable; retaining journal")

    async def check(self, epoch=None):
        history = self.history
        epoch = history.epoch if epoch is None else epoch
        if not self.current(epoch):
            return
        rows = history.store.list_held(history.account, states=("dispatched",))
        keys = dict.fromkeys(row["conv_key"] for row in rows
                             if notice_candidate(row, started_at=self.started_at, now=time.time()))
        for key in keys:
            if not self.current(epoch):
                return
            row = history.store.claim_interrupted_turn_notice(
                history.account, key, started_at=self.started_at, now=time.time())
            if row is None:
                continue
            # The durable marker precedes every send. A crash/failed send may
            # lose a notice; retrying must never repeat it or the original work.
            adapter = history.adapter
            frame_in = row["payload"]["frame"]
            chat_id = frame_in["chat_id"]
            text = NOTICE_TEXT[resolve_owner_language(adapter._owner_locale())]
            mid = new_message_id()
            frame = build_message_reply_event(
                chat_id=chat_id, chat_type=frame_in["chat_type"], message_id=mid,
                fragments=[{"kind": "text", "text": text}],
                reply_to_message_id=row["message_id"], include_message_id=True)
            # Local ledger metadata is never sent on the wire. Both history
            # tools and context/summary reads exclude this administrative notice.
            raw = dict(frame, clawchat_interrupted_turn_notice=True)
            claimed = adapter._claim_outbound_message(
                event_type="message.reply", trace_id=frame.get("trace_id") or frame.get("id"),
                chat_id=chat_id, message_id=mid, text=text, raw=raw,
                history_activity={"kind": "notice"})
            if claimed is not True or not self.current(epoch):
                continue
            if not await adapter._connection.send_frame(frame, wait_for_ack=True):
                logger.warning("clawchat interrupted turn notice delivery unconfirmed")
            if not self.current(epoch):
                return
