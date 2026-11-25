"""本地存储（SQLite）：后台任务的进度和结果，以及每次调模型的回答。

文件默认是 data/jobs.sqlite（data/ 不进仓库）。合同内容也存在里面，只留在这台机器上，删掉这个文件就全清了。
每次读写都很小，直接在事件循环里做，不放线程池；写完马上提交，服务被强行关掉也不丢已经存下的东西。
"""

import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from .clauses import Clause

SCHEMA = """
create table if not exists jobs (
    id text primary key,
    filename text not null,
    pages integer not null,
    clauses text not null,   -- 切好的条款
    status text not null,    -- running 进行中 / done 完成 / failed 失败
    info text,               -- 抽取的结果：做完就存
    risks text,              -- 比对的结果：做完就存
    missing text,            -- 合同里没写的事：两步都做完才算得出
    error text,
    created real not null,
    updated real not null
);
create table if not exists answers (
    key text primary key,    -- 整个请求的指纹：模型、提示词（含清单）、合同、输出格式
    answer text not null,
    created real not null
);
"""

# 能改的几项。save() 拼 SQL 用的是这里的名字，不是外面传进来的
COLUMNS = {"status", "info", "risks", "missing", "error"}


class Job(BaseModel):
    id: str
    filename: str
    pages: int
    status: Literal["running", "done", "failed"]
    clauses: list[Clause]
    info: dict | None = None  # 抽取：info、dropped、fixed，和 /contracts/extract 返回的一样
    risks: dict | None = None  # 比对：findings、dropped、fixed
    missing: list[dict] | None = None  # 合同里没写、值得问的事
    error: str | None = None
    created: float
    updated: float


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # 只在事件循环那一个线程里读写；测试客户端会在另一个线程里跑服务，所以关掉"同一线程"的检查
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def add(self, filename: str, pages: int, clauses: list[Clause]) -> Job:
        # 任务号随机生成、猜不到：拿到任务号就能看到整份合同
        job_id = secrets.token_urlsafe(12)
        now = time.time()
        body = json.dumps([c.model_dump(mode="json") for c in clauses], ensure_ascii=False)
        with self.db:  # with 块结束时提交
            self.db.execute(
                "insert into jobs (id, filename, pages, clauses, status, created, updated) values (?, ?, ?, ?, 'running', ?, ?)",
                (job_id, filename, pages, body, now, now),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> Job | None:
        row = self.db.execute("select * from jobs where id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        for key in ("clauses", "info", "risks", "missing"):
            if data[key] is not None:
                data[key] = json.loads(data[key])
        return Job.model_validate(data)

    def save(self, job_id: str, **fields: Any) -> None:
        """改任务的某几项。info、risks、missing 传能转成 JSON 的 dict、list。"""
        assert set(fields) <= COLUMNS, f"不能改：{set(fields) - COLUMNS}"
        values = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, dict | list) else v for k, v in fields.items()}
        values["updated"] = time.time()
        with self.db:
            self.db.execute(f"update jobs set {', '.join(f'{k} = ?' for k in values)} where id = ?", (*values.values(), job_id))

    def unfinished(self) -> list[str]:
        return [row["id"] for row in self.db.execute("select id from jobs where status = 'running' order by created")]

    def answer(self, key: str) -> str | None:
        row = self.db.execute("select answer from answers where key = ?", (key,)).fetchone()
        return row["answer"] if row else None

    def keep_answer(self, key: str, answer: str) -> None:
        with self.db:
            self.db.execute("insert or replace into answers (key, answer, created) values (?, ?, ?)", (key, answer, time.time()))

    def close(self) -> None:
        self.db.close()
