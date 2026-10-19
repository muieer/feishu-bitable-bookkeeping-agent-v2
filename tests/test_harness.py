"""LangChain migration regressions; scripted models make no network requests."""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from bookkeeping.agent import create_agent
from bookkeeping.service import BookkeepingService

from .support import TODAY, MemoryLedger, ScriptedModel, proposal


def service_for(settings, audit, journal, knowledge, script, ledger=None):
    return BookkeepingService(
        settings,
        create_agent(script.model, settings),
        ledger or MemoryLedger(),
        knowledge,
        journal,
        audit,
    )


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"no_consumption": True, "skipped": [{"source_text": "买咖啡", "reason": "缺少金额"}]},
        {"skipped": [{"source_text": "不存在的原文", "reason": "缺少金额"}]},
        {"skipped": [{"source_text": "买咖啡", "reason": " "}]},
        {"skipped": [{"source_text": "买咖啡", "reason": "已记账"}]},
        {"skipped": [{"source_text": "买咖啡", "reason": "缺少金额"}] * 2},
        {"skipped": "invalid-structure"},
    ],
)
async def test_invalid_completion_can_be_corrected_without_changing_business_result(
    settings, audit, journal, knowledge, invalid
):
    corrected = {"skipped": [{"source_text": "买咖啡", "reason": "缺少金额"}]}
    script = ScriptedModel([[("finish", invalid)], [("finish", corrected)]])
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle("correction", "chat", "买咖啡", today=TODAY)
    assert report.notice is None and not report.no_consumption
    assert len(report.skipped) == 1 and report.skipped[0].reason == "缺少金额"
    assert service.ledger.created == [] and script.calls == 2
    receipts = [message for message in script.seen_messages[-1] if isinstance(message, ToolMessage)]
    assert receipts and receipts[-1].status == "error"


async def test_output_retry_budget_stops_invalid_completions(settings, audit, journal, knowledge):
    script = ScriptedModel([[("finish", {})]] * 4)
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle("bad-output", "chat", "买咖啡", today=TODAY)
    assert report.notice and script.calls == 3 and not service.ledger.created


@pytest.mark.parametrize("finish_first", [False, True])
@pytest.mark.parametrize("request_limit", [1, 2])
async def test_same_turn_empty_completion_is_checked_before_ledger_calls(
    settings, audit, journal, knowledge, finish_first, request_limit
):
    calls = [("add_expenses", {"expenses": [proposal().model_dump()]}), ("finish", {})]
    if finish_first:
        calls.reverse()
    script = ScriptedModel([calls, [("finish", {})]])
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle(
        "mixed-tools", "chat", "今天买咖啡18元", today=TODAY, request_limit=request_limit
    )
    # The baseline rejects an empty Decision before there are any journal entries.
    # Exhaustive tool execution still writes the valid row; completing needs another request.
    assert bool(report.notice) == (request_limit == 1)
    assert len(report.entries) == len(service.ledger.created) == 1
    assert report.entries[0].outcome.status == "success" and script.calls == request_limit
    assert report.skipped == [] and not report.no_consumption


async def test_submitted_expense_cannot_be_relabelled_as_skipped(
    settings, audit, journal, knowledge
):
    script = ScriptedModel(
        [
            [("add_expenses", {"expenses": [proposal().model_dump()]})],
            [("finish", {"skipped": [{"source_text": "今天买咖啡18元", "reason": "分类不明确"}]})],
            [("finish", {})],
        ]
    )
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle("overlap", "chat", "今天买咖啡18元", today=TODAY)
    assert report.notice is None and report.skipped == [] and len(report.entries) == 1
    assert len(service.ledger.created) == 1 and script.calls == 3


async def test_same_turn_schema_error_can_be_corrected_after_writes(
    settings, audit, journal, knowledge
):
    script = ScriptedModel(
        [
            [
                ("add_expenses", {"expenses": [proposal().model_dump()]}),
                ("finish", {"skipped": "bad-schema"}),
            ],
            [("finish", {})],
        ]
    )
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle("mixed-invalid", "chat", "今天买咖啡18元", today=TODAY)
    assert report.notice is None and len(report.entries) == len(service.ledger.created) == 1
    assert not report.no_consumption and report.skipped == [] and script.calls == 2


