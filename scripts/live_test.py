"""Real Ark requests against an isolated ledger. Hard network budget: 5..10 requests per run."""

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bookkeeping.__main__ import process_lock  # noqa: E402
from bookkeeping.agent import create_agent  # noqa: E402
from bookkeeping.audit import Audit, ModelAuditHooks, RequestBudget  # noqa: E402
from bookkeeping.cards import result_cards  # noqa: E402
from bookkeeping.config import load_settings  # noqa: E402
from bookkeeping.domain import TaskReport  # noqa: E402
from bookkeeping.journal import Journal  # noqa: E402
from bookkeeping.knowledge import Knowledge  # noqa: E402
from bookkeeping.model import create_model  # noqa: E402
from bookkeeping.service import BookkeepingService  # noqa: E402
from tests.support import TODAY, MemoryLedger  # noqa: E402


def validate(case: dict, report: TaskReport) -> list[str]:
    from decimal import Decimal

    errors = []
    if report.notice:
        errors.append("Agent 未完整完成任务")
    actual = Counter(
        (
            entry.expense.expense_date.isoformat(),
            *entry.expense.category_path,
            Decimal(entry.expense.amount),
            entry.outcome.status,
        )
        for entry in report.entries
    )
    expected = Counter(
        (day, first, second, third, Decimal(amount), status)
        for day, first, second, third, amount, status in case["rows"]
    )
    if actual != expected:
        errors.append(
            f"记录与预期不一致：missing={list((expected - actual).elements())}; "
            f"extra={list((actual - expected).elements())}"
        )
    if len(report.skipped) != len(case["skipped"]):
        errors.append("未入账笔数不符合预期")
    for source in case["skipped"]:
        if not any(source in skipped.source_text for skipped in report.skipped):
            errors.append("缺少未入账说明：" + source)
    if report.no_consumption != case.get("no_consumption", False):
        errors.append("无消费状态不符合预期")
    if case["id"] == "faults":
        body = str(result_cards(report))
        if "待核对" not in body or "不记账消费记录" not in body or "已记账" not in body:
            errors.append("卡片没有同时表达三种真实写入状态")
    return errors


async def run(run_dir: Path, retry_failed: bool = False, cases_file: Path | None = None) -> dict:
    settings = load_settings(ROOT)
    selected = cases_file or ROOT / "knowledges" / "testing" / "live_cases.json"
    cases = json.loads(await asyncio.to_thread(selected.read_text, encoding="utf-8"))
    await asyncio.to_thread(run_dir.mkdir, parents=True, exist_ok=True, mode=0o700)
    budget = RequestBudget(10, run_dir / "requests.jsonl")
    audit = Audit(run_dir / "events.jsonl", settings.secrets)
    hooks = ModelAuditHooks(audit, budget)
    knowledge = Knowledge(ROOT, settings.bitable_table_id)
    results = []
    try:
        async with httpx.AsyncClient(
            timeout=settings.model_timeout_seconds,
            limits=httpx.Limits(max_keepalive_connections=0),
            event_hooks={"request": [hooks.request], "response": [hooks.response]},
        ) as client:
            agent = create_agent(create_model(settings, client), settings)
            for case in cases:
                saved = run_dir / (case["id"] + ".json")
                previous = json.loads(saved.read_text()) if saved.exists() else None
                if previous and (previous["passed"] or not retry_failed):
                    results.append(previous)
                    continue
                if budget.count >= 10:
                    results.append(
                        {
                            "id": case["id"],
                            "name": case["name"],
                            "passed": False,
                            "errors": ["已达到 10 次请求限制，未发出额外请求"],
                        }
                    )
                    continue
                ledger = MemoryLedger(case.get("faults"))
                journal = Journal(run_dir / (case["id"] + ".sqlite3"), "live-test")
                before = budget.count
                message_id = f"{case['id']}:attempt-{before + 1}"
                try:
                    service = BookkeepingService(settings, agent, ledger, knowledge, journal, audit)
                    report = await service.handle(
                        message_id,
                        "isolated-test-chat",
                        case["text"],
                        today=TODAY,
                        request_limit=10 - before,
                    )
                    errors = validate(case, report)
                    created = len(ledger.created)
                    duplicate = await service.handle(
                        message_id, "isolated-test-chat", case["text"], today=TODAY
                    )
                    if duplicate != report or len(ledger.created) != created:
                        errors.append("重复投递造成重复写入或结果变化")
                    result = {
                        "id": case["id"],
                        "name": case["name"],
                        "passed": not errors,
                        "requests": budget.count - before,
                        "errors": errors,
                        "report": report.model_dump(mode="json"),
                        "isolated_write_count": created,
                    }
                    if previous:
                        result["previous_attempts"] = [previous]
                    saved.write_text(
                        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                    saved.chmod(0o600)
                    results.append(result)
                    print(
                        json.dumps(
                            {key: result[key] for key in ("id", "passed", "requests", "errors")},
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                finally:
                    journal.close()
    finally:
        audit.close()
    summary = {
        "time": datetime.now(UTC).isoformat(),
        "model": settings.ark_model,
        "actual_model_requests": budget.count,
        "model_responses_received": sum(
            json.loads(line).get("event") == "model_response"
            for line in (run_dir / "events.jsonl").read_text().splitlines()
        ),
        "request_range_passed": 5 <= budget.count <= 10,
        "passed": all(result["passed"] for result in results) and 5 <= budget.count <= 10,
        "write_destination": "isolated in-memory ledger; no production writes",
        "cases": results,
    }
    (run_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                key: summary[key]
                for key in ("actual_model_requests", "request_range_passed", "passed")
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="真实方舟模型测试；写入隔离账本，硬限制最多 10 次请求"
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="持久计数与结果目录；复用同一目录会跳过已执行的案例，不能重置计数",
    )
    parser.add_argument("--retry-failed", action="store_true", help="保留原计数，只重测失败案例")
    parser.add_argument(
        "--cases-file",
        type=Path,
        help="私有测试场景 JSON；默认 knowledges/testing/live_cases.json",
    )
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with process_lock(run_dir / "run.lock"):
        summary = asyncio.run(run(run_dir, args.retry_failed, args.cases_file))
    raise SystemExit(0 if summary["passed"] else 1)


if __name__ == "__main__":
    main()
