"""运行配置(pydantic-settings)+ 冻结配置文件的加载入口。

- Settings:模型档位、路径(guide §2 文件职责表)。
- load_categories / load_limits / load_policy:三个配置文件的唯一加载点,
  进程内缓存 —— decide() 等纯函数消费的是已加载的常量,自身零 IO。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HELPDESK_", env_file=".env", extra="ignore"
    )

    model_main: str = "gpt-4o"
    model_small: str = "gpt-4o-mini"
    db_path: Path = REPO_ROOT / "helpdesk.db"
    trace_dir: Path = REPO_ROOT / "traces"

    # OpenAI SDK 只读进程环境变量;这两项让 .env 也生效(key 不进代码,guide §5 D3)。
    # 兼容 OpenAI 协议的国产端点(如百炼 DashScope)经 base_url 切换。
    openai_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_API_KEY")
    )
    openai_base_url: str | None = Field(
        default=None, validation_alias=AliasChoices("OPENAI_BASE_URL")
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def load_categories() -> dict[str, Any]:
    data = _load_yaml(CONFIG_DIR / "categories.yaml")
    _validate_categories(data)
    return data


@lru_cache(maxsize=1)
def load_limits() -> dict[str, Any]:
    return _load_yaml(CONFIG_DIR / "limits.yaml")


@lru_cache(maxsize=1)
def load_policy() -> dict[str, Any]:
    return _load_yaml(CONFIG_DIR / "policy.yaml")


def _validate_categories(data: dict[str, Any]) -> None:
    """item schema 校验(guide §2.1):source=TOOL 必有 tool,QUESTION 必有 question_hint。"""
    seen_ids: set[str] = set()
    for cat, spec in data["categories"].items():
        for item in spec["checklist"]:
            item_id = item["item_id"]
            assert item_id not in seen_ids, f"duplicate item_id: {item_id}"
            seen_ids.add(item_id)
            if item["source"] == "TOOL":
                assert item.get("tool"), f"{cat}/{item_id}: TOOL item requires tool"
            elif item["source"] == "QUESTION":
                assert item.get(
                    "question_hint"
                ), f"{cat}/{item_id}: QUESTION item requires question_hint"
            else:
                raise AssertionError(f"{cat}/{item_id}: bad source {item['source']}")
