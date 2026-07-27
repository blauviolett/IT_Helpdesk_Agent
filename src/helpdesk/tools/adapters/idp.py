"""IdP(Okta)adapter:get_account_status(只读)+ send_unlock_verification(写)。

账号态为进程内 Mock fixture(adapters 是唯一可替换面;data/ 冻结目录无 idp 文件,
故不落新数据文件)。写工具只经三段协议的 act 执行,目标恒为会话 actor
(运行时注入,签名无 target_user)。
"""

from __future__ import annotations

from typing import Any

from helpdesk.state.models import Actor
from helpdesk.tools.base import ToolResult, ToolRuntime, ToolStatus, error_result

_ACCOUNTS: dict[str, dict[str, Any]] = {
    "u-alice": {
        "state": "LOCKED_OUT",
        "failed_attempts": 5,
        "locked_at": "2026-07-26T09:14:00Z",
        "mfa": "enrolled",
    },
    "u-bob": {"state": "ACTIVE", "failed_attempts": 0, "mfa": "enrolled"},
    "u-carol": {"state": "ACTIVE", "failed_attempts": 0, "mfa": "pending_enrollment"},
    "u-dan": {"state": "ACTIVE", "failed_attempts": 1, "mfa": "enrolled"},
    "u-eve": {"state": "ACTIVE", "failed_attempts": 0, "mfa": "enrolled"},
}


def get_account_status(actor: Actor, params: Any, runtime: ToolRuntime) -> ToolResult:
    if not actor.user_id:
        return ToolResult(status=ToolStatus.EMPTY, digest="no user bound to session")
    account = _ACCOUNTS.get(actor.user_id)
    if account is None:
        return ToolResult(
            status=ToolStatus.EMPTY, digest=f"no IdP account for {actor.user_id}"
        )
    extra = f", locked_at {account['locked_at']}" if "locked_at" in account else ""
    digest = (
        f"okta account {actor.user_id}: {account['state']} "
        f"({account['failed_attempts']} failed attempts{extra}, mfa {account['mfa']})"
    )
    return ToolResult(status=ToolStatus.OK, digest=digest, data=account, source_ref="idp:okta")


def send_unlock_verification(actor: Actor, params: Any, runtime: ToolRuntime) -> ToolResult:
    """写工具(v2.1 C2,唯一走完整确认协议的动作):向 actor 的注册邮箱发解锁验证。"""
    if not actor.user_id:
        return error_result("no user bound to session; cannot send unlock verification", "no_actor")
    account = _ACCOUNTS.get(actor.user_id)
    if account is None:
        return error_result(f"no IdP account for {actor.user_id}", "unknown_account")
    digest = (
        f"unlock verification email sent for {actor.user_id} "
        f"to registered address (mfa {account['mfa']})"
    )
    return ToolResult(
        status=ToolStatus.OK,
        digest=digest,
        data={"user": actor.user_id, "delivery": "email"},
        source_ref="idp:okta",
    )
