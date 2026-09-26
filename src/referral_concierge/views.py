"""按权限组装的只读视图：患者、基层、县医院。"""

from __future__ import annotations

from typing import Any

from . import model as m
from .projection import CaseState
from .service import ClosedLoopService

# 基层视图不展示院内调度细节（窗口、抢占、容量、生命体征原文）。
_PRIMARY_HIDDEN_EVENTS = frozenset({m.RESOURCE_BOOKED, m.RESOURCE_RELEASED})
_PRIMARY_DROP_FIELDS = {
    m.OBSERVATION_RECORDED: ("vitals_summary",),
    m.URGENCY_ESCALATED: ("minimal_hold", "content_hash"),
}
_PRIMARY_HIDDEN_BREACHES = frozenset({m.BREACH_EXAM_WINDOW, m.BREACH_GREEN_HOLD})


def _public_payload(event: dict[str, Any]) -> dict[str, Any]:
    payload = dict(event.get("payload", {}))
    for field_name in _PRIMARY_DROP_FIELDS.get(event.get("event_type"), ()):
        payload.pop(field_name, None)
    return {k: v for k, v in payload.items() if k != "content_hash"}


def primary_view(service: ClosedLoopService, case_id: str) -> dict[str, Any] | None:
    """基层视图：来源事实、流转状态、下转与回访连续记录，不含院内调度细节。"""
    case = service.case(case_id)
    if case is None or case.referral is None:
        return None
    timeline = [
        {
            "event_id": e["event_id"],
            "event_type": e["event_type"],
            "occurred_at": e["occurred_at"],
            "payload": _public_payload(e),
        }
        for e in case.events
        if e["event_type"] not in _PRIMARY_HIDDEN_EVENTS
        and not (
            e["event_type"] == m.DEADLINE_BREACHED
            and e["payload"].get("kind") in _PRIMARY_HIDDEN_BREACHES
        )
    ]
    referral = case.referral
    return {
        "view": "primary",
        "case_id": case_id,
        "patient": {"patient_ref": referral["patient"]["patient_ref"], "name": referral["patient"]["name"]},
        "referring_facility": referral["referring_facility"],
        "primary_doctor_ref": referral["primary_doctor_ref"],
        "status": case_status(case),
        "timeline": timeline,
        "downward": _downward_summary(case),
        "review": _review_summary(case),
        "closed": case.closed,
    }


def hospital_view(service: ClosedLoopService, case_id: str) -> dict[str, Any] | None:
    """县医院视图：完整时间线、预约台账、升级依据与违约。"""
    case = service.case(case_id)
    if case is None or case.referral is None:
        return None
    bookings = sorted(
        (b for b in service.ledger().values() if b["case_id"] == case_id),
        key=lambda b: (b["booked_at"] or "", b["booking_ref"]),
    )
    return {
        "view": "hospital",
        "case_id": case_id,
        "patient": case.referral["patient"],
        "status": case_status(case),
        "timeline": [
            {
                "event_id": e["event_id"],
                "event_type": e["event_type"],
                "occurred_at": e["occurred_at"],
                "payload": {k: v for k, v in e["payload"].items() if k != "content_hash"},
            }
            for e in case.events
        ],
        "bookings": bookings,
        "conflicts": [c for c in service.conflicts() if _conflict_for_case(c, case_id)],
        "closed": case.closed,
    }


def case_status(case: CaseState) -> str:
    if not case.referral:
        return "未接收"
    if not case.arrival:
        return "待接站/未到院"
    if not case.observation:
        return "到院待分级"
    if case.active_escalation:
        return "危急升级待补依据"
    if not case.disposition:
        return "待专科处置"
    if case.downward and not case.handover:
        return "待下转交接"
    if case.open_items or (case.checklist and not case.followups):
        return "康复随访中"
    if case.closed:
        return "闭环完成"
    return "处置后流转中"


