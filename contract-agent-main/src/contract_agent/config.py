"""配置：环境变量优先，没有就读项目根目录的 .env。密钥只放在 .env 里，.env 不进仓库。"""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseModel):
    llm_base_url: str  # OpenAI 兼容的接口地址，到 /v1 为止
    llm_api_key: str
    llm_model: str = "gpt-6-luna"


def _read_env_file(path: Path) -> dict[str, str]:
    """读 KEY=VALUE 格式的 .env：跳过空行和 # 注释，去掉值两边的引号。"""
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


@lru_cache
def settings() -> Settings:
    """只读一次，之后直接用缓存。"""
    file = _read_env_file(ROOT / ".env")

    def get(key: str) -> str:
        return os.environ.get(key) or file.get(key, "")

    return Settings(
        llm_base_url=get("LLM_BASE_URL").rstrip("/"),
        llm_api_key=get("LLM_API_KEY"),
        llm_model=get("LLM_MODEL") or "gpt-6-luna",
    )
