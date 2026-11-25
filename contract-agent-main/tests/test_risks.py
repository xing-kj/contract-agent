"""核对模型报的风险，以及"合同里没写"的判断：用编好的"模型输出"测，不调用模型。"""

from pathlib import Path

from contract_agent.checklist import load
from contract_agent.clauses import split_clauses
from contract_agent.extract import ContractInfo, Dropped, Extraction, Text
from contract_agent.pdf import read_pages
from contract_agent.risks import find_missing, verify_hits

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
CLAUSES = split_clauses(read_pages((SAMPLES / "zufang-02.pdf").read_bytes()))
RISKS = load()

RAISE = "租期内如遇市场租金上涨，甲方有权调整租金，乙方应予配合。"
KEEP = "乙方提前退租的，应提前一个月告知甲方，押金不予退还。"


def hit(risk: str, *quotes: tuple[str, str], how: str = "合同这样写") -> dict:
    return {"risk": risk, "how": how, "quotes": [{"clause": c, "text": t} for c, t in quotes]}


def clause_of(text: str) -> str:
    return next(c.id for c in CLAUSES if text.replace(" ", "") in (c.heading + c.text).replace(" ", ""))


def test_verified_hit_uses_reviewed_text():
    findings, dropped, _ = verify_hits({"hits": [hit("R05", (clause_of(RAISE), RAISE))]}, CLAUSES, RISKS)
    assert [f.risk.id for f in findings] == ["R05"] and not dropped
    # 为什么、可以问、依据都来自清单，不是模型写的
    assert findings[0].risk.ask.startswith("租期内租金会不会变")
    assert findings[0].pages == [1]


def test_unknown_risk_id_is_dropped():
    findings, dropped, _ = verify_hits({"hits": [hit("R99", (clause_of(RAISE), RAISE))]}, CLAUSES, RISKS)
    assert not findings
    assert dropped[0].reason == "清单里没有这个编号"


def test_made_up_quote_is_dropped():
    findings, dropped, _ = verify_hits({"hits": [hit("R05", ("c3", "甲方可随时上调租金"))]}, CLAUSES, RISKS)
    assert not findings
    assert "找不到" in dropped[0].reason


def test_hit_without_quote_is_dropped():
    findings, dropped, _ = verify_hits({"hits": [hit("R05")]}, CLAUSES, RISKS)
    assert not findings and dropped[0].reason == "没有附原文"


def test_wrong_clause_id_is_corrected():
    findings, _, fixed = verify_hits({"hits": [hit("R01", ("c1", KEEP))]}, CLAUSES, RISKS)
    assert findings[0].quotes[0].clause == clause_of(KEEP)
    assert fixed and fixed[0].startswith("R01：出处写的是 c1")


def test_same_risk_reported_twice_is_merged_in_checklist_order():
    raw = {"hits": [hit("R13", (clause_of(KEEP), KEEP)), hit("R01", (clause_of(KEEP), KEEP)), hit("R13", (clause_of(KEEP), KEEP))]}
    findings, _, _ = verify_hits(raw, CLAUSES, RISKS)
    assert [f.risk.id for f in findings] == ["R01", "R13"]
    assert len(findings[1].quotes) == 1


def extraction(dropped: list[Dropped] | None = None, **fields) -> Extraction:
    return Extraction(info=ContractInfo(**fields), dropped=dropped or [], fixed=[])


SAID = Text(value="写了", quotes=[])


def test_missing_fields_come_from_extraction():
    written = {name: SAID for name in ("deposit_refund", "fees", "repairs")}
    missing = find_missing(extraction(**written), [], RISKS)
    assert [(m.risk.id, m.field) for m in missing] == [("R13", "early_termination")]


def test_field_that_failed_checks_is_not_called_missing():
    # 抽取时核对没通过被扔掉，只是没抽成功，不能说合同里没写
    written = {name: SAID for name in ("deposit_refund", "fees", "repairs")}
    missing = find_missing(extraction([Dropped(field="early_termination", reason="找不到")], **written), [], RISKS)
    assert missing == []


def test_nothing_is_called_missing_when_extraction_failed():
    assert find_missing(extraction([Dropped(field="*", reason="模型返回的不是 JSON")]), [], RISKS) == []


def test_hit_withdrawn_after_checking_not_risk_is_dropped():
    raw = {"hits": [{**hit("R05", (clause_of(RAISE), RAISE)), "check": "写死了涨幅，属于不算的情况", "keep": False}]}
    findings, dropped, _ = verify_hits(raw, CLAUSES, RISKS)
    assert findings == [] and dropped[0].field == "R05" and "撤回" in dropped[0].reason and "写死了涨幅" in dropped[0].reason


def test_risk_already_found_is_not_repeated_as_missing():
    findings, _, _ = verify_hits({"hits": [hit("R13", (clause_of(KEEP), KEEP))]}, CLAUSES, RISKS)
    written = {name: SAID for name in ("deposit_refund", "fees", "repairs")}
    assert find_missing(extraction(**written), findings, RISKS) == []
