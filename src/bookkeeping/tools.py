"""Ledger operations and final-result guards, independent of the agent framework."""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date

from .audit import Audit
from .cli import Ledger
from .domain import Decision, ExpenseProposal, SkippedExpense, WriteOutcome, locate_source
from .journal import Journal
from .knowledge import Knowledge

Progress = Callable[[str], Awaitable[None]]


class ToolInputError(ValueError):
    """An unsubmitted tool request the model may correct."""


@dataclass
class RunDeps:
    message_id: str
    message_text: str
    today: date
    journal: Journal
    ledger: Ledger
    knowledge: Knowledge
    audit: Audit
    progress: Progress
    rejected: dict[tuple[int, int], SkippedExpense] = field(default_factory=dict)
    request_limit: int = 12
    tool_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tool_retries: dict[str, int] = field(default_factory=dict)
    failed_tools: set[str] = field(default_factory=set)
    tool_calls: int = 0


async def query_history(deps: RunDeps, keyword: str, limit: int = 20) -> dict:
    """查询指定账本中消费详情包含关键词的有限历史样本，辅助分类；最多 50 条，不用于去重。"""
    await deps.progress("正在参考历史消费分类")
    deps.audit.event("tool_start", tool="query_history", keyword=keyword, limit=limit)
    try:
        result = await deps.ledger.query_history(keyword, limit)
    except Exception as error:
        deps.audit.event("tool_error", tool="query_history", error=repr(error))
        result = {"ok": False, "reason": "历史查询失败，请按已有知识判断"}
    deps.audit.event("tool_result", tool="query_history", result=result)
    return result


async def add_expenses(deps: RunDeps, expenses: list[ExpenseProposal]) -> list[dict]:
    """向唯一指定账本新增一笔或多笔已确认消费；逐笔校验、独立执行并返回真实状态。"""
    if len(expenses) > 100:
        raise ToolInputError("单次最多提交 100 笔，请自主拆成较小工具调用")
    await deps.progress("正在记录可确认的消费")
    results = []
    for proposal in expenses:
        deps.audit.event("tool_start", tool="add_expenses", proposal=proposal)
        try:
            expense, span = deps.knowledge.validate(proposal, deps.message_text, deps.today)
        except ValueError as error:
            # Do not feed raw pydantic errors (including input credentials) back to the LLM.
            from pydantic import ValidationError

            reason = (
                "必要字段不符合格式、金额或分类要求"
                if isinstance(error, ValidationError)
                else str(error)
            )
            result = {
                "status": "rejected",
                "source_text": proposal.source_text,
                "reason": deps.audit.redactor.text(reason),
                "submitted": False,
            }
            try:
                bad_span = locate_source(
                    deps.message_text, proposal.source_text, proposal.source_occurrence
                )
                deps.rejected[bad_span] = SkippedExpense(
                    source_text=proposal.source_text,
                    source_occurrence=proposal.source_occurrence,
                    reason=result["reason"],
                )
            except ValueError:
                pass
        else:
            cached = deps.journal.reserve(deps.message_id, span, expense)
            if cached:
                result = {
                    **cached.outcome.model_dump(),
                    "source_text": cached.expense.source_text,
                    "already_submitted": True,
                    "do_not_retry": True,
                }
            else:
                deps.rejected.pop(span, None)
                try:
                    outcome = await deps.ledger.create(expense)
                except asyncio.CancelledError:
                    deps.journal.complete_write(
                        deps.message_id,
                        span,
                        WriteOutcome(
                            status="uncertain",
                            reason="提交期间任务中断，无法确认写入结果，请核对账本",
                        ),
                    )
                    raise
                except Exception as error:
                    deps.audit.event("tool_error", tool="add_expenses", error=repr(error))
                    outcome = WriteOutcome(
                        status="uncertain", reason="写入已发起，但发生异常，请核对账本"
                    )
                deps.journal.complete_write(deps.message_id, span, outcome)
                result = {
                    **outcome.model_dump(),
                    "source_text": expense.source_text,
                    "do_not_retry": True,
                }
        deps.audit.event("tool_result", tool="add_expenses", result=result)
        results.append(result)
    await deps.progress("正在整理记账结果")
    return results


def validate_decision(deps: RunDeps, decision: Decision) -> Decision:
    if decision.no_consumption and (decision.skipped or deps.journal.entries(deps.message_id)):
        raise ValueError("已经识别出消费时，no_consumption 必须为 false")
    skipped_spans = set()
    for skipped in decision.skipped:
        try:
            span = locate_source(deps.message_text, skipped.source_text, skipped.source_occurrence)
        except ValueError as error:
            raise ValueError(str(error)) from error
        if deps.journal.overlaps(deps.message_id, span):
            raise ValueError("已提交的消费不得列入 skipped，程序会按工具真实结果反馈")
        if span in skipped_spans:
            raise ValueError("同一笔未入账消费不要重复列出")
        skipped_spans.add(span)
        if not skipped.reason.strip() or re.search(r"已记账|已入账|写入成功", skipped.reason):
            raise ValueError("未入账原因必须具体，不能宣称没有提交的消费已记账")
    if (
        not decision.no_consumption
        and not decision.skipped
        and not deps.journal.entries(deps.message_id)
    ):
        raise ValueError("请处理有效消费、说明不能入账的消费，或明确此次不含消费")
    return decision
