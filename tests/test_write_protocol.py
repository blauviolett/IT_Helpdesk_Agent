"""写操作三段协议(guide §4.5 / §7.2 必过项):

冻结 / 5min 过期(可注入时钟)/ 重复确认幂等键单次消费 / 拒绝后不执行 /
declined intent 无法产生 pending_action(P1-6)/ FakeLLM 输出恶意 target_user
被忽略 / 写工具恰好 1 次。全部 FakeLLM,CI 无 LLM。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from helpdesk import policy
from helpdesk.llm.fake import FakeLLM
from helpdesk.orchestrator import handlers
from helpdesk.orchestrator.runner import Ctx, handle_message
from helpdesk.state.models import Actor, Outcome, Phase, make_state
from helpdesk.state.store import SQLiteStore
from helpdesk.tools import registry
from helpdesk.trace import Tracer

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


def _ctx(tmp_path, responses=None):
    return Ctx(
        llm=FakeLLM(responses),
        tracer=Tracer(tmp_path / "traces"),
        store=SQLiteStore(tmp_path / "cases.db"),
    )


def _actor(user_id="u-alice"):
    return Actor(user_id=user_id, profile_loaded=True)


def _unlock_count(state):
    return sum(1 for e in state.evidence if e.tool == "send_unlock_verification")


# ================================================================ ② FREEZE


def test_freeze_builds_frozen_args_with_injectable_clock():
    state = make_state(actor=_actor())
    assert policy.freeze(state, "send_unlock_verification", now=NOW) is None
    action = state.pending_action
    assert action is not None
    assert action.tool == "send_unlock_verification"
    assert action.args_frozen == {}  # args 代码冻结:无 target_user,目标 = 会话 actor
    assert action.expires_at == NOW + timedelta(minutes=5)  # 可注入时钟
    assert action.policy_rule_id == "RULE-UNLOCK-SELF"
    assert action.idempotency_key == policy.idempotency_key(
        state.case_id, action.tool, action.args_frozen
    )


def test_freeze_rejects_declined_intent():
    # P1-6 第二道防线:declined intent 无法产生 pending_action
    state = make_state(actor=_actor(), declined_actions=["send_unlock_verification"])
    reason = policy.freeze(state, "send_unlock_verification")
    assert reason is not None
    assert state.pending_action is None


def test_freeze_rejects_undeclared_or_nonconfirmable_intents():
    # deny-by-default:未声明动作拒绝;记账型写工具不走确认协议,同样拒绝
    for intent in ("wipe_all_devices", "create_escalation_ticket", ""):
        state = make_state(actor=_actor())
        assert policy.freeze(state, intent) is not None
        assert state.pending_action is None


def test_freeze_requires_bound_actor():
    state = make_state()  # actor 未绑定
    assert policy.freeze(state, "send_unlock_verification") is not None
    assert state.pending_action is None


# ================================================================ ③ EXECUTE 四校验


def test_act_rejects_expired_action(tmp_path):
    state = make_state(phase=Phase.AWAITING_CONFIRM, actor=_actor())
    past = datetime.now(timezone.utc) - timedelta(minutes=10)
    assert policy.freeze(state, "send_unlock_verification", now=past) is None
    handlers.act(state, _ctx(tmp_path))
    assert state.pending_action is None  # 过期作废
    assert _unlock_count(state) == 0  # 未执行
    assert state.phase is Phase.INVESTIGATING  # 回 decide


def test_act_rejects_actor_mismatch(tmp_path):
    state = make_state(phase=Phase.AWAITING_CONFIRM, actor=_actor("u-alice"))
    assert policy.freeze(state, "send_unlock_verification") is None
    ctx = _ctx(tmp_path)
    ctx.session_user = "u-bob"  # 冻结后换身份续接:确认者 ≠ 动作发起人
    handlers.act(state, ctx)
    assert _unlock_count(state) == 0
    assert state.pending_action is None


def test_act_rejects_tampered_action(tmp_path):
    # 幂等键完整性:args_frozen 被改动 → 与冻结时不一致 → 拒执行
    state = make_state(phase=Phase.AWAITING_CONFIRM, actor=_actor())
    assert policy.freeze(state, "send_unlock_verification") is None
    state.pending_action.args_frozen["target_user"] = "u-bob"  # 模拟篡改
    handlers.act(state, _ctx(tmp_path))
    assert _unlock_count(state) == 0


def test_act_executes_exactly_once_idempotency_consumed(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.session_user = "u-alice"
    state = make_state(phase=Phase.AWAITING_CONFIRM, actor=_actor())
    assert policy.freeze(state, "send_unlock_verification") is None
    handlers.act(state, ctx)
    assert state.pending_action is None
    assert state.phase is Phase.AWAITING_VERIFY
    assert _unlock_count(state) == 1

    # 重复确认:同一动作重新冻结 → 幂等键相同 → 已消费,拒绝二次执行
    state.phase = Phase.AWAITING_CONFIRM
    assert policy.freeze(state, "send_unlock_verification") is None
    handlers.act(state, ctx)
    assert _unlock_count(state) == 1  # 写工具恰好 1 次
    assert state.phase is Phase.INVESTIGATING


# ================================================================ 端到端(经恢复路由)


_KB_EVIDENCE = {
    "id": "e1",
    "tool": "search_kb",
    "args_hash": "x",
    "status": "OK",
    "digest": "KB-1001 [VERIFIED] Okta account locked out — self-service unlock",
    "source_ref": "KB-1001",
    "authority": "VERIFIED",
}


def _resolve_ready_state(**overrides):
    """R1–R3 全 PASS、有 VERIFIED KB 证据的 ACCOUNT_AUTH 态(可直接进 RESOLVE)。"""
    return make_state(
        category="ACCOUNT_AUTH",
        phase=Phase.INVESTIGATING,
        actor=_actor(),
        checklist={
            "auth_account_status": "SATISFIED",
            "auth_kb_guidance": "SATISFIED",
            "auth_service_status": "SATISFIED",
        },
        hypotheses=[{"id": "h1", "text": "账号因连续失败尝试被锁", "status": "SUPPORTED"}],
        evidence=[dict(_KB_EVIDENCE)],
        **overrides,
    )


_ACTION_PROPOSAL = {
    "root_cause": "账号被锁定",
    "explanation": "连续 5 次失败登录触发锁定",
    "resolution_type": "ACTION",
    "intent": "send_unlock_verification",
    "rationale": "自助解锁验证可清除锁定",
    "target_user": "u-bob",  # 恶意/越权字段:schema 之外,必须被忽略
    "steps": [{"text": "发送解锁验证邮件", "citation": "KB-1001", "citation_kind": "KB"}],
}


def test_malicious_target_user_ignored_and_full_yes_flow(tmp_path):
    ctx = _ctx(tmp_path, {"resolve": dict(_ACTION_PROPOSAL)})
    state = _resolve_ready_state()
    ctx.store.save(state)

    # RESOLVE → PROPOSE(含恶意 target_user)→ FREEZE(代码冻结,忽略模型参数)
    s = handle_message("我又被锁了,帮我处理", ctx=ctx, case_id=state.case_id, as_user="u-alice")
    assert s.phase is Phase.AWAITING_CONFIRM
    assert s.pending_action is not None
    assert s.pending_action.args_frozen == {}  # target_user 被忽略
    assert "u-bob" not in str(s.pending_action.model_dump())

    # 显式 YES → act 真执行(目标 = 会话 actor,非模型指定)→ AWAITING_VERIFY
    s = handle_message("发吧", ctx=ctx, case_id=state.case_id, as_user="u-alice")
    assert s.phase is Phase.AWAITING_VERIFY
    assert _unlock_count(s) == 1
    unlock = next(e for e in s.evidence if e.tool == "send_unlock_verification")
    assert "u-alice" in unlock.digest and "u-bob" not in unlock.digest

    # 验证成功 → close 结算 RESOLVED_BY_AGENT
    s = handle_message("好了,能登录了", ctx=ctx, case_id=state.case_id, as_user="u-alice")
    assert s.phase is Phase.CLOSED
    assert s.outcome is Outcome.RESOLVED_BY_AGENT
    assert _unlock_count(s) == 1  # 全程恰好 1 次


def test_declined_action_leads_to_guided_not_repeat(tmp_path):
    guided = {
        "root_cause": "账号被锁定",
        "explanation": "锁定 30 分钟后自动过期,也可等待后自助重试",
        "resolution_type": "GUIDED",
        "steps": [{"text": "等待 30 分钟锁定过期后重新登录", "citation": "KB-1001", "citation_kind": "KB"}],
    }
    ctx = _ctx(tmp_path, {"resolve": [dict(_ACTION_PROPOSAL), guided]})
    state = _resolve_ready_state()
    ctx.store.save(state)

    s = handle_message("帮我看看账号", ctx=ctx, case_id=state.case_id, as_user="u-alice")
    assert s.phase is Phase.AWAITING_CONFIRM

    # 拒绝 → declined_actions 记账 → 不执行 → 第二次 RESOLVE 给 GUIDED 而非复读 ACTION
    s = handle_message("不用发", ctx=ctx, case_id=state.case_id, as_user="u-alice")
    assert s.declined_actions == ["send_unlock_verification"]
    assert _unlock_count(s) == 0  # 拒绝后不执行
    assert s.phase is Phase.AWAITING_VERIFY  # GUIDED 直接进入验证等待
    assert s.diagnosis.resolution_type.value == "GUIDED"
    assert s.pending_action is None


def test_freeze_rejected_intent_after_decline_cannot_produce_action(tmp_path):
    # 模型无视 declined 约束复读同一 ACTION:FREEZE 两次拒绝 → 诊断清空回 decide,
    # 不产生 pending_action(P1-6:prompt 防复读,代码防失守)
    ctx = _ctx(tmp_path, {"resolve": [dict(_ACTION_PROPOSAL), dict(_ACTION_PROPOSAL)]})
    state = _resolve_ready_state(declined_actions=["send_unlock_verification"])
    ctx.store.save(state)
    s = handle_message("再想想办法", ctx=ctx, case_id=state.case_id, as_user="u-alice")
    assert s.pending_action is None
    assert s.phase is not Phase.AWAITING_CONFIRM
    assert _unlock_count(s) == 0


def test_write_tools_never_in_model_tool_list():
    joined = " ".join(registry.model_tool_specs())
    assert "send_unlock_verification" not in joined
    assert "create_escalation_ticket" not in joined


def test_write_tool_not_model_invocable(tmp_path):
    # 模型侧(investigate 等)即使报出写工具名,也只会得到 ERROR 信封,不执行
    state = make_state(phase=Phase.AWAITING_CONFIRM, actor=_actor())
    result = registry.execute(state, "send_unlock_verification", {}, _ctx(tmp_path))
    assert result.status.value == "ERROR"
    assert _unlock_count(state) == 0
