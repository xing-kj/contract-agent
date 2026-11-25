"""按条款切分合同。

1. 去掉页眉页脚：只看每页最上、最下一行，在大多数页上都出现的（数字换成 # 再比）就是页眉页脚；
   "第 X 页 共 Y 页"这类页码行也去掉。
2. 找条款标题："第一条 …""第 3 条 …"；整份合同一条都没有时，改认"一、…"这种写法。
   编号必须一条接一条（1、2、3…），正文里碰巧以"第三条约定的……"开头的一行不会被当成标题。
3. 第一条之前是开头（合同名、双方），签字栏单独一块。
   "第二章 租金和押金"这样的章标题不单独成块，记在它下面每一条的 chapter 上；
   签字栏之后"附件一 家具家电清单"这样的附件各自成块（annex1、annex2……）。
4. 每块记下来自哪几页：一条被分页截断时 pages 是 [2, 3]。
5. 排版把一句话折成几行，接回去；"1.""（一）"开头的分项和表格行保留换行。

    uv run python -m contract_agent.clauses samples/zufang-01.pdf
"""

import re
import sys
from collections import Counter
from pathlib import Path

from pydantic import BaseModel

from .pdf import Line, Page, read_pages
from .textdoc import read_document


class Clause(BaseModel):
    id: str  # preamble、c1、c2……、sign、annex1……
    number: int | None  # 第几条；开头和签字栏没有
    heading: str  # 原文的标题行，如"第三条 租金及支付方式"
    title: str  # 标题里的名称，如"租金及支付方式"；标题行直接接正文时为空
    text: str  # 正文（不含标题行），折行已接回
    pages: list[int]  # 出现在哪几页
    chapter: str = ""  # 所在的章，如"第二章 租金和押金"；合同不分章时为空


CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_number(s: str) -> int | None:
    """"十二" → 12，"二十三" → 23，"一百零五" → 105，"3" → 3；认不出返回 None。"""
    if s.isdigit():
        return int(s)
    total, digit = 0, 0
    for ch in s:
        if ch in CN_DIGITS:
            digit = CN_DIGITS[ch]
        elif ch in "十百":
            total += (digit or 1) * (10 if ch == "十" else 100)
            digit = 0
        else:
            return None
    return total + digit


NUM = r"[一二三四五六七八九十百零〇两\d]+"
ARTICLE = re.compile(rf"^第\s*({NUM})\s*条\s*(.*)$")  # 第三条 租金及支付方式
SECTION = re.compile(r"^([一二三四五六七八九十]+)、\s*(.*)$")  # 三、租金和押金
PAGE_NO = re.compile(r"^(第\d+页([,，/]?共\d+页)?|\d+/\d+|-\d+-|page\d+(of\d+)?)$", re.I)  # 去掉空格后再比
CHAPTER = re.compile(rf"^第\s*({NUM})\s*章\s*(.*)$")  # 第二章 租金和押金
ANNEX = re.compile(r"^附件\s*([一二三四五六七八九十\d]*)\s*[：:]?\s*(.*)$")  # 附件一 家具家电清单（只在签字栏之后认）
SIGN = re.compile(r"^(甲方|乙方|出租方|承租方|出租人|承租人)\s*([（(](签字|签章|盖章|签字或盖章|签字盖章|签章或盖章)[）)])?\s*[：:]")
ITEM = re.compile(r"^(\d+[.．、]|[（(][一二三四五六七八九十\d]+[）)]|[一二三四五六七八九十]+、|\|)")
SENTENCE_END = tuple("。；：！？!?;:|")


