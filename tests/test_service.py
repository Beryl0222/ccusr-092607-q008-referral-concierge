"""闭环服务行为测试：角色分离、幂等、冲突、乱序、升级、冻结抢占、重启、时钟。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from referral_concierge.clock import ManualClock
from referral_concierge.service import ClosedLoopService, Reject
from referral_concierge.store import Store

SCHEMA = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))

PRIMARY = {"role": "primary_doctor", "ref": "dr-li"}
CONCIERGE = {"role": "concierge", "ref": "guan-jia"}
SPECIALIST = {"role": "specialist", "ref": "dr-chen"}

RESOURCES = {
    "resources": [
        {
            "resource_id": "ct",
            "name": "CT室",
            "slots": [
                {"slot_id": "s0900", "start_at": "2026-09-25T09:00:00+08:00", "end_at": "2026-09-25T10:00:00+08:00", "capacity": 1},
                {"slot_id": "s1300", "start_at": "2026-09-25T13:00:00+08:00", "end_at": "2026-09-25T14:00:00+08:00", "capacity": 1},
            ],
        },
        {
            "resource_id": "green",
            "name": "绿色抢救床位",
            "green_channel": True,
            "slots": [
                {"slot_id": "gday", "start_at": "2026-09-25T08:00:00+08:00", "end_at": "2026-09-25T20:00:00+08:00", "capacity": 1}
            ],
        },
        {
            "resource_id": "escort",
            "name": "陪护专班",
            "slots": [
                {"slot_id": "eday", "start_at": "2026-09-25T08:00:00+08:00", "end_at": "2026-09-25T20:00:00+08:00", "capacity": 2}
            ],
        },
    ]
}


def command(name: str, key: str, actor: dict, at: str, data: dict) -> dict:
    return {"command": name, "business_key": key, "actor": actor, "occurred_at": at, "data": data}


def referral(case_id: str = "C1", complaint: str = "胸痛两小时", key: str = "k-recv") -> dict:
    return command(
        "receive_referral", key, PRIMARY, "2026-09-25T08:00:00+08:00",
        {
            "referral_no": case_id,
            "patient": {"patient_ref": f"pat-{case_id}", "name": "患者甲", "id_ref": "ID-001"},
            "referring_facility": "张谷镇卫生院",
            "primary_doctor_ref": "dr-li",
            "chief_complaint": complaint,
            "contact": {"name": "家属", "phone": "0839-5550000"},
        },
    )


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.board = Path(self.tmp.name) / "board"
        self.store = Store.initialize(self.board, resources=RESOURCES, policy={})
        self.clock = ManualClock.at("2026-09-25T08:00:00+08:00")
        self.service = ClosedLoopService(self.store, self.clock, schema=SCHEMA)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def apply(self, cmd: dict) -> dict:
        return self.service.apply(cmd)

    def reopen(self) -> ClosedLoopService:
        """模拟重启：一切从磁盘重建。"""
        self.service = ClosedLoopService(self.store, self.clock, schema=SCHEMA)
        return self.service

    def arrive(self, case_id: str = "C1", acuity: str = "routine", prefix: str = "") -> None:
        self.apply(command("confirm_arrival", f"k-{prefix}arrive", CONCIERGE, "2026-09-25T08:20:00+08:00",
                           {"case_id": case_id, "observed_at": "2026-09-25T08:20:00+08:00", "assessor_ref": "nurse-qian"}))
        self.apply(command("record_observation", f"k-{prefix}obs", CONCIERGE, "2026-09-25T08:25:00+08:00",
                           {"case_id": case_id, "observed_at": "2026-09-25T08:25:00+08:00",
                            "assessor_ref": "nurse-qian", "acuity": acuity, "vitals_summary": "平稳"}))

    def full_loop(self) -> None:
        """走完到院-离院-下转-清单-交接（回访与复查核销留给用例）。"""
        self.apply(referral())
        self.arrive()
        self.apply(command("assign_escort", "k-es", CONCIERGE, "2026-09-25T08:40:00+08:00",
                           {"case_id": "C1", "escort_ref": "escort-sun",
                            "responsible_contact": {"name": "孙陪护", "phone": "0839-5550303"}}))
        self.apply(command("decide_disposition", "k-dp", SPECIALIST, "2026-09-25T09:00:00+08:00",
                           {"case_id": "C1", "decision": "discharge", "department": "消化内"}))
        self.apply(command("plan_downward", "k-dw", SPECIALIST, "2026-09-25T09:10:00+08:00",
                           {"case_id": "C1", "target_facility": "张谷镇卫生院", "plan_summary": "带药回镇",
                            "handover_deadline": "2026-09-25T18:00:00+08:00"}))
        self.apply(command("issue_review_checklist", "k-rc", SPECIALIST, "2026-09-25T09:15:00+08:00",
                           {"case_id": "C1", "items": [{"item_id": "i1", "title": "超声复查",
                                                        "due_at": "2026-09-28T09:00:00+08:00"}],
                            "followup_due_at": "2026-09-28T12:00:00+08:00"}))
        self.apply(command("confirm_downward_handover", "k-ho", CONCIERGE, "2026-09-25T15:00:00+08:00",
                           {"case_id": "C1", "confirmed_at": "2026-09-25T15:00:00+08:00",
                            "receiving_facility": "张谷镇卫生院"}))


class RoleSeparationTests(ServiceTestBase):
    def test_roles_cannot_cross_commands(self) -> None:
        self.apply(referral())
        cases = [
            (command("record_observation", "x", PRIMARY, "2026-09-25T08:20:00+08:00",
                     {"case_id": "C1", "observed_at": "2026-09-25T08:20:00+08:00",
                      "assessor_ref": "n", "acuity": "routine", "vitals_summary": "x"}), "role_not_allowed"),
            (command("decide_disposition", "x", CONCIERGE, "2026-09-25T09:00:00+08:00",
                     {"case_id": "C1", "decision": "admit", "department": "心内"}), "role_not_allowed"),
            (command("confirm_arrival", "x", SPECIALIST, "2026-09-25T08:20:00+08:00",
                     {"case_id": "C1", "observed_at": "2026-09-25T08:20:00+08:00", "assessor_ref": "n"}), "role_not_allowed"),
        ]
        for cmd, marker in cases:
            receipt = self.apply(cmd)
            self.assertEqual("rejected", receipt["status"])
            self.assertIn(marker, receipt["message"])

    def test_concierge_cannot_write_clinical_conclusions(self) -> None:
        self.apply(referral())
        self.apply(command("confirm_arrival", "k-arr", CONCIERGE, "2026-09-25T08:20:00+08:00",
                           {"case_id": "C1", "observed_at": "2026-09-25T08:20:00+08:00", "assessor_ref": "n"}))
        receipt = self.apply(command(
            "record_observation", "k-bad", CONCIERGE, "2026-09-25T08:25:00+08:00",
            {"case_id": "C1", "observed_at": "2026-09-25T08:25:00+08:00", "assessor_ref": "n",
             "acuity": "routine", "vitals_summary": "平稳", "disposition": "admit"}))
        self.assertEqual("rejected", receipt["status"])
        self.assertIn("cross_role_field", receipt["message"])

    def test_primary_cannot_embed_acuity_in_referral(self) -> None:
        cmd = referral()
        cmd["data"]["acuity"] = "critical"
        receipt = self.apply(cmd)
        self.assertEqual("rejected", receipt["status"])
        self.assertIn("cross_role_field", receipt["message"])


class IdempotencyConflictTests(ServiceTestBase):
    def test_same_business_key_replays_receipt(self) -> None:
        first = self.apply(referral())
        again = self.apply(referral())
        self.assertEqual("accepted", again["status"])
        self.assertTrue(again["replayed"])
        self.assertEqual(first["event_ids"], again["event_ids"])
        self.assertEqual(1, len(self.service.events()))

    def test_replay_survives_restart_without_receipts_file(self) -> None:
        cmd = referral()
        first = self.apply(cmd)
        # 回执文件被清掉：从事件日志的 content_hash 重建回执
        self.store.save_receipts({})
        service = self.reopen()
        again = service.apply(cmd)
        self.assertEqual("accepted", again["status"])
        self.assertTrue(again["replayed"])
        self.assertEqual(first["event_ids"], again["event_ids"])

    def test_content_conflict_is_isolated_not_merged(self) -> None:
        self.apply(referral(complaint="胸痛两小时"))
        changed = referral(complaint="电话改口：只是轻微胸闷")
        receipt = self.apply(changed)
        self.assertEqual("conflict", receipt["status"])
        # 原事实不变，新事实不并入
        self.assertEqual(1, len(self.service.events()))
        self.assertEqual("胸痛两小时", self.service.case("C1").referral["chief_complaint"])
        conflicts = self.store.load_conflicts()
        self.assertEqual(1, len(conflicts))
        self.assertIn("data.chief_complaint", conflicts[0]["differing_fields"])
        # 同一冲突内容重放不重复登记
        self.apply(changed)
        self.assertEqual(1, len(self.store.load_conflicts()))


class OutOfOrderTests(ServiceTestBase):
    def test_early_observation_defers_then_drains(self) -> None:
        self.apply(referral())
        obs = command("record_observation", "k-obs-early", CONCIERGE, "2026-09-25T08:25:00+08:00",
                      {"case_id": "C1", "observed_at": "2026-09-25T08:25:00+08:00",
                       "assessor_ref": "n", "acuity": "routine", "vitals_summary": "平稳"})
        deferred = self.apply(obs)
        self.assertEqual("deferred", deferred["status"])
        self.assertEqual(1, len(self.service.pending()))
        # 前置到院确认到达后自动补放
        self.apply(command("confirm_arrival", "k-arr", CONCIERGE, "2026-09-25T08:20:00+08:00",
                           {"case_id": "C1", "observed_at": "2026-09-25T08:20:00+08:00", "assessor_ref": "n"}))
        self.assertEqual([], self.service.pending())
        self.assertIsNotNone(self.service.case("C1").observation)

    def test_pending_survives_restart_and_drains(self) -> None:
        self.apply(referral())
        self.apply(command("record_observation", "k-obs-early", CONCIERGE, "2026-09-25T08:25:00+08:00",
                           {"case_id": "C1", "observed_at": "2026-09-25T08:25:00+08:00",
                            "assessor_ref": "n", "acuity": "routine", "vitals_summary": "平稳"}))
        service = self.reopen()
        self.assertEqual(1, len(service.pending()))
        service.apply(command("confirm_arrival", "k-arr", CONCIERGE, "2026-09-25T08:20:00+08:00",
                              {"case_id": "C1", "observed_at": "2026-09-25T08:20:00+08:00", "assessor_ref": "n"}))
        self.assertEqual([], service.pending())
        self.assertIsNotNone(service.case("C1").observation)


class EscalationTests(ServiceTestBase):
    def _escalate(self, due: str = "2026-09-25T09:00:00+08:00", hold: bool = True) -> dict:
        data = {"case_id": "C1", "level": "critical", "reason": "现场比电话危重，疑似心梗", "review_due_at": due}
        if hold:
            data["minimal_hold"] = {"requests": [{"resource_id": "green", "slot_id": "gday"}]}
        return self.apply(command("escalate", "k-esc", CONCIERGE, "2026-09-25T08:30:00+08:00", data))

    def test_minimal_hold_then_justified_in_time(self) -> None:
        self.apply(referral())
        self.arrive()
        receipt = self._escalate()
        self.assertEqual("accepted", receipt["status"])
        active = [b for b in self.service.ledger().values() if b["status"] == "active"]
        self.assertEqual(1, len(active))
        self.assertEqual("escalation_minimal", active[0]["hold_kind"])
        justified = self.apply(command("justify_escalation", "k-jus", SPECIALIST, "2026-09-25T08:50:00+08:00",
                                       {"case_id": "C1", "justification": "心电图 STEMI"}))
        self.assertEqual("accepted", justified["status"])
        self.assertIsNone(self.service.case("C1").active_escalation)

    def test_overdue_justification_releases_minimal_hold(self) -> None:
        self.apply(referral())
        self.arrive()
        self._escalate()
        self.clock.set(ManualClock.at("2026-09-25T09:30:00+08:00").now())
        events = self.service.tick()
        kinds = [(e["event_type"], e["payload"].get("kind")) for e in events]
        self.assertIn(("DEADLINE_BREACHED", "escalation_review"), kinds)
        released = [b for b in self.service.ledger().values() if b["status"] == "released"]
        self.assertEqual("justification_overdue", released[0]["release_reason"])
        # 逾期后不能再补写依据让记录"合规"
        receipt = self.apply(command("justify_escalation", "k-jus-late", SPECIALIST, "2026-09-25T09:35:00+08:00",
                                     {"case_id": "C1", "justification": "迟到依据"}))
        self.assertEqual("accepted", receipt["status"])
        audit_esc = self.service.case("C1").escalations[0]
        self.assertTrue(audit_esc["justified"])

    def test_double_escalation_without_justification_rejected(self) -> None:
        self.apply(referral())
        self.arrive()
        self._escalate(hold=False)
        again = self.apply(command("escalate", "k-esc2", CONCIERGE, "2026-09-25T08:31:00+08:00",
                                   {"case_id": "C1", "level": "urgent", "reason": "再次升级",
                                    "review_due_at": "2026-09-25T09:30:00+08:00"}))
        self.assertEqual("rejected", again["status"])


class SchedulingTests(ServiceTestBase):
    def _book(self, case_id: str, slot: str, key: str, at: str = "2026-09-25T08:10:00+08:00") -> dict:
        return self.apply(command("book_resources", key, CONCIERGE, at,
                                  {"case_id": case_id, "purpose": "exam",
                                   "requests": [{"resource_id": "ct", "slot_id": slot}]}))

    def test_atomic_booking_all_or_nothing(self) -> None:
        self.apply(referral())
        receipt = self.apply(command(
            "book_resources", "k-multi", CONCIERGE, "2026-09-25T08:10:00+08:00",
            {"case_id": "C1", "purpose": "exam",
             "requests": [{"resource_id": "ct", "slot_id": "s1300"}, {"resource_id": "ct", "slot_id": "nope"}]}))
        self.assertEqual("rejected", receipt["status"])
        self.assertEqual(0, len(self.service.ledger()))

    def test_critical_preempts_unfrozen_routine_booking(self) -> None:
        self.apply(referral("C1", key="k-r1"))
        self.apply(referral("C2", key="k-r2"))
        first = self._book("C1", "s1300", "k-b1")
        self.assertEqual("accepted", first["status"])
        # C2 升级后争用同一窗口：距窗口 5 小时，未冻结，可抢占
        self.arrive("C2", acuity="critical", prefix="c2-")
        self.apply(command("escalate", "k-esc2", CONCIERGE, "2026-09-25T08:30:00+08:00",
                           {"case_id": "C2", "level": "critical", "reason": "危重",
                            "review_due_at": "2026-09-25T09:30:00+08:00"}))
        grab = self._book("C2", "s1300", "k-b2", at="2026-09-25T08:31:00+08:00")
        self.assertEqual("accepted", grab["status"])
        victim = next(b for b in self.service.ledger().values() if b["case_id"] == "C1")
        self.assertEqual("released", victim["status"])
        self.assertEqual("preempted", victim["release_reason"])
        # 受害案例时间线上有释放记录，审计可还原协调过程
        self.assertTrue(any(e["event_type"] == "RESOURCE_RELEASED" for e in self.service.case("C1").events))

    def test_frozen_slot_cannot_be_preempted(self) -> None:
        self.apply(referral("C1", key="k-r1"))
        self.apply(referral("C2", key="k-r2"))
        self._book("C1", "s0900", "k-b1")  # 距 09:00 仅 1 小时，进入冻结期
        self.arrive("C2", acuity="critical", prefix="c2-")
        self.apply(command("escalate", "k-esc2", CONCIERGE, "2026-09-25T08:30:00+08:00",
                           {"case_id": "C2", "level": "critical", "reason": "危重",
                            "review_due_at": "2026-09-25T09:30:00+08:00"}))
        grab = self._book("C2", "s0900", "k-b2", at="2026-09-25T08:31:00+08:00")
        self.assertEqual("rejected", grab["status"])
        self.assertIn("冻结", grab["message"])
        # 原预约仍然有效
        self.assertEqual("active", next(iter(self.service.ledger().values()))["status"])

    def test_routine_cannot_preempt(self) -> None:
        self.apply(referral("C1", key="k-r1"))
        self.apply(referral("C2", key="k-r2"))
        self._book("C1", "s1300", "k-b1")
        grab = self._book("C2", "s1300", "k-b2")
        self.assertEqual("rejected", grab["status"])
        self.assertNotIn("冻结", grab["message"])

    def test_minimal_hold_is_protected_from_preemption(self) -> None:
        self.apply(referral("C1", key="k-r1"))
        self.apply(referral("C2", key="k-r2"))
        self.arrive("C1", acuity="critical")
        self.apply(command("escalate", "k-esc1", CONCIERGE, "2026-09-25T08:30:00+08:00",
                           {"case_id": "C1", "level": "critical", "reason": "危重",
                            "review_due_at": "2026-09-25T09:30:00+08:00",
                            "minimal_hold": {"requests": [{"resource_id": "green", "slot_id": "gday"}]}}))
        self.arrive("C2", acuity="critical", prefix="c2-")
        receipt = self.apply(command(
            "escalate", "k-esc2", CONCIERGE, "2026-09-25T08:31:00+08:00",
            {"case_id": "C2", "level": "critical", "reason": "同样危重",
             "review_due_at": "2026-09-25T09:31:00+08:00",
             "minimal_hold": {"requests": [{"resource_id": "green", "slot_id": "gday"}]}}))
        self.assertEqual("accepted", receipt["status"])
        holds = [b for b in self.service.ledger().values() if b.get("hold_kind") == "escalation_minimal"]
        self.assertEqual(1, len(holds))  # C2 没抢到，C1 的最小占用受保护

    def test_capacity_respected(self) -> None:
        self.apply(referral("C1", key="k-r1"))
        self.apply(referral("C2", key="k-r2"))
        self.apply(referral("C3", key="k-r3"))
        for idx, cid in enumerate(("C1", "C2", "C3")):
            receipt = self.apply(command(
                f"book_resources", f"k-esc{idx}", CONCIERGE, "2026-09-25T08:10:00+08:00",
                {"case_id": cid, "purpose": "escort",
                 "requests": [{"resource_id": "escort", "slot_id": "eday"}]}))
            if cid == "C3":
                self.assertEqual("rejected", receipt["status"])
            else:
                self.assertEqual("accepted", receipt["status"])


class ReleaseTests(ServiceTestBase):
    def test_needs_change_releases_unused_bookings(self) -> None:
        self.apply(referral())
        self.apply(command("book_resources", "k-b", CONCIERGE, "2026-09-25T08:10:00+08:00",
                           {"case_id": "C1", "purpose": "exam",
                            "requests": [{"resource_id": "ct", "slot_id": "s1300"}]}))
        receipt = self.apply(command("release_resources", "k-rel", CONCIERGE, "2026-09-25T08:15:00+08:00",
                                     {"case_id": "C1", "reason": "needs_changed"}))
        self.assertEqual("accepted", receipt["status"])
        self.assertEqual("released", next(iter(self.service.ledger().values()))["status"])

    def test_no_show_after_pickup_grace_releases(self) -> None:
        self.apply(referral())
        self.apply(command("plan_arrival", "k-plan", CONCIERGE, "2026-09-25T08:10:00+08:00",
                           {"case_id": "C1", "planned_arrival_at": "2026-09-25T09:00:00+08:00",
                            "pickup_point": "南门", "greeter_ref": "greeter-zhao"}))
        self.apply(command("book_resources", "k-b", CONCIERGE, "2026-09-25T08:11:00+08:00",
                           {"case_id": "C1", "purpose": "exam",
                            "requests": [{"resource_id": "ct", "slot_id": "s1300"}]}))
        self.clock.set(ManualClock.at("2026-09-25T09:40:00+08:00").now())  # 超过 30 分钟接站宽限
        events = self.service.tick()
        kinds = [e["payload"].get("kind") for e in events if e["event_type"] == "DEADLINE_BREACHED"]
        self.assertIn("pickup", kinds)
        self.assertEqual("no_show", next(iter(self.service.ledger().values()))["release_reason"])

    def test_green_channel_hold_expires_for_routine_case(self) -> None:
        self.apply(referral())
        receipt = self.apply(command("book_resources", "k-gb", CONCIERGE, "2026-09-25T08:10:00+08:00",
                                     {"case_id": "C1", "purpose": "fast_track",
                                      "requests": [{"resource_id": "green", "slot_id": "gday"}]}))
        self.assertEqual("accepted", receipt["status"])
        hold = next(iter(self.service.ledger().values()))
        self.assertEqual("2026-09-25T12:00:00+08:00", hold["hold_expires_at"])
        self.clock.set(ManualClock.at("2026-09-25T12:30:00+08:00").now())
        self.service.tick()
        self.assertEqual("green_channel_hold_expired", next(iter(self.service.ledger().values()))["release_reason"])


class ClockTests(ServiceTestBase):
    def test_tick_is_idempotent(self) -> None:
        self.full_loop()
        # 复查与回访期限都在 09-28，越过期限后每个期限只登记一次违约
        self.clock.set(ManualClock.at("2026-09-28T13:00:00+08:00").now())
        first = self.service.tick()
        second = self.service.tick()
        kinds = sorted(e["payload"].get("kind") for e in first if e["event_type"] == "DEADLINE_BREACHED")
        self.assertEqual(["followup", "review_item"], kinds)
        self.assertEqual([], second)

    def test_clock_cannot_go_backwards(self) -> None:
        self.clock.set(ManualClock.at("2026-09-26T10:00:00+08:00").now())
        self.service.tick()
        with self.assertRaises(Reject):
            self.service.tick(ManualClock.at("2026-09-26T09:00:00+08:00").now())

    def test_downward_deadline_breach(self) -> None:
        self.apply(referral())
        self.arrive()
        self.apply(command("decide_disposition", "k-dp", SPECIALIST, "2026-09-25T09:00:00+08:00",
                           {"case_id": "C1", "decision": "discharge", "department": "消化内"}))
        self.apply(command("plan_downward", "k-dw", SPECIALIST, "2026-09-25T09:10:00+08:00",
                           {"case_id": "C1", "target_facility": "张谷镇卫生院", "plan_summary": "带药回镇",
                            "handover_deadline": "2026-09-25T12:00:00+08:00"}))
        self.clock.set(ManualClock.at("2026-09-25T13:00:00+08:00").now())
        events = self.service.tick()
        self.assertIn("downward_handover", [e["payload"].get("kind") for e in events])


class ClosedLoopTests(ServiceTestBase):
    def test_not_closed_until_rehab_continues(self) -> None:
        self.full_loop()
        case = self.service.case("C1")
        self.assertFalse(case.closed)
        self.assertFalse(case.rehab_continued)
        self.apply(command("complete_followup", "k-fu", CONCIERGE, "2026-09-28T10:00:00+08:00",
                           {"case_id": "C1", "followed_at": "2026-09-28T10:00:00+08:00",
                            "outcome": "恢复良好", "next_action": "无需复诊",
                            "completed_review_items": ["i1"]}))
        self.assertTrue(self.service.case("C1").closed)
        self.assertTrue(self.service.case("C1").rehab_continued)

    def test_unknown_review_item_rejected(self) -> None:
        self.full_loop()
        receipt = self.apply(command("complete_followup", "k-fu-bad", CONCIERGE, "2026-09-28T10:00:00+08:00",
                                     {"case_id": "C1", "followed_at": "2026-09-28T10:00:00+08:00",
                                      "outcome": "x", "next_action": "y", "completed_review_items": ["nope"]}))
        self.assertEqual("rejected", receipt["status"])


if __name__ == "__main__":
    unittest.main()
