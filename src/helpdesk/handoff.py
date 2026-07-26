"""Handoff:Packet 渲染 + 队列 allowlist + 定级。

D1 仅落地定级纯函数(test_gates.py §7.2 必过项消费);Packet 渲染与
字段 allowlist 按计划 D4 落地。矩阵数据来源:config/policy.yaml priority 段。
"""

from __future__ import annotations

from typing import Any

from helpdesk.config import load_policy


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
