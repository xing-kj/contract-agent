"""对"没有踩到的坑"逐条做 AI 分析：这份合同是怎么写的、为什么这里不构成这个坑。

输入：已完成审查的任务号（/jobs/{id} 里已有切好的条款和命中的风险）。
输出：{输出目录}/{任务号}.json，每条未命中清单项一条记录：
  rid / verdict（已明确写好 | 合同未涉及）/ how（合同怎么写的）/ quotes（核对过的原文）/ why_safe

防幻觉：结构化输出（JSON Schema 严格模式）+ 每句引用逐字核对在合同里（复用 extract.check_quotes），
核对不过的引用直接扔掉，不留编造的"合同写了什么"。

    uv run python scripts/analyze_safe.py 任务号1 任务号2 [--dir 输出目录] [--base http://127.0.0.1:8300]
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pydantic import BaseModel, Field

from contract_agent.checklist import load
from contract_agent.clauses import Clause
from contract_agent.extract import Quote, _norm, check_quotes, contract_text, strict_schema
from contract_agent.llm import chat
from contract_agent.risks import annex_note

BASE = "http://127.0.0.1:8300"


class SafeItem(BaseModel):
    rid: str = Field(description="风险清单编号，如 R05")
    verdict: str = Field(description="只能填：已明确写好 或 合同未涉及")
    how: str = Field(description="这份合同对应是怎么写的（已明确写好时概括合同安排；未涉及时说明合同里没有此类安排）")
    quotes: list[Quote] = Field(default_factory=list, description="合同原文引用，已明确写好时必须给，一到三句")
    why_safe: str = Field(description="一两句：为什么这样写租客就不会踩这个坑")


class SafeAnalysis(BaseModel):
    items: list[SafeItem]


PROMPT = """你是租房合同审查助手。给你一份租房合同全文和一批风险项——这些项经核对**没有**在这份合同里踩坑，但需要逐项向用户解释为什么没有。

对每个风险项输出：
- verdict：只能填"已明确写好"（合同对这类风险有明确、对租客合理的安排）或"合同未涉及"（合同里根本没有此类安排，坑无从产生）。
- how：这份合同实际是怎么写的（已明确写好时概括对应条款安排；合同未涉及时写明合同里没有此类内容）。
- quotes：verdict 是"已明确写好"时，从合同里一字不差地复制最相关的原文（一到三句），并注明所在块 id；"合同未涉及"时 quotes 留空。
- why_safe：一两句话，说明为什么这样写（或没有这种安排）租客就不会踩这个坑。只描述合同安排，不判断条款合法性，不引用合同里没有的内容。"""


def risk_brief(r) -> str:
    return f"{r.id} {r.title}\n  典型写法：{r.patterns}\n  什么情况不算：{r.not_risk}"


def texts_of(clauses: list[Clause]) -> dict[str, str]:
    return {c.id: _norm(c.heading + c.text) for c in clauses}


async def analyze(job_id: str, out_dir: Path) -> Path:
    import urllib.request

    with urllib.request.urlopen(f"{BASE}/jobs/{job_id}", timeout=30) as resp:
        job = json.loads(resp.read())
    clauses = [Clause(**c) for c in job["clauses"]]
    hit = {f["risk"]["id"] for f in (job.get("risks") or {}).get("findings", [])}
    items = [r for r in load() if r.id not in hit]
    listing = "\n\n".join(risk_brief(r) for r in items)

    user = f"{contract_text(clauses)}\n\n{annex_note(clauses)}\n\n以下是没有踩到坑的风险项，逐项分析：\n\n{listing}"
    answer = await chat(
        [{"role": "system", "content": PROMPT}, {"role": "user", "content": user}],
        response_format={"type": "json_schema", "json_schema": {"name": "safe_analysis", "strict": True, "schema": strict_schema(SafeAnalysis.model_json_schema())}},
    )
    parsed = SafeAnalysis.model_validate_json(answer)

    texts = texts_of(clauses)
    checked, dropped, fixed = [], [], []
    for item in parsed.items:
        if item.verdict == "已明确写好" and item.quotes:
            qs, fixed_notes, bad = check_quotes(item.quotes, texts, item.rid)
            if bad:
                item = item.model_copy(update={"quotes": qs, "how": item.how + "（部分原文核对未通过，已略去）"})
            elif fixed_notes:
                item = item.model_copy(update={"quotes": qs})
            fixed += fixed_notes
        elif item.quotes:
            item = item.model_copy(update={"quotes": []})
        checked.append(item)

    result = {
        "job": job_id,
        "filename": job.get("filename"),
        "hit": sorted(hit),
        "items": [i.model_dump(mode="json") for i in checked],
        "fixed": fixed,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{job_id}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for i in checked if i.quotes)
    print(f"{job.get('filename')}: {len(checked)} 条未命中项分析完成（含原文引用 {ok} 条，出处改正 {len(fixed)} 处）→ {out}")
    return out


def main() -> None:
    global BASE
    argv = sys.argv[1:]
    base = next((a.split("=", 1)[1] for a in argv if a.startswith("--base=")), None)
    if base:
        BASE = base.rstrip("/")
    d = None
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--base="):
            i += 1
        elif a == "--base":
            i += 2
        elif a.startswith("--dir="):
            d = a.split("=", 1)[1]
            i += 1
        elif a == "--dir":
            d = argv[i + 1]
            i += 2
        else:
            rest.append(a)
            i += 1
    out_dir = Path(d) if d else Path.cwd()
    if not rest:
        raise SystemExit("用法：uv run python scripts/analyze_safe.py 任务号... [--dir 输出目录]")
    for job_id in rest:
        asyncio.run(analyze(job_id, out_dir))


if __name__ == "__main__":
    main()
