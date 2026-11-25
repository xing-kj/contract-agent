"""后台任务实测（真的调模型）：排队、同一份合同再传、跑到一半重启、清单改了一个字。

    uv run python evals/jobs.py

用临时的库，不碰 data/jobs.sqlite。结果写到 evals/results/jobs-时间.md。
"""

import asyncio
import tempfile
import time
from datetime import datetime
from pathlib import Path
from statistics import median

from contract_agent import risks
from contract_agent.clauses import Clause, split_clauses
from contract_agent.jobs import MODEL_SLOTS, Jobs
from contract_agent.pdf import read_pages
from contract_agent.store import Store

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted((ROOT / "samples").glob("trap-0*.pdf")) + [ROOT / "samples" / f"zufang-0{i}.pdf" for i in (1, 2)]


def parse(path: Path) -> tuple[int, list[Clause]]:
    pages = read_pages(path.read_bytes())
    return len(pages), split_clauses(pages)


async def watch(queue: asyncio.Queue, start: float) -> dict:
    """等这个任务的事件，记下第一步出结果、全部做完各是第几秒。"""
    first = None
    while True:
        event = await queue.get()
        now = time.monotonic() - start
        first = first or now
        if event["type"] in ("done", "failed"):
            return {"first": first, "end": now, "status": event["type"]}


async def submit_and_wait(jobs: Jobs, items: list[tuple[str, int, list[Clause]]]) -> tuple[list[dict], float]:
    start = time.monotonic()
    ids = [jobs.submit(name, pages, clauses).id for name, pages, clauses in items]
    queues = [jobs.listen(job_id) for job_id in ids]  # 交上去之后马上订阅，中间没有 await，不会漏掉事件
    marks = await asyncio.gather(*[watch(q, start) for q in queues])
    return list(marks), time.monotonic() - start


async def main() -> list[str]:
    parsed = {path.stem: parse(path) for path in FILES}
    items = [(name, *parsed[name]) for name in parsed]
    tmp = Path(tempfile.mkdtemp())
    lines = [f"# 后台任务实测 {datetime.now():%Y-%m-%d %H:%M}", "", f"同时调模型最多 {MODEL_SLOTS} 个。", ""]

    # 1. 同时传 10 份
    jobs = Jobs(Store(tmp / "a.sqlite"))
    marks, total = await submit_and_wait(jobs, items)
    done = sum(m["status"] == "done" for m in marks)
    first, end = [m["first"] for m in marks], [m["end"] for m in marks]
    lines += [
        "| 情况 | 结果 |",
        "|---|---|",
        f"| 同时传 {len(items)} 份 | 做完 {done}/{len(items)}；真调模型 {jobs.calls} 次，同时最多 {jobs.peak} 个；全部做完 {total:.0f} s；"
        f"每份第一步出结果 中位 {median(first):.1f} s、最慢 {max(first):.1f} s；做完 中位 {median(end):.1f} s、最慢 {max(end):.1f} s |",
    ]

    # 2. 同一份再传
    before = jobs.calls
    marks, total = await submit_and_wait(jobs, [("zufang-01-again", *parsed["zufang-01"])])
    lines.append(f"| 同一份合同再传 | {marks[0]['status']}；调模型 {jobs.calls - before} 次；用时 {total * 1000:.0f} ms |")

    # 3. 清单改一个字：比对的提示词变了要重新调，抽取的请求没变
    original = risks.load
    edited = tuple(r.model_copy(update={"title": r.title + "（改过）"}) if r.id == "R01" else r for r in original())
    risks.load = lambda: edited
    try:
        before = jobs.calls
        marks, total = await submit_and_wait(jobs, [("zufang-01-edited", *parsed["zufang-01"])])
    finally:
        risks.load = original
    lines.append(f"| 清单改了一个字，同一份再传 | {marks[0]['status']}；调模型 {jobs.calls - before} 次；用时 {total:.1f} s |")

    # 4. 跑到一半重启：第一步做完就关掉，换一个新的 Jobs（像重新启动服务）接着跑
    path = tmp / "b.sqlite"
    first_run = Jobs(Store(path))
    job = first_run.submit("trap-01", *parsed["trap-01"])
    event = await first_run.listen(job.id).get()
    await first_run.stop()
    second_run = Jobs(Store(path))
    start = time.monotonic()
    resumed = second_run.resume()
    queue = second_run.listen(job.id)
    mark = await watch(queue, start)
    redo = "比对" if event["type"] == "info" else "抽取"
    lines.append(
        f"| 第一步（{'抽取' if event['type'] == 'info' else '比对'}）做完就关掉服务，再启动 | 接着跑 {len(resumed)} 个任务；{mark['status']}；"
        f"重启后调模型 {second_run.calls} 次（只重做{redo}）；重启后用时 {mark['end']:.1f} s |"
    )
    return lines


if __name__ == "__main__":
    lines = asyncio.run(main())
    out = ROOT / "evals" / "results" / f"jobs-{datetime.now():%Y%m%dT%H%M%S}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n结果：{out.relative_to(ROOT)}")
