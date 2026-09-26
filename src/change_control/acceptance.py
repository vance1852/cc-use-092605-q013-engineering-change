"""贯通资源台账、快照核算、整体审批、占定和现场回执的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import ChangeService


def _change_payload(change_id: str, key: str, *, collector_mw: str, supersedes: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "change_id": change_id,
        "title": "WTG-07 基础位置调整",
        "assurance_level": "A",
        "equipment": [{"equipment_id": "WTG-07", "model": "turbine-18mw", "version": "rev-3", "quantity": 1}],
        "coordinates": [{"point_id": "WTG-07", "latitude": "21.345", "longitude": "112.782"}],
        "commissioning_curve": [
            {"date": "2026-11-01", "cumulative_mw": "18"},
            {"date": "2026-12-01", "cumulative_mw": "36"},
        ],
        "demands": [
            {"pool_id": "collector-line-a", "amount": collector_mw},
            {"pool_id": "substation-east", "amount": "36"},
            {"pool_id": "vessel-alpha", "amount": "10"},
            {"pool_id": "rescue-north", "amount": "1"},
        ],
        "construction_windows": [
            {"window_id": "w1", "starts_at": "2026-10-10T00:00:00Z", "ends_at": "2026-10-20T00:00:00Z"},
        ],
        "rollback_plan": {
            "summary": "基础位置回退至原坐标并恢复集电线路接线",
            "steps": ["拆除新基础临时固定", "恢复原电缆敷设", "复核保护定值"],
        },
        "execution_steps": [
            {"step_id": "s1", "description": "基础沉桩就位"},
            {"step_id": "s2", "description": "集电线路改接"},
        ],
        "idempotency_key": key,
    }
    if supersedes is not None:
        payload["supersedes_change_id"] = supersedes
    return payload


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = ChangeService(connection, FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)))
    for user_id, role in (
        ("plan", "planner"),
        ("eng-1", "engineer"),
        ("eng-2", "engineer"),
        ("field", "field"),
        ("appr", "approver"),
        ("ctrl", "controller"),
        ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)
    service.register_pool("plan", {"pool_id": "collector-line-a", "name": "集电线路 A 回", "kind": "collector_line", "unit": "MW", "capacity": "120"})
    service.register_pool("plan", {"pool_id": "substation-east", "name": "东升压站接入", "kind": "substation", "unit": "MW", "capacity": "200"})
    service.register_pool("plan", {"pool_id": "vessel-alpha", "name": "阿尔法号施工船窗口", "kind": "vessel_window", "unit": "船日", "capacity": "30"})
    service.register_pool("plan", {"pool_id": "rescue-north", "name": "北部应急救援覆盖", "kind": "rescue_coverage", "unit": "单元", "capacity": "4"})

    first = service.submit_change("eng-1", _change_payload("chg-001", "key-chg-001", collector_mw="36"))
    service.approve_change("appr", "chg-001", 1, "整体同意施工时段、资源预留与撤回方案")

    contending = _change_payload("chg-002", "key-chg-002", collector_mw="90")
    contending["title"] = "WTG-11 基础位置调整"
    second = service.submit_change("eng-2", contending)
    service.withdraw_change("eng-2", "chg-002", 1)

    service.record_receipt("field", "chg-001", {"step_id": "s1", "outcome": "succeeded", "note": "沉桩到位", "receipt_ref": "rcpt-001-s1"})
    blocked = service.record_receipt("field", "chg-001", {"step_id": "s2", "outcome": "failed", "note": "改接时电缆终端击穿", "receipt_ref": "rcpt-001-s2"})
    rolled_back = service.rollback_change("ctrl", "chg-001", 4, "按撤回方案恢复原接线")

    revision = service.submit_change("eng-1", _change_payload("chg-003", "key-chg-003", collector_mw="30", supersedes="chg-001"))
    service.approve_change("appr", "chg-003", 1, "修订后整体同意")
    service.record_receipt("field", "chg-003", {"step_id": "s1", "outcome": "succeeded", "note": "沉桩到位", "receipt_ref": "rcpt-003-s1"})
    done = service.record_receipt("field", "chg-003", {"step_id": "s2", "outcome": "succeeded", "note": "改接完成", "receipt_ref": "rcpt-003-s2"})

    result = {
        "status": "ok",
        "first_assessment_fits": first["assessment"]["fits"],
        "second_assessment": second["assessment"],
        "blocked_state": blocked["state"],
        "rolled_back": rolled_back,
        "revision_snapshot_id": revision["snapshot_id"],
        "completed_state": done["state"],
        "ledger": service.ledger_status("appr"),
        "explain": service.explain_change("audit", "chg-003"),
        "audit": service.audit_chain("audit"),
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行工程变更影响审批离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
