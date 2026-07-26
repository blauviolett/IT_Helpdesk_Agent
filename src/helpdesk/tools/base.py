"""ToolResult 信封(OK/EMPTY/DEGRADED/ERROR)+ ErrorInfo + digest 约定(guide §2)。

- digest 是工具结果唯一进模型上下文的表示;原始 JSON 留在 data,只供代码消费。
- 每个工具的 digest 由 adapter 内确定性函数生成(同输入必同输出,可单测)。
- ToolRuntime 承载 `--fixture` / `--fail` 开关(CLI 于 D3 接线,测试直接构造)。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from helpdesk.state.models import Authority


class ToolStatus(StrEnum):
    OK = "OK"
    EMPTY = "EMPTY"  # 有效证据("查过且没有"本身是信息),≠ ERROR
    DEGRADED = "DEGRADED"  # 拿到数据但来源降级;计入 degraded_sources
    ERROR = "ERROR"  # 信息缺口 → checklist UNAVAILABLE → E7


class ErrorInfo(BaseModel):
    code: str
    message: str


class ToolResult(BaseModel):
    status: ToolStatus
    digest: str
    data: Any = None  # 原始 JSON,不进上下文
    source_ref: str | None = None
    authority: Authority = Authority.GENERIC
    error: ErrorInfo | None = None
    latency_ms: int = 0


@dataclass
class ToolRuntime:
    """工具层运行开关:fixture 选择 + 故障/降级注入(确定性,demo 可复现)。"""

    fixture: str = "status_a"
    fail_tools: set[str] = field(default_factory=set)
    degraded_tools: set[str] = field(default_factory=set)


def hash_args(tool: str, args: dict[str, Any]) -> str:
    """确定性 args 指纹:evidence 去重与幂等键共用同一规范化方式。"""
    canonical = json.dumps(args, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(f"{tool}|{canonical}".encode()).hexdigest()[:16]


def error_result(message: str, code: str) -> ToolResult:
    return ToolResult(
        status=ToolStatus.ERROR,
        digest=message,
        error=ErrorInfo(code=code, message=message),
    )
