"""命令行入口：契约校验与审计还原。

用法：
  python -m referral_concierge.cli <schema.json> <event.json>   校验单个事件
  python -m referral_concierge.cli audit <数据目录> <case_id>    还原一次上转闭环
"""

import json
import sys
from pathlib import Path

from .audit import build_audit, render_audit
from .clock import SystemClock
from .contracts import validate_event
from .service import Service


def _validate(schema_path: str, event_path: str) -> int:
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    issues = validate_event(event, schema)
    if not issues:
        print("valid")
        return 0
    for issue in issues:
        print(f"{issue.field}	{issue.code}	{issue.message}")
    return 1


def _audit(directory: str, case_id: str) -> int:
    service = Service.open(directory, SystemClock())
    try:
        report = build_audit(service, case_id)
    except KeyError:
        print(f"病例不存在: {case_id}", file=sys.stderr)
        return 1
    print(render_audit(report))
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "audit":
        if len(args) != 3:
            print("用法: python -m referral_concierge.cli audit <数据目录> <case_id>", file=sys.stderr)
            return 2
        return _audit(args[1], args[2])
    if len(args) == 2:
        return _validate(args[0], args[1])
    print("用法: python -m referral_concierge.cli <schema.json> <event.json> | audit <数据目录> <case_id>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
