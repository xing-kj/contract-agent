"""风险清单：读 knowledge/风险清单.md（人工审核过的常见的坑）。

清单很小（二三十条），整份放进提示词让模型比对，不做检索：不会漏查，提示词的固定部分还能命中缓存。
读的时候从严：格式不对、编号重复、对应的字段不存在、有"待核"的条目，都直接报错，不带着没审过的条目上线。

    uv run python -m contract_agent.checklist
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, computed_field

from .config import ROOT
from .extract import ContractInfo

CHECKLIST = ROOT / "knowledge" / "风险清单.md"

Status = Literal["通过", "部分", "待补"]


class Risk(BaseModel):
    id: str  # R01
    title: str
    fields: list[str]  # 对应第 3 步抽取的哪几项
    absent: list[str]  # 其中"没写也提示"的项：抽取结果为空时，由代码判定"合同里没写"
    patterns: str  # 典型写法
    basis: str = Field(exclude=True)  # 依据原文；给用户看的是 basis_shown
    status: Status
    note: str = Field(exclude=True)  # 核对时的说明，只在清单里，不返回给用户
    why: str  # 为什么要留意
    ask: str  # 可以问
    not_risk: str  # 不要误报

    @computed_field
    @property
    def basis_shown(self) -> str:
        """给用户看的依据：待补的不给；部分的注明仅供参考。"""
        if self.status == "待补":
            return ""
        return self.basis + ("（仅供参考：依据只覆盖一部分，各地规定可能不同）" if self.status == "部分" else "")


class ChecklistError(ValueError):
    pass


HEADING = re.compile(r"^## (R\d{2}) (.+)$", re.M)
REVIEW = re.compile(r"^\d{4}-\d{2}-\d{2} (通过|部分|待补|待核)(?:（(.*)）)?$")


def parse(text: str) -> list[Risk]:
    risks: list[Risk] = []
    matches = list(HEADING.finditer(text))
    for i, m in enumerate(matches):
        rid, title = m.group(1), m.group(2).strip()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        # 条目到下一个 "## " 为止（分类标题也是 "## "）
        body = text[m.end():end].split("\n## ")[0]
        meta: dict[str, str] = {}
        prose: list[str] = []
        for line in body.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("- ") and "：" in line:
                key, value = line[2:].split("：", 1)
                meta[key.strip()] = value.strip()
            else:
                prose.append(line)

        def need(key: str) -> str:
            if not meta.get(key):
                raise ChecklistError(f"{rid} 缺少「{key}」")
            return meta[key]

        review = REVIEW.match(need("核对"))
        if not review:
            raise ChecklistError(f"{rid} 的核对要写成「日期 通过/部分/待补（说明）」：{meta['核对']}")
        if review.group(1) == "待核":
            raise ChecklistError(f"{rid} 还没审核（待核），不能使用")

        fields: list[str] = []
        absent: list[str] = []
        if need("对应关键信息") != "无":
            for token in re.split(r"[、,，]", meta["对应关键信息"]):
                name = token.replace("（没写也提示）", "").strip()
                if name not in ContractInfo.model_fields:
                    raise ChecklistError(f"{rid} 对应的关键信息「{name}」不存在")
                fields.append(name)
                if "没写也提示" in token:
                    absent.append(name)

        why = " ".join(p for p in prose if not p.startswith(("可以问：", "不要误报：")))
        ask = next((p.removeprefix("可以问：") for p in prose if p.startswith("可以问：")), "")
        not_risk = next((p.removeprefix("不要误报：") for p in prose if p.startswith("不要误报：")), "")
        if not (why and ask and not_risk):
            raise ChecklistError(f"{rid} 正文要有：为什么要留意、可以问、不要误报")

        risks.append(Risk(
            id=rid, title=title, fields=fields, absent=absent, patterns=need("典型写法"), basis=need("依据"),
            status=review.group(1), note=review.group(2) or "", why=why, ask=ask, not_risk=not_risk,
        ))

    ids = [r.id for r in risks]
    if not risks:
        raise ChecklistError("清单里一条也没有")
    if len(set(ids)) != len(ids):
        raise ChecklistError(f"编号重复：{sorted({i for i in ids if ids.count(i) > 1})}")
    return risks


@lru_cache
def load(path: Path = CHECKLIST) -> tuple[Risk, ...]:
    return tuple(parse(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    risks = load()
    print(f"共 {len(risks)} 条：" + "，".join(f"{s} {sum(r.status == s for r in risks)}" for s in ("通过", "部分", "待补")))
    for r in risks:
        if r.absent:
            print(f"{r.id} {r.title}：{'、'.join(r.absent)} 没写时也提示")
