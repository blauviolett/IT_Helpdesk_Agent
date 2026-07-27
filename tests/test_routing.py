"""恢复路由 7 条(guide §7.2:6 条恢复路由 + 1 条 E4 runner 异常)
+ classifier 段 ≥12 条断言(≥6 条否定/疑问反例,含 v3.1 P1-5 指定六条)。

全部使用 FakeLLM:转移序列完全确定,CI 无 LLM。
"""

from __future__ import annotations

import json

import pytest

from helpdesk import policy
from helpdesk.guards import detect_human_request
from helpdesk.llm.fake import FakeLLM
from helpdesk.orchestrator import runner
from helpdesk.orchestrator.classifier import (
    classify_confirm,
    classify_escalated,
    classify_verify,
)
from helpdesk.orchestrator.runner import Ctx, handle_message
from helpdesk.state.models import (
    Actor,
    ChecklistStatus,
    Outcome,
    Phase,
    ReasonCode,
    make_state,
)
from helpdesk.state.store import SQLiteStore
from helpdesk.trace import Tracer


def _ctx(tmp_path, responses=None):
    return Ctx(
        llm=FakeLLM(responses),
        tracer=Tracer(tmp_path / "traces"),
        store=SQLiteStore(tmp_path / "cases.db"),
    )


def _trace(tmp_path, case_id):
    path = tmp_path / "traces" / f"{case_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _routed_to(tmp_path, case_id, node):
    return any(
        e["kind"] == "node_start" and e.get("node") == node for e in _trace(tmp_path, case_id)
    )


def _confirm_state(**overrides):
    """AWAITING_CONFIRM 态 + 经真实 FREEZE 冻结的 pending_action(幂等键可复核)。"""
    state = make_state(
        phase=Phase.AWAITING_CONFIRM,
        actor=Actor(user_id="u-alice", profile_loaded=True),
        **overrides,
    )
    assert policy.freeze(state, "send_unlock_verification") is None
    return state


# ================================================= 路由 1:无 case → 建 case + intake


def test_no_case_creates_case_and_routes_intake(tmp_path):
    ctx = _ctx(tmp_path, {"intake": {"category": "OUT_OF_SCOPE_NON_IT"}})
    state = handle_message("帮我订个会议室", ctx=ctx)
    assert state.issue.verbatim == "帮我订个会议室"  # ingress 独占写
    assert ctx.llm.calls[0] == "intake"
    assert ctx.store.get(state.case_id) is not None  # 先落盘再进节点


# ============================= 路由 2:AWAITING_CLARIFY 闭环(P1-3;"我电脑坏了"场景)


def test_clarify_loop_two_rounds_then_capped_escalation(tmp_path):
    ctx = _ctx(tmp_path, {"intake": {"category": "UNKNOWN"}})
    s1 = handle_message("我电脑坏了", ctx=ctx)
    assert s1.phase is Phase.AWAITING_CLARIFY
    assert s1.pending_clarify_item_id == "symptom_detail"
    assert s1.collected.clarify_count == 1
    assert s1.messages[-1].role == "assistant"  # 问题已发出(FakeLLM 空 → hint 模板降级)

    s2 = handle_message("开机就蓝屏,昨天开始的", ctx=ctx, case_id=s1.case_id)
    entry = s2.collected.from_user[0]
    assert entry.item_id == "symptom_detail" and entry.answer == "开机就蓝屏,昨天开始的"
    assert s2.collected.checklist["symptom_detail"] is ChecklistStatus.SATISFIED
    assert s2.pending_clarify_item_id == "affected_system"  # 第二问
    assert s2.collected.clarify_count == 2

    s3 = handle_message("就是我这台笔记本", ctx=ctx, case_id=s1.case_id)
    assert s3.pending_clarify_item_id is None
    assert len(s3.collected.from_user) == 2  # 升级包可携带两个回答
    assert s3.escalation.required is True
    assert s3.escalation.reason_code is ReasonCode.LOW_CONFIDENCE  # 触顶 → L5
    assert _routed_to(tmp_path, s3.case_id, "escalate")


# ================================ 路由 3:AWAITING_CONFIRM 三分(只有显式 YES 消费)


