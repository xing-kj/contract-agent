"""关键词检索和排名融合（不加载向量模型）。"""

from pathlib import Path

from contract_agent.clauses import split_clauses
from contract_agent.index import clause_doc, contract_key, fuse, keyword_rank
from contract_agent.pdf import read_pages

CLAUSES = split_clauses(read_pages((Path(__file__).resolve().parents[1] / "samples" / "zufang-03.pdf").read_bytes()))


def test_keyword_rank_finds_shared_words():
    assert keyword_rank(CLAUSES, "可以养宠物吗")[0] == "c46"


def test_keyword_rank_misses_paraphrase():
    # 换个说法就对不上：这正是要用向量的原因
    assert "c46" not in keyword_rank(CLAUSES, "我想养只猫行不行", 6)


def test_fuse_rewards_agreement():
    assert fuse([["a", "b", "c"], ["b", "a", "d"]])[:2] in (["a", "b"], ["b", "a"])
    assert fuse([["a", "b"], ["b", "c"]])[0] == "b"


def test_chapter_is_part_of_indexed_text():
    assert clause_doc(next(c for c in CLAUSES if c.id == "c46")).startswith("第六章 使用规范\n第四十六条 宠物")


def test_contract_key_depends_on_content():
    assert contract_key(CLAUSES) == contract_key(list(CLAUSES))
    assert contract_key(CLAUSES) != contract_key(CLAUSES[:-1])
