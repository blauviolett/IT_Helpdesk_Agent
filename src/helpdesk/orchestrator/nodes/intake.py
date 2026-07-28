"""intake 节点(LLM):问题画像结构化。

- prompt schema 注入 policy.yaml 资源枚举(v3.1 P1-4):requested_resources 值域
  = resources 键 + "other",越出枚举的输出一律折叠为 other。
- 写 issue.*(verbatim 除外,ingress 独占)+ collected.tried_by_user;
  不写 actor / checklist / pending_action(所有权表)。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from helpdesk.config import get_settings, load_policy
from helpdesk.llm import render_prompt
from helpdesk.orchestrator.nodes import recent_messages
from helpdesk.state.models import CaseState, Category, TriedStep


class IntakeOutput(BaseModel):
    category: Category = Category.UNKNOWN
    urgency: Literal["LOW", "MEDIUM", "HIGH"] | None = None
    deadline: str | None = None
    scope: Literal["INDIVIDUAL", "TEAM", "ORG"] | None = None
    onset: str | None = None
    affected_systems: list[str] = Field(default_factory=list)
    requested_resources: list[str] = Field(default_factory=list)
    tried_by_user: list[TriedStep] = Field(default_factory=list)


def run_intake(state: CaseState, ctx: Any) -> None:
    resources = load_policy()["resources"]
    resource_enum = [*resources.keys(), "other"]
    # 枚举连同 aliases 注入(P1-4 匹配面):模型靠别名把口语映射到枚举键
    enum_desc = "; ".join(
        f"{name}(别名:{', '.join(spec.get('aliases', []) or ['—'])})"
        for name, spec in resources.items()
    ) + ";other(不在上述枚举内的资源)"
    prompt = render_prompt(
        "intake",
        verbatim=state.issue.verbatim,
        messages=recent_messages(state),
        categories=", ".join(c.value for c in Category),
        resource_enum=enum_desc,
    )
    out = ctx.llm.complete_structured(
        "intake", prompt, IntakeOutput, tier=get_settings().tier_intake, budget=state.budget
    )

    issue = state.issue
    issue.category = out.category
    issue.urgency = out.urgency
    issue.deadline = out.deadline
    issue.scope = out.scope
    issue.onset = out.onset
    issue.affected_systems = [s.lower() for s in out.affected_systems]
    if out.category is Category.ACCESS_REQUEST:
        issue.requested_resources = [
            r if r in resource_enum else "other" for r in out.requested_resources
        ]
    else:
        issue.requested_resources = []
    state.collected.tried_by_user = list(out.tried_by_user)
