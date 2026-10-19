"""Independent offline business acceptance; external boundaries only are simulated.

Run this file explicitly. It intentionally does not import tests.support or reuse
developer fixtures. Expected business results are stated in the individual tests.
"""

import argparse
import asyncio
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import RFC_4122, UUID

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

import bookkeeping
from bookkeeping import tools as business_tools
from bookkeeping.agent import add_expenses as registered_add
from bookkeeping.agent import create_agent
from bookkeeping.audit import Audit
from bookkeeping.cards import CardReporter
from bookkeeping.cli import LarkCliLedger, ProcessResult
from bookkeeping.config import Settings
from bookkeeping.domain import ExpenseProposal
from bookkeeping.journal import Journal
from bookkeeping.knowledge import Knowledge
from bookkeeping.service import BookkeepingService
from bookkeeping.tools import RunDeps

ROOT = Path(__file__).resolve().parents[1]
DAY = date(2027, 1, 2)
# Wire fixtures use a different field order from both Settings and create payloads.
READ_COLUMNS = ["unique_id", "金额", "三级分类", "记账日期", "二级分类", "具体", "一级分类"]
FIELD_TYPES = {
    "记账日期": "datetime",
    "一级分类": "select",
    "二级分类": "select",
    "三级分类": "select",
    "具体": "text",
    "金额": "number",
    "unique_id": "text",
}


def envelope(data):
    return ProcessResult(0, json.dumps({"ok": True, "data": data}, ensure_ascii=False))


def proposal(source="昨天乘地铁花了3元", **changes):
    return {
        "source_text": source,
        "description": "乘地铁",
        "amount": "3",
        "amount_evidence": "3元",
        "date_evidence": "昨天",
        "expense_date": "2027-01-01",
        "category_l1": "出行",
        "category_l2": "公共交通",
        "category_l3": "地铁",
        "classification_basis": "已发生的地铁车费，使用合成地铁规则",
        "rule_ids": ["ACCEPT_SUBWAY"],
        **changes,
    }


def add_turn(*items):
    return [("add_expenses", {"expenses": list(items)})]


def finish_turn(**values):
    return [("Decision", values)]


