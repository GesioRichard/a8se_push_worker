"""Log estruturado: um objeto JSON por linha no stdout (journald)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any, TextIO


class JsonLog:
    def __init__(self, stream: TextIO | None = None, clock=None):
        self.stream = stream or sys.stdout
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.records: list[dict[str, Any]] = []

    def __call__(self, event: str, level: str = "info", **fields: Any) -> None:
        record = {"ts": self.clock().strftime("%Y-%m-%dT%H:%M:%SZ"), "level": level, "event": event}
        record.update(fields)
        self.records.append(record)
        self.stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self.stream.flush()


class MemoryLog(JsonLog):
    """Para testes: guarda os registros sem escrever em lugar nenhum."""

    def __init__(self):
        super().__init__(stream=_NullStream())

    def events(self, name: str | None = None) -> list[dict[str, Any]]:
        return [r for r in self.records if name is None or r["event"] == name]


class _NullStream:
    def write(self, _: str) -> int:
        return 0

    def flush(self) -> None:
        pass
