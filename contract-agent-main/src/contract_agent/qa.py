"""追问：用户就这份合同提问，回答流式推给前端，说到合同的地方附出处。

两种给模型看合同的方式，评测里比较过（evals/qa.py，见 README）：
- full：整份合同都给模型；
- retrieve：只给向量检索出的最相关几条。
84 条的长合同全文也只有约 5400 token，全文方式更准、也更快（检索说"没写"时要改看全文重答）。
所以默认 auto：合同不超过 FULL_TEXT_LIMIT 字就用全文，更长才检索。

出处的写法是〔c39：原文片段〕，回答说完后代码逐个核对：片段在那一条里 → 核对通过；
在别的条里 → 改正出处；哪条都没有 → 标成"未能核对"（不删：回答是一整段话，删掉一句可能读不通）。

"合同里没写"由模型说，代码核对不了。检索模式下它可能只是没检索到，所以回答里说到"没写"，
就改成看全文再答一次。不只看开头：模型常在句中顺带一句"合同里也没有约定换房"，开头却没写"合同里没写"。

    uv run python -m contract_agent.qa samples/zufang-03.pdf 我能养猫吗
    uv run python -m contract_agent.qa samples/zufang-03.pdf 我能养猫吗 --retrieve   # 强制检索
"""

import asyncio
import re
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

from pydantic import BaseModel

from .clauses import Clause, split_clauses
from .extract import _norm
from .index import search
from .llm import stream_chat
from .textdoc import read_document

TOP_K = 6
# 超过这个字数才检索。实测过的最长合同是 6550 字（约 5400 token），全文方式没有输；
# 这个上限还没有测过，留了约 5 倍余量，等有更长的合同再用评测定
FULL_TEXT_LIMIT = 30_000
NOT_WRITTEN = "合同里没写"

PROMPT = f"""你回答租客关于他这份租房合同的问题。只根据下面给出的合同条款回答，每一块开头方括号里是它的 id。
- 说到合同怎么写时，在那句话后面紧跟出处，格式是〔条款id：原文〕。原文一字不差地从那一块里复制最相关的一小段，不超过 40 字。
  例如：押金在退房验收后 30 日内退还〔c39：自退房验收之日起 30 日内〕。
- 给出的条款里找不到答案时，第一句话就写"{NOT_WRITTEN}"，然后可以建议向房东或公寓问清楚。不要按常识或法律规定补充合同没写的内容。
- 只说合同怎么写，不判断条款合不合法、有没有效。
- 简短，三五句话以内。"""

CITATION = re.compile(r"〔\s*([A-Za-z]+\d*)\s*[：:]\s*([^〕]+?)\s*〕")
# 回答里说某件事合同没写的各种说法。检索模式下出现在哪里都算：多看一次全文只多花点 token，漏看可能答错
MISSING_HINT = re.compile(r"没写|没有写|没有(约定|说明|提到|提及|规定|写明)|未(约定|说明|提到|提及|规定|写明)")


class Citation(BaseModel):
    clause: str  # 核对后的出处；未能核对时是模型写的那个
    text: str
    status: str  # ok 核对通过 / fixed 改正了出处 / unverified 合同里找不到这段原文
    heading: str = ""
    pages: list[int] = []


def contract_part(clauses: list[Clause]) -> str:
    return "\n\n".join(f"[{c.id}] {' '.join(x for x in (c.chapter, c.heading) if x)}\n{c.text}".replace("] \n", "]\n") for c in clauses)


def says_not_written(answer: str) -> bool:
    """整个回答是不是"合同里没写"：只看开头。"""
    return answer.lstrip(" \n“\"「").startswith(NOT_WRITTEN)


def may_be_missing(answer: str) -> bool:
    """回答里有没有说到哪件事合同没写：哪里都算。检索模式下用它决定要不要改看全文。"""
    return bool(MISSING_HINT.search(answer))


