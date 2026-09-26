"""审计还原：一次上转为何升级、资源如何协调、何时下转、康复是否接续。"""

from __future__ import annotations

from typing import Any

from . import model as m
from .projection import CaseState
from .service import ClosedLoopService
from .views import _conflict_for_case

_SUMMARY = {
    m.REFERRAL_RECEIVED: "基层转诊单入账",
    m.PREARRIVAL_CONTACTED: "到院前联系患者",
    m.ARRIVAL_PLANNED: "登记到达计划与接站安排",
    m.ARRIVAL_CONFIRMED: "患者到院确认",
    m.OBSERVATION_RECORDED: "现场快速观察与分级",
    m.URGENCY_ESCALATED: "危急升级，限期补全依据",
    m.ESCALATION_JUSTIFIED: "专科医生补全升级依据",
    m.RESOURCE_BOOKED: "原子预约检查/陪护资源",
    m.RESOURCE_RELEASED: "释放预约",
    m.ESCORT_ASSIGNED: "指定陪护责任与联系人",
    m.DISPOSITION_DECIDED: "住院或离院决定",
    m.DOWNWARD_PLANNED: "登记下转方案",
    m.DOWNWARD_HANDOVER_CONFIRMED: "下转交接确认",
    m.REVIEW_CHECKLIST_ISSUED: "开具复查清单",
    m.FOLLOWUP_COMPLETED: "登记回访结果",
    m.DEADLINE_BREACHED: "期限违约",
}


def build_audit(service: ClosedLoopService, case_id: str) -> dict[str, Any] | None:
    case = service.case(case_id)
    if case is None or case.referral is None:
        return None
    return {
        "case_id": case_id,
        "patient": case.referral["patient"],
        "generated_at": m.iso(service.clock.now()),
        "timeline": [_timeline_row(e) for e in case.events],
        "escalation": _escalation_audit(case),
        "resources": _resource_audit(service, case),
        "downward": _downward_audit(case),
        "rehab": _rehab_audit(case),
        "conflicts": [c for c in service.conflicts() if _conflict_for_case(c, case_id)],
    }


def _timeline_row(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload", {})
    return {
        "version": event["version"],
        "event_id": event["event_id"],
        "event_type": event["event_type"],
        "occurred_at": event["occurred_at"],
        "actor_role": payload.get("actor_role"),
        "actor_ref": payload.get("actor_ref"),
        "summary": _SUMMARY.get(event["event_type"], event["event_type"]),
    }


def _escalation_audit(case: CaseState) -> dict[str, Any]:
    """为何升级、谁发起、依据是否在期限内补全。"""
    rows = []
    for esc in case.escalations:
        payload = esc["payload"]
        overdue = any(
            b.get("kind") == m.BREACH_ESCALATION_REVIEW and b.get("ref") == esc["event_id"]
            for b in case.breaches
        )
        rows.append(
            {
                "event_id": esc["event_id"],
                "reason": payload.get("reason"),
                "level": payload.get("level"),
                "raised_by": {"role": payload.get("actor_role"), "ref": payload.get("actor_ref")},
                "review_due_at": payload.get("review_due_at"),
                "minimal_hold": payload.get("minimal_hold", {}).get("booked", []),
                "justified": esc["justified"],
                "justification": esc.get("justification"),
                "justified_at": esc.get("justified_at"),
                "justified_in_time": bool(
                    esc["justified"]
                    and esc.get("justified_at")
                    and m.parse_dt(esc["justified_at"]) <= m.parse_dt(payload["review_due_at"])
                ),
                "overdue_without_justification": overdue,
            }
        )
    return {"escalated": bool(rows), "records": rows}


def _resource_audit(service: ClosedLoopService, case: CaseState) -> dict[str, Any]:
    """资源如何协调：预约、释放原因、抢占关系。"""
    bookings = sorted(
        (b for b in service.ledger().values() if b["case_id"] == case.case_id),
        key=lambda b: (b["booked_at"] or "", b["booking_ref"]),
    )
    preempted_others = [
        {
            "event_id": e["event_id"],
            "occurred_at": e["occurred_at"],
            "preempted": e["payload"].get("preempted", []),
        }
        for e in case.events
        if e["event_type"] == m.RESOURCE_BOOKED and e["payload"].get("preempted")
    ]
    return {
        "bookings": [
            {
                "booking_ref": b["booking_ref"],
                "resource": b.get("resource_name", b["resource_id"]),
                "slot_id": b["slot_id"],
                "window": {"start": b.get("start_at"), "end": b.get("end_at")},
                "green_channel": b.get("green_channel", False),
                "hold_kind": b.get("hold_kind"),
                "status": b["status"],
                "release_reason": b.get("release_reason"),
                "released_at": b.get("released_at"),
            }
            for b in bookings
        ],
        "preempted_by_this_case": preempted_others,
    }


def _downward_audit(case: CaseState) -> dict[str, Any]:
    """何时下转、是否按期交接。"""
    if not case.downward:
        return {"planned": False}
    planned = next((e for e in case.events if e["event_type"] == m.DOWNWARD_PLANNED), None)
    breached = any(b.get("kind") == m.BREACH_DOWNWARD for b in case.breaches)
    return {
        "planned": True,
        "planned_at": planned["occurred_at"] if planned else None,
        "target_facility": case.downward["target_facility"],
        "handover_deadline": case.downward["handover_deadline"],
        "handover_confirmed_at": case.handover["confirmed_at"] if case.handover else None,
        "handover_overdue": breached,
    }


def _rehab_audit(case: CaseState) -> dict[str, Any]:
    """康复指导是否真正接续：复查核销 + 回访落实。"""
    items = sorted(case.checklist.values(), key=lambda x: x["item_id"])
    if not items:
        verdict = "未开具复查清单"
    elif case.open_items:
        verdict = f"复查项目未全部完成（剩余 {len(case.open_items)} 项）"
    elif not case.followups:
        verdict = "复查已完成但回访未落实"
    else:
        verdict = "康复指导已接续：复查全部核销且回访已落实"
    return {
        "checklist": items,
        "followup_due_at": case.followup_due_at,
        "followups": [
            {
                "outcome": f.get("outcome"),
                "next_action": f.get("next_action"),
                "followed_at": f.get("followed_at"),
                "completed_review_items": f.get("completed_review_items", []),
            }
            for f in case.followups
        ],
        "continued": case.rehab_continued,
        "verdict": verdict,
    }
