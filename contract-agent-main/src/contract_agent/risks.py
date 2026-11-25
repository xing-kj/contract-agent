"""风险条款：合同和风险清单逐条比对。

整份清单放进提示词，模型找出合同里命中的条款，每条附原文。代码再核对：
1. 编号必须在清单里；
2. 原文必须真的在合同里（和抽取用同一套核对，出处写错了块就改正）；有一句对不上，整条扔掉；
3. "为什么要留意""可以问""依据"直接用清单里审过的文字，模型只写一句"这份合同具体怎么写的"。

"合同里没写"不交给模型判断：清单里标了"没写也提示"的项，第 3 步抽出来是空的，就由代码提示。
抽取时因为核对没通过被扔掉的项不算"没写"——那只是没抽成功，不能说合同里没有。

    uv run python -m contract_agent.risks samples/zufang-02.pdf
"""

import asyncio
import json
import sys
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from .checklist import Risk, load
from .clauses import Clause, split_clauses
from .extract import LABELS, Dropped, Extraction, Quote, check_quotes, clause_texts, contract_text, extract, strict_schema
from .llm import Chat, chat
from .textdoc import read_document


class Hit(BaseModel):
    risk: str = Field(description="命中的清单编号，如 R06")
    quotes: list[Quote] = Field(description="合同里命中这一条的原文，一字不差地复制；写在几处就各复制一句")
    how: str = Field(description="一句大白话：这份合同具体是怎么写的。只陈述，不评价，不说违法、无效")
    # 先写对照，再下结论：模型写完对照常常会发现自己报错了，让它能撤回（默认值只为测试方便，严格模式下两项都必须写）
    check: str = Field("", description="对照这一条的'不要误报'：合同的写法是不是那种不算的情况，一句话")
    keep: bool = Field(True, description="对照之后仍然算命中填 true；属于'不要误报'说的情况填 false")


class HitList(BaseModel):
    hits: list[Hit]


SCHEMA = strict_schema(HitList.model_json_schema())
WITHDRAWN = "对照'不要误报'后撤回"


class Finding(BaseModel):
    risk: Risk
    how: str
    quotes: list[Quote]
    pages: list[int]


class Missing(BaseModel):
    risk: Risk
    field: str  # 哪一项没写


class RiskReport(BaseModel):
    findings: list[Finding]  # 合同里写了、命中清单的
    missing: list[Missing]  # 合同里没写、清单说没写也要提示的
    dropped: list[Dropped]  # 模型报了、但没通过核对被扔掉的，以及原因
    fixed: list[str]


def checklist_text(risks: tuple[Risk, ...]) -> str:
    return "\n\n".join(
        f"{r.id} {r.title}\n典型写法：{r.patterns}\n为什么要留意：{r.why}\n不要误报：{r.not_risk}" for r in risks
    )


PROMPT = """你帮租客检查租房合同。下面是一份人工审核过的风险清单，之后用户会发来一份按条款切好的合同，每一块开头方括号里是它的 id。
逐条对照清单，找出合同里命中的条目：
- 只报清单里有的编号，按清单的意思判断，对照"不要误报"排除看起来像、其实没问题的写法。拿不准的不报。
- quotes 一字不差地复制命中的原文（可以只复制相关的一段），clause 写这句话所在块的 id。同一条风险写在几处，就各复制一句；一处原文可以命中几条风险。
- how 用一句大白话说这份合同具体怎么写的，只陈述，不评价，不说"违法""无效""霸王条款"。
- check 拿 how 去对照这一条的"不要误报"，说合同的写法是不是那种不算的情况。是的话 keep 填 false，程序会去掉这一条；不是才填 true。
- 合同完全没提到的事不报（其中押金退还、费用、维修、提前退租没写的，程序会另外提示）。但合同提到了、东西却没给的要报：比如写了"见附件""按某某规定执行"，合同后面却没附上。合同后面附了什么，看合同最后一行的说明。

风险清单：

"""


