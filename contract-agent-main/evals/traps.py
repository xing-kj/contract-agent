"""风险条款评测：用埋了坑的虚构合同（samples/trap-*.pdf），答案在 evals/traps.json。

    uv run python evals/traps.py --key            # 不调模型：检查答案表，生成 evals/traps-答案.md 给人审
    uv run python evals/traps.py                  # dev 组每份跑 3 次
    uv run python evals/traps.py --set test       # test 组：平时不跑，只在最后测
    uv run python evals/traps.py --runs 5 --label "提示词 A"   # 标签写进结果标题，比较改动时用

全部由代码打分，不用模型当裁判：
- 找到：报了这个编号，引用的原文落在答案表写的那一条里；编号对、条款不对算"位置不对"，不算找到
- 误报：报了答案表里没有的编号（"两可"的不算）；报的是"诱饵"另外标出来
- 没写提醒：合同故意没写的那几项，提示了算对；没故意不写却提示了算多报。
  模型引用相关条款把对应的风险报了出来（只写了押金金额，报 R02），也算提示到了，不算误报

结果写到 evals/results/traps-时间.md。
"""

import asyncio
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from contract_agent.checklist import load
from contract_agent.clauses import Clause, split_clauses
from contract_agent.extract import LABELS
from contract_agent.pdf import read_pages
from contract_agent.risks import WITHDRAWN, review

ROOT = Path(__file__).resolve().parents[1]
KEY = json.loads((ROOT / "evals" / "traps.json").read_text(encoding="utf-8"))["contracts"]
RISKS = {r.id: r for r in load()}
RISK_OF = {name: r.id for r in RISKS.values() for name in r.absent}  # 没写也提示的项 → 清单编号
ABSENT = set(RISK_OF)


def clauses_of(item: dict) -> list[Clause]:
    return split_clauses(read_pages((ROOT / item["file"]).read_bytes()))


def resolve(ref: str, clauses: list[Clause]) -> str:
    """答案表里的"第九条""六、""preamble"换成切分后的块 id。"""
    found = [c.id for c in clauses if c.id == ref or (c.heading and c.heading.startswith(ref))]
    if len(found) != 1:
        raise ValueError(f"条款 {ref} 对上了 {len(found)} 块")
    return found[0]


def load_key(item: dict) -> dict:
    """检查答案表并把条款换成块 id。有问题直接报错。"""
    clauses = clauses_of(item)
    out = {"file": item["file"], "set": item["set"], "about": item["about"], "clauses": clauses, "missing": item["missing"]}
    seen: set[str] = set()
    for kind in ("traps", "decoys", "either"):
        out[kind] = []
        for e in item[kind]:
            if e["risk"] not in RISKS:
                raise ValueError(f"{item['file']} {kind}：清单里没有 {e['risk']}")
            if e["risk"] in seen:
                raise ValueError(f"{item['file']}：{e['risk']} 出现在两处")
            seen.add(e["risk"])
            out[kind].append({**e, "ids": [resolve(r, clauses) for r in e["clause"]]})
    for name in item["missing"]:
        if name not in ABSENT:
            raise ValueError(f"{item['file']}：{name} 不是'没写也提示'的项")
    return out


def check_coverage(keys: list[dict]) -> list[str]:
    """每组都要把 26 种坑各埋至少一次。"""
    problems = []
    for name in ("dev", "test"):
        got = {t["risk"] for k in keys if k["set"] == name for t in k["traps"]}
        lack = sorted(set(RISKS) - got)
        if lack:
            problems.append(f"{name} 组没埋：{'、'.join(lack)}")
    return problems


def key_doc(keys: list[dict]) -> str:
    lines = ["# 埋坑合同答案表（给人审）", "", "由 `uv run python evals/traps.py --key` 从 evals/traps.json 生成，别手改。", ""]
    for k in keys:
        by_id = {c.id: c for c in k["clauses"]}
        lines += [f"## {Path(k['file']).stem}（{k['set']}）{k['about']}", ""]
        for kind, label in (("traps", "坑：应该报"), ("decoys", "诱饵：不该报"), ("either", "两可：报不报都行")):
            if not k[kind]:
                continue
            lines += [f"**{label}**", ""]
            for e in k[kind]:
                where = "、".join((by_id[i].heading or i).split(" ")[0] for i in e["ids"])
                lines.append(f"- {e['risk']} {RISKS[e['risk']].title}｜{where}｜{e['why']}")
                for i in e["ids"]:
                    lines.append(f"  > {by_id[i].text.replace(chr(10), ' ')[:160]}")
            lines.append("")
        missing = "、".join(f"{LABELS[m]}（{m}）" for m in k["missing"]) or "无"
        lines += [f"**合同故意没写、应提示**：{missing}", ""]
    return "\n".join(lines)


