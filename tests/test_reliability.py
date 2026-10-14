import asyncio
from datetime import date

from lark_channel.channel.types import SendResult
from pydantic_ai.messages import ToolReturnPart

from bookkeeping.agent import create_agent
from bookkeeping.cards import CardReporter, result_cards
from bookkeeping.domain import TaskReport, WriteOutcome
from bookkeeping.journal import Journal
from bookkeeping.service import BookkeepingService

from .support import TODAY, MemoryLedger, ScriptedModel, proposal


def service_for(settings, audit, journal, knowledge, script, ledger=None, reporter=None):
    return BookkeepingService(
        settings,
        create_agent(script.model),
        ledger or MemoryLedger(),
        knowledge,
        journal,
        audit,
        reporter,
    )


async def test_invalid_peer_does_not_block_valid_row_and_no_false_success(
    settings, audit, journal, knowledge
):
    good = proposal()
    bad = proposal(
        "买咖啡0元", amount="0", amount_evidence="0元", date_evidence=None, expense_date=None
    )
    script = ScriptedModel(
        [
            [("add_expenses", {"expenses": [bad.model_dump(), good.model_dump()]})],
            [("finish", {"skipped": [{"source_text": "买咖啡0元", "reason": "金额必须大于零"}]})],
        ]
    )
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle("m1", "c1", "今天买咖啡18元，买咖啡0元", today=TODAY)
    assert len(service.ledger.created) == 1 and len(report.entries) == 1
    assert report.notice is None
    assert report.entries[0].outcome.record_id == "recFake001"
    assert report.skipped[0].source_text == "买咖啡0元"
    returns = [
        part.content
        for msg in script.seen_messages[-1]
        for part in msg.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert any(item[0]["status"] == "rejected" for item in returns)


async def test_duplicate_id_is_blocked_across_restart_but_new_id_is_not(
    settings, audit, journal, knowledge
):
    script = ScriptedModel(
        [
            [("add_expenses", {"expenses": [proposal().model_dump()]})],
            [("finish", {})],
            [("add_expenses", {"expenses": [proposal().model_dump()]})],
            [("finish", {})],
        ]
    )
    ledger = MemoryLedger()
    service = service_for(settings, audit, journal, knowledge, script, ledger)
    first = await service.handle("same", "chat", "今天买咖啡18元", today=TODAY)
    duplicate = await service.handle("same", "chat", "今天买咖啡18元", today=TODAY)
    assert first == duplicate and script.calls == 2 and len(ledger.created) == 1
    assert first.notice is None
    reopened = Journal(settings.private_dir / "journal.sqlite3", "unit-test")
    try:
        restarted = service_for(settings, audit, reopened, knowledge, script, ledger)
        assert await restarted.handle("same", "chat", "今天买咖啡18元", today=TODAY) == first
        await restarted.handle("new", "chat", "今天买咖啡18元", today=TODAY)
        assert len(ledger.created) == 2 and script.calls == 4
    finally:
        reopened.close()


async def test_retries_and_aliases_of_uncertain_write_do_not_submit_again(
    settings, audit, journal, knowledge
):
    original = proposal()
    alias = proposal("买咖啡18元", date_evidence="今天")
    script = ScriptedModel(
        [
            [("add_expenses", {"expenses": [original.model_dump()]})],
            [("add_expenses", {"expenses": [alias.model_dump()]})],
            [("finish", {})],
        ]
    )
    service = service_for(
        settings, audit, journal, knowledge, script, MemoryLedger({"咖啡": "uncertain"})
    )
    report = await service.handle("unknown", "chat", original.source_text, today=TODAY)
    assert len(service.ledger.created) == 1
    assert len(report.entries) == 1 and report.entries[0].outcome.status == "uncertain"
    assert report.notice is None
    body = str(result_cards(report))
    assert "待核对" in body and "已记账消费记录" not in body


async def test_identical_real_transactions_with_two_occurrences_are_both_written(
    settings, audit, journal, knowledge
):
    rows = [proposal("买咖啡18元", date_evidence=None, source_occurrence=i) for i in (1, 2)]
    script = ScriptedModel(
        [[("add_expenses", {"expenses": [r.model_dump() for r in rows]})], [("finish", {})]]
    )
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle("twice", "chat", "早上买咖啡18元，下午买咖啡18元", today=TODAY)
    assert len(report.entries) == 2 and len(service.ledger.created) == 2
    assert report.notice is None


async def test_all_outcomes_survive_model_failure(settings, audit, journal, knowledge):
    rows = [proposal(), proposal("今天买咖啡19元", amount="19", amount_evidence="19元")]
    script = ScriptedModel([[("add_expenses", {"expenses": [row.model_dump() for row in rows]})]])
    ledger = MemoryLedger()
    service = service_for(settings, audit, journal, knowledge, script, ledger)
    report = await service.handle(
        "limit", "chat", "今天买咖啡18元，今天买咖啡19元", today=TODAY, request_limit=1
    )
    assert report.notice and len(report.entries) == 2
    assert all(entry.outcome.status == "success" for entry in report.entries)
    assert journal.report("limit") == report


def test_crash_recovery_preserves_success_and_marks_only_inflight_unknown(journal, knowledge):
    message = "今天买咖啡18元，今天买咖啡19元"
    journal.accept("crash", "chat", message, TODAY)
    journal.start("crash", TODAY)
    for index, amount in enumerate(("18", "19")):
        row = proposal(f"今天买咖啡{amount}元", amount=amount, amount_evidence=f"{amount}元")
        expense, span = knowledge.validate(row, message, TODAY)
        journal.reserve("crash", span, expense)
        if index == 0:
            journal.complete_write(
                "crash", span, WriteOutcome(status="success", record_id="recKnown")
            )
    assert journal.recover_interrupted() == ["crash"]
    report = journal.report("crash")
    assert [entry.outcome.status for entry in report.entries] == ["success", "uncertain"]
    assert "crash" in journal.pending_notifications()
    assert not journal.start("crash", TODAY)


async def test_agent_can_choose_history_first_or_finish_without_tools(
    settings, audit, journal, knowledge
):
    scripted = ScriptedModel(
        [
            [("query_history", {"keyword": "咖啡", "limit": 2})],
            [("add_expenses", {"expenses": [proposal().model_dump()]})],
            [("finish", {})],
            [("finish", {"no_consumption": True})],
        ]
    )
    service = service_for(settings, audit, journal, knowledge, scripted)
    await service.handle("coffee", "chat", "今天买咖啡18元", today=TODAY)
    report = await service.handle("chatting", "chat", "明天准备买衣服，今天心情不错", today=TODAY)
    assert service.ledger.queries == [("咖啡", 2)]
    assert len(service.ledger.created) == 1 and report.no_consumption
    assert report.notice is None
    assert "此次未包含消费记录。" in str(result_cards(report))


async def test_cancellation_after_submission_is_persisted_as_uncertain(
    settings, audit, journal, knowledge
):
    started = asyncio.Event()

    class HangingLedger(MemoryLedger):
        async def create(self, expense):
            self.created.append(expense)
            started.set()
            await asyncio.Event().wait()

    script = ScriptedModel([[("add_expenses", {"expenses": [proposal().model_dump()]})]])
    ledger = HangingLedger()
    service = service_for(settings, audit, journal, knowledge, script, ledger)
    task = asyncio.create_task(service.handle("cancel", "chat", "今天买咖啡18元", today=TODAY))
    await started.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    report = journal.report("cancel")
    assert report and report.entries[0].outcome.status == "uncertain"
    assert len(ledger.created) == 1


async def test_failed_card_delivery_is_retried_without_repeating_writes(
    settings, audit, journal, knowledge
):
    class FakeChannel:
        def __init__(self):
            self.sent = []
            self.updates = []
            self.fail_update = True

        async def send(self, chat, message, opts):
            self.sent.append((chat, message, opts))
            return SendResult.ok("omCard")

        async def update_card(self, card_id, card):
            self.updates.append(card_id)
            return SendResult(success=not self.fail_update, message_id=card_id)

    channel = FakeChannel()
    reporter = CardReporter(channel, journal, audit)
    script = ScriptedModel(
        [[("add_expenses", {"expenses": [proposal().model_dump()]})], [("finish", {})]]
    )
    service = service_for(settings, audit, journal, knowledge, script, reporter=reporter)
    await service.handle("notify", "chat", "今天买咖啡18元", today=TODAY)
    assert journal.pending_notifications() == ["notify"]
    channel.fail_update = False
    await service.deliver("notify")
    assert journal.pending_notifications() == []
    assert len(service.ledger.created) == 1 and len(channel.sent) == 1 and script.calls == 2


def test_execution_day_can_differ_from_acceptance_day(journal):
    journal.accept("midnight", "chat", "买咖啡18元", date(2026, 10, 13))
    journal.start("midnight", TODAY)
    assert journal.message("midnight").execution_date == TODAY


def test_mismatched_ledger_cannot_reuse_dedup_database(settings, journal):
    import pytest

    with pytest.raises(ValueError, match="其他应用或账本"):
        Journal(settings.private_dir / "journal.sqlite3", "different-ledger")


def test_card_escapes_mentions_and_paginates_without_hiding_results():
    from bookkeeping.domain import SkippedExpense

    report = TaskReport(
        skipped=[
            SkippedExpense(source_text=f"<at id=all>消费{i}" + "长" * 40, reason="分类不明确")
            for i in range(10)
        ]
    )
    cards = result_cards(report, page_size=200)
    bodies = [item["body"]["elements"][0]["content"] for item in cards]
    assert len(cards) > 1 and "<at" not in "".join(bodies)
    for index in range(10):
        assert f"消费{index}" in "".join(bodies)
