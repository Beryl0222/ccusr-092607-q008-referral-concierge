"""可控时钟：测试可手动推进，生产用系统时钟。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """返回带时区的当前时间。"""

    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """手动时钟，只能前进，保证期限判断可复现。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("手动时钟起点必须携带时区")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("时间必须携带时区")
        if moment < self._now:
            raise ValueError("时钟不能回拨")
        self._now = moment

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now
