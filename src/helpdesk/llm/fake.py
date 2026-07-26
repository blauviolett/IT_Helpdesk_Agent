"""FakeLLM:CI 确定性的唯一来源(guide §2)。

`{node: response}` dict;value 为 list 时按调用次数出队;耗尽(或未配置该 node)
返回 schema 的全默认实例 —— 对 investigate 即空工具列表,天然触发终止条件 1。
"""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel

from helpdesk.state.models import Budget

M = TypeVar("M", bound=BaseModel)


class FakeLLM:
    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self._responses = dict(responses or {})
        self.calls: list[str] = []  # 供测试断言调用序列

    def _next(self, node: str) -> Any:
        value = self._responses.get(node)
        if isinstance(value, list):
            return value.pop(0) if value else None
        return value

    def complete_structured(
        self, node: str, prompt: str, schema: type[M], *, tier: str = "MAIN", budget: Budget | None = None
    ) -> M:
        self.calls.append(node)
        payload = self._next(node)
        if payload is None:
            return schema()  # 耗尽:全默认值(investigate → 空工具列表)
        if isinstance(payload, schema):
            return payload
        return schema.model_validate(payload)

    def complete_text(
        self, node: str, prompt: str, *, tier: str = "MAIN", budget: Budget | None = None
    ) -> str:
        self.calls.append(node)
        payload = self._next(node)
        return "" if payload is None else str(payload)
