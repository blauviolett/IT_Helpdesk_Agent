"""唯一工具入口(guide §2):三层门控 + 运行时 actor 注入 + 去重 + 重试 + 落账。

- 三层门控(v2.1 C4,D3 接线):
  ① 阶段门:只读工具仅限调查阶段(INTAKE/INVESTIGATING/AWAITING_CLARIFY,
     后者覆盖澄清恢复后立即进入的 investigate);写工具仅限 AWAITING_CONFIRM
     (只可能来自 act handler,D4);
  ② 策略门:非只读工具执行前 policy.check 必须 ALLOW(deny-by-default);
  ③ 运行门:预算拦截(触顶拒执行,不落 evidence)+ actor 注入 + 去重 + 重试。
- 任何工具签名(模型可见 params schema)不得出现 target_user / user_id;
  actor 由本层注入,模型无法指定操作对象(test_tools 静态检查钉死)。
- 写工具(send_unlock_verification / create_escalation_ticket)D4 落地,
  read_only=False,永不出现在 model_tool_specs() —— 写路径只走三段协议的 act。
- checklist 置位按 guide §2.2 四态映射:OK/EMPTY/DEGRADED → SATISFIED;
  ERROR → UNAVAILABLE(仅覆盖 PENDING,不抹掉已有成功证据)。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, ValidationError, field_validator

from helpdesk import policy
from helpdesk.config import load_categories
from helpdesk.orchestrator.budget import over_budget
from helpdesk.state.models import (
    Actor,
    CaseState,
    ChecklistStatus,
    EvidenceItem,
    Phase,
    append_evidence,
)
from helpdesk.tools import base
from helpdesk.tools.adapters import directory, idp, kb, status
from helpdesk.tools.base import ToolResult, ToolStatus, error_result, hash_args

# 阶段门(①):read_only → 调查阶段;写工具 → 仅 AWAITING_CONFIRM(act,D4)
_READ_PHASES = frozenset({Phase.INTAKE, Phase.INVESTIGATING, Phase.AWAITING_CLARIFY})
_WRITE_PHASES = frozenset({Phase.AWAITING_CONFIRM})


class NoParams(BaseModel):
    pass


class SearchKBParams(BaseModel):
    query: str
    applies_to: list[str] = []

    @field_validator("applies_to", mode="before")
    @classmethod
    def _coerce_list(cls, v: Any) -> Any:
        # 模型边界宽容:实测 qwen 会把单标签传成字符串,包一层即等价
        return [v] if isinstance(v, str) else v


class ServiceParams(BaseModel):
    service: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    params: type[BaseModel]
    run: Callable[[Actor, Any, base.ToolRuntime], ToolResult]
    read_only: bool = True


TOOLS: dict[str, ToolSpec] = {
    s.name: s
    for s in (
        ToolSpec(
            "get_user_profile",
            "当前用户的目录画像(部门/设备/组)",
            NoParams,
            directory.get_user_profile,
        ),
        ToolSpec(
            "get_account_status",
            "当前用户的 IdP(Okta)账号状态",
            NoParams,
            idp.get_account_status,
        ),
        ToolSpec(
            "get_entitlements",
            "当前用户的权限清单(entitlements 视图)",
            NoParams,
            directory.get_entitlements,
        ),
        ToolSpec(
            "search_kb",
            "BM25 检索知识库;query 为检索词,applies_to 为可选标签过滤",
            SearchKBParams,
            kb.search_kb,
        ),
        ToolSpec(
            "check_service_status",
            "查询服务状态页;service 缺省时返回全部服务摘要",
            ServiceParams,
            status.check_service_status,
        ),
        ToolSpec(
            "get_recent_changes",
            "查询最近变更记录;service 为可选过滤",
            ServiceParams,
            status.get_recent_changes,
        ),
    )
}


def model_tool_specs() -> list[str]:
    """模型可见工具列表:仅只读工具;写工具永不出现(冻结契约)。"""
    lines = []
    for spec in TOOLS.values():
        if not spec.read_only:
            continue
        params = ", ".join(
            name + ("" if f.is_required() else "?")
            for name, f in spec.params.model_fields.items()
        )
        lines.append(f"{spec.name}({params}) — {spec.description}")
    return lines


def execute(state: CaseState, tool_name: str, args: dict[str, Any] | None, ctx: Any) -> ToolResult:
    """执行一次工具调用:注入 actor、去重、重试,写 evidence / checklist / budget。

    ctx 只需提供 .runtime(ToolRuntime)与 .tracer(Tracer)。模型给出的非法工具名
    或非法 args 走 ERROR 信封,不抛异常(不触发 E4)。
    """
    tracer, runtime = ctx.tracer, ctx.runtime
    spec = TOOLS.get(tool_name)
    if spec is None:
        state.budget.tool_calls += 1  # 模型给错名也计入预算,防无谓循环
        tracer.event(state.case_id, "tool_call", tool=tool_name, status="ERROR", reason="unknown_tool")
        return error_result(f"tool not available: {tool_name}", "unknown_tool")
    # ① 阶段门
    allowed = _READ_PHASES if spec.read_only else _WRITE_PHASES
    if state.phase not in allowed:
        tracer.event(state.case_id, "tool_call", tool=tool_name, status="ERROR", reason="stage_blocked")
        return error_result(f"{tool_name} not allowed in phase {state.phase.value}", "stage_blocked")
    # ② 策略门(只对写动作;deny-by-default)
    if not spec.read_only and policy.check(tool_name).decision != "ALLOW":
        tracer.event(state.case_id, "tool_call", tool=tool_name, status="ERROR", reason="policy_denied")
        return error_result(f"{tool_name} denied by policy", "policy_denied")
    # ③ 运行门:预算拦截(超限后果由 decide E5 裁决,此处只拒执行)
    if (dim := over_budget(state.budget)) is not None:
        tracer.event(state.case_id, "tool_call", tool=tool_name, status="ERROR", reason="budget_exhausted", dimension=dim)
        return error_result(f"budget exhausted ({dim})", "budget_exhausted")
    try:
        params = spec.params.model_validate(args or {})
    except ValidationError:
        state.budget.tool_calls += 1
        tracer.event(state.case_id, "tool_call", tool=tool_name, status="ERROR", reason="invalid_args")
        return error_result(f"invalid args for {tool_name}", "invalid_args")

    canonical = params.model_dump(mode="json")
    args_hash = hash_args(tool_name, canonical)
    dup = next(
        (e for e in state.evidence if e.tool == tool_name and e.args_hash == args_hash and e.status != "ERROR"),
        None,
    )
    if dup is not None:
        tracer.event(state.case_id, "tool_call", tool=tool_name, args=canonical, args_hash=args_hash, status=dup.status, dedup=True)
        return ToolResult(status=ToolStatus(dup.status), digest=dup.digest, source_ref=dup.source_ref, authority=dup.authority)

    start = time.perf_counter()
    if tool_name in runtime.fail_tools:
        result = error_result(f"{tool_name} unavailable (injected failure)", "injected_failure")
    else:
        result = _run_with_retry(spec, state.actor, params, runtime)
    if tool_name in runtime.degraded_tools and result.status is ToolStatus.OK:
        result.status = ToolStatus.DEGRADED
        result.digest += " [degraded source]"
    result.latency_ms = int((time.perf_counter() - start) * 1000)

    if result.status is ToolStatus.DEGRADED and tool_name not in state.degraded_sources:
        state.degraded_sources.append(tool_name)
    append_evidence(
        state,
        EvidenceItem(
            id=f"e{len(state.evidence) + 1}",
            tool=tool_name,
            args_hash=args_hash,
            status=result.status.value,
            digest=result.digest,
            source_ref=result.source_ref,
            authority=result.authority,
            latency_ms=result.latency_ms,
        ),
        writer="tool_layer",
    )
    state.budget.tool_calls += 1
    _apply_checklist(state, tool_name, result.status)
    tracer.event(state.case_id, "tool_call", tool=tool_name, args=canonical, args_hash=args_hash, status=result.status.value, latency_ms=result.latency_ms, dedup=False)
    return result


def _run_with_retry(spec: ToolSpec, actor: Actor, params: BaseModel, runtime: base.ToolRuntime) -> ToolResult:
    """adapter 异常重试 1 次;仍失败 → ERROR 信封(不外泄堆栈)。"""
    for attempt in (1, 2):
        try:
            return spec.run(actor, params, runtime)
        except Exception as exc:  # noqa: BLE001 — 工具故障必须收敛为四态信封
            if attempt == 2:
                return error_result(f"{spec.name} failed after retry", type(exc).__name__)
    raise AssertionError("unreachable")


def _apply_checklist(state: CaseState, tool_name: str, result_status: ToolStatus) -> None:
    """guide §2.2 四态映射。ERROR 只覆盖 PENDING:成功证据不被后续失败调用抹掉。"""
    items = load_categories()["categories"][state.issue.category.value]["checklist"]
    for item in items:
        if item.get("tool") != tool_name:
            continue
        item_id = item["item_id"]
        if result_status in (ToolStatus.OK, ToolStatus.EMPTY, ToolStatus.DEGRADED):
            state.collected.checklist[item_id] = ChecklistStatus.SATISFIED
        elif state.collected.checklist.get(item_id, ChecklistStatus.PENDING) is ChecklistStatus.PENDING:
            state.collected.checklist[item_id] = ChecklistStatus.UNAVAILABLE