@pytest.mark.parametrize("finish_first", [False, True])
@pytest.mark.parametrize("kind", ["skipped", "no_consumption"])
async def test_same_turn_completion_retains_baseline_pre_submission_snapshot(
    settings, audit, journal, knowledge, finish_first, kind
):
    # Deliberately retain the baseline's known business discrepancy. Fixing it is
    # separate from preserving the framework migration's existing behavior.
    decision = (
        {"skipped": [{"source_text": "今天买咖啡18元", "reason": "分类不明确"}]}
        if kind == "skipped"
        else {"no_consumption": True}
    )
    calls = [("add_expenses", {"expenses": [proposal().model_dump()]}), ("finish", decision)]
    if finish_first:
        calls.reverse()
    script = ScriptedModel([calls])
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle(
        "baseline-snapshot", "chat", "今天买咖啡18元", today=TODAY, request_limit=1
    )
    assert report.notice is None and script.calls == 1
    assert len(report.entries) == len(service.ledger.created) == 1
    assert report.entries[0].outcome.status == "success"
    assert report.no_consumption == (kind == "no_consumption")
    assert [item.model_dump() for item in report.skipped] == [
        {**item, "source_occurrence": 1} for item in decision.get("skipped", [])
    ]


@pytest.mark.parametrize("order", ["both-valid", "invalid-first", "invalid-last"])
async def test_exhaustive_completion_retains_first_valid_result(
    settings, audit, journal, knowledge, order
):
    valid = {"skipped": [{"source_text": "买咖啡", "reason": "缺少金额"}]}
    other = {"skipped": [{"source_text": "买咖啡", "reason": "未提供实付金额"}]}
    completions = {
        "both-valid": [valid, other],
        "invalid-first": [{"skipped": "bad-schema"}, valid],
        "invalid-last": [valid, {}],
    }[order]
    script = ScriptedModel([[("finish", args) for args in completions]])
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle("several-outputs", "chat", "买咖啡", today=TODAY)
    assert report.notice is None and script.calls == 1
    assert report.entries == [] and service.ledger.created == []
    assert len(report.skipped) == 1 and report.skipped[0].reason == "缺少金额"


async def test_clean_tool_round_resets_completion_tool_retry_count(
    settings, audit, journal, knowledge
):
    script = ScriptedModel(
        [
            step
            for _ in range(3)
            for step in [[("finish", {})], [("query_history", {"keyword": "咖啡"})]]
        ]
        + [[("finish", {"no_consumption": True})]]
    )
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle("output-reset", "chat", "今天心情不错", today=TODAY)
    assert report.notice is None and report.no_consumption and script.calls == 7
    assert len(service.ledger.queries) == 3 and report.entries == []


async def test_business_tools_remain_serial_even_when_model_emits_parallel_calls(
    settings, audit, journal, knowledge
):
    class SerialLedger(MemoryLedger):
        active = False

        async def create(self, expense, unique_id):
            assert not self.active, "Ledger writes must never overlap"
            self.active = True
            await asyncio.sleep(0)
            result = await super().create(expense, unique_id)
            self.active = False
            return result

    rows = [proposal(), proposal("今天买咖啡19元", amount="19", amount_evidence="19元")]
    script = ScriptedModel(
        [
            [("add_expenses", {"expenses": [row.model_dump()]}) for row in rows],
            [("finish", {})],
        ]
    )
    service = service_for(settings, audit, journal, knowledge, script, SerialLedger())
    report = await service.handle("serial", "chat", "今天买咖啡18元，今天买咖啡19元", today=TODAY)
    assert report.notice is None and len(report.entries) == 2
    assert all(entry.outcome.status == "success" for entry in report.entries)
    assert [str(entry.expense.amount) for entry in report.entries] == [row.amount for row in rows]


