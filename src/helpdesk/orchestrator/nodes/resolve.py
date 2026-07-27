"""resolve 节点(LLM):方案生成 + 引用锚定 + Output Guard 出口接线(guide §5 D3)。

- diagnosis 由本节点独占写;guard_failures 由本节点出口 guard 独占写(E10 = ≥2)。
- Guard 失败控制流(冻结):带违规信息节点内重试 1 次 → 仍失败 guard_failures=2,
  不落任何不可信诊断(diagnosis 清空 → transition 不进等待态)→ 回 decide → E10。
  节点内重试不计 resolution_attempts(由 runner 在 RESOLVE 分支计数)。
- 三段协议 ① PROPOSE:resolution_type=ACTION 时只输出 intent/rationale;
  args 由 ActionBuilder 代码冻结(D4),模型侧任何参数都不被消费。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from helpdesk.guards import output_guard
from helpdesk.llm import render_prompt
from helpdesk.orchestrator.nodes import say
from helpdesk.state.models import (
    CaseState,
    Confidence,
    Diagnosis,
    DiagnosisStep,
    ResolutionType,
)


class StepOut(BaseModel):
    text: str
    citation: str | None = None
    citation_kind: Literal["KB", "GENERIC"] = "GENERIC"


class ResolveOutput(BaseModel):
    root_cause: str = ""
    explanation: str = ""
    resolution_type: ResolutionType | None = None
    steps: list[StepOut] = Field(default_factory=list)
    # PROPOSE(§4.5 ①):ACTION 时只提意图;target 恒为发起用户,由运行时注入
    intent: str | None = None
    rationale: str | None = None


def run_resolve(state: CaseState, ctx: Any) -> None:
    previous_steps = _render_steps(state.diagnosis.steps)
    failure_feedback = (
        _last_user_text(state) if state.diagnosis.steps else "(无)"
    )  # P1-7:仅在存在上一次方案时,最后一条用户消息即 verify 失败反馈
    guard_feedback = "(无)"

    for _attempt in (1, 2):
        prompt = _prompt(state, previous_steps, failure_feedback, guard_feedback)
        out = ctx.llm.complete_structured("resolve", prompt, ResolveOutput, budget=state.budget)
        steps = [DiagnosisStep(**s.model_dump()) for s in out.steps]
        violations = output_guard(state, steps)
        if not violations:
            _apply(state, out, steps)
            return
        state.guard_failures += 1  # 出口 guard 独占写;E10 = ≥2
        guard_feedback = ";".join(violations)
        ctx.tracer.event(state.case_id, "output_guard_failed", violations=violations)
    # 两次均未通过:不落不可信诊断,回 decide(guard_failures=2 → E10)
    state.diagnosis = Diagnosis()


def _apply(state: CaseState, out: ResolveOutput, steps: list[DiagnosisStep]) -> None:
    state.diagnosis = Diagnosis(
        root_cause=out.root_cause or None,
        explanation=out.explanation or None,
        resolution_type=out.resolution_type,
        steps=steps,
    )
    say(state, _render_reply(state, out, steps))


def _render_reply(state: CaseState, out: ResolveOutput, steps: list[DiagnosisStep]) -> str:
    lines: list[str] = []
    if out.explanation:
        lines.append(out.explanation)
    for i, step in enumerate(steps, 1):
        cite = f"(依据 {step.citation})" if step.citation else ""
        lines.append(f"{i}. {step.text}{cite}")
    if out.resolution_type is ResolutionType.ACTION:
        lines.append("这一步需要系统侧操作,征得你的确认后才会执行。")
    elif out.resolution_type is ResolutionType.GUIDED:
        lines.append("请按上述步骤操作,然后告诉我是否解决。")
    if state.confidence is Confidence.LOW:
        # v2.1 D3:LOW → 显式声明不确定 + 强制附转人工入口
        lines.append("说明:此方案的证据支撑有限,我不完全确定。如需人工协助,随时说\"转人工\"。")
    return "\n".join(lines)


def _prompt(
    state: CaseState, previous_steps: str, failure_feedback: str, guard_feedback: str
) -> str:
    issue = state.issue
    evidence = "\n".join(
        f"[{e.id}] {e.tool} ({e.status}, {e.authority}): {e.digest}" for e in state.evidence
    ) or "(无)"
    hypotheses = "\n".join(
        f"[{h.id}] ({h.status}) {h.text}" for h in state.hypotheses
    ) or "(无)"
    tried = "\n".join(
        f"- {t.step}: {t.outcome or '结果未知'}" for t in state.collected.tried_by_user
    ) or "(无)"
    return render_prompt(
        "resolve",
        issue=(
            f"category={issue.category.value}; verbatim={issue.verbatim!r}; "
            f"affected_systems={issue.affected_systems}; urgency={issue.urgency}"
        ),
        evidence=evidence,
        hypotheses=hypotheses,
        tried_by_user=tried,
        previous_steps=previous_steps,
        failure_feedback=failure_feedback,
        declined_actions=", ".join(state.declined_actions) or "(无)",
        guard_feedback=guard_feedback,
    )


def _render_steps(steps: list[DiagnosisStep]) -> str:
    return "\n".join(f"{i}. {s.text}" for i, s in enumerate(steps, 1)) or "(无)"


def _last_user_text(state: CaseState) -> str:
    for message in reversed(state.messages):
        if message.role == "user":
            return message.content
    return "(无)"
