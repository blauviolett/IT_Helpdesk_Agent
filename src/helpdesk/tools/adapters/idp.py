"""IdP(Okta)adapter:get_account_status(只读)。

账号态为进程内 Mock fixture(adapters 是唯一可替换面;data/ 冻结目录无 idp 文件,
故不落新数据文件)。写工具 send_unlock_verification 按计划 D4 落地。
"""

from __future__ import annotations

from typing import Any

from helpdesk.state.models import Actor
from helpdesk.tools.base import ToolResult, ToolRuntime, ToolStatus

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