@pytest.mark.parametrize("call_count", [30, 31])
async def test_tool_limit_is_enforced_before_dispatch(
    settings, audit, journal, knowledge, call_count
):
    script = ScriptedModel(
        [
            [("query_history", {"keyword": "咖啡"})] * call_count,
            [("finish", {"no_consumption": True})],
        ]
    )
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle("tool-limit", "chat", "今天心情不错", today=TODAY)
    assert bool(report.notice) == (call_count > 30)
    assert len(service.ledger.queries) == (30 if call_count == 30 else 0)
    assert script.calls == (2 if call_count == 30 else 1)


async def test_bad_tool_arguments_are_sanitized_and_correctable(
    settings, audit, journal, knowledge
):
    script = ScriptedModel(
        [
            [("add_expenses", {"expenses": settings.ark_api_key.get_secret_value()})],
            [("add_expenses", {"expenses": [proposal().model_dump()]})],
            [("finish", {})],
        ]
    )
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle("bad-tool", "chat", "今天买咖啡18元", today=TODAY)
    assert report.notice is None and len(report.entries) == 1
    receipts = [message for message in script.seen_messages[1] if isinstance(message, ToolMessage)]
    assert len(receipts) == 1 and receipts[0].status == "error"
    assert settings.ark_api_key.get_secret_value() not in receipts[0].content


@pytest.mark.parametrize("failure_kind", ["schema", "quantity"])
async def test_tool_validation_retry_budget_is_bounded(
    settings, audit, journal, knowledge, failure_kind
):
    expenses = "bad" if failure_kind == "schema" else [proposal().model_dump()] * 101
    script = ScriptedModel([[("add_expenses", {"expenses": expenses})]] * 4)
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle("tool-retries", "chat", "买咖啡", today=TODAY)
    assert report.notice and not report.entries and script.calls == 3


async def test_quantity_errors_share_a_round_and_keep_actionable_feedback(
    settings, audit, journal, knowledge
):
    script = ScriptedModel(
        [
            [("add_expenses", {"expenses": [proposal().model_dump()] * 101})] * 3,
            [("add_expenses", {"expenses": [proposal().model_dump()]})],
            [("finish", {})],
        ]
    )
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle("quantity-errors", "chat", "今天买咖啡18元", today=TODAY)
    assert report.notice is None and script.calls == 3
    assert len(report.entries) == len(service.ledger.created) == 1
    receipts = [item for item in script.seen_messages[1] if isinstance(item, ToolMessage)]
    assert len(receipts) == 3
    assert all(item.status == "error" and "100" in item.content for item in receipts)


@pytest.mark.parametrize("extra", [{"table_id": "tblOtherTest"}, {"runtime": {}}])
async def test_undeclared_tool_arguments_are_rejected_before_dispatch(
    settings, audit, journal, knowledge, extra
):
    script = ScriptedModel(
        [
            [("query_history", {"keyword": "咖啡", **extra})],
            [("query_history", {"keyword": "咖啡"})],
            [("finish", {"no_consumption": True})],
        ]
    )
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle("undeclared-args", "chat", "今天心情不错", today=TODAY)
    assert report.notice is None and report.no_consumption and script.calls == 3
    assert service.ledger.queries == [("咖啡", 20)] and service.ledger.created == []
    receipts = [item for item in script.seen_messages[1] if isinstance(item, ToolMessage)]
    assert len(receipts) == 1 and receipts[0].status == "error"


async def test_successful_tool_round_resets_prior_validation_errors(
    settings, audit, journal, knowledge
):
    rows = [
        proposal(f"今天买咖啡{amount}元", amount=str(amount), amount_evidence=f"{amount}元")
        for amount in (18, 19, 20)
    ]
    steps = []
    for row in rows:
        steps.extend(
            [
                [("add_expenses", {"expenses": "bad"})],
                [("add_expenses", {"expenses": [row.model_dump()]})],
            ]
        )
    script = ScriptedModel([*steps, [("finish", {})]])
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle(
        "independent-errors", "chat", "，".join(row.source_text for row in rows), today=TODAY
    )
    assert report.notice is None and script.calls == 7
    assert len(report.entries) == len(service.ledger.created) == 3
    assert all(entry.outcome.status == "success" for entry in report.entries)


