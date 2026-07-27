"""ITSM adapter:create_escalation_ticket(写,记账型,无需用户确认;v2.1 C3)。

- 工单为进程内 Mock 存储;digest 确定性(同输入必同输出,ticket_id 由参数哈希派生)。
- append_comment 是 escalate_followup handler 的代码副作用(追加工单评论),
  不是注册工具 —— 工具冻结在 6 只读 + 2 写,不为评论增加第 3 个写工具。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from helpdesk.state.models import Actor
from helpdesk.tools.base import ToolResult, ToolRuntime, ToolStatus, hash_args


class TicketParams(BaseModel):
    queue: str
    priority: str
    subject: str
    packet: dict[str, Any] = Field(default_factory=dict)


_TICKETS: dict[str, dict[str, Any]] = {}


def create_escalation_ticket(actor: Actor, params: Any, runtime: ToolRuntime) -> ToolResult:
    ticket_id = f"TCK-{hash_args('create_escalation_ticket', params.model_dump(mode='json'))[:8].upper()}"
    _TICKETS[ticket_id] = {
        "queue": params.queue,
        "priority": params.priority,
        "subject": params.subject,
        "packet": params.packet,
        "comments": [],
    }
    digest = (
        f"ticket {ticket_id} created in {params.queue} "
        f"(priority {params.priority}): {params.subject}"
    )
    return ToolResult(
        status=ToolStatus.OK,
        digest=digest,
        data={"ticket_id": ticket_id, "queue": params.queue, "priority": params.priority},
        source_ref=f"itsm:{ticket_id}",
    )


def append_comment(ticket_id: str | None, text: str) -> bool:
    """追加工单评论(mock)。跨进程恢复时内存工单不在:重建空壳,评论仍可追加。"""
    if not ticket_id:
        return False
    ticket = _TICKETS.setdefault(ticket_id, {"comments": []})
    ticket["comments"].append(text)
    return True
