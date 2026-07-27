"""LLM 节点(5 个,冻结):intake / investigate(D2)+ clarify / resolve / escalate(D3/D4)。

节点只做语义生成:不做边界判定、不写 pending_action、不写 actor(guide §2)。
"""

from __future__ import annotations

from helpdesk.state.models import CaseState, Message


def recent_messages(state: CaseState, n: int = 6) -> str:
    """最近 6 轮进上下文(guide §3 messages 行)。"""
    lines = [f"{m.role}: {m.content}" for m in state.messages[-n:]]
    return "\n".join(lines) if lines else "(无)"


def say(state: CaseState, text: str) -> None:
    """追加一条用户可见的 assistant 消息(messages 归运行时,节点/handler 经此写)。"""
    state.messages.append(Message(turn_id=state.turn_count, role="assistant", content=text))
