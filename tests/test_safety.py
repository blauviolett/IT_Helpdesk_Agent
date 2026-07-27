"""安全测试。

D1:转人工词表段(v3.1 P0-1a:≥5 正例置位 + ≥3 负例不置位)。
D3:policy deny-by-default 段 + Input Guard 段 + Output Guard 段
(伪造引用两次 → GUARD_FAILED 且 guard_failures=2 落盘)。
D4:一致性检查(确定性,10/10 复现)+ handoff packet 字段 allowlist
(data-platform-approvers 不含设备信息;transcript 永不入包)。
"""

import json

import pytest

from helpdesk import handoff, policy
from helpdesk.guards import (
    consistency_checks,
    detect_human_request,
    detect_security_signal,
    has_question_marker,
    output_guard,
    redact_credentials,
)
from helpdesk.llm.fake import FakeLLM
from helpdesk.orchestrator.runner import Ctx, handle_message
from helpdesk.state.models import (
    Actor,
    Device,
    DiagnosisStep,
    Message,
    ReasonCode,
    make_state,
    mark_user_requested_human,
)
from helpdesk.state.store import SQLiteStore
from helpdesk.trace import Tracer

# ------------------------------------------------- 转人工词表:正例(≥5)


@pytest.mark.parametrize(
    "text",
    [
        "转人工",
        "请帮我转人工吧",
        "我要找个人来处理这个问题",
        "I want to talk to a person",
        "Can I get a human agent",
        "不要机器人",
    ],
)
def test_human_request_positive(text):
    assert detect_human_request(text) is True


def test_positive_sets_ratchet_field():
    state = make_state()
    if detect_human_request("转人工"):
        mark_user_requested_human(state)
    assert state.user_requested_human is True


# ------------------------------------------------- 转人工词表:负例(≥3,必含指定两条)


@pytest.mark.parametrize(
    "text",
    [
        "人工智能真好用",  # v3.1 指定:含"人工"子串但无锚定短语
        "不用转人工",      # v3.1 指定:否定前缀否决
        "别转人工,我再试试",
        "我在研究人工智能的课题",
    ],
)
def test_human_request_negative(text):
    assert detect_human_request(text) is False


# ------------------------------------------------- 共享否定实现的疑问标记(classifier D3 复用)


def test_question_marker_detected():
    assert has_question_marker("好了吗?") is True
    assert has_question_marker("发吧?") is True
    assert has_question_marker("已经好了") is False


# ------------------------------------------------- policy:deny-by-default(D3)


def test_policy_deny_by_default_for_undeclared_action():
    verdict = policy.check("wipe_all_devices")  # 任何未声明 action 一律 DENY
    assert verdict.decision == "DENY"
    assert verdict.rule_id is None


def test_policy_snowflake_prod_requires_human():
    verdict = policy.check("grant_access:snowflake_prod")
    assert verdict.decision == "DENY_REQUIRE_HUMAN"
    assert verdict.queue == "data-platform-approvers"


def test_policy_unlock_allowed_with_confirm():
    verdict = policy.check("send_unlock_verification")
    assert verdict.decision == "ALLOW"
    assert verdict.requires_confirm is True


def test_policy_grafana_allowed():
    assert policy.check("grant_access:grafana_dashboards").decision == "ALLOW"


# ------------------------------------------------- Input Guard(D3,确定性)


def test_security_signal_positive_and_negated():
    assert detect_security_signal("我好像点了一封钓鱼邮件里的链接") is True
    assert detect_security_signal("my laptop may be hacked") is True
    assert detect_security_signal("不是钓鱼,我就是忘了密码") is False  # 否定前缀共用实现


def test_credential_redaction():
    clean, found = redact_credentials("我的密码是 hunter2,帮我看看")
    assert found is True
    assert "hunter2" not in clean
    clean, found = redact_credentials("Okta 登录一直转圈")
    assert found is False
    assert clean == "Okta 登录一直转圈"


