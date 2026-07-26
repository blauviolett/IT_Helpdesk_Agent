"""LLM 节点(5 个,冻结):intake / investigate(D2)+ clarify / resolve / escalate(D3/D4)。

节点只做语义生成:不做边界判定、不写 pending_action、不写 actor(guide §2)。
"""

from __future__ import annotations

from helpdesk.state.models import CaseState


def recent_messages(state: CaseState, n: int = 6) -> str:
    """最近 6 轮进上下文(guide §3 messages 行)。"""
    lines = [f"{m.role}: {m.content}" for m in state.messages[-n:]]
    return "\n".join(lines) if lines else "(无)"
