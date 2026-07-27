"""预算拦截器(v2.1 A5,guide §2):tool_calls / turns / cost / elapsed 四维上限。

唯一口径函数 over_budget(),三处消费:
- gates.decide 的 E5(纯函数,limits 由调用方传入);
- investigate 循环终止条件 3(预算触顶);
- registry.execute 运行门(触顶后拒绝继续执行工具,不落 evidence)。
超限的业务后果(ESCALATE(BUDGET_EXHAUSTED))始终由 decide 裁决,本文件只做拦截。
"""

from __future__ import annotations

from typing import Any

from helpdesk.config import load_limits
from helpdesk.state.models import Budget


def over_budget(budget: Budget, limits: dict[str, Any] | None = None) -> str | None:
    """任一维度触顶返回该维度名,否则 None。纯函数(limits 传入时零 IO)。"""
    limits = limits if limits is not None else load_limits()
    if budget.tool_calls >= limits["tool_calls_max"]:
        return "tool_calls"
    if budget.turns >= limits["turns_max"]:
        return "turns"
    if budget.llm_cost_usd >= limits["llm_cost_usd_max"]:
        return "llm_cost_usd"
    if budget.elapsed_sec >= limits["elapsed_sec_max"]:
        return "elapsed_sec"
    return None
