"""埋坑评测的答案表和打分（不调用模型）。"""

import sys
from pathlib import Path

from contract_agent.checklist import load
from contract_agent.extract import Quote
from contract_agent.risks import Finding, Missing

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import traps  # noqa: E402

KEYS = [traps.load_key(item) for item in traps.KEY]
RISKS = {r.id: r for r in load()}


def test_key_resolves_and_covers_every_risk_in_both_sets():
    assert traps.check_coverage(KEYS) == []
    assert {k["set"] for k in KEYS} == {"dev", "test"}


def finding(risk: str, clause: str) -> Finding:
    return Finding(risk=RISKS[risk], how="", quotes=[Quote(clause=clause, text="x")], pages=[1])


def test_grade_counts_found_misplaced_false_and_missing():
    key = next(k for k in KEYS if k["file"].endswith("trap-02.pdf"))
    findings = [
        finding("R21", "c1"),  # 对
        finding("R07", "c9"),  # 编号对，条款不对
        finding("R17", "c7"),  # 诱饵
    ]
    missing = [Missing(risk=RISKS["R11"], field="repairs"), Missing(risk=RISKS["R09"], field="fees")]
    g = traps.grade(key, findings, missing)
    assert g["found"] == ["R21"] and g["misplaced"] == ["R07"]
    assert "R19" in g["missed"] and g["false"] == ["R17"] and g["decoy_hits"] == ["R17"]
    assert g["missing_ok"] == ["repairs"] and g["missing_lost"] == ["early_termination"] and g["missing_extra"] == ["fees"]


def test_missing_item_reported_as_risk_counts_as_reminded():
    # trap-03 押金退还没写：模型引用押金那条报 R02，和程序提示"没写"是同一条提醒
    key = next(k for k in KEYS if k["file"].endswith("trap-03.pdf"))
    g = traps.grade(key, [finding("R02", "c4")], [])
    assert g["false"] == [] and g["missing_ok"] == ["deposit_refund"] and g["missing_lost"] == []


def test_either_is_not_a_false_positive():
    key = next(k for k in KEYS if k["file"].endswith("trap-03.pdf"))
    assert traps.grade(key, [finding("R15", "c10")], [])["false"] == []