def test_security_signal_lifts_straight_to_escalate(tmp_path):
    ctx = _ctx(tmp_path)
    state = handle_message("我收到一封钓鱼邮件,还输入了账号", ctx=ctx)
    assert state.escalation.required is True
    assert state.escalation.reason_code is ReasonCode.SECURITY
    assert ctx.llm.calls == []  # 不做诊断、不给建议:未调用任何 LLM 节点


# ------------------------------------------------- Output Guard(D3,确定性)

_KB_STATUS = {"KB-1001": "VERIFIED", "KB-1005": "DRAFT"}


def _guard_state():
    return make_state(
        evidence=[
            {
                "id": "e1",
                "tool": "search_kb",
                "args_hash": "x",
                "status": "OK",
                "digest": "KB-1001 [VERIFIED] Okta lockout; KB-1005 [DRAFT] Grafana access",
                "source_ref": "KB-1001",
                "authority": "VERIFIED",
            },
            {
                "id": "e2",
                "tool": "check_service_status",
                "args_hash": "y",
                "status": "OK",
                "digest": "okta operational",
            },
        ]
    )


def test_output_guard_passes_verified_kb_and_generic_evidence():
    steps = [
        DiagnosisStep(text="按 KB 步骤解锁", citation="KB-1001", citation_kind="KB"),
        DiagnosisStep(text="服务状态正常,排除事故", citation="e2", citation_kind="GENERIC"),
        DiagnosisStep(text="不行再重启浏览器", citation=None, citation_kind="GENERIC"),
    ]
    assert output_guard(_guard_state(), steps, kb_status=_KB_STATUS) == []


def test_output_guard_blocks_fabricated_citation():
    steps = [DiagnosisStep(text="做点什么", citation="KB-9999", citation_kind="KB")]
    violations = output_guard(_guard_state(), steps, kb_status=_KB_STATUS)
    assert any("KB-9999" in v for v in violations)


def test_output_guard_blocks_non_verified_kb_citation():
    steps = [DiagnosisStep(text="按草稿操作", citation="KB-1005", citation_kind="KB")]
    violations = output_guard(_guard_state(), steps, kb_status=_KB_STATUS)
    assert any("KB-1005" in v and "VERIFIED" in v for v in violations)


def test_output_guard_generic_steps_alone_cannot_resolve():
    steps = [DiagnosisStep(text="重启试试", citation=None, citation_kind="GENERIC")]
    violations = output_guard(_guard_state(), steps, kb_status=_KB_STATUS)
    assert violations  # v2.1 D10:GENERIC 步骤不能单独构成"已解决"


def test_fabricated_citation_twice_escalates_guard_failed(tmp_path):
    """§7.2 必过项:伪造引用两次 → GUARD_FAILED 且 guard_failures=2 落盘。"""
    bad = {
        "resolution_type": "GUIDED",
        "explanation": "编造的方案",
        "steps": [{"text": "do", "citation": "KB-9999", "citation_kind": "KB"}],
    }
    ctx = _ctx(tmp_path, {"resolve": [bad, dict(bad)]})
    state = make_state(
        category="ACCOUNT_AUTH",
        checklist={
            "auth_account_status": "SATISFIED",
            "auth_kb_guidance": "SATISFIED",
            "auth_service_status": "SATISFIED",
        },
        hypotheses=[{"id": "h1", "text": "账号被锁", "status": "SUPPORTED"}],
        evidence=[
            {
                "id": "e1",
                "tool": "search_kb",
                "args_hash": "x",
                "status": "OK",
                "digest": "KB-1001 [VERIFIED] Okta lockout",
                "source_ref": "KB-1001",
                "authority": "VERIFIED",
            }
        ],
    )
    ctx.store.save(state)
    s = handle_message("我又试了一遍,麻烦给个方案", ctx=ctx, case_id=state.case_id)
    assert s.guard_failures == 2  # 节点内重试 1 次后仍失败
    assert s.escalation.required is True
    assert s.escalation.reason_code is ReasonCode.GUARD_FAILED  # E10
    assert s.diagnosis.steps == []  # 不落任何不可信引用
    assert s.resolution_attempts == 1  # guard 节点内重试不计数
    assert ctx.store.get(s.case_id).guard_failures == 2  # 落盘