def test_confirm_three_way_only_explicit_yes_consumes(tmp_path):
    # YES → act:四校验通过 → 真执行 → 清动作 → AWAITING_VERIFY(§4.5 ③)
    ctx = _ctx(tmp_path)
    yes = _confirm_state()
    ctx.store.save(yes)
    s = handle_message("发吧", ctx=ctx, case_id=yes.case_id, as_user="u-alice")
    assert _routed_to(tmp_path, yes.case_id, "act")
    assert s.pending_action is None  # 单次消费
    assert s.phase is Phase.AWAITING_VERIFY
    assert sum(1 for e in s.evidence if e.tool == "send_unlock_verification") == 1

    # NO → 记 declined_actions + 作废 → 回 decide(UNKNOWN 缺口 → clarify),不执行
    ctx = _ctx(tmp_path)
    no = _confirm_state()
    ctx.store.save(no)
    s = handle_message("不用发", ctx=ctx, case_id=no.case_id, as_user="u-alice")
    assert s.declined_actions == ["send_unlock_verification"]
    assert s.pending_action is None
    assert not any(e.tool == "send_unlock_verification" for e in s.evidence)
    assert s.phase is Phase.AWAITING_CLARIFY  # decide 已重新裁决,不停留在确认态

    # OTHER("这会做什么?")→ 不触发执行、动作作废、不记 declined
    ctx = _ctx(tmp_path)
    other = _confirm_state()
    ctx.store.save(other)
    s = handle_message("这会做什么?", ctx=ctx, case_id=other.case_id, as_user="u-alice")
    assert s.pending_action is None
    assert s.declined_actions == []
    assert not _routed_to(tmp_path, other.case_id, "act")


# ============================== 路由 4:AWAITING_VERIFY 三态(UNKNOWN 模板追问一次)


def test_verify_three_way_with_single_probe(tmp_path):
    # 成功 → close
    ctx = _ctx(tmp_path)
    ok = make_state(phase=Phase.AWAITING_VERIFY)
    ctx.store.save(ok)
    handle_message("好了,谢谢", ctx=ctx, case_id=ok.case_id)
    assert _routed_to(tmp_path, ok.case_id, "close")

    # 失败 → 回 decide(UNKNOWN 缺口 → clarify)
    ctx = _ctx(tmp_path)
    fail = make_state(phase=Phase.AWAITING_VERIFY)
    ctx.store.save(fail)
    s = handle_message("还没好", ctx=ctx, case_id=fail.case_id)
    assert s.phase is not Phase.AWAITING_VERIFY
    assert s.verify_probe_sent is False

    # 不确定 → 模板追问一次(不调 LLM);第二次仍非成功按失败
    ctx = _ctx(tmp_path)
    unk = make_state(phase=Phase.AWAITING_VERIFY)
    ctx.store.save(unk)
    s = handle_message("我再观察一下", ctx=ctx, case_id=unk.case_id)
    assert s.verify_probe_sent is True
    assert s.phase is Phase.AWAITING_VERIFY  # 等第二次回答
    assert s.messages[-1].role == "assistant"  # 模板追问已发出
    s = handle_message("说不好", ctx=ctx, case_id=unk.case_id)
    assert s.phase is not Phase.AWAITING_VERIFY  # 第二次仍不确定 → 按失败回 decide


# ========================= 路由 5:ESCALATED 二分(RESOLVED → close;OTHER 不重诊断)


def test_escalated_two_way(tmp_path):
    # RESOLVED → close:phase=CLOSED,outcome 保持 ESCALATED(建单时刻归因,冻结契约)
    ctx = _ctx(tmp_path)
    done = make_state(phase=Phase.ESCALATED, outcome=Outcome.ESCALATED)
    ctx.store.save(done)
    s = handle_message("好了,有人帮我处理了", ctx=ctx, case_id=done.case_id)
    assert _routed_to(tmp_path, done.case_id, "close")
    assert s.phase is Phase.CLOSED
    assert s.outcome is Outcome.ESCALATED  # 不被改写为 RESOLVED_BY_AGENT

    ctx = _ctx(tmp_path)
    wait = make_state(phase=Phase.ESCALATED, outcome=Outcome.ESCALATED)
    ctx.store.save(wait)
    s = handle_message("进展如何?", ctx=ctx, case_id=wait.case_id)
    assert s.phase is Phase.ESCALATED  # phase 不变,不重诊断
    assert s.outcome is Outcome.ESCALATED
    assert s.messages[-1].role == "assistant"  # 追加回执(工单评论)
    assert any(e["kind"] == "escalate_followup" for e in _trace(tmp_path, wait.case_id))
    assert not _routed_to(tmp_path, wait.case_id, "intake")


# ================================================= 路由 6:CLOSED → 新问题建新 case


def test_closed_case_message_opens_new_case(tmp_path):
    ctx = _ctx(tmp_path, {"intake": {"category": "NETWORK_VPN"}})
    old = make_state(phase=Phase.CLOSED)
    ctx.store.save(old)
    s = handle_message("VPN 一直连不上", ctx=ctx, case_id=old.case_id)
    assert s.case_id != old.case_id
    assert s.issue.verbatim == "VPN 一直连不上"
    assert ctx.store.get(old.case_id).phase is Phase.CLOSED  # 旧 case 不被改动


# ==================== 路由 7:E4 —— 节点抛异常:回滚 + 直升 escalate + 无堆栈外泄


