"""Retriever Protocol + BM25 + applies_to 硬过滤(不做 Embedding,冻结)。

- DEPRECATED 文档在建索引时整体排除:永不出现在可引用结果(test_tools 钉死)。
- applies_to 硬过滤先于打分:命中标签交集为空的文档直接不参与排名。
- 排序键 (-score, kb_id) 保证 digest 确定性。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel
from rank_bm25 import BM25Okapi

from helpdesk.config import DATA_DIR

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class KBHit(BaseModel):
    kb_id: str
    title: str
    status: str  # VERIFIED | DRAFT(DEPRECATED 已在索引层排除)
    applies_to: list[str]
    snippet: str
    score: float


class Retriever(Protocol):
    def search(
        self, query: str, applies_to: list[str] | None = None, k: int = 3
    ) -> list[KBHit]: ...


class BM25Retriever:
    def __init__(self, kb_dir: Path) -> None:
        self._docs: list[dict] = []
        for path in sorted(kb_dir.glob("*.md")):
            _, meta_raw, body = path.read_text(encoding="utf-8").split("---", 2)
            meta = yaml.safe_load(meta_raw)
            if meta["status"] == "DEPRECATED":
                continue  # 不出现在可引用结果
            self._docs.append(
                {
                    "kb_id": meta["kb_id"],
                    "title": meta["title"],
                    "status": meta["status"],
                    "applies_to": [str(t) for t in meta.get("applies_to", [])],
                    "snippet": _snippet(body),
                    "tokens": _tokens(f"{meta['title']} {body}"),
                }
            )
        self._bm25 = BM25Okapi([d["tokens"] for d in self._docs])

    def search(
        self, query: str, applies_to: list[str] | None = None, k: int = 3
    ) -> list[KBHit]:
        allowed = set(range(len(self._docs)))
        if applies_to:
            tags = {t.lower() for t in applies_to}
            allowed = {
                i
                for i in allowed
                if tags & {t.lower() for t in self._docs[i]["applies_to"]}
            }
        scores = self._bm25.get_scores(_tokens(query))
        ranked = sorted(
            (i for i in allowed if scores[i] > 0),
            key=lambda i: (-scores[i], self._docs[i]["kb_id"]),
        )[:k]
        return [
            KBHit(**{key: self._docs[i][key] for key in
                     ("kb_id", "title", "status", "applies_to", "snippet")},
                  score=round(float(scores[i]), 4))
            for i in ranked
        ]


def _snippet(body: str, limit: int = 200) -> str:
    lines = [
        ln.strip()
        for ln in body.splitlines()
        if ln.strip() and not ln.lstrip().startswith(("#", ">", "---"))
    ]
    return " ".join(lines)[:limit]


@lru_cache(maxsize=1)
def get_retriever() -> BM25Retriever:
    return BM25Retriever(DATA_DIR / "kb")
