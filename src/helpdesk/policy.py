"""授权裁决(guide §2):deny-by-default 匹配器。

- D3:check() 规则匹配器 —— 4 条规则见 config/policy.yaml,未声明 action 一律 DENY;
- D4:ActionBuilder(args 冻结 + freeze 前置 declined 检查)。

消费方:intake 后 pre-decide handler(对每个枚举资源跑 check → 写 policy_decisions,
E2 唯一输入);registry 策略门(写工具执行前二次校验);D4 的 FREEZE / EXECUTE 复核。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from helpdesk.config import load_policy


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


def _validate_rules(policy: dict[str, Any]) -> None:
    """grant_access 规则与 resources 段(v3.1 P1-4 冻结结构)不得漂移。"""
    rules = {r["action"]: r for r in policy.get("rules", [])}
    for name, cfg in policy["resources"].items():
        rule = rules.get(f"grant_access:{name}")
        assert rule is not None, f"resource {name} 缺少 grant_access 规则"
        assert rule["decision"] == cfg["decision"] and rule.get("queue") == cfg.get(
            "queue"
        ), f"resource {name} 的 rules 与 resources 两处 decision/queue 不一致"
