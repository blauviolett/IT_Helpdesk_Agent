"""状态机转移(纯函数,guide §2 / §4.2):三张冻结映射表。

- route_message:恢复路由表(6 行)的 phase → 入口节点/handler 部分;
  消息语义分支(confirm 三分 / verify 三态 / ESCALATED 二分)由 classifier 在
  对应 handler 内完成(D3),本表只负责"进哪个门"。
- on_decision:decide Branch → 节点。
- 节点后置 phase 与等待态集合:每个等待态唯一恢复点。
"""

from __future__ import annotations

from helpdesk.orchestrator.gates import Branch
from helpdesk.state.models import CaseState, Phase, ResolutionType

# phase → 消息入口(guide §4.2)。CLOSED / 无 case 由 ingress 建新 case,不进本表;
# INTAKE / INVESTIGATING 是中间态,消息恢复直接回 decide(None)或重进 intake。
_MESSAGE_ROUTE: dict[Phase, str | None] = {
    Phase.INTAKE: "intake",
    Phase.INVESTIGATING: None,
    Phase.AWAITING_CLARIFY: "clarify_resume",   # D3:恢复 handler → investigate
    Phase.AWAITING_CONFIRM: "confirm",          # D3:classifier 三分,仅显式 YES → act
    Phase.AWAITING_VERIFY: "verify",            # D3:classifier 三态
    Phase.ESCALATED: "escalated_followup",      # D4:二分(RESOLVED → close / 追加评论)
}

_BRANCH_NODE: dict[Branch, str | None] = {
    Branch.ESCALATE: "escalate",
    Branch.REDIRECT: "close",     # E9 不建单,close 结算 outcome=REDIRECTED(D4)
    Branch.RESOLVE: "resolve",
    Branch.ASK: "clarify",
    Branch.INVESTIGATE: "investigate",
    Branch.OBSERVE: None,
}

# 节点完成后的 phase(phase 唯一写入者 = transition/runner)。
_POST_PHASE: dict[str, Phase] = {
    "intake": Phase.INVESTIGATING,
    "investigate": Phase.INVESTIGATING,
    "clarify": Phase.AWAITING_CLARIFY,
    "escalate": Phase.ESCALATED,
    "close": Phase.CLOSED,
}

_WAITING: frozenset[Phase] = frozenset(
    {
        Phase.AWAITING_CLARIFY,
        Phase.AWAITING_CONFIRM,
        Phase.AWAITING_VERIFY,
        Phase.ESCALATED,
        Phase.CLOSED,
    }
)


def route_message(phase: Phase) -> str | None:
    return _MESSAGE_ROUTE[phase]


def on_decision(branch: Branch) -> str | None:
    return _BRANCH_NODE[branch]


def apply_post_phase(state: CaseState, node_name: str) -> None:
    if node_name == "resolve":
        # 依 resolution_type 分派:ACTION → AWAITING_CONFIRM;GUIDED/INFORMATIONAL →
        # AWAITING_VERIFY;None(Output Guard 两次未过,诊断已清空)→ phase 不动,
        # 回 decide 由 E10 承接。
        rt = state.diagnosis.resolution_type
        if rt is ResolutionType.ACTION:
            state.phase = Phase.AWAITING_CONFIRM
        elif rt is not None:
            state.phase = Phase.AWAITING_VERIFY
        return
    if node_name in _POST_PHASE:
        state.phase = _POST_PHASE[node_name]


def is_waiting(phase: Phase) -> bool:
    return phase in _WAITING
