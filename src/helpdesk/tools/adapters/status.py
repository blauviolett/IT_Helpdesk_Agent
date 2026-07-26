"""状态页 adapter:check_service_status / get_recent_changes。

数据源 data/status_{a,b}.json,由 ToolRuntime.fixture 选择(`--fixture` 开关,
Demo Case B 的两个分支)。digest 确定性:服务名排序、条目按 fixture 原序。
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from helpdesk.config import DATA_DIR
from helpdesk.state.models import Actor
from helpdesk.tools.base import ToolResult, ToolRuntime, ToolStatus


@lru_cache(maxsize=4)
def _load(fixture: str) -> dict[str, Any]:
    return json.loads((DATA_DIR / f"{fixture}.json").read_text(encoding="utf-8"))


def _service_line(name: str, svc: dict[str, Any]) -> str:
    if "incident" in svc:
        inc = svc["incident"]
        return (
            f"{name}: {svc['status']} — {inc['id']} {inc['summary']} "
            f"(regions: {', '.join(inc['regions'])}, since {inc['started_at']})"
        )
    return f"{name}: {svc['status']}"


def check_service_status(actor: Actor, params: Any, runtime: ToolRuntime) -> ToolResult:
    data = _load(runtime.fixture)
    services = data["services"]
    ref = f"{runtime.fixture}.json"
    if params.service:
        name = params.service.lower()
        svc = services.get(name)
        if svc is None:
            return ToolResult(
                status=ToolStatus.EMPTY, digest=f"no status entry for service: {name}"
            )
        return ToolResult(
            status=ToolStatus.OK, digest=_service_line(name, svc), data=svc, source_ref=ref
        )
    abnormal = [n for n in sorted(services) if services[n]["status"] != "operational"]
    if abnormal:
        detail = "; ".join(_service_line(n, services[n]) for n in abnormal)
        digest = f"{len(services)} services — attention: {detail}; others operational"
    else:
        digest = f"{len(services)} services — all operational"
    return ToolResult(status=ToolStatus.OK, digest=digest, data=services, source_ref=ref)


def get_recent_changes(actor: Actor, params: Any, runtime: ToolRuntime) -> ToolResult:
    data = _load(runtime.fixture)
    log = data.get("change_log", [])
    scope = ""
    if params.service:
        name = params.service.lower()
        log = [c for c in log if c["service"] == name]
        scope = f" for {name}"
    if not log:
        return ToolResult(status=ToolStatus.EMPTY, digest=f"no recent changes{scope}")
    digest = "; ".join(
        f"{c['change_id']} [{c['service']}] {c['summary']} ({c['ts']})" for c in log
    )
    return ToolResult(
        status=ToolStatus.OK, digest=digest, data=log, source_ref=f"{runtime.fixture}.json"
    )