def _strip_headers_footers(pages: list[Page]) -> list[tuple[int, Line]]:
    """去掉页眉页脚，返回 (页码, 行)。"""

    def norm(text: str) -> str:
        return re.sub(r"\d+", "#", text.replace(" ", ""))

    edges = Counter(norm(text) for p in pages if p.lines for text in {p.lines[0].text, p.lines[-1].text})
    repeated = {k for k, n in edges.items() if len(pages) >= 2 and n >= max(2, 0.6 * len(pages))}

    def is_edge(line: Line) -> bool:
        return norm(line.text) in repeated or bool(PAGE_NO.match(line.text.replace(" ", "")))

    out: list[tuple[int, Line]] = []
    for p in pages:
        lines = list(p.lines)
        # 只动每页最上、最下一行：页眉和合同标题一样时（第 1 页常见），标题还在下一行
        if lines and is_edge(lines[0]):
            lines = lines[1:]
        if lines and is_edge(lines[-1]):
            lines = lines[:-1]
        out += [(p.number, line) for line in lines]
    return out


def _join_wrapped(lines: list[Line]) -> str:
    """把排版折断的句子接回去：上一行写到了右边缘、又没在句末标点处结束，
    这一行也不是分项、表格或签字栏的开头，才接上。"""
    out: list[str] = []
    wraps = False
    for line in lines:
        if out and wraps and not out[-1].endswith(SENTENCE_END) and not ITEM.match(line.text) and not SIGN.match(line.text):
            out[-1] += line.text
        else:
            out.append(line.text)
        wraps = line.wraps
    return "\n".join(out)


def _split_title(rest: str) -> tuple[str, str]:
    """标题行"第三条"后面的部分：短、不以句号结尾的是条款名称，否则是正文的开头。"""
    rest = rest.strip()
    if rest and len(rest) <= 20 and not rest.endswith(SENTENCE_END):
        return rest, ""
    return "", rest


def split_clauses(pages: list[Page]) -> list[Clause]:
    lines = _strip_headers_footers(pages)
    pattern = ARTICLE if any(ARTICLE.match(line.text) for _, line in lines) else SECTION

    blocks: list[dict] = [{"id": "preamble", "number": None, "heading": "", "title": "", "lines": [], "pages": []}]
    expected = 1
    chapter = ""
    annexes = 0
    for page, line in lines:
        if CHAPTER.match(line.text) and pattern is ARTICLE:
            chapter = re.sub(r"\s+", " ", line.text.strip())
            continue
        if ANNEX.match(line.text) and blocks[-1]["id"].startswith(("sign", "annex")):
            annexes += 1
            blocks.append({"id": f"annex{annexes}", "number": None, "heading": line.text, "title": ANNEX.match(line.text).group(2).strip(), "lines": [], "pages": [page], "chapter": ""})
            continue
        m = pattern.match(line.text)
        number = cn_number(m.group(1)) if m else None
        if number is not None and number == expected:
            title, first = _split_title(m.group(2))
            # 标题行直接接着正文时（"第三条 乙方应于……"），正文的第一段可能也被折了行
            body = [Line(first, line.wraps)] if first else []
            blocks.append({"id": f"c{number}", "number": number, "heading": line.text, "title": title, "lines": body, "pages": [page], "chapter": chapter})
            expected += 1
            continue
        if SIGN.match(line.text) and blocks[-1]["id"] != "sign" and expected > 1:
            blocks.append({"id": "sign", "number": None, "heading": "", "title": "签署", "lines": [], "pages": []})
        block = blocks[-1]
        block["lines"].append(line)
        if page not in block["pages"]:
            block["pages"].append(page)

    return [
        Clause(id=b["id"], number=b["number"], heading=b["heading"], title=b["title"], text=_join_wrapped(b["lines"]), pages=b["pages"], chapter=b.get("chapter", ""))
        for b in blocks
        if b["lines"] or b["heading"]
    ]


if __name__ == "__main__":
    for clause in split_clauses(read_document(Path(sys.argv[1]).read_bytes())):
        print(f"[{clause.id}] 第 {clause.pages} 页　{clause.chapter + '　' if clause.chapter else ''}{clause.heading or clause.title or '（开头）'}")
        for line in clause.text.splitlines():
            print("    " + line)
