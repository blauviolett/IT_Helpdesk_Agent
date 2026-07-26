"""工具层测试(guide §7.2):四态语义(EMPTY ≠ ERROR 可区分)、digest 确定性、
签名无 target_user 静态检查、故障注入;另覆盖去重与 DEPRECATED 不可引用。
"""

from types import SimpleNamespace

from helpdesk.state.models import Actor, ChecklistStatus, make_state, write_actor
from helpdesk.tools import registry
from helpdesk.tools.base import ToolRuntime, ToolStatus
from helpdesk.trace import Tracer


def _ctx(tmp_path, **runtime_kwargs):
    return SimpleNamespace(runtime=ToolRuntime(**runtime_kwargs), tracer=Tracer(tmp_path))


def _state(category="ACCOUNT_AUTH", user_id="u-alice"):
    state = make_state(category=category)
    write_actor(state, Actor(user_id=user_id), writer="runtime")
    return state


# ---------------------------------------------------------------- 四态语义


def test_ok_result_satisfies_checklist(tmp_path):
    state = _state()
    result = registry.execute(state, "get_account_status", {}, _ctx(tmp_path))
    assert result.status is ToolStatus.OK
    assert "LOCKED_OUT" in result.digest
    assert state.collected.checklist["auth_account_status"] is ChecklistStatus.SATISFIED
    assert state.budget.tool_calls == 1
    assert len(state.evidence) == 1


def test_empty_is_valid_evidence_not_error(tmp_path):
    # status_a 的 change_log 为空:EMPTY("查过且没有"是有效证据)→ SATISFIED
    state = _state(category="APP_PERFORMANCE")
    result = registry.execute(state, "get_recent_changes", {"service": "okta"}, _ctx(tmp_path))
    assert result.status is ToolStatus.EMPTY
    assert result.error is None
    assert state.collected.checklist["app_recent_changes"] is ChecklistStatus.SATISFIED


def test_error_injection_marks_unavailable(tmp_path):
    # 同一调用注入故障:ERROR → UNAVAILABLE(信息缺口 → E7),与 EMPTY 语义可区分
    state = _state(category="APP_PERFORMANCE")
    ctx = _ctx(tmp_path, fail_tools={"get_recent_changes"})
    result = registry.execute(state, "get_recent_changes", {"service": "okta"}, ctx)
    assert result.status is ToolStatus.ERROR
    assert result.error is not None and result.error.code == "injected_failure"
    assert state.collected.checklist["app_recent_changes"] is ChecklistStatus.UNAVAILABLE


def test_degraded_counts_source_and_satisfies(tmp_path):
    state = _state(category="APP_PERFORMANCE")
    ctx = _ctx(tmp_path, degraded_tools={"check_service_status"})
    result = registry.execute(state, "check_service_status", {"service": "okta"}, ctx)
    assert result.status is ToolStatus.DEGRADED
    assert "check_service_status" in state.degraded_sources
    assert state.collected.checklist["app_service_status"] is ChecklistStatus.SATISFIED


def test_unknown_tool_returns_error_envelope(tmp_path):
    # 模型幻觉工具名不抛异常(不触发 E4),收敛为 ERROR 信封
    result = registry.execute(_state(), "page_oncall", {}, _ctx(tmp_path))
    assert result.status is ToolStatus.ERROR


# ---------------------------------------------------------------- digest 确定性与去重


def test_digest_deterministic_across_runs(tmp_path):
    args = {"query": "okta account locked unlock", "applies_to": ["okta"]}
    r1 = registry.execute(_state(), "search_kb", args, _ctx(tmp_path))
    r2 = registry.execute(_state(), "search_kb", args, _ctx(tmp_path))
    assert r1.digest == r2.digest
    r3 = registry.execute(_state(), "get_user_profile", {}, _ctx(tmp_path))
    r4 = registry.execute(_state(), "get_user_profile", {}, _ctx(tmp_path))
    assert r3.digest == r4.digest


def test_dedup_same_args_executes_once(tmp_path):
    state = _state()
    ctx = _ctx(tmp_path)
    registry.execute(state, "get_account_status", {}, ctx)
    result = registry.execute(state, "get_account_status", {}, ctx)
    assert result.status is ToolStatus.OK  # 去重命中仍返回可用结果
    assert state.budget.tool_calls == 1
    assert len(state.evidence) == 1


# ---------------------------------------------------------------- 签名静态检查(冻结契约)


def test_no_tool_signature_has_target_user():
    for spec in registry.TOOLS.values():
        fields = set(spec.params.model_fields)
        assert not fields & {"target_user", "user_id"}, spec.name


def test_model_tool_list_is_read_only_six():
    lines = registry.model_tool_specs()
    assert len(lines) == 6
    joined = " ".join(lines)
    assert "send_unlock_verification" not in joined
    assert "create_ticket" not in joined


# ---------------------------------------------------------------- 检索:DEPRECATED / applies_to


def test_deprecated_kb_never_citable(tmp_path):
    result = registry.execute(
        _state(), "search_kb", {"query": "unlock okta account locked portal"}, _ctx(tmp_path)
    )
    kb_ids = {hit["kb_id"] for hit in result.data}
    assert "KB-1004" not in kb_ids  # DEPRECATED 不出现在可引用结果
    assert "KB-1001" in kb_ids


def test_applies_to_hard_filter(tmp_path):
    hit = registry.execute(
        _state(), "search_kb", {"query": "vpn keeps disconnecting"}, _ctx(tmp_path)
    )
    assert hit.status is ToolStatus.OK and hit.source_ref == "KB-1002"
    filtered = registry.execute(
        _state(),
        "search_kb",
        {"query": "vpn keeps disconnecting", "applies_to": ["okta"]},
        _ctx(tmp_path),
    )
    assert filtered.status is ToolStatus.EMPTY


# ---------------------------------------------------------------- fixture 开关(--fixture)


def test_fixture_switch_changes_status_branch(tmp_path):
    a = registry.execute(
        _state(), "check_service_status", {"service": "salesforce"}, _ctx(tmp_path)
    )
    assert a.status is ToolStatus.OK and "operational" in a.digest
    b = registry.execute(
        _state(),
        "check_service_status",
        {"service": "salesforce"},
        _ctx(tmp_path, fixture="status_b"),
    )
    assert "degraded" in b.digest and "INC-4021" in b.digest