def grade(key: dict, findings: list, missing: list) -> dict:
    reported = {f.risk.id: {q.clause for q in f.quotes} for f in findings}
    found, misplaced, missed = [], [], []
    for t in key["traps"]:
        if t["risk"] not in reported:
            missed.append(t["risk"])
        elif reported[t["risk"]] & set(t["ids"]):
            found.append(t["risk"])
        else:
            misplaced.append(t["risk"])
    want_missing = set(key["missing"])
    # 合同故意没写的事，模型引用相关条款把对应的那条风险报了出来（比如只写了押金金额，报 R02）：
    # 用户看到的是同一条提醒，算提示到了，也不算误报
    via_hit = {name for name in want_missing if RISK_OF[name] in reported}
    expected = {t["risk"] for t in key["traps"]} | {e["risk"] for e in key["either"]} | {RISK_OF[name] for name in want_missing}
    decoys = {d["risk"] for d in key["decoys"]}
    false = [r for r in reported if r not in expected]
    got_missing = {m.field for m in missing} | via_hit
    return {
        "found": found, "misplaced": misplaced, "missed": missed,
        "false": false, "decoy_hits": [r for r in false if r in decoys],
        "missing_ok": sorted(got_missing & want_missing), "missing_lost": sorted(want_missing - got_missing),
        "missing_extra": sorted(got_missing - want_missing),
    }


async def run_one(key: dict, gate: asyncio.Semaphore) -> dict:
    async with gate:
        extraction, report = await review(key["clauses"])
    result = grade(key, report.findings, report.missing)
    withdrawn = [d for d in report.dropped if d.reason.startswith(WITHDRAWN)]
    result["withdrawn"] = {d.field: d.reason[len(WITHDRAWN):] for d in withdrawn}
    result["dropped"] = [f"{d.field}：{d.reason}" for d in report.dropped + extraction.dropped if d not in withdrawn]
    # 模型报了什么：看误报的理由、漏掉的那条被报成了什么
    result["reported"] = {f.risk.id: {"clauses": sorted({q.clause for q in f.quotes}), "how": f.how} for f in report.findings}
    return {"file": key["file"], **result}


