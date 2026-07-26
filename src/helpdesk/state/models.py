"""CaseState 单一事实源(guide §3 字段表冻结版)+ make_state() 测试工厂。

字段单一所有权:每个字段只有一个写入者(见 guide §3 表)。其中三个最危险字段
(actor / evidence / pending_action)配运行时 assert —— 本文件提供唯一合法的
写入口(write_actor / append_evidence / set_pending_action),runner 与工具层
只能经由它们写入(D2 接线)。

6 个新字段(v3 三个 + v3.1 三个,全部代码独占写):
  guard_failures / declined_actions / verify_probe_sent
  user_requested_human / pending_clarify_item_id / issue.requested_resources
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from helpdesk.config import load_categories

# ---------------------------------------------------------------- 枚举


class Phase(StrEnum):
    INTAKE = "INTAKE"
    INVESTIGATING = "INVESTIGATING"
    AWAITING_CLARIFY = "AWAITING_CLARIFY"
    AWAITING_CONFIRM = "AWAITING_CONFIRM"
    AWAITING_VERIFY = "AWAITING_VERIFY"
    ESCALATED = "ESCALATED"
    CLOSED = "CLOSED"


class Outcome(StrEnum):
    RESOLVED_BY_AGENT = "RESOLVED_BY_AGENT"
    INFORMED_KNOWN_INCIDENT = "INFORMED_KNOWN_INCIDENT"
    ESCALATED = "ESCALATED"
    REDIRECTED = "REDIRECTED"


class Category(StrEnum):
    ACCOUNT_AUTH = "ACCOUNT_AUTH"
    APP_PERFORMANCE = "APP_PERFORMANCE"
    NETWORK_VPN = "NETWORK_VPN"
    ACCESS_REQUEST = "ACCESS_REQUEST"
    MULTI_SYSTEM = "MULTI_SYSTEM"
    SECURITY = "SECURITY"
    OUT_OF_SCOPE_NON_IT = "OUT_OF_SCOPE_NON_IT"
    UNKNOWN = "UNKNOWN"


class ChecklistStatus(StrEnum):
    PENDING = "PENDING"
    SATISFIED = "SATISFIED"
    UNAVAILABLE = "UNAVAILABLE"


class ReasonCode(StrEnum):
    """冻结枚举(guide §3),9 值。E6 复用 BUDGET_EXHAUSTED(用户裁决 2026-07-26)。"""

    SECURITY = "SECURITY"
    POLICY_REQUIRED = "POLICY_REQUIRED"
    USER_REQUESTED = "USER_REQUESTED"
    GUARD_FAILED = "GUARD_FAILED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    UNRESOLVED_CONTRADICTION = "UNRESOLVED_CONTRADICTION"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    SYSTEM_ERROR = "SYSTEM_ERROR"


class Confidence(StrEnum):
    HIGH = "HIGH"
    LOW = "LOW"


class HypothesisStatus(StrEnum):
    OPEN = "OPEN"
    SUPPORTED = "SUPPORTED"
    REFUTED = "REFUTED"


class Authority(StrEnum):
    VERIFIED = "VERIFIED"
    DRAFT = "DRAFT"
    DEPRECATED = "DEPRECATED"
    GENERIC = "GENERIC"


class ResolutionType(StrEnum):
    INFORMATIONAL = "INFORMATIONAL"
    GUIDED = "GUIDED"
    ACTION = "ACTION"


# ---------------------------------------------------------------- 子模型


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Message(BaseModel):
    turn_id: int
    role: str
    content: str
    ts: datetime = Field(default_factory=_utcnow)


class Device(BaseModel):
    model: str | None = None
    os: str | None = None
    vpn_client_version: str | None = None


class Actor(BaseModel):
    """唯一写入者:运行时(--as-user + get_user_profile)。任何含 LLM 节点不可写(assert)。"""

    user_id: str | None = None
    display_name: str | None = None
    department: str | None = None
    location: str | None = None
    tenure_days: int | None = None
    device: Device = Field(default_factory=Device)
    groups: list[str] = Field(default_factory=list)
    profile_loaded: bool = False


class Issue(BaseModel):
    verbatim: str = ""  # 唯一写入者:ingress,写一次不可变
    category: Category = Category.UNKNOWN
    urgency: str | None = None
    deadline: str | None = None
    scope: str | None = None
    onset: str | None = None
    affected_systems: list[str] = Field(default_factory=list)
    # 仅 ACCESS_REQUEST 填;值域 = policy.yaml resources 键 + "other"(v3.1 P1-4)
    requested_resources: list[str] = Field(default_factory=list)


class FromUserEntry(BaseModel):
    item_id: str  # v3.1 P1-3 增,QUESTION 项闭环依赖它
    question: str
    answer: str
    turn_id: int


class TriedStep(BaseModel):
    step: str
    outcome: str | None = None


class Collected(BaseModel):
    checklist: dict[str, ChecklistStatus] = Field(default_factory=dict)
    from_user: list[FromUserEntry] = Field(default_factory=list)
    tried_by_user: list[TriedStep] = Field(default_factory=list)
    clarify_count: int = 0  # 硬上限 2


class EvidenceItem(BaseModel):
    """append-only,工具层独占写(经 append_evidence)。"""

    id: str
    tool: str
    args_hash: str
    status: str  # OK | EMPTY | DEGRADED | ERROR
    digest: str
    source_ref: str | None = None
    authority: Authority = Authority.GENERIC
    latency_ms: int = 0
    ts: datetime = Field(default_factory=_utcnow)


class Hypothesis(BaseModel):
    id: str
    text: str
    status: HypothesisStatus = HypothesisStatus.OPEN
    supporting: list[str] = Field(default_factory=list)
    refuting: list[str] = Field(default_factory=list)


class Contradiction(BaseModel):
    """唯一写入者:guards.consistency_checks()(确定性代码,非 LLM)。E8 = 非空。"""

    check_id: str
    description: str
    involved: list[str] = Field(default_factory=list)


class DiagnosisStep(BaseModel):
    text: str
    citation: str | None = None
    citation_kind: str | None = None  # KB | GENERIC


class Diagnosis(BaseModel):
    root_cause: str | None = None
    explanation: str | None = None
    resolution_type: ResolutionType | None = None
    steps: list[DiagnosisStep] = Field(default_factory=list)


class PendingAction(BaseModel):
    """ActionBuilder 代码独占写(经 set_pending_action)。只有显式 YES 消费它。"""

    action_id: str
    tool: str
    args_frozen: dict[str, Any]
    policy_rule_id: str
    idempotency_key: str
    expires_at: datetime  # now + 5min,可注入时钟
    prompt_text: str


class Escalation(BaseModel):
    required: bool = False
    reason_code: ReasonCode | None = None
    triggered_by: str | None = None
    impact: str | None = None
    urgency: str | None = None
    priority: str | None = None
    queue: str | None = None
    ticket_id: str | None = None


class PolicyDecision(BaseModel):
    """E2 唯一输入;intake 后 pre-decide handler(代码)独占写(v3 C-3)。"""

    model_config = ConfigDict(extra="allow")

    action: str
    decision: str  # ALLOW | DENY_REQUIRE_HUMAN | DENY
    rule_id: str | None = None
    queue: str | None = None


class Budget(BaseModel):
    tool_calls: int = 0
    turns: int = 0
    llm_cost_usd: float = 0.0
    elapsed_sec: float = 0.0


# ---------------------------------------------------------------- CaseState


class CaseState(BaseModel):
    case_id: str
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    phase: Phase = Phase.INTAKE
    outcome: Outcome | None = None  # 业务结论,与 phase 解耦;close 结算

    messages: list[Message] = Field(default_factory=list)
    turn_count: int = 0

    actor: Actor = Field(default_factory=Actor)
    issue: Issue = Field(default_factory=Issue)
    collected: Collected = Field(default_factory=Collected)

    evidence: list[EvidenceItem] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    diagnosis: Diagnosis = Field(default_factory=Diagnosis)

    gates: dict[str, str] = Field(default_factory=dict)  # {R1..R3: PASS|FAIL},decide 独占
    confidence: Confidence | None = None  # decide 独占

    pending_action: PendingAction | None = None
    escalation: Escalation = Field(default_factory=Escalation)
    policy_decisions: list[PolicyDecision] = Field(default_factory=list)

    budget: Budget = Field(default_factory=Budget)
    resolution_attempts: int = 0  # runner 计数;guard 节点内重试不计数
    degraded_sources: list[str] = Field(default_factory=list)

    # ---- v3 新字段(全部代码独占写) ----
    guard_failures: int = 0  # resolve 出口 guard 独占写;E10 = ≥2
    declined_actions: list[str] = Field(default_factory=list)  # confirm handler 独占写
    verify_probe_sent: bool = False  # verify handler 独占写

    # ---- v3.1 新字段(全部代码独占写) ----
    user_requested_human: bool = False  # ingress 独占写;棘轮,置 true 不回退
    pending_clarify_item_id: str | None = None  # clarify 节点独占写


# ------------------------------------------------- 三处运行时 assert(唯一合法写入口)

_OWNERS = {"actor": "runtime", "evidence": "tool_layer", "pending_action": "action_builder"}


def write_actor(state: CaseState, actor: Actor, *, writer: str) -> None:
    assert writer == _OWNERS["actor"], f"actor is runtime-owned, got writer={writer!r}"
    state.actor = actor


def append_evidence(state: CaseState, item: EvidenceItem, *, writer: str) -> None:
    assert writer == _OWNERS["evidence"], f"evidence is tool-layer-owned, got writer={writer!r}"
    state.evidence.append(item)  # append-only:不提供替换/删除入口


def set_pending_action(state: CaseState, action: PendingAction | None, *, writer: str) -> None:
    assert (
        writer == _OWNERS["pending_action"]
    ), f"pending_action is ActionBuilder-owned, got writer={writer!r}"
    state.pending_action = action


def mark_user_requested_human(state: CaseState) -> None:
    """棘轮:只能置 true,不存在回退入口(v3.1 P0-1a)。仅 ingress 调用。"""
    state.user_requested_human = True


# ---------------------------------------------------------------- 测试工厂


_ISSUE_KEYS = (
    "verbatim",
    "urgency",
    "deadline",
    "scope",
    "onset",
    "affected_systems",
    "requested_resources",
)


def make_state(**overrides: Any) -> CaseState:
    """一行造出任意测试态。

    便捷键:category / checklist / clarify_count / budget(dict)/ issue 级字段;
    其余键直接落到 CaseState 顶层字段。checklist 缺省 = 该类目全部项 PENDING。
    """
    category = Category(overrides.pop("category", Category.UNKNOWN))
    state = CaseState(case_id=f"case-{uuid.uuid4().hex[:8]}")
    state.phase = Phase(overrides.pop("phase", Phase.INVESTIGATING))
    state.issue.category = category

    checklist = overrides.pop("checklist", None)
    if checklist is None:
        items = load_categories()["categories"][category.value]["checklist"]
        checklist = {i["item_id"]: ChecklistStatus.PENDING for i in items}
    state.collected.checklist = {k: ChecklistStatus(v) for k, v in checklist.items()}

    for key in _ISSUE_KEYS:
        if key in overrides:
            setattr(state.issue, key, overrides.pop(key))
    if "clarify_count" in overrides:
        state.collected.clarify_count = overrides.pop("clarify_count")
    if "budget" in overrides:
        state.budget = Budget(**overrides.pop("budget"))

    _wrap = {
        "policy_decisions": PolicyDecision,
        "hypotheses": Hypothesis,
        "contradictions": Contradiction,
        "evidence": EvidenceItem,
        "messages": Message,
    }
    for key, model in _wrap.items():
        if key in overrides:
            setattr(
                state,
                key,
                [model(**v) if isinstance(v, dict) else v for v in overrides.pop(key)],
            )
    if "pending_action" in overrides:
        value = overrides.pop("pending_action")
        state.pending_action = PendingAction(**value) if isinstance(value, dict) else value
    if "diagnosis" in overrides:
        value = overrides.pop("diagnosis")
        state.diagnosis = Diagnosis(**value) if isinstance(value, dict) else value

    for key, value in overrides.items():
        setattr(state, key, value)
    return state
