from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from change_control.api import JsonApplication
from change_control.clock import FrozenClock
from change_control.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from change_control.impact import (
    ConstraintCapacity,
    Demand,
    OwnWindow,
    PendingDemand,
    WindowHold,
    evaluate_impact,
)
from change_control.service import ChangeService


SNAPSHOT = {
    "snapshot_id": "site-north",
    "constraints": [
        {"constraint_id": "cl-array-3", "kind": "collector_line", "capacity": "60"},
        {"constraint_id": "ss-main", "kind": "substation_access", "capacity": "120"},
        {"constraint_id": "rescue-north", "kind": "rescue_coverage", "capacity": "2"},
    ],
}


def change_payload(request_id: str, *, amount: str = "18", key: str | None = None, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "request_id": request_id,
        "title": f"{request_id} 机位调整",
        "snapshot_id": "site-north",
        "equipment": [{"equipment_id": "WTG-T07", "model": "turbine-18mw", "rated_mw": "18"}],
        "coordinates": [{"equipment_id": "WTG-T07", "latitude": "21.386", "longitude": "112.914"}],
        "commissioning_curve": [{"date": "2026-12-01", "cumulative_mw": "18"}],
        "guarantee_level": "critical",
        "access_requirements": [
            {"constraint_id": "cl-array-3", "amount": amount},
            {"constraint_id": "ss-main", "amount": amount},
            {"constraint_id": "rescue-north", "amount": "1"},
        ],
        "construction_windows": [
            {"window_id": f"{request_id}-win", "vessel_id": "vessel-01",
             "starts_at": "2026-10-05T00:00:00Z", "ends_at": "2026-10-09T00:00:00Z"},
        ],
        "execution_steps": [
            {"seq": 1, "description": "基础沉桩"},
            {"seq": 2, "description": "海缆改接"},
        ],
        "rollback_plan": [{"seq": 1, "description": "恢复原接线"}],
        "idempotency_key": key or f"key-{request_id}",
    }
    payload.update(overrides)
    return payload


class ImpactTests(unittest.TestCase):
    def capacities(self) -> dict[str, ConstraintCapacity]:
        return {
            "cl-array-3": ConstraintCapacity("cl-array-3", "collector_line", Decimal("60"), "MW"),
            "rescue-north": ConstraintCapacity("rescue-north", "rescue_coverage", Decimal("2"), "slot"),
        }

    def evaluate(self, **kwargs: object) -> dict[str, object]:
        defaults: dict[str, object] = {
            "capacities": self.capacities(),
            "reserved": {},
            "demands": [],
            "own_windows": [],
            "other_windows": [],
            "pending_demands": [],
        }
        defaults.update(kwargs)
        return evaluate_impact(**defaults)  # type: ignore[arg-type]

    def test_remaining_capacity_accounts_for_reservations(self) -> None:
        result = self.evaluate(
            reserved={"cl-array-3": Decimal("18")},
            demands=[Demand("cl-array-3", Decimal("30"))],
        )
        row = result["constraints"][0]
        self.assertEqual(row["remaining_before"], "42.000")
        self.assertEqual(row["remaining_after"], "12.000")
        self.assertEqual(result["outcome"], "clear")

    def test_capacity_exceeded_is_blocking(self) -> None:
        result = self.evaluate(
            reserved={"cl-array-3": Decimal("50")},
            demands=[Demand("cl-array-3", Decimal("30"))],
        )
        self.assertEqual(result["outcome"], "conflicted")
        conflict = result["conflicts"][0]
        self.assertEqual(conflict["type"], "capacity_exceeded")
        self.assertTrue(conflict["blocking"])

    def test_unknown_constraint_is_blocking(self) -> None:
        result = self.evaluate(demands=[Demand("cl-missing", Decimal("1"))])
        self.assertEqual(result["outcome"], "conflicted")
        self.assertEqual(result["conflicts"][0]["type"], "unknown_constraint")

    def test_vessel_overlap_blocking_only_for_active_holds(self) -> None:
        own = [OwnWindow("w1", "vessel-01", "2026-10-05T00:00:00Z", "2026-10-09T00:00:00Z")]
        blocking_hold = [WindowHold("CR-X", "wx", "vessel-01", "2026-10-07T00:00:00Z", "2026-10-10T00:00:00Z", True)]
        warning_hold = [WindowHold("CR-Y", "wy", "vessel-01", "2026-10-07T00:00:00Z", "2026-10-10T00:00:00Z", False)]
        self.assertEqual(self.evaluate(own_windows=own, other_windows=blocking_hold)["outcome"], "conflicted")
        self.assertEqual(self.evaluate(own_windows=own, other_windows=warning_hold)["outcome"], "clear")
        no_overlap = [WindowHold("CR-Z", "wz", "vessel-01", "2026-10-09T00:00:00Z", "2026-10-11T00:00:00Z", True)]
        self.assertEqual(self.evaluate(own_windows=own, other_windows=no_overlap)["outcome"], "clear")

    def test_pending_competition_is_warning(self) -> None:
        result = self.evaluate(
            demands=[Demand("cl-array-3", Decimal("10"))],
            pending_demands=[PendingDemand("CR-P", "cl-array-3", Decimal("5"))],
        )
        self.assertEqual(result["outcome"], "clear")
        self.assertEqual(result["conflicts"][0]["type"], "pending_competition")
        self.assertFalse(result["conflicts"][0]["blocking"])


class ChangeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = ChangeService(self.connection, self.clock)
        for user_id, role in (
            ("eng", "engineer"), ("appr", "approver"), ("ops", "operator"),
            ("plan", "planner"), ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.create_snapshot("plan", SNAPSHOT)

    def tearDown(self) -> None:
        self.connection.close()

    def submit(self, request_id: str, **overrides: object) -> dict[str, object]:
        return self.service.submit_request("eng", change_payload(request_id, **overrides))

    def approve(self, request_id: str, expected_revision: int = 1) -> dict[str, object]:
        return self.service.approve_request("appr", request_id, expected_revision, "整体批准")

    def test_submit_evaluates_against_current_snapshot_revision(self) -> None:
        first = self.submit("CR-1")
        self.assertEqual(first["evaluation"]["snapshot_revision"], 1)
        self.assertEqual(first["evaluation"]["outcome"], "clear")
        self.service.revise_snapshot("plan", "site-north", {
            "constraints": [
                {"constraint_id": "cl-array-3", "kind": "collector_line", "capacity": "10"},
                {"constraint_id": "ss-main", "kind": "substation_access", "capacity": "120"},
                {"constraint_id": "rescue-north", "kind": "rescue_coverage", "capacity": "2"},
            ],
        })
        second = self.submit("CR-2")
        self.assertEqual(second["evaluation"]["snapshot_revision"], 2)
        self.assertEqual(second["evaluation"]["outcome"], "conflicted")

    def test_validation_of_payload_invariants(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.submit("CR-bad-curve", commissioning_curve=[{"date": "2026-12-01", "cumulative_mw": "9"}])
        with self.assertRaises(ValidationFailed):
            self.submit("CR-bad-coord", coordinates=[])
        with self.assertRaises(ValidationFailed):
            self.submit("CR-bad-level", guarantee_level="platinum")
        with self.assertRaises(ValidationFailed):
            self.submit("CR-no-rescue", guarantee_level="elevated",
                        access_requirements=[{"constraint_id": "cl-array-3", "amount": "18"}])
        with self.assertRaises(ValidationFailed):
            self.submit("CR-bad-window", construction_windows=[
                {"window_id": "w1", "vessel_id": "vessel-01",
                 "starts_at": "2026-10-05T00:00:00Z", "ends_at": "2026-10-09T00:00:00Z"},
                {"window_id": "w2", "vessel_id": "vessel-01",
                 "starts_at": "2026-10-08T00:00:00Z", "ends_at": "2026-10-12T00:00:00Z"},
            ])
        with self.assertRaises(ValidationFailed):
            self.submit("CR-no-rollback", rollback_plan=[{"seq": 1, "description": "x"}], execution_steps=[])

    def test_approval_requires_non_applicant_and_current_revision(self) -> None:
        self.submit("CR-1")
        with self.assertRaises(Forbidden):
            self.service.approve_request("eng", "CR-1", 1, "自我审批")
        with self.assertRaises(InvalidState):
            self.service.approve_request("appr", "CR-1", 99, "版本不符")
        with self.assertRaises(Forbidden):
            self.service.approve_request("ops", "CR-1", 1, "角色不符")

    def test_approval_reserves_capacity_and_blocks_oversubscription(self) -> None:
        self.submit("CR-1")
        approved = self.approve("CR-1")
        self.assertEqual(approved["state"], "approved")
        second = self.submit("CR-2", amount="50", key="key-CR-2",
                             construction_windows=[{"window_id": "CR-2-win", "vessel_id": "vessel-02",
                                                    "starts_at": "2026-10-05T00:00:00Z",
                                                    "ends_at": "2026-10-09T00:00:00Z"}])
        self.assertEqual(second["evaluation"]["outcome"], "conflicted")
        with self.assertRaises(Conflict):
            self.service.approve_request("appr", "CR-2", 1, "尝试批准")

    def test_approval_reevaluates_instead_of_trusting_submission(self) -> None:
        first = self.submit("CR-1", amount="50", key="key-CR-1",
                            construction_windows=[{"window_id": "CR-1-win", "vessel_id": "vessel-02",
                                                   "starts_at": "2026-10-05T00:00:00Z",
                                                   "ends_at": "2026-10-09T00:00:00Z"}])
        self.assertEqual(first["evaluation"]["outcome"], "clear")
        self.submit("CR-2")
        self.approve("CR-2")
        with self.assertRaises(Conflict):
            self.service.approve_request("appr", "CR-1", 1, "提交后余量已被占定")

    def test_vessel_window_overlap_blocks_approval(self) -> None:
        self.submit("CR-1")
        self.approve("CR-1")
        overlapping = self.submit("CR-2", key="key-CR-2",
                                  access_requirements=[{"constraint_id": "ss-main", "amount": "5"},
                                                       {"constraint_id": "rescue-north", "amount": "1"}])
        self.assertEqual(overlapping["evaluation"]["outcome"], "conflicted")
        types = {conflict["type"] for conflict in overlapping["evaluation"]["conflicts"]}
        self.assertIn("vessel_overlap", types)

    def test_receipts_drive_completion_and_consume_capacity(self) -> None:
        self.submit("CR-1")
        self.approve("CR-1")
        first = self.service.report_receipt("ops", "CR-1", 1, "succeeded", "沉桩完成", "rcpt-1")
        self.assertEqual(first["request_state"], "in_execution")
        second = self.service.report_receipt("ops", "CR-1", 2, "succeeded", "改接完成", "rcpt-2")
        self.assertEqual(second["request_state"], "completed")
        later = self.submit("CR-2", amount="50", key="key-CR-2",
                            construction_windows=[{"window_id": "CR-2-win", "vessel_id": "vessel-02",
                                                   "starts_at": "2026-11-01T00:00:00Z",
                                                   "ends_at": "2026-11-05T00:00:00Z"}])
        row = next(item for item in later["evaluation"]["constraints"] if item["constraint_id"] == "cl-array-3")
        self.assertEqual(row["remaining_before"], "42.000")
        self.assertEqual(later["evaluation"]["outcome"], "conflicted")

    def test_failed_step_blocks_and_cannot_be_aggregated_as_success(self) -> None:
        self.submit("CR-1")
        self.approve("CR-1")
        self.service.report_receipt("ops", "CR-1", 1, "succeeded", "沉桩完成", "rcpt-1")
        failed = self.service.report_receipt("ops", "CR-1", 2, "failed", "海缆受损", "rcpt-2")
        self.assertEqual(failed["request_state"], "blocked")
        with self.assertRaises(InvalidState):
            self.service.report_receipt("ops", "CR-1", 2, "succeeded", "补报成功", "rcpt-3")
        self.assertEqual(self.service.get_request("eng", "CR-1")["state"], "blocked")

    def test_rollback_releases_reserved_capacity(self) -> None:
        self.submit("CR-1")
        self.approve("CR-1")
        self.service.report_receipt("ops", "CR-1", 1, "failed", "沉桩失败", "rcpt-1")
        rolled_back = self.service.execute_rollback("ops", "CR-1", "按撤回方案恢复")
        self.assertEqual(rolled_back["state"], "rolled_back")
        self.assertEqual(rolled_back["released_reservations"], 3)
        later = self.submit("CR-2", amount="60", key="key-CR-2",
                            equipment=[{"equipment_id": "WTG-T09", "model": "turbine-18mw", "rated_mw": "60"}],
                            coordinates=[{"equipment_id": "WTG-T09", "latitude": "21.4", "longitude": "112.9"}],
                            commissioning_curve=[{"date": "2026-12-01", "cumulative_mw": "60"}],
                            access_requirements=[{"constraint_id": "cl-array-3", "amount": "60"},
                                                 {"constraint_id": "rescue-north", "amount": "1"}],
                            construction_windows=[{"window_id": "CR-2-win", "vessel_id": "vessel-02",
                                                   "starts_at": "2026-11-01T00:00:00Z",
                                                   "ends_at": "2026-11-05T00:00:00Z"}])
        row = next(item for item in later["evaluation"]["constraints"] if item["constraint_id"] == "cl-array-3")
        self.assertEqual(row["remaining_before"], "60.000")

    def test_manual_takeover_holds_capacity_and_allows_only_rollback(self) -> None:
        self.submit("CR-1")
        self.approve("CR-1")
        self.service.report_receipt("ops", "CR-1", 1, "failed", "沉桩失败", "rcpt-1")
        taken = self.service.take_over("ops", "CR-1", "现场指挥部接管")
        self.assertEqual(taken["state"], "manual_hold")
        with self.assertRaises(InvalidState):
            self.service.report_receipt("ops", "CR-1", 2, "succeeded", "补报", "rcpt-2")
        with self.assertRaises(InvalidState):
            self.service.withdraw_request("eng", "CR-1")
        rolled_back = self.service.execute_rollback("ops", "CR-1", "接管后决定回退")
        self.assertEqual(rolled_back["state"], "rolled_back")

    def test_revision_links_original_without_overwriting_history(self) -> None:
        self.submit("CR-1")
        self.approve("CR-1")
        revision = self.service.revise_request("eng", "CR-1", change_payload("CR-2", amount="12", key="key-CR-2"))
        self.assertEqual(revision["design_revision"], 2)
        self.assertEqual(revision["root_request_id"], "CR-1")
        approved = self.service.approve_request("appr", "CR-2", 1, "批准修订")
        self.assertEqual(approved["state"], "approved")
        self.assertEqual(self.service.get_request("eng", "CR-1")["state"], "superseded")
        history = self.service.request_history("eng", "CR-2")
        self.assertEqual([row["request_id"] for row in history["revisions"]], ["CR-1", "CR-2"])
        original = self.service.explain_request("eng", "CR-1")
        self.assertEqual(original["request"]["access_requirements"][0]["amount"], "18")
        with self.assertRaises(Conflict):
            self.service.revise_request("eng", "CR-1", change_payload("CR-3", key="key-CR-3"))

    def test_revision_of_executing_original_cannot_be_approved(self) -> None:
        self.submit("CR-1")
        self.approve("CR-1")
        self.service.report_receipt("ops", "CR-1", 1, "succeeded", "沉桩完成", "rcpt-1")
        self.service.revise_request("eng", "CR-1", change_payload("CR-2", amount="12", key="key-CR-2"))
        with self.assertRaises(InvalidState):
            self.service.approve_request("appr", "CR-2", 1, "原申请仍在执行")

    def test_withdraw_releases_reservations(self) -> None:
        self.submit("CR-1")
        self.approve("CR-1")
        withdrawn = self.service.withdraw_request("eng", "CR-1")
        self.assertEqual(withdrawn["state"], "withdrawn")
        rows = self.connection.execute(
            "SELECT state FROM change_reservations WHERE request_id='CR-1'"
        ).fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(row["state"] == "released" for row in rows))

    def test_reject_records_rationale(self) -> None:
        self.submit("CR-1")
        rejected = self.service.reject_request("appr", "CR-1", "施工窗口与检修计划冲突")
        self.assertEqual(rejected["state"], "rejected")
        explain = self.service.explain_request("eng", "CR-1")
        self.assertEqual(explain["decision"]["rationale"], "施工窗口与检修计划冲突")

    def test_submit_and_receipt_are_idempotent(self) -> None:
        payload = change_payload("CR-1")
        first = self.service.submit_request("eng", payload)
        self.assertEqual(first, self.service.submit_request("eng", payload))
        with self.assertRaises(Conflict):
            self.service.submit_request("eng", change_payload("CR-9", key="key-CR-1"))
        self.approve("CR-1")
        receipt = self.service.report_receipt("ops", "CR-1", 1, "succeeded", "沉桩完成", "rcpt-1")
        self.assertEqual(receipt, self.service.report_receipt("ops", "CR-1", 1, "succeeded", "沉桩完成", "rcpt-1"))
        with self.assertRaises(Conflict):
            self.service.report_receipt("ops", "CR-1", 1, "failed", "改口", "rcpt-1")

    def test_explain_shows_snapshot_constraints_and_conflicts(self) -> None:
        self.submit("CR-1")
        self.approve("CR-1")
        self.submit("CR-2", amount="50", key="key-CR-2",
                    construction_windows=[{"window_id": "CR-2-win", "vessel_id": "vessel-02",
                                           "starts_at": "2026-10-05T00:00:00Z",
                                           "ends_at": "2026-10-09T00:00:00Z"}])
        explain = self.service.explain_request("audit", "CR-2")
        evaluation = explain["evaluations"][0]
        self.assertEqual(evaluation["snapshot_id"], "site-north")
        self.assertEqual(evaluation["snapshot_revision"], 1)
        self.assertEqual(evaluation["outcome"], "conflicted")
        exceeded = next(item for item in evaluation["conflicts"] if item["type"] == "capacity_exceeded")
        self.assertEqual(exceeded["constraint_id"], "cl-array-3")
        collector = next(item for item in evaluation["constraints"] if item["constraint_id"] == "cl-array-3")
        self.assertEqual(collector["reserved"], "18.000")
        approval_explain = self.service.explain_request("audit", "CR-1")
        self.assertEqual(approval_explain["decision"]["decided_by"], "appr")
        approval_evaluation = next(
            item for item in approval_explain["evaluations"] if item["purpose"] == "approval"
        )
        self.assertEqual(approval_explain["decision"]["evaluation_id"], approval_evaluation["evaluation_id"])
        self.assertTrue(approval_explain["audit_trail"])

    def test_audit_chain_detects_tampering(self) -> None:
        self.submit("CR-1")
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE change_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])

    def test_unknown_request_and_snapshot_raise_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.service.get_request("eng", "CR-missing")
        with self.assertRaises(NotFound):
            self.submit("CR-x", snapshot_id="site-missing")

    def test_api_exposes_browser_free_boundary(self) -> None:
        app = JsonApplication(self.service)
        self.assertEqual(app.handle("GET", "/health").status, 200)
        missing_actor = app.handle("GET", "/changes/CR-1")
        self.assertEqual(missing_actor.status, 422)
        response = app.handle("GET", "/changes/CR-1", {"X-Actor-Id": "eng"})
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "not_found")
        created = app.handle(
            "POST", "/changes", {"X-Actor-Id": "eng"},
            __import__("json").dumps(change_payload("CR-1")).encode("utf-8"),
        )
        self.assertEqual(created.status, 201)
        explained = app.handle("GET", "/changes/CR-1/explain", {"X-Actor-Id": "audit"})
        self.assertEqual(explained.status, 200)
        self.assertEqual(explained.body["request"]["state"], "evaluated")


if __name__ == "__main__":
    unittest.main()
