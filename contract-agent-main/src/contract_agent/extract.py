"""抽取关键信息：租金、押金、租期、押金怎么退……

格式：ContractInfo 生成 JSON Schema 交给接口，模型严格按格式输出（OpenAI 的结构化输出）。
格式对不等于内容对，代码再查两道：
1. 逐项用 Pydantic 校验：哪一项不合格，只扔那一项；
2. 核对证据：每一项都附合同原文（quotes，内容写在几处就附几句），每一句都必须真的在合同里；
   金额、日期还必须真的出现在附的原文里。对不上就扔掉整项——宁可留空，不收没有依据的内容。

    uv run python -m contract_agent.extract samples/zufang-01.pdf
"""

import asyncio
import copy
import json
import re
import sys
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from .clauses import Clause, split_clauses
from .llm import Chat, chat
from .textdoc import read_document


class Quote(BaseModel):
    clause: str = Field(description="这句原文所在块的 id，如 c3；合同开头是 preamble")
    text: str = Field(description="合同原文里的一句话，一字不差地复制，不改字、不概括")


class Evidence(BaseModel):
    quotes: list[Quote] = Field(description="能证明这一项的原文：这一项的内容写在几处，就各复制一句；表格里的内容复制整行")


class Text(Evidence):
    value: str = Field(description="用一句简短的大白话概括这一项，只说合同怎么写，不评价好坏")


class Money(Evidence):
    yuan: int = Field(description="金额，单位元，只写数字")


class Period(Evidence):
    start: date = Field(description="起始日期，YYYY-MM-DD")
    end: date = Field(description="结束日期，YYYY-MM-DD")


class ContractInfo(BaseModel):
    landlord: Text | None = Field(None, description="出租方是谁")
    tenant: Text | None = Field(None, description="承租方是谁")
    address: Text | None = Field(None, description="房屋地址")
    period: Period | None = Field(None, description="租期起止日期")
    rent: Money | None = Field(None, description="月租金")
    payment: Text | None = Field(None, description="租金怎么付、哪天之前付，如押一付三、每季度首月 5 日前")
    deposit: Money | None = Field(None, description="押金金额")
    deposit_refund: Text | None = Field(None, description="押金在什么条件下退、多久退")
    late_payment: Text | None = Field(None, description="晚交租金要承担什么")
    rent_increase: Text | None = Field(None, description="租期内能不能涨租金")
    early_termination: Text | None = Field(None, description="提前退租或解约要承担什么")
    repairs: Text | None = Field(None, description="房屋、家具家电坏了谁修")
    sublet: Text | None = Field(None, description="能不能转租")
    fees: Text | None = Field(None, description="水电燃气、物业、网络、取暖等费用谁出")


LABELS = {
    "landlord": "出租方", "tenant": "承租方", "address": "房屋地址", "period": "租期", "rent": "月租金",
    "payment": "付款方式", "deposit": "押金", "deposit_refund": "押金退还", "late_payment": "逾期付租",
    "rent_increase": "涨租", "early_termination": "提前解约", "repairs": "维修", "sublet": "转租", "fees": "各项费用",
}


class Dropped(BaseModel):
    field: str
    reason: str


class Extraction(BaseModel):
    info: ContractInfo
    dropped: list[Dropped]  # 模型给了、但没通过检查被扔掉的项，以及原因
    fixed: list[str]  # 出处写错了、代码按原文找到并改正的项


def strict_schema(schema: dict) -> dict:
    """把 Pydantic 生成的 JSON Schema 改成 OpenAI 严格模式要的样子：
    每个对象都写 additionalProperties: false、所有字段都列进 required（可以为空的字段本来就允许 null），去掉 default 和 title。"""

    def walk(node):
        if isinstance(node, dict):
            node.pop("default", None)
            node.pop("title", None)
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    schema = copy.deepcopy(schema)
    walk(schema)
    return schema


SCHEMA = strict_schema(ContractInfo.model_json_schema())
ADAPTERS = {name: TypeAdapter(field.annotation) for name, field in ContractInfo.model_fields.items()}

PROMPT = """你是租房合同的信息抽取助手。下面是一份按条款切好的合同，每一块开头方括号里是它的 id（preamble 是合同开头，sign 是签字栏）。
按给定的格式抽取信息：
- 只根据这份合同的原文。合同里没写的项填 null：不要猜，不要按常识或法律规定补。
- quotes 是合同原文里能证明这一项的句子，一字不差地复制（可以只复制其中相关的一段），clause 写这句话所在块的 id。
  这一项的内容写在几处，就各复制一句；value 里说到的每件事，都要能在 quotes 里找到。表格里的内容复制整行，带上 | 。
- value 用一句简短的大白话概括，只说合同怎么写，不评价好坏。
- 金额只写数字，单位元；日期写成 YYYY-MM-DD。"""


