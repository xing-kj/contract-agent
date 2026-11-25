"""后台任务：做完一步存一步、同一份合同不再调模型、排队、重启接着跑、失败重试。用假的模型，不真的调用。"""

import asyncio
import json
from collections import Counter
from pathlib import Path

from fastapi.testclient import TestClient

from contract_agent import api, risks
from contract_agent.clauses import split_clauses
from contract_agent.jobs import Jobs, events_of
from contract_agent.pdf import read_pages
from contract_agent.store import Store

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
CLAUSES = split_clauses(read_pages((SAMPLES / "zufang-01.pdf").read_bytes()))
LATE = "乙方逾期支付租金的，每逾期一日，应按当期租金的 1% 向甲方支付滞纳金。"
ANSWERS = {
    "contract_info": {"rent": {"yuan": 3200, "quotes": [{"clause": "c3", "text": "月租金为人民币 3200 元"}]}},
    "risk_hits": {"hits": [{"risk": "R06", "how": "每天 1%", "quotes": [{"clause": "c3", "text": LATE}], "check": "没有宽限期，不是不算的情况", "keep": True}]},
}


class FakeModel:
    """按输出格式分辨是抽取（contract_info）还是比对（risk_hits），回固定的 JSON；记下每次调的是哪一步。"""

    def __init__(self, delays: dict[str, float] | None = None, fail: str = "", broken: str = ""):
        self.calls: list[str] = []
        self.delays = delays or {}
        self.fail = fail  # 这一步抛错，像接口返回 503
        self.broken = broken  # 这一步回的不是 JSON

    async def __call__(self, messages, *, response_format, **options) -> str:
        step = response_format["json_schema"]["name"]
        self.calls.append(step)
        await asyncio.sleep(self.delays.get(step, 0))
        if step == self.fail:
            raise RuntimeError("模型接口返回 503")
        return "不是 JSON" if step == self.broken else json.dumps(ANSWERS[step], ensure_ascii=False)


async def wait(jobs: Jobs) -> None:
    await asyncio.gather(*list(jobs.running.values()), return_exceptions=True)


def test_job_saves_each_step_and_ends_with_missing(tmp_path):
    model = FakeModel()

    async def go():
        jobs = Jobs(Store(tmp_path / "jobs.sqlite"), chat=model)
        job = jobs.submit("zufang-01.pdf", 3, CLAUSES)
        assert job.status == "running" and len(job.clauses) == 12
        await wait(jobs)
        return jobs.store.get(job.id)

    job = asyncio.run(go())
    assert job.status == "done" and Counter(model.calls) == {"contract_info": 1, "risk_hits": 1}
    assert job.info["info"]["rent"]["yuan"] == 3200
    assert [f["risk"]["id"] for f in job.risks["findings"]] == ["R06"]
    assert "note" not in job.risks["findings"][0]["risk"]  # 清单里核对时的说明不给用户
    assert [m["risk"]["id"] for m in job.missing] == ["R02", "R09", "R11", "R13"]
    assert [e["type"] for e in events_of(job)] == ["info", "risks", "done"]


def test_same_contract_again_uses_saved_answers(tmp_path):
    model = FakeModel()

    async def go():
        jobs = Jobs(Store(tmp_path / "jobs.sqlite"), chat=model)
        first = jobs.submit("a.pdf", 3, CLAUSES)
        await wait(jobs)
        second = jobs.submit("b.pdf", 3, CLAUSES)
        await wait(jobs)
        return jobs, jobs.store.get(first.id), jobs.store.get(second.id)

    jobs, first, second = asyncio.run(go())
    assert len(model.calls) == 2 and jobs.calls == 2  # 第二次一次都没调
    assert second.status == "done" and second.id != first.id and second.risks == first.risks


def test_changed_checklist_calls_match_again_but_reuses_extraction(tmp_path, monkeypatch):
    model = FakeModel()

    async def go():
        jobs = Jobs(Store(tmp_path / "jobs.sqlite"), chat=model)
        jobs.submit("a.pdf", 3, CLAUSES)
        await wait(jobs)
        # 清单改了一个字：比对的提示词变了，要重新调；抽取的请求没变，用存着的
        edited = tuple(r.model_copy(update={"title": r.title + "（改过）"}) if r.id == "R01" else r for r in risks.load())
        monkeypatch.setattr(risks, "load", lambda: edited)
        jobs.submit("a.pdf", 3, CLAUSES)
        await wait(jobs)

    asyncio.run(go())
    assert Counter(model.calls) == {"contract_info": 1, "risk_hits": 2}


