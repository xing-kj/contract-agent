"""条款检索：本地中文向量模型 + Qdrant 本地模式，合同文字不出这台机器。

- 一条条款是一个检索单位（一个向量），检索结果就是条款 id，回答时可以直接引用。
- 向量模型 BAAI/bge-small-zh-v1.5（约 90 MB，512 维，CPU 就能跑），第一次用时自动下载到 data/models。
- 向量存在 data/qdrant，按合同内容的哈希分集合：同一份合同第二次问，不用再算向量。
- 另有关键词检索（两字片段重合度）做对照，评测时比较三种：关键词、向量、两者融合。

    uv run python -m contract_agent.index samples/zufang-03.pdf 押金多久退
"""

import atexit
import hashlib
import math
import re
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path

from .clauses import Clause, split_clauses
from .config import ROOT
from .textdoc import read_document

MODEL = "BAAI/bge-small-zh-v1.5"
# bge 中文模型建议：短问题检索长段落时，问题前加这句说明
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："
DATA = ROOT / "data"


@lru_cache
def embedder():
    from fastembed import TextEmbedding  # 导入要一两秒，用到时再导

    try:  # 已经下载过就不联网检查（否则每次启动多等好几秒）
        return TextEmbedding(MODEL, cache_dir=str(DATA / "models"), local_files_only=True)
    except Exception:
        return TextEmbedding(MODEL, cache_dir=str(DATA / "models"))


@lru_cache
def client():
    from qdrant_client import QdrantClient

    db = QdrantClient(path=str(DATA / "qdrant"))  # 本地模式：存在文件里，不用另起服务
    atexit.register(db.close)  # 程序退出前先关掉，不然解释器关闭时它会报错
    return db


def clause_doc(c: Clause) -> str:
    """参与检索的文字：章、标题、正文。"""
    return "\n".join(x for x in (c.chapter, c.heading or c.title, c.text) if x)


def contract_key(clauses: list[Clause]) -> str:
    return "c_" + hashlib.sha256("\n\n".join(clause_doc(c) for c in clauses).encode()).hexdigest()[:16]


def build(clauses: list[Clause]) -> str:
    """建好这份合同的集合，返回集合名。已经建过的直接用。"""
    from qdrant_client import models

    name = contract_key(clauses)
    db = client()
    if db.collection_exists(name) and db.count(name).count == len(clauses):
        return name
    if db.collection_exists(name):
        db.delete_collection(name)
    vectors = list(embedder().embed([clause_doc(c) for c in clauses]))
    db.create_collection(name, vectors_config=models.VectorParams(size=len(vectors[0]), distance=models.Distance.COSINE))
    db.upsert(name, [models.PointStruct(id=i, vector=v.tolist(), payload={"clause": c.id}) for i, (c, v) in enumerate(zip(clauses, vectors))])
    return name


def vector_rank(clauses: list[Clause], query: str, k: int = 10) -> list[str]:
    name = build(clauses)
    vector = next(iter(embedder().embed([QUERY_PREFIX + query]))).tolist()
    return [p.payload["clause"] for p in client().query_points(name, query=vector, limit=k).points]


def _grams(text: str) -> list[str]:
    t = re.sub(r"[^\w]", "", text)
    return [t[i:i + 2] for i in range(len(t) - 1)]


def keyword_rank(clauses: list[Clause], query: str, k: int = 10) -> list[str]:
    """两字片段重合度：越少条款包含的片段越有区分度（和心懂的关键词检索同一个思路）。"""
    docs = {c.id: set(_grams(clause_doc(c))) for c in clauses}
    df = Counter(g for grams in docs.values() for g in grams)
    idf = {g: math.log(1 + len(docs) / n) for g, n in df.items()}
    query_grams = set(_grams(query))
    scores = {cid: sum(idf[g] for g in query_grams & grams) for cid, grams in docs.items()}
    return [cid for cid, s in sorted(scores.items(), key=lambda x: -x[1]) if s > 0][:k]


def fuse(ranks: list[list[str]], k: int = 10) -> list[str]:
    """倒数排名融合：不用调两种分数之间的比例。"""
    score: Counter = Counter()
    for rank in ranks:
        for i, cid in enumerate(rank):
            score[cid] += 1 / (60 + i)
    return [cid for cid, _ in score.most_common(k)]


def search(clauses: list[Clause], query: str, k: int = 6) -> list[str]:
    """线上用的检索：向量排名。评测里比较过关键词和融合，见 README。"""
    return vector_rank(clauses, query, k)


if __name__ == "__main__":
    clauses = split_clauses(read_document(Path(sys.argv[1]).read_bytes()))
    by_id = {c.id: c for c in clauses}
    query = " ".join(sys.argv[2:])
    for name, rank in (("向量", vector_rank(clauses, query, 5)), ("关键词", keyword_rank(clauses, query, 5))):
        print(name)
        for cid in rank:
            print(f"  [{cid}] {by_id[cid].heading or by_id[cid].title}")
