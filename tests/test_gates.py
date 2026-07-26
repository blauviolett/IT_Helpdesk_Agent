"""decide() 逐条单测(guide §7.2):E1–E3、E5–E10、R1–R3、短路序 + handoff 定级。

E4 不在此文件 —— 已迁移至 runner 层,由 test_routing.py 的抛异常 FakeNode 触发(D3)。
"""

from datetime import datetime, timedelta, timezone

import pytest

from helpdesk.handoff import assign_priority
from helpdesk.orchestrator.gates import Branch, decide
from helpdesk.state.models import ReasonCode, make_state

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


def resolve_ready(**overrides):
    """R1–R3 全 PASS 的最小态:AUTO_RESOLVABLE 类目 + critical 全 SATISFIED + 恰 1 个 SUPPORTED 假设。"""
    base = dict(
        category="ACCOUNT_AUTH",
        checklist={
            "auth_account_status": "SATISFIED",
            "auth_kb_guidance": "SATISFIED",
            "auth_service_status": "PENDING",
        },
        hypotheses=[{"id": "h1", "text": "account locked by failed attempts", "status": "SUPPORTED"}],
    )
    base.update(overrides)
    return make_state(**base)


# ---------------------------------------------------------------- L1 硬红线


def test_e1_security_escalates_immediately():
    d = decide(make_state(category="SECURITY"))
    assert d.branch == Branch.ESCALATE
    assert d.reason_code == ReasonCode.SECURITY


def test_e2_policy_deny_require_human():
    # 经 policy_decisions 构造,不依赖 policy 引擎(guide §7.2)
    d = decide(
        make_state(
            category="ACCESS_REQUEST",
            policy_decisions=[
                {
                    "action": "grant_access:snowflake_prod",
                    "decision": "DENY_REQUIRE_HUMAN",
                    "rule_id": "R-ACCESS-1",
                    "queue": "data-platform-approvers",
                }
            ],
        )
    )
    assert d.branch == Branch.ESCALATE
    assert d.reason_code == ReasonCode.POLICY_REQUIRED


def test_e3_user_requested_human():
    d = decide(make_state(user_requested_human=True))
    assert d.branch == Branch.ESCALATE
    assert d.reason_code == ReasonCode.USER_REQUESTED


def test_e3_takes_priority_over_l2_budget():
    d = decide(make_state(user_requested_human=True, budget={"tool_calls": 10}))
    assert d.reason_code == ReasonCode.USER_REQUESTED


def test_e3_takes_priority_over_l3_contradictions():
    d = decide(
        make_state(
            user_requested_human=True,
            contradictions=[{"check_id": "C1", "description": "groups vs entitlements"}],
        )
    )
    assert d.reason_code == ReasonCode.USER_REQUESTED


def test_e3_takes_priority_over_l4_resolve():
    d = decide(resolve_ready(user_requested_human=True))
    assert d.branch == Branch.ESCALATE
    assert d.reason_code == ReasonCode.USER_REQUESTED


def test_e10_guard_failures_two_triggers():
    d = decide(make_state(guard_failures=2))
    assert d.branch == Branch.ESCALATE
    assert d.reason_code == ReasonCode.GUARD_FAILED


def test_e10_guard_failures_one_does_not_trigger():
    d = decide(resolve_ready(guard_failures=1))
    assert d.branch == Branch.RESOLVE


def test_l1_shortcircuits_l2():
    # SECURITY 与预算同时触发 → L1 先短路
    d = decide(make_state(category="SECURITY", budget={"tool_calls": 10}))
    assert d.reason_code == ReasonCode.SECURITY


# ---------------------------------------------------------------- L2 预算


@pytest.mark.parametrize(
    "budget",
    [
        {"tool_calls": 10},
        {"turns": 8},
        {"llm_cost_usd": 0.10},
        {"elapsed_sec": 180},
    ],
)
def test_e5_budget_exhausted(budget):
    d = decide(make_state(budget=budget))
    assert d.branch == Branch.ESCALATE
    assert d.reason_code == ReasonCode.BUDGET_EXHAUSTED


def test_e6_repeated_failure_two_attempts():
    # REPEATED_FAILURE 语义归 E6;reason_code 复用 BUDGET_EXHAUSTED(冻结枚举,用户裁决)
    d = decide(resolve_ready(resolution_attempts=2))
    assert d.branch == Branch.ESCALATE
    assert d.reason_code == ReasonCode.BUDGET_EXHAUSTED


# ---------------------------------------------------------------- L3 能力


def test_e7_critical_unavailable():
    d = decide(
        make_state(
            category="ACCOUNT_AUTH",
            checklist={"auth_account_status": "UNAVAILABLE", "auth_kb_guidance": "PENDING"},
        )
    )
    assert d.branch == Branch.ESCALATE
    assert d.reason_code == ReasonCode.TOOL_UNAVAILABLE


def test_e8_contradictions_nonempty():
    d = decide(
        make_state(
            contradictions=[
                {"check_id": "groups_vs_entitlements", "description": "目录与权限系统矛盾"}
            ]
        )
    )
    assert d.branch == Branch.ESCALATE
    assert d.reason_code == ReasonCode.UNRESOLVED_CONTRADICTION


