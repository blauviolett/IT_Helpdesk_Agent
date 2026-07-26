"""KB adapter:search_kb(BM25 检索 + applies_to 硬过滤,见 retrieval.py)。

evidence.authority 取 top hit 的 frontmatter status(VERIFIED/DRAFT);
KB-1003 含注入指令的段落只会以 snippet 形式进上下文,Input Guard 于 D3 接线。
"""

from __future__ import annotations

from typing import Any

from helpdesk.state.models import Actor, Authority
from helpdesk.tools.base import ToolResult, ToolRuntime, ToolStatus
from helpdesk.tools.retrieval import get_retriever


def search_kb(actor: Actor, params: Any, runtime: ToolRuntime) -> ToolResult:
    hits = get_retriever().search(params.query, applies_to=params.applies_to or None)
    if not hits:
        return ToolResult(
            status=ToolStatus.EMPTY, digest=f"no KB match for query: {params.query}"
        )
    top = hits[0]
    parts = [f"{top.kb_id} [{top.status}] {top.title}: {top.snippet[:120]}"]
    parts += [f"{h.kb_id} [{h.status}] {h.title}" for h in hits[1:]]
    authority = (
        Authority(top.status) if top.status in ("VERIFIED", "DRAFT") else Authority.GENERIC
    )
    return ToolResult(
        status=ToolStatus.OK,
        digest="; ".join(parts),
        data=[h.model_dump() for h in hits],
        source_ref=top.kb_id,
        authority=authority,
    )
