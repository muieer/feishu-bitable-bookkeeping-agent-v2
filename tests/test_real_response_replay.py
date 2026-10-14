"""Replay captured LLM arguments offline; this file makes zero external model requests."""

import json
from collections import Counter
from decimal import Decimal

from bookkeeping.agent import create_agent
from bookkeeping.service import BookkeepingService

from .support import TODAY, MemoryLedger, ScriptedModel


async def test_captured_real_model_decisions_pass_corrected_guards(
    settings, audit, journal, private_knowledge, private_replay
):
    fixture = private_replay
    function = fixture["tool_call"]["function"]
    # Arguments come unchanged from HTTP response 10. Only the termination is simulated.
    scripted = ScriptedModel(
        [[(function["name"], json.loads(function["arguments"]))], [("finish", {})]]
    )
    ledger = MemoryLedger()
    service = BookkeepingService(
        settings, create_agent(scripted.model), ledger, private_knowledge, journal, audit
    )
    report = await service.handle("replay", "test-chat", fixture["user_message"], today=TODAY)
    expected = Counter(
        (day, first, second, third, Decimal(amount))
        for day, first, second, third, amount, _ in fixture["expected_rows"]
    )
    actual = Counter(
        (entry.expense.expense_date.isoformat(), *entry.expense.category_path, entry.expense.amount)
        for entry in report.entries
    )
    assert actual == expected
    assert report.notice is None and report.skipped == []
    assert len(ledger.created) == len(fixture["expected_rows"])
    assert all(entry.outcome.status == "success" for entry in report.entries)


async def test_captured_response_remains_truthful_when_budget_ends_after_tools(
    settings, audit, journal, private_knowledge, private_replay
):
    fixture = private_replay
    function = fixture["tool_call"]["function"]
    scripted = ScriptedModel([[(function["name"], json.loads(function["arguments"]))]])
    ledger = MemoryLedger()
    service = BookkeepingService(
        settings, create_agent(scripted.model), ledger, private_knowledge, journal, audit
    )
    report = await service.handle(
        "capped-replay", "test-chat", fixture["user_message"], today=TODAY, request_limit=1
    )
    assert report.notice
    assert len(report.entries) == len(ledger.created) == len(fixture["expected_rows"])
    assert all(entry.outcome.status == "success" for entry in report.entries)
    assert report.skipped == []