def test_e9_out_of_scope_redirects_without_ticket():
    d = decide(make_state(category="OUT_OF_SCOPE_NON_IT"))
    assert d.branch == Branch.REDIRECT
    assert d.reason_code is None


# ---------------------------------------------------------------- L4 R 门


def test_r1_fail_category_not_auto_resolvable():
    # critical 满足、假设唯一,但 ACCESS_REQUEST ∉ AUTO_RESOLVABLE → 无 ASK/INVESTIGATE 可走 → L5
    d = decide(
        make_state(
            category="ACCESS_REQUEST",
            checklist={"access_entitlements": "SATISFIED", "access_kb_guidance": "SATISFIED"},
            hypotheses=[{"id": "h1", "text": "needs approval", "status": "SUPPORTED"}],
        )
    )
    assert d.gates["R1"] == "FAIL"
    assert d.branch == Branch.ESCALATE
    assert d.reason_code == ReasonCode.LOW_CONFIDENCE


def test_r2_fail_critical_pending_goes_investigate():
    d = decide(resolve_ready(checklist={"auth_account_status": "PENDING", "auth_kb_guidance": "SATISFIED"}))
    assert d.gates["R2"] == "FAIL"
    assert d.branch == Branch.INVESTIGATE


def test_r3_fail_no_supported_hypothesis():
    d = decide(resolve_ready(hypotheses=[]))
    assert d.gates["R3"] == "FAIL"
    assert d.branch != Branch.RESOLVE


def test_r3_fail_competing_hypotheses():
    d = decide(
        resolve_ready(
            hypotheses=[
                {"id": "h1", "text": "locked", "status": "SUPPORTED"},
                {"id": "h2", "text": "expired password", "status": "SUPPORTED"},
            ]
        )
    )
    assert d.gates["R3"] == "FAIL"
    assert d.branch != Branch.RESOLVE


def test_r_gates_all_pass_resolves():
    d = decide(resolve_ready())
    assert d.gates == {"R1": "PASS", "R2": "PASS", "R3": "PASS"}
    assert d.branch == Branch.RESOLVE


# ---------------------------------------------------------------- L4/L5 UNKNOWN 路径(P0-2)


def test_unknown_first_pass_asks():
    # "电脑坏了":R1 FAIL → 存在 QUESTION 型缺口 且 clarify_count<2 → ASK
    d = decide(make_state())  # 默认 UNKNOWN,两个 QUESTION 项 PENDING
    assert d.branch == Branch.ASK


def test_unknown_clarify_capped_escalates_low_confidence():
    d = decide(make_state(clarify_count=2))
    assert d.branch == Branch.ESCALATE
    assert d.reason_code == ReasonCode.LOW_CONFIDENCE


# ---------------------------------------------------------------- L0 生命周期


def test_l0_escalated_phase_observes():
    d = decide(make_state(phase="ESCALATED"))
    assert d.branch == Branch.OBSERVE


def test_l0_expired_pending_action_is_defensive_only():
    # 过期动作视同不存在,继续往下正常裁决(权威检查点在 act 前置校验)
    state = resolve_ready(
        pending_action={
            "action_id": "a1",
            "tool": "send_unlock_verification",
            "args_frozen": {"user_id": "u-alice"},
            "policy_rule_id": "R-UNLOCK-1",
            "idempotency_key": "k1",
            "expires_at": NOW - timedelta(minutes=1),
            "prompt_text": "发送解锁验证邮件?",
        }
    )
    d = decide(state, now=NOW)
    assert d.branch == Branch.RESOLVE


# ---------------------------------------------------------------- confidence(不参与安全判定)


def _kb_evidence(authority="VERIFIED"):
    return {
        "id": "e1",
        "tool": "search_kb",
        "args_hash": "abc",
        "status": "OK",
        "digest": "KB-1001 self-service unlock",
        "authority": authority,
    }


def test_confidence_high_with_verified_kb_and_no_degraded():
    d = decide(resolve_ready(evidence=[_kb_evidence()]))
    assert d.confidence == "HIGH"


def test_confidence_low_when_critical_source_degraded():
    d = decide(
        resolve_ready(evidence=[_kb_evidence()], degraded_sources=["get_account_status"])
    )
    assert d.confidence == "LOW"


def test_confidence_low_without_verified_kb():
    d = decide(resolve_ready(evidence=[_kb_evidence(authority="DRAFT")]))
    assert d.confidence == "LOW"


# ---------------------------------------------------------------- handoff 定级(§7.2 必过项)


def test_handoff_priority_team_medium_is_p3():
    assert assign_priority("TEAM", "MEDIUM") == "P3"


def test_handoff_individual_capped_at_p2():
    # 个人 deadline 上限 P2:INDIVIDUAL 无论多急不产生 P1
    assert assign_priority("INDIVIDUAL", "HIGH") in ("P2", "P3", "P4")
    assert assign_priority("INDIVIDUAL", "HIGH") != "P1"