async def test_same_tool_errors_share_a_single_retry_round(settings, audit, journal, knowledge):
    script = ScriptedModel(
        [
            [("add_expenses", {"expenses": "bad"})] * 3
            + [("add_expenses", {"expenses": [proposal().model_dump()]})],
            [("finish", {})],
        ]
    )
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle("same-round-errors", "chat", "今天买咖啡18元", today=TODAY)
    assert report.notice is None and script.calls == 2
    assert len(report.entries) == len(service.ledger.created) == 1
    assert report.entries[0].outcome.status == "success"


async def test_exhausted_retry_budget_is_checked_before_any_same_round_write(
    settings, audit, journal, knowledge
):
    script = ScriptedModel(
        [
            [("add_expenses", {"expenses": "bad"})],
            [("add_expenses", {"expenses": "bad"})],
            [
                ("add_expenses", {"expenses": [proposal().model_dump()]}),
                ("add_expenses", {"expenses": "bad"}),
            ],
        ]
    )
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle("exhausted-errors", "chat", "今天买咖啡18元", today=TODAY)
    assert report.notice and script.calls == 3
    assert report.entries == [] and service.ledger.created == []


async def test_validation_failures_do_not_consume_completed_tool_allowance(
    settings, audit, journal, knowledge
):
    script = ScriptedModel(
        [
            [("add_expenses", {"expenses": "bad"})] * 30,
            [("query_history", {"keyword": "咖啡"})] * 30,
            [("finish", {"no_consumption": True})],
        ]
    )
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle("failed-call-usage", "chat", "今天心情不错", today=TODAY)
    assert report.notice is None and report.no_consumption and script.calls == 3
    assert len(service.ledger.queries) == 30 and service.ledger.created == []


async def test_plain_text_cannot_claim_success_and_does_not_end_without_decision(
    settings, audit, journal, knowledge
):
    original = ScriptedModel([[("finish", {"no_consumption": True})]])
    respond = original.respond

    def with_plain_text(messages):
        if original.calls == 0:
            original.calls += 1
            return ChatResult(generations=[ChatGeneration(message=AIMessage("已记账"))])
        return respond(messages)

    original.respond = with_plain_text
    service = service_for(settings, audit, journal, knowledge, original)
    report = await service.handle("plain", "chat", "今天心情不错", today=TODAY)
    assert report.notice is None and report.no_consumption and report.entries == []
    assert original.calls == 2


async def test_context_and_limits_do_not_leak_between_invocations(
    settings, audit, journal, knowledge
):
    script = ScriptedModel([[("finish", {"no_consumption": True})]] * 2)
    service = service_for(settings, audit, journal, knowledge, script)
    for index, message in enumerate(("今天心情不错", "明天再买咖啡")):
        report = await service.handle(str(index), "chat", message, today=TODAY, request_limit=1)
        assert report.notice is None and report.no_consumption
        users = [item.content for item in script.seen_messages[-1] if item.type == "human"]
        assert len(users) == 1 and message in users[0]
    assert script.calls == 2


async def test_skipped_schema_errors_do_not_echo_input_credentials(
    settings, audit, journal, knowledge
):
    script = ScriptedModel(
        [
            [("finish", {"skipped": settings.ark_api_key.get_secret_value()})],
            [("finish", {"no_consumption": True})],
        ]
    )
    service = service_for(settings, audit, journal, knowledge, script)
    report = await service.handle("safe-output", "chat", "今天心情不错", today=TODAY)
    assert report.notice is None
    receipts = [item.content for item in script.seen_messages[-1] if isinstance(item, ToolMessage)]
    assert settings.ark_api_key.get_secret_value() not in json.dumps(receipts)
