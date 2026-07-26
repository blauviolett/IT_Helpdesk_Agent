"""可观测 + 事件日志:每 case 一份 JSONL(兼作事件日志,无 events 表,冻结)。

记录节点、工具、决策、token/cost、latency、gate 结果等;由 runner / registry /
nodes 调用。文件名 = {case_id}.jsonl,append-only。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Tracer:
    def __init__(self, trace_dir: Path | str) -> None:
        self._dir = Path(trace_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    def event(self, case_id: str, kind: str, **fields: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}
        with open(self._dir / f"{case_id}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