def test_e4_node_exception_rolls_back_and_escalates(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)

    def bad_intake(state, _ctx):
        # 先污染嵌套字段再抛异常:断言回滚必须完全复原(深拷贝语义)
        state.collected.checklist["symptom_detail"] = ChecklistStatus.SATISFIED
        state.issue.affected_systems.append("corrupted")
        state.collected.tried_by_user.append({"step": "corrupted"})  # type: ignore[arg-type]
        raise RuntimeError("boom-secret-stacktrace")

    monkeypatch.setitem(runner.NODES, "intake", bad_intake)
    state = handle_message("我电脑坏了", ctx=ctx)

    assert state.collected.checklist == {}  # 嵌套字段完全复原
    assert state.issue.affected_systems == []
    assert state.collected.tried_by_user == []
    assert state.escalation.required is True
    assert state.escalation.reason_code is ReasonCode.SYSTEM_ERROR
    assert _routed_to(tmp_path, state.case_id, "escalate")  # 直升,不经 decide
    assert not any(
        e["kind"] == "decision" for e in _trace(tmp_path, state.case_id)
    )
    for message in state.messages:  # 用户可见消息不含堆栈
        assert "boom-secret-stacktrace" not in message.content
        assert "Traceback" not in message.content
    saved = ctx.store.get(state.case_id)
    assert saved.escalation.reason_code is ReasonCode.SYSTEM_ERROR  # 已落盘


# ==================== investigate 再入活锁回归(guide §5 D2 修订 2026-07-27)


def test_investigate_empty_first_batch_with_kb_gap_goes_to_model_batch(tmp_path):
    """代码首批为空但 search_kb 项(critical TOOL)仍 PENDING → 直接进入模型批次,
    而非按"空工具列表"静默终止(否则与 decide 的 INVESTIGATE 分支互等成活锁)。"""
    from helpdesk.orchestrator.nodes.investigate import run_investigate

    ctx = _ctx(
        tmp_path,
        {"investigate": [{"tool_calls": [{"tool": "search_kb", "args": {"query": "okta account locked"}}]}, {}]},
    )
    state = make_state(
        category="ACCOUNT_AUTH",
        checklist={
            "auth_account_status": ChecklistStatus.SATISFIED,
            "auth_kb_guidance": ChecklistStatus.PENDING,
            "auth_service_status": ChecklistStatus.SATISFIED,
        },
    )
    run_investigate(state, ctx)
    assert any(e.tool == "search_kb" for e in state.evidence)
    assert state.collected.checklist["auth_kb_guidance"] is ChecklistStatus.SATISFIED


# ================================================================ classifier 段
# ≥12 条断言,其中 ≥6 条否定/疑问反例;必含:还没好 / 好了吗? / 没好 / 不用发 /
# 发吧? / 人工智能真好用(v3.1 P1-5)。


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("发吧", "YES"),
        ("好的,可以", "YES"),
        ("不用发", "NO"),  # 反例:否定前缀
        ("不要发了,算了", "NO"),
        ("发吧?", "OTHER"),  # 反例:疑问标记否决 YES
        ("这会做什么?", "OTHER"),  # 反例:疑问 → 作废动作方向
    ],
)
def test_classifier_confirm(text, expected):
    assert classify_confirm(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("好了,谢谢", "RESOLVED"),
        ("问题解决了", "RESOLVED"),
        ("还没好", "FAILED"),  # 反例:含"好了"子串但否定优先
        ("没好", "FAILED"),  # 反例
        ("还是不行", "FAILED"),
        ("好了吗?", "UNKNOWN"),  # 反例:疑问标记否决 RESOLVED
        ("我再看看", "UNKNOWN"),  # 词表不确定,无 SMALL → 安全默认
        ("嗯我还没来得及试", "UNKNOWN"),  # 反例:尚未验证 ≠ 失败(真实模型实测回归)
    ],
)
def test_classifier_verify(text, expected):
    assert classify_verify(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("好了", "RESOLVED"),
        ("搞定了,可以关了", "RESOLVED"),
        ("还没有人联系我", "OTHER"),  # 反例:否定前缀
        ("进展如何?", "OTHER"),  # 反例:疑问
    ],
)
def test_classifier_escalated(text, expected):
    assert classify_escalated(text) == expected


def test_classifier_wordlist_shares_negation_with_human_request():
    assert detect_human_request("人工智能真好用") is False  # 反例(P1-5 指定)


def test_classifier_small_fallback_and_safe_default():
    # 词表不确定 → SMALL 兜底;输出可解析则采纳
    assert classify_verify("嗯嗯", llm=FakeLLM({"classifier": "RESOLVED"})) == "RESOLVED"
    # SMALL 输出越界 → 落安全默认(confirm → OTHER:作废动作方向)
    assert classify_confirm("嗯嗯", llm=FakeLLM({"classifier": "GARBAGE"})) == "OTHER"