def test_restart_redoes_only_the_unfinished_step(tmp_path):
    path = tmp_path / "jobs.sqlite"

    async def crash() -> str:
        jobs = Jobs(Store(path), chat=FakeModel(delays={"risk_hits": 10}))
        job = jobs.submit("a.pdf", 3, CLAUSES)
        while jobs.store.get(job.id).info is None:  # 抽取做完、比对还在等模型
            await asyncio.sleep(0.01)
        await jobs.stop()  # 像服务这时被关掉
        return job.id

    job_id = asyncio.run(crash())
    after = FakeModel()

    async def restart():
        jobs = Jobs(Store(path), chat=after)
        assert jobs.resume() == [job_id]
        await wait(jobs)
        return jobs.store.get(job_id)

    job = asyncio.run(restart())
    assert after.calls == ["risk_hits"] and job.status == "done"


def test_at_most_four_model_calls_at_once(tmp_path):
    model = FakeModel(delays={"contract_info": 0.05, "risk_hits": 0.05})

    async def go():
        jobs = Jobs(Store(tmp_path / "jobs.sqlite"), chat=model)
        ids = []
        for i in range(10):
            # 每份合同改一个字，请求就各不一样，不会用到存着的回答
            clauses = [c.model_copy(update={"text": c.text + str(i)}) if c.id == "c1" else c for c in CLAUSES]
            ids.append(jobs.submit(f"{i}.pdf", 3, clauses).id)
        await wait(jobs)
        return jobs, [jobs.store.get(i).status for i in ids]

    jobs, statuses = asyncio.run(go())
    assert statuses == ["done"] * 10 and len(model.calls) == 20 and jobs.peak == 4


def test_failed_step_is_retried_alone(tmp_path):
    model = FakeModel(fail="risk_hits")

    async def go():
        jobs = Jobs(Store(tmp_path / "jobs.sqlite"), chat=model)
        job = jobs.submit("a.pdf", 3, CLAUSES)
        await wait(jobs)
        failed = jobs.store.get(job.id)
        model.fail = ""
        jobs.retry(job.id)
        await wait(jobs)
        return failed, jobs.store.get(job.id)

    failed, job = asyncio.run(go())
    assert failed.status == "failed" and "503" in failed.error
    assert [e["type"] for e in events_of(failed)] == ["info", "failed"]  # 抽取做完了，照样给用户看
    assert job.status == "done" and job.error is None
    assert Counter(model.calls) == {"contract_info": 1, "risk_hits": 2}


def test_broken_answer_is_not_saved(tmp_path):
    model = FakeModel(broken="contract_info")

    async def go():
        jobs = Jobs(Store(tmp_path / "jobs.sqlite"), chat=model)
        for _ in range(2):
            jobs.submit("a.pdf", 3, CLAUSES)
            await wait(jobs)

    asyncio.run(go())
    assert Counter(model.calls) == {"contract_info": 2, "risk_hits": 1}


def test_job_api_upload_progress_result_and_ask(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "make_jobs", lambda: Jobs(Store(tmp_path / "jobs.sqlite"), chat=FakeModel()))

    async def fake_answer(question, clauses, mode):
        yield {"type": "delta", "text": f"{question}|{mode}|{len(clauses)}"}
        yield {"type": "done"}

    monkeypatch.setattr(api, "answer", fake_answer)
    pdf = (SAMPLES / "zufang-01.pdf").read_bytes()
    with TestClient(api.app) as client:  # with：启动和关闭服务时的那一段（读库、接着跑没做完的）也会执行
        r = client.post("/jobs", files={"file": ("zufang-01.pdf", pdf, "application/pdf")})
        assert r.status_code == 202
        job = r.json()
        assert job["status"] == "running" and len(job["clauses"]) == 12

        stream = client.get(f"/jobs/{job['id']}/events").text
        types = [json.loads(line[6:])["type"] for line in stream.splitlines() if line.startswith("data: ")]
        assert sorted(types[:2]) == ["info", "risks"] and types[2:] == ["done"]

        done = client.get(f"/jobs/{job['id']}").json()
        assert done["status"] == "done" and [m["risk"]["id"] for m in done["missing"]] == ["R02", "R09", "R11", "R13"]
        assert client.post(f"/jobs/{job['id']}/retry").status_code == 409  # 没失败，不用重试

        answer = client.post(f"/jobs/{job['id']}/ask", json={"question": "能养猫吗"})
        assert '"text": "能养猫吗|auto|12"' in answer.text  # 用存着的条款回答，没再传 PDF

        assert client.get("/jobs/nope").status_code == 404
        assert client.post("/jobs", files={"file": ("a.bin", b"\xff\xfe\x00\x00", "application/octet-stream")}).status_code == 415
