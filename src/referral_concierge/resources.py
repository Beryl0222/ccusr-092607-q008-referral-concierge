"""检查窗口与陪护人员调度：容量台账、冻结规则、危急抢占。

资源统一建模为「资源 + 时间窗口 + 容量」：检查设备窗口容量通常为 1，
陪护班次容量可大于 1。绿色通道资源可被普通案例预约，但受最长持有期
限制，超期由服务层时钟释放，防止普通陪诊长期占用稀缺通道。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .model import Policy, parse_dt


@dataclass
class SlotDef:
    resource_id: str
    slot_id: str
    start_at: datetime
    end_at: datetime
    capacity: int


@dataclass
class Plan:
    ok: bool
    code: str = ""
    message: str = ""
    bookings: list[dict[str, Any]] = field(default_factory=list)
    preemptions: list[dict[str, Any]] = field(default_factory=list)


class Scheduler:
    def __init__(
        self,
        inventory: dict[str, Any],
        ledger: dict[str, dict[str, Any]],
        policy: Policy,
        now: datetime,
    ) -> None:
        self.policy = policy
        self.now = now
        self._green: set[str] = set()
        self._names: dict[str, str] = {}
        self._slots: dict[tuple[str, str], SlotDef] = {}
        for res in inventory.get("resources", []):
            rid = res["resource_id"]
            self._names[rid] = res.get("name", rid)
            if res.get("green_channel"):
                self._green.add(rid)
            for slot in res.get("slots", []):
                self._slots[(rid, slot["slot_id"])] = SlotDef(
                    resource_id=rid,
                    slot_id=slot["slot_id"],
                    start_at=parse_dt(slot["start_at"]),
                    end_at=parse_dt(slot["end_at"]),
                    capacity=int(slot.get("capacity", 1)),
                )
        self._ledger = ledger

    def _active(self, resource_id: str, slot_id: str) -> list[dict[str, Any]]:
        return [
            b
            for b in self._ledger.values()
            if b["status"] == "active"
            and b["resource_id"] == resource_id
            and b["slot_id"] == slot_id
        ]

    def _frozen(self, booking: dict[str, Any]) -> bool:
        slot = self._slots.get((booking["resource_id"], booking["slot_id"]))
        if slot is None:
            return False
        return slot.start_at - self.now <= self.policy.freeze_horizon

    def plan_bookings(
        self,
        case_id: str,
        requests: list[dict[str, Any]],
        escalated: bool,
        hold_kind: str,
        protected_case_ids: frozenset[str] = frozenset(),
    ) -> Plan:
        """原子试算：全部窗口可安排才返回预约与抢占清单，否则整体失败。

        冻结规则：窗口开始前进入冻结期的预约不可被抢占；危急案例可抢占
        普通案例中未冻结的预约；已升级案例与危急最小占用不可被抢占。
        """
        if not requests:
            return Plan(False, "empty_requests", "预约请求为空")
        seen: set[tuple[str, str]] = set()
        tentative_preempt: set[str] = set()
        bookings: list[dict[str, Any]] = []
        preemptions: list[dict[str, Any]] = []
        for req in requests:
            rid = req.get("resource_id")
            sid = req.get("slot_id")
            ref = req.get("booking_ref")
            key = (rid, sid)
            if not isinstance(rid, str) or not isinstance(sid, str) or not isinstance(ref, str):
                return Plan(False, "bad_request", "预约项必须包含 booking_ref/resource_id/slot_id")
            if key in seen:
                return Plan(False, "duplicate_slot", f"同一窗口重复预约: {rid}/{sid}")
            seen.add(key)
            slot = self._slots.get(key)
            if slot is None:
                return Plan(False, "unknown_slot", f"资源窗口不存在: {rid}/{sid}")
            if slot.end_at <= self.now:
                return Plan(False, "window_elapsed", f"窗口已过: {rid}/{sid}")
            active = self._active(rid, sid)  # type: ignore[arg-type]
            others = [b for b in active if b["case_id"] != case_id]
            own = [b for b in active if b["case_id"] == case_id]
            if own:
                return Plan(False, "already_booked", f"本案例已占用窗口: {rid}/{sid}")
            free = slot.capacity - len(others) + sum(
                1 for b in others if b["booking_ref"] in tentative_preempt
            )
            if free <= 0:
                if not escalated:
                    return Plan(False, "slot_full", f"窗口已满且不可抢占: {rid}/{sid}")
                victims = sorted(
                    (
                        b
                        for b in others
                        if b["booking_ref"] not in tentative_preempt
                        and b["case_id"] not in protected_case_ids
                        and b.get("hold_kind") != "escalation_minimal"
                        and not self._frozen(b)
                    ),
                    key=lambda b: (b["booked_at"] or "", b["case_id"], b["booking_ref"]),
                )
                if not victims:
                    return Plan(False, "slot_frozen_full", f"窗口已满且剩余预约均在冻结期: {rid}/{sid}")
                victim = victims[0]
                tentative_preempt.add(victim["booking_ref"])
                preemptions.append(
                    {
                        "booking_ref": victim["booking_ref"],
                        "case_id": victim["case_id"],
                        "resource_id": rid,
                        "slot_id": sid,
                    }
                )
                free += 1
            if free <= 0:
                return Plan(False, "slot_full", f"窗口容量不足: {rid}/{sid}")
            bookings.append(
                {
                    "booking_ref": ref,
                    "resource_id": rid,
                    "resource_name": self._names.get(rid, rid),
                    "slot_id": sid,
                    "start_at": slot.start_at.isoformat(),
                    "end_at": slot.end_at.isoformat(),
                    "green_channel": rid in self._green,
                    "hold_kind": hold_kind,
                }
            )
        return Plan(True, bookings=bookings, preemptions=preemptions)
