"""读 PDF：按页读出每一行文字，表格单独整理成 "| 单元格 | 单元格 |" 的行，按位置插回原处。

只处理能复制出文字的 PDF。扫描件、照片读不出文字，第一版不支持，读出来是空的就明确报错。
"""

import io
import logging
from dataclasses import dataclass

import pdfplumber

# pdfminer（pdfplumber 底层）遇到 Chrome、Word 导出的部分字体会刷一堆 FontBBox 警告，不影响读文字
logging.getLogger("pdfminer").setLevel(logging.ERROR)


@dataclass
class Line:
    text: str
    # 这一行写到了右边缘：排版把句子折断时，上一行一定是写满的；没写满就结束的行，是一段话真的结束了
    wraps: bool = False


@dataclass
class Page:
    number: int  # 从 1 开始
    lines: list[Line]


class NoTextError(ValueError):
    """PDF 里读不出文字：多半是扫描件或照片。"""


def _table_lines(rows: list[list[str | None]]) -> list[str]:
    """表格的每一行写成 "| a | b | c |"；空单元格写成空，单元格里的换行去掉。"""
    lines = []
    for row in rows:
        if any(row):
            lines.append("| " + " | ".join((cell or "").replace("\n", "") for cell in row) + " |")
    return lines


def _page_lines(page) -> list[Line]:
    tables = page.find_tables()
    boxes = [t.bbox for t in tables]

    def outside_tables(obj) -> bool:
        x0, top, x1, bottom = obj["x0"], obj["top"], obj["x1"], obj["bottom"]
        return not any(bx0 - 1 <= x0 and x1 <= bx1 + 1 and btop - 1 <= top and bottom <= bbottom + 1 for bx0, btop, bx1, bbottom in boxes)

    # 先把表格以外的文字按行读出来，再把表格按它在页面上的高度插回去
    text_page = page.filter(outside_tables) if boxes else page
    text_lines = [line for line in text_page.extract_text_lines() if line["text"].strip()]
    # 右边缘取这一页最长那行的右端；离它不到两个字宽，就算写满了
    right = max((line["x1"] for line in text_lines), default=0)
    items = [(line["top"], [Line(line["text"].strip(), right - line["x1"] < 2 * _char_width(line))]) for line in text_lines]
    items += [(t.bbox[1], [Line(text) for text in _table_lines(t.extract())]) for t in tables]
    items.sort(key=lambda item: item[0])
    return [line for _, lines in items for line in lines]


def _char_width(line: dict) -> float:
    """一行里一个字大约多宽：用行高近似（中文字基本是方的）。"""
    return max(line["bottom"] - line["top"], 1.0)


def read_pages(data: bytes) -> list[Page]:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [Page(i, _page_lines(p)) for i, p in enumerate(pdf.pages, 1)]
    if not any(p.lines for p in pages):
        raise NoTextError("这份 PDF 里读不出文字，可能是扫描件或照片。目前只支持能复制文字的 PDF。")
    return pages