def tables(keys: list[dict], runs: list[dict], n: int) -> list[str]:
    lines = ["| 合同 | 坑 | 找到（每次） | 位置不对 | 误报（其中诱饵） | 没写：提示对 / 应提示 | 没写：多报 |", "|---|---|---|---|---|---|---|"]
    total = Counter()
    for k in keys:
        rs = [r for r in runs if r["file"] == k["file"]]
        found = [len(r["found"]) for r in rs]
        cells = [
            Path(k["file"]).stem, str(len(k["traps"])), "、".join(map(str, found)),
            str(sum(len(r["misplaced"]) for r in rs)),
            f"{sum(len(r['false']) for r in rs)}（{sum(len(r['decoy_hits']) for r in rs)}）",
            f"{sum(len(r['missing_ok']) for r in rs)}/{len(k['missing']) * len(rs)}",
            str(sum(len(r["missing_extra"]) for r in rs)),
        ]
        lines.append("| " + " | ".join(cells) + " |")
        total.update(traps=len(k["traps"]) * len(rs), found=sum(found), false=sum(len(r["false"]) for r in rs),
                     want=len(k["missing"]) * len(rs), ok=sum(len(r["missing_ok"]) for r in rs),
                     extra=sum(len(r["missing_extra"]) for r in rs))
    lines += ["", f"合计（{n} 次）：坑找到 {total['found']}/{total['traps']}，误报 {total['false']} 条，"
              f"没写提示对 {total['ok']}/{total['want']}，多报 {total['extra']} 条", ""]

    lines += ["## 每个坑找到几次", "", "| 合同 | 坑 | 找到 | 位置不对 |", "|---|---|---|---|"]
    for k in keys:
        rs = [r for r in runs if r["file"] == k["file"]]
        for t in k["traps"]:
            hit = sum(t["risk"] in r["found"] for r in rs)
            off = sum(t["risk"] in r["misplaced"] for r in rs)
            mark = "" if hit == len(rs) else " ←"
            lines.append(f"| {Path(k['file']).stem} | {t['risk']} {RISKS[t['risk']].title} | {hit}/{len(rs)}{mark} | {off or ''} |")
    lines += ["", "## 漏报、误报和没写提示的错", ""]
    for k in keys:
        rs = [r for r in runs if r["file"] == k["file"]]
        stem = Path(k["file"]).stem
        for t in k["traps"]:
            lost = [r for r in rs if t["risk"] not in r["found"]]
            if not lost:
                continue
            # 漏掉的那几次，同一条上报了别的什么
            other = Counter(risk for r in lost for risk, x in r["reported"].items() if risk != t["risk"] and set(x["clauses"]) & set(t["ids"]))
            said = "、".join(f"{risk}×{n}" for risk, n in other.items()) or "什么都没报"
            back = sum(t["risk"] in r["withdrawn"] for r in lost)
            said += f"；报了又撤回 {back} 次" if back else ""
            lines.append(f"- {stem} 漏 {t['risk']} {RISKS[t['risk']].title} {len(lost)}/{len(rs)} 次（{'/'.join(t['ids'])} 上{said}）")
        kinds = {e["risk"]: label for kind, label in (("traps", "坑"), ("decoys", "诱饵"), ("either", "两可")) for e in k[kind]}
        for risk, count in Counter(x for r in rs for x in r["withdrawn"]).items():
            why = next(r["withdrawn"][risk] for r in rs if risk in r["withdrawn"])
            lines.append(f"- {stem} 撤回 {risk} {RISKS[risk].title}（{kinds.get(risk, '没埋')}）{count}/{len(rs)} 次{why}")
        for risk, count in Counter(x for r in rs for x in r["false"]).items():
            tag = "诱饵" if any(risk in r["decoy_hits"] for r in rs) else "没埋"
            how = next(r["reported"][risk] for r in rs if risk in r["false"])
            lines.append(f"- {stem} 误报 {risk} {RISKS[risk].title}（{tag}）{count}/{len(rs)} 次：{'/'.join(how['clauses'])}「{how['how']}」")
        for name, count in Counter(x for r in rs for x in r["missing_lost"]).items():
            lines.append(f"- {stem} 没提示 {LABELS[name]} {count}/{len(rs)} 次")
        for name, count in Counter(x for r in rs for x in r["missing_extra"]).items():
            lines.append(f"- {stem} 多提示 {LABELS[name]} {count}/{len(rs)} 次")
        for note, count in Counter(x for r in rs for x in r["dropped"]).items():
            lines.append(f"- {stem} 扔掉 {note}（{count}/{len(rs)} 次）")
    return lines


def main() -> None:
    keys = [load_key(item) for item in KEY]
    problems = check_coverage(keys)
    if "--key" in sys.argv:
        out = ROOT / "evals" / "traps-答案.md"
        out.write_text(key_doc(keys) + "\n", encoding="utf-8")
        print(f"答案表检查通过，{len(keys)} 份合同，坑 {sum(len(k['traps']) for k in keys)} 个" if not problems else "\n".join(problems))
        print(f"生成 {out.relative_to(ROOT)}")
        return
    if problems:
        sys.exit("\n".join(problems))
    which = sys.argv[sys.argv.index("--set") + 1] if "--set" in sys.argv else "dev"
    n = int(sys.argv[sys.argv.index("--runs") + 1]) if "--runs" in sys.argv else 3
    chosen = [k for k in keys if which == "all" or k["set"] == which]

    async def run_all():
        gate = asyncio.Semaphore(4)
        return await asyncio.gather(*[run_one(k, gate) for k in chosen for _ in range(n)])

    runs = asyncio.run(run_all())
    label = sys.argv[sys.argv.index("--label") + 1] if "--label" in sys.argv else ""
    lines = [f"# 风险条款评测 {datetime.now():%Y-%m-%d %H:%M}（{which} 组，每份 {n} 次）{label}", ""] + tables(chosen, runs, n)
    out = ROOT / "evals" / "results" / f"traps-{which}-{datetime.now():%Y%m%dT%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n结果：{out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
