"""工程变更申请、设施快照、整体审批与现场回执的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .impact import (
    ConstraintCapacity,
    Demand,
    OwnWindow,
    PendingDemand,
    WindowHold,
    canonical_json,
    decimal_text,
    digest,
    evaluate_impact,
    quantize_amount,
)
from .models import (
    RESCUE_REQUIRED_LEVELS,
    ChangePayload,
    identifier,
    parse_snapshot_constraints,
    required_text,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "engineer": {"change.submit", "change.revise", "change.withdraw"},
    "approver": {"change.approve"},
    "operator": {"receipt.write", "rollback.execute", "takeover.execute"},
    "planner": {"snapshot.write"},
    "auditor": {"change.read", "audit.read"},
}

ACTIVE_WINDOW_STATES = ("evaluated", "approved", "in_execution", "blocked", "manual_hold")
BLOCKING_WINDOW_STATES = ("approved", "in_execution", "blocked", "manual_hold")
EXECUTING_STATES = ("in_execution", "blocked", "manual_hold")


class ChangeService:
    """在单个 SQLite 连接上提供工程变更全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM change_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM change_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO change_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO change_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------------
    # 设施快照
    # ------------------------------------------------------------------

    def _latest_snapshot(self, snapshot_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM facility_snapshots WHERE snapshot_id=? ORDER BY revision DESC LIMIT 1",
            (snapshot_id,),
        ).fetchone()

    def _insert_constraints(
        self,
        snapshot_id: str,
        revision: int,
        constraints: tuple[Any, ...],
    ) -> None:
        for item in constraints:
            self.connection.execute(
                "INSERT INTO snapshot_constraints(snapshot_id,revision,constraint_id,kind,capacity,unit) "
                "VALUES(?,?,?,?,?,?)",
                (snapshot_id, revision, item.constraint_id, item.kind, decimal_text(item.capacity), item.unit),
            )

    def create_snapshot(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "snapshot.write")
        snapshot_id = identifier(raw.get("snapshot_id"), "snapshot_id")
        constraints = parse_snapshot_constraints(raw.get("constraints"))
        content_sha256 = hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO facility_snapshots(snapshot_id,revision,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (snapshot_id, 1, content_sha256, actor_id, self._now()),
                )
                self._insert_constraints(snapshot_id, 1, constraints)
                self._audit("snapshot", snapshot_id, "snapshot.created", actor_id, {"revision": 1})
        except sqlite3.IntegrityError as exc:
            raise Conflict("设施快照已经存在，请使用修订接口") from exc
        return {"snapshot_id": snapshot_id, "revision": 1, "sha256": content_sha256}

    def revise_snapshot(self, actor_id: str, snapshot_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "snapshot.write")
        constraints = parse_snapshot_constraints(raw.get("constraints"))
        latest = self._latest_snapshot(snapshot_id)
        if latest is None:
            raise NotFound("设施快照不存在")
        revision = int(latest["revision"]) + 1
        content_sha256 = hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO facility_snapshots(snapshot_id,revision,content_sha256,created_by,created_at) "
                "VALUES(?,?,?,?,?)",
                (snapshot_id, revision, content_sha256, actor_id, self._now()),
            )
            self._insert_constraints(snapshot_id, revision, constraints)
            self._audit("snapshot", snapshot_id, "snapshot.revised", actor_id, {"revision": revision})
        return {"snapshot_id": snapshot_id, "revision": revision, "sha256": content_sha256}

    def current_snapshot(self, actor_id: str, snapshot_id: str) -> dict[str, Any]:
        self._user(actor_id)
        row = self._latest_snapshot(snapshot_id)
        if row is None:
            raise NotFound("设施快照不存在")
        constraints = self.connection.execute(
            "SELECT constraint_id,kind,capacity,unit FROM snapshot_constraints "
            "WHERE snapshot_id=? AND revision=? ORDER BY constraint_id",
            (snapshot_id, row["revision"]),
        ).fetchall()
        return {
            "snapshot_id": snapshot_id,
            "revision": row["revision"],
            "content_sha256": row["content_sha256"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "constraints": [dict(item) for item in constraints],
        }

    # ------------------------------------------------------------------
    # 影响评估
    # ------------------------------------------------------------------

    def _capacities(self, snapshot_id: str, revision: int) -> dict[str, ConstraintCapacity]:
        rows = self.connection.execute(
            "SELECT constraint_id,kind,capacity,unit FROM snapshot_constraints "
            "WHERE snapshot_id=? AND revision=?",
            (snapshot_id, revision),
        ).fetchall()
        return {
            row["constraint_id"]: ConstraintCapacity(
                row["constraint_id"], row["kind"], Decimal(row["capacity"]), row["unit"]
            )
            for row in rows
        }

    def _reserved_amounts(self, exclude_request_ids: set[str]) -> dict[str, Decimal]:
        rows = self.connection.execute(
            "SELECT request_id,constraint_id,amount FROM change_reservations WHERE state IN ('held','consumed')"
        ).fetchall()
        totals: dict[str, Decimal] = {}
        for row in rows:
            if row["request_id"] in exclude_request_ids:
                continue
            totals[row["constraint_id"]] = totals.get(row["constraint_id"], Decimal("0")) + Decimal(row["amount"])
        return totals

    def _pending_demands(self, exclude_request_ids: set[str]) -> list[PendingDemand]:
        rows = self.connection.execute(
            "SELECT request_id,access_json FROM change_requests WHERE state='evaluated'"
        ).fetchall()
        demands: list[PendingDemand] = []
        for row in rows:
            if row["request_id"] in exclude_request_ids:
                continue
            for item in json.loads(row["access_json"]):
                demands.append(
                    PendingDemand(row["request_id"], item["constraint_id"], Decimal(str(item["amount"])))
                )
        return demands

    def _window_holds(self, exclude_request_ids: set[str]) -> list[WindowHold]:
        placeholders = ",".join("?" for _ in ACTIVE_WINDOW_STATES)
        rows = self.connection.execute(
            "SELECT w.request_id,w.window_id,w.vessel_id,w.starts_at,w.ends_at,r.state "
            "FROM change_windows w JOIN change_requests r ON r.request_id=w.request_id "
            f"WHERE r.state IN ({placeholders})",
            ACTIVE_WINDOW_STATES,
        ).fetchall()
        holds: list[WindowHold] = []
        for row in rows:
            if row["request_id"] in exclude_request_ids:
                continue
            holds.append(
                WindowHold(
                    row["request_id"],
                    row["window_id"],
                    row["vessel_id"],
                    row["starts_at"],
                    row["ends_at"],
                    row["state"] in BLOCKING_WINDOW_STATES,
                )
            )
        return holds

    def _run_evaluation(
        self,
        snapshot_id: str,
        demands: list[Demand],
        own_windows: list[OwnWindow],
        exclude_request_ids: set[str],
    ) -> tuple[dict[str, Any], sqlite3.Row, str]:
        snapshot = self._latest_snapshot(snapshot_id)
        if snapshot is None:
            raise NotFound("设施快照不存在")
        capacities = self._capacities(snapshot_id, int(snapshot["revision"]))
        reserved = self._reserved_amounts(exclude_request_ids)
        other_windows = self._window_holds(exclude_request_ids)
        pending = self._pending_demands(exclude_request_ids)
        evaluation = evaluate_impact(
            capacities=capacities,
            reserved=reserved,
            demands=demands,
            own_windows=own_windows,
            other_windows=other_windows,
            pending_demands=pending,
        )
        input_sha256 = digest({
            "snapshot_id": snapshot_id,
            "snapshot_revision": snapshot["revision"],
            "capacities": {
                key: {"kind": item.kind, "capacity": decimal_text(item.capacity)}
                for key, item in sorted(capacities.items())
            },
            "reserved": {key: decimal_text(value) for key, value in sorted(reserved.items())},
            "demands": [
                {"constraint_id": item.constraint_id, "amount": decimal_text(item.amount)} for item in demands
            ],
            "own_windows": [
                {
                    "window_id": item.window_id,
                    "vessel_id": item.vessel_id,
                    "starts_at": item.starts_at,
                    "ends_at": item.ends_at,
                }
                for item in own_windows
            ],
            "other_windows": [
                {
                    "request_id": item.request_id,
                    "window_id": item.window_id,
                    "vessel_id": item.vessel_id,
                    "starts_at": item.starts_at,
                    "ends_at": item.ends_at,
                    "blocking": item.blocking,
                }
                for item in other_windows
            ],
            "pending_demands": [
                {"request_id": item.request_id, "constraint_id": item.constraint_id, "amount": decimal_text(item.amount)}
                for item in pending
            ],
        })
        return evaluation, snapshot, input_sha256

    def _store_evaluation(
        self,
        request_id: str,
        purpose: str,
        snapshot: sqlite3.Row,
        input_sha256: str,
        evaluation: Mapping[str, Any],
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO change_evaluations(request_id,purpose,snapshot_id,snapshot_revision,input_sha256,"
            "outcome,constraints_json,conflicts_json,evaluated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                request_id,
                purpose,
                snapshot["snapshot_id"],
                snapshot["revision"],
                input_sha256,
                evaluation["outcome"],
                canonical_json(evaluation["constraints"]),
                canonical_json(evaluation["conflicts"]),
                self._now(),
            ),
        )
        return int(cursor.lastrowid)

    # ------------------------------------------------------------------
    # 变更申请
    # ------------------------------------------------------------------

    def _request_row(self, request_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM change_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise NotFound("变更申请不存在")
        return row

    @staticmethod
    def _check_guarantee_level(payload: ChangePayload, capacities: Mapping[str, ConstraintCapacity]) -> None:
        if payload.guarantee_level not in RESCUE_REQUIRED_LEVELS:
            return
        kinds = {
            capacities[item.constraint_id].kind
            for item in payload.access_requirements
            if item.constraint_id in capacities
        }
        if "rescue_coverage" not in kinds:
            raise ValidationFailed("提升或关键保障等级必须包含应急救援覆盖接入需求")

    def submit_request(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        payload = ChangePayload.from_dict(raw)
        permission = "change.revise" if payload.supersedes_request_id else "change.submit"
        self._require(actor_id, permission)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM change_idempotency "
            "WHERE scope='change-submit' AND idempotency_key=?",
            (payload.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同变更内容")
            return json.loads(stored["response_json"])
        original: sqlite3.Row | None = None
        root_id = payload.request_id
        design_revision = 1
        exclude_ids = {payload.request_id}
        if payload.supersedes_request_id is not None:
            original = self._request_row(payload.supersedes_request_id)
            child = self.connection.execute(
                "SELECT request_id FROM change_requests WHERE supersedes_request_id=?",
                (original["request_id"],),
            ).fetchone()
            if child is not None:
                raise Conflict("原申请已存在后续修订")
            root_id = original["root_request_id"]
            design_revision = int(original["design_revision"]) + 1
            exclude_ids.add(original["request_id"])
        snapshot = self._latest_snapshot(payload.snapshot_id)
        if snapshot is None:
            raise NotFound("设施快照不存在")
        capacities = self._capacities(payload.snapshot_id, int(snapshot["revision"]))
        self._check_guarantee_level(payload, capacities)
        demands = [Demand(item.constraint_id, item.amount) for item in payload.access_requirements]
        own_windows = [
            OwnWindow(item.window_id, item.vessel_id, item.starts_at, item.ends_at)
            for item in payload.construction_windows
        ]
        evaluation, snapshot, input_sha256 = self._run_evaluation(
            payload.snapshot_id, demands, own_windows, exclude_ids
        )
        content_sha256 = hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO change_requests(request_id,root_request_id,supersedes_request_id,design_revision,"
                    "title,snapshot_id,equipment_json,coordinates_json,commissioning_json,guarantee_level,"
                    "access_json,rollback_json,steps_json,content_sha256,state,revision,submitted_by,submitted_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'evaluated',1,?,?)",
                    (
                        payload.request_id,
                        root_id,
                        payload.supersedes_request_id,
                        design_revision,
                        payload.title,
                        payload.snapshot_id,
                        canonical_json(raw.get("equipment")),
                        canonical_json(raw.get("coordinates")),
                        canonical_json(raw.get("commissioning_curve")),
                        payload.guarantee_level,
                        canonical_json(raw.get("access_requirements")),
                        canonical_json(raw.get("rollback_plan")),
                        canonical_json(raw.get("execution_steps")),
                        content_sha256,
                        actor_id,
                        now,
                    ),
                )
                evaluation_id = self._store_evaluation(
                    payload.request_id, "submission", snapshot, input_sha256, evaluation
                )
                for window in payload.construction_windows:
                    self.connection.execute(
                        "INSERT INTO change_windows(request_id,window_id,vessel_id,starts_at,ends_at) "
                        "VALUES(?,?,?,?,?)",
                        (payload.request_id, window.window_id, window.vessel_id, window.starts_at, window.ends_at),
                    )
                for step in payload.execution_steps:
                    self.connection.execute(
                        "INSERT INTO change_steps(request_id,seq,description) VALUES(?,?,?)",
                        (payload.request_id, step.seq, step.description),
                    )
                if original is not None and original["state"] == "evaluated":
                    self.connection.execute(
                        "UPDATE change_requests SET state='superseded',revision=revision+1 "
                        "WHERE request_id=? AND state='evaluated'",
                        (original["request_id"],),
                    )
                    self._audit(
                        "change_request",
                        original["request_id"],
                        "change.superseded",
                        actor_id,
                        {"superseded_by": payload.request_id},
                    )
                response = {
                    "request_id": payload.request_id,
                    "root_request_id": root_id,
                    "design_revision": design_revision,
                    "state": "evaluated",
                    "revision": 1,
                    "evaluation": {
                        "evaluation_id": evaluation_id,
                        "purpose": "submission",
                        "snapshot_id": snapshot["snapshot_id"],
                        "snapshot_revision": snapshot["revision"],
                        "outcome": evaluation["outcome"],
                        "constraints": evaluation["constraints"],
                        "conflicts": evaluation["conflicts"],
                    },
                }
                self.connection.execute(
                    "INSERT INTO change_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('change-submit',?,?,?,?)",
                    (payload.idempotency_key, request_digest, canonical_json(response), now),
                )
                self._audit(
                    "change_request",
                    payload.request_id,
                    "change.submitted",
                    actor_id,
                    {
                        "design_revision": design_revision,
                        "supersedes_request_id": payload.supersedes_request_id,
                        "evaluation_id": evaluation_id,
                        "outcome": evaluation["outcome"],
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("变更编号或幂等键冲突") from exc
        return response

    def revise_request(self, actor_id: str, original_request_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        original = self._request_row(original_request_id)
        payload = dict(raw)
        payload["supersedes_request_id"] = original["request_id"]
        return self.submit_request(actor_id, payload)

    # ------------------------------------------------------------------
    # 整体审批
    # ------------------------------------------------------------------

    def approve_request(
        self,
        actor_id: str,
        request_id: str,
        expected_revision: int,
        rationale: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "change.approve")
        rationale = required_text(rationale, "rationale", 512)
        request = self._request_row(request_id)
        if request["state"] != "evaluated":
            raise InvalidState("变更不在待审批状态")
        if int(request["revision"]) != expected_revision:
            raise InvalidState("变更版本已变化，请重新读取后再审批")
        if request["submitted_by"] == actor_id:
            raise Forbidden("审批人不能是申请人")
        original: sqlite3.Row | None = None
        exclude_ids = {request_id}
        if request["supersedes_request_id"] is not None:
            original = self._request_row(request["supersedes_request_id"])
            if original["state"] in EXECUTING_STATES:
                raise InvalidState("原申请正在执行，需先完成、回退或人工接管")
            exclude_ids.add(original["request_id"])
        demands = [
            Demand(item["constraint_id"], Decimal(str(item["amount"])))
            for item in json.loads(request["access_json"])
        ]
        window_rows = self.connection.execute(
            "SELECT window_id,vessel_id,starts_at,ends_at FROM change_windows WHERE request_id=? ORDER BY window_id",
            (request_id,),
        ).fetchall()
        own_windows = [
            OwnWindow(row["window_id"], row["vessel_id"], row["starts_at"], row["ends_at"])
            for row in window_rows
        ]
        now = self._now()
        with transaction(self.connection, immediate=True):
            evaluation, snapshot, input_sha256 = self._run_evaluation(
                request["snapshot_id"], demands, own_windows, exclude_ids
            )
            evaluation_id = self._store_evaluation(request_id, "approval", snapshot, input_sha256, evaluation)
            if evaluation["outcome"] != "clear":
                raise Conflict("当前设施余量或施工窗口不满足，无法批准")
            for demand in demands:
                self.connection.execute(
                    "INSERT INTO change_reservations(request_id,constraint_id,amount,state,created_at) "
                    "VALUES(?,?,?,'held',?)",
                    (request_id, demand.constraint_id, decimal_text(quantize_amount(demand.amount)), now),
                )
            if original is not None:
                if original["state"] == "approved":
                    self.connection.execute(
                        "UPDATE change_reservations SET state='released',released_at=? "
                        "WHERE request_id=? AND state='held'",
                        (now, original["request_id"]),
                    )
                if original["state"] in ("approved", "evaluated"):
                    self.connection.execute(
                        "UPDATE change_requests SET state='superseded',revision=revision+1 "
                        "WHERE request_id=? AND state IN ('approved','evaluated')",
                        (original["request_id"],),
                    )
                    self._audit(
                        "change_request",
                        original["request_id"],
                        "change.superseded",
                        actor_id,
                        {"superseded_by": request_id},
                    )
            cursor = self.connection.execute(
                "UPDATE change_requests SET state='approved',decided_by=?,decided_at=?,decision_rationale=?,"
                "decision_evaluation_id=?,revision=revision+1 "
                "WHERE request_id=? AND state='evaluated' AND revision=?",
                (actor_id, now, rationale, evaluation_id, request_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("变更状态已变化，请重新读取后再审批")
            self._audit(
                "change_request",
                request_id,
                "change.approved",
                actor_id,
                {
                    "evaluation_id": evaluation_id,
                    "snapshot_id": snapshot["snapshot_id"],
                    "snapshot_revision": snapshot["revision"],
                },
            )
        return {
            "request_id": request_id,
            "state": "approved",
            "revision": expected_revision + 1,
            "evaluation_id": evaluation_id,
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_revision": snapshot["revision"],
        }

    def reject_request(self, actor_id: str, request_id: str, rationale: str) -> dict[str, Any]:
        self._require(actor_id, "change.approve")
        rationale = required_text(rationale, "rationale", 512)
        request = self._request_row(request_id)
        if request["state"] != "evaluated":
            raise InvalidState("变更不在待审批状态")
        if request["submitted_by"] == actor_id:
            raise Forbidden("审批人不能是申请人")
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE change_requests SET state='rejected',decided_by=?,decided_at=?,decision_rationale=?,"
                "closed_at=?,revision=revision+1 WHERE request_id=? AND state='evaluated'",
                (actor_id, now, rationale, now, request_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("变更状态已变化，请重新读取")
            self._audit("change_request", request_id, "change.rejected", actor_id, {"rationale": rationale})
        return {"request_id": request_id, "state": "rejected", "revision": int(request["revision"]) + 1}

    def withdraw_request(self, actor_id: str, request_id: str) -> dict[str, Any]:
        self._require(actor_id, "change.withdraw")
        request = self._request_row(request_id)
        if request["submitted_by"] != actor_id:
            raise Forbidden("只有申请人可以撤回变更")
        if request["state"] not in ("evaluated", "approved"):
            raise InvalidState("当前状态不可撤回")
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE change_requests SET state='withdrawn',closed_at=?,revision=revision+1 "
                "WHERE request_id=? AND state IN ('evaluated','approved')",
                (now, request_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("变更状态已变化，请重新读取")
            released = self.connection.execute(
                "UPDATE change_reservations SET state='released',released_at=? WHERE request_id=? AND state='held'",
                (now, request_id),
            ).rowcount
            self._audit(
                "change_request",
                request_id,
                "change.withdrawn",
                actor_id,
                {"released_reservations": released},
            )
        return {"request_id": request_id, "state": "withdrawn", "revision": int(request["revision"]) + 1}

    # ------------------------------------------------------------------
    # 现场回执、回退与人工接管
    # ------------------------------------------------------------------

    def report_receipt(
        self,
        actor_id: str,
        request_id: str,
        seq: int,
        outcome: str,
        note: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "receipt.write")
        if outcome not in ("succeeded", "failed"):
            raise ValidationFailed("outcome 必须是 succeeded 或 failed")
        note = required_text(note, "note", 512)
        key = identifier(idempotency_key, "idempotency_key")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq <= 0:
            raise ValidationFailed("seq 必须是正整数")
        request = self._request_row(request_id)
        request_digest = digest({
            "request_id": request_id,
            "seq": seq,
            "outcome": outcome,
            "note": note,
            "idempotency_key": key,
        })
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM change_idempotency "
            "WHERE scope='receipt' AND idempotency_key=?",
            (key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同回执内容")
            return json.loads(stored["response_json"])
        if request["state"] not in ("approved", "in_execution"):
            raise InvalidState("变更不在可回执状态")
        step = self.connection.execute(
            "SELECT * FROM change_steps WHERE request_id=? AND seq=?", (request_id, seq)
        ).fetchone()
        if step is None:
            raise NotFound("现场步骤不存在")
        if step["state"] != "pending":
            raise InvalidState("现场步骤已回执")
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE change_steps SET state=?,receipt_note=?,reported_by=?,reported_at=? "
                "WHERE request_id=? AND seq=? AND state='pending'",
                (outcome, note, actor_id, now, request_id, seq),
            )
            if cursor.rowcount != 1:
                raise InvalidState("现场步骤已回执")
            new_state = "in_execution"
            closed_at = None
            if outcome == "failed":
                new_state = "blocked"
            else:
                counts = {
                    row["state"]: row["n"]
                    for row in self.connection.execute(
                        "SELECT state,COUNT(*) AS n FROM change_steps WHERE request_id=? GROUP BY state",
                        (request_id,),
                    ).fetchall()
                }
                if counts.get("pending", 0) == 0 and counts.get("failed", 0) == 0:
                    new_state = "completed"
                    closed_at = now
            self.connection.execute(
                "UPDATE change_requests SET state=?,revision=revision+1,closed_at=COALESCE(?,closed_at) "
                "WHERE request_id=? AND state IN ('approved','in_execution')",
                (new_state, closed_at, request_id),
            )
            if new_state == "completed":
                self.connection.execute(
                    "UPDATE change_reservations SET state='consumed' WHERE request_id=? AND state='held'",
                    (request_id,),
                )
            response = {
                "request_id": request_id,
                "seq": seq,
                "step_state": outcome,
                "request_state": new_state,
                "revision": int(request["revision"]) + 1,
            }
            self.connection.execute(
                "INSERT INTO change_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                "VALUES('receipt',?,?,?,?)",
                (key, request_digest, canonical_json(response), now),
            )
            self._audit(
                "change_request",
                request_id,
                "receipt.reported",
                actor_id,
                {"seq": seq, "outcome": outcome, "request_state": new_state},
            )
        return response

    def execute_rollback(self, actor_id: str, request_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "rollback.execute")
        note = required_text(note, "note", 512)
        request = self._request_row(request_id)
        if request["state"] not in ("blocked", "manual_hold"):
            raise InvalidState("只有受阻或人工接管的变更可以回退")
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE change_requests SET state='rolled_back',closed_at=?,revision=revision+1 "
                "WHERE request_id=? AND state IN ('blocked','manual_hold')",
                (now, request_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("变更状态已变化，请重新读取")
            released = self.connection.execute(
                "UPDATE change_reservations SET state='released',released_at=? WHERE request_id=? AND state='held'",
                (now, request_id),
            ).rowcount
            self._audit(
                "change_request",
                request_id,
                "change.rolled_back",
                actor_id,
                {"note": note, "released_reservations": released},
            )
        return {
            "request_id": request_id,
            "state": "rolled_back",
            "released_reservations": released,
            "revision": int(request["revision"]) + 1,
        }

    def take_over(self, actor_id: str, request_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "takeover.execute")
        note = required_text(note, "note", 512)
        request = self._request_row(request_id)
        if request["state"] != "blocked":
            raise InvalidState("只有受阻变更可以人工接管")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE change_requests SET state='manual_hold',revision=revision+1 "
                "WHERE request_id=? AND state='blocked'",
                (request_id,),
            )
            if cursor.rowcount != 1:
                raise InvalidState("变更状态已变化，请重新读取")
            self._audit("change_request", request_id, "change.taken_over", actor_id, {"note": note})
        return {"request_id": request_id, "state": "manual_hold", "revision": int(request["revision"]) + 1}

    # ------------------------------------------------------------------
    # 查询端
    # ------------------------------------------------------------------

    def get_request(self, actor_id: str, request_id: str) -> dict[str, Any]:
        self._user(actor_id)
        request = self._request_row(request_id)
        return {
            "request_id": request["request_id"],
            "root_request_id": request["root_request_id"],
            "supersedes_request_id": request["supersedes_request_id"],
            "design_revision": request["design_revision"],
            "title": request["title"],
            "state": request["state"],
            "revision": request["revision"],
            "snapshot_id": request["snapshot_id"],
            "guarantee_level": request["guarantee_level"],
            "submitted_by": request["submitted_by"],
            "submitted_at": request["submitted_at"],
            "decided_by": request["decided_by"],
            "decided_at": request["decided_at"],
            "closed_at": request["closed_at"],
        }

    def explain_request(self, actor_id: str, request_id: str) -> dict[str, Any]:
        self._user(actor_id)
        request = self._request_row(request_id)
        evaluations = [
            {
                "evaluation_id": row["evaluation_id"],
                "purpose": row["purpose"],
                "snapshot_id": row["snapshot_id"],
                "snapshot_revision": row["snapshot_revision"],
                "input_sha256": row["input_sha256"],
                "outcome": row["outcome"],
                "constraints": json.loads(row["constraints_json"]),
                "conflicts": json.loads(row["conflicts_json"]),
                "evaluated_at": row["evaluated_at"],
            }
            for row in self.connection.execute(
                "SELECT * FROM change_evaluations WHERE request_id=? ORDER BY evaluation_id",
                (request_id,),
            ).fetchall()
        ]
        reservations = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM change_reservations WHERE request_id=? ORDER BY reservation_id",
                (request_id,),
            ).fetchall()
        ]
        steps = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM change_steps WHERE request_id=? ORDER BY seq", (request_id,)
            ).fetchall()
        ]
        windows = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM change_windows WHERE request_id=? ORDER BY window_id", (request_id,)
            ).fetchall()
        ]
        audit_trail = [
            {
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "event_hash": row["event_hash"],
            }
            for row in self.connection.execute(
                "SELECT * FROM change_audit_events WHERE entity_type='change_request' AND entity_id=? "
                "ORDER BY event_id",
                (request_id,),
            ).fetchall()
        ]
        decision = None
        if request["decided_by"] is not None:
            decision = {
                "decided_by": request["decided_by"],
                "decided_at": request["decided_at"],
                "rationale": request["decision_rationale"],
                "evaluation_id": request["decision_evaluation_id"],
            }
        return {
            "request": {
                **self.get_request(actor_id, request_id),
                "equipment": json.loads(request["equipment_json"]),
                "coordinates": json.loads(request["coordinates_json"]),
                "commissioning_curve": json.loads(request["commissioning_json"]),
                "access_requirements": json.loads(request["access_json"]),
                "rollback_plan": json.loads(request["rollback_json"]),
                "execution_steps": json.loads(request["steps_json"]),
                "content_sha256": request["content_sha256"],
            },
            "decision": decision,
            "evaluations": evaluations,
            "reservations": reservations,
            "steps": steps,
            "windows": windows,
            "audit_trail": audit_trail,
        }

    def request_history(self, actor_id: str, request_id: str) -> dict[str, Any]:
        self._user(actor_id)
        request = self._request_row(request_id)
        rows = self.connection.execute(
            "SELECT request_id,design_revision,supersedes_request_id,state,guarantee_level,"
            "submitted_by,submitted_at,decided_by,decided_at,closed_at "
            "FROM change_requests WHERE root_request_id=? ORDER BY design_revision",
            (request["root_request_id"],),
        ).fetchall()
        return {"root_request_id": request["root_request_id"], "revisions": [dict(row) for row in rows]}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM change_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
