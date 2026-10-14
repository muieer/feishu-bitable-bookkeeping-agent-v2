import hashlib
import html
import re

from lark_channel import FeishuChannel

from .audit import Audit
from .domain import EntryResult, TaskReport
from .journal import Journal


def escape(value: str) -> str:
    value = html.escape(value)
    return re.sub(r"([\\`*_\[\]{}()#])", r"\\\1", value)


def card(title: str, markdown: str, color: str = "blue") -> dict:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
        "body": {"elements": [{"tag": "markdown", "content": markdown}]},
    }


def expense_line(entry: EntryResult) -> str:
    item = entry.expense
    path = "｜".join(escape(level) for level in item.category_path if level)
    return f"{item.expense_date:%Y/%m/%d}｜{path}｜{escape(item.description)}｜{item.amount:.2f} 元"


def result_cards(report: TaskReport, page_size: int = 4000) -> list[dict]:
    blocks = []
    if report.notice:
        blocks.append(escape(report.notice))
    if report.no_consumption and not report.entries and not report.skipped:
        blocks.append("此次未包含消费记录。")
    grouped = {
        "success": "已记账消费记录",
        "failed": "以下消费记录未入账",
        "uncertain": "以下消费记录待核对",
    }
    for status, title in grouped.items():
        entries = [entry for entry in report.entries if entry.outcome.status == status]
        for index, entry in enumerate(entries, start=1):
            block = f"**{title}**\n\n{index}. {expense_line(entry)}"
            if status != "success":
                block += (
                    f"\n原文：{escape(entry.expense.source_text)}"
                    f"\n原因：{escape(entry.outcome.reason)}"
                )
            blocks.append(block)
    for index, item in enumerate(report.skipped, start=1):
        blocks.append(
            f"**以下消费记录未入账**\n\n{index}. {escape(item.source_text)}"
            f"\n原因：{escape(item.reason)}"
        )
    if any(entry.outcome.status == "uncertain" for entry in report.entries):
        blocks.append("待核对消费不会自动再次提交，请检查账本后再决定后续处理。")
    pages, current = [], ""
    for block in blocks:
        if current and len(current) + len(block) > page_size:
            pages.append(current)
            current = ""
        current += ("\n\n" if current else "") + block
    pages.append(current or "此次处理未能完成，请稍后查看处理结果。")
    color = (
        "orange"
        if report.notice
        or report.skipped
        or any(entry.outcome.status != "success" for entry in report.entries)
        else "green"
    )
    return [
        card("记账结果" + (f"（{i}/{len(pages)}）" if len(pages) > 1 else ""), body, color)
        for i, body in enumerate(pages, start=1)
    ]


class CardReporter:
    def __init__(self, channel: FeishuChannel, journal: Journal, audit: Audit):
        self.channel, self.journal, self.audit = channel, journal, audit

    async def _send_or_update(self, message_id: str, payload: dict, page: int = 0) -> bool:
        message = self.journal.message(message_id)
        if page == 0 and message.card_id:
            result = await self.channel.update_card(message.card_id, payload)
        else:
            uuid = hashlib.sha256(f"bookkeeping:{message_id}:{page}".encode()).hexdigest()[:32]
            result = await self.channel.send(
                message.chat_id, {"card": payload}, {"reply_to": message_id, "uuid": uuid}
            )
            if page == 0 and result.success and result.message_id:
                self.journal.set_card(message_id, result.message_id)
        self.audit.event(
            "card_result",
            success=result.success,
            page=page,
            card_id=result.message_id,
            error=str(result.error) if result.error else None,
        )
        return bool(result.success)

    async def progress(self, message_id: str, text: str) -> None:
        entries = self.journal.entries(message_id)
        successes = sum(entry.outcome.status == "success" for entry in entries)
        if entries:
            text += f"\n\n已确认记账 {successes} 笔，正在处理其他消费。"
        await self._send_or_update(message_id, card("记账处理中", text))

    async def final(self, message_id: str, report: TaskReport) -> bool:
        for page, payload in enumerate(result_cards(report)):
            if not await self._send_or_update(message_id, payload, page):
                return False
        return True
