"""追问：出处核对、"没写"时改看全文、选全文还是检索。不调用大模型，也不加载向量模型。"""

import asyncio
from pathlib import Path

from contract_agent import qa
from contract_agent.clauses import Clause, split_clauses
from contract_agent.pdf import read_pages

CLAUSES = split_clauses(read_pages((Path(__file__).resolve().parents[1] / "samples" / "zufang-03.pdf").read_bytes()))


def test_citation_found_in_its_clause():
    (c,) = qa.check_citations("不能养猫〔c46：乙方不得在房间内饲养猫、狗等宠物〕。", CLAUSES)
    assert c.status == "ok" and c.clause == "c46" and c.heading == "第四十六条 宠物" and c.pages


def test_citation_whitespace_and_colon_differences_are_fine():
    (c,) = qa.check_citations("〔c46:乙方不得在房间内饲养猫、狗 等宠物〕", CLAUSES)
    assert c.status == "ok"


def test_citation_with_wrong_clause_is_corrected():
    (c,) = qa.check_citations("〔c45：乙方不得在房间内饲养猫、狗等宠物〕", CLAUSES)
    assert c.status == "fixed" and c.clause == "c46"


def test_made_up_citation_is_marked_not_removed():
    (c,) = qa.check_citations("可以养猫〔c46：经甲方同意可以养猫〕", CLAUSES)
    assert c.status == "unverified" and c.clause == "c46"


def test_not_written_is_detected_at_the_start_only():
    assert qa.says_not_written("合同里没写停车的事。")
    assert qa.says_not_written("「合同里没写」")
    assert not qa.says_not_written("不行〔c46：……〕。合同里没写别的例外。")


def test_missing_hint_is_found_anywhere_in_the_answer():
    # 第 5 步检索方式答错的那道题：开头答了别的，句中才说"没有约定"
    assert qa.may_be_missing("合同只写了房间面积〔c9：套内面积约 28 平方米〕。合同里也没有约定换房流程或条件。")
    assert qa.may_be_missing("合同里没写停车的事。")
    assert not qa.may_be_missing("不行〔c46：乙方不得在房间内饲养猫、狗等宠物〕。")


def test_short_contract_uses_full_text_long_one_retrieves():
    assert qa.choose_mode(CLAUSES, "auto") == "full"
    long = CLAUSES + [Clause(id=f"x{i}", number=None, heading="", title="", text="字" * 1000, pages=[1]) for i in range(30)]
    assert qa.choose_mode(long, "auto") == "retrieve"
    assert qa.choose_mode(CLAUSES, "retrieve") == "retrieve"


def run(question: str, mode: str) -> list[dict]:
    async def collect():
        return [e async for e in qa.answer(question, CLAUSES, mode)]

    return asyncio.run(collect())


def fake_model(monkeypatch, partial_answer: str, full_answer: str) -> list[bool]:
    calls: list[bool] = []

    async def fake_stream(question, part, partial, usage):
        calls.append(partial)
        usage.update(prompt_tokens=100 if partial else 900, completion_tokens=10)
        yield partial_answer if partial else full_answer

    monkeypatch.setattr(qa, "_stream", fake_stream)
    monkeypatch.setattr(qa, "search", lambda clauses, question, k: ["c46", "c47"])
    return calls


def test_retrieve_then_full_when_not_written(monkeypatch):
    calls = fake_model(monkeypatch, "合同里没写。", "不行〔c46：乙方不得在房间内饲养猫、狗等宠物〕。")
    events = run("能养猫吗", "retrieve")
    assert calls == [True, False]
    types = [e["type"] for e in events]
    assert types == ["retrieved", "delta", "retry", "delta", "citations", "done"]
    done = events[-1]
    assert done["mode"] == "retrieve+full" and not done["not_written"] and done["prompt_tokens"] == 1000
    assert events[-2]["items"][0]["status"] == "ok"  # 只核对最后那个回答的出处


def test_retrieve_then_full_when_missing_is_mentioned_midway(monkeypatch):
    calls = fake_model(monkeypatch, "合同只写了房间面积。合同里也没有约定换房。", "可以申请换房〔c24：……〕")
    events = run("能换房吗", "retrieve")
    assert calls == [True, False] and "retry" in [e["type"] for e in events]
    assert events[-1]["mode"] == "retrieve+full"


def test_retrieve_answer_is_kept_when_found(monkeypatch):
    calls = fake_model(monkeypatch, "不行〔c46：乙方不得在房间内饲养猫、狗等宠物〕。", "不会用到")
    events = run("能养猫吗", "retrieve")
    assert calls == [True] and events[0]["clauses"] == ["c46", "c47"]
    assert events[-1]["mode"] == "retrieve"


def test_full_mode_does_not_search(monkeypatch):
    calls = fake_model(monkeypatch, "不会用到", "合同里没写。")
    events = run("有停车位吗", "auto")
    assert calls == [False] and "retrieved" not in [e["type"] for e in events]
    assert events[-1]["not_written"]