def _norm(text: str) -> str:
    """去掉所有空白再比：PDF 读出来的空格、换行和模型复制时的不一定一样。"""
    return re.sub(r"\s+", "", text)


def _date_forms(d: date) -> list[str]:
    """合同里同一个日期常见的几种写法。"""
    y, m, dd = d.year, d.month, d.day
    return [f"{y}年{m}月{dd}日", f"{y}年{m:02d}月{dd:02d}日", f"{y}-{m:02d}-{dd:02d}", f"{y}.{m}.{dd}", f"{y}.{m:02d}.{dd:02d}", f"{y}/{m}/{dd}", f"{y}/{m:02d}/{dd:02d}"]


def clause_texts(clauses: list[Clause]) -> dict[str, str]:
    return {c.id: _norm(c.heading + c.text) for c in clauses}


def check_quotes(quotes: list[Quote], texts: dict[str, str], label: str) -> tuple[list[Quote], list[str], str | None]:
    """逐句核对原文是否真的在合同里。出处写错了块，按原文找到并改正。
    返回（核对过的原文，改正记录，第一句找不到的原文）；有一句找不到，调用方就扔掉整项。"""
    checked: list[Quote] = []
    fixed: list[str] = []
    for q in quotes:
        text = _norm(q.text)
        if text in texts.get(q.clause, ""):
            checked.append(q)
            continue
        found = next((cid for cid, t in texts.items() if text in t), None)
        if found is None:
            return checked, fixed, q.text
        fixed.append(f"{label}：出处写的是 {q.clause}，原文其实在 {found}")
        checked.append(q.model_copy(update={"clause": found}))
    return checked, fixed, None


def verify(raw: dict, clauses: list[Clause]) -> Extraction:
    """逐项检查模型的输出。不调用模型，方便单独测试。"""
    texts = clause_texts(clauses)
    kept: dict = {}
    dropped: list[Dropped] = []
    fixed: list[str] = []
    for name, adapter in ADAPTERS.items():
        if raw.get(name) is None:
            continue
        label = LABELS[name]
        try:
            fact = adapter.validate_python(raw[name])
        except ValidationError as e:
            dropped.append(Dropped(field=name, reason=f"格式不对：{e.errors()[0]['msg']}"))
            continue
        quotes = [q for q in fact.quotes if _norm(q.text)]
        if not quotes:
            dropped.append(Dropped(field=name, reason="没有附原文"))
            continue
        checked, notes, missing = check_quotes(quotes, texts, label)
        fixed += notes
        if missing is not None:
            dropped.append(Dropped(field=name, reason=f"附的原文在合同里找不到：「{missing}」"))
            continue
        fact = fact.model_copy(update={"quotes": checked})
        joined = "".join(_norm(q.text) for q in checked)
        if isinstance(fact, Money) and not re.search(rf"(?<!\d){fact.yuan}(?!\d)", joined.replace(",", "")):
            dropped.append(Dropped(field=name, reason=f"金额 {fact.yuan} 不在附的原文里"))
            continue
        if isinstance(fact, Period) and not all(any(f in joined for f in _date_forms(d)) for d in (fact.start, fact.end)):
            dropped.append(Dropped(field=name, reason=f"日期 {fact.start}～{fact.end} 不在附的原文里"))
            continue
        kept[name] = fact
    return Extraction(info=ContractInfo(**kept), dropped=dropped, fixed=fixed)


def contract_text(clauses: list[Clause]) -> str:
    return "\n\n".join(f"[{c.id}] {c.heading}\n{c.text}".replace("] \n", "]\n") for c in clauses)


async def extract(clauses: list[Clause], chat: Chat = chat) -> Extraction:
    answer = await chat(
        [{"role": "system", "content": PROMPT}, {"role": "user", "content": contract_text(clauses)}],
        response_format={"type": "json_schema", "json_schema": {"name": "contract_info", "strict": True, "schema": SCHEMA}},
    )
    try:
        raw = json.loads(answer)
    except json.JSONDecodeError:
        return Extraction(info=ContractInfo(), dropped=[Dropped(field="*", reason="模型返回的不是 JSON")], fixed=[])
    return verify(raw, clauses)


def describe(fact: BaseModel) -> str:
    if isinstance(fact, Money):
        return f"{fact.yuan} 元"
    if isinstance(fact, Period):
        return f"{fact.start} 至 {fact.end}"
    return fact.value


if __name__ == "__main__":
    clauses = split_clauses(read_document(Path(sys.argv[1]).read_bytes()))
    result = asyncio.run(extract(clauses))
    for name, label in LABELS.items():
        fact = getattr(result.info, name)
        print(f"{label:6}  {describe(fact) if fact else '（合同里没写）'}")
        for q in fact.quotes if fact else []:
            print(f"          ← {q.clause}「{q.text}」")
    for d in result.dropped:
        print(f"扔掉 {LABELS.get(d.field, d.field)}：{d.reason}")
    for f in result.fixed:
        print(f"改正 {f}")
