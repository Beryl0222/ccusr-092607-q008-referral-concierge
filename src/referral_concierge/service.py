"""县域转诊陪护闭环服务。

贯通基层转诊单、前置联系、到达计划、现场观察、专科与检查资源、陪护责任、
住院或离院决定、下转方案、复查清单与回访结果。

- 业务键幂等：内容一致沿用回执；内容冲突登记隔离，绝不自动合并。
- 乱序重放：前置事实未到的命令进入待办，前置入账后自动补放，重启继续。
- 角色分离：基层医生确认来源事实，转诊管家确认现场情况，专科医生确认临床处置。
- 危急升级：可先占用最小必要资源，专科医生限期补全依据，逾期时钟释放。
- 资源争用：按冻结规则原子安排；患者未到或需求变化释放未使用预约。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Iterable, Mapping

from . import model as m
from .clock import Clock
from .projection import CaseState, apply_booking_event, build_cases, build_resource_ledger
from .resources import Scheduler
from .store import Store


class Defer(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Reject(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def content_hash(command: Mapping[str, Any]) -> str:
    body = {
        "command": command.get("command"),
        "actor": command.get("actor"),
        "occurred_at": command.get("occurred_at"),
        "data": command.get("data"),
    }
    blob = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _diff_fields(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    diffs: list[str] = []
    for key in ("command", "occurred_at"):
        if old.get(key) != new.get(key):
            diffs.append(key)
    for part in ("actor", "data"):
        lo = old.get(part) or {}
        ln = new.get(part) or {}
        for field in sorted(set(lo) | set(ln)):
            if lo.get(field) != ln.get(field):
                diffs.append(f"{part}.{field}")
    return diffs


class ClosedLoopService:
    def __init__(
        self,
        store: Store,
        clock: Clock,
        policy: m.Policy | None = None,
        schema: Mapping[str, Any] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        meta = store.load_meta()
        self.policy = policy or m.Policy.from_mapping(meta.get("policy"))
        self.schema = schema
        self._events = store.load_events()
        self._receipts = store.load_receipts()
        self._conflicts = store.load_conflicts()
        self._pending = store.load_pending()
        self._meta = meta
        self._rebuild()

    # ------------------------------------------------------------------ 状态

    def _rebuild(self) -> None:
        self._cases = build_cases(self._events)
        self._ledger = build_resource_ledger(self._events)

    def case(self, case_id: str) -> CaseState | None:
        return self._cases.get(case_id)

    def events(self, case_id: str | None = None) -> list[dict[str, Any]]:
        if case_id is None:
            return list(self._events)
        case = self._cases.get(case_id)
        return list(case.events) if case else []

    def ledger(self) -> dict[str, dict[str, Any]]:
        return self._ledger

    def conflicts(self) -> list[dict[str, Any]]:
        return list(self._conflicts)

    def pending(self) -> list[dict[str, Any]]:
        return list(self._pending)

    # ------------------------------------------------------------------ 回执

    def _now(self) -> datetime:
        return self.clock.now()

    def _store_receipt(
        self, key: str, digest: str, receipt: dict[str, Any], command: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._receipts[key] = {"content_hash": digest, "receipt": receipt, "command": command}
        self.store.save_receipts(self._receipts)
        return receipt

    def _receipt(
        self,
        command: Mapping[str, Any],
        status: str,
        message: str,
        event_ids: Iterable[str] = (),
        case_id: str | None = None,
    ) -> dict[str, Any]:
        return {
            "business_key": command.get("business_key"),
            "command": command.get("command"),
            "status": status,
            "case_id": case_id,
            "event_ids": list(event_ids),
            "message": message,
            "issued_at": m.iso(self._now()),
        }

    # ------------------------------------------------------------------ 校验

    def _validate_static(self, command: Any) -> list[str]:
        problems: list[str] = []
        if not isinstance(command, Mapping):
            return ["命令必须是 JSON 对象"]
        name = command.get("command")
        if not isinstance(name, str) or name not in m.COMMAND_SPECS:
            return [f"未知命令: {name!r}"]
        spec = m.COMMAND_SPECS[name]
        key = command.get("business_key")
        if not isinstance(key, str) or not key.strip():
            problems.append("business_key 必须是非空字符串")
        actor = command.get("actor")
        if not isinstance(actor, Mapping):
            problems.append("actor 必须包含 role 与 ref")
        else:
            role = actor.get("role")
            if role not in m.ACTOR_ROLES:
                problems.append(f"未知角色: {role!r}")
            elif role not in spec.roles:
                problems.append(f"角色 {role} 无权执行 {name}（role_not_allowed）")
            if not isinstance(actor.get("ref"), str) or not actor.get("ref", "").strip():
                problems.append("actor.ref 必须是非空字符串")
        occurred_at = command.get("occurred_at")
        if not isinstance(occurred_at, str):
            problems.append("occurred_at 缺失")
        else:
            try:
                m.parse_dt(occurred_at)
            except ValueError:
                problems.append("occurred_at 必须携带时区")
        data = command.get("data")
        if not isinstance(data, Mapping):
            problems.append("data 必须是 JSON 对象")
            return problems
        for field_name in spec.data_required:
            if field_name not in data:
                problems.append(f"data.{field_name} 缺失")
        for sub, fields in spec.sub_required:
            body = data.get(sub)
            if isinstance(body, Mapping):
                for field_name in fields:
                    value = body.get(field_name)
                    if not isinstance(value, str) or not value.strip():
                        problems.append(f"data.{sub}.{field_name} 必须是非空字符串")
            elif sub in data:
                problems.append(f"data.{sub} 必须是 JSON 对象")
        for field_name in spec.tz_fields:
            value = data.get(field_name)
            if value is not None:
                try:
                    m.parse_dt(value)
                except (ValueError, TypeError):
                    problems.append(f"data.{field_name} 必须携带时区")
        for list_field, sub_field in spec.list_tz_fields:
            rows = data.get(list_field)
            if isinstance(rows, list):
                for index, row in enumerate(rows):
                    if isinstance(row, Mapping) and row.get(sub_field) is not None:
                        try:
                            m.parse_dt(row[sub_field])
                        except (ValueError, TypeError):
                            problems.append(f"data.{list_field}[{index}].{sub_field} 必须携带时区")
        for field_name, allowed in spec.enums.items():
            value = data.get(field_name)
            if value is not None and value not in allowed:
                problems.append(f"data.{field_name} 取值须为 {sorted(allowed)}")
        for field_name in spec.forbidden:
            if field_name in data:
                problems.append(f"data.{field_name} 属于其他角色的结论，禁止代写（cross_role_field）")
        return problems

    # ------------------------------------------------------------------ 命令

    def apply(self, command: dict[str, Any]) -> dict[str, Any]:
        """应用一条命令，返回回执；相同业务键内容一致时沿用首次回执。"""
        problems = self._validate_static(command)
        key = command.get("business_key") if isinstance(command, Mapping) else None
        if problems:
            receipt = self._receipt(
                command if isinstance(command, Mapping) else {}, "rejected", "；".join(problems)
            )
            if isinstance(key, str) and key.strip():
                return self._store_receipt(key, content_hash(command), receipt, command)
            return receipt
        digest = content_hash(command)
        existing = self._receipts.get(key)
        if existing:
            if existing["content_hash"] == digest:
                return {**existing["receipt"], "replayed": True}
            return self._register_conflict(key, digest, command, existing.get("command", {}))
        prior = [e for e in self._events if e["payload"].get("business_key") == key]
        if prior:
            if prior[0]["payload"].get("content_hash") == digest:
                receipt = self._receipt(
                    command,
                    "accepted",
                    "相同业务键内容一致，沿用已入账事件",
                    [e["event_id"] for e in prior],
                    prior[0]["aggregate_id"],
                )
                return {**self._store_receipt(key, digest, receipt, command), "replayed": True}
            return self._register_conflict(key, digest, command, {})
        try:
            event_ids, case_id, message = self._execute(command)
        except Defer as hold:
            receipt = self._receipt(command, "deferred", f"前置事实未就绪，转入待办：{hold.reason}")
            self._pending.append(command)
            self.store.save_pending(self._pending)
            return self._store_receipt(key, digest, receipt, command)
        except Reject as deny:
            return self._store_receipt(key, digest, self._receipt(command, "rejected", deny.message), command)
        receipt = self._receipt(command, "accepted", message, event_ids, case_id)
        stored = self._store_receipt(key, digest, receipt, command)
        self._drain_pending()
        return stored

    def _register_conflict(
        self, key: str, new_hash: str, command: Mapping[str, Any], original: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        known = any(
            c.get("business_key") == key and c.get("incoming_hash") == new_hash for c in self._conflicts
        )
        if not known:
            record = {
                "business_key": key,
                "incoming_hash": new_hash,
                "differing_fields": _diff_fields(original, command) if original else [],
                "incoming_command": command,
                "detected_at": m.iso(self._now()),
            }
            if not original:
                record["note"] = "原始命令快照缺失（回执文件丢失），仅依据事件日志判定冲突"
            self._conflicts.append(record)
            self.store.append_conflict(record)
        receipt = self._receipt(
            command,
            "conflict",
            "业务键相同但病情、时间或身份等内容不一致，已登记冲突，不自动合并",
        )
        return {**receipt, "conflict": True}

    def _drain_pending(self) -> None:
        """前置事件入账后，按到达顺序补放待办命令。"""
        progressed = True
        while progressed:
            progressed = False
            for command in list(self._pending):
                key = command["business_key"]
                digest = content_hash(command)
                try:
                    event_ids, case_id, message = self._execute(command)
                except Defer:
                    continue
                except Reject as deny:
                    receipt = self._receipt(command, "rejected", deny.message)
                    self._store_receipt(key, digest, receipt, command)
                else:
                    receipt = self._receipt(command, "accepted", message, event_ids, case_id)
                    self._store_receipt(key, digest, receipt, command)
                self._pending.remove(command)
                self.store.save_pending(self._pending)
                progressed = True

    # ------------------------------------------------------------------ 执行

    def _require_case(self, data: Mapping[str, Any]) -> CaseState:
        case_id = data.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise Reject("case_id_required", "data.case_id 必须是非空字符串")
        case = self._cases.get(case_id)
        if case is None or case.referral is None:
            raise Defer(f"转诊单 {case_id!r} 尚未入账")
        return case

    def _emit(self, case: CaseState, event_type: str, payload: dict[str, Any], occurred_at: str) -> dict[str, Any]:
        event = {
            "event_id": f"evt-{case.case_id}-{case.version + 1:04d}",
            "event_type": event_type,
            "aggregate_type": m.EVENT_AGGREGATE[event_type],
            "aggregate_id": case.case_id,
            "occurred_at": occurred_at,
            "version": case.version + 1,
            "payload": payload,
        }
        if self.schema is not None:
            from .contracts import validate_event

            issues = validate_event(event, self.schema)
            if issues:
                raise RuntimeError(f"事件未通过契约校验: {[i.code for i in issues]}")
        self._events.append(event)
        case.apply(event)
        apply_booking_event(self._ledger, event)
        return event

    def _base_payload(self, command: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **command["data"],
            "actor_role": command["actor"]["role"],
            "actor_ref": command["actor"]["ref"],
            "business_key": command["business_key"],
            "content_hash": content_hash(command),
        }

    def _execute(self, command: Mapping[str, Any]) -> tuple[list[str], str, str]:
        before = len(self._events)
        handler = getattr(self, f"_do_{command['command']}")
        case_id, message = handler(command)
        new_events = self._events[before:]
        self.store.append_events(new_events)
        return [e["event_id"] for e in new_events], case_id, message

    def _scheduler(self) -> Scheduler:
        return Scheduler(self.store.load_resources(), self._ledger, self.policy, self._now())

    def _protected_cases(self) -> frozenset[str]:
        return frozenset(c.case_id for c in self._cases.values() if c.escalated)

    # -------------------------------------------------------------- 各命令

    def _do_receive_referral(self, command: Mapping[str, Any]) -> tuple[str, str]:
        data = command["data"]
        case_id = data["referral_no"]
        if case_id in self._cases:
            raise Reject("duplicate_case", f"转诊单号已存在: {case_id}")
        case = CaseState(case_id)
        self._cases[case_id] = case
        self._emit(case, m.REFERRAL_RECEIVED, self._base_payload(command), command["occurred_at"])
        return case_id, "转诊单已接收"

    def _do_record_prearrival_contact(self, command: Mapping[str, Any]) -> tuple[str, str]:
        case = self._require_case(command["data"])
        if case.arrival:
            raise Reject("invalid_state", "患者已到院，前置联系不再适用")
        self._emit(case, m.PREARRIVAL_CONTACTED, self._base_payload(command), command["occurred_at"])
        return case.case_id, "前置联系已记录"

    def _do_plan_arrival(self, command: Mapping[str, Any]) -> tuple[str, str]:
        case = self._require_case(command["data"])
        if case.arrival:
            raise Reject("invalid_state", "患者已到院，到达计划不再适用")
        self._emit(case, m.ARRIVAL_PLANNED, self._base_payload(command), command["occurred_at"])
        return case.case_id, "到达计划已登记"

    def _do_confirm_arrival(self, command: Mapping[str, Any]) -> tuple[str, str]:
        case = self._require_case(command["data"])
        if case.arrival:
            raise Reject("invalid_state", "到院已确认，请勿重复登记")
        if case.disposition:
            raise Reject("invalid_state", "已完成处置决定，不能再确认到院")
        self._emit(case, m.ARRIVAL_CONFIRMED, self._base_payload(command), command["occurred_at"])
        return case.case_id, "到院已确认"

    def _do_record_observation(self, command: Mapping[str, Any]) -> tuple[str, str]:
        case = self._require_case(command["data"])
        if not case.arrival:
            raise Defer("患者到院确认尚未入账")
        if case.disposition:
            raise Reject("invalid_state", "已完成处置决定，不能再补写现场观察")
        self._emit(case, m.OBSERVATION_RECORDED, self._base_payload(command), command["occurred_at"])
        return case.case_id, "现场观察已记录"

    def _do_escalate(self, command: Mapping[str, Any]) -> tuple[str, str]:
        case = self._require_case(command["data"])
        if case.active_escalation:
            raise Reject("invalid_state", "存在未补全依据的升级，请先由专科医生补全")
        if case.disposition:
            raise Reject("invalid_state", "已完成处置决定，不能再发起升级")
        data = command["data"]
        hold = data.get("minimal_hold")
        plan = None
        if isinstance(hold, Mapping) and hold.get("requests"):
            requests = [
                {**req, "booking_ref": self._next_booking_ref(case, i)}
                for i, req in enumerate(hold["requests"])
            ]
            candidate = self._scheduler().plan_bookings(
                case.case_id,
                requests,
                escalated=True,
                hold_kind="escalation_minimal",
                protected_case_ids=self._protected_cases(),
            )
            if candidate.ok:
                plan = candidate
        payload = self._base_payload(command)
        payload["minimal_hold"] = {"booked": [b["booking_ref"] for b in plan.bookings] if plan else []}
        self._emit(case, m.URGENCY_ESCALATED, payload, command["occurred_at"])
        booked = []
        if plan is not None:
            booked = self._book(case, plan, command, purpose="escalation_minimal")
        note = f"，已占用最小必要资源 {len(booked)} 项" if booked else ""
        return case.case_id, f"危急升级已登记{note}，待专科医生限期补全依据"

    def _do_justify_escalation(self, command: Mapping[str, Any]) -> tuple[str, str]:
        case = self._require_case(command["data"])
        active = case.active_escalation
        if active is None:
            if case.escalations:
                raise Reject("invalid_state", "升级依据已补全，请勿重复登记")
            raise Defer("危急升级尚未入账")
        payload = self._base_payload(command)
        payload["escalation_ref"] = active["event_id"]
        self._emit(case, m.ESCALATION_JUSTIFIED, payload, command["occurred_at"])
        return case.case_id, "升级依据已补全"

    def _next_booking_ref(self, case: CaseState, offset: int) -> str:
        count = sum(1 for b in self._ledger.values() if b["case_id"] == case.case_id)
        return f"bk-{case.case_id}-{count + offset + 1:03d}"

    def _book(self, case: CaseState, plan: Any, command: Mapping[str, Any], purpose: str) -> list[str]:
        """预约与抢占在同一命令内原子入账；被抢占释放记在受害案例时间线上。"""
        occurred_at = command["occurred_at"]
        by_victim: dict[str, list[dict[str, Any]]] = {}
        for item in plan.preemptions:
            by_victim.setdefault(item["case_id"], []).append(item)
        for victim_id in sorted(by_victim):
            victim = self._cases[victim_id]
            self._emit(
                victim,
                m.RESOURCE_RELEASED,
                {
                    "releases": [
                        {"booking_ref": p["booking_ref"], "resource_id": p["resource_id"], "slot_id": p["slot_id"]}
                        for p in sorted(by_victim[victim_id], key=lambda x: x["booking_ref"])
                    ],
                    "reason": m.RELEASE_PREEMPTED,
                    "preempted_by": case.case_id,
                    "actor_role": m.SYSTEM,
                    "actor_ref": "scheduler",
                    "business_key": f"preempt:{command['business_key']}:{victim_id}",
                },
                occurred_at,
            )
        bookings = []
        for booking in plan.bookings:
            record = dict(booking)
            if record["green_channel"] and not case.escalated and record["hold_kind"] != "escalation_minimal":
                record["hold_expires_at"] = m.iso(self._now() + self.policy.green_channel_max_hold)
            else:
                record["hold_expires_at"] = None
            bookings.append(record)
        payload = self._base_payload(command)
        payload["purpose"] = purpose
        payload["bookings"] = bookings
        if plan.preemptions:
            payload["preempted"] = [p["booking_ref"] for p in plan.preemptions]
        self._emit(case, m.RESOURCE_BOOKED, payload, occurred_at)
        return [b["booking_ref"] for b in bookings]

    def _do_book_resources(self, command: Mapping[str, Any]) -> tuple[str, str]:
        case = self._require_case(command["data"])
        if case.disposition:
            raise Reject("invalid_state", "已完成处置决定，检查预约由复查或门诊承接")
        requests = [
            {**req, "booking_ref": self._next_booking_ref(case, i)}
            for i, req in enumerate(command["data"]["requests"])
        ]
        plan = self._scheduler().plan_bookings(
            case.case_id,
            requests,
            escalated=case.escalated,
            hold_kind="normal",
            protected_case_ids=self._protected_cases(),
        )
        if not plan.ok:
            raise Reject(plan.code, plan.message)
        refs = self._book(case, plan, command, purpose=command["data"]["purpose"])
        preempted = f"，抢占未冻结预约 {len(plan.preemptions)} 项" if plan.preemptions else ""
        return case.case_id, f"已原子预约 {len(refs)} 项资源{preempted}"

    def _do_release_resources(self, command: Mapping[str, Any]) -> tuple[str, str]:
        case = self._require_case(command["data"])
        data = command["data"]
        refs = data.get("booking_refs")
        active = [b for b in self._ledger.values() if b["case_id"] == case.case_id and b["status"] == "active"]
        if refs:
            chosen = [b for b in active if b["booking_ref"] in refs]
            missing = sorted(set(refs) - {b["booking_ref"] for b in chosen})
            if missing:
                raise Reject("unknown_booking", f"预约不存在或已释放: {missing}")
        else:
            chosen = active
        if not chosen:
            raise Reject("nothing_to_release", "没有可释放的预约")
        self._release(case, chosen, data["reason"], command["actor"], command["occurred_at"])
        return case.case_id, f"已释放未使用预约 {len(chosen)} 项"

    def _release(
        self,
        case: CaseState,
        bookings: list[dict[str, Any]],
        reason: str,
        actor: Mapping[str, Any],
        occurred_at: str,
    ) -> None:
        self._emit(
            case,
            m.RESOURCE_RELEASED,
            {
                "releases": [
                    {"booking_ref": b["booking_ref"], "resource_id": b["resource_id"], "slot_id": b["slot_id"]}
                    for b in sorted(bookings, key=lambda x: x["booking_ref"])
                ],
                "reason": reason,
                "actor_role": actor.get("role"),
                "actor_ref": actor.get("ref"),
            },
            occurred_at,
        )

    def _do_assign_escort(self, command: Mapping[str, Any]) -> tuple[str, str]:
        case = self._require_case(command["data"])
        if case.disposition:
            raise Reject("invalid_state", "已完成处置决定，陪护责任已转交科室")
        self._emit(case, m.ESCORT_ASSIGNED, self._base_payload(command), command["occurred_at"])
        return case.case_id, "陪护责任已指定"

    def _do_decide_disposition(self, command: Mapping[str, Any]) -> tuple[str, str]:
        case = self._require_case(command["data"])
        if not case.observation:
            raise Defer("现场观察尚未入账")
        if case.disposition:
            raise Reject("invalid_state", "处置决定已登记，请勿重复")
        self._emit(case, m.DISPOSITION_DECIDED, self._base_payload(command), command["occurred_at"])
        return case.case_id, "住院或离院决定已登记"

    def _do_plan_downward(self, command: Mapping[str, Any]) -> tuple[str, str]:
        case = self._require_case(command["data"])
        if not case.disposition:
            raise Defer("住院或离院决定尚未入账")
        if case.downward:
            raise Reject("invalid_state", "下转方案已登记，请勿重复")
        self._emit(case, m.DOWNWARD_PLANNED, self._base_payload(command), command["occurred_at"])
        return case.case_id, "下转方案已登记"

    def _do_confirm_downward_handover(self, command: Mapping[str, Any]) -> tuple[str, str]:
        case = self._require_case(command["data"])
        if not case.downward:
            raise Defer("下转方案尚未入账")
        if case.handover:
            raise Reject("invalid_state", "下转交接已确认，请勿重复")
        self._emit(case, m.DOWNWARD_HANDOVER_CONFIRMED, self._base_payload(command), command["occurred_at"])
        return case.case_id, "下转交接已确认"

    def _do_issue_review_checklist(self, command: Mapping[str, Any]) -> tuple[str, str]:
        case = self._require_case(command["data"])
        if not case.disposition:
            raise Defer("住院或离院决定尚未入账")
        items = command["data"]["items"]
        if not isinstance(items, list) or not items:
            raise Reject("empty_checklist", "复查清单不能为空")
        for index, item in enumerate(items):
            if not isinstance(item, Mapping) or not item.get("item_id") or not item.get("title"):
                raise Reject("bad_checklist_item", f"复查项目 {index} 缺少 item_id 或 title")
        self._emit(case, m.REVIEW_CHECKLIST_ISSUED, self._base_payload(command), command["occurred_at"])
        return case.case_id, f"复查清单已开具（{len(items)} 项）"

    def _do_complete_followup(self, command: Mapping[str, Any]) -> tuple[str, str]:
        case = self._require_case(command["data"])
        if not case.checklist:
            raise Defer("复查清单尚未入账")
        done = command["data"].get("completed_review_items", [])
        unknown = sorted(set(done) - set(case.checklist))
        if unknown:
            raise Reject("unknown_review_item", f"复查项目不存在: {unknown}")
        self._emit(case, m.FOLLOWUP_COMPLETED, self._base_payload(command), command["occurred_at"])
        return case.case_id, "回访结果已登记"

    # ------------------------------------------------------------------ 时钟

    def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """推进可控时钟：检查接站、升级依据、检查、下转、复查与回访期限。

        幂等：同一期限只登记一次违约；重启后从事件日志重建待办继续计时。
        """
        now = now or self._now()
        last = self._meta.get("last_tick_at")
        if last and now < m.parse_dt(last):
            raise Reject("clock_regression", "时钟不得回拨")
        before = len(self._events)
        for case_id in sorted(self._cases):
            self._tick_case(self._cases[case_id], now)
        emitted = self._events[before:]
        if emitted:
            self.store.append_events(emitted)
        self._meta["last_tick_at"] = m.iso(now)
        self.store.save_meta(self._meta)
        return emitted

    def _breach(self, case: CaseState, kind: str, ref: str, due_at: str, now: datetime) -> bool:
        if case.breached(kind, ref):
            return False
        self._emit(
            case,
            m.DEADLINE_BREACHED,
            {
                "kind": kind,
                "ref": ref,
                "due_at": due_at,
                "actor_role": m.SYSTEM,
                "actor_ref": "clock",
                "business_key": f"tick:{kind}:{case.case_id}:{ref}",
            },
            m.iso(now),
        )
        return True

    def _active_bookings(self, case: CaseState) -> list[dict[str, Any]]:
        return [
            b for b in self._ledger.values() if b["case_id"] == case.case_id and b["status"] == "active"
        ]

    def _tick_case(self, case: CaseState, now: datetime) -> None:
        system_actor = {"role": m.SYSTEM, "ref": "clock"}
        # 接站期限：患者未到则释放尚未使用的预约
        plan = case.arrival_plan
        if plan and not case.arrival:
            due = m.parse_dt(plan["planned_arrival_at"]) + self.policy.pickup_grace
            if now > due and self._breach(case, m.BREACH_PICKUP, "pickup", m.iso(due), now):
                unused = self._active_bookings(case)
                if unused:
                    self._release(case, unused, m.RELEASE_NO_SHOW, system_actor, m.iso(now))
        # 升级依据期限：逾期释放最小必要占用
        active = case.active_escalation
        if active:
            due_at = active["payload"]["review_due_at"]
            if now > m.parse_dt(due_at) and self._breach(
                case, m.BREACH_ESCALATION_REVIEW, active["event_id"], due_at, now
            ):
                holds = [b for b in self._active_bookings(case) if b.get("hold_kind") == "escalation_minimal"]
                if holds:
                    self._release(case, holds, m.RELEASE_JUSTIFICATION_OVERDUE, system_actor, m.iso(now))
        # 绿色通道占用上限：普通案例不得长期占用稀缺通道
        if not case.escalated:
            for booking in self._active_bookings(case):
                expires = booking.get("hold_expires_at")
                if (
                    booking.get("green_channel")
                    and expires
                    and now > m.parse_dt(expires)
                    and self._breach(case, m.BREACH_GREEN_HOLD, booking["booking_ref"], expires, now)
                ):
                    self._release(case, [booking], m.RELEASE_GREEN_HOLD_EXPIRED, system_actor, m.iso(now))
        # 检查窗口期限：窗口已过仍未使用则释放；已到院未处置的登记违约
        for booking in self._active_bookings(case):
            end_at = booking.get("end_at")
            if end_at and now > m.parse_dt(end_at):
                if case.arrival and not case.disposition and booking.get("hold_kind") != "escalation_minimal":
                    self._breach(case, m.BREACH_EXAM_WINDOW, booking["booking_ref"], end_at, now)
                self._release(case, [booking], m.RELEASE_WINDOW_ELAPSED, system_actor, m.iso(now))
        # 下转交接期限
        if case.downward and not case.handover:
            due_at = case.downward["handover_deadline"]
            if now > m.parse_dt(due_at):
                self._breach(case, m.BREACH_DOWNWARD, "downward", due_at, now)
        # 复查项目期限
        for item in case.open_items:
            due_at = item.get("due_at")
            if due_at and now > m.parse_dt(due_at):
                self._breach(case, m.BREACH_REVIEW_ITEM, item["item_id"], due_at, now)
        # 回访期限
        if case.checklist and not case.followups and case.followup_due_at:
            if now > m.parse_dt(case.followup_due_at):
                self._breach(case, m.BREACH_FOLLOWUP, "followup", case.followup_due_at, now)
