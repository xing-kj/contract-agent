"""读 Markdown / 纯文本合同：整份当一页（第 1 页），出处统一是"第 1 页 · 第几条"。

Markdown 本身"一行是一行"，不需要 PDF 那套折行合并和页眉页脚处理（单页时
`clauses._strip_headers_footers` 也不会动任何行）。表格 `| a | b | c |` 与 PDF
表格清洗后的格式一致，切条款时按表格行保留换行。

只剥掉影响条款识别和阅读的 Markdown 标记：行首标题井号、加粗、行内代码、
列表符号、分隔线、表格对齐行。勾选框【√ 】、内容全保留。
"""

import re

from .pdf import Line, Page, read_pages

HEADING = re.compile(r"^#{1,6}\s*")
BOLD = re.compile(r"\*\*(.+?)\*\*")
CODE = re.compile(r"`([^`]*)`")
LIST_MARK = re.compile(r"^[-*+]\s+")
RULE = re.compile(r"^([-*=~_]\s*){3,}$")
TABLE_ALIGN = re.compile(r"^\|[\s:\-|]*\|?\s*$")


def _clean(raw: str) -> str | None:
    """清洗一行 Markdown；返回 None 表示这一行丢弃（空行、分隔线、对齐行）。"""
    line = raw.rstrip()
    stripped = line.strip()
    if not stripped:
        return None
    if RULE.match(stripped) or TABLE_ALIGN.match(stripped):
        return None
    line = HEADING.sub("", stripped)
    line = BOLD.sub(r"\1", line)
    line = CODE.sub(r"\1", line)
    line = LIST_MARK.sub("", line)
    line = line.strip()
    return line or None


def decode_text(data: bytes) -> str:
    """优先 UTF-8（容错 BOM），失败再试 GB 系列；都不行就交给上层报错。"""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("gb18030")


def read_text_pages(data: bytes) -> list[Page]:
    """把 Markdown / 纯文本合同读成一页 Line。"""
    lines = [Line(text) for text in (_clean(raw) for raw in decode_text(data).splitlines()) if text]
    if not lines:
        raise ValueError("文件里读不出文字内容")
    return [Page(1, lines)]


def read_document(data: bytes) -> list[Page]:
    """按内容分派：PDF 走原路，其余按文本读。命令行入口共用这一个。"""
    if data.startswith(b"%PDF"):
        return read_pages(data)
    return read_text_pages(data)
