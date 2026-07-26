"""安全测试。D1 落地:转人工词表段(v3.1 P0-1a:≥5 正例置位 + ≥3 负例不置位)。

其余段按计划补:policy deny-by-default(D3)、Output Guard / 一致性检查 /
签名静态检查(D3/D4)、packet 字段 allowlist(D4)。
"""

import pytest

from helpdesk.guards import detect_human_request, has_question_marker
from helpdesk.state.models import make_state, mark_user_requested_human

# ------------------------------------------------- 转人工词表:正例(≥5)


@pytest.mark.parametrize(
    "text",
    [
        "转人工",
        "请帮我转人工吧",
        "我要找个人来处理这个问题",
        "I want to talk to a person",
        "Can I get a human agent",
        "不要机器人",
    ],
)
def test_human_request_positive(text):
    assert detect_human_request(text) is True


def test_positive_sets_ratchet_field():
    state = make_state()
    if detect_human_request("转人工"):
        mark_user_requested_human(state)
    assert state.user_requested_human is True


# ------------------------------------------------- 转人工词表:负例(≥3,必含指定两条)


@pytest.mark.parametrize(
    "text",
    [
        "人工智能真好用",  # v3.1 指定:含"人工"子串但无锚定短语
        "不用转人工",      # v3.1 指定:否定前缀否决
        "别转人工,我再试试",
        "我在研究人工智能的课题",
    ],
)
def test_human_request_negative(text):
    assert detect_human_request(text) is False


# ------------------------------------------------- 共享否定实现的疑问标记(classifier D3 复用)


def test_question_marker_detected():
    assert has_question_marker("好了吗?") is True
    assert has_question_marker("发吧?") is True
    assert has_question_marker("已经好了") is False