# ------------------------------------------------- 一致性检查(D4,确定性,非 LLM)


def _entitlements_evidence(digest):
    return {
        "id": "e9",
        "tool": "get_entitlements",
        "args_hash": "z",
        "status": "OK",
        "digest": digest,
    }


def test_consistency_check_reproducible_10_of_10():
    # fixture 埋点:u-eve 在 grafana-editors 组,权限视图却只有 grafana:viewer
    state = make_state(
        actor=Actor(user_id="u-eve", groups=["eng", "grafana-editors"], profile_loaded=True),
        evidence=[
            _entitlements_evidence(
                "entitlements for u-eve: github:write, grafana:viewer, okta:sso, vpn:standard"
            )
        ],
    )
    results = [consistency_checks(state) for _ in range(10)]
    assert all(
        len(r) == 1 and r[0].check_id == "groups_vs_entitlements" for r in results
    )  # 10/10 复现
    assert len(state.contradictions) == 1  # 整体重算:重入不累积


def test_consistency_check_no_false_positive():
    state = make_state(
        actor=Actor(user_id="u-bob", groups=["eng", "vpn-users"], profile_loaded=True),
        evidence=[
            _entitlements_evidence(
                "entitlements for u-bob: github:write, grafana:viewer, okta:sso, vpn:standard"
            )
        ],
    )
    assert consistency_checks(state) == []
    assert state.contradictions == []


# ------------------------------------------------- handoff packet 字段 allowlist(D4)


def _escalated_state(queue):
    state = make_state(
        category="ACCESS_REQUEST",
        requested_resources=["snowflake_prod"],
        actor=Actor(
            user_id="u-carol",
            display_name="Carol Singh",
            department="Data",
            location="London",
            device=Device(model="MacBook Pro M4", os="macOS 15.5", vpn_client_version="5.3.1"),
            profile_loaded=True,
        ),
    )
    state.escalation.required = True
    state.escalation.reason_code = ReasonCode.POLICY_REQUIRED
    state.escalation.queue = queue
    state.escalation.priority = "P3"
    return state


def test_packet_data_platform_queue_excludes_device_info():
    packet = handoff.build_packet(
        _escalated_state("data-platform-approvers"),
        agent_diagnosis="新人申请 snowflake 生产库权限,策略要求人工审批",
        needed_from_human="请审批人核对身份与用途",
    )
    assert "device_info" not in packet  # §7.2 必过项:该队列不含设备信息
    rendered = json.dumps(packet, ensure_ascii=False)
    assert "MacBook" not in rendered and "macOS" not in rendered
    assert packet["requested_resources"] == ["snowflake_prod"]


def test_packet_it_helpdesk_includes_device_info_but_never_transcript():
    state = _escalated_state("it-helpdesk")
    state.messages.append(Message(turn_id=1, role="user", content="TRANSCRIPT-SENTINEL"))
    packet = handoff.build_packet(state, agent_diagnosis="d", needed_from_human="n")
    assert packet["device_info"]["model"] == "MacBook Pro M4"
    rendered = json.dumps(packet, ensure_ascii=False)
    assert "TRANSCRIPT-SENTINEL" not in rendered  # transcript 不入工单正文
    assert "messages" not in packet


def _ctx(tmp_path, responses=None):
    return Ctx(
        llm=FakeLLM(responses),
        tracer=Tracer(tmp_path / "traces"),
        store=SQLiteStore(tmp_path / "cases.db"),
    )
