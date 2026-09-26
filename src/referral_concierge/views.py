"""患者视图与分权记录视图。

- 患者看到下一步、责任联系人和与自己相关的期限。
- 基层看到来源事实、下转方案、复查清单与回访结果这一连续链条。
- 县医院看到完整记录；审计视角额外包含冲突条目。
"""

from __future__ import annotations

from typing import Any

from .service import ROLE_NAMES, Case, Service

VIEWERS = ("patient", "primary_clinic", "county_hospital", "auditor")

_NEXT_STEP = {
    "received": "等待转诊管家前置联系，请保持电话畅通",
    "contacted": "请按到达计划到院，转诊管家将在接站点等候",
    "arrived": "已完成到院确认，等待专科医生处置",
    "dispositioned": "专科医生已作出住院或离院决定",
    "returned": "已安排下转，请按复查清单在基层完成复查",
    "followup_done": "回访已完成，按康复指导继续休养",
    "cancelled": "本次转诊已取消，如需再次转诊请联系基层医生",
}


def _contact_for(case: Case) -> dict[str, str]:
    if case.escalated and "specialist" in case.contacts:
        role = "specialist"
    elif case.status in ("dispositioned", "returned") and "specialist" in case.contacts:
        role = "specialist"
    else:
        role = "concierge"
    return {"role": role, "role_name": ROLE_NAMES[role], "ref": case.contacts.get(role, case.concierge_ref)}


def patient_view(service: Service, case_id: str) -> dict[str, Any]:
    case = service.get_case(case_id)
    next_step = _NEXT_STEP[case.status]
    if case.escalated and case.escalation_basis_pending:
        next_step = "病情已危急升级，医院正在按绿色通道处置"
    deadlines = [
        {"kind": todo.kind, "due_at": todo.due_at.isoformat()}
        for todo in service.pending_todos(case_id)
    ]
    return {
        "case_id": case_id,
        "status": case.status,
        "escalated": case.escalated,
        "next_step": next_step,
        "contact": _contact_for(case),
        "arrival_plan": case.arrival_plan,
        "return_plan": case.return_plan["plan"] if case.return_plan else None,
        "checklist": [
            {"item_id": item["item_id"], "title": item["title"], "due_at": item["due_at"], "done": bool(item["done_at"])}
            for item in case.checklist
        ],
        "deadlines": deadlines,
    }


def record_view(service: Service, case_id: str, viewer: str) -> dict[str, Any]:
    if viewer not in VIEWERS:
        raise ValueError(f"未知查看方: {viewer}")
    if viewer == "patient":
        return patient_view(service, case_id)
    case = service.get_case(case_id)
    base: dict[str, Any] = {
        "case_id": case_id,
        "status": case.status,
        "patient": case.patient,
        "referral": case.referral,
        "confirmations": case.confirmations,
        "return_plan": case.return_plan,
        "checklist": case.checklist,
        "followups": case.followups,
        "cancelled": case.cancelled,
    }
    if viewer == "primary_clinic":
        return base
    base.update(
        {
            "precontact": case.precontact,
            "arrival_plan": case.arrival_plan,
            "observation": case.observation,
            "escalation": case.escalation,
            "disposition": case.disposition,
            "contacts": case.contacts,
        }
    )
    if viewer == "auditor":
        base["conflicts"] = service.conflicts(case_id)
    return base
