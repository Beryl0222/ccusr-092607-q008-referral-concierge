"""命令行入口。

旧用法（领域契约校验，保持兼容）：
    python -m referral_concierge.cli <schema.json> <event.json>

闭环簿用法：
    python -m referral_concierge.cli init     <board_dir> [--resources r.json] [--policy p.json]
    python -m referral_concierge.cli apply    <board_dir> <command.json> [--now ISO]
    python -m referral_concierge.cli tick     <board_dir> [--now ISO]
    python -m referral_concierge.cli view     <board_dir> <case_id> --as patient|primary|hospital
    python -m referral_concierge.cli audit    <board_dir> <case_id>
    python -m referral_concierge.cli pending  <board_dir>
    python -m referral_concierge.cli conflicts <board_dir>
    python -m referral_concierge.cli ledger   <board_dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .clock import ManualClock, SystemClock
from .model import parse_dt
from .service import ClosedLoopService
from .store import Store

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts" / "domain.schema.json"

_SUBCOMMANDS = {"init", "apply", "tick", "view", "audit", "pending", "conflicts", "ledger"}


def _load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _print_json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _validate_legacy(schema_path: str, event_path: str) -> int:
    from .contracts import validate_event

    schema = _load_json(schema_path)
    event = _load_json(event_path)
    issues = validate_event(event, schema)
    if not issues:
        print("valid")
        return 0
    for issue in issues:
        print(f"{issue.field}\t{issue.code}\t{issue.message}")
    return 1


def _build_service(board_dir: str, now: str | None) -> ClosedLoopService:
    store = Store(board_dir)
    if not store.events_path.exists():
        raise SystemExit(f"闭环簿尚未初始化: {board_dir}（先运行 init）")
    if now:
        clock: ManualClock | SystemClock = ManualClock(parse_dt(now))
    else:
        clock = SystemClock()
    schema = _load_json(_SCHEMA_PATH) if _SCHEMA_PATH.exists() else None
    return ClosedLoopService(store, clock, schema=schema)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] not in _SUBCOMMANDS:
        return _validate_legacy(argv[0], argv[1])

    parser = argparse.ArgumentParser(prog="referral_concierge", description="县域转诊陪护闭环簿")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="初始化闭环簿")
    p_init.add_argument("board_dir")
    p_init.add_argument("--resources", help="资源台账 JSON")
    p_init.add_argument("--policy", help="调度策略 JSON")

    p_apply = sub.add_parser("apply", help="应用一条命令")
    p_apply.add_argument("board_dir")
    p_apply.add_argument("command_json")
    p_apply.add_argument("--now", help="指定当前时间（带时区 ISO），便于演练")

    p_tick = sub.add_parser("tick", help="推进可控时钟并处理到期事项")
    p_tick.add_argument("board_dir")
    p_tick.add_argument("--now", help="指定当前时间（带时区 ISO）")

    p_view = sub.add_parser("view", help="按权限查看连续记录")
    p_view.add_argument("board_dir")
    p_view.add_argument("case_id")
    p_view.add_argument("--as", dest="audience", choices=("patient", "primary", "hospital"), required=True)

    p_audit = sub.add_parser("audit", help="审计还原一次上转")
    p_audit.add_argument("board_dir")
    p_audit.add_argument("case_id")

    p_pending = sub.add_parser("pending", help="列出乱序待办命令")
    p_pending.add_argument("board_dir")

    p_conflicts = sub.add_parser("conflicts", help="列出内容冲突记录")
    p_conflicts.add_argument("board_dir")

    p_ledger = sub.add_parser("ledger", help="列出资源预约台账")
    p_ledger.add_argument("board_dir")

    args = parser.parse_args(argv)

    if args.cmd == "init":
        resources = _load_json(args.resources) if args.resources else {"resources": []}
        policy = _load_json(args.policy) if args.policy else None
        Store.initialize(args.board_dir, resources=resources, policy=policy)
        print(f"initialized {args.board_dir}")
        return 0

    if args.cmd == "apply":
        service = _build_service(args.board_dir, getattr(args, "now", None))
        receipt = service.apply(_load_json(args.command_json))
        _print_json(receipt)
        return 0 if receipt["status"] in {"accepted", "deferred"} else 1

    if args.cmd == "tick":
        service = _build_service(args.board_dir, getattr(args, "now", None))
        events = service.tick()
        _print_json({"emitted": len(events), "events": events})
        return 0

    if args.cmd == "view":
        from .views import hospital_view, patient_view, primary_view

        service = _build_service(args.board_dir, getattr(args, "now", None))
        builders = {"patient": patient_view, "primary": primary_view, "hospital": hospital_view}
        view = builders[args.audience](service, args.case_id)
        if view is None:
            print(f"案例不存在: {args.case_id}", file=sys.stderr)
            return 1
        _print_json(view)
        return 0

    if args.cmd == "audit":
        from .audit import build_audit

        service = _build_service(args.board_dir, getattr(args, "now", None))
        report = build_audit(service, args.case_id)
        if report is None:
            print(f"案例不存在: {args.case_id}", file=sys.stderr)
            return 1
        _print_json(report)
        return 0

    if args.cmd == "pending":
        service = _build_service(args.board_dir, getattr(args, "now", None))
        _print_json(service.pending())
        return 0

    if args.cmd == "conflicts":
        service = _build_service(args.board_dir, getattr(args, "now", None))
        _print_json(service.conflicts())
        return 0

    if args.cmd == "ledger":
        service = _build_service(args.board_dir, getattr(args, "now", None))
        _print_json(sorted(service.ledger().values(), key=lambda b: b["booking_ref"]))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
