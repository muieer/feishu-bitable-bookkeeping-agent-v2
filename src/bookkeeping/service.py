import asyncio
import traceback
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

from .agent import RunDeps, user_prompt
from .audit import Audit, message_context
from .cli import Ledger
from .config import Settings
from .domain import Decision, TaskReport, locate_source
from .journal import Journal
from .knowledge import Knowledge


class Reporter(Protocol):
    async def progress(self, message_id: str, text: str) -> None: ...
    async def final(self, message_id: str, report: TaskReport) -> bool: ...


class NullReporter:
    async def progress(self, message_id: str, text: str) -> None:
        pass

    async def final(self, message_id: str, report: TaskReport) -> bool:
        return True


class BookkeepingService:
    def __init__(
        self,
        settings: Settings,
        agent: Agent[RunDeps, Decision],
        ledger: Ledger,
        knowledge: Knowledge,
        journal: Journal,
        audit: Audit,
        reporter: Reporter | None = None,
    ):
        self.settings, self.agent, self.ledger = settings, agent, ledger
        self.knowledge, self.journal, self.audit = knowledge, journal, audit
        self.reporter = reporter or NullReporter()
        self._lock = asyncio.Lock()

    def today(self) -> date:
        return datetime.now(ZoneInfo(self.settings.timezone)).date()

    def accept(self, message_id: str, chat_id: str, text: str) -> bool:
        accepted = self.journal.accept(message_id, chat_id, text, self.today())
        self.audit.event(
            "message_received" if accepted else "message_duplicate",
            message_id=message_id,
            chat_id=chat_id,
            text=text,
        )
        return accepted

    async def _progress(self, message_id: str, text: str) -> None:
        try:
            await self.reporter.progress(message_id, text)
        except Exception as error:
            self.audit.event("progress_error", error=repr(error))

    async def deliver(self, message_id: str) -> None:
        report = self.journal.report(message_id)
        if report is None:
            return
        try:
            if await self.reporter.final(message_id, report):
                self.journal.notified(message_id)
        except Exception as error:
            self.audit.event("notification_error", message_id=message_id, error=repr(error))

    async def process(
        self, message_id: str, *, today: date | None = None, request_limit: int | None = None
    ) -> TaskReport | None:
        async with self._lock:
            token = message_context.set(message_id)
            try:
                if not self.journal.start(message_id, today or self.today()):
                    return self.journal.report(message_id)
                message = self.journal.message(message_id)

                async def progress(text: str) -> None:
                    await self._progress(message_id, text)

                deps = RunDeps(
                    message_id,
                    message.text,
                    message.execution_date,
                    self.journal,
                    self.ledger,
                    self.knowledge,
                    self.audit,
                    progress,
                )
                await progress("正在理解消费信息与分类")
                cancelled = False
                try:
                    async with asyncio.timeout(self.settings.task_timeout_seconds):
                        result = await self.agent.run(
                            user_prompt(message.text),
                            deps=deps,
                            usage_limits=UsageLimits(
                                request_limit=request_limit or self.settings.model_request_limit,
                                tool_calls_limit=30,
                            ),
                        )
                    skipped = list(result.output.skipped)
                    spans = {
                        locate_source(message.text, item.source_text, item.source_occurrence)
                        for item in skipped
                    }
                    for span, rejected in deps.rejected.items():
                        if span not in spans and not self.journal.overlaps(message_id, span):
                            skipped.append(rejected)
                    report = TaskReport(
                        entries=self.journal.entries(message_id),
                        skipped=skipped,
                        no_consumption=result.output.no_consumption and not skipped,
                    )
                    self.audit.event("agent_completed", usage=result.usage, decision=result.output)
                except (Exception, asyncio.CancelledError) as error:
                    cancelled = isinstance(error, asyncio.CancelledError)
                    self.audit.event(
                        "agent_error", error=repr(error), traceback=traceback.format_exc()
                    )
                    report = TaskReport(
                        entries=self.journal.entries(message_id),
                        skipped=[
                            item
                            for span, item in deps.rejected.items()
                            if not self.journal.overlaps(message_id, span)
                        ],
                        notice="此次处理未能完成，部分消费可能尚未处理。已确认的结果如下；待核对消费请检查账本。",
                    )
                self.journal.finish(message_id, report)
                self.audit.event("task_report", report=report)
                if not cancelled:
                    await self.deliver(message_id)
                if cancelled:
                    raise asyncio.CancelledError
                return report
            finally:
                message_context.reset(token)

    async def handle(self, message_id: str, chat_id: str, text: str, **kwargs) -> TaskReport | None:
        self.accept(message_id, chat_id, text)
        return await self.process(message_id, **kwargs)
