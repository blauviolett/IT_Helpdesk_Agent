"""decide() — 全部业务边界裁决(guide §4.1 冻结门序,严格按序短路)。

纯函数:零 IO、零 LLM、无副作用。配置(categories / limits)作为已加载常量传入
或取进程内缓存;时钟可注入。decide 不修改 state —— gates / confidence 由它独占
产出,经 Decision 返回,由 runner 落盘(D2)。

E4(SYSTEM_ERROR)已迁移至 runner 层(guide §4.3):快照 → 异常 → 回滚 → 直升
escalate(reason=SYSTEM_ERROR),不经 decide。编号保留不复用,E5–E10 不重编号。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from helpdesk.config import load_categories, load_limits
from helpdesk.state.models import (
    Category,
    CaseState,
    ChecklistStatus,
    Confidence,
    HypothesisStatus,
    Phase,
    ReasonCode,
)


class Branch(StrEnum):
    OBSERVE = "OBSERVE"
    ESCALATE = "ESCALATE"
    REDIRECT = "REDIRECT"
    RESOLVE = "RESOLVE"
    ASK = "ASK"
    INVESTIGATE = "INVESTIGATE"


@dataclass(frozen=True)
class Decision:
    branch: Branch
    reason_code: ReasonCode | None
    confidence: Confidence
    gates: dict[str, str] = field(default_factory=dict)  # {R1..R3: PASS|FAIL},仅 L4 评估时填


def decide(
    state: CaseState,
    *,
    now: datetime | None = None,
    categories: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
) -> Decision:
    categories = categories if categories is not None else load_categories()
    limits = limits if limits is not None else load_limits()
    now = now or datetime.now(timezone.utc)

    items = categories["categories"][state.issue.category.value]["checklist"]
    confidence = _confidence(state, items)

    def escalate(reason: ReasonCode, gates: dict[str, str] | None = None) -> Decision:
        return Decision(Branch.ESCALATE, reason, confidence, gates or {})

    # ---------------- L0 生命周期 ----------------
    if state.phase == Phase.ESCALATED:
        # 该分支实际由恢复路由承接(guide §4.2),此处保留语义
        return Decision(Branch.OBSERVE, None, confidence)
    # pending_action 已过期 → 作废,继续往下。防御性检查:decide 是纯函数,不落盘
    # 作废动作;权威检查点是 act 前置校验(惰性发现)。此处将过期动作视同不存在。
    _ = state.pending_action is not None and state.pending_action.expires_at <= now

    # ---------------- L1 硬红线 ----------------
    # E1
    if state.issue.category == Category.SECURITY:
        return escalate(ReasonCode.SECURITY)
    # E2(唯一输入:policy_decisions,由 intake 后 pre-decide handler 预写入)
    if any(d.decision == "DENY_REQUIRE_HUMAN" for d in state.policy_decisions):
        return escalate(ReasonCode.POLICY_REQUIRED)
    # E3(棘轮字段,ingress 词表写入)
    if state.user_requested_human:
        return escalate(ReasonCode.USER_REQUESTED)
    # E4 —— 已迁移至 runner 层(guide §4.3),编号保留不复用,永不回到 decide
    # E10
    if state.guard_failures >= 2:
        return escalate(ReasonCode.GUARD_FAILED)

    # ---------------- L2 预算 ----------------
    # E5
    b = state.budget
    if (
        b.tool_calls >= limits["tool_calls_max"]
        or b.turns >= limits["turns_max"]
        or b.llm_cost_usd >= limits["llm_cost_usd_max"]
        or b.elapsed_sec >= limits["elapsed_sec_max"]
    ):
        return escalate(ReasonCode.BUDGET_EXHAUSTED)
    # E6:REPEATED_FAILURE 语义,归 E6;reason_code 复用 BUDGET_EXHAUSTED
    # (冻结枚举无 REPEATED_FAILURE,E6 在 L2 预算层 —— 用户裁决 2026-07-26)
    if state.resolution_attempts >= 2:
        return escalate(ReasonCode.BUDGET_EXHAUSTED)

    # ---------------- L3 能力 ----------------
    # E7
    if any(
        state.collected.checklist.get(i["item_id"]) == ChecklistStatus.UNAVAILABLE
        for i in items
        if i["critical"]
    ):
        return escalate(ReasonCode.TOOL_UNAVAILABLE)
    # E8(contradictions 非空;由确定性一致性检查写入,非 LLM)
    if state.contradictions:
        return escalate(ReasonCode.UNRESOLVED_CONTRADICTION)
    # E9(REDIRECT,不建单)
    if state.issue.category == Category.OUT_OF_SCOPE_NON_IT:
        return Decision(Branch.REDIRECT, None, confidence)

    # ---------------- L4 正常分支 ----------------
    gates = _r_gates(state, categories, items)
    if all(v == "PASS" for v in gates.values()):
        return Decision(Branch.RESOLVE, None, confidence, gates)
    # UNKNOWN"电脑坏了"走这里:R1 FAIL → 存在 QUESTION 型缺口 → ASK(v3.1 P0-2)
    question_gap = any(
        i["source"] == "QUESTION"
        and state.collected.checklist.get(i["item_id"], ChecklistStatus.PENDING)
        == ChecklistStatus.PENDING
        for i in items
    )
    if question_gap and state.collected.clarify_count < 2:
        return Decision(Branch.ASK, None, confidence, gates)
    tool_gap = any(
        i["source"] == "TOOL"
        and i["critical"]
        and state.collected.checklist.get(i["item_id"], ChecklistStatus.PENDING)
        == ChecklistStatus.PENDING
        for i in items
    )
    if tool_gap and b.tool_calls < limits["tool_calls_max"]:
        return Decision(Branch.INVESTIGATE, None, confidence, gates)

    # ---------------- L5 兜底 ----------------
    return escalate(ReasonCode.LOW_CONFIDENCE, gates)


def _r_gates(
    state: CaseState, categories: dict[str, Any], items: list[dict[str, Any]]
) -> dict[str, str]:
    """R 门恒为 3 条,全 PASS 才 RESOLVE,无豁免。Output Guard 不是 R 门(resolve 出口检查)。"""
    r1 = state.issue.category.value in categories["auto_resolvable"]
    r2 = all(
        state.collected.checklist.get(i["item_id"]) == ChecklistStatus.SATISFIED
        for i in items
        if i["critical"]
    )
    r3 = sum(1 for h in state.hypotheses if h.status == HypothesisStatus.SUPPORTED) == 1
    return {
        "R1": "PASS" if r1 else "FAIL",
        "R2": "PASS" if r2 else "FAIL",
        "R3": "PASS" if r3 else "FAIL",
    }


def _confidence(state: CaseState, items: list[dict[str, Any]]) -> Confidence:
    """2 布尔量派生:有 VERIFIED KB 引用 且 无 degraded critical 源(不参与安全判定)。"""
    has_verified_kb = any(
        e.tool == "search_kb" and e.authority == "VERIFIED" for e in state.evidence
    )
    critical_tools = {i["tool"] for i in items if i["critical"] and i["source"] == "TOOL"}
    degraded_critical = any(t in state.degraded_sources for t in critical_tools)
    return Confidence.HIGH if has_verified_kb and not degraded_critical else Confidence.LOW
