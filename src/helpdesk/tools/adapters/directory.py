"""目录 adapter:get_user_profile / get_entitlements。

数据源 data/directory.json。groups(users 视图)与 entitlements(顶层视图)是两个
独立视图,fixture 中埋 1 处矛盾(u-eve:grafana-editors 组 vs 仅 grafana:viewer),
供 D4 guards.consistency_checks() 消费。

签名约定:actor 由 registry 运行时注入,永不出现 target_user / user_id 参数。
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from helpdesk.config import DATA_DIR
from helpdesk.state.models import Actor
from helpdesk.tools.base import ToolResult, ToolRuntime, ToolStatus


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    return json.loads((DATA_DIR / "directory.json").read_text(encoding="utf-8"))


def get_user_profile(actor: Actor, params: Any, runtime: ToolRuntime) -> ToolResult:
    if not actor.user_id:
        return ToolResult(status=ToolStatus.EMPTY, digest="no user bound to session")
    user = _load()["users"].get(actor.user_id)
    if user is None:
        return ToolResult(
            status=ToolStatus.EMPTY, digest=f"user {actor.user_id} not found in directory"
        )
    d = user["device"]
    digest = (
        f"{user['display_name']} — {user['department']}, {user['location']}, "
        f"tenure {user['tenure_days']}d; device {d['model']} / {d['os']}, "
        f"vpn client {d['vpn_client_version']}; groups: {', '.join(user['groups'])}"
    )
    return ToolResult(
        status=ToolStatus.OK, digest=digest, data=user, source_ref="directory.json"
    )


def get_entitlements(actor: Actor, params: Any, runtime: ToolRuntime) -> ToolResult:
    if not actor.user_id:
        return ToolResult(status=ToolStatus.EMPTY, digest="no user bound to session")
    entitlements = _load()["entitlements"].get(actor.user_id)
    if entitlements is None:
        return ToolResult(
            status=ToolStatus.EMPTY,
            digest=f"no entitlement record for {actor.user_id}",
        )
    digest = f"entitlements for {actor.user_id}: {', '.join(sorted(entitlements))}"
    return ToolResult(
        status=ToolStatus.OK,
        digest=digest,
        data=entitlements,
        source_ref="directory.json#entitlements",
    )
