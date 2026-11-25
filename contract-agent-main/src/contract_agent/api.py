"""HTTP 接口。

/              上传页面（web/index.html）
/fields        关键信息各项的名称，页面按这个顺序显示
/health        服务在不在
/ask           流式回答一个问题（确认"收到请求 → 调模型 → 边生成边推给前端"这条链路是通的）
/contracts/parse    上传合同 PDF，返回按条款切好的内容
/contracts/extract  再抽出关键信息（租金、押金、租期……），每项附原文，核对不上的扔掉并说明原因
/contracts/review   关键信息 + 风险条款（和人工审核过的清单比对，每条附原文和页码）+ 合同里没写、值得问的事
/contracts/ask      就这份合同追问，流式回答，说到合同的地方附出处并由代码核对

后台任务（页面用这一组；上面几个一直等到出结果，留着方便命令行试）：
/jobs                  上传合同，马上返回任务号和切好的条款；抽取和比对在后台跑
/jobs/{id}             任务现在的状态和已经出来的结果
/jobs/{id}/events      进度（SSE）：先补发已经做完的步骤，之后做完一步推一步
/jobs/{id}/retry       失败的任务重试，只重做失败的那一步
/jobs/{id}/ask         就这份合同追问，不用再传 PDF
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .clauses import Clause, split_clauses
from .config import ROOT
from .extract import LABELS, ContractInfo, Dropped, extract
from .jobs import Jobs, events_of
from .risks import RiskReport, review
from .llm import stream_chat
from .qa import answer
from .pdf import NoTextError, Page, read_pages
from .textdoc import read_text_pages
from .store import Job, Store

DB = ROOT / "data" / "jobs.sqlite"
PING_SECONDS = 15


def make_jobs() -> Jobs:
    return Jobs(Store(DB))


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.jobs = make_jobs()
    app.state.jobs.resume()  # 上次关掉或崩溃时没做完的任务，接着跑
    yield
    await app.state.jobs.stop()


app = FastAPI(title="租房合同助手", version="0.1.0", lifespan=lifespan)

# 导出的报告 HTML 双击打开时是 file:// 页面（origin 为 null），追问要连本服务，
# 浏览器按跨域处理：服务端放行所有来源即可（服务只监听本机，无 cookie 凭据）。
# allow_private_network：Chrome 的专用网络访问（PNA）要求 file:// → localhost 的
# 请求通过带 Access-Control-Request-Private-Network 的预检，服务端要回 true
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_private_network=True,
)

MAX_PDF_BYTES = 10 * 1024 * 1024


class AskRequest(BaseModel):
    # 请求体的格式由 Pydantic 校验：不是字符串、空字符串、太长，FastAPI 都会直接返回 422
    question: str = Field(min_length=1, max_length=2000)


def sse(event: dict) -> str:
    """拼一条 SSE 消息：'data: ' 后面跟一行 JSON，再空一行表示这条结束。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def page() -> FileResponse:
    """上传页面：一个 HTML 文件，样式和脚本都写在里面，不用前端构建工具。"""
    return FileResponse(Path(__file__).parent / "web" / "index.html")


@app.get("/fields")
async def fields() -> list[dict[str, str]]:
    """关键信息各项的名称，页面按这个顺序显示。"""
    return [{"name": name, "label": label} for name, label in LABELS.items()]


@app.post("/ask")
async def ask(req: AskRequest) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        messages = [
            {"role": "system", "content": "你是租房合同助手，用简短的中文回答。"},
            {"role": "user", "content": req.question},
        ]
        try:
            async for text in stream_chat(messages):
                yield sse({"type": "delta", "text": text})
            yield sse({"type": "done"})
        except Exception as e:  # 模型出错也要给前端一个明确的结尾，而不是连接突然断掉
            yield sse({"type": "error", "message": f"模型调用失败：{e}"})

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


class ParseResult(BaseModel):
    filename: str
    pages: int
    clauses: list[Clause]


class ExtractResult(ParseResult):
    info: ContractInfo
    dropped: list[Dropped]  # 模型给了、但没通过核对被扔掉的项，以及原因
    fixed: list[str]  # 出处写错了、按原文改正的项


async def read_upload(file: UploadFile) -> list[Page]:
    data = await file.read()
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, "文件超过 10 MB")
    try:
        if data.startswith(b"%PDF"):
            # 解析 PDF 是纯计算、要花一点时间：放到线程池里跑，不卡住同时进来的其他请求
            return await run_in_threadpool(read_pages, data)
        return await run_in_threadpool(read_text_pages, data)
    except UnicodeDecodeError as e:
        raise HTTPException(415, "只支持 PDF，或 UTF-8/GB 编码的 md、txt 文本") from e
    except NoTextError as e:
        raise HTTPException(422, str(e)) from e
    except ValueError as e:
        raise HTTPException(422, str(e) or "文件里读不出文字内容") from e
    except Exception as e:
        raise HTTPException(422, "这个文件打不开，可能已经损坏") from e


