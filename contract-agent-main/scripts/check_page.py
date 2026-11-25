"""页面验收：用无头 Chrome 走一遍上传 → 进度 → 结果 → 点出处 → 追问，再在手机宽度下重新打开同一个任务。

先启动服务（uv run contract-agent），再运行（会真的调模型；字体环境和 make_samples.py 一样）：

    uv run python scripts/check_page.py samples/trap-02.pdf 押金多久退

截图存到 evals/results/page/（不进仓库），检查结果打印出来。
Chrome 用开发者协议（CDP）控制：发 JSON 命令，比如"设置文件框里的文件""执行一段 JS""截图"。
"""

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
CHROME = os.environ.get("CHROME", str(Path.home() / ".cache/puppeteer/chrome/linux-131.0.6778.204/chrome-linux64/chrome"))
BASE = "http://127.0.0.1:8300"
PORT = 9333
OUT = ROOT / "evals" / "results" / "page"


class Page:
    def __init__(self, ws):
        self.ws = ws
        self.next_id = 0

    async def call(self, method: str, **params) -> dict:
        self.next_id += 1
        await self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        while True:  # 中间夹着的事件消息不用管，等到自己这条的回复
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self.next_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}：{msg['error']}")
                return msg.get("result", {})

    async def js(self, expression: str):
        result = await self.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result["result"].get("value")

    async def wait(self, expression: str, timeout: float = 240) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if await self.js(expression):
                return
            await asyncio.sleep(0.3)
        raise TimeoutError(expression)

    async def size(self, width: int, height: int, mobile: bool = False) -> None:
        await self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=mobile)

    async def shot(self, name: str, width: int, mobile: bool = False) -> Path:
        """整页截图：先把窗口拉到和页面一样高。"""
        height = await self.js("document.documentElement.scrollHeight")
        await self.size(width, height, mobile)
        data = (await self.call("Page.captureScreenshot", format="png"))["data"]
        await self.size(width, 900 if not mobile else 844, mobile)
        path = OUT / f"{name}.png"
        path.write_bytes(base64.b64decode(data))
        return path

    async def set_file(self, selector: str, path: Path) -> None:
        root = (await self.call("DOM.getDocument"))["root"]["nodeId"]
        node = (await self.call("DOM.querySelector", nodeId=root, selector=selector))["nodeId"]
        await self.call("DOM.setFileInputFiles", nodeId=node, files=[str(path.resolve())])


async def run(pdf: Path, question: str) -> list[str]:
    checks: list[str] = []
    page_ws = None
    for _ in range(50):  # 等 Chrome 起来
        try:
            page_ws = next(t["webSocketDebuggerUrl"] for t in httpx.get(f"http://127.0.0.1:{PORT}/json").json() if t["type"] == "page")
            break
        except (httpx.HTTPError, StopIteration):
            await asyncio.sleep(0.2)
    async with websockets.connect(page_ws, max_size=None) as ws:
        page = Page(ws)
        await page.size(1280, 900)
        await page.call("Page.navigate", url=f"{BASE}/")
        await page.wait("document.readyState === 'complete' && !!document.querySelector('#file')", 30)

        # 不是 PDF：要有明确的错误
        bad = Path(tempfile.mkdtemp()) / "合同.txt"
        bad.write_text("第一条 租金", encoding="utf-8")
        await page.set_file("#file", bad)
        await page.wait("!document.querySelector('#upload-error').hidden", 15)
        checks.append(f"传 txt：{await page.js('document.querySelector(\"#upload-error\").textContent')}")

        started = time.monotonic()
        await page.set_file("#file", pdf)
        await page.wait("document.body.dataset.state !== 'idle'", 30)
        checks.append(f"上传后 {time.monotonic() - started:.1f} s 看到任务：{await page.js('document.querySelector(\"#meta\").textContent')}")
        await asyncio.sleep(1.5)
        await page.shot("1-进度", 1280)

        # 关键信息、风险各自出来就显示；记下先后（和"做完"一起出来的排在最后）
        order: list[str] = []

        async def seen() -> None:
            for name, sel in (("关键信息", "#info-section"), ("风险", "#risks-section")):
                if name not in order and await page.js(f"!document.querySelector('{sel}').hidden"):
                    order.append(name)

        while (state := await page.js("document.body.dataset.state")) == "running":
            await seen()
            await asyncio.sleep(0.3)
            if time.monotonic() - started > 240:
                raise TimeoutError("等结果超时")
        await seen()
        checks.append(f"结果：{state}，{time.monotonic() - started:.0f} s；出来的先后：{' → '.join(order)}")
        risks = await page.js("[...document.querySelectorAll('.risk .rid')].map(e => e.textContent).join('、')")
        missing = await page.js("[...document.querySelectorAll('#missing .what')].map(e => e.textContent).join('；')")
        checks.append(f"风险：{risks}；没写：{missing or '无'}")

        await page.js("document.querySelector('.risk .src').click(); true")
        opened = await page.js("!!document.querySelector('.risk .clause-full')")
        checks.append(f"点出处展开整条原文：{'是' if opened else '否'}")
        await page.shot("2-结果-电脑", 1280)

        await page.js(f"document.querySelector('#question').value = {json.dumps(question)}; document.querySelector('#ask-form').requestSubmit(); true")
        await page.wait("document.body.dataset.answer === 'done'", 120)
        answer = await page.js("document.querySelector('#answer .answer').textContent")
        note = await page.js("document.querySelector('#answer .answer-note').textContent")
        checks.append(f"追问「{question}」：{answer[:80]}……（{note}）")
        await page.js("document.querySelector('#answer .src')?.click(); true")
        await page.shot("3-追问", 1280)
        await page.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark"}])
        await page.shot("5-深色", 1280)
        await page.call("Emulation.setEmulatedMedia", features=[])

        # 手机宽度，重新打开同一个网址：刷新、关掉再开都应该恢复
        url = await page.js("location.href")
        await page.size(390, 844, mobile=True)
        await page.call("Page.navigate", url=url)
        await page.wait("document.body.dataset.state === 'done'", 30)
        wide = await page.js("document.documentElement.scrollWidth > window.innerWidth")
        checks.append(f"手机宽度重新打开：恢复了；横向滚动：{'有' if wide else '没有'}")
        await page.shot("4-手机", 390, mobile=True)
    return checks


def main() -> None:
    pdf, question = Path(sys.argv[1]), " ".join(sys.argv[2:]) or "押金多久退"
    OUT.mkdir(parents=True, exist_ok=True)
    profile = tempfile.mkdtemp()
    chrome = subprocess.Popen(
        [CHROME, "--headless", "--no-sandbox", "--disable-gpu", f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for line in asyncio.run(run(pdf, question)):
            print("-", line)
        print(f"截图：{OUT.relative_to(ROOT)}/")
    finally:
        chrome.terminate()


if __name__ == "__main__":
    main()
