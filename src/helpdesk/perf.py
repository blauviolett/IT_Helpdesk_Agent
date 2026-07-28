"""性能埋点(纯 profiling,零业务语义;Performance Engineering 专用)。

- runner 在 handle_message 起止处开/关采集,结束时把整份 profile 写入该 case 的
  trace JSONL(kind=perf_profile),spans 含相对起点偏移 + 耗时(ms)。
- 其余模块通过 `with perf.span("stage", **meta):` 打点;无活动采集器时 span 是
  no-op(contextvar 为 None),对 L1 测试与库调用方零侵入。
- 只测量、不改变任何控制流:span 不吞异常、不改返回值。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Iterator

_ACTIVE: ContextVar["PerfCollector | None"] = ContextVar("helpdesk_perf", default=None)


class PerfCollector:
    def __init__(self) -> None:
        self.t0 = time.perf_counter()
        self.spans: list[dict[str, Any]] = []

    def record(self, stage: str, start: float, end: float, meta: dict[str, Any]) -> None:
        self.spans.append(
            {
                "stage": stage,
                "start_ms": round((start - self.t0) * 1000, 1),
                "duration_ms": round((end - start) * 1000, 1),
                **meta,
            }
        )

    def total_ms(self) -> float:
        return round((time.perf_counter() - self.t0) * 1000, 1)


def start() -> tuple[PerfCollector, Token]:
    """开启一次请求级采集;调用方负责用返回的 token 调 stop()。"""
    collector = PerfCollector()
    return collector, _ACTIVE.set(collector)


def stop(token: Token) -> None:
    _ACTIVE.reset(token)


@contextmanager
def span(stage: str, **meta: Any) -> Iterator[None]:
    """计时一个阶段;异常照常上抛(finally 里仍记录耗时,便于定位失败前开销)。"""
    collector = _ACTIVE.get()
    if collector is None:
        yield
        return
    start_t = time.perf_counter()
    try:
        yield
    finally:
        collector.record(stage, start_t, time.perf_counter(), meta)
