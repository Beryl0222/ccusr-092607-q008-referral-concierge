"""视图权限与审计还原测试。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from referral_concierge.audit import build_audit
from referral_concierge.clock import ManualClock
from referral_concierge.service import ClosedLoopService
from referral_concierge.store import Store
from referral_concierge.views import hospital_view, patient_view, primary_view

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
                {"slot_id": "s1300", "start_at": "2026-09-25T13:00:00+08:00", "end_at": "2026-09-25T14:00:00+08:00", "capacity": 1}
            ],
        }
    ]
}


def command(name: str, key: str, actor: dict, at: str, data: dict) -> dict:
    return {"command": name, "business_key": key, "actor": actor, "occurred_at": at, "data": data}


class ViewAuditBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store.initialize(Path(self.tmp.name) / "board", resources=RESOURCES, policy={})
        self.clock = ManualClock.at("2026-09-25T08:00:00+08:00")
        self.service = ClosedLoopService(self.store, self.clock, schema=SCHEMA)
        self.apply = self.service.apply

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build_critical_case(self) -> None:
        """危急案例：升级-补依据-抢占-住院-清单-回访，供视图与审计断言。"""
        self.apply(command("receive_referral", "k-r1", PRIMARY, "2026-09-25T08:00:00+08:00",
                           {"referral_no": "C1",
                            "patient": {"patient_ref": "pat-1", "name": "王桂兰", "id_ref": "ID-1"},
                            "referring_facility": "张谷镇卫生院", "primary_doctor_ref": "dr-li",
                            "chief_complaint": "电话中描述：胸痛", "contact": {"name": "家属", "phone": "1"}}))
        self.apply(command("receive_referral", "k-r2", PRIMARY, "2026-09-25T08:01:00+08:00",
                           {"referral_no": "C2",
                            "patient": {"patient_ref": "pat-2", "name": "李建国", "id_ref": "ID-2"},
                            "referring_facility": "张谷镇卫生院", "primary_doctor_ref": "dr-li",
                            "chief_complaint": "腹痛", "contact": {"name": "家属", "phone": "2"}}))
        self.apply(command("book_resources", "k-b2", CONCIERGE, "2026-09-25T08:05:00+08:00",
                           {"case_id": "C2", "purpose": "exam",
                            "requests": [{"resource_id": "ct", "slot_id": "s1300"}]}))
        self.apply(command("confirm_arrival", "k-a1", CONCIERGE, "2026-09-25T08:20:00+08:00",
                           {"case_id": "C1", "observed_at": "2026-09-25T08:20:00+08:00", "assessor_ref": "nurse-qian"}))
        self.apply(command("record_observation", "k-o1", CONCIERGE, "2026-09-25T08:25:00+08:00",
                           {"case_id": "C1", "observed_at": "2026-09-25T08:25:00+08:00",
                            "assessor_ref": "nurse-qian", "acuity": "critical", "vitals_summary": "BP 90/60"}))
        self.apply(command("escalate", "k-e1", CONCIERGE, "2026-09-25T08:30:00+08:00",
                           {"case_id": "C1", "level": "critical", "reason": "现场状态差于电话描述",
                            "review_due_at": "2026-09-25T09:00:00+08:00"}))
        self.apply(command("justify_escalation", "k-j1", SPECIALIST, "2026-09-25T08:50:00+08:00",
                           {"case_id": "C1", "justification": "心电图 STEMI，符合危急升级"}))
        self.apply(command("book_resources", "k-b1", CONCIERGE, "2026-09-25T08:55:00+08:00",
                           {"case_id": "C1", "purpose": "emergency_exam",
                            "requests": [{"resource_id": "ct", "slot_id": "s1300"}]}))
        self.apply(command("assign_escort", "k-s1", CONCIERGE, "2026-09-25T09:00:00+08:00",
                           {"case_id": "C1", "escort_ref": "escort-zhou",
                            "responsible_contact": {"name": "周陪护", "phone": "0839-5550404"}}))
        self.apply(command("decide_disposition", "k-d1", SPECIALIST, "2026-09-25T10:00:00+08:00",
                           {"case_id": "C1", "decision": "admit", "department": "心内科"}))
        self.apply(command("issue_review_checklist", "k-c1", SPECIALIST, "2026-09-25T10:05:00+08:00",
                           {"case_id": "C1",
                            "items": [{"item_id": "ecg-1", "title": "心电图复查", "due_at": "2026-09-26T09:00:00+08:00"}],
                            "followup_due_at": "2026-09-26T12:00:00+08:00"}))
        self.apply(command("complete_followup", "k-f1", CONCIERGE, "2026-09-26T10:00:00+08:00",
                           {"case_id": "C1", "followed_at": "2026-09-26T10:00:00+08:00",
                            "outcome": "恢复良好", "next_action": "门诊复诊",
                            "completed_review_items": ["ecg-1"]}))


class ViewTests(ViewAuditBase):
    def test_patient_view_shows_next_step_and_contact_only(self) -> None:
        self.build_critical_case()
        view = patient_view(self.service, "C1")
        self.assertEqual("周陪护", view["responsible_contact"]["name"])
        self.assertTrue(view["next_step"])
        blob = json.dumps(view, ensure_ascii=False)
        # 患者视图不暴露临床细节与他人信息
        self.assertNotIn("vitals_summary", blob)
        self.assertNotIn("STEMI", blob)
        self.assertNotIn("李建国", blob)
        self.assertNotIn("chief_complaint", blob)

    def test_patient_view_unknown_case(self) -> None:
        self.assertIsNone(patient_view(self.service, "NOPE"))

    def test_primary_view_hides_resource_internals(self) -> None:
        self.build_critical_case()
        view = primary_view(self.service, "C1")
        self.assertEqual("闭环完成", view["status"])
        types = [row["event_type"] for row in view["timeline"]]
        self.assertNotIn("RESOURCE_BOOKED", types)
        self.assertNotIn("RESOURCE_RELEASED", types)
        self.assertIn("URGENCY_ESCALATED", types)
        # 连续记录包含下转/复查段落（本案例为住院，无下转）
        self.assertIsNone(view["downward"])
        self.assertTrue(view["review"]["rehab_continued"])

    def test_hospital_view_has_full_record(self) -> None:
        self.build_critical_case()
        view = hospital_view(self.service, "C1")
        types = [row["event_type"] for row in view["timeline"]]
        self.assertIn("RESOURCE_BOOKED", types)
        self.assertEqual(1, len(view["bookings"]))
        self.assertEqual("active", view["bookings"][0]["status"])

    def test_hospital_view_shows_victim_preemption(self) -> None:
        self.build_critical_case()
        view = hospital_view(self.service, "C2")
        victim = view["bookings"][0]
        self.assertEqual("released", victim["status"])
        self.assertEqual("preempted", victim["release_reason"])


class AuditTests(ViewAuditBase):
    def test_audit_reconstructs_escalation_and_preemption(self) -> None:
        self.build_critical_case()
        report = build_audit(self.service, "C1")
        esc = report["escalation"]["records"][0]
        self.assertEqual("现场状态差于电话描述", esc["reason"])
        self.assertEqual("concierge", esc["raised_by"]["role"])
        self.assertTrue(esc["justified_in_time"])
        self.assertEqual("心电图 STEMI，符合危急升级", esc["justification"])
        # 资源协调：C1 抢占了 C2 的窗口
        self.assertEqual([["bk-C2-001"]], [r["preempted"] for r in report["resources"]["preempted_by_this_case"]])
        # 康复接续判定
        self.assertTrue(report["rehab"]["continued"])
        self.assertIn("已接续", report["rehab"]["verdict"])
        # 时间线按版本连续
        versions = [row["version"] for row in report["timeline"]]
        self.assertEqual(list(range(1, len(versions) + 1)), versions)

    def test_audit_marks_overdue_escalation(self) -> None:
        self.apply(command("receive_referral", "k-r1", PRIMARY, "2026-09-25T08:00:00+08:00",
                           {"referral_no": "C9",
                            "patient": {"patient_ref": "pat-9", "name": "赵六", "id_ref": "ID-9"},
                            "referring_facility": "张谷镇卫生院", "primary_doctor_ref": "dr-li",
                            "chief_complaint": "头晕", "contact": {"name": "家属", "phone": "9"}}))
        self.apply(command("confirm_arrival", "k-a9", CONCIERGE, "2026-09-25T08:20:00+08:00",
                           {"case_id": "C9", "observed_at": "2026-09-25T08:20:00+08:00", "assessor_ref": "n"}))
        self.apply(command("record_observation", "k-o9", CONCIERGE, "2026-09-25T08:25:00+08:00",
                           {"case_id": "C9", "observed_at": "2026-09-25T08:25:00+08:00",
                            "assessor_ref": "n", "acuity": "urgent", "vitals_summary": "一般"}))
        self.apply(command("escalate", "k-e9", CONCIERGE, "2026-09-25T08:30:00+08:00",
                           {"case_id": "C9", "level": "urgent", "reason": "疑似卒中",
                            "review_due_at": "2026-09-25T09:00:00+08:00"}))
        self.clock.set(ManualClock.at("2026-09-25T09:30:00+08:00").now())
        self.service.tick()
        report = build_audit(self.service, "C9")
        esc = report["escalation"]["records"][0]
        self.assertFalse(esc["justified"])
        self.assertTrue(esc["overdue_without_justification"])

    def test_audit_unknown_case(self) -> None:
        self.assertIsNone(build_audit(self.service, "NOPE"))


if __name__ == "__main__":
    unittest.main()
