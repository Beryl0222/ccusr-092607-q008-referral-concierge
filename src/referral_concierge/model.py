"""闭环静态模型：角色、事件类型、命令规格与调度策略。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

# 角色：任何人都不能替其他角色补写结论。
PRIMARY_DOCTOR = "primary_doctor"  # 基层医生：确认来源事实
CONCIERGE = "concierge"  # 转诊管家：确认现场情况
SPECIALIST = "specialist"  # 专科医生：确认临床处置
SYSTEM = "system"  # 可控时钟等系统行为
ACTOR_ROLES = frozenset({PRIMARY_DOCTOR, CONCIERGE, SPECIALIST})

# 事件类型
REFERRAL_RECEIVED = "REFERRAL_RECEIVED"
PREARRIVAL_CONTACTED = "PREARRIVAL_CONTACTED"
ARRIVAL_PLANNED = "ARRIVAL_PLANNED"
ARRIVAL_CONFIRMED = "ARRIVAL_CONFIRMED"
OBSERVATION_RECORDED = "OBSERVATION_RECORDED"
URGENCY_ESCALATED = "URGENCY_ESCALATED"
ESCALATION_JUSTIFIED = "ESCALATION_JUSTIFIED"
RESOURCE_BOOKED = "RESOURCE_BOOKED"
RESOURCE_RELEASED = "RESOURCE_RELEASED"
ESCORT_ASSIGNED = "ESCORT_ASSIGNED"
DISPOSITION_DECIDED = "DISPOSITION_DECIDED"
DOWNWARD_PLANNED = "DOWNWARD_PLANNED"
DOWNWARD_HANDOVER_CONFIRMED = "DOWNWARD_HANDOVER_CONFIRMED"
REVIEW_CHECKLIST_ISSUED = "REVIEW_CHECKLIST_ISSUED"
FOLLOWUP_COMPLETED = "FOLLOWUP_COMPLETED"
DEADLINE_BREACHED = "DEADLINE_BREACHED"

EVENT_AGGREGATE = {
    REFERRAL_RECEIVED: "referral_order",
    PREARRIVAL_CONTACTED: "referral_order",
    ARRIVAL_PLANNED: "referral_order",
    ARRIVAL_CONFIRMED: "arrival_assessment",
    OBSERVATION_RECORDED: "arrival_assessment",
    URGENCY_ESCALATED: "arrival_assessment",
    ESCALATION_JUSTIFIED: "arrival_assessment",
    RESOURCE_BOOKED: "care_coordination",
    RESOURCE_RELEASED: "care_coordination",
    ESCORT_ASSIGNED: "care_coordination",
    DISPOSITION_DECIDED: "care_coordination",
    DEADLINE_BREACHED: "care_coordination",
    DOWNWARD_PLANNED: "return_plan",
    DOWNWARD_HANDOVER_CONFIRMED: "return_plan",
    REVIEW_CHECKLIST_ISSUED: "return_plan",
    FOLLOWUP_COMPLETED: "return_plan",
}

# 现场分级与升级级别
ACUITY_LEVELS = frozenset({"routine", "urgent", "critical"})
ESCALATION_LEVELS = frozenset({"urgent", "critical"})
DISPOSITIONS = frozenset({"admit", "discharge"})

# 预约释放原因
RELEASE_NO_SHOW = "no_show"  # 患者未到
RELEASE_NEEDS_CHANGED = "needs_changed"  # 需求变化
RELEASE_PREEMPTED = "preempted"  # 被危急案例抢占
RELEASE_JUSTIFICATION_OVERDUE = "justification_overdue"  # 升级依据逾期未补
RELEASE_GREEN_HOLD_EXPIRED = "green_channel_hold_expired"  # 普通占用绿色通道超期
RELEASE_WINDOW_ELAPSED = "window_elapsed"  # 窗口已过仍未使用

# 期限违约种类
BREACH_PICKUP = "pickup"  # 接站期限
BREACH_ESCALATION_REVIEW = "escalation_review"  # 升级依据期限
BREACH_EXAM_WINDOW = "exam_window"  # 检查窗口期限
BREACH_DOWNWARD = "downward_handover"  # 下转交接期限
BREACH_REVIEW_ITEM = "review_item"  # 复查项目期限
BREACH_FOLLOWUP = "followup"  # 回访期限
BREACH_GREEN_HOLD = "green_channel_hold"  # 绿色通道占用期限


def parse_dt(value: str) -> datetime:
    """解析带时区的 ISO 时间；不带时区视为契约外数据，抛 ValueError。"""
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"时间必须携带时区: {value!r}")
    return parsed


def iso(dt: datetime) -> str:
    return dt.isoformat()


@dataclass(frozen=True)
class Policy:
    """调度策略：冻结窗口、接站宽限与普通占用绿色通道上限。"""

    freeze_horizon: timedelta = timedelta(hours=2)
    pickup_grace: timedelta = timedelta(minutes=30)
    green_channel_max_hold: timedelta = timedelta(hours=4)

    @staticmethod
    def from_mapping(data: Mapping[str, Any] | None) -> "Policy":
        if not data:
            return Policy()
        return Policy(
            freeze_horizon=timedelta(minutes=float(data.get("freeze_horizon_minutes", 120))),
            pickup_grace=timedelta(minutes=float(data.get("pickup_grace_minutes", 30))),
            green_channel_max_hold=timedelta(minutes=float(data.get("green_channel_max_hold_minutes", 240))),
        )


@dataclass(frozen=True)
class CommandSpec:
    """命令的静态校验规格：角色、必填字段、时区字段与越权结论字段。"""

    name: str
    roles: frozenset[str]
    data_required: tuple[str, ...] = ()
    tz_fields: tuple[str, ...] = ()
    list_tz_fields: tuple[tuple[str, str], ...] = ()
    sub_required: tuple[tuple[str, tuple[str, ...]], ...] = ()
    enums: Mapping[str, frozenset[str]] = field(default_factory=dict)
    forbidden: tuple[str, ...] = ()


COMMAND_SPECS: dict[str, CommandSpec] = {
    spec.name: spec
    for spec in (
        CommandSpec(
            name="receive_referral",
            roles=frozenset({PRIMARY_DOCTOR}),
            data_required=("referral_no", "patient", "referring_facility", "primary_doctor_ref", "chief_complaint", "contact"),
            sub_required=(
                ("patient", ("patient_ref", "name", "id_ref")),
                ("contact", ("name", "phone")),
            ),
            forbidden=("acuity", "vitals_summary", "observed_at", "disposition", "diagnosis", "justification"),
        ),
        CommandSpec(
            name="record_prearrival_contact",
            roles=frozenset({CONCIERGE}),
            data_required=("contacted_at", "channel"),
            tz_fields=("contacted_at",),
            forbidden=("acuity", "vitals_summary", "disposition", "diagnosis"),
        ),
        CommandSpec(
            name="plan_arrival",
            roles=frozenset({CONCIERGE}),
            data_required=("planned_arrival_at", "pickup_point", "greeter_ref"),
            tz_fields=("planned_arrival_at",),
            forbidden=("acuity", "disposition", "diagnosis"),
        ),
        CommandSpec(
            name="confirm_arrival",
            roles=frozenset({CONCIERGE}),
            data_required=("observed_at", "assessor_ref"),
            tz_fields=("observed_at",),
            forbidden=("acuity", "vitals_summary", "disposition", "diagnosis"),
        ),
        CommandSpec(
            name="record_observation",
            roles=frozenset({CONCIERGE}),
            data_required=("observed_at", "assessor_ref", "acuity", "vitals_summary"),
            tz_fields=("observed_at",),
            enums={"acuity": ACUITY_LEVELS},
            forbidden=("disposition", "diagnosis", "department", "justification", "treatment"),
        ),
        CommandSpec(
            name="escalate",
            roles=frozenset({CONCIERGE, SPECIALIST}),
            data_required=("reason", "level", "review_due_at"),
            tz_fields=("review_due_at",),
            enums={"level": ESCALATION_LEVELS},
            forbidden=("justification", "disposition", "diagnosis"),
        ),
        CommandSpec(
            name="justify_escalation",
            roles=frozenset({SPECIALIST}),
            data_required=("justification",),
            forbidden=("acuity", "observed_at", "disposition"),
        ),
        CommandSpec(
            name="book_resources",
            roles=frozenset({CONCIERGE}),
            data_required=("requests", "purpose"),
        ),
        CommandSpec(
            name="release_resources",
            roles=frozenset({CONCIERGE}),
            data_required=("reason",),
        ),
        CommandSpec(
            name="assign_escort",
            roles=frozenset({CONCIERGE}),
            data_required=("escort_ref", "responsible_contact"),
            sub_required=(("responsible_contact", ("name", "phone")),),
        ),
        CommandSpec(
            name="decide_disposition",
            roles=frozenset({SPECIALIST}),
            data_required=("decision", "department"),
            enums={"decision": DISPOSITIONS},
            forbidden=("vitals_summary", "observed_at", "assessor_ref"),
        ),
        CommandSpec(
            name="plan_downward",
            roles=frozenset({SPECIALIST}),
            data_required=("target_facility", "plan_summary", "handover_deadline"),
            tz_fields=("handover_deadline",),
        ),
        CommandSpec(
            name="confirm_downward_handover",
            roles=frozenset({CONCIERGE}),
            data_required=("confirmed_at", "receiving_facility"),
            tz_fields=("confirmed_at",),
        ),
        CommandSpec(
            name="issue_review_checklist",
            roles=frozenset({SPECIALIST}),
            data_required=("items", "followup_due_at"),
            tz_fields=("followup_due_at",),
            list_tz_fields=(("items", "due_at"),),
        ),
        CommandSpec(
            name="complete_followup",
            roles=frozenset({CONCIERGE}),
            data_required=("outcome", "next_action", "followed_at"),
            tz_fields=("followed_at",),
        ),
    )
}
