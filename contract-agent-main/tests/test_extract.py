"""核对模型抽出来的信息：用编好的"模型输出"测 verify()，不调用模型。合同用 samples/zufang-01.pdf 切好的条款。"""

from pathlib import Path

import pytest

from contract_agent.clauses import split_clauses
from contract_agent.extract import SCHEMA, verify
from contract_agent.pdf import read_pages

CLAUSES = split_clauses(read_pages((Path(__file__).resolve().parents[1] / "samples" / "zufang-01.pdf").read_bytes()))


def q(clause: str, text: str) -> dict:
    return {"clause": clause, "text": text}


RENT = {"yuan": 3200, "quotes": [q("c3", "月租金为人民币 3200 元")]}


def test_verbatim_quote_is_kept():
    result = verify({"rent": RENT}, CLAUSES)
    assert result.info.rent.yuan == 3200
    assert not result.dropped and not result.fixed


def test_whitespace_differences_are_fine():
    # PDF 读出来的空格和模型复制的不一定一样
    result = verify({"rent": {"yuan": 3200, "quotes": [q("c3", "月租金为人民币3200元")]}}, CLAUSES)
    assert result.info.rent is not None


def test_wrong_clause_id_is_corrected():
    result = verify({"rent": {"yuan": 3200, "quotes": [q("c4", "月租金为人民币 3200 元")]}}, CLAUSES)
    assert result.info.rent.quotes[0].clause == "c3"
    assert result.fixed == ["月租金：出处写的是 c4，原文其实在 c3"]


def test_made_up_quote_is_dropped():
    result = verify({"rent": {"yuan": 3000, "quotes": [q("c3", "月租金为人民币 3000 元")]}}, CLAUSES)
    assert result.info.rent is None
    assert "找不到" in result.dropped[0].reason


def test_amount_must_appear_in_the_quote():
    result = verify({"deposit": {"yuan": 2000, "quotes": [q("c4", "乙方应于签订本合同时向甲方支付押金人民币 3200 元")]}}, CLAUSES)
    assert result.info.deposit is None
    assert "金额 2000" in result.dropped[0].reason


def test_amount_is_matched_as_a_whole_number():
    # 320 是 3200 的一部分，不能算对上
    result = verify({"rent": {"yuan": 320, "quotes": [q("c3", "月租金为人民币 3200 元")]}}, CLAUSES)
    assert result.info.rent is None


def test_dates_must_appear_in_the_quote():
    quote = [q("c2", "租赁期限自 2026 年 12 月 1 日起至 2027 年 11 月 30 日止")]
    assert verify({"period": {"start": "2026-12-01", "end": "2027-11-30", "quotes": quote}}, CLAUSES).info.period is not None
    assert verify({"period": {"start": "2026-12-01", "end": "2027-12-31", "quotes": quote}}, CLAUSES).info.period is None


def test_bad_format_drops_only_that_item():
    result = verify({"rent": {"yuan": "三千二", "quotes": [q("c3", "月租金为人民币 3200 元")]}, "deposit": {"yuan": 3200, "quotes": [q("c4", "押金人民币 3200 元")]}}, CLAUSES)
    assert result.info.rent is None and result.info.deposit.yuan == 3200
    assert result.dropped[0].field == "rent" and "格式不对" in result.dropped[0].reason


def test_one_made_up_quote_drops_the_whole_item():
    quotes = [q("c6", "由甲方负责维修"), q("c6", "乙方承担所有维修费用")]
    result = verify({"repairs": {"value": "都由乙方修", "quotes": quotes}}, CLAUSES)
    assert result.info.repairs is None


def test_table_rows_can_be_quoted():
    quotes = [q("c5", "| 物业管理费 | 甲方 | — |"), q("c5", "| 网络费 | 乙方 | 乙方自行办理 |")]
    assert verify({"fees": {"value": "物业费甲方出，网费乙方出", "quotes": quotes}}, CLAUSES).info.fees is not None


def test_missing_items_stay_empty():
    result = verify({"sublet": None}, CLAUSES)
    assert result.info.sublet is None and not result.dropped


@pytest.mark.parametrize("path", ["", "$defs.Money", "$defs.Quote"])
def test_schema_is_strict(path):
    # OpenAI 严格模式：每个对象都不许多出字段，所有字段都列进 required，没有 default
    node = SCHEMA
    for key in filter(None, path.split(".")):
        node = node[key]
    assert node["additionalProperties"] is False
    assert node["required"] == list(node["properties"])
    assert "default" not in str(node)