def patient_view(service: ClosedLoopService, case_id: str) -> dict[str, Any] | None:
    """患者视图：只告知下一步与责任联系人，不暴露临床细节。"""
    case = service.case(case_id)
    if case is None or case.referral is None:
        return None
    next_step, stage = _next_step(case)
    return {
        "view": "patient",
        "case_id": case_id,
        "patient_name": case.referral["patient"]["name"],
        "stage": stage,
        "next_step": next_step,
        "responsible_contact": _responsible_contact(case),
        "alerts": [
            {"kind": b["kind"], "due_at": b.get("due_at")}
            for b in case.breaches
            if b.get("kind") in {m.BREACH_PICKUP, m.BREACH_REVIEW_ITEM, m.BREACH_FOLLOWUP, m.BREACH_DOWNWARD}
        ],
        "closed": case.closed,
    }


def _responsible_contact(case: CaseState) -> dict[str, str]:
    if case.escort:
        return {
            "role": "concierge",
            "name": case.escort["responsible_contact"]["name"],
            "phone": case.escort["responsible_contact"]["phone"],
        }
    if not case.arrival and case.arrival_plan:
        return {"role": "greeter", "name": case.arrival_plan["greeter_ref"], "phone": ""}
    if case.arrival:
        return {"role": "assessor", "name": case.arrival["assessor_ref"], "phone": ""}
    return {"role": "primary_doctor", "name": case.referral["primary_doctor_ref"], "phone": ""}


def _next_step(case: CaseState) -> tuple[str, str]:
    if not case.arrival:
        if any(b.get("kind") == m.BREACH_PICKUP for b in case.breaches):
            return "接站期限已过仍未查到您的到院记录，请立即联系责任接站人", "接站逾期"
        return f"请按计划到达 {case.arrival_plan['pickup_point'] if case.arrival_plan else '县医院'}，接站人会与您联系", "待到院"
    if not case.observation:
        return "请在快速观察区等候现场分级", "到院待分级"
    if case.active_escalation:
        return "您的情况已按危急通道处理，请配合转诊管家与专科医生安排", "危急处理中"
    if not case.disposition:
        return "请在陪护人员陪同下完成检查并等待专科医生决定", "待专科处置"
    decision = case.disposition.get("decision")
    if not case.downward:
        if decision == "admit":
            return f"请办理住院，科室：{case.disposition.get('department', '')}", "住院"
        return f"离院前请领取康复与复查指导，科室：{case.disposition.get('department', '')}", "待离院"
    if not case.handover:
        return f"请按安排下转至 {case.downward['target_facility']}，转诊管家负责交接", "待下转"
    if case.open_items:
        next_item = min(case.open_items, key=lambda x: x.get("due_at") or "")
        return f"请按时完成复查：{next_item['title']}（截止 {next_item.get('due_at', '待通知')}）", "复查中"
    if case.checklist and not case.followups:
        return "请保持电话畅通，等待康复回访", "待回访"
    if case.followups:
        latest = case.followups[-1]
        return f"最近回访建议：{latest.get('next_action', '遵医嘱')}", "随访中"
    return "本转诊流程已完成", "闭环完成"


def _downward_summary(case: CaseState) -> dict[str, Any] | None:
    if not case.downward:
        return None
    planned = next(
        (e for e in case.events if e["event_type"] == m.DOWNWARD_PLANNED),
        None,
    )
    return {
        "target_facility": case.downward["target_facility"],
        "plan_summary": case.downward.get("plan_summary"),
        "planned_at": planned["occurred_at"] if planned else None,
        "handover_deadline": case.downward["handover_deadline"],
        "handover_confirmed_at": case.handover["confirmed_at"] if case.handover else None,
        "receiving_facility": case.handover["receiving_facility"] if case.handover else None,
    }


def _review_summary(case: CaseState) -> dict[str, Any]:
    return {
        "items": sorted(case.checklist.values(), key=lambda x: x["item_id"]),
        "followup_due_at": case.followup_due_at,
        "followups": [
            {"outcome": f.get("outcome"), "next_action": f.get("next_action"), "followed_at": f.get("followed_at")}
            for f in case.followups
        ],
        "rehab_continued": case.rehab_continued,
    }


def _conflict_for_case(record: dict[str, Any], case_id: str) -> bool:
    cmd = record.get("incoming_command", {})
    data = cmd.get("data", {})
    return data.get("case_id") == case_id or data.get("referral_no") == case_id
