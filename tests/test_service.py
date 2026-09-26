import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from referral_concierge.audit import build_audit, render_audit
from referral_concierge.cli import main as cli_main
from referral_concierge.clock import ManualClock
from referral_concierge.resources import FrozenError, Slot
from referral_concierge.service import Service
from referral_concierge.views import patient_view, record_view

T0 = datetime(2026, 9, 25, 8, 0, 0, tzinfo=__import__("datetime").timezone(timedelta(hours=8)))
TZ = T0.tzinfo

PATIENT = {"name": "王大山", "id_no": "513701196204180012"}
REFERRAL = {"from_facility": "青坪乡卫生院", "condition_summary": "反复胸痛三天，含服硝酸甘油可缓解"}


def make_service(directory, clock, with_slots=True):
    schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
    service = Service.open(directory, clock, schema=schema, freeze_lead=timedelta(minutes=30))
    if with_slots:
        service.add_slot(Slot("ct-0900", "exam_window", T0 + timedelta(hours=3), T0 + timedelta(hours=4)))
        service.add_slot(Slot("ct-1000", "exam_window", T0 + timedelta(hours=4), T0 + timedelta(hours=5)))
        service.add_slot(Slot("escort-a", "escort", T0 + timedelta(hours=2), T0 + timedelta(hours=8)))
        service.add_slot(Slot("green-1", "exam_window", T0 + timedelta(minutes=20), T0 + timedelta(hours=1)))
    return service


def receive(service, key="k-referral", case_id="case-001"):
    return service.receive_referral(key, case_id, "primary_doctor", "dr-li-xiang", PATIENT, REFERRAL, "concierge-chen")


class ClosedLoopTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.clock = ManualClock(T0)
        self.service = make_service(self.dir.name, self.clock)

    def tearDown(self):
        self.dir.cleanup()

    def run_full_loop(self):
        s = self.service
        self.assertEqual("applied", receive(s).status)
        self.assertEqual("applied", s.record_precontact(
            "k-contact", "case-001", "concierge", "concierge-chen",
            {"channel": "phone", "summary": "已电话联系患者，明早动身"},
            {"arrive_by": (T0 + timedelta(hours=2)).isoformat(), "pickup_point": "县医院东门"},
        ).status)
        self.assertEqual("applied", s.confirm_arrival(
            "k-arrival", "case-001", "concierge", "concierge-chen",
            T0 + timedelta(hours=1, minutes=50), {"condition_summary": "神志清楚，胸痛缓解中"},
        ).status)
        self.assertEqual("applied", s.book_resources(
            "k-book", "case-001", "concierge", "concierge-chen", ["ct-0900", "escort-a"],
        ).status)
        self.assertEqual("applied", s.record_disposition(
            "k-dispo", "case-001", "specialist", "dr-zhou", "discharge", {"summary": "稳定型心绞痛，药物调整"},
        ).status)
        self.assertEqual("applied", s.plan_return(
            "k-return", "case-001", "specialist", "dr-zhou",
            {"to_facility": "青坪乡卫生院", "transfer_by": (T0 + timedelta(days=2)).isoformat(),
             "guidance": "低盐饮食，按时服药"},
            [{"item_id": "rc-1", "title": "心电图复查", "due_at": (T0 + timedelta(days=7)).isoformat()}],
        ).status)
        self.assertEqual("applied", s.complete_recheck(
            "k-recheck", "case-001", "primary_doctor", "dr-li-xiang", "rc-1", "心电图大致正常",
        ).status)
        self.assertEqual("applied", s.complete_followup(
            "k-followup", "case-001", "concierge", "concierge-chen", "恢复良好", "一月后电话随访",
        ).status)

    def test_full_closed_loop(self):
        self.run_full_loop()
        case = self.service.get_case("case-001")
        self.assertEqual("followup_done", case.status)
        self.assertEqual({"source", "arrival", "clinical"}, set(case.confirmations))
        report = build_audit(self.service, "case-001")
        self.assertTrue(report["rehab_continued"])
        text = render_audit(report)
        self.assertIn("康复指导已真正接续", text)
        self.assertIn("青坪乡卫生院", text)
        view = patient_view(self.service, "case-001")
        self.assertEqual("回访已完成，按康复指导继续休养", view["next_step"])
        self.assertEqual("concierge-chen", view["contact"]["ref"])

    def test_emitted_events_pass_contract(self):
        self.run_full_loop()
        schema = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
        from referral_concierge.contracts import validate_event
        events = [e["event"] for e in self.service.trail("case-001") if e["kind"] == "event"]
        self.assertEqual(
            ["REFERRAL_RECEIVED", "ARRIVAL_CONFIRMED", "RESOURCE_BOOKED", "FOLLOWUP_COMPLETED"],
            [e["event_type"] for e in events],
        )
        for event in events:
            self.assertEqual([], validate_event(event, schema))

    def test_role_separation(self):
        receive(self.service)
        with self.assertRaises(PermissionError):
            self.service.record_disposition("k-x1", "case-001", "concierge", "concierge-chen", "admit", {})
        with self.assertRaises(PermissionError):
            self.service.confirm_arrival("k-x2", "case-001", "specialist", "dr-zhou",
                                         T0, {"condition_summary": "x"})
        with self.assertRaises(PermissionError):
            self.service.complete_recheck("k-x3", "case-001", "concierge", "concierge-chen", "rc-1", "y")
        with self.assertRaises(PermissionError):
            self.service.receive_referral("k-x4", "case-002", "concierge", "c", PATIENT, REFERRAL, "c2")
        # 越权尝试没有留下任何结论
        case = self.service.get_case("case-001")
        self.assertEqual({"source"}, set(case.confirmations))

    def test_idempotent_replay_reuses_receipt(self):
        first = receive(self.service)
        second = receive(self.service)
        self.assertEqual(first, second)
        notes = [e for e in self.service.trail("case-001") if e["kind"] == "note"]
        self.assertEqual(1, len(notes))

    def test_out_of_order_replay(self):
        s = self.service
        receive(s)
        # 到院确认先于前置联系到达（离线补录乱序）
        self.assertEqual("applied", s.confirm_arrival(
            "k-arrival", "case-001", "concierge", "concierge-chen",
            T0 + timedelta(hours=2), {"condition_summary": "到院时状态平稳"},
        ).status)
        self.assertEqual("applied", s.record_precontact(
            "k-contact", "case-001", "concierge", "concierge-chen",
            {"channel": "phone", "summary": "补录前置联系"},
            {"arrive_by": (T0 + timedelta(hours=2)).isoformat()},
        ).status)
        case = s.get_case("case-001")
        self.assertEqual("arrived", case.status)
        self.assertIsNotNone(case.precontact)

    def test_same_key_different_content_conflicts(self):
        receive(self.service)
        changed = self.service.receive_referral(
            "k-referral", "case-001", "primary_doctor", "dr-li-xiang",
            dict(PATIENT), dict(REFERRAL, condition_summary="被篡改的病情"), "concierge-chen",
        )
        self.assertEqual("conflict", changed.status)
        self.assertEqual("反复胸痛三天，含服硝酸甘油可缓解",
                         self.service.get_case("case-001").referral["condition_summary"])
        self.assertEqual(1, len(self.service.conflicts("case-001")))

    def test_identity_and_condition_conflicts_not_merged(self):
        receive(self.service)
        other_patient = self.service.receive_referral(
            "k-other", "case-001", "primary_doctor", "dr-li-xiang",
            dict(PATIENT, id_no="513701196204180099"), REFERRAL, "concierge-chen",
        )
        self.assertEqual("conflict", other_patient.status)
        self.assertEqual("513701196204180012", self.service.get_case("case-001").patient["id_no"])
        self.assertEqual("applied", self.service.confirm_arrival(
            "k-arr", "case-001", "concierge", "concierge-chen",
            T0 + timedelta(hours=2), {"condition_summary": "到院观察"},
        ).status)
        different_time = self.service.confirm_arrival(
            "k-arr-2", "case-001", "concierge", "concierge-chen",
            T0 + timedelta(hours=5), {"condition_summary": "到院观察"},
        )
        self.assertEqual("conflict", different_time.status)
        observed = self.service.get_case("case-001").observation["observed_at"]
        self.assertEqual((T0 + timedelta(hours=2)).isoformat(), observed)

    def test_escalation_occupies_minimal_resources_and_basis_deadline(self):
        s = self.service
        receive(s)
        receipt = s.escalate("k-esc", "case-001", "concierge", "concierge-chen",
                             "到院途中突发意识丧失", min_slots=["green-1"])
        self.assertEqual("applied", receipt.status)
        self.assertEqual(0, s.pool().remaining("green-1"))
        basis = s.complete_escalation_basis("k-basis", "case-001", "specialist", "dr-zhou",
                                            "床旁心电图提示室速，已复律")
        self.assertEqual("applied", basis.status)
        self.assertIn("期限内", basis.detail)
        report = build_audit(s, "case-001")
        self.assertTrue(report["escalation"]["basis_in_time"])
        self.assertEqual([], s.due_todos("case-001"))

    def test_escalation_basis_overdue_flags_in_audit(self):
        s = self.service
        receive(s)
        s.escalate("k-esc", "case-001", "concierge", "concierge-chen", "呼吸衰竭",
                   review_within=timedelta(minutes=30))
        self.clock.advance(timedelta(minutes=45))
        due = s.due_todos("case-001")
        self.assertEqual(["escalation_review"], [t.kind for t in due])
        report = build_audit(s, "case-001")
        self.assertTrue(report["escalation"]["basis_overdue"])
        late = s.complete_escalation_basis("k-basis", "case-001", "specialist", "dr-zhou", "血气分析确认")
        self.assertIn("超过复核期限", late.detail)

    def test_atomic_booking_contention(self):
        s = self.service
        receive(s)
        receive(s, key="k-referral-2", case_id="case-002")
        ok = s.book_resources("k-b1", "case-001", "concierge", "concierge-chen", ["ct-0900", "escort-a"])
        self.assertEqual("applied", ok.status)
        failed = s.book_resources("k-b2", "case-002", "concierge", "concierge-chen", ["ct-0900", "ct-1000"])
        self.assertEqual("rejected", failed.status)
        # 原子性：ct-1000 没有被部分占用
        self.assertEqual(1, s.pool().remaining("ct-1000"))
        self.assertEqual([], s.pool().bookings_for("case-002"))
        report = build_audit(s, "case-002")
        self.assertEqual(1, len(report["resource_coordination"]["failures"]))

    def test_freeze_rules_and_no_show_release(self):
        s = self.service
        receive(s)
        s.book_resources("k-b1", "case-001", "concierge", "concierge-chen", ["ct-0900"])
        # 未冻结前：患者未到，释放成功
        receipt = s.release_unused("k-rel", "case-001", "concierge", "concierge-chen", "no_show")
        self.assertEqual("applied", receipt.status)
        self.assertEqual(1, s.pool().remaining("ct-0900"))
        self.assertEqual("cancelled", s.get_case("case-001").status)

        receive(s, key="k-referral-3", case_id="case-003")
        s.book_resources("k-b3", "case-003", "concierge", "concierge-chen", ["ct-1000"])
        self.clock.set(T0 + timedelta(hours=3, minutes=45))  # ct-1000 开场前 15 分钟，已冻结
        kept = s.release_unused("k-rel-3", "case-003", "concierge", "concierge-chen", "no_show")
        self.assertIn("保留 1 项", kept.detail)
        self.assertEqual(0, s.pool().remaining("ct-1000"))
        with self.assertRaises(FrozenError):
            s.pool().release("bk-k-b3")

    def test_restart_resumes_state_and_todos(self):
        s = self.service
        receive(s)
        s.record_precontact("k-contact", "case-001", "concierge", "concierge-chen",
                            {"channel": "phone", "summary": "已联系"},
                            {"arrive_by": (T0 + timedelta(hours=2)).isoformat()})
        s.escalate("k-esc", "case-001", "concierge", "concierge-chen", "呕血",
                   review_within=timedelta(hours=1))
        # 模拟重启：同一目录重新打开
        reopened = make_service(self.dir.name, self.clock, with_slots=False)
        case = reopened.get_case("case-001")
        self.assertEqual("contacted", case.status)
        self.assertTrue(case.escalated)
        # 重启前已处理的业务键仍然幂等
        again = reopened.receive_referral("k-referral", "case-001", "primary_doctor", "dr-li-xiang",
                                          PATIENT, REFERRAL, "concierge-chen")
        self.assertEqual("applied", again.status)
        self.assertEqual("转诊单已登记", again.detail)
        # 时钟推进后，接站与复核期限继续到期
        self.clock.advance(timedelta(hours=3))
        kinds = sorted(t.kind for t in reopened.due_todos("case-001"))
        self.assertEqual(["escalation_review", "pickup"], kinds)

    def test_record_view_permissions(self):
        self.run_full_loop()
        primary = record_view(self.service, "case-001", "primary_clinic")
        self.assertIn("checklist", primary)
        self.assertNotIn("observation", primary)
        county = record_view(self.service, "case-001", "county_hospital")
        self.assertIn("observation", county)
        auditor = record_view(self.service, "case-001", "auditor")
        self.assertIn("conflicts", auditor)
        with self.assertRaises(ValueError):
            record_view(self.service, "case-001", "stranger")

    def test_cli_audit_command(self):
        self.run_full_loop()
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["cli", "audit", self.dir.name, "case-001"]):
            with redirect_stdout(buf):
                code = cli_main()
        self.assertEqual(0, code)
        self.assertIn("case-001", buf.getvalue())
        self.assertIn("康复指导已真正接续", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