@app.post("/contracts/parse")
async def parse_contract(file: UploadFile) -> ParseResult:
    pages = await read_upload(file)
    return ParseResult(filename=file.filename or "", pages=len(pages), clauses=split_clauses(pages))


@app.post("/contracts/extract")
async def extract_contract(file: UploadFile) -> ExtractResult:
    pages = await read_upload(file)
    clauses = split_clauses(pages)
    try:
        result = await extract(clauses)
    except Exception as e:
        raise HTTPException(502, f"模型调用失败：{e}") from e
    return ExtractResult(filename=file.filename or "", pages=len(pages), clauses=clauses, info=result.info, dropped=result.dropped, fixed=result.fixed)


class ReviewResult(ExtractResult):
    risks: RiskReport


@app.post("/contracts/review")
async def review_contract(file: UploadFile) -> ReviewResult:
    pages = await read_upload(file)
    clauses = split_clauses(pages)
    try:
        extraction, risks = await review(clauses)
    except Exception as e:
        raise HTTPException(502, f"模型调用失败：{e}") from e
    return ReviewResult(
        filename=file.filename or "", pages=len(pages), clauses=clauses,
        info=extraction.info, dropped=extraction.dropped, fixed=extraction.fixed, risks=risks,
    )


@app.post("/contracts/ask")
async def ask_contract(
    file: UploadFile,
    question: str = Form(min_length=1, max_length=500),
    mode: Literal["auto", "full", "retrieve"] = Form("auto"),
) -> StreamingResponse:
    return answer_stream(question, split_clauses(await read_upload(file)), mode)


def answer_stream(question: str, clauses: list[Clause], mode: str) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        try:
            async for event in answer(question, clauses, mode):
                yield sse(event)
        except Exception as e:  # 和 /ask 一样：出错也给前端一个明确的结尾
            yield sse({"type": "error", "message": f"回答失败：{e}"})

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def job_or_404(jobs: Jobs, job_id: str) -> Job:
    job = jobs.store.get(job_id)
    if job is None:
        raise HTTPException(404, "没有这个任务")
    return job


@app.post("/jobs", status_code=202)
async def create_job(request: Request, file: UploadFile) -> Job:
    """读 PDF、切条款当场做（不到半秒，读不出来马上报错）；抽取和比对交给后台。202 表示"收下了，还没做完"。"""
    pages = await read_upload(file)
    return request.app.state.jobs.submit(file.filename or "", len(pages), split_clauses(pages))


@app.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: str) -> Job:
    return job_or_404(request.app.state.jobs, job_id)


@app.get("/jobs/{job_id}/events")
async def job_events(request: Request, job_id: str) -> StreamingResponse:
    jobs: Jobs = request.app.state.jobs
    job_or_404(jobs, job_id)

    async def events() -> AsyncIterator[str]:
        # 先订阅，再补发已经做完的步骤：补发时刚好做完的那一步不会漏掉，最多收到两次，按类型去掉重复
        queue = jobs.listen(job_id)
        try:
            sent: set[str] = set()
            pending = events_of(jobs.store.get(job_id))
            while True:
                for event in pending:
                    if event["type"] not in sent:
                        sent.add(event["type"])
                        yield sse(event)
                if sent & {"done", "failed"}:
                    return
                try:
                    pending = [await asyncio.wait_for(queue.get(), PING_SECONDS)]
                except TimeoutError:
                    pending = []
                    yield ": ping\n\n"  # 排队久了也隔一会儿发一行注释，免得连接被当成断了
        finally:
            jobs.unlisten(job_id, queue)

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/jobs/{job_id}/retry", status_code=202)
async def retry_job(request: Request, job_id: str) -> Job:
    jobs: Jobs = request.app.state.jobs
    if job_or_404(jobs, job_id).status != "failed":
        raise HTTPException(409, "只有失败的任务可以重试")
    jobs.retry(job_id)
    return jobs.store.get(job_id)


class JobQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    mode: Literal["auto", "full", "retrieve"] = "auto"


@app.post("/jobs/{job_id}/ask")
async def ask_job(request: Request, job_id: str, req: JobQuestion) -> StreamingResponse:
    """用任务里存着的条款回答，不用再传一次 PDF。"""
    return answer_stream(req.question, job_or_404(request.app.state.jobs, job_id).clauses, req.mode)
