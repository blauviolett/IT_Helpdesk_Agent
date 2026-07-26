"""prompt 外置加载:prompts/*.md 带 version frontmatter,变量用 $name 占位。

frontmatter 的 variables 列表是必填变量表(resolve.md 的三个必填变量段 D3 消费
同一机制);缺变量在渲染时立即 assert,不留到模型侧。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any

import yaml

PROMPTS_DIR = Path(__file__).parent / "prompts"


@lru_cache(maxsize=None)
def _load(name: str) -> tuple[dict[str, Any], str]:
    raw = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    _, meta_raw, body = raw.split("---", 2)
    return yaml.safe_load(meta_raw), body.strip()


def render_prompt(name: str, **variables: str) -> str:
    meta, body = _load(name)
    missing = set(meta.get("variables", [])) - variables.keys()
    assert not missing, f"prompt {name} missing variables: {sorted(missing)}"
    return Template(body).substitute(**variables)
