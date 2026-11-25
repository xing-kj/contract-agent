"""把 samples/src/*.md 里的虚构合同渲染成 PDF（samples/*.pdf），用来测试读 PDF 和切分条款。

用无头 Chrome 的"打印成 PDF"：生成的是能复制文字的 PDF，字体嵌在文件里，和 Word 导出的差不多。
每页加页眉（合同标题）和页脚（第 X 页 共 Y 页），用来检验切分时能不能把它们去掉。
只有改了样本才需要重新生成：

    CHROME=/path/to/chrome uv run python scripts/make_samples.py

源文件格式：'# ' 开头是标题，'## ' 开头是条款标题，'|' 开头的几行是表格（第一行是表头），其余每行一段。
"""

import html
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "samples" / "src"
OUT = ROOT / "samples"
CHROME = os.environ.get("CHROME", str(Path.home() / ".cache/puppeteer/chrome/linux-131.0.6778.204/chrome-linux64/chrome"))

CSS = """
@page {
  size: A4;
  margin: 24mm 22mm;
  @top-right { content: "{title}"; font-size: 9pt; color: #777; }
  @bottom-center { content: "第 " counter(page) " 页 共 " counter(pages) " 页"; font-size: 9pt; color: #777; }
}
body { font-family: "WenQuanYi Micro Hei", sans-serif; font-size: 12pt; line-height: 2; color: #111; }
h1 { text-align: center; font-size: 18pt; margin: 0 0 18pt; }
h2 { font-size: 12pt; margin: 12pt 0 2pt; }
p { margin: 0; }
table { border-collapse: collapse; margin: 6pt 0; }
th, td { border: 1px solid #999; padding: 2pt 8pt; font-size: 11pt; }
"""


def to_html(md: str) -> str:
    lines = md.splitlines()
    title = next((line[2:].strip() for line in lines if line.startswith("# ")), "合同")
    body: list[str] = []
    table: list[list[str]] = []

    def flush_table() -> None:
        if not table:
            return
        head, *rows = table

        def cells(tag: str, row: list[str]) -> str:
            return "".join(f"<{tag}>{html.escape(c)}</{tag}>" for c in row)

        rows_html = "".join(f"<tr>{cells('td', r)}</tr>" for r in rows)
        body.append(f"<table><tr>{cells('th', head)}</tr>{rows_html}</table>")
        table.clear()

    for line in lines:
        if line.startswith("|"):
            table.append([c.strip() for c in line.strip().strip("|").split("|")])
            continue
        flush_table()
        if line.startswith("# "):
            body.append(f"<h1>{html.escape(line[2:].strip())}</h1>")
        elif line.startswith("## "):
            body.append(f"<h2>{html.escape(line[3:].strip())}</h2>")
        elif line.strip():
            body.append(f"<p>{html.escape(line.strip())}</p>")
        else:
            body.append('<p style="height:8pt"></p>')
    flush_table()
    css = CSS.replace("{title}", title)
    return f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>{css}</style></head><body>{"".join(body)}</body></html>'


def render(src: Path) -> Path:
    out = OUT / f"{src.stem}.pdf"
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(to_html(src.read_text(encoding="utf-8")), encoding="utf-8")
        subprocess.run(
            [CHROME, "--headless", "--no-sandbox", "--disable-gpu", "--no-pdf-header-footer", f"--print-to-pdf={out}", page.as_uri()],
            check=True,
            capture_output=True,
        )
    return out


if __name__ == "__main__":
    for src in sorted(SRC.glob("*.md")):
        print("生成", render(src).relative_to(ROOT))
