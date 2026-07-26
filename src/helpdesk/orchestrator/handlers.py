"""生命周期路由与副作用执行(代码,不调 LLM;guide §2)。

D2 落地:ingress / intake 后 pre-decide 预写入 / sync_checklist / load_actor。
confirm / verify / clarify 恢复 / escalate_followup / close 按计划 D3–D4 落地。
"""

from __future__ import annotations

import uuid
from typing import Any

from helpdesk.config import load_categories, load_policy
from helpdesk.guards import detect_human_request
from helpdesk.orchestrator import transition
from helpdesk.state.models import (
    Actor,
    CaseState,
    Category,
    ChecklistStatus,
    Device,
    Message,
    Phase,
    PolicyDecision,
    mark_user_requested_human,
    write_actor,
)
from helpdesk.state.store import Store
from helpdesk.tools.base import ToolStatus


def ingress(store: Store, state: CaseState | None, text: str) -> tuple[CaseState, str | None]:
    """每条用户消息的唯一入口(guide §4.2):

    ① 转人工词表检查(每条消息,棘轮置 user_requested_human);
    ② 安全信号直升 —— Input Guard 按计划 D3 接线(intake 归类 SECURITY 走 E1 兜住);
    ③ 按 phase 路由;无 case / CLOSED → 建最小 case 并落盘,写 issue.verbatim(独占,一次)。
    """
    if state is None or state.phase is Phase.CLOSED:
        state = CaseState(case_id=f"case-{uuid.uuid4().hex[:12]}")
        state.issue.verbatim = text
        next_node = "intake"
    else:
        next_node = transition.route_message(state.phase)
    state.turn_count += 1
    state.budget.turns = state.turn_count  # E5 turns 消费;预算拦截器 D3 接管
    state.messages.append(Message(turn_id=state.turn_count, role="user", content=text))
    if detect_human_request(text):
        mark_user_requested_human(state)
    store.save(state)  # 先落盘再进节点(§4.2:建最小 case 并落盘)
    return state, next_node


def pre_decide(state: CaseState) -> None:
    """intake 后、decide 前的代码 handler(v3 C-3):

    ① checklist 按类目补齐 PENDING 项;
    ② ACCESS_REQUEST:对每个枚举资源查 policy.yaml → 写 policy_decisions(E2 唯一输入);
      "other" 不触发 E2(P1-4),走 R1 FAIL → 升级方向。
    """
    sync_checklist(state)
    if state.issue.category is not Category.ACCESS_REQUEST:
        return
    resources = load_policy()["resources"]
    decisions: list[PolicyDecision] = []
    for name in state.issue.requested_resources:
        cfg = resources.get(name)
        if cfg is None:  # "other" 或未知值
            continue
        decisions.append(
            PolicyDecision(
                action=f"grant_access:{name}",
                decision=cfg["decision"],
                rule_id=f"RES-{name}",
                queue=cfg.get("queue"),
            )
        )
    state.policy_decisions = decisions


def sync_checklist(state: CaseState) -> None:
    """按当前类目补齐 checklist 缺失项为 PENDING;不覆盖已有置位。"""
    items = load_categories()["categories"][state.issue.category.value]["checklist"]
    for item in items:
        state.collected.checklist.setdefault(item["item_id"], ChecklistStatus.PENDING)


def load_actor(state: CaseState, user_id: str, ctx: Any) -> None:
    """运行时装载 actor(--as-user + get_user_profile;guide §3 所有权表)。"""
    from helpdesk.tools import registry  # 延迟导入避免环

    write_actor(state, Actor(user_id=user_id), writer="runtime")
    result = registry.execute(state, "get_user_profile", {}, ctx)
    if result.status is not ToolStatus.OK:
        return  # 目录查不到:actor 仅带 user_id,profile_loaded 保持 False
    data = result.data
    write_actor(
        state,
        Actor(
            user_id=user_id,
            display_name=data["display_name"],
            department=data["department"],
            location=data["location"],
            tenure_days=data["tenure_days"],
            device=Device(**data["device"]),
            groups=list(data["groups"]),
            profile_loaded=True,
        ),
        writer="runtime",
    )
