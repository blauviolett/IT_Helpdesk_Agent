"""生命周期路由与副作用执行(代码,不调 LLM 节点;guide §2)。

D2 落地:ingress / intake 后 pre-decide 预写入 / sync_checklist / load_actor。
D3 落地:Input Guard 接线 + 四个恢复 handler(clarify_resume / confirm / verify /
escalated_followup;消息语义分支由共享 classifier 完成)。
D4 落地:act(三段协议 ③ EXECUTE,前置四校验)/ close(outcome 结算)/
escalate_followup 追加工单评论。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from helpdesk import policy
from helpdesk.config import load_categories, load_policy
from helpdesk.guards import (
    detect_attachment,
    detect_human_request,
    detect_security_signal,
    redact_credentials,
)
from helpdesk.orchestrator import classifier, transition
from helpdesk.orchestrator.nodes import say
from helpdesk.state.models import (
    Actor,
    CaseState,
    Category,
    ChecklistStatus,
    Device,
    FromUserEntry,
    Message,
    Outcome,
    PendingAction,
    Phase,
    PolicyDecision,
    ReasonCode,
    ResolutionType,
    mark_user_requested_human,
    set_pending_action,
    write_actor,
)
from helpdesk.state.store import Store
from helpdesk.tools.adapters import itsm
from helpdesk.tools.base import ToolStatus, hash_args


def ingress(store: Store, state: CaseState | None, text: str) -> tuple[CaseState, str | None]:
    """每条用户消息的唯一入口(guide §4.2):

    ① Input Guard(确定性):凭据脱敏(原文不落库)、附件拒收提示、
       转人工词表(每条消息,棘轮置 user_requested_human);
    ② 安全信号 → 先建 case 落盘再直升 escalate(词表漏检由 intake 归类 SECURITY 走 E1 兜住);
    ③ 按 phase 路由;无 case / CLOSED → 建最小 case 并落盘,写 issue.verbatim(独占,一次)。
    """
    text, had_credential = redact_credentials(text)
    if state is None or state.phase is Phase.CLOSED:
        state = CaseState(case_id=f"case-{uuid.uuid4().hex[:12]}")
        state.issue.verbatim = text
        next_node = "intake"
    else:
        next_node = transition.route_message(state.phase)
    state.turn_count += 1
    state.budget.turns = state.turn_count  # E5 turns 口径 = 消息轮数
    state.messages.append(Message(turn_id=state.turn_count, role="user", content=text))
    if had_credential:
        say(state, "出于安全考虑,请不要在对话中发送密码或密钥;刚才的内容已做脱敏处理。")
    if detect_attachment(text):
        say(state, "本通道不接收附件或截图,请用文字描述问题现象。")
    if detect_human_request(text):
        mark_user_requested_human(state)
    if detect_security_signal(text):
        # 直升属 runner 层代码路径(同 E4 先例):不经 decide,不做诊断、不给建议
        state.escalation.required = True
        state.escalation.reason_code = ReasonCode.SECURITY
        next_node = "escalate"
    store.save(state)  # 先落盘再进节点(§4.2:建最小 case 并落盘)
    return state, next_node


# ================================================= 等待态恢复 handler(§4.2 路由表)

_VERIFY_PROBE = "想跟你确认一下:按刚才的步骤操作后,问题解决了吗?回复\"好了\"或\"还没好\"即可。"


def resume(state: CaseState, entry: str, ctx: Any) -> str | None:
    """等待态消息的语义分支。返回下一节点名;None = 交回主循环
    (phase 仍为等待态 → 本轮结束;已切回 INVESTIGATING → 回 decide)。"""
    return _RESUME_HANDLERS[entry](state, ctx)


def _clarify_resume(state: CaseState, ctx: Any) -> str:
    """AWAITING_CLARIFY(P1-3 闭环):记 from_user → 置位 SATISFIED → 清指针 → investigate。"""
    item_id = state.pending_clarify_item_id
    if item_id is not None:
        state.collected.from_user.append(
            FromUserEntry(
                item_id=item_id,
                question=_last_text(state, "assistant"),
                answer=_last_text(state, "user"),
                turn_id=state.turn_count,
            )
        )
        state.collected.checklist[item_id] = ChecklistStatus.SATISFIED
        state.pending_clarify_item_id = None
    state.phase = Phase.INVESTIGATING
    return "investigate"


def _confirm(state: CaseState, ctx: Any) -> str | None:
    """AWAITING_CONFIRM(三分):只有显式 YES 消费 pending_action(冻结契约)。
    NO → 记 declined_actions + 作废 → decide;OTHER → 作废 → decide。"""
    label = classifier.classify_confirm(_last_text(state, "user"), llm=ctx.llm, budget=state.budget)
    if label == "YES" and state.pending_action is not None:
        return "act"  # 消费动作在 act 前置四校验之后(D4)
    if label == "NO" and state.pending_action is not None:
        # intent 命名空间 = 写工具名;confirm handler 独占写 declined_actions
        state.declined_actions.append(state.pending_action.tool)
        say(state, "好的,这个操作不会执行。我看看别的办法。")
    elif state.pending_action is not None:
        say(state, "这个操作先不执行。")
    # 作废:非显式 YES 一律不消费(协议代码路径,经唯一合法写入口)
    set_pending_action(state, None, writer="action_builder")
    state.phase = Phase.INVESTIGATING
    return None  # 回 decide


def _verify(state: CaseState, ctx: Any) -> str | None:
    """AWAITING_VERIFY(三态):成功 → close;失败 → decide;
    UNKNOWN → 模板追问 1 次(不调 LLM),第二次仍非成功按失败。"""
    label = classifier.classify_verify(_last_text(state, "user"), llm=ctx.llm, budget=state.budget)
    if label == "RESOLVED":
        return "close"
    if label == "UNKNOWN" and not state.verify_probe_sent:
        state.verify_probe_sent = True  # verify handler 独占写
        say(state, _VERIFY_PROBE)
        return None  # phase 保持 AWAITING_VERIFY,本轮结束
    state.phase = Phase.INVESTIGATING
    return None  # 按失败回 decide(P1-7:第二次 resolve 带 failure_feedback)


def _escalated_followup(state: CaseState, ctx: Any) -> str | None:
    """ESCALATED(二分):RESOLVED → close(outcome 保持 ESCALATED);
    OTHER → 追加工单评论,phase 不变,不重诊断。"""
    label = classifier.classify_escalated(
        _last_text(state, "user"), llm=ctx.llm, budget=state.budget
    )
    if label == "RESOLVED":
        return "close"
    itsm.append_comment(state.escalation.ticket_id, _last_text(state, "user"))
    ctx.tracer.event(state.case_id, "escalate_followup", ticket_id=state.escalation.ticket_id)
    say(state, "已记录,会同步给正在处理这个工单的同事;有进展会在工单里更新。")
    return None  # phase 保持 ESCALATED


# ============================================ act(三段协议 ③ EXECUTE,代码节点)


def act(state: CaseState, ctx: Any) -> None:
    """只可能从 confirm 的显式 YES 路由进入。前置四校验(§4.5 ③):
    未过期 / actor 一致 / policy 复核仍 ALLOW / 幂等键未消费;
    任一失败 → 作废动作、不执行、回 decide。成功 → 执行 → 清动作 → AWAITING_VERIFY。"""
    from helpdesk.tools import registry  # 延迟导入避免环

    action = state.pending_action
    failure = _act_precheck(state, action, getattr(ctx, "session_user", None))
    if failure is not None:
        set_pending_action(state, None, writer="action_builder")
        ctx.tracer.event(state.case_id, "act_rejected", reason=failure)
        say(state, f"这个操作没有执行({failure})。我再看看别的办法。")
        state.phase = Phase.INVESTIGATING
        return
    result = registry.execute(state, action.tool, action.args_frozen, ctx, invoked_by="system")
    set_pending_action(state, None, writer="action_builder")  # 单次消费,无论成败
    ctx.tracer.event(
        state.case_id, "act_executed", tool=action.tool,
        action_id=action.action_id, status=result.status.value,
    )
    if result.status is ToolStatus.ERROR:
        say(state, "操作在系统侧执行失败,我重新评估一下,必要时转人工。")
        state.phase = Phase.INVESTIGATING
        return
    say(state, f"已执行:{result.digest}。请按邮件提示操作,然后告诉我是否解决。")
    state.phase = Phase.AWAITING_VERIFY


def _act_precheck(
    state: CaseState, action: PendingAction | None, session_user: str | None,
    now: datetime | None = None,
) -> str | None:
    """返回失败原因;None = 四校验全过。"""
    if action is None:
        return "没有待执行的动作"
    now = now or datetime.now(timezone.utc)
    if action.expires_at <= now:
        return "确认已超时,动作过期作废"
    if session_user and state.actor.user_id and session_user != state.actor.user_id:
        return "确认者与动作发起人不一致"  # actor 一致校验
    if policy.check(action.tool).decision != "ALLOW":
        return "策略复核未通过"
    if policy.idempotency_key(state.case_id, action.tool, action.args_frozen) != action.idempotency_key:
        return "动作与冻结时的内容不一致"
    consumed = any(
        e.tool == action.tool
        and e.args_hash == hash_args(action.tool, action.args_frozen)
        and e.status != "ERROR"
        for e in state.evidence
    )
    if consumed:
        return "该动作已执行过,幂等键已消费"
    return None


# ==================================================== close(outcome 结算,代码节点)


def close(state: CaseState, ctx: Any) -> None:
    """outcome 唯一结算点。升级 case 的 outcome 已在建单时刻归因为 ESCALATED,
    此处只保持不改写(冻结契约);phase=CLOSED 由 transition 后置。"""
    if state.outcome is not None:  # ESCALATED 经用户确认关闭
        ctx.tracer.event(
            state.case_id, "case_closed",
            outcome=state.outcome.value, note="user_confirmed_fixed",
        )
        say(state, "好的,已确认解决,这个工单就关闭了。之后有问题随时找我。")
        return
    if state.issue.category is Category.OUT_OF_SCOPE_NON_IT:
        state.outcome = Outcome.REDIRECTED  # E9:不建单,指路
        say(state, "这个请求不属于 IT 支持范围,建议联系行政/设施或对应业务团队;这里就不建工单了。")
    elif state.diagnosis.resolution_type is ResolutionType.INFORMATIONAL:
        state.outcome = Outcome.INFORMED_KNOWN_INCIDENT
        say(state, "好的。这个问题源于已知的服务事故,服务恢复后会自动好转;本次咨询就记录到这里。")
    else:
        state.outcome = Outcome.RESOLVED_BY_AGENT
        say(state, "太好了,问题已解决,本次工单关闭。之后有问题随时找我。")
    ctx.tracer.event(state.case_id, "case_closed", outcome=state.outcome.value)


_RESUME_HANDLERS: dict[str, Callable[[CaseState, Any], str | None]] = {
    "clarify_resume": _clarify_resume,
    "confirm": _confirm,
    "verify": _verify,
    "escalated_followup": _escalated_followup,
}

RESUME_ENTRIES = frozenset(_RESUME_HANDLERS)


def _last_text(state: CaseState, role: str) -> str:
    for message in reversed(state.messages):
        if message.role == role:
            return message.content
    return ""


def pre_decide(state: CaseState) -> None:
    """intake 后、decide 前的代码 handler(v3 C-3):

    ① checklist 按类目补齐 PENDING 项;
    ② ACCESS_REQUEST:对每个枚举资源跑 policy.check(P1-4 契约链)→
      写 policy_decisions(E2 唯一输入);"other" 不触发 E2,走 R1 FAIL → 升级方向。
    """
    sync_checklist(state)
    if state.issue.category is not Category.ACCESS_REQUEST:
        return
    resource_names = set(load_policy()["resources"])
    decisions: list[PolicyDecision] = []
    for name in state.issue.requested_resources:
        if name not in resource_names:  # "other" 或未知值
            continue
        action = f"grant_access:{name}"
        verdict = policy.check(action)
        decisions.append(
            PolicyDecision(
                action=action,
                decision=verdict.decision,
                rule_id=verdict.rule_id,
                queue=verdict.queue,
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
