"""escalate 节点(LLM,纯文本两段):升级叙述 + 建单(guide §5 D4)。

- LLM 只写 agent_diagnosis / needed_from_human 两段;队列/定级/packet 字段全部
  代码决定(handoff + policy.yaml)。
- SECURITY(红线:不做诊断、不给建议)与 SYSTEM_ERROR(E4 之后不再调用 LLM)
  两类 reason 用确定性模板,不经模型;其余 reason 的 LLM 输出为空或格式不符时
  同样降级到模板(FakeLLM 默认行为,CI 确定性)。
- outcome=ESCALATED 在建单时刻写入(指标在建单时刻归因,close 只保持不改写)。
- 防御:重复进入且已有 ticket_id 时不重复建单。
"""

from __future__ import annotations

from typing import Any

from helpdesk import handoff
from helpdesk.config import load_policy
from helpdesk.llm import render_prompt
from helpdesk.orchestrator.nodes import say
from helpdesk.state.models import CaseState, Outcome, ReasonCode
from helpdesk.tools import registry
from helpdesk.tools.base import ToolStatus

# reason → 触发来源(E4 在 runner,E5/E6 共用 BUDGET_EXHAUSTED,L5 兜底)
_TRIGGERED_BY: dict[ReasonCode, str] = {
    ReasonCode.SECURITY: "E1/ingress",
    ReasonCode.POLICY_REQUIRED: "E2",
    ReasonCode.USER_REQUESTED: "E3",
    ReasonCode.SYSTEM_ERROR: "runner(E4)",
    ReasonCode.BUDGET_EXHAUSTED: "E5/E6",
    ReasonCode.TOOL_UNAVAILABLE: "E7",
    ReasonCode.UNRESOLVED_CONTRADICTION: "E8",
    ReasonCode.GUARD_FAILED: "E10",
    ReasonCode.LOW_CONFIDENCE: "L5",
}

_NO_LLM_REASONS = frozenset({ReasonCode.SECURITY, ReasonCode.SYSTEM_ERROR})

_NEEDED_TEMPLATES: dict[ReasonCode, str] = {
    ReasonCode.SECURITY: "请安全团队直接联系用户接管处置;对话侧已停止诊断与建议。",
    ReasonCode.SYSTEM_ERROR: "系统处理中断,状态已回滚;请人工接管后续诊断(详情见 trace)。",
    ReasonCode.POLICY_REQUIRED: "该权限授予需人工审批;请审批人核对申请人身份与用途后处理。",
    ReasonCode.USER_REQUESTED: "用户明确要求人工服务;请直接联系用户继续处理。",
    ReasonCode.BUDGET_EXHAUSTED: "自动处置预算/尝试次数已用尽;请人工继续诊断。",
    ReasonCode.TOOL_UNAVAILABLE: "关键数据源不可用,信息缺口无法闭合;请人工补齐后处理。",
    ReasonCode.UNRESOLVED_CONTRADICTION: "数据源之间存在矛盾(见 contradictions);请人工核对权威源。",
    ReasonCode.GUARD_FAILED: "方案生成两次未通过引用校验,不输出不可信方案;请人工给出处置。",
    ReasonCode.LOW_CONFIDENCE: "自动路径无法安全收敛;请人工接管(用户回答已附在包内)。",
}


def run_escalate(state: CaseState, ctx: Any) -> None:
    esc = state.escalation
    if esc.ticket_id:  # 已建单:不重复建单(如 ESCALATED 后误路由重入)
        return
    reason = esc.reason_code or ReasonCode.LOW_CONFIDENCE
    policy_cfg = load_policy()
    esc.triggered_by = _TRIGGERED_BY.get(reason, "decide")
    esc.queue = _queue_for(state, reason, policy_cfg)
    esc.impact = policy_cfg["priority"]["scope_to_impact"].get(
        state.issue.scope or "INDIVIDUAL", "LOW"
    )
    esc.urgency = state.issue.urgency or "LOW"
    esc.priority = handoff.assign_priority(state.issue.scope, esc.urgency, policy=policy_cfg)

    diagnosis, needed = _narrative(state, ctx, reason)
    packet = handoff.build_packet(
        state, agent_diagnosis=diagnosis, needed_from_human=needed, policy=policy_cfg
    )
    result = registry.execute(
        state,
        "create_escalation_ticket",
        {
            "queue": esc.queue,
            "priority": esc.priority,
            "subject": f"[{state.issue.category.value}] {state.issue.verbatim[:80]}",
            "packet": packet,
        },
        ctx,
        invoked_by="system",
    )
    if result.status is ToolStatus.OK:
        esc.ticket_id = result.data["ticket_id"]
    state.outcome = Outcome.ESCALATED  # 归因钉在建单时刻(冻结契约)
    say(state, _user_message(state, reason))


