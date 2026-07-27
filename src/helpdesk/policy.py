"""授权裁决(guide §2):deny-by-default 匹配器 + ActionBuilder(args 冻结)。

- check():4 条规则见 config/policy.yaml,未声明 action 一律 DENY;
- freeze():三段协议 ② FREEZE(guide §4.5)。模型只提 intent,args 由本文件代码
  冻结;前置检查 intent ∈ declined_actions → 拒绝冻结(v3.1 P1-6 第二道防线)。

消费方:intake 后 pre-decide handler(对每个枚举资源跑 check → 写 policy_decisions,
E2 唯一输入);registry 策略门(写工具执行前二次校验);act 的 EXECUTE 前置复核。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from helpdesk.config import load_policy
from helpdesk.state.models import CaseState, PendingAction, set_pending_action


@dataclass(frozen=True)
class PolicyVerdict:
    decision: str  # ALLOW | DENY_REQUIRE_HUMAN | DENY
    rule_id: str | None = None
    queue: str | None = None
    requires_confirm: bool = False


_DENY = PolicyVerdict(decision="DENY")  # deny-by-default:未声明 action 的唯一出口


def check(action: str, *, policy: dict[str, Any] | None = None) -> PolicyVerdict:
    """按 action 精确匹配规则;无匹配一律 DENY(deny-by-default)。"""
    policy = policy if policy is not None else load_policy()
    _validate_rules(policy)
    for rule in policy.get("rules", []):
        if rule["action"] == action:
            return PolicyVerdict(
                decision=rule["decision"],
                rule_id=rule["rule_id"],
                queue=rule.get("queue"),
                requires_confirm=bool(rule.get("requires_confirm", False)),
            )
    return _DENY


# ============================================= ActionBuilder(三段协议 ② FREEZE)

_ACTION_TTL = timedelta(minutes=5)

# 走完整确认协议的写动作(v2.1 C2 唯一一个)。intent 命名空间 = 写工具名,
# 与 confirm handler 写入 declined_actions 的值同一命名空间。
_CONFIRMABLE_INTENTS: dict[str, str] = {
    "send_unlock_verification": (
        "要现在发送 Okta 解锁验证邮件到你的注册邮箱吗?"
        "回复\"发吧\"执行,\"不用\"取消(确认 5 分钟内有效)。"
    ),
}


def idempotency_key(case_id: str, tool: str, args: dict[str, Any]) -> str:
    """幂等键 = sha256(case_id|tool|args_frozen),与 evidence 去重同一规范化方式。"""
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(f"{case_id}|{tool}|{canonical}".encode()).hexdigest()[:16]


def freeze(state: CaseState, intent: str, *, now: datetime | None = None) -> str | None:
    """冻结一个待确认动作,写入 pending_action(本函数是唯一合法写入口的唯一调用方)。

    返回 None = 已冻结;返回 str = 拒绝原因(调用方走"LLM 提议非法动作"的既有
    节点内重试路径,不抛异常 —— 拒绝不是系统错误,不触发 E4)。
    args 由代码冻结:目标恒为会话 actor(运行时注入),永不含 target_user。
    """
    if intent in state.declined_actions:  # P1-6 前置检查,不可绕过
        return f"用户已明确拒绝过动作 {intent},不得再次提议"
    prompt_text = _CONFIRMABLE_INTENTS.get(intent)
    if prompt_text is None:
        return f"未知或不支持确认协议的写动作:{intent or '(空)'}"
    verdict = check(intent)
    if verdict.decision != "ALLOW":
        return f"策略不允许执行 {intent}(deny-by-default)"
    if not state.actor.user_id:
        return "会话未绑定用户身份,无法确定动作目标"
    args: dict[str, Any] = {}  # 冻结:无参数,目标 = 会话 actor,由 registry 注入
    now = now or datetime.now(timezone.utc)
    set_pending_action(
        state,
        PendingAction(
            action_id=f"act-{uuid.uuid4().hex[:8]}",
            tool=intent,
            args_frozen=args,
            policy_rule_id=verdict.rule_id or "",
            idempotency_key=idempotency_key(state.case_id, intent, args),
            expires_at=now + _ACTION_TTL,
            prompt_text=prompt_text,
        ),
        writer="action_builder",
    )
    return None


def _validate_rules(policy: dict[str, Any]) -> None:
    """grant_access 规则与 resources 段(v3.1 P1-4 冻结结构)不得漂移。"""
    rules = {r["action"]: r for r in policy.get("rules", [])}
    for name, cfg in policy["resources"].items():
        rule = rules.get(f"grant_access:{name}")
        assert rule is not None, f"resource {name} 缺少 grant_access 规则"
        assert rule["decision"] == cfg["decision"] and rule.get("queue") == cfg.get(
            "queue"
        ), f"resource {name} 的 rules 与 resources 两处 decision/queue 不一致"
