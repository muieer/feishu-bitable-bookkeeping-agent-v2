import json
from decimal import Decimal

import pytest

from bookkeeping.cli import LarkCliLedger, ProcessResult, classify_write
from bookkeeping.domain import Expense

from .support import TODAY


@pytest.mark.parametrize(
    ("process", "state"),
    [
        (ProcessResult(0, '{"ok":true,"data":{"record_id_list":["rec123"]}}'), "success"),
        (ProcessResult(0, '{"ok":true,"data":{}}'), "uncertain"),
        (ProcessResult(0, '{"ok":true,"data":{"record_id_list":[""]}}'), "uncertain"),
        (ProcessResult(0, '{"ok":true,"data":{"record_id_list":["invalid"]}}'), "uncertain"),
        (ProcessResult(0, '{"code":0,"data":{"record_id_list":["rec123"]}}'), "uncertain"),
        (ProcessResult(0, "broken json"), "uncertain"),
        (ProcessResult(-9, timeout=True), "uncertain"),
        (ProcessResult(None, launched=False), "failed"),
        (ProcessResult(1, stderr='{"ok":false,"error":{"type":"authorization"}}'), "failed"),
        (ProcessResult(1, stderr='{"ok":false,"error":{"type":"api","code":1254005}}'), "failed"),
        (ProcessResult(1, stderr='{"ok":false,"error":{"type":"api","code":500}}'), "uncertain"),
        (
            ProcessResult(
                0, '{"ok":true,"data":{"record_id_list":["rec123"],"ignored_fields":["金额"]}}'
            ),
            "success",
        ),
    ],
)
def test_receipt_truth_is_conservative(process, state):
    result = classify_write(process)
    assert result.status == state
    if state == "success":
        assert result.record_id == "rec123"


def coffee():
    return Expense(
        source_text="咖啡18元",
        source_occurrence=1,
        description="咖啡",
        amount=Decimal("18"),
        expense_date=TODAY,
        category_l1="饮食",
        category_l2="咖啡",
    )


async def test_success_is_one_create_and_no_readback(settings, audit, monkeypatch):
    ledger = LarkCliLedger(settings, audit)
    calls = []

    async def fake_run(action, args):
        calls.append((action, args))
        return ProcessResult(0, '{"ok":true,"data":{"record_id_list":["recConfirmed"]}}')

    monkeypatch.setattr(ledger, "_run", fake_run)
    result = await ledger.create(coffee())
    assert result.status == "success"
    assert len(calls) == 1
    action, args = calls[0]
    assert action == "create" and args[:2] == ["base", "+record-batch-create"]
    payload = json.loads(args[args.index("--json") + 1])["create_records"][0]
    assert set(payload) == {"记账日期", "一级分类", "二级分类", "具体", "金额"}
    assert payload["记账日期"] == "2026-10-14T00:00:00+08:00"
    assert payload["一级分类"] == ["饮食"] and payload["金额"] == 18


async def test_model_cannot_turn_keyword_into_shell_or_target(settings, audit, monkeypatch):
    ledger = LarkCliLedger(settings, audit)
    captured = []

    async def fake_run(action, args):
        captured.append(args)
        return ProcessResult(
            0, '{"ok":true,"data":{"fields":["具体"],"data":[["咖啡"]],"has_more":true}}'
        )

    monkeypatch.setattr(ledger, "_run", fake_run)
    keyword = "$(touch /tmp/never_execute); --table-id other"
    result = await ledger.query_history(keyword, 1)
    args = captured[0]
    condition = json.loads(args[args.index("--filter-json") + 1])["conditions"][0]
    assert condition == ["具体", "intersects", keyword]
    assert args[args.index("--table-id") + 1] == settings.bitable_table_id
    assert result["records"] == [{"具体": "咖啡"}] and result["has_more"]
    assert (await ledger.query_history("", 51))["ok"] is False
    assert len(captured) == 1
