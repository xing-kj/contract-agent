"""后台任务：上传后马上给任务号，抽取和比对在后台跑，做完一步存一步、推一步。

- 抽取和比对同时跑，谁先做完谁先存、先推给页面；两步都做完，再算"合同里没写"的事。
- 每次调模型的回答一回来就存下（按整个请求的指纹）。之后遇到一模一样的请求，直接用存着的：
  - 同一份合同再审一次，不再调模型；
  - 服务重启后，没做完的任务从头再跑，做完的那次调用直接用存着的回答，只有没做完的才真的调模型；
  - 清单、提示词、模型改了，请求就不一样了，自然重新调用，不会拿到旧结论。
  只存模型的回答，不存核对后的结论：核对由代码做，快、不花钱，每次重做，改了核对规则马上生效。
- 同时调模型最多 MODEL_SLOTS 个，多出来的排队。
- 评测不走这里，直接调模型：评测要量模型每次答得不一样，用存着的回答就量不出来了。
"""

import asyncio
import hashlib
import json
from collections import defaultdict

from .checklist import load
from .clauses import Clause
from .config import settings
from .extract import extract
from .llm import Chat, chat
from .risks import find_missing, match
from .store import Job, Store

MODEL_SLOTS = 4


def request_key(messages: list[dict], options: dict) -> str:
    """一次请求的指纹：模型、提示词（含清单）、合同、输出格式，任何一样变了，指纹就变。"""
    body = json.dumps({"model": settings().llm_model, "messages": messages, **options}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def is_json(text: str) -> bool:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


def events_of(job: Job) -> list[dict]:
    """这个任务到现在为止该推给页面的事件。页面中途打开、刷新时，先补发这些。"""
    events = []
    if job.info is not None:
        events.append({"type": "info", **job.info})
    if job.risks is not None:
        events.append({"type": "risks", **job.risks})
    if job.status == "done":
        events.append({"type": "done", "missing": job.missing})
    elif job.status == "failed":
        events.append({"type": "failed", "error": job.error})
    return events


class Jobs:
    def __init__(self, store: Store, chat: Chat = chat, slots: int = MODEL_SLOTS):
        self.store = store
        self._chat = chat  # 真正调模型的函数，测试时换成假的
        self.slots = asyncio.Semaphore(slots)
        self.calls = 0  # 真的调了几次模型（用存着的回答不算）
        self.busy = 0  # 正在调模型的个数
        self.peak = 0  # busy 最多到过几个
        self.running: dict[str, asyncio.Task] = {}
        self.listeners: dict[str, set[asyncio.Queue]] = defaultdict(set)

    async def chat(self, messages: list[dict], **options) -> str:
        """交给抽取、比对用：先查存着的回答，没有才排队调模型，回答一回来就存下。"""
        key = request_key(messages, options)
        if (saved := self.store.answer(key)) is not None:
            return saved
        async with self.slots:
            # 排队的时候，前面的任务可能刚问过一模一样的请求
            if (saved := self.store.answer(key)) is not None:
                return saved
            self.calls += 1
            self.busy += 1
            self.peak = max(self.peak, self.busy)
            try:
                text = await self._chat(messages, **options)
            finally:
                self.busy -= 1
        if is_json(text):  # 格式坏了的回答不存：下次重新调，不要一直拿到同一个坏回答
            self.store.keep_answer(key, text)
        return text

    def submit(self, filename: str, pages: int, clauses: list[Clause]) -> Job:
        job = self.store.add(filename, pages, clauses)
        self.start(job.id)
        return job

    def start(self, job_id: str) -> None:
        task = asyncio.create_task(self.run(job_id))
        self.running[job_id] = task  # 留着引用：没人引用的任务可能被回收；关服务时也靠它取消
        task.add_done_callback(lambda _: self.running.pop(job_id, None))

    def resume(self) -> list[str]:
        """服务启动时调用：上次关掉或崩溃时还没做完的任务，接着跑。"""
        ids = self.store.unfinished()
        for job_id in ids:
            self.start(job_id)
        return ids

    def retry(self, job_id: str) -> None:
        self.store.save(job_id, status="running", error=None)
        self.start(job_id)

    async def stop(self) -> None:
        """关服务时取消还在跑的任务。它们在库里还是"进行中"，下次启动接着跑。"""
        tasks = list(self.running.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self, job_id: str) -> None:
        clauses = self.store.get(job_id).clauses

        async def extraction():
            result = await extract(clauses, self.chat)
            self.update(job_id, "info", info=result.model_dump(mode="json"))
            return result

        async def matching():
            findings, dropped, fixed = await match(clauses, self.chat)
            risks = {"findings": [f.model_dump(mode="json") for f in findings], "dropped": [d.model_dump() for d in dropped], "fixed": fixed}
            self.update(job_id, "risks", risks=risks)
            return findings

        results = await asyncio.gather(extraction(), matching(), return_exceptions=True)
        errors = [r for r in results if isinstance(r, Exception)]
        if errors:
            # 做完的那一步，回答已经存下了；重试时直接用，只重做失败的
            self.update(job_id, "failed", status="failed", error=f"模型调用失败：{errors[0]}")
            return
        info, findings = results
        missing = find_missing(info, findings, load())
        self.update(job_id, "done", status="done", missing=[m.model_dump(mode="json") for m in missing])

    def update(self, job_id: str, kind: str, **fields) -> None:
        """存下这一步，再推给正在看这个任务的页面。"""
        self.store.save(job_id, **fields)
        event = next(e for e in events_of(self.store.get(job_id)) if e["type"] == kind)
        for queue in self.listeners.get(job_id, ()):
            queue.put_nowait(event)

    def listen(self, job_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.listeners[job_id].add(queue)
        return queue

    def unlisten(self, job_id: str, queue: asyncio.Queue) -> None:
        self.listeners[job_id].discard(queue)
        if not self.listeners[job_id]:
            del self.listeners[job_id]
