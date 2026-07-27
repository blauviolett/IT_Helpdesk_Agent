"""共享分类器(guide §2):confirm 三分 / verify 三态 / ESCALATED 二分。

- 三条否定规则(§4.4)为确定性代码,与 ingress 转人工词表共用同一套否定实现
  (guards.contains_anchored / has_question_marker);
- 词表优先,词表不确定 → SMALL 模型兜底(llm 为 None 或输出不可解析时落安全默认:
  confirm→OTHER(作废动作)、verify→UNKNOWN(模板追问)、ESCALATED→OTHER(追加评论));
- 分类结果不是安全判定:只有显式 YES 消费 pending_action,误判 OTHER 的代价是
  多一轮确认,不是越权。
"""

from __future__ import annotations

from typing import Any

from helpdesk.guards import contains_anchored, has_question_marker

# confirm(AWAITING_CONFIRM):YES | NO | OTHER
YES_ANCHORS: tuple[str, ...] = (
    "发吧",
    "发送",
    "确认",
    "可以",
    "好的",
    "是的",
    "同意",
    "请发",
    "执行吧",
    "yes",
    "go ahead",
    "confirm",
    "do it",
    "ok",
)
NO_ANCHORS: tuple[str, ...] = (
    "不用",
    "不要",
    "别发",
    "算了",
    "取消",
    "拒绝",
    "no",
    "don't",
    "cancel",
)

# verify(AWAITING_VERIFY)/ ESCALATED:正例 = 已解决,反例 = 未解决
RESOLVED_ANCHORS: tuple[str, ...] = (
    "好了",
    "解决了",
    "可以了",
    "正常了",
    "没问题了",
    "搞定",
    "fixed",
    "resolved",
    "it works",
    "works now",
    "working now",
)
FAILED_ANCHORS: tuple[str, ...] = (
    # 不收裸"还没":会误伤"还没来得及试"(= 尚未验证,应 UNKNOWN 走模板追问);
    # "还没好"由"没好"子串覆盖。真实模型验收实测案例,2026-07-27。
    "没好",
    "没用",
    "没解决",
    "还是",
    "仍然",
    "不行",
    "failed",
    "still not",
    "doesn't work",
    "not working",
)


def classify_confirm(text: str, llm: Any = None, budget: Any = None) -> str:
    """三分:YES / NO / OTHER。规则 3:疑问("发吧?")不判 YES,落 OTHER。"""
    if has_question_marker(text):
        return "OTHER"
    if contains_anchored(text, NO_ANCHORS, negation_prefixes=()):
        return "NO"  # 否定词表自身不再做否定否决("不用发"→ NO)
    if contains_anchored(text, YES_ANCHORS):
        return "YES"
    return _small_fallback(
        llm, "confirm", text, ("YES", "NO", "OTHER"), default="OTHER", budget=budget
    )


def classify_verify(text: str, llm: Any = None, budget: Any = None) -> str:
    """三态:RESOLVED / FAILED / UNKNOWN。"还没好"/"没好"→ FAILED,"好了吗?"→ UNKNOWN。"""
    if has_question_marker(text):
        return "UNKNOWN"
    if contains_anchored(text, FAILED_ANCHORS, negation_prefixes=()):
        return "FAILED"
    if contains_anchored(text, RESOLVED_ANCHORS):
        return "RESOLVED"
    return _small_fallback(
        llm, "verify", text, ("RESOLVED", "FAILED", "UNKNOWN"), default="UNKNOWN", budget=budget
    )


def classify_escalated(text: str, llm: Any = None, budget: Any = None) -> str:
    """二分:RESOLVED / OTHER(OTHER → escalate_followup 追加工单评论,phase 不变)。"""
    if has_question_marker(text):
        return "OTHER"
    if contains_anchored(text, FAILED_ANCHORS, negation_prefixes=()):
        return "OTHER"
    if contains_anchored(text, RESOLVED_ANCHORS):
        return "RESOLVED"
    return _small_fallback(
        llm, "escalated", text, ("RESOLVED", "OTHER"), default="OTHER", budget=budget
    )


_SMALL_PROMPT = """你是 IT Helpdesk 的短文本分类器。对用户消息做 {kind} 分类。
只输出以下标签之一,不输出任何其他内容:{labels}
拿不准时输出 {default}。

用户消息:{text}"""


def _small_fallback(
    llm: Any, kind: str, text: str, labels: tuple[str, ...], *, default: str, budget: Any = None
) -> str:
    """词表不确定 → SMALL 模型兜底;llm 缺席或输出越界一律落安全默认。"""
    if llm is None:
        return default
    raw = llm.complete_text(
        "classifier",
        _SMALL_PROMPT.format(kind=kind, labels=" / ".join(labels), default=default, text=text),
        tier="SMALL",
        budget=budget,
    )
    label = raw.strip().upper()
    return label if label in labels else default
