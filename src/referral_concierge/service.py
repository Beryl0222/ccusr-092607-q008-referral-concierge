"""县域转诊陪护闭环服务。

贯通基层转诊单、前置联系、到达计划、现场观察、资源协调、陪护责任、
住院或离院决定、下转方案、复查清单与回访结果。

规则要点：

- 角色分权：基层医生确认来源事实，转诊管家确认现场情况，专科医生确认
  临床处置；任何角色都不能替其他角色补写结论，已登记的结论不可改写。
- 幂等回执：相同业务键且内容一致的消息沿用首次回执，不重复推进状态；
  离线消息乱序重放是安全的。
- 冲突隔离：同一业务键内容不一致，或病情、时间、患者身份与已登记事实
  冲突时，登记冲突条目，绝不自动合并。
- 危急升级：允许先占用最小必要资源，复核期限前补全临床依据。
- 患者未到或需求变化：释放未使用且未冻结的预约，已冻结的保留。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from .clock import Clock, SystemClock
from .contracts import validate_event
from .ledger import Ledger
from .resources import BookingError, ResourcePool, Slot
from .scheduler import Scheduler, Todo

ROLES = ("primary_doctor", "concierge", "specialist")

ROLE_NAMES = {
    "primary_doctor": "基层医生",
    "concierge": "转诊管家",
    "specialist": "专科医生",
}

# 每类确认唯一允许的书写角色：任何人都不能替其他角色补写结论。
CONFIRMATION_ROLES = {
    "source": "primary_doctor",
    "arrival": "concierge",
    "clinical": "specialist",
}

_EVENT_AGGREGATE = {
    "REFERRAL_RECEIVED": "referral_order",
    "ARRIVAL_CONFIRMED": "arrival_assessment",
    "URGENCY_ESCALATED": "care_coordination",
    "RESOURCE_BOOKED": "care_coordination",
    "FOLLOWUP_COMPLETED": "return_plan",
}

DEFAULT_FREEZE_LEAD = timedelta(minutes=30)
DEFAULT_REVIEW_WITHIN = timedelta(minutes=30)
DEFAULT_FOLLOWUP_WITHIN = timedelta(hours=72)


@dataclass(frozen=True)
class Receipt:
    key: str
    status: str  # applied | rejected | conflict
    detail: str


@dataclass
class Case:
    case_id: str
    patient: dict[str, Any]
    referral: dict[str, Any]
    concierge_ref: str
    created_at: str
    contacts: dict[str, str] = field(default_factory=dict)
    confirmations: dict[str, dict[str, str]] = field(default_factory=dict)
    precontact: dict[str, Any] | None = None
    arrival_plan: dict[str, Any] | None = None
    observation: dict[str, Any] | None = None
    escalation: dict[str, Any] | None = None
    disposition: dict[str, Any] | None = None
    return_plan: dict[str, Any] | None = None
    checklist: list[dict[str, Any]] = field(default_factory=list)
    followups: list[dict[str, Any]] = field(default_factory=list)
    cancelled: dict[str, Any] | None = None

    @property
    def status(self) -> str:
        """状态由已登记事实推导，乱序到达的消息不会改变推导规则。"""
        if self.cancelled:
            return "cancelled"
        if self.followups:
            return "followup_done"
        if self.return_plan:
            return "returned"
        if self.disposition:
            return "dispositioned"
        if self.observation:
            return "arrived"
        if self.precontact:
            return "contacted"
        return "received"

    @property
    def escalated(self) -> bool:
        return self.escalation is not None

    @property
    def escalation_basis_pending(self) -> bool:
        return bool(self.escalation) and not self.escalation.get("basis")


def _fingerprint(content: Mapping[str, Any]) -> str:
    return json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)


def _as_dt(value: datetime | str, field_name: str) -> datetime:
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{field_name} 必须携带时区")
    return moment


class Service:
    """闭环服务：命令入口、状态推进与持久化重建。"""

    def __init__(
        self,
        clock: Clock,
        ledger: Ledger,
        scheduler: Scheduler,
        pool: ResourcePool,
        schema: Mapping[str, Any] | None = None,
    ) -> None:
        self._clock = clock
        self._ledger = ledger
        self._scheduler = scheduler
        self._pool = pool
        self._schema = schema
        self._cases: dict[str, Case] = {}
        self._receipts: dict[str, dict[str, str]] = {}
        self._conflicts: list[dict[str, Any]] = []
        self._versions: dict[tuple[str, str], int] = {}
        for entry in self._ledger.entries():
            self._replay(entry)

    @classmethod
    def open(
        cls,
        directory: str | Path,
        clock: Clock | None = None,
        schema: Mapping[str, Any] | None = None,
        freeze_lead: timedelta = DEFAULT_FREEZE_LEAD,
    ) -> "Service":
        """打开（或创建）一个持久化服务目录，重启后待办与状态继续。"""
        root = Path(directory)
        clock = clock or SystemClock()
        return cls(
            clock=clock,
            ledger=Ledger(root / "ledger.jsonl"),
            scheduler=Scheduler(clock, root / "todos.json"),
            pool=ResourcePool(clock, freeze_lead, root / "resources.json"),
            schema=schema,
        )

    # ------------------------------------------------------------------
    # 资源登记
    # ------------------------------------------------------------------
    def add_slot(self, slot: Slot) -> None:
        self._pool.add_slot(slot)

    # ------------------------------------------------------------------
    # 命令
    # ------------------------------------------------------------------
    def receive_referral(
        self,
        key: str,
        case_id: str,
        role: str,
        actor_ref: str,
        patient: Mapping[str, Any],
        referral: Mapping[str, Any],
        concierge_ref: str,
        at: datetime | str | None = None,
    ) -> Receipt:
        """基层医生登记转诊单，确认来源事实。"""
        content = {
            "command": "receive_referral",
            "case_id": case_id,
            "role": role,
            "actor_ref": actor_ref,
            "patient": dict(patient),
            "referral": dict(referral),
            "concierge_ref": concierge_ref,
        }
        moment = self._moment(at)

        def apply() -> str:
            self._require_role(role, ("primary_doctor",))
            for field_name, container in (("patient.name", patient), ("patient.id_no", patient),
                                          ("referral.from_facility", referral), ("referral.condition_summary", referral)):
                if not str(container.get(field_name.split(".")[1], "")).strip():
                    raise ValueError(f"{field_name} 不能为空")
            existing = self._cases.get(case_id)
            if existing is not None:
                mismatch = self._source_mismatch(existing, patient, referral)
                if mismatch:
                    return self._conflict(key, case_id, "source_facts", mismatch)
                return "来源事实已登记，内容一致"
            case = Case(
                case_id=case_id,
                patient=dict(patient),
                referral=dict(referral),
                concierge_ref=concierge_ref,
                created_at=moment.isoformat(),
                contacts={"concierge": concierge_ref, "primary_doctor": actor_ref},
            )
            self._cases[case_id] = case
            self._confirm(case, "source", role, actor_ref, moment)
            self._note("referral_received", case_id, actor_ref, moment,
                       {"patient": dict(patient), "referral": dict(referral), "concierge_ref": concierge_ref})
            self._emit("REFERRAL_RECEIVED", case_id, moment,
                       {"from_facility": referral["from_facility"], "condition_summary": referral["condition_summary"]})
            return "转诊单已登记"

        return self._run(key, case_id, content, apply)

    def record_precontact(
        self,
        key: str,
        case_id: str,
        role: str,
        actor_ref: str,
        contact: Mapping[str, Any],
        arrival_plan: Mapping[str, Any],
        at: datetime | str | None = None,
    ) -> Receipt:
        """转诊管家登记前置联系与到达计划，并排定接站期限。"""
        content = {"command": "record_precontact", "case_id": case_id, "role": role, "actor_ref": actor_ref,
                   "contact": dict(contact), "arrival_plan": dict(arrival_plan)}
        moment = self._moment(at)

        def apply() -> str:
            self._require_role(role, ("concierge",))
            case = self._require_case(case_id)
            arrive_by = _as_dt(arrival_plan["arrive_by"], "arrival_plan.arrive_by")
            if case.precontact is not None:
                if case.precontact.get("contact") == dict(contact) and case.arrival_plan and \
                        case.arrival_plan.get("arrive_by") == arrive_by.isoformat():
                    return "前置联系已登记，内容一致"
                return self._conflict(key, case_id, "precontact", "前置联系或到达计划与已登记内容不一致")
            plan = dict(arrival_plan)
            plan["arrive_by"] = arrive_by.isoformat()
            case.precontact = {"contact": dict(contact), "by": actor_ref, "at": moment.isoformat()}
            case.arrival_plan = plan
            self._note("precontact_recorded", case_id, actor_ref, moment,
                       {"contact": dict(contact), "arrival_plan": plan})
            self._scheduler.schedule(f"{case_id}:pickup", case_id, "pickup", arrive_by)
            return "前置联系与到达计划已登记"

        return self._run(key, case_id, content, apply)

    def confirm_arrival(
        self,
        key: str,
        case_id: str,
        role: str,
        actor_ref: str,
        observed_at: datetime | str,
        observation: Mapping[str, Any],
        at: datetime | str | None = None,
    ) -> Receipt:
        """转诊管家现场确认到院与快速观察结果。"""
        observed = _as_dt(observed_at, "observed_at")
        content = {"command": "confirm_arrival", "case_id": case_id, "role": role, "actor_ref": actor_ref,
                   "observed_at": observed.isoformat(), "observation": dict(observation)}
        moment = self._moment(at)

        def apply() -> str:
            self._require_role(role, ("concierge",))
            case = self._require_case(case_id)
            if not str(observation.get("condition_summary", "")).strip():
                raise ValueError("observation.condition_summary 不能为空")
            if case.observation is not None:
                if case.observation.get("observed_at") == observed.isoformat() and \
                        case.observation.get("observation") == dict(observation):
                    return "到院确认已登记，内容一致"
                return self._conflict(key, case_id, "arrival", "现场观察的时间或病情与已登记内容不一致")
            case.observation = {
                "observed_at": observed.isoformat(),
                "observation": dict(observation),
                "by": actor_ref,
                "at": moment.isoformat(),
            }
            self._confirm(case, "arrival", role, actor_ref, moment)
            self._note("arrival_confirmed", case_id, actor_ref, moment,
                       {"observed_at": observed.isoformat(), "observation": dict(observation)})
            self._emit("ARRIVAL_CONFIRMED", case_id, moment,
                       {"observed_at": observed.isoformat(), "assessor_ref": actor_ref})
            self._scheduler.complete(f"{case_id}:pickup")
            return "到院与现场观察已确认"

        return self._run(key, case_id, content, apply)

    def escalate(
        self,
        key: str,
        case_id: str,
        role: str,
        actor_ref: str,
        reason: str,
        review_due_at: datetime | str | None = None,
        review_within: timedelta = DEFAULT_REVIEW_WITHIN,
        min_slots: Iterable[str] = (),
        at: datetime | str | None = None,
    ) -> Receipt:
        """危急升级：先占用最小必要资源，复核期限前补全临床依据。"""
        moment = self._moment(at)
        due = _as_dt(review_due_at, "review_due_at") if review_due_at is not None else moment + review_within
        slot_ids = list(min_slots)
        content = {"command": "escalate", "case_id": case_id, "role": role, "actor_ref": actor_ref,
                   "reason": reason, "review_due_at": due.isoformat(), "min_slots": slot_ids}

        def apply() -> str:
            self._require_role(role, ("concierge", "specialist"))
            case = self._require_case(case_id)
            if not reason.strip():
                raise ValueError("reason 不能为空")
            if case.escalation is not None:
                if case.escalation.get("reason") == reason:
                    return "危急升级已登记，内容一致"
                return self._conflict(key, case_id, "escalation", "升级原因与已登记内容不一致")
            emergency_booking = None
            shortage: list[str] = []
            if slot_ids:
                try:
                    booking = self._pool.book(f"{key}:emergency", case_id, slot_ids)
                    emergency_booking = booking.booking_id
                except BookingError as exc:
                    shortage = list(exc.unavailable)
            case.escalation = {
                "reason": reason,
                "by": actor_ref,
                "at": moment.isoformat(),
                "review_due_at": due.isoformat(),
                "basis": None,
                "basis_at": None,
                "emergency_booking": emergency_booking,
                "shortage": shortage,
            }
            self._note("urgency_escalated", case_id, actor_ref, moment,
                       {"reason": reason, "review_due_at": due.isoformat(),
                        "emergency_booking": emergency_booking, "shortage": shortage})
            self._emit("URGENCY_ESCALATED", case_id, moment, {"reason": reason, "review_due_at": due.isoformat()})
            self._scheduler.schedule(f"{case_id}:escalation_review", case_id, "escalation_review", due)
            if shortage:
                return f"危急升级已登记，但最小必要资源不足: {', '.join(shortage)}"
            return "危急升级已登记，最小必要资源已占用"

        return self._run(key, case_id, content, apply)

    def complete_escalation_basis(
        self,
        key: str,
        case_id: str,
        role: str,
        actor_ref: str,
        basis: str,
        at: datetime | str | None = None,
    ) -> Receipt:
        """专科医生在复核期限前补全危急升级的临床依据。"""
        content = {"command": "complete_escalation_basis", "case_id": case_id, "role": role,
                   "actor_ref": actor_ref, "basis": basis}
        moment = self._moment(at)

        def apply() -> str:
            self._require_role(role, ("specialist",))
            case = self._require_case(case_id)
            if case.escalation is None:
                return self._reject(key, "尚未登记危急升级，无法补全依据")
            if not basis.strip():
                raise ValueError("basis 不能为空")
            if case.escalation.get("basis"):
                if case.escalation["basis"] == basis:
                    return "升级依据已补全，内容一致"
                return self._conflict(key, case_id, "escalation_basis", "升级依据与已登记内容不一致")
            case.escalation["basis"] = basis
            case.escalation["basis_at"] = moment.isoformat()
            self._note("escalation_basis_completed", case_id, actor_ref, moment, {"basis": basis})
            self._scheduler.complete(f"{case_id}:escalation_review")
            due = datetime.fromisoformat(case.escalation["review_due_at"])
            if moment > due:
                return "升级依据已补全，但已超过复核期限"
            return "升级依据已在期限内补全"

        return self._run(key, case_id, content, apply)

    def book_resources(
        self,
        key: str,
        case_id: str,
        role: str,
        actor_ref: str,
        slot_ids: Iterable[str],
        at: datetime | str | None = None,
    ) -> Receipt:
        """按冻结规则原子安排检查窗口与陪护人员。"""
        ids = list(slot_ids)
        content = {"command": "book_resources", "case_id": case_id, "role": role,
                   "actor_ref": actor_ref, "slot_ids": ids}
        moment = self._moment(at)

        def apply() -> str:
            self._require_role(role, ("concierge",))
            self._require_case(case_id)
            if not ids:
                raise ValueError("slot_ids 不能为空")
            try:
                booking = self._pool.book(f"bk-{key}", case_id, ids)
            except BookingError as exc:
                self._note("resource_booking_failed", case_id, actor_ref, moment,
                           {"slot_ids": ids, "unavailable": list(exc.unavailable)})
                return self._reject(key, f"资源原子安排失败: {exc}")
            self._note("resource_booked", case_id, actor_ref, moment,
                       {"booking_id": booking.booking_id, "slot_ids": ids})
            self._emit("RESOURCE_BOOKED", case_id, moment, {"booking_id": booking.booking_id, "slot_ids": ids})
            for slot_id in ids:
                slot = self._pool.slot(slot_id)
                if slot.kind == "exam_window":
                    self._scheduler.schedule(f"{case_id}:exam:{slot_id}", case_id, "exam", slot.start)
            return f"资源已原子安排: {booking.booking_id}"

        return self._run(key, case_id, content, apply)

    def mark_booking_used(self, booking_id: str) -> None:
        """登记预约已实际使用；已使用的预约不再参与未到释放。"""
        booking = self._pool.mark_used(booking_id)
        for slot_id in booking.slot_ids:
            self._scheduler.complete(f"{booking.case_id}:exam:{slot_id}")

    def record_disposition(
        self,
        key: str,
        case_id: str,
        role: str,
        actor_ref: str,
        decision: str,
        detail: Mapping[str, Any],
        at: datetime | str | None = None,
    ) -> Receipt:
        """专科医生作出住院或离院决定，确认临床处置。"""
        content = {"command": "record_disposition", "case_id": case_id, "role": role,
                   "actor_ref": actor_ref, "decision": decision, "detail": dict(detail)}
        moment = self._moment(at)

        def apply() -> str:
            self._require_role(role, ("specialist",))
            case = self._require_case(case_id)
            if decision not in ("admit", "discharge"):
                raise ValueError("decision 必须是 admit 或 discharge")
            if case.disposition is not None:
                if case.disposition.get("decision") == decision and case.disposition.get("detail") == dict(detail):
                    return "临床处置已登记，内容一致"
                return self._conflict(key, case_id, "disposition", "住院或离院决定与已登记内容不一致")
            case.disposition = {"decision": decision, "detail": dict(detail),
                                "by": actor_ref, "at": moment.isoformat()}
            case.contacts["specialist"] = actor_ref
            self._confirm(case, "clinical", role, actor_ref, moment)
            self._note("disposition_recorded", case_id, actor_ref, moment,
                       {"decision": decision, "detail": dict(detail)})
            return "住院或离院决定已登记"

        return self._run(key, case_id, content, apply)

    def plan_return(
        self,
        key: str,
        case_id: str,
        role: str,
        actor_ref: str,
        plan: Mapping[str, Any],
        checklist: Iterable[Mapping[str, Any]],
        followup_due_at: datetime | str | None = None,
        followup_within: timedelta = DEFAULT_FOLLOWUP_WITHIN,
        at: datetime | str | None = None,
    ) -> Receipt:
        """专科医生登记下转方案与复查清单，排定下转、复查与回访期限。"""
        moment = self._moment(at)
        items = [dict(item) for item in checklist]
        follow_due = _as_dt(followup_due_at, "followup_due_at") if followup_due_at is not None else moment + followup_within
        content = {"command": "plan_return", "case_id": case_id, "role": role, "actor_ref": actor_ref,
                   "plan": dict(plan), "checklist": items, "followup_due_at": follow_due.isoformat()}

        def apply() -> str:
            self._require_role(role, ("specialist",))
            case = self._require_case(case_id)
            transfer_by = _as_dt(plan["transfer_by"], "plan.transfer_by")
            if not str(plan.get("to_facility", "")).strip():
                raise ValueError("plan.to_facility 不能为空")
            if case.return_plan is not None:
                return self._conflict(key, case_id, "return_plan", "下转方案已登记，不可改写")
            stored_plan = dict(plan)
            stored_plan["transfer_by"] = transfer_by.isoformat()
            case.return_plan = {"plan": stored_plan, "by": actor_ref, "at": moment.isoformat()}
            case.contacts["specialist"] = actor_ref
            for item in items:
                due = _as_dt(item["due_at"], "checklist.due_at")
                entry = {"item_id": item["item_id"], "title": item.get("title", ""),
                         "due_at": due.isoformat(), "result": None, "done_at": None}
                case.checklist.append(entry)
                self._scheduler.schedule(f"{case_id}:recheck:{item['item_id']}", case_id, "recheck", due)
            self._note("return_planned", case_id, actor_ref, moment,
                       {"plan": stored_plan, "checklist": items, "followup_due_at": follow_due.isoformat()})
            self._scheduler.schedule(f"{case_id}:downward", case_id, "downward", transfer_by)
            self._scheduler.schedule(f"{case_id}:followup", case_id, "followup", follow_due)
            return "下转方案与复查清单已登记"

        return self._run(key, case_id, content, apply)

    def complete_recheck(
        self,
        key: str,
        case_id: str,
        role: str,
        actor_ref: str,
        item_id: str,
        result: str,
        at: datetime | str | None = None,
    ) -> Receipt:
        """基层医生回填复查清单结果。"""
        content = {"command": "complete_recheck", "case_id": case_id, "role": role,
                   "actor_ref": actor_ref, "item_id": item_id, "result": result}
        moment = self._moment(at)

        def apply() -> str:
            self._require_role(role, ("primary_doctor",))
            case = self._require_case(case_id)
            item = next((x for x in case.checklist if x["item_id"] == item_id), None)
            if item is None:
                return self._reject(key, f"复查项目不存在: {item_id}")
            if item.get("done_at"):
                if item.get("result") == result:
                    return "复查结果已登记，内容一致"
                return self._conflict(key, case_id, "recheck", f"复查项目 {item_id} 结果与已登记内容不一致")
            item["result"] = result
            item["done_at"] = moment.isoformat()
            self._note("recheck_completed", case_id, actor_ref, moment, {"item_id": item_id, "result": result})
            self._scheduler.complete(f"{case_id}:recheck:{item_id}")
            return "复查结果已登记"

        return self._run(key, case_id, content, apply)

    def complete_followup(
        self,
        key: str,
        case_id: str,
        role: str,
        actor_ref: str,
        outcome: str,
        next_action: str,
        at: datetime | str | None = None,
    ) -> Receipt:
        """转诊管家登记回访结果与下一步安排。"""
        content = {"command": "complete_followup", "case_id": case_id, "role": role,
                   "actor_ref": actor_ref, "outcome": outcome, "next_action": next_action}
        moment = self._moment(at)

        def apply() -> str:
            self._require_role(role, ("concierge",))
            case = self._require_case(case_id)
            if not outcome.strip():
                raise ValueError("outcome 不能为空")
            record = {"outcome": outcome, "next_action": next_action,
                      "by": actor_ref, "at": moment.isoformat()}
            case.followups.append(record)
            self._note("followup_completed", case_id, actor_ref, moment,
                       {"outcome": outcome, "next_action": next_action})
            self._emit("FOLLOWUP_COMPLETED", case_id, moment, {"outcome": outcome, "next_action": next_action})
            self._scheduler.complete(f"{case_id}:followup")
            return "回访结果已登记"

        return self._run(key, case_id, content, apply)

    def release_unused(
        self,
        key: str,
        case_id: str,
        role: str,
        actor_ref: str,
        reason: str,
        at: datetime | str | None = None,
    ) -> Receipt:
        """患者未到或需求变化：释放未使用且未冻结的预约。"""
        content = {"command": "release_unused", "case_id": case_id, "role": role,
                   "actor_ref": actor_ref, "reason": reason}
        moment = self._moment(at)

        def apply() -> str:
            self._require_role(role, ("concierge",))
            case = self._require_case(case_id)
            if reason not in ("no_show", "needs_changed"):
                raise ValueError("reason 必须是 no_show 或 needs_changed")
            released = self._pool.release_unused_for_case(case_id)
            retained = [b.booking_id for b in self._pool.bookings_for(case_id)]
            if reason == "no_show" and case.cancelled is None:
                case.cancelled = {"reason": reason, "by": actor_ref, "at": moment.isoformat()}
            self._note("bookings_released", case_id, actor_ref, moment,
                       {"reason": reason, "released": released, "retained": retained})
            for booking_id in released:
                booking = next(b for b in self._pool.bookings_for(case_id, active_only=False) if b.booking_id == booking_id)
                for slot_id in booking.slot_ids:
                    self._scheduler.complete(f"{case_id}:exam:{slot_id}")
            return f"已释放未使用预约 {len(released)} 项，保留 {len(retained)} 项"

        return self._run(key, case_id, content, apply)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_case(self, case_id: str) -> Case:
        return self._require_case(case_id)

    def list_cases(self) -> list[str]:
        return sorted(self._cases)

    def due_todos(self, case_id: str | None = None) -> list[Todo]:
        """当前到期的待办；重启后重新打开服务即可继续。"""
        return self._scheduler.due(case_id)

    def pending_todos(self, case_id: str | None = None) -> list[Todo]:
        return self._scheduler.pending(case_id)

    def conflicts(self, case_id: str | None = None) -> list[dict[str, Any]]:
        return [c for c in self._conflicts if case_id is None or c.get("case_id") == case_id]

    def pool(self) -> ResourcePool:
        return self._pool

    def now(self) -> datetime:
        return self._clock.now()

    def trail(self, case_id: str) -> list[dict[str, Any]]:
        """按落账顺序返回该病例的台账条目，供审计还原。"""
        return [entry for entry in self._ledger.entries() if entry.get("case_id") == case_id]

    # ------------------------------------------------------------------
    # 内部机制
    # ------------------------------------------------------------------
    def _moment(self, at: datetime | str | None) -> datetime:
        return _as_dt(at, "at") if at is not None else self._clock.now()

    def _run(self, key: str, case_id: str, content: Mapping[str, Any], apply) -> Receipt:
        fingerprint = _fingerprint(content)
        stored = self._receipts.get(key)
        if stored is not None:
            if stored["fingerprint"] == fingerprint:
                return Receipt(key, stored["status"], stored["detail"])
            detail = "相同业务键的消息内容不一致，已登记冲突，未自动合并"
            self._record_conflict(key, case_id, "business_key", detail)
            return Receipt(key, "conflict", detail)
        outcome = apply()
        if isinstance(outcome, Receipt):
            receipt = outcome
        elif outcome.startswith("冲突:"):
            receipt = Receipt(key, "conflict", outcome)
        else:
            receipt = Receipt(key, "applied", outcome)
        self._receipts[key] = {"fingerprint": fingerprint, "status": receipt.status, "detail": receipt.detail}
        self._ledger.append("receipt", {"key": key, "case_id": case_id,
                                        "status": receipt.status, "detail": receipt.detail,
                                        "fingerprint": fingerprint})
        return receipt

    def _reject(self, key: str, detail: str) -> Receipt:
        return Receipt(key, "rejected", detail)

    def _conflict(self, key: str, case_id: str, subject: str, detail: str) -> str:
        self._record_conflict(key, case_id, subject, detail)
        return f"冲突:{detail}"

    def _record_conflict(self, key: str, case_id: str, subject: str, detail: str) -> None:
        if any(c.get("key") == key and c.get("subject") == subject for c in self._conflicts):
            return
        entry = {"key": key, "case_id": case_id, "subject": subject, "detail": detail,
                 "at": self._clock.now().isoformat()}
        self._conflicts.append(entry)
        self._ledger.append("conflict", entry)

    def _require_role(self, role: str, allowed: tuple[str, ...]) -> None:
        if role not in ROLES:
            raise ValueError(f"未知角色: {role}")
        if role not in allowed:
            names = "、".join(ROLE_NAMES[r] for r in allowed)
            raise PermissionError(f"该结论只能由{names}书写，当前角色 {ROLE_NAMES[role]} 不能代写")

    def _require_case(self, case_id: str) -> Case:
        case = self._cases.get(case_id)
        if case is None:
            raise KeyError(f"病例不存在: {case_id}")
        return case

    def _confirm(self, case: Case, kind: str, role: str, actor_ref: str, moment: datetime) -> None:
        expected = CONFIRMATION_ROLES[kind]
        if role != expected:
            raise PermissionError(f"{kind} 确认只能由{ROLE_NAMES[expected]}书写")
        if kind in case.confirmations:
            raise PermissionError(f"{kind} 确认已登记，不可改写")
        case.confirmations[kind] = {"by": actor_ref, "role": role, "at": moment.isoformat()}

    def _source_mismatch(self, case: Case, patient: Mapping[str, Any], referral: Mapping[str, Any]) -> str | None:
        for field_name in ("name", "id_no"):
            if str(case.patient.get(field_name)) != str(patient.get(field_name, "")):
                return f"患者身份与已登记来源事实不一致: {field_name}"
        if str(case.referral.get("condition_summary")) != str(referral.get("condition_summary", "")):
            return "病情描述与已登记来源事实不一致: condition_summary"
        return None

    def _note(self, note_type: str, case_id: str, actor_ref: str, moment: datetime, data: Mapping[str, Any]) -> None:
        self._ledger.append("note", {"note_type": note_type, "case_id": case_id, "actor_ref": actor_ref,
                                     "at": moment.isoformat(), "data": dict(data)})

    def _emit(self, event_type: str, case_id: str, moment: datetime, payload: Mapping[str, Any]) -> None:
        aggregate_type = _EVENT_AGGREGATE[event_type]
        version_key = (aggregate_type, case_id)
        version = self._versions.get(version_key, 0) + 1
        self._versions[version_key] = version
        event = {
            "event_id": f"{case_id}:{event_type}:{version}",
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": case_id,
            "occurred_at": moment.isoformat(),
            "version": version,
            "payload": dict(payload),
        }
        if self._schema is not None:
            issues = validate_event(event, self._schema)
            if issues:
                raise RuntimeError(f"事件未通过契约校验: {[i.code for i in issues]}")
        self._ledger.append("event", {"case_id": case_id, "event": event})

    def _replay(self, entry: Mapping[str, Any]) -> None:
        kind = entry.get("kind")
        if kind == "receipt":
            self._receipts[entry["key"]] = {
                "fingerprint": entry["fingerprint"],
                "status": entry["status"],
                "detail": entry["detail"],
            }
        elif kind == "conflict":
            self._conflicts.append(dict(entry))
        elif kind == "event":
            event = entry["event"]
            version_key = (event["aggregate_type"], event["aggregate_id"])
            self._versions[version_key] = max(self._versions.get(version_key, 0), int(event["version"]))
        elif kind == "note":
            self._apply_note(entry)

    def _apply_note(self, entry: Mapping[str, Any]) -> None:
        note_type = entry["note_type"]
        case_id = entry["case_id"]
        data = entry.get("data", {})
        actor = entry.get("actor_ref", "")
        at = entry.get("at", "")
        if note_type == "referral_received":
            case = Case(case_id=case_id, patient=dict(data["patient"]), referral=dict(data["referral"]),
                        concierge_ref=data["concierge_ref"], created_at=at,
                        contacts={"concierge": data["concierge_ref"], "primary_doctor": actor})
            case.confirmations["source"] = {"by": actor, "role": "primary_doctor", "at": at}
            self._cases[case_id] = case
            return
        case = self._cases.get(case_id)
        if case is None:
            return
        if note_type == "precontact_recorded":
            case.precontact = {"contact": dict(data["contact"]), "by": actor, "at": at}
            case.arrival_plan = dict(data["arrival_plan"])
        elif note_type == "arrival_confirmed":
            case.observation = {"observed_at": data["observed_at"], "observation": dict(data["observation"]),
                                "by": actor, "at": at}
            case.confirmations.setdefault("arrival", {"by": actor, "role": "concierge", "at": at})
        elif note_type == "urgency_escalated":
            case.escalation = {"reason": data["reason"], "by": actor, "at": at,
                               "review_due_at": data["review_due_at"], "basis": None, "basis_at": None,
                               "emergency_booking": data.get("emergency_booking"),
                               "shortage": list(data.get("shortage", []))}
        elif note_type == "escalation_basis_completed" and case.escalation is not None:
            case.escalation["basis"] = data["basis"]
            case.escalation["basis_at"] = at
        elif note_type == "disposition_recorded":
            case.disposition = {"decision": data["decision"], "detail": dict(data["detail"]), "by": actor, "at": at}
            case.contacts["specialist"] = actor
            case.confirmations.setdefault("clinical", {"by": actor, "role": "specialist", "at": at})
        elif note_type == "return_planned":
            case.return_plan = {"plan": dict(data["plan"]), "by": actor, "at": at}
            case.contacts["specialist"] = actor
            for item in data.get("checklist", []):
                case.checklist.append({"item_id": item["item_id"], "title": item.get("title", ""),
                                       "due_at": item["due_at"], "result": None, "done_at": None})
        elif note_type == "recheck_completed":
            for item in case.checklist:
                if item["item_id"] == data["item_id"] and not item.get("done_at"):
                    item["result"] = data["result"]
                    item["done_at"] = at
        elif note_type == "followup_completed":
            case.followups.append({"outcome": data["outcome"], "next_action": data["next_action"],
                                   "by": actor, "at": at})
        elif note_type == "bookings_released":
            if data.get("reason") == "no_show" and case.cancelled is None:
                case.cancelled = {"reason": data["reason"], "by": actor, "at": at}
