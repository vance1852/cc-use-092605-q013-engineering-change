from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from change_control.api import JsonApplication
from change_control.clock import FrozenClock
from change_control.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from change_control.impact import (
    ContendingLoad,
    PoolDemand,
    assess_constraints,
    capacity_shortfalls,
    windows_overlap,
)
from change_control.service import ChangeService


def change_payload(change_id: str, key: str, **overrides: object) -> dict[str, object]:
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
            {"pool_id": "collector-line-a", "amount": "36"},
            {"pool_id": "vessel-alpha", "amount": "10"},
            {"pool_id": "rescue-north", "amount": "1"},
        ],
        "construction_windows": [
            {"window_id": "w1", "starts_at": "2026-10-10T00:00:00Z", "ends_at": "2026-10-20T00:00:00Z"},
        ],
        "rollback_plan": {"summary": "恢复原接线", "steps": ["拆除临时固定", "恢复电缆"]},
        "execution_steps": [
            {"step_id": "s1", "description": "基础沉桩"},
            {"step_id": "s2", "description": "集电线路改接"},
        ],
        "idempotency_key": key,
    }
    payload.update(overrides)
    return payload


class ContractTests(unittest.TestCase):
    def submit(self, **overrides: object) -> None:
        from change_control.models import ChangeSubmission

        ChangeSubmission.from_dict(change_payload("chg-x", "key-x", **overrides))

    def test_coordinate_range_is_enforced(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.submit(coordinates=[{"point_id": "WTG-07", "latitude": "91", "longitude": "112"}])
        with self.assertRaises(ValidationFailed):
            self.submit(coordinates=[{"point_id": "WTG-07", "latitude": "21", "longitude": "181"}])

    def test_commissioning_curve_must_be_ordered_and_non_decreasing(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.submit(commissioning_curve=[
                {"date": "2026-12-01", "cumulative_mw": "36"},
                {"date": "2026-11-01", "cumulative_mw": "18"},
            ])
        with self.assertRaises(ValidationFailed):
            self.submit(commissioning_curve=[
                {"date": "2026-11-01", "cumulative_mw": "36"},
                {"date": "2026-12-01", "cumulative_mw": "18"},
            ])

    def test_window_must_end_after_start(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.submit(construction_windows=[
                {"window_id": "w1", "starts_at": "2026-10-20T00:00:00Z", "ends_at": "2026-10-10T00:00:00Z"},
            ])

    def test_duplicate_equipment_and_version_required(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.submit(equipment=[
                {"equipment_id": "WTG-07", "model": "m", "version": "v1", "quantity": 1},
                {"equipment_id": "WTG-07", "model": "m", "version": "v2", "quantity": 1},
            ])
        with self.assertRaises(ValidationFailed):
            self.submit(equipment=[{"equipment_id": "WTG-07", "model": "m", "version": " ", "quantity": 1}])

    def test_rollback_plan_and_steps_are_mandatory(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.submit(rollback_plan={"summary": "", "steps": ["x"]})
        with self.assertRaises(ValidationFailed):
            self.submit(execution_steps=[])


class ImpactTests(unittest.TestCase):
    def test_windows_overlap(self) -> None:
        first = ("2026-10-10T00:00:00Z", "2026-10-20T00:00:00Z")
        self.assertTrue(windows_overlap(first, ("2026-10-15T00:00:00Z", "2026-10-25T00:00:00Z")))
        self.assertFalse(windows_overlap(first, ("2026-10-20T00:00:00Z", "2026-10-25T00:00:00Z")))
        self.assertFalse(windows_overlap(first, ("2026-10-01T00:00:00Z", "2026-10-10T00:00:00Z")))

    def test_remaining_and_conflicts_are_deterministic(self) -> None:
        result = assess_constraints(
            capacities={"collector-line-a": Decimal("120")},
            pool_kinds={"collector-line-a": "collector_line"},
            demands=[PoolDemand("collector-line-a", Decimal("36"))],
            held_by_others=[ContendingLoad("chg-9", "collector-line-a", Decimal("40"), ())],
            pending_by_others=[ContendingLoad("chg-8", "collector-line-a", Decimal("10"), ())],
            own_windows=[],
        )
        constraint = result["constraints"][0]
        self.assertEqual(constraint["remaining"], "80.000")
        self.assertTrue(constraint["fits"])
        self.assertEqual(
            [(c["other_change_id"], c["conflict_kind"]) for c in result["conflicts"]],
            [("chg-8", "pending_request"), ("chg-9", "held_reservation")],
        )

    def test_vessel_window_loads_only_count_overlapping_windows(self) -> None:
        capacities = {"vessel-alpha": Decimal("30")}
        kinds = {"vessel-alpha": "vessel_window"}
        demands = [PoolDemand("vessel-alpha", Decimal("10"))]
        own = [("2026-10-10T00:00:00Z", "2026-10-20T00:00:00Z")]
        overlapping = ContendingLoad("chg-9", "vessel-alpha", Decimal("25"), (("2026-10-15T00:00:00Z", "2026-10-16T00:00:00Z"),))
        disjoint = ContendingLoad("chg-8", "vessel-alpha", Decimal("25"), (("2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z"),))
        result = assess_constraints(
            capacities=capacities,
            pool_kinds=kinds,
            demands=demands,
            held_by_others=[overlapping, disjoint],
            pending_by_others=[],
            own_windows=own,
        )
        self.assertEqual(result["constraints"][0]["held_by_others"], "25.000")
        self.assertEqual([c["other_change_id"] for c in result["conflicts"]], ["chg-9"])

    def test_shortfall_detection(self) -> None:
        shortfalls = capacity_shortfalls(
            capacities={"collector-line-a": Decimal("120")},
            pool_kinds={"collector-line-a": "collector_line"},
            demands=[PoolDemand("collector-line-a", Decimal("36"))],
            held_by_others=[ContendingLoad("chg-9", "collector-line-a", Decimal("90"), ())],
            own_windows=[],
        )
        self.assertEqual(shortfalls, ["collector-line-a"])


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = ChangeService(self.connection, self.clock)
        for user_id, role in (
            ("plan", "planner"),
            ("eng-1", "engineer"),
            ("eng-2", "engineer"),
            ("field", "field"),
            ("appr", "approver"),
            ("ctrl", "controller"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.register_pool("plan", {"pool_id": "collector-line-a", "name": "集电线路 A 回", "kind": "collector_line", "unit": "MW", "capacity": "120"})
        self.service.register_pool("plan", {"pool_id": "vessel-alpha", "name": "阿尔法号施工船", "kind": "vessel_window", "unit": "船日", "capacity": "30"})
        self.service.register_pool("plan", {"pool_id": "rescue-north", "name": "北部救援覆盖", "kind": "rescue_coverage", "unit": "单元", "capacity": "4"})

    def tearDown(self) -> None:
        self.connection.close()

    def submit(self, change_id: str = "chg-001", key: str = "key-001", **overrides: object) -> dict[str, object]:
        return self.service.submit_change("eng-1", change_payload(change_id, key, **overrides))

    def approve(self, change_id: str = "chg-001", revision: int = 1) -> dict[str, object]:
        return self.service.approve_change("appr", change_id, revision, "整体同意")

    def test_submission_takes_snapshot_and_assesses_constraints(self) -> None:
        result = self.submit()
        self.assertEqual(result["state"], "submitted")
        snapshot = self.service.snapshot("audit", result["snapshot_id"])
        self.assertEqual(len(snapshot["entries"]), 3)
        assessment = result["assessment"]
        self.assertTrue(assessment["fits"])
        remaining = {row["pool_id"]: row["remaining"] for row in assessment["constraints"]}
        self.assertEqual(remaining["collector-line-a"], "120.000")

    def test_assurance_level_a_requires_rescue_coverage(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.submit(demands=[{"pool_id": "collector-line-a", "amount": "36"}])

    def test_unknown_pool_is_rejected(self) -> None:
        with self.assertRaises(NotFound):
            self.submit(demands=[
                {"pool_id": "collector-line-a", "amount": "36"},
                {"pool_id": "rescue-missing", "amount": "1"},
            ])

    def test_submission_is_idempotent_and_payload_conflict_is_detected(self) -> None:
        first = self.submit()
        replay = self.submit()
        self.assertEqual(first, replay)
        with self.assertRaises(Conflict):
            self.submit(change_id="chg-002", title="另一份内容")

    def test_applicant_cannot_approve_own_change(self) -> None:
        self.submit()
        with self.assertRaises(Forbidden):
            self.service.approve_change("eng-1", "chg-001", 1)
        with self.assertRaises(Forbidden):
            self.service.reject_change("eng-1", "chg-001", 1, "自查")

    def test_engineer_role_cannot_approve(self) -> None:
        self.submit()
        with self.assertRaises(Forbidden):
            self.service.approve_change("eng-2", "chg-001", 1)

    def test_approval_occupies_capacity_and_later_assessment_sees_it(self) -> None:
        self.submit()
        approved = self.approve()
        self.assertEqual(approved["state"], "approved")
        ledger = self.service.ledger_status("appr")
        collector = next(pool for pool in ledger["pools"] if pool["pool_id"] == "collector-line-a")
        self.assertEqual(collector["held"], "36.000")
        self.assertEqual(collector["remaining"], "84.000")
        self.assertEqual(collector["holding_changes"], ["chg-001"])
        second = self.service.submit_change("eng-2", change_payload("chg-002", "key-002"))
        assessment = second["assessment"]
        collector_row = next(row for row in assessment["constraints"] if row["pool_id"] == "collector-line-a")
        self.assertEqual(collector_row["held_by_others"], "36.000")
        self.assertEqual(collector_row["remaining"], "84.000")
        self.assertIn(
            {"other_change_id": "chg-001", "pool_id": "collector-line-a", "conflict_kind": "held_reservation", "amount": "36.000"},
            assessment["conflicts"],
        )

    def test_pending_application_appears_as_conflict(self) -> None:
        self.submit()
        second = self.service.submit_change("eng-2", change_payload("chg-002", "key-002"))
        kinds = {(c["other_change_id"], c["conflict_kind"]) for c in second["assessment"]["conflicts"]}
        self.assertIn(("chg-001", "pending_request"), kinds)

    def test_approval_fails_when_capacity_no_longer_sufficient(self) -> None:
        self.submit()
        self.approve()
        self.service.submit_change("eng-2", change_payload(
            "chg-002", "key-002",
            demands=[
                {"pool_id": "collector-line-a", "amount": "90"},
                {"pool_id": "rescue-north", "amount": "1"},
            ],
        ))
        with self.assertRaises(Conflict):
            self.service.approve_change("appr", "chg-002", 1)

    def test_approval_requires_current_revision(self) -> None:
        self.submit()
        with self.assertRaises(InvalidState):
            self.service.approve_change("appr", "chg-001", 2)

    def test_reject_records_decision_and_is_terminal(self) -> None:
        self.submit()
        rejected = self.service.reject_change("appr", "chg-001", 1, "余量不足")
        self.assertEqual(rejected["state"], "rejected")
        with self.assertRaises(InvalidState):
            self.service.approve_change("appr", "chg-001", 2)
        explain = self.service.explain_change("audit", "chg-001")
        self.assertEqual(explain["decisions"][0]["decision"], "rejected")
        self.assertEqual(explain["decisions"][0]["reason"], "余量不足")

    def test_withdraw_only_by_applicant(self) -> None:
        self.submit()
        with self.assertRaises(Forbidden):
            self.service.withdraw_change("eng-2", "chg-001", 1)
        withdrawn = self.service.withdraw_change("eng-1", "chg-001", 1)
        self.assertEqual(withdrawn["state"], "withdrawn")

    def test_receipts_advance_change_to_completed(self) -> None:
        self.submit()
        self.approve()
        first = self.service.record_receipt("field", "chg-001", {"step_id": "s1", "outcome": "succeeded", "note": "到位", "receipt_ref": "r-1"})
        self.assertEqual(first["state"], "in_progress")
        second = self.service.record_receipt("field", "chg-001", {"step_id": "s2", "outcome": "succeeded", "note": "完成", "receipt_ref": "r-2"})
        self.assertEqual(second["state"], "completed")
        with self.assertRaises(InvalidState):
            self.service.record_receipt("field", "chg-001", {"step_id": "s1", "outcome": "succeeded", "note": "重复", "receipt_ref": "r-3"})

    def test_receipt_requires_approval_first_and_unique_reference(self) -> None:
        self.submit()
        with self.assertRaises(InvalidState):
            self.service.record_receipt("field", "chg-001", {"step_id": "s1", "outcome": "succeeded", "note": "太早", "receipt_ref": "r-1"})
        self.approve()
        self.service.record_receipt("field", "chg-001", {"step_id": "s1", "outcome": "succeeded", "note": "到位", "receipt_ref": "r-1"})
        with self.assertRaises(Conflict):
            self.service.record_receipt("field", "chg-001", {"step_id": "s2", "outcome": "succeeded", "note": "重号", "receipt_ref": "r-1"})
        with self.assertRaises(Conflict):
            self.service.record_receipt("field", "chg-001", {"step_id": "s1", "outcome": "succeeded", "note": "重复步骤", "receipt_ref": "r-2"})
        with self.assertRaises(NotFound):
            self.service.record_receipt("field", "chg-001", {"step_id": "s9", "outcome": "succeeded", "note": "无此步骤", "receipt_ref": "r-3"})

    def test_partial_failure_cannot_be_aggregated_into_success(self) -> None:
        self.submit()
        self.approve()
        self.service.record_receipt("field", "chg-001", {"step_id": "s1", "outcome": "succeeded", "note": "到位", "receipt_ref": "r-1"})
        failed = self.service.record_receipt("field", "chg-001", {"step_id": "s2", "outcome": "failed", "note": "电缆终端击穿", "receipt_ref": "r-2"})
        self.assertEqual(failed["state"], "failed")
        with self.assertRaises(InvalidState):
            self.service.record_receipt("field", "chg-001", {"step_id": "s2", "outcome": "succeeded", "note": "补记成功", "receipt_ref": "r-3"})
        self.assertEqual(self.service.get_change("audit", "chg-001")["state"], "failed")

    def test_failed_change_can_only_rollback_or_be_taken_over(self) -> None:
        self.submit()
        self.approve()
        self.service.record_receipt("field", "chg-001", {"step_id": "s1", "outcome": "failed", "note": "沉桩偏位", "receipt_ref": "r-1"})
        with self.assertRaises(InvalidState):
            self.service.takeover_change("ctrl", "chg-001", 99, "版本不符")
        taken = self.service.takeover_change("ctrl", "chg-001", 3, "现场指挥部接管")
        self.assertEqual(taken["state"], "manual_control")
        rolled_back = self.service.rollback_change("ctrl", "chg-001", 4, "人工处置完毕登记回退")
        self.assertEqual(rolled_back["state"], "rolled_back")
        ledger = self.service.ledger_status("ctrl")
        self.assertTrue(all(pool["held"] == "0.000" for pool in ledger["pools"]))

    def test_rollback_releases_reserved_capacity(self) -> None:
        self.submit()
        self.approve()
        rolled_back = self.service.rollback_change("ctrl", "chg-001", 2, "计划取消")
        self.assertEqual(rolled_back["released_reservations"], 3)
        ledger = self.service.ledger_status("appr")
        collector = next(pool for pool in ledger["pools"] if pool["pool_id"] == "collector-line-a")
        self.assertEqual(collector["remaining"], "120.000")

    def test_revision_links_original_without_overwriting_history(self) -> None:
        self.submit()
        self.approve()
        self.service.record_receipt("field", "chg-001", {"step_id": "s1", "outcome": "failed", "note": "沉桩偏位", "receipt_ref": "r-1"})
        revision = self.service.submit_change("eng-1", change_payload(
            "chg-002", "key-002",
            title="WTG-07 基础位置调整（修订）",
            supersedes_change_id="chg-001",
        ))
        self.assertEqual(revision["state"], "submitted")
        original = self.service.get_change("audit", "chg-001")
        self.assertEqual(original["state"], "superseded")
        self.assertEqual(original["equipment"][0]["version"], "rev-3")
        explain_original = self.service.explain_change("audit", "chg-001")
        self.assertEqual(len(explain_original["receipts"]), 1)
        self.assertEqual(explain_original["reservations"][0]["state"], "released")
        lineage = self.service.change_lineage("audit", "chg-002")
        self.assertEqual([item["change_id"] for item in lineage["chain"]], ["chg-001", "chg-002"])
        self.assertEqual(lineage["root_change_id"], "chg-001")

    def test_terminal_change_cannot_be_revised(self) -> None:
        self.submit()
        self.service.reject_change("appr", "chg-001", 1, "余量不足")
        with self.assertRaises(InvalidState):
            self.service.submit_change("eng-1", change_payload("chg-002", "key-002", supersedes_change_id="chg-001"))

    def test_revision_of_approved_change_releases_its_capacity_for_the_successor(self) -> None:
        self.submit(demands=[
            {"pool_id": "collector-line-a", "amount": "100"},
            {"pool_id": "rescue-north", "amount": "1"},
        ])
        self.approve()
        revision = self.service.submit_change("eng-1", change_payload(
            "chg-002", "key-002",
            demands=[
                {"pool_id": "collector-line-a", "amount": "100"},
                {"pool_id": "rescue-north", "amount": "1"},
            ],
            supersedes_change_id="chg-001",
        ))
        collector_row = next(row for row in revision["assessment"]["constraints"] if row["pool_id"] == "collector-line-a")
        self.assertEqual(collector_row["remaining"], "120.000")
        self.assertTrue(revision["assessment"]["fits"])

    def test_explain_shows_snapshot_constraints_conflicts_and_decision(self) -> None:
        self.submit()
        self.approve()
        self.service.submit_change("eng-2", change_payload("chg-002", "key-002"))
        explain = self.service.explain_change("audit", "chg-002")
        self.assertEqual(explain["snapshot"]["snapshot_id"], explain["assessment"]["snapshot_id"])
        self.assertEqual(len(explain["snapshot"]["entries"]), 3)
        kinds = {row["kind"] for row in explain["assessment"]["constraints"]}
        self.assertEqual(kinds, {"collector_line", "vessel_window", "rescue_coverage"})
        self.assertIn("chg-001", {c["other_change_id"] for c in explain["assessment"]["conflicts"]})
        approved_explain = self.service.explain_change("audit", "chg-001")
        self.assertEqual(approved_explain["decisions"][0]["decision"], "approved")
        self.assertEqual(approved_explain["decisions"][0]["actor_id"], "appr")
        self.assertEqual(approved_explain["decisions"][0]["snapshot_id"], approved_explain["snapshot"]["snapshot_id"])

    def test_snapshot_capacity_is_preserved_for_later_explanation(self) -> None:
        result = self.submit()
        snapshot_id = result["snapshot_id"]
        self.service.adjust_pool("plan", "collector-line-a", "200", 1, "线路扩容")
        snapshot = self.service.snapshot("audit", snapshot_id)
        entry = next(row for row in snapshot["entries"] if row["pool_id"] == "collector-line-a")
        self.assertEqual(entry["capacity"], "120")
        self.assertEqual(entry["pool_revision"], 1)

    def test_audit_chain_is_valid(self) -> None:
        self.submit()
        self.approve()
        self.service.record_receipt("field", "chg-001", {"step_id": "s1", "outcome": "succeeded", "note": "到位", "receipt_ref": "r-1"})
        chain = self.service.audit_chain("audit")
        self.assertTrue(chain["valid"])
        self.assertGreater(chain["events"], 0)
        with self.assertRaises(Forbidden):
            self.service.audit_chain("eng-1")


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(ChangeService(self.connection, FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))))

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, payload: dict[str, object], actor: str = "plan") -> tuple[int, dict[str, object]]:
        response = self.app.handle(
            "POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode("utf-8")
        )
        return response.status, dict(response.body)

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)

    def test_missing_actor_is_rejected(self) -> None:
        response = self.app.handle("POST", "/pools", body=b"{}")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_full_flow_over_http(self) -> None:
        status, _ = self.post("/users", {"user_id": "plan", "display_name": "计划", "role": "planner"})
        self.assertEqual(status, 201)
        for user_id, role in (("eng", "engineer"), ("fld", "field"), ("appr", "approver"), ("ctrl", "controller"), ("aud", "auditor")):
            self.post("/users", {"user_id": user_id, "display_name": user_id, "role": role})
        status, _ = self.post("/pools", {"pool_id": "collector-line-a", "name": "集电线路", "kind": "collector_line", "unit": "MW", "capacity": "120"})
        self.assertEqual(status, 201)
        self.post("/pools", {"pool_id": "rescue-north", "name": "救援", "kind": "rescue_coverage", "unit": "单元", "capacity": "4"})
        self.post("/pools", {"pool_id": "vessel-alpha", "name": "施工船", "kind": "vessel_window", "unit": "船日", "capacity": "30"})
        status, body = self.post("/changes", change_payload("chg-1", "key-1"), actor="eng")
        self.assertEqual(status, 201)
        self.assertTrue(body["assessment"]["fits"])
        status, body = self.post("/changes/chg-1/approve", {"expected_revision": 1}, actor="appr")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "approved")
        status, body = self.post("/changes/chg-1/receipts", {"step_id": "s1", "outcome": "succeeded", "note": "到位", "receipt_ref": "r-1"}, actor="fld")
        self.assertEqual(status, 201)
        self.assertEqual(body["state"], "in_progress")
        status, body = self.post("/changes/chg-1/receipts", {"step_id": "s2", "outcome": "succeeded", "note": "完成", "receipt_ref": "r-2"}, actor="fld")
        self.assertEqual(body["state"], "completed")
        response = self.app.handle("GET", "/changes/chg-1/explain", {"X-Actor-Id": "aud"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["state"], "completed")
        response = self.app.handle("GET", "/ledger", {"X-Actor-Id": "aud"})
        self.assertEqual(response.status, 200)
        response = self.app.handle("GET", "/audit/chain", {"X-Actor-Id": "aud"})
        self.assertTrue(response.body["valid"])

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope", {"X-Actor-Id": "x"})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
