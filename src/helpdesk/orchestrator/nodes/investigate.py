"""investigate 节点(LLM):多批次调查回路(guide §2 / §5 D2)。

- 首批由 checklist 生成(代码):PENDING 的 TOOL 项中参数可由代码确定者。
  search_kb 的 query 是语义参数,无法由代码确定 → 从第 2 批起由模型构造
  (这也正是 D2 验收"第二批工具集依赖第一批结果"的承载点)。
- 硬上限 3 批;终止条件仅 3 条:空工具列表 / critical 全 SATISFIED / 预算触顶。
  "空工具列表"终止只作用于模型批次(guide §5 D2,2026-07-27 修订):代码首批
  为空但仍有 critical TOOL 缺口(即 search_kb 项 PENDING)时直接进入模型批次,
  否则 decide 的 INVESTIGATE 分支与本节点互相等待成活锁。
- checklist 置位按 §2.2 四态映射,由工具层(registry)完成,本节点不直接写。
- hypotheses 由本节点独占写(每次模型调用整体替换);issue.category 可修正。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from helpdesk.config import get_settings, load_categories, load_limits
from helpdesk.guards import consistency_checks
from helpdesk.llm import render_prompt
from helpdesk.orchestrator import handlers
from helpdesk.orchestrator.budget import over_budget
from helpdesk.state.models import (
    CaseState,
    Category,
    ChecklistStatus,
    Hypothesis,
    HypothesisStatus,
)
from helpdesk.tools import registry

_MAX_BATCHES = 3  # 硬上限,冻结


class ToolCallRequest(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class HypothesisOut(BaseModel):
    id: str
    text: str
    status: HypothesisStatus = HypothesisStatus.OPEN
    supporting: list[str] = Field(default_factory=list)
    refuting: list[str] = Field(default_factory=list)


class InvestigateOutput(BaseModel):
    hypotheses: list[HypothesisOut] = Field(default_factory=list)
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    category: Category | None = None  # 修正归类(v2.1 既有权限),否则 null


def run_investigate(state: CaseState, ctx: Any) -> None:
    limits = load_limits()
    calls = _first_batch(state)
    for batch_no in range(1, _MAX_BATCHES + 1):
        if not calls and not (batch_no == 1 and _critical_tool_gap(state)):
            break  # 终止 1:空工具列表(仅模型批次;代码首批空 + 缺口 → 交模型)
        if over_budget(state.budget, limits) is not None:
            break  # 终止 3:预算触顶
        ctx.tracer.event(
            state.case_id,
            "investigate_batch",
            batch=batch_no,
            calls=[{"tool": c.tool, "args": c.args} for c in calls],
        )
        for call in calls:
            registry.execute(state, call.tool, call.args, ctx)
        out = ctx.llm.complete_structured(
            "investigate", _prompt(state), InvestigateOutput,
            tier=get_settings().tier_investigate, budget=state.budget,
        )
        _apply(state, out)
        calls = out.tool_calls
        if _critical_satisfied(state):
            break  # 终止 2:critical 全 SATISFIED
    # 调查出口:跨源一致性检查(确定性代码,D4);E8 由 decide 消费
    consistency_checks(state)


def _first_batch(state: CaseState) -> list[ToolCallRequest]:
    """首批(代码生成):PENDING 的 TOOL 项,参数可由代码确定;search_kb 留给模型批次。"""
    items = _items(state)
    calls: list[ToolCallRequest] = []
    for item in items:
        if item["source"] != "TOOL" or item["tool"] == "search_kb":
            continue
        if state.collected.checklist.get(item["item_id"]) != ChecklistStatus.PENDING:
            continue
        args: dict[str, Any] = {}
        if item["tool"] in ("check_service_status", "get_recent_changes") and state.issue.affected_systems:
            args = {"service": state.issue.affected_systems[0]}
        calls.append(ToolCallRequest(tool=item["tool"], args=args))
    return calls


def _apply(state: CaseState, out: InvestigateOutput) -> None:
    state.hypotheses = [Hypothesis(**h.model_dump()) for h in out.hypotheses]
    if out.category is not None and out.category is not state.issue.category:
        state.issue.category = out.category
        handlers.sync_checklist(state)  # 按新类目 checklist 生成缺口(P0-2 修正路径)


def _prompt(state: CaseState) -> str:
    issue = state.issue
    evidence = "\n".join(
        f"[{e.id}] {e.tool} ({e.status}): {e.digest}" for e in state.evidence
    ) or "(无)"
    hypotheses = "\n".join(
        f"[{h.id}] ({h.status}) {h.text}" for h in state.hypotheses
    ) or "(无)"
    return render_prompt(
        "investigate",
        issue=(
            f"category={issue.category.value}; verbatim={issue.verbatim!r}; "
            f"affected_systems={issue.affected_systems}; urgency={issue.urgency}; "
            f"scope={issue.scope}; onset={issue.onset}"
        ),
        checklist=json.dumps(
            {k: v.value for k, v in state.collected.checklist.items()}, ensure_ascii=False
        ),
        evidence=evidence,
        hypotheses=hypotheses,
        tools="\n".join(registry.model_tool_specs()),
    )


def _items(state: CaseState) -> list[dict[str, Any]]:
    return load_categories()["categories"][state.issue.category.value]["checklist"]


def _critical_tool_gap(state: CaseState) -> bool:
    """与 decide L4 tool_gap 同口径:critical TOOL 项仍 PENDING。"""
    return any(
        i["source"] == "TOOL"
        and i["critical"]
        and state.collected.checklist.get(i["item_id"], ChecklistStatus.PENDING)
        is ChecklistStatus.PENDING
        for i in _items(state)
    )


def _critical_satisfied(state: CaseState) -> bool:
    return all(
        state.collected.checklist.get(i["item_id"]) is ChecklistStatus.SATISFIED
        for i in _items(state)
        if i["critical"]
    )