def check_citations(answer: str, clauses: list[Clause]) -> list[Citation]:
    by_id = {c.id: c for c in clauses}
    texts = {c.id: _norm(c.heading + c.text) for c in clauses}
    out: list[Citation] = []
    for cid, quote in CITATION.findall(answer):
        q = _norm(quote)
        if q and q in texts.get(cid, ""):
            status, found = "ok", cid
        else:
            found = next((k for k, t in texts.items() if q and q in t), None)
            status = "fixed" if found else "unverified"
        c = by_id.get(found or cid)
        out.append(Citation(clause=found or cid, text=quote, status=status, heading=c.heading if c else "", pages=c.pages if c else []))
    return out


def choose_mode(clauses: list[Clause], mode: str) -> str:
    if mode != "auto":
        return mode
    return "full" if len(contract_part(clauses)) <= FULL_TEXT_LIMIT else "retrieve"


async def _stream(question: str, part: list[Clause], partial: bool, usage: dict) -> AsyncIterator[str]:
    note = "下面是从合同里检索出的、和问题最相关的几条（不是全文）。" if partial else "下面是合同全文。"
    messages = [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": f"{note}\n\n{contract_part(part)}\n\n问题：{question}"},
    ]
    async for text in stream_chat(messages, temperature=0.2, usage=usage):
        yield text


async def answer(question: str, clauses: list[Clause], mode: str = "auto", k: int = TOP_K) -> AsyncIterator[dict]:
    """事件流：retrieved（检索到哪几条，只有检索方式有）→ delta……→（可能有 retry）→ citations → done。"""
    mode = choose_mode(clauses, mode)
    started = time.monotonic()
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
    first_ms: int | None = None

    async def run(part: list[Clause], partial: bool) -> AsyncIterator[dict]:
        nonlocal first_ms
        usage: dict = {}
        async for text in _stream(question, part, partial, usage):
            if first_ms is None:
                first_ms = int((time.monotonic() - started) * 1000)
            yield {"type": "delta", "text": text}
        for key in usage_total:
            usage_total[key] += usage.get(key, 0)

    used = mode
    text = ""
    if mode == "retrieve":
        t = time.monotonic()
        ids = set(search(clauses, question, k))
        part = [c for c in clauses if c.id in ids]  # 按合同原来的顺序给模型
        yield {"type": "retrieved", "clauses": [c.id for c in part], "ms": int((time.monotonic() - t) * 1000)}
        async for event in run(part, partial=True):
            text += event["text"]
            yield event
        if may_be_missing(text):
            # 可能只是没检索到：看全文再答一次，前端用新的回答替换旧的
            yield {"type": "retry", "reason": "检索到的几条里有没找到的，改看合同全文再答一次"}
            used, text = "retrieve+full", ""
            async for event in run(clauses, partial=False):
                text += event["text"]
                yield event
    else:
        async for event in run(clauses, partial=False):
            text += event["text"]
            yield event

    yield {"type": "citations", "items": [c.model_dump() for c in check_citations(text, clauses)]}
    yield {
        "type": "done", "mode": used, "not_written": says_not_written(text),
        "first_ms": first_ms, "total_ms": int((time.monotonic() - started) * 1000), **usage_total,
    }


async def _main(path: str, question: str, mode: str) -> None:
    clauses = split_clauses(read_document(Path(path).read_bytes()))
    async for event in answer(question, clauses, mode):
        if event["type"] == "delta":
            print(event["text"], end="", flush=True)
        elif event["type"] == "retrieved":
            print(f"（检索到 {'、'.join(event['clauses'])}，{event['ms']} ms）")
        elif event["type"] == "retry":
            print(f"\n（{event['reason']}）")
        elif event["type"] == "citations":
            print()
            for c in event["items"]:
                print(f"  出处 {c['clause']} {c['heading']}「{c['text']}」：{ {'ok': '核对通过', 'fixed': '改正了出处', 'unverified': '未能核对'}[c['status']] }")
        elif event["type"] == "done":
            print(f"（{event['mode']}，首字 {event['first_ms']} ms，共 {event['total_ms']} ms，输入 {event['prompt_tokens']} token）")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    mode = "full" if "--full" in sys.argv else "retrieve" if "--retrieve" in sys.argv else "auto"
    asyncio.run(_main(args[0], " ".join(args[1:]), mode))
