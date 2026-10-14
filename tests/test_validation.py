from datetime import date
from decimal import Decimal

import pytest

from bookkeeping.domain import calculate_grounded, locate_source, resolve_date

from .support import TODAY, proposal


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": "0"},
        {"amount": "-18"},
        {"amount": "18.001"},
        {"amount": "NaN"},
        {"amount": "Infinity"},
        {"amount": "19"},
        {"currency": "USD"},
        {"category_l1": "虚构分类"},
        {"category_l2": "不存在的二级"},
        {"category_l3": "其他"},
        {"category_l1": "交通通信", "category_l2": "汽车", "rule_ids": []},
        {"source_text": "凭空捏造的咖啡18元"},
        {"description": "买外卖"},
        {"description": " "},
        {"date_evidence": "昨天"},
        {"expense_date": "2026-10-15"},
        {"rule_ids": ["TEST_OTHER"]},
        {"rule_ids": ["MISSING_RULE"]},
        {"classification_basis": ""},
        {"amount_evidence": "20元"},
        {"source_occurrence": 2},
    ],
)
def test_ungrounded_or_invalid_row_never_passes(knowledge, changes):
    with pytest.raises(ValueError):
        knowledge.validate(proposal(**changes), "今天买咖啡18元", TODAY)


def test_legal_third_level_and_real_purpose(knowledge):
    item = proposal(
        "昨天汽车停车费8元",
        description="汽车停车费",
        amount="8",
        amount_evidence="8元",
        date_evidence="昨天",
        expense_date="2026-10-13",
        category_l1="交通通信",
        category_l2="汽车",
        category_l3="停车费",
        rule_ids=["TEST_PARKING"],
    )
    expense, _ = knowledge.validate(item, item.source_text, TODAY)
    assert expense.category_path == ("交通通信", "汽车", "停车费")
    assert expense.expense_date == date(2026, 10, 13)


def test_total_chinese_amount_and_safe_arithmetic(knowledge):
    source = "买了两瓶饮用水，每瓶3元"
    item = proposal(
        source,
        description="买了两瓶饮用水",
        amount="6",
        amount_evidence=source,
        amount_calculation="2*3",
        expense_date=None,
        date_evidence=None,
        category_l2="饮用水",
        rule_ids=["TEST_WATER"],
    )
    expense, _ = knowledge.validate(item, source, TODAY)
    assert expense.amount == Decimal("6")
    chinese = proposal(
        "买咖啡十八元", amount_evidence="十八元", expense_date=None, date_evidence=None
    )
    assert knowledge.validate(chinese, chinese.source_text, TODAY)[0].amount == Decimal("18")
    discounted = proposal(
        "买咖啡原价20元，优惠2元",
        amount_evidence="原价20元，优惠2元",
        amount_calculation="20-2",
        expense_date=None,
        date_evidence=None,
    )
    assert knowledge.validate(discounted, discounted.source_text, TODAY)[0].amount == Decimal("18")


@pytest.mark.parametrize(
    "expression", ["__import__('os').system('id')", "1<<3", "3**3", "3/0", "3+100", "[3][0]", "-3"]
)
def test_arithmetic_cannot_execute_code_or_invent_numbers(expression):
    with pytest.raises((ValueError, SyntaxError)):
        calculate_grounded(expression, "每瓶3元")


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        (None, "2026-10-14"),
        ("今天", "2026-10-14"),
        ("昨天", "2026-10-13"),
        ("前天", "2026-10-12"),
        ("10月10日", "2026-10-10"),
        ("2025/12/31", "2025-12-31"),
        ("2026-10-01", "2026-10-01"),
    ],
)
def test_dates_use_execution_day(evidence, expected):
    assert (
        resolve_date(evidence, None if evidence is None else expected, TODAY).isoformat()
        == expected
    )


def test_source_occurrences_distinguish_equal_real_transactions():
    message = "早上买咖啡12元，下午买咖啡12元"
    first = locate_source(message, "买咖啡12元", 1)
    second = locate_source(message, "买咖啡12元", 2)
    assert first[1] < second[0]


def test_external_currency_is_not_silently_converted(knowledge):
    with pytest.raises(ValueError, match="外币"):
        knowledge.validate(
            proposal(
                "买咖啡18美元", amount_evidence="18美元", date_evidence=None, expense_date=None
            ),
            "买咖啡18美元",
            TODAY,
        )
