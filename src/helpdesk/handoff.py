"""Handoff:Packet 渲染 + 队列字段 allowlist + 定级。

- assign_priority:impact × urgency 查表(config/policy.yaml priority 段)。
- build_packet:升级包渲染。LLM 只写 agent_diagnosis / needed_from_human 两段,
  其余字段全部代码取自 CaseState;按目标队列的 packet_fields allowlist 过滤
  (deny-by-default:不在 allowlist 的字段不出现);transcript(messages)
  永不进入工单正文(冻结契约)。
"""

from __future__ import annotations

from typing import Any

from helpdesk.config import load_policy
from helpdesk.state.models import CaseState


def assign_priority(
    scope: str | None,
    urgency: str | None,
    *,
    policy: dict[str, Any] | None = None,
) -> str:
    """impact × urgency 9 格查表;个人(INDIVIDUAL)deadline 上限 P2(不产生 P1)。

    未知 scope 按 INDIVIDUAL(最保守的低影响),未知 urgency 按 LOW。
    """
    policy = policy if policy is not None else load_policy()
    cfg = policy["priority"]
    impact = cfg["scope_to_impact"].get(scope or "INDIVIDUAL", "LOW")
    priority = cfg["matrix"][impact].get(urgency or "LOW", cfg["matrix"][impact]["LOW"])
    if (scope or "INDIVIDUAL") == "INDIVIDUAL" and priority == "P1":
        priority = "P2"
    return priority


def build_packet(
    state: CaseState,
    *,
    agent_diagnosis: str,
    needed_from_human: str,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """渲染升级包并按队列 allowlist 过滤。队列取 state.escalation.queue
    (escalate 节点先定队列再建包),未知队列回落 it-helpdesk 的 allowlist。"""
    policy = policy if policy is not None else load_policy()
    esc, issue, actor = state.escalation, state.issue, state.actor
    full: dict[str, Any] = {
        "case_id": state.case_id,
        "category": issue.category.value,
        "reason_code": esc.reason_code.value if esc.reason_code else None,
        "priority": esc.priority,
        "queue": esc.queue,
        "verbatim": issue.verbatim,
        "agent_diagnosis": agent_diagnosis,
        "needed_from_human": needed_from_human,
        "requested_resources": list(issue.requested_resources),
        "user_answers": [
            {"item_id": a.item_id, "question": a.question, "answer": a.answer}
            for a in state.collected.from_user
        ],
        "tried_by_user": [
            {"step": t.step, "outcome": t.outcome} for t in state.collected.tried_by_user
        ],
        "evidence": [f"[{e.id}] {e.tool} ({e.status}): {e.digest}" for e in state.evidence],
        "contradictions": [c.description for c in state.contradictions],
        "requester": {
            "user_id": actor.user_id,
            "display_name": actor.display_name,
            "department": actor.department,
            "location": actor.location,
        },
        "device_info": {
            "model": actor.device.model,
            "os": actor.device.os,
            "vpn_client_version": actor.device.vpn_client_version,
        },
    }
    queues = policy["queues"]
    allowed = set(queues.get(esc.queue or "", queues["it-helpdesk"])["packet_fields"])
    return {k: v for k, v in full.items() if k in allowed}
