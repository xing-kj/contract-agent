"""风险清单：审核过的清单能读出来；格式不对、没审过的条目直接报错。"""

import pytest

from contract_agent.checklist import ChecklistError, load, parse

ENTRY = """## R01 押金一律不退
- 对应关键信息：deposit_refund（没写也提示）、early_termination
- 典型写法：押金不予退还
- 依据：《民法典》第五百八十五条
- 核对：2026-11-24 {status}

押金是担保。
可以问：押金按实际损失扣吗？
不要误报：写明扣除实际损失后退还的，不算。
"""


def test_reviewed_checklist_loads():
    risks = load()
    ids = [r.id for r in risks]
    assert len(ids) == len(set(ids)) == 26
    assert {r.id for r in risks if r.absent} == {"R02", "R09", "R11", "R13"}
    assert all(r.why and r.ask and r.not_risk for r in risks)


def test_fields_and_status_are_parsed():
    (risk,) = parse(ENTRY.format(status="部分（只覆盖一部分）"))
    assert risk.fields == ["deposit_refund", "early_termination"]
    assert risk.absent == ["deposit_refund"]
    assert risk.status == "部分" and risk.note == "只覆盖一部分"
    assert risk.basis_shown.endswith("（仅供参考：依据只覆盖一部分，各地规定可能不同）")


def test_review_notes_are_not_returned_to_users():
    (risk,) = parse(ENTRY.format(status="通过（内部说明）"))
    dumped = risk.model_dump()
    assert "note" not in dumped and "basis" not in dumped
    assert dumped["basis_shown"] == "《民法典》第五百八十五条"


def test_basis_is_hidden_when_missing():
    (risk,) = parse(ENTRY.format(status="待补"))
    assert risk.basis_shown == ""


def test_unreviewed_entry_is_refused():
    with pytest.raises(ChecklistError, match="待核"):
        parse(ENTRY.format(status="待核"))


def test_unknown_field_is_refused():
    with pytest.raises(ChecklistError, match="不存在"):
        parse(ENTRY.format(status="通过").replace("early_termination", "early_exit"))


def test_duplicate_ids_are_refused():
    with pytest.raises(ChecklistError, match="重复"):
        parse(ENTRY.format(status="通过") * 2)
