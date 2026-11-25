"""追问评测：检索 vs 全文。

    uv run python evals/qa.py            # 只比较检索召回率（关键词 / 向量 / 融合），不调大模型
    uv run python evals/qa.py --answer   # 再让两种方式各回答一遍，比正确率、耗时、token

结果写到 evals/results/qa-时间.md。
"""

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import median

from contract_agent.clauses import split_clauses
from contract_agent.index import fuse, keyword_rank, vector_rank
from contract_agent.pdf import read_pages
from contract_agent.qa import answer

ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / "evals" / "qa.json").read_text(encoding="utf-8"))
CLAUSES = split_clauses(read_pages((ROOT / SPEC["contract"]).read_bytes()))
QUESTIONS = SPEC["questions"]
TYPES = ["直白", "换说法", "没写"]
CUTOFFS = [1, 3, 6]


def recall_table() -> list[str]:
    written = [q for q in QUESTIONS if q["expect"]]
    vector_rank(CLAUSES, "预热")  # 先加载模型、建好向量，下面只计检索本身的时间
    ranks: dict[str, list[list[str]]] = {"关键词": [], "向量": [], "融合": []}
    ms: list[float] = []
    for q in written:
        kw = keyword_rank(CLAUSES, q["q"], 20)
        t = time.perf_counter()
        vec = vector_rank(CLAUSES, q["q"], 20)
        ms.append((time.perf_counter() - t) * 1000)
        ranks["关键词"].append(kw)
        ranks["向量"].append(vec)
        ranks["融合"].append(fuse([kw, vec], 20))
    lines = [f"## 检索召回率（{len(CLAUSES)} 块，{len(written)} 个合同里写了的问题；正确条款在前 k 条里就算找到）", "",
             "| 方法 | 说法 | " + " | ".join(f"前 {k}" for k in CUTOFFS) + " |", "|---|---|" + "---|" * len(CUTOFFS)]
    for name, rs in ranks.items():
        for kind in ("直白", "换说法"):
            pairs = [(q, r) for q, r in zip(written, rs) if q["type"] == kind]
            cells = [f"{sum(any(c in r[:k] for c in q['expect']) for q, r in pairs)}/{len(pairs)}" for k in CUTOFFS]
            lines.append(f"| {name} | {kind} | " + " | ".join(cells) + " |")
    lines += ["", f"向量检索一次（问题向量化 + 查库）：中位 {median(ms):.0f} ms，最慢 {max(ms):.0f} ms", ""]
    misses = [(q, r) for q, r in zip(written, ranks["向量"]) if not any(c in r[:6] for c in q["expect"])]
    for q, r in misses:
        lines.append(f"- 向量前 6 没找到：{q['q']}（应为 {'/'.join(q['expect'])}，实际 {'、'.join(r[:6])}）")
    return lines


async def ask(q: dict, mode: str, gate: asyncio.Semaphore) -> dict:
    async with gate:
        text, result, citations = "", {}, []
        async for event in answer(q["q"], CLAUSES, mode):
            if event["type"] == "delta":
                text += event["text"]
            elif event["type"] == "retry":
                text = ""
            elif event["type"] == "citations":
                citations = event["items"]
            elif event["type"] == "done":
                result = event
    verified = {c["clause"] for c in citations if c["status"] != "unverified"}
    # 写了的：引用了对的条款、核对通过就算对（"合同里没写会不会涨，只写了另行协商〔c21〕"这样的回答也对）
    # 没写的：要以"合同里没写"开头
    ok = bool(verified & set(q["expect"])) if q["expect"] else result["not_written"]
    stats = {k: v for k, v in result.items() if k not in ("type", "mode")}
    return {**q, **stats, "mode": mode, "used": result.get("mode"), "ok": ok, "text": text, "citations": citations}


def answer_tables(runs: list[dict]) -> list[str]:
    lines = ["## 回答（每题每种方式答一次）", "", "| 方式 | " + " | ".join(TYPES) + " | 出处未能核对 | 首字中位 | 总耗时中位 | 输入 token 中位 | 检索后改看全文 |", "|---|" + "---|" * (len(TYPES) + 5)]
    for mode, label in (("retrieve", "检索前 6 条"), ("full", "全文")):
        rs = [r for r in runs if r["mode"] == mode]
        cells = []
        for kind in TYPES:
            group = [r for r in rs if r["type"] == kind]
            cells.append(f"{sum(r['ok'] for r in group)}/{len(group)}")
        unverified = sum(c["status"] == "unverified" for r in rs for c in r["citations"])
        total = sum(len(r["citations"]) for r in rs)
        retried = sum(1 for r in rs if r.get("used") == "retrieve+full")
        lines.append(
            f"| {label} | " + " | ".join(cells)
            + f" | {unverified}/{total} | {median(r['first_ms'] for r in rs) / 1000:.1f}s | {median(r['total_ms'] for r in rs) / 1000:.1f}s"
            + f" | {median(r['prompt_tokens'] for r in rs):.0f} | {retried}/{len(rs)} |"
        )
    lines += ["", "## 答错的", ""]
    for r in runs:
        if not r["ok"]:
            cited = "、".join(f"{c['clause']}({c['status']})" for c in r["citations"]) or "无"
            lines.append(f"- [{r['mode']}·{r['type']}] {r['q']}（应为 {'/'.join(r['expect']) or '没写'}；引用 {cited}）：{r['text'].strip()[:120]}")
    return lines


async def run_answers() -> list[dict]:
    gate = asyncio.Semaphore(4)
    vector_rank(CLAUSES, "预热")
    return await asyncio.gather(*[ask(q, mode, gate) for q in QUESTIONS for mode in ("retrieve", "full")])


def main() -> None:
    lines = [f"# 追问评测 {datetime.now():%Y-%m-%d %H:%M}", "", f"合同：{SPEC['contract']}", ""] + recall_table()
    if "--answer" in sys.argv:
        runs = asyncio.run(run_answers())
        lines += [""] + answer_tables(runs)
    out = ROOT / "evals" / "results" / f"qa-{datetime.now():%Y%m%dT%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n结果：{out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
