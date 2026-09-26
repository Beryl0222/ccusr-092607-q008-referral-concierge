"""检查窗口与陪护人员的资源池。

- 预订是原子的：一次请求的所有槽位要么全部占用，要么全部不占用，
  多患者争用时不会出现占一半的死锁状态。
- 冻结规则：槽位开始前的 ``freeze_lead`` 时间内进入冻结，
  冻结中的预约不可释放、不可改派，防止临开场被挤占。
- 患者未到或需求变化时，只释放未使用且未冻结的预约。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .clock import Clock

RESOURCE_KINDS = ("exam_window", "escort")


@dataclass(frozen=True)
class Slot:
    slot_id: str
    kind: str
    start: datetime
    end: datetime
    capacity: int = 1

    def to_dict(self) -> dict:
        data = asdict(self)
        data["start"] = self.start.isoformat()
        data["end"] = self.end.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Slot":
        return cls(
            slot_id=data["slot_id"],
            kind=data["kind"],
            start=datetime.fromisoformat(data["start"]),
            end=datetime.fromisoformat(data["end"]),
            capacity=int(data.get("capacity", 1)),
        )


@dataclass(frozen=True)
class Booking:
    booking_id: str
    case_id: str
    slot_ids: tuple[str, ...]
    created_at: datetime
    released: bool = False
    used: bool = False

    def to_dict(self) -> dict:
        data = asdict(self)
        data["slot_ids"] = list(self.slot_ids)
        data["created_at"] = self.created_at.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Booking":
        return cls(
            booking_id=data["booking_id"],
            case_id=data["case_id"],
            slot_ids=tuple(data["slot_ids"]),
            created_at=datetime.fromisoformat(data["created_at"]),
            released=bool(data.get("released", False)),
            used=bool(data.get("used", False)),
        )


class BookingError(Exception):
    """原子预订失败：无任何槽位被占用。"""

    def __init__(self, message: str, unavailable: list[str]) -> None:
        super().__init__(message)
        self.unavailable = unavailable


class FrozenError(Exception):
    """冻结期内拒绝变更。"""


class ResourcePool:
    def __init__(self, clock: Clock, freeze_lead: timedelta, path: Path | None = None) -> None:
        self._clock = clock
        self._freeze_lead = freeze_lead
        self._path = Path(path) if path else None
        self._slots: dict[str, Slot] = {}
        self._bookings: dict[str, Booking] = {}
        if self._path and self._path.exists():
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for item in data.get("slots", []):
                slot = Slot.from_dict(item)
                self._slots[slot.slot_id] = slot
            for item in data.get("bookings", []):
                booking = Booking.from_dict(item)
                self._bookings[booking.booking_id] = booking

    def add_slot(self, slot: Slot) -> None:
        if slot.kind not in RESOURCE_KINDS:
            raise ValueError(f"未知资源类型: {slot.kind}")
        if slot.end <= slot.start:
            raise ValueError("槽位结束时间必须晚于开始时间")
        if slot.capacity < 1:
            raise ValueError("槽位容量必须为正数")
        self._slots[slot.slot_id] = slot
        self._save()

    def slot(self, slot_id: str) -> Slot:
        return self._slots[slot_id]

    def is_frozen(self, slot_id: str) -> bool:
        slot = self._slots[slot_id]
        return self._clock.now() >= slot.start - self._freeze_lead

    def remaining(self, slot_id: str) -> int:
        slot = self._slots[slot_id]
        held = sum(
            1
            for booking in self._bookings.values()
            if not booking.released and slot_id in booking.slot_ids
        )
        return slot.capacity - held

    def book(self, booking_id: str, case_id: str, slot_ids: list[str]) -> Booking:
        """全有或全无：任一槽位不可用则整体失败。"""
        existing = self._bookings.get(booking_id)
        if existing is not None:
            if existing.case_id == case_id and list(existing.slot_ids) == list(slot_ids):
                return existing
            raise BookingError(f"预约标识 {booking_id} 已被其他请求占用", [])
        unknown = [sid for sid in slot_ids if sid not in self._slots]
        if unknown:
            raise BookingError(f"槽位不存在: {', '.join(unknown)}", unknown)
        unavailable = [sid for sid in slot_ids if self.remaining(sid) < 1]
        if unavailable:
            raise BookingError(f"槽位余量不足: {', '.join(unavailable)}", unavailable)
        booking = Booking(
            booking_id=booking_id,
            case_id=case_id,
            slot_ids=tuple(slot_ids),
            created_at=self._clock.now(),
        )
        self._bookings[booking_id] = booking
        self._save()
        return booking

    def mark_used(self, booking_id: str) -> Booking:
        booking = self._require(booking_id)
        updated = Booking(
            booking_id=booking.booking_id,
            case_id=booking.case_id,
            slot_ids=booking.slot_ids,
            created_at=booking.created_at,
            released=booking.released,
            used=True,
        )
        self._bookings[booking_id] = updated
        self._save()
        return updated

    def release(self, booking_id: str) -> Booking:
        """释放单个预约；任一槽位处于冻结期则拒绝。"""
        booking = self._require(booking_id)
        if booking.released:
            return booking
        frozen = [sid for sid in booking.slot_ids if self.is_frozen(sid)]
        if frozen:
            raise FrozenError(f"槽位已冻结，不可释放: {', '.join(frozen)}")
        return self._set_released(booking)

    def release_unused_for_case(self, case_id: str) -> list[str]:
        """患者未到或需求变化：释放该病例未使用且未冻结的预约。

        返回实际释放的预约标识；已冻结或已使用的预约保留原状。
        """
        released: list[str] = []
        for booking in sorted(self._bookings.values(), key=lambda b: b.booking_id):
            if booking.case_id != case_id or booking.released or booking.used:
                continue
            if any(self.is_frozen(sid) for sid in booking.slot_ids):
                continue
            self._set_released(booking)
            released.append(booking.booking_id)
        return released

    def bookings_for(self, case_id: str, active_only: bool = True) -> list[Booking]:
        items = [b for b in self._bookings.values() if b.case_id == case_id and (not active_only or not b.released)]
        return sorted(items, key=lambda b: b.booking_id)

    def _set_released(self, booking: Booking) -> Booking:
        updated = Booking(
            booking_id=booking.booking_id,
            case_id=booking.case_id,
            slot_ids=booking.slot_ids,
            created_at=booking.created_at,
            released=True,
            used=booking.used,
        )
        self._bookings[booking.booking_id] = updated
        self._save()
        return updated

    def _require(self, booking_id: str) -> Booking:
        booking = self._bookings.get(booking_id)
        if booking is None:
            raise KeyError(f"预约不存在: {booking_id}")
        return booking

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "slots": [slot.to_dict() for slot in sorted(self._slots.values(), key=lambda s: s.slot_id)],
            "bookings": [b.to_dict() for b in sorted(self._bookings.values(), key=lambda b: b.booking_id)],
        }
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
