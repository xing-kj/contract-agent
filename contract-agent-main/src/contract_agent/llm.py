"""调用大模型。中转接口是 OpenAI 兼容的 /chat/completions。
直接用 httpx 发请求、自己解析流式数据，不用官方 SDK，方便看清楚每一步在做什么。"""

import json
from collections.abc import AsyncIterator, Awaitable, Callable

import httpx

from .config import settings

# 连上接口最多等 10 秒；之后每一段数据之间最多等 60 秒
TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# 非流式调用要等模型把整段回答（含推理）写完才返回：推理模型在长合同上可能要几分钟
CHAT_TIMEOUT = httpx.Timeout(600.0, connect=10.0)

# 和 chat 用法一样的函数。抽取、比对都可以传一个进来：默认就是 chat，直接调模型；
# 后台任务传的是先查存着的回答、再排队调模型的版本（见 jobs.py）
Chat = Callable[..., Awaitable[str]]


async def chat(messages: list[dict[str, str]], *, temperature: float = 0, response_format: dict | None = None) -> str:
    """一次拿到完整回答。抽取信息要拿到整段 JSON 才能检查，用不上流式。

    response_format 传 JSON Schema 时，接口会让模型严格按这个格式输出（OpenAI 的结构化输出）。
    """
    s = settings()
    payload: dict = {"model": s.llm_model, "messages": messages, "temperature": temperature}
    if response_format:
        payload["response_format"] = response_format
    headers = {"Authorization": f"Bearer {s.llm_api_key}"}
    async with httpx.AsyncClient(timeout=CHAT_TIMEOUT) as client:
        resp = await client.post(f"{s.llm_base_url}/chat/completions", json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"模型接口返回 {resp.status_code}：{resp.text[:200]}")
    return resp.json()["choices"][0]["message"]["content"] or ""


async def stream_chat(messages: list[dict[str, str]], *, temperature: float = 0.3, usage: dict | None = None) -> AsyncIterator[str]:
    """流式调用：模型每生成一小段文字，就 yield 一段。

    接口返回的是一行一行的 "data: {...}"，最后一行是 "data: [DONE]"。
    每一行里，新生成的文字在 choices[0].delta.content。
    传入 usage 字典时，顺便要回这次的 token 用量（在最后一段数据里），写进这个字典。
    """
    s = settings()
    payload: dict = {"model": s.llm_model, "messages": messages, "stream": True}
    # 部分推理模型（如 kimi-k3）不接受非 0 的 temperature，直接 400：
    # 非 0 值一律省略、用服务端默认；0 要传，抽取类调用靠它保证确定性
    if temperature == 0:
        payload["temperature"] = 0
    if usage is not None:
        payload["stream_options"] = {"include_usage": True}
    headers = {"Authorization": f"Bearer {s.llm_api_key}"}
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        async with client.stream("POST", f"{s.llm_base_url}/chat/completions", json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")[:200]
                raise RuntimeError(f"模型接口返回 {resp.status_code}：{body}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if usage is not None and chunk.get("usage"):
                    usage.update(chunk["usage"])
                choices = chunk.get("choices") or []
                text = (choices[0].get("delta") or {}).get("content") if choices else None
                if text:
                    yield text
