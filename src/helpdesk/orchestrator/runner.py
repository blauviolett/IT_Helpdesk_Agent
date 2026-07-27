"""主循环(≤80 行)。E4 承载在此:节点前深拷贝快照,未捕获异常 → 回滚 → 置
escalation.required → 直升 escalate(SYSTEM_ERROR),不经 decide,永不外泄堆栈。
resolution_attempts 由本层计数(RESOLVE 分支 +1);elapsed_sec 由本层记账。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from helpdesk.orchestrator import handlers, transition
from helpdesk.orchestrator.gates import Branch, decide
from helpdesk.orchestrator.nodes import clarify, intake, investigate, resolve
from helpdesk.state.models import CaseState, ReasonCode
from helpdesk.state.store import Store
from helpdesk.tools.base import ToolRuntime
from helpdesk.trace import Tracer


@dataclass
class Ctx:
    llm: Any
    tracer: Tracer
    store: Store
    runtime: ToolRuntime = field(default_factory=ToolRuntime)

# 节点注册表:act / escalate / close 按计划 D4 注册。
NODES: dict[str, Callable[[CaseState, Ctx], None]] = {
    "intake": intake.run_intake, "investigate": investigate.run_investigate,
    "clarify": clarify.run_clarify, "resolve": resolve.run_resolve,
}
_MAX_HOPS = 16  # 纯防御(死循环保险丝);业务边界由 decide 的 E5/E6 承担


def handle_message(
    text: str, *, ctx: Ctx, case_id: str | None = None, as_user: str | None = None
) -> CaseState:
    started = time.perf_counter()
    state = ctx.store.get(case_id) if case_id else None
    state, node_name = handlers.ingress(ctx.store, state, text)
    if as_user and not state.actor.profile_loaded:
        handlers.load_actor(state, as_user, ctx)
    if node_name in handlers.RESUME_ENTRIES:  # 等待态恢复:语义分支在 handler 内完成
        node_name = handlers.resume(state, node_name, ctx)
    for _ in range(_MAX_HOPS):
        if node_name is not None:
            node = NODES.get(node_name)
            if node is None:  # 该节点后日交付:停在当前 decide 结果,状态已落账
                ctx.tracer.event(state.case_id, "node_pending_delivery", node=node_name)
                break
            snapshot = state.model_copy(deep=True)  # §4.3:深拷贝,嵌套结构不可穿透
            ctx.tracer.event(state.case_id, "node_start", node=node_name)
            try:
                node(state, ctx)
            except Exception as exc:  # noqa: BLE001 — E4:回滚→直升,不经 decide
                state = snapshot
                state.escalation.required = True
                state.escalation.reason_code = ReasonCode.SYSTEM_ERROR
                ctx.tracer.event(state.case_id, "system_error", node=node_name, error=type(exc).__name__)
                node_name = "escalate"
                continue
            if node_name == "intake":
                handlers.pre_decide(state)  # checklist 同步 + policy 预写入(E2 输入)
            transition.apply_post_phase(state, node_name)
        if transition.is_waiting(state.phase):
            break
        decision = decide(state)
        state.gates, state.confidence = dict(decision.gates), decision.confidence
        ctx.tracer.event(state.case_id, "decision", branch=decision.branch.value, gates=decision.gates,
                         reason=decision.reason_code.value if decision.reason_code else None,
                         confidence=decision.confidence.value)
        if decision.branch is Branch.RESOLVE:
            state.resolution_attempts += 1  # guard 节点内重试不经此处,不计数
        if decision.branch is Branch.ESCALATE:
            state.escalation.required, state.escalation.reason_code = True, decision.reason_code
        node_name = transition.on_decision(decision.branch)
        if node_name is None:
            break
    state.budget.elapsed_sec += time.perf_counter() - started  # E5 elapsed:处理时长
    ctx.store.save(state)
    return state
