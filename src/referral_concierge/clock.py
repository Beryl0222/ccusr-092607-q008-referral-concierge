"""可控时钟：测试可手动推进，运行时使用系统时间。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .model import parse_dt


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class ManualClock:
    """测试与演练用手动时钟，只能前进。"""

    current: datetime

    @classmethod
    def at(cls, value: str) -> "ManualClock":
        return cls(parse_dt(value))

    def now(self) -> datetime:
        return self.current

    def set(self, value: datetime) -> None:
        if value < self.current:
            raise ValueError("时钟不得回拨")
        self.current = value

    def advance(self, delta: timedelta) -> None:
        self.set(self.current + delta)
