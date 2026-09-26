"""期限待办调度器：接站、检查、升级复核、下转、复查、回访。

待办持久化到 JSON 文件，进程重启后重新加载即可继续到期判断；
时钟只负责回答“现在”，到期集合由 ``due`` 按当前时刻计算。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .clock import Clock

TODO_KINDS = ("pickup", "exam", "escalation_review", "downward", "recheck", "followup")


@dataclass(frozen=True)
class Todo:
    todo_id: str
    case_id: str
    kind: str
    due_at: datetime
    created_at: datetime
    done: bool = False
    done_at: datetime | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["due_at"] = self.due_at.isoformat()
        data["created_at"] = self.created_at.isoformat()
        data["done_at"] = self.done_at.isoformat() if self.done_at else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Todo":
        return cls(
            todo_id=data["todo_id"],
            case_id=data["case_id"],
            kind=data["kind"],
            due_at=datetime.fromisoformat(data["due_at"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            done=bool(data.get("done", False)),
            done_at=datetime.fromisoformat(data["done_at"]) if data.get("done_at") else None,
        )


class Scheduler:
    """按 (case_id, kind, 标的) 去重的期限登记处。"""

    def __init__(self, clock: Clock, path: Path | None = None) -> None:
        self._clock = clock
        self._path = Path(path) if path else None
        self._todos: dict[str, Todo] = {}
        if self._path and self._path.exists():
            for item in json.loads(self._path.read_text(encoding="utf-8")):
                todo = Todo.from_dict(item)
                self._todos[todo.todo_id] = todo

    def schedule(self, todo_id: str, case_id: str, kind: str, due_at: datetime) -> Todo:
        if kind not in TODO_KINDS:
            raise ValueError(f"未知待办类型: {kind}")
        if due_at.tzinfo is None or due_at.utcoffset() is None:
            raise ValueError("期限必须携带时区")
        existing = self._todos.get(todo_id)
        if existing is not None:
            return existing
        todo = Todo(todo_id=todo_id, case_id=case_id, kind=kind, due_at=due_at, created_at=self._clock.now())
        self._todos[todo_id] = todo
        self._save()
        return todo

    def complete(self, todo_id: str) -> Todo | None:
        todo = self._todos.get(todo_id)
        if todo is None or todo.done:
            return todo
        done = Todo(
            todo_id=todo.todo_id,
            case_id=todo.case_id,
            kind=todo.kind,
            due_at=todo.due_at,
            created_at=todo.created_at,
            done=True,
            done_at=self._clock.now(),
        )
        self._todos[todo_id] = done
        self._save()
        return done

    def due(self, case_id: str | None = None) -> list[Todo]:
        """当前已到期的未完成待办，按期限升序。"""
        now = self._clock.now()
        items = [
            todo
            for todo in self._todos.values()
            if not todo.done and todo.due_at <= now and (case_id is None or todo.case_id == case_id)
        ]
        return sorted(items, key=lambda todo: (todo.due_at, todo.todo_id))

    def pending(self, case_id: str | None = None) -> list[Todo]:
        items = [todo for todo in self._todos.values() if not todo.done and (case_id is None or todo.case_id == case_id)]
        return sorted(items, key=lambda todo: (todo.due_at, todo.todo_id))

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [todo.to_dict() for todo in sorted(self._todos.values(), key=lambda t: t.todo_id)]
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
