"""持久化:Store Protocol + SQLiteStore。

单表 cases(case_id PK, state_json, updated_at),无 events 表(事件由 trace JSONL 承载)。
Protocol 抽象使替换后端 < 1h(Postgres 等列为 future)。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from helpdesk.state.models import CaseState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id    TEXT PRIMARY KEY,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


class Store(Protocol):
    def save(self, state: CaseState) -> None: ...

    def get(self, case_id: str) -> CaseState | None: ...


class SQLiteStore:
    def __init__(self, db_path: Path | str) -> None:
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def save(self, state: CaseState) -> None:
        state.updated_at = datetime.now(timezone.utc)
        self._conn.execute(
            "INSERT OR REPLACE INTO cases (case_id, state_json, updated_at) VALUES (?, ?, ?)",
            (state.case_id, state.model_dump_json(), state.updated_at.isoformat()),
        )
        self._conn.commit()

    def get(self, case_id: str) -> CaseState | None:
        row = self._conn.execute(
            "SELECT state_json FROM cases WHERE case_id = ?", (case_id,)
        ).fetchone()
        return CaseState.model_validate_json(row[0]) if row else None

    def close(self) -> None:
        self._conn.close()
