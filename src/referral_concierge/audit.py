"""审计还原：一次上转为何升级、资源如何协调、何时下转、康复是否接续。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .service import Service


def build_audit(service: Service, case_id: str) -> dict[str, Any]:
    case = service.get_case(case_id)
    trail = service.trail(case_id)
    notes = [e for e in trail if e["kind"] == "note"]
    events = [e["event"] for e in trail if e["kind"] == "event"]

    bookings: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    releases: list[dict[str, Any]] = []
    for note in notes:
        data = note["data"]
        if note["note_type"] == "resource_booked":
            bookings.append({"booking_id": data["booking_id"], "slot_ids": data["slot_ids"], "at": note["at"]})
        elif note["note_type"] == "resource_booking_failed":
            failures.append({"slot_ids": data["slot_ids"], "unavailable": data["unavailable"], "at": note["at"]})
        elif note["note_type"] == "bookings_released":
            releases.append({"reason": data["reason"], "released": data["released"],
                             "retained": data["retained"], "at": note["at"]})

    escalation = None
    if case.escalation:
        esc = case.escalation
        due = datetime.fromisoformat(esc["review_due_at"])
        basis_at = datetime.fromisoformat(esc["basis_at"]) if esc.get("basis_at") else None
        escalation = {
            "reason": esc["reason"],
            "by": esc["by"],
            "at": esc["at"],
            "review_due_at": esc["review_due_at"],
            "basis": esc.get("basis"),
            "basis_at": esc.get("basis_at"),
            "basis_in_time": bool(basis_at and basis_at <= due),
            "basis_overdue": not bool(basis_at and basis_at <= due) and service.now() > due,
            "emergency_booking": esc.get("emergency_booking"),
            "shortage": esc.get("shortage", []),
        }

    recheck_items = [
        {"item_id": item["item_id"], "title": item["title"], "due_at": item["due_at"],
         "result": item["result"], "done_at": item["done_at"]}
        for item in case.checklist
    ]
    rehab_continued = bool(case.checklist) and all(item["done_at"] for item in case.checklist) and bool(case.followups)

    return {
        "case_id": case_id,
        "status": case.status,
        "patient": case.patient,
        "referral": case.referral,
        "confirmations": case.confirmations,
        "observation": case.observation,
        "escalation": escalation,
        "resource_coordination": {"bookings": bookings, "failures": failures, "releases": releases},
        "disposition": case.disposition,
        "return_plan": case.return_plan,
        "recheck_items": recheck_items,
        "followups": case.followups,
        "rehab_continued": rehab_continued,
        "conflicts": service.conflicts(case_id),
        "events": events,
    }


def render_audit(report: dict[str, Any]) -> str:
    lines: list[str] = []
    patient = report["patient"]
    referral = report["referral"]
    lines.append(f"病例 {report['case_id']} ｜ 状态 {report['status']}")
    lines.append(f"患者 {patient.get('name')}（{patient.get('id_no')}） ｜ 来自 {referral.get('from_facility')}")
    lines.append(f"来源病情：{referral.get('condition_summary')}")
    source = report["confirmations"].get("source")
    if source:
        lines.append(f"来源事实确认：{source['by']} 于 {source['at']}")

    esc = report["escalation"]
    if esc:
        lines.append("危急升级：")
        lines.append(f"  原因：{esc['reason']}（{esc['by']} 于 {esc['at']}）")
        lines.append(f"  复核期限：{esc['review_due_at']}")
        if esc["basis"]:
            mark = "期限内" if esc["basis_in_time"] else "超期"
            lines.append(f"  依据：{esc['basis']}（{esc['basis_at']} 补全，{mark}）")
        else:
            lines.append("  依据：尚未补全" + ("，已超期" if esc["basis_overdue"] else ""))
        if esc["emergency_booking"]:
            lines.append(f"  最小必要资源：{esc['emergency_booking']}")
        if esc["shortage"]:
            lines.append(f"  资源缺口：{', '.join(esc['shortage'])}")
    else:
        lines.append("危急升级：无")

    coord = report["resource_coordination"]
    lines.append("资源协调：")
    if not (coord["bookings"] or coord["failures"] or coord["releases"]):
        lines.append("  无资源调度记录")
    for booking in coord["bookings"]:
        lines.append(f"  预订 {booking['booking_id']}：{', '.join(booking['slot_ids'])}（{booking['at']}）")
    for failure in coord["failures"]:
        lines.append(f"  争用失败：{', '.join(failure['slot_ids'])}，占用中 {', '.join(failure['unavailable'])}（{failure['at']}）")
    for release in coord["releases"]:
        kept = f"，保留 {', '.join(release['retained'])}" if release["retained"] else ""
        lines.append(f"  释放（{release['reason']}）：{', '.join(release['released']) or '无'}{kept}（{release['at']}）")

    disposition = report["disposition"]
    if disposition:
        lines.append(f"临床处置：{disposition['decision']}（{disposition['by']} 于 {disposition['at']}）")

    plan = report["return_plan"]
    if plan:
        detail = plan["plan"]
        lines.append(f"下转：{detail.get('to_facility')}，期限 {detail.get('transfer_by')}（{plan['by']} 于 {plan['at']}）")
    else:
        lines.append("下转：尚未安排")

    lines.append("康复接续：")
    if not report["recheck_items"]:
        lines.append("  无复查清单")
    for item in report["recheck_items"]:
        if item["done_at"]:
            lines.append(f"  复查 {item['item_id']} {item['title']}：{item['result']}（{item['done_at']}）")
        else:
            lines.append(f"  复查 {item['item_id']} {item['title']}：未完成，期限 {item['due_at']}")
    for follow in report["followups"]:
        lines.append(f"  回访：{follow['outcome']} ｜ 下一步 {follow['next_action']}（{follow['at']}）")
    lines.append("  结论：康复指导" + ("已真正接续" if report["rehab_continued"] else "尚未完全接续"))

    if report["conflicts"]:
        lines.append("冲突记录：")
        for conflict in report["conflicts"]:
            lines.append(f"  [{conflict['subject']}] {conflict['detail']}（{conflict['at']}）")
    return "\n".join(lines)
