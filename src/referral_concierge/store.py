"""持久化：追加式事件日志与回执、冲突、待办、资源台账文件。

事件日志（events.jsonl）是唯一事实来源；回执、待办等 JSON 文件采用
临时文件原子替换写入。重启后从事件日志重建状态，待办继续有效。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.events_path = self.root / "events.jsonl"
        self.receipts_path = self.root / "receipts.json"
        self.conflicts_path = self.root / "conflicts.jsonl"
        self.pending_path = self.root / "pending.json"
        self.resources_path = self.root / "resources.json"
        self.meta_path = self.root / "meta.json"

    @classmethod
    def initialize(
        cls,
        root: str | Path,
        resources: dict[str, Any] | None = None,
        policy: dict[str, Any] | None = None,
    ) -> "Store":
        store = cls(root)
        store.root.mkdir(parents=True, exist_ok=True)
        if not store.events_path.exists():
            store.events_path.write_text("", encoding="utf-8")
        if not store.receipts_path.exists():
            store._write_json(store.receipts_path, {})
        if not store.conflicts_path.exists():
            store.conflicts_path.write_text("", encoding="utf-8")
        if not store.pending_path.exists():
            store._write_json(store.pending_path, [])
        if resources is not None:
            store.save_resources(resources)
        if not store.meta_path.exists():
            store.save_meta({"last_tick_at": None, "policy": policy or {}})
        return store

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        text = path.read_text(encoding="utf-8").strip()
        return json.loads(text) if text else default

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows

    @staticmethod
    def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with path.open("a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def load_events(self) -> list[dict[str, Any]]:
        return self._read_jsonl(self.events_path)

    def append_events(self, events: list[dict[str, Any]]) -> None:
        self._append_jsonl(self.events_path, events)

    def load_receipts(self) -> dict[str, Any]:
        return self._read_json(self.receipts_path, {})

    def save_receipts(self, receipts: dict[str, Any]) -> None:
        self._write_json(self.receipts_path, receipts)

    def load_conflicts(self) -> list[dict[str, Any]]:
        return self._read_jsonl(self.conflicts_path)

    def append_conflict(self, record: dict[str, Any]) -> None:
        self._append_jsonl(self.conflicts_path, [record])

    def load_pending(self) -> list[dict[str, Any]]:
        return self._read_json(self.pending_path, [])

    def save_pending(self, pending: list[dict[str, Any]]) -> None:
        self._write_json(self.pending_path, pending)

    def load_resources(self) -> dict[str, Any]:
        return self._read_json(self.resources_path, {"resources": []})

    def save_resources(self, inventory: dict[str, Any]) -> None:
        self._write_json(self.resources_path, inventory)

    def load_meta(self) -> dict[str, Any]:
        return self._read_json(self.meta_path, {"last_tick_at": None, "policy": {}})

    def save_meta(self, meta: dict[str, Any]) -> None:
        self._write_json(self.meta_path, meta)
