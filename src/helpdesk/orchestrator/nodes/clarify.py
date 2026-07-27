"""clarify 节点(LLM,纯文本 completion):QUESTION 型缺口的提问侧(v3.1 P1-3)。

闭环:从 PENDING 的 QUESTION 项选一 → 写 pending_clarify_item_id(本节点独占)→
提问(AWAITING_CLARIFY)→ 恢复 handler 记 from_user、置位 SATISFIED、清空
pending_clarify_item_id → investigate。clarify_count 由本节点计数,硬上限 2
(上限判定在 decide L4,本节点不做边界判定)。

降级路径(guide §5 D3 落后处置,亦是 FakeLLM 默认行为):LLM 输出为空时
直接用 categories.yaml 的 question_hint 模板提问,不阻塞闭环。
"""

from __future__ import annotations

from typing import Any

from helpdesk.config import load_categories
from helpdesk.llm import render_prompt
from helpdesk.orchestrator.nodes import recent_messages, say
from helpdesk.state.models import CaseState, ChecklistStatus


def run_clarify(state: CaseState, ctx: Any) -> None:
    item = _next_question_item(state)
    if item is None:  # decide 只在存在 QUESTION 缺口时路由到此;防御性早退
        return
    state.pending_clarify_item_id = item["item_id"]  # 独占写(v3.1 P1-3)
    state.collected.clarify_count += 1
    question = ctx.llm.complete_text(
        "clarify",
        render_prompt(
            "clarify",
            issue=f"category={state.issue.category.value}; verbatim={state.issue.verbatim!r}",
            hint=item["question_hint"],
            messages=recent_messages(state),
        ),
        budget=state.budget,
    ).strip()
    say(state, question or item["question_hint"])


def _next_question_item(state: CaseState) -> dict[str, Any] | None:
    """按 checklist 声明顺序取第一个 PENDING 的 QUESTION 项(确定性选题)。"""
    items = load_categories()["categories"][state.issue.category.value]["checklist"]
    for item in items:
        if (
            item["source"] == "QUESTION"
            and state.collected.checklist.get(item["item_id"], ChecklistStatus.PENDING)
            is ChecklistStatus.PENDING
        ):
            return item
    return None
