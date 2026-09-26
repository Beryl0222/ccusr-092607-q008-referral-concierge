"""命令行入口测试：旧校验用法与闭环簿子命令。"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from referral_concierge.cli import main

DEMO_RESOURCES = ROOT / "data/demo/resources.json"


def run_cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = main(list(argv))
    return code, out.getvalue()


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.board = str(Path(self.tmp.name) / "board")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def write(self, name: str, payload: dict) -> str:
        path = Path(self.tmp.name) / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_legacy_validate_still_works(self) -> None:
        code, out = run_cli(str(ROOT / "contracts/domain.schema.json"), str(ROOT / "data/sample.json"))
        self.assertEqual(0, code)
        self.assertIn("valid", out)

    def test_legacy_validate_reports_issues(self) -> None:
        bad = self.write("bad.json", {"event_type": "UNKNOWN"})
        code, out = run_cli(str(ROOT / "contracts/domain.schema.json"), bad)
        self.assertEqual(1, code)
        self.assertIn("required", out)

    def test_board_flow(self) -> None:
        code, _ = run_cli("init", self.board, "--resources", str(DEMO_RESOURCES))
        self.assertEqual(0, code)
        command = self.write("cmd.json", {
            "command": "receive_referral", "business_key": "rk-1",
            "actor": {"role": "primary_doctor", "ref": "dr-li"},
            "occurred_at": "2026-09-25T08:00:00+08:00",
            "data": {"referral_no": "ZZ-1",
                     "patient": {"patient_ref": "p1", "name": "王五", "id_ref": "ID1"},
                     "referring_facility": "镇卫生院", "primary_doctor_ref": "dr-li",
                     "chief_complaint": "胸痛", "contact": {"name": "家属", "phone": "1"}},
        })
        code, out = run_cli("apply", self.board, command, "--now", "2026-09-25T08:00:00+08:00")
        self.assertEqual(0, code)
        self.assertEqual("accepted", json.loads(out)["status"])
        # 幂等重放
        code, out = run_cli("apply", self.board, command, "--now", "2026-09-25T08:01:00+08:00")
        self.assertTrue(json.loads(out)["replayed"])
        # 视图与审计
        code, out = run_cli("view", self.board, "ZZ-1", "--as", "patient")
        self.assertEqual(0, code)
        self.assertIn("next_step", json.loads(out))
        code, out = run_cli("audit", self.board, "ZZ-1")
        self.assertEqual(0, code)
        self.assertEqual("ZZ-1", json.loads(out)["case_id"])
        # 其余子命令
        for sub in ("tick", "pending", "conflicts", "ledger"):
            code, _ = run_cli(sub, self.board, "--now", "2026-09-25T09:00:00+08:00") if sub == "tick" else run_cli(sub, self.board)
            self.assertEqual(0, code, sub)

    def test_apply_rejected_returns_nonzero(self) -> None:
        run_cli("init", self.board)
        bad = self.write("bad-cmd.json", {"command": "receive_referral", "business_key": "x"})
        code, out = run_cli("apply", self.board, bad)
        self.assertEqual(1, code)
        self.assertEqual("rejected", json.loads(out)["status"])

    def test_view_unknown_case(self) -> None:
        run_cli("init", self.board)
        code, _ = run_cli("view", self.board, "NOPE", "--as", "patient")
        self.assertEqual(1, code)


if __name__ == "__main__":
    unittest.main()