def verify_hits(raw: dict, clauses: list[Clause], risks: tuple[Risk, ...]) -> tuple[list[Finding], list[Dropped], list[str]]:
    """核对模型报的每一条。不调用模型，方便单独测试。同一编号报了几次的合并成一条。"""
    by_id = {r.id: r for r in risks}
    texts = clause_texts(clauses)
    pages = {c.id: c.pages for c in clauses}
    merged: dict[str, Finding] = {}
    dropped: list[Dropped] = []
    fixed: list[str] = []
    for item in raw.get("hits") or []:
        try:
            hit = Hit.model_validate(item)
        except ValidationError as e:
            dropped.append(Dropped(field="?", reason=f"格式不对：{e.errors()[0]['msg']}"))
            continue
        risk = by_id.get(hit.risk.strip())
        if risk is None:
            dropped.append(Dropped(field=hit.risk, reason="清单里没有这个编号"))
            continue
        if not hit.keep:
            dropped.append(Dropped(field=risk.id, reason=f"{WITHDRAWN}（{hit.check.strip()}）"))
            continue
        quotes = [q for q in hit.quotes if q.text.strip()]
        if not quotes:
            dropped.append(Dropped(field=risk.id, reason="没有附原文"))
            continue
        checked, notes, missing = check_quotes(quotes, texts, risk.id)
        fixed += notes
        if missing is not None:
            dropped.append(Dropped(field=risk.id, reason=f"附的原文在合同里找不到：「{missing}」"))
            continue
        if risk.id in merged:
            seen = merged[risk.id]
            seen.quotes += [q for q in checked if q.text not in {s.text for s in seen.quotes}]
        else:
            merged[risk.id] = Finding(risk=risk, how=hit.how.strip(), quotes=checked, pages=[])
    findings = [merged[r.id] for r in risks if r.id in merged]  # 按清单顺序
    for f in findings:
        f.pages = sorted({p for q in f.quotes for p in pages.get(q.clause, [])})
    return findings, dropped, fixed


def find_missing(extraction: Extraction, findings: list[Finding], risks: tuple[Risk, ...]) -> list[Missing]:
    """合同里没写、清单要求提示的项。已经命中原文的风险不再重复提示。"""
    failed = {d.field for d in extraction.dropped}
    if "*" in failed:  # 抽取整个失败了，什么都不知道，不能说"没写"
        return []
    hit = {f.risk.id for f in findings}
    out: list[Missing] = []
    for r in risks:
        if r.id in hit:
            continue
        for name in r.absent:
            if getattr(extraction.info, name) is None and name not in failed:
                out.append(Missing(risk=r, field=name))
                break
    return out


def annex_note(clauses: list[Clause]) -> str:
    """合同后面附了什么，由切分结果告诉模型。模型自己分不清附件是没附上，还是没给它看。"""
    annexes = [c.heading for c in clauses if c.id.startswith("annex")]
    if annexes:
        return f"（合同到此结束。签字栏后面附有：{'、'.join(annexes)}。）"
    return "（合同到此结束，签字栏后面没有附件。）"


async def match(clauses: list[Clause], chat: Chat = chat) -> tuple[list[Finding], list[Dropped], list[str]]:
    risks = load()
    answer = await chat(
        [
            {"role": "system", "content": PROMPT + checklist_text(risks)},
            {"role": "user", "content": f"{contract_text(clauses)}\n\n{annex_note(clauses)}"},
        ],
        response_format={"type": "json_schema", "json_schema": {"name": "risk_hits", "strict": True, "schema": SCHEMA}},
    )
    try:
        raw = json.loads(answer)
    except json.JSONDecodeError:
        return [], [Dropped(field="*", reason="模型返回的不是 JSON")], []
    return verify_hits(raw, clauses, risks)


async def review(clauses: list[Clause]) -> tuple[Extraction, RiskReport]:
    """抽关键信息和比对风险同时进行，再用抽取结果判断哪些"没写"。"""
    extraction, (findings, dropped, fixed) = await asyncio.gather(extract(clauses), match(clauses))
    report = RiskReport(findings=findings, missing=find_missing(extraction, findings, load()), dropped=dropped, fixed=fixed)
    return extraction, report


if __name__ == "__main__":
    clauses = split_clauses(read_document(Path(sys.argv[1]).read_bytes()))
    heading = {c.id: c.heading or c.id for c in clauses}
    _, report = asyncio.run(review(clauses))
    for f in report.findings:
        print(f"\n{f.risk.id} {f.risk.title}（第 {'、'.join(map(str, f.pages))} 页）")
        print(f"  合同怎么写：{f.how}")
        for q in f.quotes:
            print(f"  ← {heading[q.clause]}「{q.text}」")
        print(f"  可以问：{f.risk.ask}")
        if basis := f.risk.basis_shown:
            print(f"  依据：{basis}")
    for m in report.missing:
        print(f"\n没写：{LABELS[m.field]} → {m.risk.id} {m.risk.title}\n  可以问：{m.risk.ask}")
    for d in report.dropped:
        print(f"\n扔掉 {d.field}：{d.reason}")
    for x in report.fixed:
        print(f"改正 {x}")