def _queue_for(state: CaseState, reason: ReasonCode, policy_cfg: dict[str, Any]) -> str:
    if reason is ReasonCode.SECURITY:
        name = "security-ir"
    elif reason is ReasonCode.POLICY_REQUIRED:
        name = next(
            (d.queue for d in state.policy_decisions if d.decision == "DENY_REQUIRE_HUMAN" and d.queue),
            "it-helpdesk",
        )
    else:
        name = "it-helpdesk"
    return name if name in policy_cfg["queues"] else "it-helpdesk"  # 队列 allowlist


def _narrative(state: CaseState, ctx: Any, reason: ReasonCode) -> tuple[str, str]:
    """两段叙述:红线/系统错误走模板;其余 LLM 生成,空输出或格式不符降级模板。"""
    if reason not in _NO_LLM_REASONS:
        text = ctx.llm.complete_text(
            "escalate",
            render_prompt(
                "escalate",
                issue=(
                    f"category={state.issue.category.value}; "
                    f"verbatim={state.issue.verbatim!r}; scope={state.issue.scope}; "
                    f"urgency={state.issue.urgency}"
                ),
                reason=reason.value,
                evidence="\n".join(
                    f"[{e.id}] {e.tool} ({e.status}): {e.digest}" for e in state.evidence
                ) or "(无)",
                hypotheses="\n".join(
                    f"[{h.id}] ({h.status}) {h.text}" for h in state.hypotheses
                ) or "(无)",
                user_answers="\n".join(
                    f"- {a.question} → {a.answer}" for a in state.collected.from_user
                ) or "(无)",
                tried_by_user="\n".join(
                    f"- {t.step}: {t.outcome or '结果未知'}" for t in state.collected.tried_by_user
                ) or "(无)",
            ),
            budget=state.budget,
        )
        diagnosis, sep, needed = text.partition("---")
        if sep and diagnosis.strip() and needed.strip():
            return diagnosis.strip(), needed.strip()
    return _template_narrative(state, reason)


def _template_narrative(state: CaseState, reason: ReasonCode) -> tuple[str, str]:
    if reason is ReasonCode.SECURITY:
        diagnosis = f"用户报告疑似安全事件(原话:{state.issue.verbatim!r})。按红线不做诊断、不给建议,直接转安全响应。"
    elif reason is ReasonCode.SYSTEM_ERROR:
        diagnosis = "自动处理过程中发生系统内部错误,case 状态已回滚到进入节点前;错误详情仅在 trace 中,不外泄给用户。"
    else:
        top_evidence = "; ".join(e.digest for e in state.evidence[:3]) or "(无)"
        diagnosis = (
            f"类目 {state.issue.category.value};用户原话:{state.issue.verbatim!r};"
            f"升级原因:{reason.value}。关键证据:{top_evidence}"
        )
    return diagnosis, _NEEDED_TEMPLATES[reason]


def _user_message(state: CaseState, reason: ReasonCode) -> str:
    esc = state.escalation
    ticket = esc.ticket_id or "(建单暂未成功,已记录待补)"
    if reason is ReasonCode.SECURITY:
        return (
            f"这可能涉及账号安全,我已直接转给安全团队(工单 {ticket},队列 {esc.queue}),"
            "他们会尽快联系你。在此之前请不要再点击可疑链接,也不要在任何地方输入账号密码。"
        )
    if reason is ReasonCode.SYSTEM_ERROR:
        return (
            f"抱歉,刚才系统处理时出了点问题。为稳妥起见我已转人工跟进(工单 {ticket}),"
            "会有同事尽快联系你,你不需要重复描述问题。"
        )
    return (
        f"这个问题需要人工处理,我已创建工单 {ticket}(队列 {esc.queue},优先级 {esc.priority}),"
        "并附上了完整的诊断上下文,你不需要再重复描述。有进展会在工单里更新;"
        "如果问题已经解决,回复\"好了\"我就把工单关掉。"
    )
