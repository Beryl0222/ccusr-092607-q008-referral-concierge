"""追加式台账：所有事实、回执与冲突先落账，再推进状态。

台账条目按行存储 JSON，重启后逐行重放即可重建病例状态；
条目一旦写入不修改，离线消息乱序重放时靠业务键幂等兜底。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

ENTRY_KINDS = ("event", "note", "receipt", "conflict")


class Ledger:
    def __init__(self, path: Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._entries: list[dict[str, Any]] = []
        if self._path and self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self._entries.append(json.loads(line))

    def append(self, kind: str, body: dict[str, Any]) -> dict[str, Any]:
        if kind not in ENTRY_KINDS:
            raise ValueError(f"未知台账条目类型: {kind}")
        entry = {"kind": kind, **body}
        self._entries.append(entry)
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def entries(self, kind: str | None = None) -> Iterator[dict[str, Any]]:
        for entry in self._entries:
            if kind is None or entry.get("kind") == kind:
                yield entry
