#!/usr/bin/env python3
"""端到端演练：两个案例走完县域转诊陪护闭环。

场景：
- 案例 A（ZZ-2026-0001）：胸痛危重，现场升级，先占最小必要资源，
  专科医生限期补全依据，按冻结规则抢占普通案例的 CT 窗口，收入住院。
- 案例 B（ZZ-2026-0002）：普通上转，演示幂等重放、内容冲突、乱序待办、
  CT 窗口被抢占、离院下转、复查超期违约与回访核销闭环。

用法：PYTHONPATH=src python3 scripts/run_demo.py [board_dir]
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from referral_concierge.audit import build_audit
from referral_concierge.clock import ManualClock
from referral_concierge.service import ClosedLoopService
from referral_concierge.store import Store
from referral_concierge.views import patient_view, primary_view

SCHEMA = json.loads((ROOT / "contracts/domain.schema.json").read_text(encoding="utf-8"))
RESOURCES = json.loads((ROOT / "data/demo/resources.json").read_text(encoding="utf-8"))
POLICY = json.loads((ROOT / "data/demo/policy.json").read_text(encoding="utf-8"))

PRIMARY = {"role": "primary_doctor", "ref": "dr-li-zhanggu"}
CONCIERGE = {"role": "concierge", "ref": "concierge-wang"}
SPECIALIST = {"role": "specialist", "ref": "dr-chen-county"}


def cmd(name: str, key: str, actor: dict, occurred_at: str, data: dict) -> dict:
    return {"command": name, "business_key": key, "actor": actor, "occurred_at": occurred_at, "data": data}


def show(title: str, payload: object) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    board = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="referral-board-"))
    store = Store.initialize(board, resources=RESOURCES, policy=POLICY)
    clock = ManualClock.at("2026-09-25T08:00:00+08:00")
    service = ClosedLoopService(store, clock, schema=SCHEMA)

    def apply(command: dict) -> dict:
        receipt = service.apply(command)
        print(f"[{command['occurred_at']}] {command['command']:<28} -> {receipt['status']}: {receipt['message']}")
        return receipt

    # ---- 案例 B：普通上转（先到） -------------------------------------
    referral_b = cmd(
        "receive_referral", "rk-B", PRIMARY, "2026-09-25T08:30:00+08:00",
        {
            "referral_no": "ZZ-2026-0002",
            "patient": {"patient_ref": "pat-6203", "name": "李建国", "id_ref": "5107261962033"},
            "referring_facility": "张谷镇中心卫生院",
            "primary_doctor_ref": "dr-li-zhanggu",
            "chief_complaint": "电话中描述：反复腹痛一周，加重一天",
            "contact": {"name": "李建国家属", "phone": "0839-5550202"},
        },
    )
    apply(referral_b)
    apply(cmd("plan_arrival", "pk-B", CONCIERGE, "2026-09-25T08:40:00+08:00",
              {"case_id": "ZZ-2026-0002", "planned_arrival_at": "2026-09-25T10:00:00+08:00",
               "pickup_point": "县医院南门接站点", "greeter_ref": "greeter-zhao"}))
    apply(cmd("book_resources", "bk-B1", CONCIERGE, "2026-09-25T08:45:00+08:00",
              {"case_id": "ZZ-2026-0002", "purpose": "routine",
               "requests": [{"resource_id": "ct", "slot_id": "ct-25-1300"},
                            {"resource_id": "escort", "slot_id": "escort-day"}]}))

    # 幂等：相同业务键内容一致，沿用回执
    replay = apply(referral_b)
    assert replay.get("replayed") is True and replay["status"] == "accepted"

    # 冲突：相同业务键但病情描述不一致，不自动合并
    conflict = apply(cmd("receive_referral", "rk-B", PRIMARY, "2026-09-25T08:31:00+08:00",
                         {**referral_b["data"], "chief_complaint": "电话中描述：腹痛伴呕血"}))
    assert conflict["status"] == "conflict"

    # 乱序：现场观察先于到院确认到达，转入待办
    deferred = apply(cmd("record_observation", "ob-B", CONCIERGE, "2026-09-25T10:05:00+08:00",
                         {"case_id": "ZZ-2026-0002", "observed_at": "2026-09-25T10:05:00+08:00",
                          "assessor_ref": "nurse-qian", "acuity": "routine",
                          "vitals_summary": "BP 128/80，HR 76，腹软"}))
    assert deferred["status"] == "deferred"

    # ---- 案例 A：胸痛危重 ---------------------------------------------
    apply(cmd("receive_referral", "rk-A", PRIMARY, "2026-09-25T09:00:00+08:00",
              {"referral_no": "ZZ-2026-0001",
               "patient": {"patient_ref": "pat-9305", "name": "王桂兰", "id_ref": "5107261930511"},
               "referring_facility": "张谷镇中心卫生院",
               "primary_doctor_ref": "dr-li-zhanggu",
               "chief_complaint": "电话中描述：持续胸痛两小时，伴出汗",
               "contact": {"name": "王桂兰家属", "phone": "0839-5550101"}}))
    apply(cmd("plan_arrival", "pk-A", CONCIERGE, "2026-09-25T09:05:00+08:00",
              {"case_id": "ZZ-2026-0001", "planned_arrival_at": "2026-09-25T09:30:00+08:00",
               "pickup_point": "县医院急诊入口", "greeter_ref": "greeter-zhao"}))
    apply(cmd("confirm_arrival", "cf-A", CONCIERGE, "2026-09-25T09:32:00+08:00",
              {"case_id": "ZZ-2026-0001", "observed_at": "2026-09-25T09:32:00+08:00",
               "assessor_ref": "nurse-qian"}))
    apply(cmd("record_observation", "ob-A", CONCIERGE, "2026-09-25T09:35:00+08:00",
              {"case_id": "ZZ-2026-0001", "observed_at": "2026-09-25T09:35:00+08:00",
               "assessor_ref": "nurse-qian", "acuity": "critical",
               "vitals_summary": "BP 90/60，HR 118，大汗，濒死感"}))
    # 危急升级：先占用最小必要资源（绿色抢救床位），限期补全依据
    apply(cmd("escalate", "es-A", CONCIERGE, "2026-09-25T09:36:00+08:00",
              {"case_id": "ZZ-2026-0001", "level": "critical",
               "reason": "现场状态明显差于电话描述，疑似急性心梗",
               "review_due_at": "2026-09-25T10:06:00+08:00",
               "minimal_hold": {"requests": [{"resource_id": "green-bed", "slot_id": "green-day"}]}}))
    apply(cmd("justify_escalation", "js-A", SPECIALIST, "2026-09-25T09:50:00+08:00",
              {"case_id": "ZZ-2026-0001",
               "justification": "心电图 ST 段抬高，肌钙蛋白升高，确诊 STEMI，符合危急升级"}))
    # 危急案例按冻结规则抢占普通案例未冻结的 CT 窗口
    apply(cmd("book_resources", "bk-A1", CONCIERGE, "2026-09-25T09:55:00+08:00",
              {"case_id": "ZZ-2026-0001", "purpose": "emergency_exam",
               "requests": [{"resource_id": "ct", "slot_id": "ct-25-1300"},
                            {"resource_id": "escort", "slot_id": "escort-day"}]}))

    # 模拟重启：从事件日志重建，待办与台账继续有效
    service = ClosedLoopService(store, clock, schema=SCHEMA)
    print("\n--- 模拟重启：状态已从事件日志重建 ---")

    # 案例 B 到院，乱序待办自动补放
    apply(cmd("confirm_arrival", "cf-B", CONCIERGE, "2026-09-25T10:02:00+08:00",
              {"case_id": "ZZ-2026-0002", "observed_at": "2026-09-25T10:02:00+08:00",
               "assessor_ref": "nurse-qian"}))
    apply(cmd("assign_escort", "as-B", CONCIERGE, "2026-09-25T10:10:00+08:00",
              {"case_id": "ZZ-2026-0002", "escort_ref": "escort-sun",
               "responsible_contact": {"name": "孙陪护", "phone": "0839-5550303"}}))

    # 案例 A 处置：收入住院
    apply(cmd("assign_escort", "as-A", CONCIERGE, "2026-09-25T10:20:00+08:00",
              {"case_id": "ZZ-2026-0001", "escort_ref": "escort-zhou",
               "responsible_contact": {"name": "周陪护", "phone": "0839-5550404"}}))
    apply(cmd("decide_disposition", "dp-A", SPECIALIST, "2026-09-25T11:30:00+08:00",
              {"case_id": "ZZ-2026-0001", "decision": "admit", "department": "心内科"}))
    apply(cmd("issue_review_checklist", "rc-A", SPECIALIST, "2026-09-25T11:35:00+08:00",
              {"case_id": "ZZ-2026-0001",
               "items": [{"item_id": "ecg-1", "title": "术后24小时心电图复查", "due_at": "2026-09-26T09:00:00+08:00"}],
               "followup_due_at": "2026-09-26T11:00:00+08:00"}))

    # 案例 B 离院下转
    apply(cmd("decide_disposition", "dp-B", SPECIALIST, "2026-09-25T13:30:00+08:00",
              {"case_id": "ZZ-2026-0002", "decision": "discharge", "department": "消化内科"}))
    apply(cmd("plan_downward", "dw-B", SPECIALIST, "2026-09-25T13:35:00+08:00",
              {"case_id": "ZZ-2026-0002", "target_facility": "张谷镇中心卫生院",
               "plan_summary": "带回口服药，三日后镇卫生院复查",
               "handover_deadline": "2026-09-25T18:00:00+08:00"}))
    apply(cmd("issue_review_checklist", "rc-B", SPECIALIST, "2026-09-25T13:36:00+08:00",
              {"case_id": "ZZ-2026-0002",
               "items": [{"item_id": "us-1", "title": "腹部超声复查", "due_at": "2026-09-26T10:00:00+08:00"}],
               "followup_due_at": "2026-09-26T12:00:00+08:00"}))
    apply(cmd("confirm_downward_handover", "ho-B", CONCIERGE, "2026-09-25T16:30:00+08:00",
              {"case_id": "ZZ-2026-0002", "confirmed_at": "2026-09-25T16:30:00+08:00",
               "receiving_facility": "张谷镇中心卫生院"}))

    # 可控时钟：复查与回访期限违约
    clock.set(ManualClock.at("2026-09-26T10:30:00+08:00").now())
    emitted = service.tick()
    print(f"\n[2026-09-26T10:30+08:00] tick -> 发现 {len(emitted)} 条期限事件")
    for event in emitted:
        print(f"  {event['aggregate_id']} {event['event_type']} {event['payload'].get('kind')} ref={event['payload'].get('ref')}")

    apply(cmd("complete_followup", "fu-A", CONCIERGE, "2026-09-26T10:40:00+08:00",
              {"case_id": "ZZ-2026-0001", "followed_at": "2026-09-26T10:40:00+08:00",
               "outcome": "术后恢复良好，胸痛未再发", "next_action": "一周后心内科门诊复诊",
               "completed_review_items": ["ecg-1"]}))
    apply(cmd("complete_followup", "fu-B", CONCIERGE, "2026-09-26T12:40:00+08:00",
              {"case_id": "ZZ-2026-0002", "followed_at": "2026-09-26T12:40:00+08:00",
               "outcome": "腹痛缓解，已在镇卫生院完成超声复查", "next_action": "无需进一步处理",
               "completed_review_items": ["us-1"]}))

    show("患者视图（案例 B）", patient_view(service, "ZZ-2026-0002"))
    show("基层视图（案例 B，节选）", {k: v for k, v in primary_view(service, "ZZ-2026-0002").items() if k != "timeline"})
    show("审计还原（案例 A：为何升级、资源如何协调）", build_audit(service, "ZZ-2026-0001"))
    print(f"\n闭环簿目录: {board}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