class AcceptanceChat(BaseChatModel):
    """A local async-only model boundary with an explicit, finite conversation."""

    turns: list[Any] = Field(exclude=True)
    requests: int = 0
    seen: list[Any] = Field(default_factory=list, exclude=True)

    @property
    def _llm_type(self):
        return "independent-acceptance-offline"

    def bind_tools(self, tools, **kwargs):
        return self.bind(**kwargs)

    def _generate(self, messages, **kwargs):
        raise AssertionError("Acceptance must use the actual async agent path")

    async def _agenerate(self, messages, **kwargs):
        self.seen.append([message.model_copy(deep=True) for message in messages])
        index = self.requests
        self.requests += 1
        if index >= len(self.turns):
            raise AssertionError("Unexpected extra model request")
        step = self.turns[index]
        if isinstance(step, Exception):
            raise step
        message = AIMessage(
            content="",
            tool_calls=[
                {"name": name, "args": args, "id": f"acceptance{index}call{number}"}
                for number, (name, args) in enumerate(step)
            ],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


class SyntheticPowerLoss(BaseException):
    """Bypasses the ordinary task failure/cancellation path, as abrupt exit would."""


class OfflineProcessBoundary:
    """A bag of remote rows: duplicate IDs are expressly permitted.

    The object consumes actual CLI argv and returns serialized process output.
    It never calls production field conversion, receipt classification or match
    logic to construct expected results.
    """

    def __init__(self, settings, database):
        self.settings = settings
        self.database = database
        self.remote = []
        self.trace = []
        self.plans = {}
        self.counts = {}
        self.lookup_transform = None
        self.schema = dict(FIELD_TYPES)
        self.last_uid = None
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.block_phase = None
        self.crash_phase = None
        self.after_lookup = None

    def saved(self, uid):
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM writes WHERE unique_id=?", (uid,)).fetchone()
            assert row is not None, "The ID must be committed before any remote operation"
            result = dict(row)
        result["expense"] = json.loads(result["expense"])
        return result

    def append(self, fields):
        record = {"id": f"recAcceptance{len(self.remote) + 1:04d}", "fields": deepcopy(fields)}
        self.remote.append(record)
        return record["id"]

    async def phase(self, phase):
        if phase == self.crash_phase:
            raise SyntheticPowerLoss(phase)
        if phase == self.block_phase:
            self.entered.set()
            await self.release.wait()

    async def run(self, action, argv):
        assert argv[argv.index("--base-token") + 1] == "synthetic-acceptance-base"
        assert argv[argv.index("--table-id") + 1] == "tblAcceptanceOnly"
        assert argv[argv.index("--as") + 1] == self.settings.lark_cli_identity
        assert "--view-id" not in argv
        event = {"action": action, "argv": list(argv), "time": time.monotonic()}
        self.trace.append(event)
        if action == "schema":
            assert argv[:2] == ["base", "+field-list"]
            return envelope(
                {"fields": [{"name": name, "type": kind} for name, kind in self.schema.items()]}
            )
        if action == "query":
            assert argv[:2] == ["base", "+record-list"]
            return envelope(
                {
                    "fields": ["具体", "一级分类", "二级分类", "三级分类"],
                    "data": [["月票", ["出行"], ["公共交通"], ["地铁"]]],
                    "has_more": False,
                }
            )
        if action == "create":
            assert argv[:2] == ["base", "+record-batch-create"]
            payload = json.loads(argv[argv.index("--json") + 1])
            assert set(payload) == {"create_records"} and len(payload["create_records"]) == 1
            fields = payload["create_records"][0]
            uid = fields["unique_id"]
            parsed = UUID(uid)
            assert parsed.version == 4 and parsed.variant == RFC_4122
            saved = self.saved(uid)
            snapshot = saved["expense"]
            # Independent projection from the durable, validated business snapshot.
            expected = {
                "记账日期": snapshot["expense_date"] + "T00:00:00+08:00",
                "一级分类": [snapshot["category_l1"]],
                "二级分类": [snapshot["category_l2"]],
                "具体": snapshot["description"],
                "金额": float(Decimal(snapshot["amount"])),
                "unique_id": uid,
            }
            if snapshot["category_l3"] is not None:
                expected["三级分类"] = [snapshot["category_l3"]]
            assert fields == expected
            self.last_uid = uid
            number = self.counts.get(uid, 0) + 1
            self.counts[uid] = number
            event.update(uid=uid, fields=deepcopy(fields), persisted=saved, attempt=number)
            assert saved["state"] == ("submitted" if number == 1 else "uncertain")
            await self.phase(f"create:{number}")
            plan = self.plans.get(fields["具体"], ["accept"])
            assert number <= len(plan), "More write attempts than the scenario permits"
            behavior = plan[number - 1]
            if behavior in {"accept", "apply_timeout"}:
                row_id = self.append(fields)
                if behavior == "accept":
                    return envelope({"record_id_list": [row_id]})
            if behavior in {"apply_timeout", "drop_timeout"}:
                return ProcessResult(-9, timeout=True)
            if behavior == "deny":
                return ProcessResult(1, '{"ok":false,"error":{"type":"authorization"}}')
            if behavior == "launch_error":
                return ProcessResult(None, launched=False)
            if behavior == "broken_success":
                return envelope({"record_id_list": []})
            if behavior == "unknown_error":
                return ProcessResult(1, '{"ok":false,"error":{"type":"transport"}}')
            if behavior == "raise":
                raise RuntimeError("synthetic post-submission transport exception")
            if behavior == "not_json":
                return ProcessResult(0, "synthetic truncated output")
            if behavior == "ok_nonzero":
                return ProcessResult(8, '{"ok":true,"data":{"record_id_list":["recUnused"]}}')
            raise AssertionError(behavior)
        assert action == "reconcile", f"Unplanned external action: {action}"
        assert argv[:2] == ["base", "+record-list"]
        exact_filter = json.loads(argv[argv.index("--filter-json") + 1])
        assert exact_filter == {"logic": "and", "conditions": [["unique_id", "==", self.last_uid]]}
        requested = [argv[index + 1] for index, arg in enumerate(argv) if arg == "--field-id"]
        assert set(requested) == set(READ_COLUMNS)
        saved = self.saved(self.last_uid)
        assert saved["state"] == "uncertain"
        event.update(uid=self.last_uid, persisted=saved)
        await self.phase("reconcile")
        matching = [row for row in self.remote if row["fields"].get("unique_id") == self.last_uid]
        limit = int(argv[argv.index("--limit") + 1])
        data = {
            "fields": list(READ_COLUMNS),
            "data": [
                [row["fields"].get(name) for name in READ_COLUMNS] for row in matching[:limit]
            ],
            "record_id_list": [row["id"] for row in matching[:limit]],
            "has_more": len(matching) > limit,
        }
        body = {"ok": True, "data": data}
        if self.lookup_transform:
            body = self.lookup_transform(deepcopy(body), self)
        if self.after_lookup:
            self.after_lookup(self)
        event["response"] = body if isinstance(body, dict) else repr(body)
        if isinstance(body, ProcessResult):
            return body
        return ProcessResult(0, json.dumps(body, ensure_ascii=False))


class OfflineCli(LarkCliLedger):
    def __init__(self, settings, audit, boundary):
        super().__init__(settings, audit)
        self.boundary = boundary

    async def _run(self, action, args):
        return await self.boundary.run(action, args)


class ChannelBoundary:
    def __init__(self):
        self.payloads = []
        self.fail_finals = 0

    def respond(self, payload):
        self.payloads.append(deepcopy(payload))
        is_final = payload["header"]["title"]["content"].startswith("记账结果")
        fail = is_final and self.fail_finals > 0
        if fail:
            self.fail_finals -= 1
        return SimpleNamespace(success=not fail, message_id="omSyntheticCard", error=None)

    async def send(self, chat_id, content, options):
        assert chat_id == "ocSyntheticAcceptance"
        assert options["reply_to"].startswith("omAcceptance")
        return self.respond(content["card"])

    async def update_card(self, message_id, payload):
        assert message_id == "omSyntheticCard"
        return self.respond(payload)

    @property
    def finals(self):
        return [
            payload
            for payload in self.payloads
            if payload["header"]["title"]["content"].startswith("记账结果")
        ]


class ClockBoundary:
    def __init__(self, rig):
        self.rig = rig
        self.calls = []
        self.block = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.hook = None
        self.crash = False

    async def sleep(self, seconds):
        saved = self.rig.boundary.saved(self.rig.boundary.last_uid)
        self.calls.append(
            {"seconds": seconds, "persisted": saved, "model_requests": self.rig.model.requests}
        )
        assert seconds == 3 and saved["state"] == "uncertain"
        if self.hook:
            self.hook()
        if self.crash:
            raise SyntheticPowerLoss("wait")
        if self.block:
            self.entered.set()
            await self.release.wait()
        await asyncio.sleep(0)

    def __getattr__(self, name):
        return getattr(asyncio, name)


class AcceptanceRig:
    def __init__(self, temporary, monkeypatch):
        self.settings = Settings(
            _env_file=None,
            _env_prefix="INDEPENDENT_ACCEPTANCE_UNUSED_",
            lark_app_id="cliAcceptanceSynthetic",
            lark_app_secret="synthetic-secret",
            ark_api_key="synthetic-key",
            ark_model="synthetic-offline-model",
            bitable_base_token="synthetic-acceptance-base",
            bitable_table_id="tblAcceptanceOnly",
            bitable_view_id="vewMustNotLimitReconciliation",
            lark_cli_identity="bot",
            lark_cli_path=sys.executable,
            project_dir=temporary,
            runtime_dir=temporary / "runtime",
            task_timeout_seconds=20,
        )
        folder = temporary / "knowledges"
        folder.mkdir()
        categories = {
            "version": "acceptance-only",
            "table_id": "tblAcceptanceOnly",
            "tree": [
                {
                    "name": "出行",
                    "children": [
                        {"name": "公共交通", "children": [{"name": "地铁", "children": []}]}
                    ],
                },
                {"name": "日常", "children": [{"name": "餐食", "children": []}]},
                {"name": "学习", "children": [{"name": "资料", "children": []}]},
            ],
        }
        rules = {
            "version": "acceptance-only",
            "compatible_categories_version": "acceptance-only",
            "rules": [
                {"id": "ACCEPT_SUBWAY", "category": ["出行", "公共交通", "地铁"]},
                {"id": "ACCEPT_MEAL", "category": ["日常", "餐食", None]},
                {"id": "ACCEPT_BOOK", "category": ["学习", "资料", None]},
            ],
        }
        for name, body in [("categories", categories), ("rules", rules)]:
            (folder / f"{name}.json").write_text(
                json.dumps(body, ensure_ascii=False), encoding="utf-8"
            )
        self.knowledge = Knowledge(temporary, "tblAcceptanceOnly")
        self.audit = Audit(
            self.settings.private_dir / "acceptance-events.jsonl", self.settings.secrets
        )
        self.database = self.settings.private_dir / "acceptance.sqlite3"
        self.journal = Journal(self.database, "independent-offline-acceptance")
        self.boundary = OfflineProcessBoundary(self.settings, self.database)
        self.ledger = OfflineCli(self.settings, self.audit, self.boundary)
        self.channel = ChannelBoundary()
        self.model = AcceptanceChat(turns=[])
        self.clock = ClockBoundary(self)
        monkeypatch.setattr(business_tools, "asyncio", self.clock)
        self.service = None

    def start(self, turns):
        self.model = AcceptanceChat(turns=turns)
        reporter = CardReporter(self.channel, self.journal, self.audit)
        self.service = BookkeepingService(
            self.settings,
            create_agent(self.model, self.settings),
            self.ledger,
            self.knowledge,
            self.journal,
            self.audit,
            reporter,
        )
        return self.service

    async def run(self, text="昨天乘地铁花了3元", turns=None, message_id="omAcceptanceMessage"):
        service = self.start(turns or [add_turn(proposal()), finish_turn()])
        return await service.handle(message_id, "ocSyntheticAcceptance", text, today=DAY)

    def reopen(self):
        self.journal.close()
        self.journal = Journal(self.database, "independent-offline-acceptance")

    def evidence(self):
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            messages = [dict(row) for row in connection.execute("SELECT * FROM messages")]
            writes = [dict(row) for row in connection.execute("SELECT * FROM writes")]
        return {
            "trace": self.boundary.trace,
            "remote_rows": self.boundary.remote,
            "waits": self.clock.calls,
            "model_requests": self.model.requests,
            "sqlite_messages": messages,
            "sqlite_writes": writes,
            "final_cards": self.channel.finals,
        }


@pytest.fixture(autouse=True)
def acceptance_offline(monkeypatch):
    attempts = []

    def blocked(*args, **kwargs):
        attempts.append(True)
        raise AssertionError("Independent acceptance forbids network and real child processes")

    for name in ["connect", "connect_ex"]:
        monkeypatch.setattr(socket.socket, name, blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", blocked)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", blocked)
    yield
    assert not attempts, "Even a caught external-I/O attempt invalidates offline acceptance"


@pytest.fixture(scope="session")
def evidence_sink():
    filename = os.environ.get("BOOKKEEPING_ACCEPTANCE_EVIDENCE")
    if not filename:
        return None
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    code = Path(bookkeeping.__file__).resolve()
    assert code.parent == ROOT / "src" / "bookkeeping", "Wrong checkout imported"
    stamp = {
        "kind": "provenance",
        "root": str(ROOT),
        "imported": str(code),
        "python": sys.executable,
        "sha256": {
            str(item.relative_to(ROOT)): hashlib.sha256(item.read_bytes()).hexdigest()
            for item in sorted((ROOT / "src" / "bookkeeping").glob("*.py"))
        },
    }
    path.write_text(json.dumps(stamp, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def acceptance_rig(tmp_path, monkeypatch, request, evidence_sink):
    rig = AcceptanceRig(tmp_path, monkeypatch)
    yield rig
    if evidence_sink:
        with evidence_sink.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {"test": request.node.nodeid, **rig.evidence()}, ensure_ascii=False, default=str
                )
                + "\n"
            )
    rig.journal.close()
    rig.audit.close()


def actions(rig):
    return [event["action"] for event in rig.boundary.trace]


def statuses(report):
    return [entry.outcome.status for entry in report.entries]


def card_text(rig):
    return "\n".join(payload["body"]["elements"][0]["content"] for payload in rig.channel.finals)


async def test_mixed_message_keeps_each_business_outcome_and_durable_notification(acceptance_rig):
    rig = acceptance_rig
    metro = proposal()
    meal = proposal(
        "午餐花了22.4元",
        description="午餐",
        amount="22.40",
        amount_evidence="22.4元",
        date_evidence=None,
        expense_date=None,
        category_l1="日常",
        category_l2="餐食",
        category_l3=None,
        rule_ids=["ACCEPT_MEAL"],
    )
    book = proposal(
        "买参考书72元",
        description="参考书",
        amount="72",
        amount_evidence="72元",
        date_evidence=None,
        expense_date=None,
        category_l1="学习",
        category_l2="资料",
        category_l3=None,
        rule_ids=["ACCEPT_BOOK"],
    )
    denied = {
        **metro,
        "source_text": "晚间乘地铁花了4元",
        "amount": "4",
        "amount_evidence": "4元",
        "description": "晚间乘地铁",
        "date_evidence": None,
        "expense_date": None,
    }
    conflicting = {
        **metro,
        "source_text": "返程乘地铁花了5元",
        "amount": "5",
        "amount_evidence": "5元",
        "description": "返程乘地铁",
        "date_evidence": None,
        "expense_date": None,
    }
    invalid = {
        **meal,
        "source_text": "加餐花了0元",
        "description": "加餐",
        "amount": "0",
        "amount_evidence": "0元",
    }
    foreign = {
        **book,
        "source_text": "买电子书8美元",
        "description": "电子书",
        "amount": "8",
        "amount_evidence": "8美元",
    }
    text = (
        "；".join(
            item["source_text"]
            for item in [metro, meal, book, denied, conflicting, invalid, foreign]
        )
        + "；买文具忘记了金额"
    )
    rig.boundary.plans = {
        "午餐": ["apply_timeout"],
        "参考书": ["drop_timeout", "accept"],
        "晚间乘地铁": ["deny"],
        "返程乘地铁": ["apply_timeout"],
    }

    def conflict(body, boundary):
        if boundary.saved(boundary.last_uid)["expense"]["description"] == "返程乘地铁":
            index = body["data"]["fields"].index("金额")
            body["data"]["data"][0][index] = 500
        return body

    rig.boundary.lookup_transform = conflict
    rig.channel.fail_finals = 1
    report = await rig.run(
        text,
        [
            add_turn(metro, meal, book, denied, conflicting, invalid, foreign),
            finish_turn(skipped=[{"source_text": "买文具忘记了金额", "reason": "实付金额缺失"}]),
        ],
    )
    assert statuses(report) == ["success", "success", "success", "failed", "uncertain"]
    assert [
        (entry.expense.description, str(entry.expense.amount), str(entry.expense.expense_date))
        for entry in report.entries
    ] == [
        ("乘地铁", "3", "2027-01-01"),
        ("午餐", "22.40", "2027-01-02"),
        ("参考书", "72", "2027-01-02"),
        ("晚间乘地铁", "4", "2027-01-02"),
        ("返程乘地铁", "5", "2027-01-02"),
    ]
    assert {item.source_text for item in report.skipped} == {
        "买文具忘记了金额",
        "加餐花了0元",
        "买电子书8美元",
    }
    assert actions(rig).count("create") == 6 and actions(rig).count("reconcile") == 3
    assert len(rig.boundary.counts) == 5 and len(rig.boundary.remote) == 4
    assert rig.model.requests == 2
    assert rig.journal.report("omAcceptanceMessage") == report
    assert rig.journal.pending_notifications() == ["omAcceptanceMessage"]
    before = deepcopy(rig.boundary.trace)
    await rig.service.deliver("omAcceptanceMessage")
    assert rig.journal.pending_notifications() == []
    assert rig.boundary.trace == before and rig.model.requests == 2
    assert rig.channel.finals[0] == rig.channel.finals[1]
    text = card_text(rig)
    assert all(
        label in text for label in ["已记账消费记录", "不记账消费记录", "以下消费记录待核对"]
    )
    assert all(item in text for item in ["22.40 元", "72.00 元", "地铁", "实付金额缺失"])


@pytest.mark.parametrize(
    "behavior,expected", [("accept", "success"), ("deny", "failed"), ("launch_error", "failed")]
)
async def test_definite_initial_receipts_finish_without_wait_or_query(
    acceptance_rig, behavior, expected
):
    rig = acceptance_rig
    rig.boundary.plans["乘地铁"] = [behavior]
    report = await rig.run()
    assert statuses(report) == [expected]
    assert actions(rig) == ["create"] and rig.clock.calls == []
    assert rig.model.requests == 2


@pytest.mark.parametrize(
    "behavior", ["broken_success", "unknown_error", "raise", "not_json", "ok_nonzero"]
)
async def test_ambiguous_initial_receipt_can_retry_only_after_confirmed_absence(
    acceptance_rig, behavior
):
    rig = acceptance_rig
    rig.boundary.plans["乘地铁"] = [behavior, "accept"]
    report = await rig.run()
    assert statuses(report) == ["success"]
    assert actions(rig) == ["create", "reconcile", "create"]
    assert len(rig.clock.calls) == 1 and len(rig.boundary.remote) == 1
    attempts = [event for event in rig.boundary.trace if event["action"] == "create"]
    assert attempts[0]["fields"] == attempts[1]["fields"]
    assert report.entries[0].outcome.record_id == rig.boundary.remote[0]["id"]


@pytest.mark.parametrize(
    "difference", ["description", "amount", "date", "level1", "level2", "level3", "id"]
)
async def test_one_row_with_a_business_conflict_remains_pending(acceptance_rig, difference):
    rig = acceptance_rig
    rig.boundary.plans["乘地铁"] = ["apply_timeout"]
    changes = {
        "description": ("具体", "他人地铁"),
        "amount": ("金额", 30),
        "date": ("记账日期", "2027-01-02T00:00:00+08:00"),
        "level1": ("一级分类", ["学习"]),
        "level2": ("二级分类", ["资料"]),
        "level3": ("三级分类", []),
        "id": ("unique_id", "synthetic-unrelated-id"),
    }

    def change(body, boundary):
        name, value = changes[difference]
        body["data"]["data"][0][body["data"]["fields"].index(name)] = value
        return body

    rig.boundary.lookup_transform = change
    report = await rig.run()
    assert statuses(report) == ["uncertain"] and report.entries[0].outcome.record_id is None
    assert actions(rig) == ["create", "reconcile"] and len(rig.clock.calls) == 1
    assert "待核对" in card_text(rig)


@pytest.mark.parametrize(
    "defect",
    [
        "duplicate",
        "third_match",
        "query_denied",
        "timeout",
        "not_json",
        "incomplete",
        "missing_completion",
        "missing_field",
        "bad_row_id",
        "bad_matrix",
        "id_count",
        "exception",
    ],
)
async def test_unsafe_lookup_cannot_authorize_a_second_write(acceptance_rig, defect):
    rig = acceptance_rig
    rig.boundary.plans["乘地铁"] = ["apply_timeout"]

    def corrupt(body, boundary):
        data = body["data"]
        if defect in {"duplicate", "third_match"}:
            data["data"].append(deepcopy(data["data"][0]))
            data["record_id_list"].append("recAnotherSynthetic")
            boundary.append(boundary.remote[0]["fields"])
            if defect == "third_match":
                data["has_more"] = True
                boundary.append(boundary.remote[0]["fields"])
        elif defect == "query_denied":
            return ProcessResult(1, '{"ok":false,"error":{"type":"authorization"}}')
        elif defect == "timeout":
            return ProcessResult(None, timeout=True)
        elif defect == "not_json":
            return ProcessResult(0, '{"ok":true,"data":')
        elif defect == "incomplete":
            data.update(data=[], record_id_list=[], has_more=True)
        elif defect == "missing_completion":
            data.update(data=[], record_id_list=[])
            del data["has_more"]
        elif defect == "missing_field":
            data["fields"].pop()
            data["data"][0].pop()
        elif defect == "bad_row_id":
            data["record_id_list"] = ["invalid-row-id"]
        elif defect == "bad_matrix":
            data["data"] = [{"金额": 3}]
        elif defect == "id_count":
            data["record_id_list"] = []
        elif defect == "exception":
            raise RuntimeError("synthetic query transport failure")
        return body

    rig.boundary.lookup_transform = corrupt
    report = await rig.run()
    assert statuses(report) == ["uncertain"]
    assert actions(rig) == ["create", "reconcile"]
    assert len(rig.clock.calls) == 1 and rig.model.requests == 2


@pytest.mark.parametrize("absent_third", [None, []])
async def test_unique_matching_row_accepts_same_local_day_and_numeric_amount(
    acceptance_rig, absent_third
):
    rig = acceptance_rig
    meal = proposal(
        "昨天晚餐16元",
        description="晚餐",
        amount="16.00",
        amount_evidence="16元",
        category_l1="日常",
        category_l2="餐食",
        category_l3=None,
        rule_ids=["ACCEPT_MEAL"],
    )
    rig.boundary.plans["晚餐"] = ["apply_timeout"]

    def equivalent(body, boundary):
        values = {"记账日期": "2026-12-31T16:40:00+00:00", "金额": 16, "三级分类": absent_third}
        for key, value in values.items():
            body["data"]["data"][0][body["data"]["fields"].index(key)] = value
        return body

    rig.boundary.lookup_transform = equivalent
    report = await rig.run(meal["source_text"], [add_turn(meal), finish_turn()])
    assert statuses(report) == ["success"]
    assert actions(rig) == ["create", "reconcile"]
    assert report.entries[0].outcome.record_id == "recAcceptance0001"


@pytest.mark.parametrize("retry", ["deny", "drop_timeout", "broken_success", "raise"])
async def test_unconfirmed_retry_does_not_disprove_the_first_submission(acceptance_rig, retry):
    rig = acceptance_rig
    rig.boundary.plans["乘地铁"] = ["drop_timeout", retry]

    # Original request may complete after the complete empty lookup, even if retry is denied.
    def original_arrives_late(boundary):
        boundary.append(
            next(event["fields"] for event in boundary.trace if event["action"] == "create")
        )

    rig.boundary.after_lookup = original_arrives_late
    report = await rig.run()
    assert statuses(report) == ["uncertain"]
    assert actions(rig) == ["create", "reconcile", "create"]
    assert len(rig.clock.calls) == 1 and len(rig.boundary.remote) == 1
    assert "不记账消费记录" not in card_text(rig) and "待核对" in card_text(rig)


async def test_snapshot_survives_clock_and_knowledge_changes_without_revalidation(acceptance_rig):
    rig = acceptance_rig
    rig.boundary.plans["乘地铁"] = ["drop_timeout", "accept"]
    validation_calls = []
    original_validate = rig.knowledge.validate

    def observe(*args, **kwargs):
        validation_calls.append(True)
        return original_validate(*args, **kwargs)

    rig.knowledge.validate = observe

    def change_context():
        rig.knowledge.paths.clear()
        rig.knowledge.rule_map.clear()
        rig.service.today = lambda: date(2027, 2, 5)

    rig.clock.hook = change_context
    report = await rig.run()
    assert statuses(report) == ["success"] and validation_calls == [True]
    attempts = [event for event in rig.boundary.trace if event["action"] == "create"]
    assert attempts[0]["fields"] == attempts[1]["fields"]
    assert report.entries[0].expense.expense_date == date(2027, 1, 1)
    assert rig.model.requests == 2


@pytest.mark.parametrize(
    "behavior,expected",
    [("apply_timeout", "success"), ("deny", "failed"), ("drop_timeout", "uncertain")],
)
async def test_reentering_the_same_span_reuses_the_saved_submission(
    acceptance_rig, behavior, expected
):
    rig = acceptance_rig
    rig.boundary.plans["乘地铁"] = [behavior]
    if behavior == "drop_timeout":
        rig.boundary.lookup_transform = lambda body, boundary: ProcessResult(None, timeout=True)
    changed = proposal(expense_date="2040-09-09", category_l1="不存在", rule_ids=["INVALID_RULE"])
    alias = {**changed, "source_text": "乘地铁花了3元"}
    report = await rig.run(
        turns=[add_turn(proposal()), add_turn(changed), add_turn(alias), finish_turn()]
    )
    assert statuses(report) == [expected]
    assert actions(rig).count("create") == 1
    assert len(rig.clock.calls) == (0 if behavior == "deny" else 1)
    assert report.entries[0].expense.category_path == ("出行", "公共交通", "地铁")
    receipts = [
        json.loads(message.content)
        for message in rig.model.seen[-1]
        if isinstance(message, ToolMessage) and message.name == "add_expenses"
    ]
    assert receipts[1][0]["already_submitted"] and receipts[2][0]["already_submitted"]
    before = deepcopy(rig.boundary.trace)
    assert (
        await rig.service.handle(
            "omAcceptanceMessage",
            "ocSyntheticAcceptance",
            "changed duplicate",
            today=date(2027, 5, 5),
        )
        == report
    )
    assert rig.model.requests == 4 and rig.boundary.trace == before


async def test_distinct_occurrences_and_new_messages_get_independent_ids(acceptance_rig):
    rig = acceptance_rig
    source = "地铁车费3元"
    first = proposal(source, description="地铁车费", date_evidence=None, expense_date=None)
    second = {**first, "source_occurrence": 2}
    report = await rig.run(source + "；" + source, [add_turn(first, second), finish_turn()])
    assert statuses(report) == ["success", "success"]
    rig.start([add_turn(first), finish_turn()])
    report2 = await rig.service.handle(
        "omAcceptanceNewMessage", "ocSyntheticAcceptance", source, today=DAY
    )
    assert statuses(report2) == ["success"]
    ids = [event["uid"] for event in rig.boundary.trace if event["action"] == "create"]
    assert len(ids) == len(set(ids)) == 3
    assert len(rig.boundary.remote) == 3


@pytest.mark.parametrize("stage", ["create:1", "wait", "reconcile", "create:2"])
async def test_cancellation_preserves_pending_result_without_restart_replay(acceptance_rig, stage):
    rig = acceptance_rig
    rig.boundary.plans["乘地铁"] = ["drop_timeout", "accept"]
    rig.start([add_turn(proposal()), finish_turn()])
    rig.service.accept("omAcceptanceCancelled", "ocSyntheticAcceptance", "昨天乘地铁花了3元")
    if stage == "wait":
        rig.clock.block = True
        entered = rig.clock.entered
    else:
        rig.boundary.block_phase = stage
        entered = rig.boundary.entered
    task = asyncio.create_task(rig.service.process("omAcceptanceCancelled", today=DAY))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    report = rig.journal.report("omAcceptanceCancelled")
    assert statuses(report) == ["uncertain"] and report.notice
    assert rig.channel.finals == []
    before = deepcopy(rig.boundary.trace)
    rig.reopen()
    assert rig.journal.recover_interrupted() == []
    rig.start([])
    await rig.service.deliver("omAcceptanceCancelled")
    assert statuses(rig.journal.report("omAcceptanceCancelled")) == ["uncertain"]
    assert rig.boundary.trace == before and rig.model.requests == 0
    assert "待核对" in card_text(rig)


@pytest.mark.parametrize("stage", ["create:1", "wait"])
async def test_abrupt_exit_recovers_only_report_and_notifications(acceptance_rig, stage):
    rig = acceptance_rig
    rig.boundary.plans["乘地铁"] = ["drop_timeout"]
    if stage == "wait":
        rig.clock.crash = True
    else:
        rig.boundary.crash_phase = stage
    with pytest.raises(SyntheticPowerLoss):
        await rig.run()
    assert rig.journal.message("omAcceptanceMessage").state == "running"
    assert rig.journal.report("omAcceptanceMessage") is None
    before = deepcopy(rig.boundary.trace)
    rig.reopen()
    assert rig.journal.recover_interrupted() == ["omAcceptanceMessage"]
    report = rig.journal.report("omAcceptanceMessage")
    assert statuses(report) == ["uncertain"]
    rig.start([])
    await rig.service.deliver("omAcceptanceMessage")
    assert await rig.service.process("omAcceptanceMessage", today=DAY) == report
    assert rig.boundary.trace == before and rig.model.requests == 0


@pytest.mark.parametrize("old_status", ["submitted", "success", "failed", "uncertain"])
async def test_old_database_adds_nullable_id_without_inventing_or_replaying(
    acceptance_rig, old_status
):
    rig = acceptance_rig
    rig.journal.close()
    with sqlite3.connect(rig.database) as connection:
        connection.execute("DROP TABLE writes")
        connection.execute("""CREATE TABLE writes (
            message_id TEXT NOT NULL, source_start INTEGER NOT NULL, source_end INTEGER NOT NULL,
            expense TEXT NOT NULL, state TEXT NOT NULL, record_id TEXT, reason TEXT NOT NULL,
            PRIMARY KEY(message_id, source_start, source_end))""")
        connection.execute(
            "INSERT INTO messages(message_id,chat_id,text,execution_date,state) VALUES (?,?,?,?,?)",
            (
                "omAcceptanceLegacy",
                "ocSyntheticAcceptance",
                "昨天乘地铁花了3元",
                "2027-01-02",
                "running",
            ),
        )
        old_snapshot = {
            "source_text": "昨天乘地铁花了3元",
            "source_occurrence": 1,
            "description": "乘地铁",
            "amount": "3",
            "expense_date": "2027-01-01",
            "category_l1": "出行",
            "category_l2": "公共交通",
            "category_l3": "地铁",
        }
        connection.execute(
            "INSERT INTO writes VALUES (?,?,?,?,?,?,?)",
            (
                "omAcceptanceLegacy",
                0,
                len(old_snapshot["source_text"]),
                json.dumps(old_snapshot, ensure_ascii=False),
                old_status,
                "recOldSynthetic" if old_status == "success" else None,
                "old saved result",
            ),
        )
    rig.journal = Journal(rig.database, "independent-offline-acceptance")
    with sqlite3.connect(rig.database) as connection:
        assert connection.execute("SELECT unique_id FROM writes").fetchone() == (None,)
    assert rig.journal.recover_interrupted() == ["omAcceptanceLegacy"]
    report = rig.journal.report("omAcceptanceLegacy")
    expected_status = "uncertain" if old_status == "submitted" else old_status
    assert statuses(report) == [expected_status]

    async def no_progress(text):
        pass

    deps = RunDeps(
        "omAcceptanceLegacy",
        "昨天乘地铁花了3元",
        DAY,
        rig.journal,
        rig.ledger,
        rig.knowledge,
        rig.audit,
        no_progress,
    )
    result = await business_tools.add_expenses(deps, [ExpenseProposal(**proposal())])
    assert result[0]["already_submitted"] and result[0]["status"] == expected_status
    rig.start([])
    await rig.service.deliver("omAcceptanceLegacy")
    assert actions(rig) == [] and rig.model.requests == 0
    with sqlite3.connect(rig.database) as connection:
        assert connection.execute("SELECT unique_id FROM writes").fetchone() == (None,)


@pytest.mark.parametrize("kind", ["text", "number", "missing"])
async def test_startup_requires_existing_text_field_without_schema_mutation(
    acceptance_rig, monkeypatch, kind
):
    from bookkeeping import __main__ as application

    rig = acceptance_rig
    if kind == "missing":
        del rig.boundary.schema["unique_id"]
    else:
        rig.boundary.schema["unique_id"] = kind
    before = dict(rig.boundary.schema)
    monkeypatch.setattr(application, "LarkCliLedger", lambda *args: rig.ledger)
    command = "doctor" if kind == "text" else "start"
    args = argparse.Namespace(command=command, websocket=False)
    if kind == "text":
        await application.run(args, rig.settings, rig.audit)
    else:
        with pytest.raises(ValueError, match="unique_id"):
            await application.run(args, rig.settings, rig.audit)
    assert actions(rig) == ["schema"] and rig.boundary.schema == before
    assert rig.model.requests == 0


async def test_accepted_late_original_and_successful_retry_can_both_remain(acceptance_rig):
    rig = acceptance_rig
    rig.boundary.plans["乘地铁"] = ["drop_timeout", "accept"]

    def late_original(boundary):
        boundary.append(
            next(event["fields"] for event in boundary.trace if event["action"] == "create")
        )

    rig.boundary.after_lookup = late_original
    report = await rig.run()
    assert statuses(report) == ["success"]
    assert len(rig.boundary.remote) == 2
    assert rig.boundary.remote[0]["fields"] == rig.boundary.remote[1]["fields"]
    assert actions(rig) == ["create", "reconcile", "create"]
    assert report.entries[0].outcome.record_id == rig.boundary.remote[1]["id"]


async def test_real_three_second_wait_yields_and_precedes_the_single_lookup(
    acceptance_rig, monkeypatch
):
    rig = acceptance_rig
    monkeypatch.setattr(business_tools, "asyncio", asyncio)
    rig.boundary.plans["乘地铁"] = ["apply_timeout"]
    rig.start([add_turn(proposal()), finish_turn()])
    task = asyncio.create_task(
        rig.service.handle(
            "omAcceptanceMessage", "ocSyntheticAcceptance", "昨天乘地铁花了3元", today=DAY
        )
    )
    for _ in range(100):
        if rig.boundary.last_uid:
            break
        await asyncio.sleep(0.01)
    assert rig.boundary.last_uid is not None
    await asyncio.sleep(0.03)
    assert not task.done() and actions(rig) == ["create"]
    assert rig.boundary.saved(rig.boundary.last_uid)["state"] == "uncertain"
    assert rig.model.requests == 1
    report = await asyncio.wait_for(task, 6)
    assert statuses(report) == ["success"]
    create, lookup = rig.boundary.trace
    assert lookup["time"] - create["time"] >= 2.95
    assert actions(rig) == ["create", "reconcile"]


async def test_model_failure_after_write_preserves_the_saved_success(acceptance_rig):
    rig = acceptance_rig
    report = await rig.run(
        turns=[add_turn(proposal()), RuntimeError("synthetic model unavailable")]
    )
    assert statuses(report) == ["success"] and report.notice
    assert actions(rig) == ["create"]
    assert rig.journal.report("omAcceptanceMessage") == report
    assert "已记账消费记录" in card_text(rig) and "3.00 元" in card_text(rig)


async def test_task_timeout_during_wait_saves_a_final_pending_report(acceptance_rig):
    rig = acceptance_rig
    rig.settings.task_timeout_seconds = 0.05
    rig.clock.block = True
    rig.boundary.plans["乘地铁"] = ["drop_timeout"]
    report = await asyncio.wait_for(rig.run(), 3)
    assert statuses(report) == ["uncertain"] and report.notice
    assert actions(rig) == ["create"] and len(rig.clock.calls) == 1
    assert rig.journal.report("omAcceptanceMessage") == report
    assert "待核对" in card_text(rig)


async def test_model_cannot_supply_the_program_owned_id(acceptance_rig):
    rig = acceptance_rig
    from_model = {**proposal(), "unique_id": "model-chosen-id"}
    report = await rig.run(turns=[add_turn(from_model), add_turn(proposal()), finish_turn()])
    assert statuses(report) == ["success"] and rig.model.requests == 3
    assert actions(rig) == ["create"]
    assert UUID(rig.boundary.last_uid).version == 4
    assert rig.boundary.last_uid != "model-chosen-id"
    assert any(
        isinstance(message, ToolMessage) and message.status == "error"
        for message in rig.model.seen[1]
    )


async def test_optional_history_and_no_consumption_do_not_start_reconciliation(acceptance_rig):
    rig = acceptance_rig
    report = await rig.run(
        "明天计划乘地铁，今天没有消费",
        [[("query_history", {"keyword": "月票", "limit": 2})], finish_turn(no_consumption=True)],
    )
    assert report.no_consumption and not report.entries and not report.skipped
    assert actions(rig) == ["query"] and rig.clock.calls == []
    assert "此次未包含消费记录" in card_text(rig)
    assert "unique_id" not in json.dumps(registered_add.tool_call_schema.model_json_schema())


async def test_final_report_never_calls_a_successful_submission_skipped(acceptance_rig):
    """Whole-flow consistency requirement, independently checked in one model round."""
    rig = acceptance_rig
    turn = add_turn(proposal()) + finish_turn(
        skipped=[{"source_text": "昨天乘地铁花了3元", "reason": "金额暂时不明确，未提交"}]
    )
    report = await rig.run(turns=[turn, finish_turn()])
    assert statuses(report) == ["success"]
    assert not report.skipped, "Final report contradicts the persisted successful submission"
