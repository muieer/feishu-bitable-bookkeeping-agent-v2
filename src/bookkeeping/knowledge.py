import json
import re
from datetime import date
from pathlib import Path

from .domain import (
    Expense,
    ExpenseProposal,
    calculate_grounded,
    locate_source,
    numbers_in,
    resolve_date,
)


class Knowledge:
    def __init__(self, project_dir: Path, table_id: str):
        knowledge_dir = project_dir / "knowledges"
        try:
            self.categories = json.loads(
                (knowledge_dir / "categories.json").read_text(encoding="utf-8")
            )
            self.rules = json.loads((knowledge_dir / "rules.json").read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise ValueError(
                "缺少个人知识库，请在 knowledges/ 中提供 categories.json 和 rules.json"
            ) from error
        if self.categories.get("table_id") != table_id:
            raise ValueError("分类知识库与配置中的目标表不一致")
        if self.rules.get("compatible_categories_version") != self.categories.get("version"):
            raise ValueError("分类规则与分类清单的版本不兼容")
        self.paths: set[tuple[str, str, str | None]] = set()
        for first in self.categories["tree"]:
            for second in first["children"]:
                for third in second["children"] or [{"name": None}]:
                    self.paths.add((first["name"], second["name"], third["name"]))
        self.rule_map = {item["id"]: item for item in self.rules["rules"]}
        for rule in self.rule_map.values():
            if tuple(rule["category"]) not in self.paths:
                raise ValueError(f"分类规则 {rule['id']} 引用了非法分类路径")

    @property
    def context(self) -> str:
        return json.dumps(
            {"legal_category_paths": sorted(self.paths, key=str), "personal_rules": self.rules},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def validate(
        self, proposal: ExpenseProposal, message: str, today: date
    ) -> tuple[Expense, tuple[int, int]]:
        span = locate_source(message, proposal.source_text, proposal.source_occurrence)
        if not proposal.description.strip() or proposal.description not in proposal.source_text:
            raise ValueError("具体消费内容必须有原文依据，不能补写缺失的商品或服务")
        if proposal.currency != "CNY":
            raise ValueError("只支持人民币消费")
        if not proposal.amount_evidence or proposal.amount_evidence not in proposal.source_text:
            raise ValueError("必须提供该笔消费的原文金额依据")
        if re.search(
            r"美元|美金|港币|港元|日元|欧元|英镑|USD|HKD|JPY|EUR|GBP|\$|€|£",
            proposal.source_text,
            re.I,
        ):
            if not re.search(r"人民币|CNY|RMB|￥", proposal.amount_evidence, re.I):
                raise ValueError("不能把外币金额直接作为人民币入账")
        if proposal.date_evidence and proposal.date_evidence not in message:
            raise ValueError("日期依据不在原消息中")
        expense = Expense(
            source_text=proposal.source_text,
            source_occurrence=proposal.source_occurrence,
            description=proposal.description,
            amount=proposal.amount,
            expense_date=resolve_date(proposal.date_evidence, proposal.expense_date, today),
            category_l1=proposal.category_l1,
            category_l2=proposal.category_l2,
            category_l3=proposal.category_l3,
        )
        if expense.category_path not in self.paths:
            raise ValueError("分类路径不存在、不完整或三级分类不合法")
        if not proposal.classification_basis.strip():
            raise ValueError("必须有足以确定唯一分类的依据")
        for rule_id in proposal.rule_ids:
            rule = self.rule_map.get(rule_id)
            if not rule:
                raise ValueError("分类依据引用了不存在的规则编号")
        if proposal.rule_ids and not any(
            tuple(self.rule_map[rule_id]["category"]) == expense.category_path
            for rule_id in proposal.rule_ids
        ):
            raise ValueError("分类规则编号与所选分类路径不一致")
        if proposal.amount_calculation:
            grounded = calculate_grounded(proposal.amount_calculation, proposal.source_text)
            if expense.amount != grounded:
                raise ValueError("金额与原文计算结果不一致")
        elif expense.amount not in numbers_in(proposal.amount_evidence):
            raise ValueError("金额不在该笔消费的原文依据中")
        return expense, span
