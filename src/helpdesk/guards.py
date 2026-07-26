"""护栏(确定性代码,不得改由 LLM 判定)。

D1 落地:转人工词表 + 共享否定匹配实现(v3.1 P0-1a / P1-5)。
D3 落地:Input Guard / Output Guard;D4 落地:consistency_checks()(1 条规则)。

否定匹配三规则(guide §4.4,全部确定性),本实现供两处复用:
① ingress 转人工词表(本文件 detect_human_request);
② classifier 的 confirm/verify/ESCALATED 分类(D3,复用 contains_anchored 与
   has_question_marker)。
"""

from __future__ import annotations

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
