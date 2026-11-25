"""读 PDF、切条款。样本是 samples/ 里两份虚构的合同：
zufang-01 用"第一条"做标题、有表格、3 页；zufang-02 用"一、"做标题、"（一）"分项、2 页。"""

import io
from pathlib import Path

import pypdfium2 as pdfium
import pytest

from contract_agent.clauses import cn_number, split_clauses
from contract_agent.pdf import Line, NoTextError, Page, read_pages

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def clauses_of(name: str) -> dict:
    return {c.id: c for c in split_clauses(read_pages((SAMPLES / name).read_bytes()))}


@pytest.mark.parametrize(("text", "number"), [("一", 1), ("十", 10), ("十二", 12), ("二十", 20), ("二十三", 23), ("一百零五", 105), ("7", 7), ("甲", None)])
def test_cn_number(text, number):
    assert cn_number(text) == number


def test_article_style_contract():
    c = clauses_of("zufang-01.pdf")
    assert [x.number for x in c.values() if x.number] == list(range(1, 11))
    assert c["c3"].title == "租金及支付方式"
    assert "月租金为人民币 3200 元" in c["c3"].text
    # 被分页截断的条款记下两页
    assert c["c10"].pages == [2, 3]


def test_headers_and_footers_removed():
    c = clauses_of("zufang-01.pdf")
    everything = "\n".join(x.text for x in c.values())
    assert "共 3 页" not in everything
    # 页眉和合同标题都是"房屋租赁合同"：页眉去掉了，标题还在
    assert everything.count("房屋租赁合同") == 1
    assert c["preamble"].text.splitlines()[0] == "房屋租赁合同"


def test_short_lines_stay_separate_and_wrapped_lines_join():
    c = clauses_of("zufang-01.pdf")
    # 开头几行都很短，是各自独立的一行，不能接成一句
    assert c["preamble"].text.splitlines()[1:3] == ["出租方（以下简称甲方）：周某某", "承租方（以下简称乙方）：林某某"]
    # 第八条两个分项都比一行长，排版折断的部分接回去后正好两行
    items = c["c8"].text.splitlines()
    assert len(items) == 2
    assert items[0].startswith("1. ") and items[0].endswith("违约金。")


def test_table_and_signature():
    c = clauses_of("zufang-01.pdf")
    assert "| 物业管理费 | 甲方 | — |" in c["c5"].text.splitlines()
    assert c["sign"].text.startswith("甲方（签字）")


def test_section_style_contract():
    c = clauses_of("zufang-02.pdf")
    assert [x.number for x in c.values() if x.number] == list(range(1, 8))
    assert c["c6"].pages == [1, 2]
    assert [line[:3] for line in c["c3"].text.splitlines()] == ["（一）", "（二）", "（三）"]
    assert c["sign"].text.splitlines() == ["甲方：__________ 乙方：__________", "日期：2026 年 12 月 1 日"]


def test_reference_to_another_article_is_not_a_heading():
    lines = ["第一条 租金", "月租金 2000 元。", "第三条约定的违约金另计。", "第二条 押金", "押金 2000 元。"]
    c = {x.id: x for x in split_clauses([Page(1, [Line(t) for t in lines])])}
    assert [x.heading for x in c.values() if x.number] == ["第一条 租金", "第二条 押金"]
    assert "第三条约定的违约金另计。" in c["c1"].text


def test_pdf_without_text():
    doc = pdfium.PdfDocument.new()
    doc.new_page(595, 842)
    buf = io.BytesIO()
    doc.save(buf)
    with pytest.raises(NoTextError):
        read_pages(buf.getvalue())


def test_chapters_and_annex():
    clauses = split_clauses(read_pages((SAMPLES / "zufang-03.pdf").read_bytes()))
    by_id = {c.id: c for c in clauses}
    assert [c.id for c in clauses if c.number] == [f"c{i}" for i in range(1, 85)]
    assert by_id["c1"].chapter == "第一章 总则" and by_id["c84"].chapter == "第十章 其他"
    # 章标题不混进上一条的正文
    assert "第二章" not in by_id["c8"].text
    assert [c.id for c in clauses][-2:] == ["sign", "annex1"]
    assert by_id["annex1"].title == "房间设施清单" and "| 热水器 | 1 | 完好 |" in by_id["annex1"].text
