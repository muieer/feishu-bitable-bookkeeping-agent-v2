"""Offline developer checks for durable one-shot reconciliation through the real agent/tool/CLI."""

import asyncio
import json
import socket
import sqlite3
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from langchain_core.messages import ToolMessage
from pydantic import ValidationError

from bookkeeping import tools
from bookkeeping.agent import create_agent
from bookkeeping.cards import result_cards
from bookkeeping.cli import LarkCliLedger, ProcessResult
from bookkeeping.domain import ExpenseProposal
from bookkeeping.journal import Journal
from bookkeeping.service import BookkeepingService

from .support import TODAY, ScriptedModel, proposal

UNKNOWN = ProcessResult(-9, timeout=True)
DENIED = ProcessResult(1, '{"ok":false,"error":{"type":"validation"}}')


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("External I/O is forbidden in reconciliation tests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)


class CliBoundary:
    """Synthetic remote state, with no uniqueness constraint on unique_id."""

    def __init__(self, ledger, journal, settings, monkeypatch):
        self.ledger, self.journal, self.settings = ledger, journal, settings
        self.records = []
        self.attempts = []
        self.calls = []
        self.waits = []
        self.plan = [(True, None)]
        self.query_override = None
        monkeypatch.setattr(ledger, "_run", self.run)
        monkeypatch.setattr(
            tools,
            "asyncio",
            SimpleNamespace(sleep=self.wait, CancelledError=asyncio.CancelledError),
        )

    async def wait(self, seconds):
        self.waits.append(seconds)
        assert seconds == 3
        # Read using a second connection, proving the pending status is committed before waiting.
        with sqlite3.connect(self.settings.private_dir / "journal.sqlite3") as connection:
            state, uid, payload = connection.execute(
                "SELECT state, unique_id, expense FROM writes ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        assert state == "uncertain" and UUID(uid).version == 4
        assert json.loads(payload)["expense_date"] == TODAY.isoformat()
        await asyncio.sleep(0)

    async def run(self, action, args):
        self.calls.append(action)
        assert args[args.index("--table-id") + 1] == self.settings.bitable_table_id
        assert args[args.index("--as") + 1] == self.settings.lark_cli_identity
        assert "--view-id" not in args
        if action == "create":
            fields = json.loads(args[args.index("--json") + 1])["create_records"][0]
            with sqlite3.connect(self.settings.private_dir / "journal.sqlite3") as connection:
                uid, snapshot = connection.execute(
                    "SELECT unique_id, expense FROM writes WHERE unique_id=?",
                    (fields["unique_id"],),
                ).fetchone()
            assert UUID(uid).version == 4 and Decimal(json.loads(snapshot)["amount"]) == Decimal(
                str(fields["金额"])
            )
            self.attempts.append(deepcopy(fields))
            commit, receipt = self.plan.pop(0)
            rid = f"recDev{len(self.records) + 1}"
            if commit:
                self.records.append({"record_id": rid, **deepcopy(fields)})
            if isinstance(receipt, Exception):
                raise receipt
            return receipt or ProcessResult(
                0, json.dumps({"ok": True, "data": {"record_id_list": [rid]}})
            )
        assert action == "reconcile"
        condition = json.loads(args[args.index("--filter-json") + 1])
        uid = self.attempts[0]["unique_id"]
        assert condition == {"logic": "and", "conditions": [["unique_id", "==", uid]]}
        assert args[args.index("--limit") + 1] == "2"
        if self.query_override is not None:
            if isinstance(self.query_override, Exception):
                raise self.query_override
            return self.query_override
        fields = [args[index + 1] for index, arg in enumerate(args) if arg == "--field-id"]
        found = [row for row in self.records if row["unique_id"] == uid]
        return ProcessResult(
            0,
            json.dumps(
                {
                    "ok": True,
                    "data": {
                        "fields": fields,
                        "data": [[row.get(name) for name in fields] for row in found[:2]],
                        "record_id_list": [row["record_id"] for row in found[:2]],
                        "has_more": len(found) > 2,
                    },
                }
            ),
        )


@pytest.fixture
def boundary(settings, audit, journal, monkeypatch):
    return CliBoundary(LarkCliLedger(settings, audit), journal, settings, monkeypatch)


async def run_message(settings, audit, journal, knowledge, boundary, *, repeat=False):
    data = proposal()
    steps = [[("add_expenses", {"expenses": [data.model_dump()]})]]
    if repeat:
        # Even an invalid changed category/date must reuse the already committed submission.
        changed = proposal("买咖啡18元", category_l1="不存在", expense_date="nonsense")
        steps.append([("add_expenses", {"expenses": [changed.model_dump()]})])
    steps.append([("finish", {})])
    script = ScriptedModel(steps)
    service = BookkeepingService(
        settings, create_agent(script.model), boundary.ledger, knowledge, journal, audit
    )
    report = await service.handle("dev-message", "dev-chat", data.source_text, today=TODAY)
    assert report == journal.report("dev-message") and len(report.entries) == 1
    assert report.notice is None
    return service, script, report


@pytest.mark.parametrize("initial,status", [(None, "success"), (DENIED, "failed")])
async def test_definite_receipts_never_wait_or_query(
    settings, audit, journal, knowledge, boundary, initial, status
):
    boundary.plan = [(initial is None, initial)]
    _, script, report = await run_message(settings, audit, journal, knowledge, boundary)
    assert report.entries[0].outcome.status == status
    assert boundary.calls == ["create"] and boundary.waits == [] and script.calls == 2


@pytest.mark.parametrize("initial", [UNKNOWN, ProcessResult(0, "broken"), RuntimeError("lost")])
async def test_committed_unknown_is_found_once_without_resubmitting(
    settings, audit, journal, knowledge, boundary, initial
):
    boundary.plan = [(True, initial)]
    service, script, report = await run_message(settings, audit, journal, knowledge, boundary)
    assert report.entries[0].outcome.status == "success"
    assert report.entries[0].outcome.record_id == "recDev1"
    assert boundary.calls == ["create", "reconcile"] and boundary.waits == [3]
    assert script.calls == 2 and "已记账消费记录" in str(result_cards(report))
    await service.deliver("dev-message")
    await service.process("dev-message")
    assert boundary.calls == ["create", "reconcile"] and script.calls == 2


async def test_absent_is_resubmitted_from_saved_snapshot_once_and_duplicate_call_reuses_result(
    settings, audit, journal, knowledge, boundary, monkeypatch
):
    validated = []
    original = knowledge.validate

    def track(*args):
        validated.append(True)
        return original(*args)

    monkeypatch.setattr(knowledge, "validate", track)
    boundary.plan = [(False, UNKNOWN), (True, None)]
    _, script, report = await run_message(
        settings, audit, journal, knowledge, boundary, repeat=True
    )
    assert report.entries[0].outcome.status == "success"
    assert boundary.calls == ["create", "reconcile", "create"] and boundary.waits == [3]
    assert boundary.attempts[0] == boundary.attempts[1] and validated == [True]
    assert script.calls == 3 and len(boundary.records) == 1
    receipts = [
        json.loads(msg.content)
        for msg in script.seen_messages[-1]
        if isinstance(msg, ToolMessage) and msg.name == "add_expenses"
    ]
    assert receipts[-1][0]["already_submitted"] is True


@pytest.mark.parametrize("retry", [DENIED, UNKNOWN, ProcessResult(0, "?"), OSError("unavailable")])
async def test_unconfirmed_supplement_never_reclassifies_original_as_failed_or_restarts(
    settings, audit, journal, knowledge, boundary, retry
):
    boundary.plan = [(False, UNKNOWN), (False, retry)]
    _, script, report = await run_message(
        settings, audit, journal, knowledge, boundary, repeat=True
    )
    assert report.entries[0].outcome.status == "uncertain" and script.calls == 3
    assert boundary.calls == ["create", "reconcile", "create"] and boundary.waits == [3]
    assert boundary.attempts[0] == boundary.attempts[1]
    cards = str(result_cards(report))
    assert "待核对" in cards and "已记账消费记录" not in cards and "不记账消费记录" not in cards


@pytest.mark.parametrize(
    "response",
    [
        UNKNOWN,
        DENIED,
        ProcessResult(None, launched=False),
        ProcessResult(0, "[]"),
        ProcessResult(0, "garbled"),
        RuntimeError("query unavailable"),
        *[
            ProcessResult(0, json.dumps({"ok": True, "data": value}))
            for value in (
                None,
                [],
                {},
                {"fields": [], "data": [], "record_id_list": [], "has_more": False},
                {"fields": ["unique_id"], "data": [], "record_id_list": [], "has_more": False},
            )
        ],
    ],
)
async def test_unusable_queries_remain_pending_without_supplement(
    settings, audit, journal, knowledge, boundary, response
):
    boundary.plan = [(False, UNKNOWN)]
    boundary.query_override = response
    _, _, report = await run_message(settings, audit, journal, knowledge, boundary)
    assert report.entries[0].outcome.status == "uncertain"
    assert boundary.calls == ["create", "reconcile"] and boundary.waits == [3]


@pytest.mark.parametrize(
    "mutation",
    [
        "more",
        "missing_more",
        "wrong_more",
        "duplicate",
        "missing_ids",
        "bad_id",
        "bad_row",
        "unique_id",
        "金额",
        "具体",
        "记账日期",
        "一级分类",
        "二级分类",
        "三级分类",
        "bool_amount",
    ],
)
async def test_nonempty_or_incomplete_conflicts_never_supplement(
    settings, audit, journal, knowledge, boundary, monkeypatch, mutation
):
    boundary.plan = [(True, UNKNOWN)]
    real_run = boundary.run

    async def mutated(action, args):
        result = await real_run(action, args)
        if action != "reconcile":
            return result
        envelope = json.loads(result.stdout)
        data = envelope["data"]
        if mutation == "more":
            data["has_more"] = True
        elif mutation == "missing_more":
            del data["has_more"]
        elif mutation == "wrong_more":
            data["has_more"] = "false"
        elif mutation == "duplicate":
            data["data"] *= 2
            data["record_id_list"] = ["recDev1", "recDev2"]
        elif mutation == "missing_ids":
            del data["record_id_list"]
        elif mutation == "bad_id":
            data["record_id_list"] = ["invalid"]
        elif mutation == "bad_row":
            data["data"][0].pop()
        else:
            name = "金额" if mutation == "bool_amount" else mutation
            data["data"][0][data["fields"].index(name)] = {
                "unique_id": "someone-else",
                "金额": 99,
                "bool_amount": True,
                "具体": "不同内容",
                "记账日期": "2026-10-15T00:00:00+08:00",
                "一级分类": ["交通通信"],
                "二级分类": ["饮用水"],
                "三级分类": ["停车费"],
            }[mutation]
        return ProcessResult(0, json.dumps(envelope))

    monkeypatch.setattr(boundary.ledger, "_run", mutated)
    _, _, report = await run_message(settings, audit, journal, knowledge, boundary)
    assert report.entries[0].outcome.status == "uncertain"
    assert boundary.calls == ["create", "reconcile"]


async def test_matching_date_in_different_offset_and_empty_third_category_are_accepted(
    settings, audit, journal, knowledge, boundary, monkeypatch
):
    boundary.plan = [(True, UNKNOWN)]
    run = boundary.run

    async def canonical(action, args):
        if action == "reconcile":
            boundary.records[0]["记账日期"] = "2026-10-13T16:00:00Z"
            boundary.records[0]["三级分类"] = []
        return await run(action, args)

    monkeypatch.setattr(boundary.ledger, "_run", canonical)
    _, _, report = await run_message(settings, audit, journal, knowledge, boundary)
    assert report.entries[0].outcome.status == "success"


@pytest.mark.parametrize("stage", ["wait", "query", "supplement"])
async def test_cancellation_at_every_reconciliation_stage_persists_pending_without_restart(
    settings, audit, journal, knowledge, boundary, monkeypatch, stage
):
    boundary.plan = [(False, UNKNOWN), (True, None)]
    reached = asyncio.Event()
    original_run, original_wait = boundary.run, boundary.wait

    async def wait(seconds):
        await original_wait(seconds)
        if stage == "wait":
            reached.set()
            await asyncio.Event().wait()

    async def run(action, args):
        block = (stage == "query" and action == "reconcile") or (
            stage == "supplement" and action == "create" and boundary.attempts
        )
        result = await original_run(action, args)
        if block:
            reached.set()
            await asyncio.Event().wait()
        return result

    monkeypatch.setattr(boundary.ledger, "_run", run)
    monkeypatch.setattr(
        tools, "asyncio", SimpleNamespace(sleep=wait, CancelledError=asyncio.CancelledError)
    )
    task = asyncio.create_task(run_message(settings, audit, journal, knowledge, boundary))
    await asyncio.wait_for(reached.wait(), 5)
    task.cancel()
    result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    report = journal.report("dev-message")
    assert report.entries[0].outcome.status == "uncertain"
    before = list(boundary.calls)
    reopened = Journal(settings.private_dir / "journal.sqlite3", "unit-test")
    try:
        assert reopened.recover_interrupted() == []
        assert reopened.report("dev-message") == report
        script = ScriptedModel([])
        service = BookkeepingService(
            settings, create_agent(script.model), boundary.ledger, knowledge, reopened, audit
        )
        await service.process("dev-message")
        await service.deliver("dev-message")
        assert script.calls == 0 and boundary.calls == before
    finally:
        reopened.close()


@pytest.mark.parametrize("status", ["submitted", "success", "failed", "uncertain"])
async def test_legacy_database_keeps_null_ids_and_never_replays(
    settings, audit, knowledge, boundary, status
):
    path = settings.private_dir / "legacy.sqlite3"
    item = proposal()
    expense, span = knowledge.validate(item, item.source_text, TODAY)
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE writes (
                message_id TEXT NOT NULL, source_start INTEGER NOT NULL,
                source_end INTEGER NOT NULL, expense TEXT NOT NULL, state TEXT NOT NULL,
                record_id TEXT, reason TEXT NOT NULL,
                PRIMARY KEY (message_id, source_start, source_end)
            );
        """)
        connection.execute(
            "INSERT INTO writes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "old-message",
                *span,
                expense.model_dump_json(),
                status,
                "recLegacy" if status == "success" else None,
                "legacy reason",
            ),
        )
    old = Journal(path, "unit-test")
    try:
        old.accept("old-message", "old-chat", item.source_text, TODAY)
        script = ScriptedModel(
            [[("add_expenses", {"expenses": [item.model_dump()]})], [("finish", {})]]
        )
        service = BookkeepingService(
            settings, create_agent(script.model), boundary.ledger, knowledge, old, audit
        )
        report = await service.process("old-message", today=TODAY)
        assert report.entries[0].outcome.status == (
            "uncertain" if status == "submitted" else status
        )
        assert old.submission("old-message", span).unique_id is None
        assert old.entries("old-message")[0].expense == expense and boundary.calls == []
    finally:
        old.close()


@pytest.mark.parametrize("unique_type", [None, "number", "text"])
async def test_startup_requires_existing_text_unique_id_without_modifying_schema(
    settings, audit, monkeypatch, unique_type
):
    ledger = LarkCliLedger(settings, audit)
    calls = []
    kinds = ("datetime", "select", "select", "select", "text", "number")
    fields = [
        {"name": name, "type": kind}
        for name, kind in zip(settings.writable_fields, kinds, strict=True)
    ]
    if unique_type is not None:
        fields.append({"name": "unique_id", "type": unique_type})

    async def run(action, args):
        calls.append((action, args[:2]))
        return ProcessResult(0, json.dumps({"ok": True, "data": {"fields": fields}}))

    monkeypatch.setattr(ledger, "_run", run)
    if unique_type == "text":
        await ledger.check_schema()
    else:
        with pytest.raises(ValueError, match="unique_id"):
            await ledger.check_schema()
    assert calls == [("schema", ["base", "+field-list"])]


def test_uuid_is_not_a_model_argument():
    assert "unique_id" not in ExpenseProposal.model_fields
    with pytest.raises(ValidationError):
        ExpenseProposal(**proposal().model_dump(), unique_id="model-supplied")


async def test_distinct_occurrences_get_distinct_program_ids(
    settings, audit, journal, knowledge, boundary
):
    rows = [proposal("买咖啡18元", date_evidence=None, source_occurrence=i) for i in (1, 2)]
    script = ScriptedModel(
        [[("add_expenses", {"expenses": [row.model_dump() for row in rows]})], [("finish", {})]]
    )
    boundary.plan = [(True, None), (True, None)]
    service = BookkeepingService(
        settings, create_agent(script.model), boundary.ledger, knowledge, journal, audit
    )
    report = await service.handle("two", "chat", "早上买咖啡18元，下午买咖啡18元", today=TODAY)
    assert [entry.outcome.status for entry in report.entries] == ["success", "success"]
    ids = [row["unique_id"] for row in boundary.attempts]
    assert len(set(ids)) == 2 and all(UUID(uid).version == 4 for uid in ids)


async def test_late_original_and_supplement_may_both_commit_without_second_query(
    settings, audit, journal, knowledge, boundary, monkeypatch
):
    boundary.plan = [(False, UNKNOWN), (True, None)]
    original = boundary.run

    async def late_commit(action, args):
        if action == "create" and len(boundary.attempts) == 1:
            boundary.records.append({"record_id": "recLateOriginal", **boundary.attempts[0]})
        return await original(action, args)

    monkeypatch.setattr(boundary.ledger, "_run", late_commit)
    _, _, report = await run_message(settings, audit, journal, knowledge, boundary)
    assert report.entries[0].outcome.status == "success"
    assert len(boundary.records) == 2
    assert boundary.records[0]["unique_id"] == boundary.records[1]["unique_id"]
    assert boundary.calls == ["create", "reconcile", "create"] and boundary.waits == [3]
