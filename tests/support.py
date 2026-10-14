from collections import deque
from datetime import date

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from bookkeeping.domain import Expense, ExpenseProposal, WriteOutcome

TODAY = date(2026, 10, 14)


def proposal(source: str = "今天买咖啡18元", **changes) -> ExpenseProposal:
    values = {
        "source_text": source,
        "description": "买咖啡",
        "amount": "18",
        "amount_evidence": "18元",
        "date_evidence": "今天",
        "expense_date": "2026-10-14",
        "category_l1": "饮食",
        "category_l2": "咖啡",
        "classification_basis": "本人购买咖啡，符合咖啡规则",
        "rule_ids": ["TEST_COFFEE"],
        **changes,
    }
    return ExpenseProposal(**values)


class MemoryLedger:
    def __init__(self, outcomes: dict[str, str] | None = None):
        self.created: list[Expense] = []
        self.queries: list[tuple[str, int]] = []
        self.outcomes = outcomes or {}

    async def query_history(self, keyword: str, limit: int) -> dict:
        self.queries.append((keyword, limit))
        return {
            "ok": True,
            "records": [{"具体": "买咖啡", "一级分类": ["饮食"], "二级分类": ["咖啡"]}],
            "has_more": False,
        }

    async def create(self, expense: Expense) -> WriteOutcome:
        self.created.append(expense)
        state = next(
            (status for word, status in self.outcomes.items() if word in expense.description),
            "success",
        )
        if state == "raise":
            raise RuntimeError("connection lost after submitting")
        if state == "success":
            return WriteOutcome(status="success", record_id=f"recFake{len(self.created):03}")
        reason = (
            "测试工具明确拒绝该笔写入" if state == "failed" else "测试工具提交后超时，请核对账本"
        )
        return WriteOutcome(status=state, reason=reason)


class ScriptedModel:
    def __init__(self, steps: list[list[tuple[str, dict]]]):
        self.steps = deque(steps)
        self.seen_messages = []
        self.calls = 0
        self.model = FunctionModel(self.respond)

    def respond(self, messages, info):
        self.calls += 1
        self.seen_messages.append(messages)
        calls = self.steps.popleft()
        return ModelResponse(
            [
                ToolCallPart(
                    info.output_tools[0].name if name == "finish" else name,
                    arguments,
                    tool_call_id=f"call_{self.calls}_{index}",
                )
                for index, (name, arguments) in enumerate(calls)
            ]
        )
