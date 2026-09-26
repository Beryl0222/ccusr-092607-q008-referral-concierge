"""案例投影：从事件日志重建单个转诊案例的当前状态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import model as m


@dataclass
class CaseState:
    """一次上转的闭环状态；所有字段均可由事件流重放得到。"""

    case_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    referral: dict[str, Any] | None = None
    contact: dict[str, Any] | None = None
    arrival_plan: dict[str, Any] | None = None
    arrival: dict[str, Any] | None = None
    observations: list[dict[str, Any]] = field(default_factory=list)
    escalations: list[dict[str, Any]] = field(default_factory=list)
    escort: dict[str, Any] | None = None
    disposition: dict[str, Any] | None = None
    downward: dict[str, Any] | None = None
    handover: dict[str, Any] | None = None
    checklist: dict[str, dict[str, Any]] = field(default_factory=dict)
    followup_due_at: str | None = None
    followups: list[dict[str, Any]] = field(default_factory=list)
    breaches: list[dict[str, Any]] = field(default_factory=list)

    def apply(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        payload = event.get("payload", {})
        etype = event.get("event_type")
        if etype == m.REFERRAL_RECEIVED:
            self.referral = payload
        elif etype == m.PREARRIVAL_CONTACTED:
            self.contact = payload
        elif etype == m.ARRIVAL_PLANNED:
            self.arrival_plan = payload
        elif etype == m.ARRIVAL_CONFIRMED:
            self.arrival = payload
        elif etype == m.OBSERVATION_RECORDED:
            self.observations.append(payload)
        elif etype == m.URGENCY_ESCALATED:
            self.escalations.append(
                {"event_id": event["event_id"], "payload": payload, "justified": False, "justification": None}
            )
        elif etype == m.ESCALATION_JUSTIFIED:
            target = payload.get("escalation_ref")
            for esc in self.escalations:
                if esc["event_id"] == target or (target is None and not esc["justified"]):
                    esc["justified"] = True
                    esc["justification"] = payload.get("justification")
                    esc["justified_at"] = event.get("occurred_at")
        elif etype == m.ESCORT_ASSIGNED:
            self.escort = payload
        elif etype == m.DISPOSITION_DECIDED:
            self.disposition = payload
        elif etype == m.DOWNWARD_PLANNED:
            self.downward = payload
        elif etype == m.DOWNWARD_HANDOVER_CONFIRMED:
            self.handover = payload
        elif etype == m.REVIEW_CHECKLIST_ISSUED:
            for item in payload.get("items", []):
                existing = self.checklist.get(item["item_id"], {})
                self.checklist[item["item_id"]] = {
                    "item_id": item["item_id"],
                    "title": item.get("title", ""),
                    "due_at": item.get("due_at"),
                    "completed": existing.get("completed", False),
                    "completed_at": existing.get("completed_at"),
                }
            self.followup_due_at = payload.get("followup_due_at")
        elif etype == m.FOLLOWUP_COMPLETED:
            self.followups.append(payload)
            for item_id in payload.get("completed_review_items", []):
                if item_id in self.checklist:
                    self.checklist[item_id]["completed"] = True
                    self.checklist[item_id]["completed_at"] = payload.get("followed_at")
        elif etype == m.DEADLINE_BREACHED:
            self.breaches.append(payload)

    @property
    def version(self) -> int:
        return len(self.events)

    @property
    def observation(self) -> dict[str, Any] | None:
        return self.observations[-1] if self.observations else None

    @property
    def active_escalation(self) -> dict[str, Any] | None:
        for esc in reversed(self.escalations):
            if not esc["justified"]:
                return esc
        return None

    @property
    def escalated(self) -> bool:
        """已升级且尚未完成处置的案例保持危急优先级。"""
        return bool(self.escalations) and self.disposition is None

    @property
    def open_items(self) -> list[dict[str, Any]]:
        return [item for item in self.checklist.values() if not item["completed"]]

    @property
    def closed(self) -> bool:
        if not self.disposition:
            return False
        if self.downward and not self.handover:
            return False
        if self.open_items:
            return False
        if self.checklist and not self.followups:
            return False
        return True

    @property
    def rehab_continued(self) -> bool:
        """康复指导真正接续：复查项目全部核销且完成过回访。"""
        return bool(self.checklist) and not self.open_items and bool(self.followups)

    def breached(self, kind: str, ref: str) -> bool:
        return any(b.get("kind") == kind and b.get("ref") == ref for b in self.breaches)


def build_cases(events: list[dict[str, Any]]) -> dict[str, CaseState]:
    cases: dict[str, CaseState] = {}
    for event in events:
        case_id = event["aggregate_id"]
        cases.setdefault(case_id, CaseState(case_id)).apply(event)
    return cases


def apply_booking_event(ledger: dict[str, dict[str, Any]], event: dict[str, Any]) -> None:
    """把单条预约/释放事件并入台账。"""
    payload = event.get("payload", {})
    if event.get("event_type") == m.RESOURCE_BOOKED:
        for booking in payload.get("bookings", []):
            ledger[booking["booking_ref"]] = {
                **booking,
                "case_id": event["aggregate_id"],
                "booked_at": event.get("occurred_at"),
                "status": "active",
                "release_reason": None,
                "released_at": None,
            }
    elif event.get("event_type") == m.RESOURCE_RELEASED:
        for release in payload.get("releases", []):
            booking = ledger.get(release["booking_ref"])
            if booking and booking["status"] == "active":
                booking["status"] = "released"
                booking["release_reason"] = payload.get("reason")
                booking["released_at"] = event.get("occurred_at")


def build_resource_ledger(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """全部案例的预约台账：booking_ref -> 预约（含释放状态）。"""
    ledger: dict[str, dict[str, Any]] = {}
    for event in events:
        apply_booking_event(ledger, event)
    return ledger
