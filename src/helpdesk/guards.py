"""护栏(确定性代码,不得改由 LLM 判定)。

D1 落地:转人工词表 + 共享否定匹配实现(v3.1 P0-1a / P1-5)。
D3 落地:Input Guard(凭据检测 + 注入/安全信号 + 附件拒收,v2.1 D8)+
Output Guard(引用存在性 + authority 仅对 KB + GENERIC 规则,v2.1 D5/D6/D10)。
D4 落地:consistency_checks()(1 条规则,groups vs entitlements;E8 唯一输入)。

否定匹配三规则(guide §4.4,全部确定性),本实现供两处复用:
① ingress 转人工词表(本文件 detect_human_request);
② classifier 的 confirm/verify/ESCALATED 分类(D3,复用 contains_anchored 与
   has_question_marker)。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from helpdesk import perf

if TYPE_CHECKING:
    from helpdesk.state.models import CaseState, DiagnosisStep

# 规则 2:否定前缀,命中(紧邻锚定短语之前)即否决正例
NEGATION_PREFIXES: tuple[str, ...] = (
    "还没",
    "没有",
    "不",
    "别",
    "not",
    "didn't",
    "hasn't",
)

# 规则 3:疑问标记 → 不判 RESOLVED/YES,落 OTHER(classifier D3 消费)
QUESTION_MARKERS: tuple[str, ...] = ("吗", "?", "?")

# 转人工正例锚定短语(v3.1 P0-1a)。不含裸"人工",否则"人工智能"误命中。
HUMAN_REQUEST_ANCHORS: tuple[str, ...] = (
    "转人工",
    "转接人工",
    "人工客服",
    "找个人",
    "human agent",
    "real person",
    "talk to a person",
    "talk to a human",
    "不要机器人",
)

# 锚定短语前检查否定语素的窗口(覆盖"不用"/"还没"/"not a"等紧邻前缀)
_NEGATION_WINDOW = 8


def contains_anchored(
    text: str,
    anchors: tuple[str, ...],
    negation_prefixes: tuple[str, ...] = NEGATION_PREFIXES,
) -> bool:
    """规则 1+2:锚定短语匹配(非全文子串扫描),紧邻否定前缀否决该次命中。"""
    lower = text.lower()
    for anchor in anchors:
        needle = anchor.lower()
        start = 0
        while (idx := lower.find(needle, start)) != -1:
            window = lower[max(0, idx - _NEGATION_WINDOW) : idx]
            if not any(neg in window for neg in negation_prefixes):
                return True
            start = idx + len(needle)
    return False


def has_question_marker(text: str) -> bool:
    """规则 3:含疑问标记 → 不判 RESOLVED/YES(classifier 落 OTHER)。"""
    return any(m in text for m in QUESTION_MARKERS)


def detect_human_request(text: str) -> bool:
    """ingress 对每条用户消息调用;命中 → mark_user_requested_human(棘轮)。

    已知局限(README):纯词表会漏掉隐晦表达("我不想跟机器说话")。
    漏检代价是用户换一句更明确的话,不是安全问题。
    """
    return contains_anchored(text, HUMAN_REQUEST_ANCHORS)


# ================================================================ Input Guard
# v2.1 D8:凭据检测 + 注入模式/安全信号 + 附件拒收,正则 + 关键词,全部确定性。

# 安全信号锚定短语:命中 → ingress 先建 case 落盘再直升 escalate(guide §4.2 ②);
# intake 归类 SECURITY 走 E1 兜住词表漏检的表达。
SECURITY_SIGNAL_ANCHORS: tuple[str, ...] = (
    "钓鱼",
    "被黑",
    "被盗",
    "盗号",
    "泄密",
    "泄露",
    "中毒",
    "可疑登录",
    "phishing",
    "hacked",
    "compromised",
    "malware",
    "suspicious login",
)

# 凭据模式:密码/密钥出现在消息里 → 存库前脱敏(不进 messages / trace)。
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(password|passwd|pwd|密码)\s*(is|是|为|[:=:])\s*\S+"),
    re.compile(r"(?i)(api[_ ]?key|token|secret)\s*(is|是|为|[:=:])\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"),
)

# 附件拒收:CLI 是纯文本通道,粘贴的附件/内联图一律拒收并提示文字描述(v2.1 边界)。
_ATTACHMENT_MARKERS: tuple[str, ...] = ("data:image/", "[attachment", "[附件")


def detect_security_signal(text: str) -> bool:
    return contains_anchored(text, SECURITY_SIGNAL_ANCHORS)


def redact_credentials(text: str) -> tuple[str, bool]:
    """凭据脱敏:返回 (脱敏后文本, 是否命中)。命中值以 [REDACTED] 替换,原文不落库。"""
    found = False
    for pattern in _CREDENTIAL_PATTERNS:
        text, n = pattern.subn(r"[REDACTED]", text)
        found = found or n > 0
    return text, found


def detect_attachment(text: str) -> bool:
    lower = text.lower()
    return any(m in lower for m in _ATTACHMENT_MARKERS)


# ================================================================ Output Guard
# v2.1 D5/D6/D10 + guide §2:resolve 出口的引用校验,失败控制流由 resolve 节点承载
# (带违规信息节点内重试 1 次 → 仍失败 guard_failures=2 → 回 decide → E10)。


def output_guard(
    state: CaseState,
    steps: list[DiagnosisStep],
    *,
    kb_status: dict[str, str] | None = None,
) -> list[str]:
    """返回违规列表(空 = 通过)。三条规则,全部确定性:

    1. 引用存在性:citation 必须命中本 case 证据账本(evidence id / source_ref /
       search_kb digest 中出现过的 KB 编号)—— 伪造 KB-9999 在此拦截;
    2. authority 仅对 KB:citation_kind=KB 的引用必须是 VERIFIED 文档
       (DRAFT 仅背景、DEPRECATED 已在索引层排除);非 KB 引用只查账本存在性;
    3. GENERIC 规则:无引用步骤是通用建议,允许存在,但全部步骤都无有效引用时
       不能单独构成"已解决"(v2.1 D10)。
    """
    with perf.span("output_guard", steps=len(steps)):
        return _output_guard(state, steps, kb_status=kb_status)


def _output_guard(
    state: CaseState,
    steps: list[DiagnosisStep],
    *,
    kb_status: dict[str, str] | None = None,
) -> list[str]:
    if kb_status is None:
        from helpdesk.tools.retrieval import get_retriever  # 延迟导入,便于测试注入

        kb_status = {d["kb_id"]: d["status"] for d in get_retriever()._docs}

    ledger_refs = {e.id for e in state.evidence} | {
        e.source_ref for e in state.evidence if e.source_ref
    }
    kb_seen = " ".join(e.digest for e in state.evidence if e.tool == "search_kb")

    violations: list[str] = []
    valid_citations = 0
    for i, step in enumerate(steps, 1):
        citation = step.citation
        if not citation:
            continue  # 无引用 = GENERIC 建议,由规则 3 兜底
        exists = citation in ledger_refs or citation in kb_seen
        if not exists:
            violations.append(f"step {i}: 引用 {citation} 不在本 case 证据账本中")
            continue
        if step.citation_kind == "KB" and kb_status.get(citation) != "VERIFIED":
            violations.append(f"step {i}: KB 引用 {citation} 非 VERIFIED,不可作为依据")
            continue
        valid_citations += 1
    if steps and valid_citations == 0:
        violations.append("全部步骤均无有效引用:通用建议不能单独构成解决方案")
    return violations


# ==================================================== 跨源一致性检查(D4,确定性)
# 唯一规则:目录 groups 视图与 entitlements 权限视图矛盾(fixture 埋点:u-eve 在
# grafana-editors 组,但权限视图只有 grafana:viewer)。E8 = contradictions 非空。

_GROUP_IMPLIES_ENTITLEMENT: dict[str, str] = {"grafana-editors": "grafana:editor"}


def consistency_checks(state: CaseState) -> list:
    """contradictions 唯一写入者(确定性代码,非 LLM)。

    整体重算(幂等):同一状态跑 N 次结果一致(10/10 复现),重入不累积。
    输入:actor.groups(get_user_profile 装载)+ get_entitlements 的证据 digest
    (digest 格式由 directory adapter 冻结:"entitlements for u-x: a, b, c")。
    """
    from helpdesk.state.models import Contradiction

    found: list[Contradiction] = []
    ent = next(
        (e for e in reversed(state.evidence) if e.tool == "get_entitlements" and e.status == "OK"),
        None,
    )
    if ent is not None and state.actor.groups:
        entitlements = _entitlements_from_digest(ent.digest)
        for group, required in _GROUP_IMPLIES_ENTITLEMENT.items():
            if group in state.actor.groups and required not in entitlements:
                found.append(
                    Contradiction(
                        check_id="groups_vs_entitlements",
                        description=(
                            f"目录 groups 含 {group},但权限视图无 {required}"
                            f"(实际:{', '.join(sorted(entitlements)) or '(空)'});"
                            "两个数据源矛盾,不能作为授权依据,需人工核对"
                        ),
                        involved=["get_user_profile", ent.id],
                    )
                )
    state.contradictions = found
    return found


def _entitlements_from_digest(digest: str) -> set[str]:
    _, _, tail = digest.partition(":")
    return {part.strip() for part in tail.split(",") if part.strip()}
