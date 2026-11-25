"""Markdown / 纯文本直读：清洗规则和切条款兼容性。"""

from contract_agent.clauses import split_clauses
from contract_agent.textdoc import read_text_pages

MD = """# 租房合同

**合同编号：** CSZL-2019-0288

## 第一条　房屋基本情况

房屋坐落**长沙市**岳麓区，用途为住宅。

## 第二条　租金及押金

| 项目 | 金额 |
| --- | --- |
| 月租金 | 1600 元 |

押金于**2019**年2月28日前支付。

## 第三条　转租

甲方【× 同意】【√ 不同意】乙方转租房屋。

- 押金于验收后 7 日内退还

## 第四条　其他约定

无。

**甲方（签字或盖章）：** 张伟

**乙方（签字或盖章）：** 李娜
"""


def _clauses(md: str):
    return split_clauses(read_text_pages(md.encode("utf-8")))


def test_single_page():
    pages = read_text_pages(MD.encode("utf-8"))
    assert len(pages) == 1 and pages[0].number == 1
    assert all(not line.wraps for line in pages[0].lines)


def test_markdown_noise_stripped():
    pages = read_text_pages(MD.encode("utf-8"))
    text = "\n".join(line.text for line in pages[0].lines)
    assert "## " not in text and "**" not in text
    assert "---" not in text and "| --- |" not in text
    # 表格内容行保留
    assert "| 项目 | 金额 |" in text
    # 加粗里的内容保留
    assert "2019年2月28日前支付" in text


def test_clauses_split_on_markdown():
    clauses = {c.id: c for c in _clauses(MD)}
    assert set(clauses) == {"preamble", "c1", "c2", "c3", "c4", "sign"}
    assert clauses["c1"].title == "房屋基本情况"
    assert clauses["c1"].pages == [1]
    assert "岳麓区" in clauses["c1"].text
    assert "1600 元" in clauses["c2"].text
    assert "不同意" in clauses["c3"].text


def test_gbk_text():
    pages = read_text_pages("第一条 房屋坐落北京。".encode("gb18030"))
    assert pages[0].lines[0].text == "第一条 房屋坐落北京。"


def test_empty_raises():
    import pytest

    with pytest.raises(ValueError):
        read_text_pages("\n---\n\n".encode("utf-8"))
