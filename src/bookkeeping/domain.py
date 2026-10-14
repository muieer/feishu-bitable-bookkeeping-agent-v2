import ast
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

import cn2an
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExpenseProposal(StrictModel):
    """Permissive values are validated individually, so one bad row cannot block its peers."""

    source_text: str = ""
    source_occurrence: int = 1
    description: str = ""
    amount: str = ""
    amount_evidence: str = ""
    amount_calculation: str | None = None
    expense_date: str | None = None
    date_evidence: str | None = None
    category_l1: str = ""
    category_l2: str = ""
    category_l3: str | None = None
    currency: str = "CNY"
    classification_basis: str = ""
    rule_ids: list[str] = Field(default_factory=list)


class Expense(StrictModel):
    source_text: str = Field(min_length=1, max_length=2000)
    source_occurrence: int = Field(ge=1)
    description: str = Field(min_length=1, max_length=500)
    amount: Decimal = Field(gt=0, decimal_places=2, allow_inf_nan=False)
    expense_date: date
    category_l1: str = Field(min_length=1)
    category_l2: str = Field(min_length=1)
    category_l3: str | None = None

    @field_validator("description", "source_text", "category_l1", "category_l2")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("字段不能为空白")
        return value

    @property
    def category_path(self) -> tuple[str, str, str | None]:
        return self.category_l1, self.category_l2, self.category_l3


class SkippedExpense(StrictModel):
    source_text: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=500)
    source_occurrence: int = Field(default=1, ge=1)


class Decision(StrictModel):
    """LLM may report omissions; successful writes come exclusively from the journal."""

    skipped: list[SkippedExpense] = Field(default_factory=list)
    no_consumption: bool = False


class WriteOutcome(StrictModel):
    status: Literal["success", "failed", "uncertain"]
    record_id: str | None = None
    reason: str = ""


class EntryResult(StrictModel):
    expense: Expense
    outcome: WriteOutcome


class TaskReport(StrictModel):
    entries: list[EntryResult] = Field(default_factory=list)
    skipped: list[SkippedExpense] = Field(default_factory=list)
    no_consumption: bool = False
    notice: str | None = None


def locate_source(message: str, source: str, occurrence: int = 1) -> tuple[int, int]:
    if not source.strip() or occurrence < 1:
        raise ValueError("必须提供该笔消费在原消息中的准确描述")
    start = -1
    for _ in range(occurrence):
        start = message.find(source, start + 1)
        if start < 0:
            raise ValueError("消费原文或出现次数不在原消息中")
    return start, start + len(source)


def numbers_in(text: str) -> list[Decimal]:
    normalized = cn2an.transform(text.replace(",", "").replace("，", ""), "cn2an")
    return [Decimal(value) for value in re.findall(r"(?<![\d.])\d+(?:\.\d+)?", normalized)]


def calculate_grounded(expression: str, evidence: str) -> Decimal:
    """Evaluate decimal arithmetic only; no names, calls, attributes, or arbitrary execution."""
    if len(expression) > 100 or not re.fullmatch(r"[\d\s.+*/()\-]+", expression):
        raise ValueError("金额计算式只允许数字和加减乘除")
    tree = ast.parse(expression, mode="eval")
    if len(list(ast.walk(tree))) > 40:
        raise ValueError("金额计算式过长")
    available = numbers_in(evidence)

    def evaluate(node: ast.AST) -> Decimal:
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = Decimal(ast.get_source_segment(expression, node) or "")
            if value not in available:
                raise ValueError("金额计算式包含原文没有提供的数字")
            return value
        if isinstance(node, ast.BinOp):
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div) and right != 0:
                return left / right
        raise ValueError("金额计算式不合法")

    return evaluate(tree.body)


def resolve_date(evidence: str | None, proposed: str | None, today: date) -> date:
    selected = date.fromisoformat(proposed) if proposed else today
    if not evidence:
        if selected != today:
            raise ValueError("未提供日期依据时只能使用任务执行日")
        return today
    relative = {"今天": 0, "今日": 0, "昨天": 1, "昨日": 1, "前天": 2, "大前天": 3}
    if evidence in relative:
        expected = today - timedelta(days=relative[evidence])
    else:
        match = re.fullmatch(
            r"(?:(\d{4})\s*(?:年|[-/])\s*)?(\d{1,2})\s*(?:月|[-/])\s*"
            r"(\d{1,2})\s*(?:日|号)?",
            evidence.strip(),
        )
        if match:
            expected = date(int(match[1] or today.year), int(match[2]), int(match[3]))
        else:
            # Less common natural date expressions remain LLM decisions, with literal provenance.
            if not proposed:
                raise ValueError("日期描述无法解析，需给出有依据的 ISO 日期")
            expected = selected
    if proposed and selected != expected:
        raise ValueError("消费日期与日期依据不一致")
    if expected > today:
        raise ValueError("消费日期在未来，不属于已经发生的消费")
    return expected
