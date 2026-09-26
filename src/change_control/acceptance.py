"""贯通设施快照、变更申请、整体审批、现场回执和修订链的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import Conflict
from .service import ChangeService


def _change_payload(request_id: str, *, amount: str, idempotency_key: str, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "request_id": request_id,
        "title": "T07 机位基础位置调整",
        "snapshot_id": "site-north",
        "equipment": [{"equipment_id": "WTG-T07", "model": "turbine-18mw", "rated_mw": "18"}],
        "coordinates": [{"equipment_id": "WTG-T07", "latitude": "21.386", "longitude": "112.914"}],
        "commissioning_curve": [
            {"date": "2026-11-01", "cumulative_mw": "9"},
            {"date": "2026-12-01", "cumulative_mw": "18"},
        ],
        "guarantee_level": "critical",
        "access_requirements": [
            {"constraint_id": "cl-array-3", "amount": amount},
            {"constraint_id": "ss-main", "amount": amount},
            {"constraint_id": "rescue-north", "amount": "1"},
        ],
        "construction_windows": [
            {"window_id": f"{request_id}-win-1", "vessel_id": "vessel-01",
             "starts_at": "2026-10-05T00:00:00Z", "ends_at": "2026-10-09T00:00:00Z"},
        ],
        "execution_steps": [
            {"seq": 1, "description": "基础沉桩与就位"},
            {"seq": 2, "description": "集电海缆改接"},
            {"seq": 3, "description": "并网调试与应急演练"},
        ],
        "rollback_plan": [
            {"seq": 1, "description": "恢复原有海缆接线"},
            {"seq": 2, "description": "撤离施工船并释放窗口"},
        ],
        "idempotency_key": idempotency_key,
    }
    payload.update(overrides)
    return payload


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = ChangeService(connection, FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (
        ("eng-li", "engineer"),
        ("appr-wang", "approver"),
        ("ops-zhao", "operator"),
        ("plan-chen", "planner"),
        ("audit-lu", "auditor"),
    ):
        service.create_user(user_id, user_id, role)
    service.create_snapshot("plan-chen", {
        "snapshot_id": "site-north",
        "constraints": [
            {"constraint_id": "cl-array-3", "kind": "collector_line", "capacity": "60"},
            {"constraint_id": "ss-main", "kind": "substation_access", "capacity": "120"},
            {"constraint_id": "rescue-north", "kind": "rescue_coverage", "capacity": "2"},
        ],
    })
    submitted = service.submit_request("eng-li", _change_payload("CR-001", amount="18", idempotency_key="key-cr-001"))
    approved = service.approve_request("appr-wang", "CR-001", 1, "施工时段、资源预留与撤回方案整体可行")
    oversized = service.submit_request("eng-li", _change_payload(
        "CR-002", amount="50", idempotency_key="key-cr-002",
        title="T08 机位基础位置调整",
        equipment=[{"equipment_id": "WTG-T08", "model": "turbine-18mw", "rated_mw": "18"}],
        coordinates=[{"equipment_id": "WTG-T08", "latitude": "21.391", "longitude": "112.926"}],
        commissioning_curve=[{"date": "2026-12-01", "cumulative_mw": "18"}],
        construction_windows=[{"window_id": "CR-002-win-1", "vessel_id": "vessel-02",
                               "starts_at": "2026-10-05T00:00:00Z", "ends_at": "2026-10-09T00:00:00Z"}],
    ))
    try:
        service.approve_request("appr-wang", "CR-002", 1, "尝试批准超余量申请")
        oversized_blocked = False
    except Conflict:
        oversized_blocked = True
    service.report_receipt("ops-zhao", "CR-001", 1, "succeeded", "沉桩完成", "rcpt-001")
    service.report_receipt("ops-zhao", "CR-001", 2, "failed", "海缆改接失败，旧接头受损", "rcpt-002")
    rolled_back = service.execute_rollback("ops-zhao", "CR-001", "按撤回方案恢复原接线并撤离施工船")
    revision = service.revise_request("eng-li", "CR-001", _change_payload(
        "CR-003", amount="12", idempotency_key="key-cr-003",
        title="T07 机位基础位置调整（修订：缩小移位）",
        commissioning_curve=[
            {"date": "2026-11-01", "cumulative_mw": "6"},
            {"date": "2026-12-01", "cumulative_mw": "18"},
        ],
        construction_windows=[{"window_id": "CR-003-win-1", "vessel_id": "vessel-01",
                               "starts_at": "2026-10-12T00:00:00Z", "ends_at": "2026-10-15T00:00:00Z"}],
    ))
    service.approve_request("appr-wang", "CR-003", 1, "修订后余量充足，整体批准")
    service.report_receipt("ops-zhao", "CR-003", 1, "succeeded", "沉桩完成", "rcpt-003")
    service.report_receipt("ops-zhao", "CR-003", 2, "succeeded", "海缆改接完成", "rcpt-004")
    completed = service.report_receipt("ops-zhao", "CR-003", 3, "succeeded", "并网调试与演练完成", "rcpt-005")
    result = {
        "status": "ok",
        "submitted_outcome": submitted["evaluation"]["outcome"],
        "approved_revision": approved["revision"],
        "oversized_outcome": oversized["evaluation"]["outcome"],
        "oversized_blocked": oversized_blocked,
        "rolled_back": rolled_back["state"],
        "revision_design_revision": revision["design_revision"],
        "completed_state": completed["request_state"],
        "history": service.request_history("audit-lu", "CR-003"),
        "explain": service.explain_request("audit-lu", "CR-003"),
        "audit": service.audit_chain("audit-lu"),
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行工程变更影响审批服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
