"""资源台账、设施快照、变更申请、审批占定和现场回执的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .impact import (
    ContendingLoad,
    PoolDemand,
    assess_constraints,
    canonical_json,
    capacity_shortfalls,
    decimal_text,
    digest,
    quantize_amount,
)
from .models import ChangeSubmission, PoolRegistration, ReceiptInput
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"pool.write", "snapshot.write", "report.read"},
    "engineer": {"change.submit", "change.withdraw", "report.read"},
    "field": {"receipt.write", "report.read"},
    "approver": {"change.approve", "report.read"},
    "controller": {"change.rollback", "change.takeover", "report.read"},
    "auditor": {"report.read", "audit.read"},
}

SUPERSEDABLE_STATES = ("submitted", "approved", "in_progress", "failed", "rolled_back")
ROLLBACKABLE_STATES = ("approved", "in_progress", "failed", "manual_control")


class ChangeService:
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
    # 资源台账与设施快照
    # ------------------------------------------------------------------

    def register_pool(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "pool.write")
        pool = PoolRegistration.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO resource_pools(pool_id,name,kind,unit,capacity,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        pool.pool_id,
                        pool.name,
                        pool.kind,
                        pool.unit,
                        decimal_text(pool.capacity),
                        self._now(),
                    ),
                )
                self._audit("pool", pool.pool_id, "pool.registered", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("资源池编号已经存在") from exc
        return self.pool(pool.pool_id)

    def pool(self, pool_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM resource_pools WHERE pool_id=?", (pool_id,)
        ).fetchone()
        if row is None:
            raise NotFound("资源池不存在")
        return dict(row)

    def adjust_pool(
        self,
        actor_id: str,
        pool_id: str,
        capacity: object,
        expected_revision: int,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "pool.write")
        current = self.pool(pool_id)
        new_capacity = Decimal(str(capacity))
        if new_capacity <= 0:
            raise ValidationFailed("capacity 必须大于零")
        if not reason.strip():
            raise ValidationFailed("调整原因不能为空")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE resource_pools SET capacity=?,revision=revision+1 "
                "WHERE pool_id=? AND revision=?",
                (decimal_text(new_capacity), pool_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("资源池不是当前版本")
            self._audit(
                "pool",
                pool_id,
                "pool.adjusted",
                actor_id,
                {"from": current["capacity"], "to": decimal_text(new_capacity), "reason": reason.strip()},
            )
        return self.pool(pool_id)

    def _take_snapshot(self, reason: str, actor_id: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO facility_snapshots(reason,created_by,created_at) VALUES(?,?,?)",
            (reason, actor_id, self._now()),
        )
        snapshot_id = int(cursor.lastrowid)
        pools = self.connection.execute(
            "SELECT pool_id,capacity,revision FROM resource_pools ORDER BY pool_id"
        ).fetchall()
        for pool in pools:
            self.connection.execute(
                "INSERT INTO snapshot_entries(snapshot_id,pool_id,capacity,pool_revision) VALUES(?,?,?,?)",
                (snapshot_id, pool["pool_id"], pool["capacity"], pool["revision"]),
            )
        return snapshot_id

    def take_snapshot(self, actor_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "snapshot.write")
        if not reason.strip():
            raise ValidationFailed("快照原因不能为空")
        with transaction(self.connection, immediate=True):
            snapshot_id = self._take_snapshot(reason.strip(), actor_id)
            self._audit("snapshot", str(snapshot_id), "snapshot.taken", actor_id, {"reason": reason.strip()})
        return self.snapshot(actor_id, snapshot_id)

    def snapshot(self, actor_id: str, snapshot_id: int) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self.connection.execute(
            "SELECT * FROM facility_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise NotFound("设施快照不存在")
        entries = self.connection.execute(
            "SELECT pool_id,capacity,pool_revision FROM snapshot_entries WHERE snapshot_id=? ORDER BY pool_id",
            (snapshot_id,),
        ).fetchall()
        return {**dict(row), "entries": [dict(entry) for entry in entries]}

    def ledger_status(self, actor_id: str) -> dict[str, Any]:
        """资源台账整体视图：每个资源池的总量、已占定和剩余量。"""

        self._require(actor_id, "report.read")
        pools = self.connection.execute("SELECT * FROM resource_pools ORDER BY pool_id").fetchall()
        held_rows = self.connection.execute(
            "SELECT pool_id,change_id,amount FROM reservations WHERE state='held' "
            "ORDER BY pool_id,change_id"
        ).fetchall()
        held_by_pool: dict[str, list[sqlite3.Row]] = {}
        for row in held_rows:
            held_by_pool.setdefault(row["pool_id"], []).append(row)
        result: list[dict[str, Any]] = []
        for pool in pools:
            held_entries = held_by_pool.get(pool["pool_id"], [])
            held_total = sum((Decimal(row["amount"]) for row in held_entries), Decimal("0"))
            capacity = Decimal(pool["capacity"])
            result.append({
                "pool_id": pool["pool_id"],
                "kind": pool["kind"],
                "unit": pool["unit"],
                "state": pool["state"],
                "revision": pool["revision"],
                "capacity": decimal_text(capacity),
                "held": decimal_text(quantize_amount(held_total)),
                "remaining": decimal_text(quantize_amount(capacity - held_total)),
                "holding_changes": sorted({row["change_id"] for row in held_entries}),
            })
        return {"pools": result}

    # ------------------------------------------------------------------
    # 变更申请提交与影响核算
    # ------------------------------------------------------------------

    def _load_change(self, change_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM change_requests WHERE change_id=?", (change_id,)
        ).fetchone()
        if row is None:
            raise NotFound("变更申请不存在")
        return row

    def _windows_for(self, change_ids: Iterable[str]) -> dict[str, list[tuple[str, str]]]:
        ids = sorted(set(change_ids))
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"SELECT change_id,starts_at,ends_at FROM change_windows WHERE change_id IN ({marks}) "
            "ORDER BY change_id,window_id",
            ids,
        ).fetchall()
        result: dict[str, list[tuple[str, str]]] = {}
        for row in rows:
            result.setdefault(row["change_id"], []).append((row["starts_at"], row["ends_at"]))
        return result

    def _held_loads(self, exclude_change_id: str) -> list[ContendingLoad]:
        rows = self.connection.execute(
            "SELECT change_id,pool_id,amount FROM reservations WHERE state='held' AND change_id<>? "
            "ORDER BY change_id,pool_id",
            (exclude_change_id,),
        ).fetchall()
        windows = self._windows_for([row["change_id"] for row in rows])
        return [
            ContendingLoad(
                change_id=row["change_id"],
                pool_id=row["pool_id"],
                amount=Decimal(row["amount"]),
                windows=tuple(windows.get(row["change_id"], ())),
            )
            for row in rows
        ]

    def _pending_loads(self, exclude_change_id: str) -> list[ContendingLoad]:
        rows = self.connection.execute(
            "SELECT d.change_id,d.pool_id,d.amount FROM change_demands d "
            "JOIN change_requests c ON c.change_id=d.change_id "
            "WHERE c.state='submitted' AND d.change_id<>? ORDER BY d.change_id,d.pool_id",
            (exclude_change_id,),
        ).fetchall()
        windows = self._windows_for([row["change_id"] for row in rows])
        return [
            ContendingLoad(
                change_id=row["change_id"],
                pool_id=row["pool_id"],
                amount=Decimal(row["amount"]),
                windows=tuple(windows.get(row["change_id"], ())),
            )
            for row in rows
        ]

    def _compute_assessment(
        self,
        submission: ChangeSubmission,
        snapshot_id: int,
        pools: Mapping[str, sqlite3.Row],
    ) -> dict[str, Any]:
        entries = self.connection.execute(
            "SELECT pool_id,capacity FROM snapshot_entries WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchall()
        capacities = {row["pool_id"]: Decimal(row["capacity"]) for row in entries}
        pool_kinds = {pool_id: row["kind"] for pool_id, row in pools.items()}
        own_windows = [(w.starts_at, w.ends_at) for w in submission.construction_windows]
        return assess_constraints(
            capacities=capacities,
            pool_kinds=pool_kinds,
            demands=[PoolDemand(item.pool_id, item.amount) for item in submission.demands],
            held_by_others=self._held_loads(submission.change_id),
            pending_by_others=self._pending_loads(submission.change_id),
            own_windows=own_windows,
        )

    def _record_assessment(
        self, change_id: str, snapshot_id: int, assessment: Mapping[str, Any]
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO assessments(change_id,snapshot_id,fits,computed_at) VALUES(?,?,?,?)",
            (change_id, snapshot_id, 1 if assessment["fits"] else 0, self._now()),
        )
        assessment_id = int(cursor.lastrowid)
        for constraint in assessment["constraints"]:
            self.connection.execute(
                "INSERT INTO assessment_constraints(assessment_id,pool_id,kind,capacity,"
                "held_by_others,remaining,requested,fits) VALUES(?,?,?,?,?,?,?,?)",
                (
                    assessment_id,
                    constraint["pool_id"],
                    constraint["kind"],
                    constraint["capacity"],
                    constraint["held_by_others"],
                    constraint["remaining"],
                    constraint["requested"],
                    1 if constraint["fits"] else 0,
                ),
            )
        for conflict in assessment["conflicts"]:
            self.connection.execute(
                "INSERT INTO assessment_conflicts(assessment_id,other_change_id,pool_id,"
                "conflict_kind,amount) VALUES(?,?,?,?,?)",
                (
                    assessment_id,
                    conflict["other_change_id"],
                    conflict["pool_id"],
                    conflict["conflict_kind"],
                    conflict["amount"],
                ),
            )
        return assessment_id

    def submit_change(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "change.submit")
        submission = ChangeSubmission.from_dict(raw)
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM change_idempotency "
            "WHERE scope='change' AND idempotency_key=?",
            (submission.idempotency_key,),
        ).fetchone()
        if stored is not None:
            if stored["request_sha256"] != request_digest:
                raise Conflict("幂等键对应不同变更内容")
            return json.loads(stored["response_json"])
        pools: dict[str, sqlite3.Row] = {}
        for demand in submission.demands:
            row = self.connection.execute(
                "SELECT * FROM resource_pools WHERE pool_id=?", (demand.pool_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"资源池不存在: {demand.pool_id}")
            if row["state"] != "active":
                raise InvalidState(f"资源池已停用: {demand.pool_id}")
            pools[demand.pool_id] = row
        if submission.assurance_level == "A":
            kinds = {pools[item.pool_id]["kind"] for item in submission.demands}
            if "rescue_coverage" not in kinds:
                raise ValidationFailed("保障等级 A 的变更必须登记应急救援覆盖需求")
        superseded: sqlite3.Row | None = None
        if submission.supersedes_change_id is not None:
            superseded = self._load_change(submission.supersedes_change_id)
            if superseded["state"] not in SUPERSEDABLE_STATES:
                raise InvalidState("原申请当前状态不可修订")
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                snapshot_id = self._take_snapshot(f"变更提交 {submission.change_id}", actor_id)
                self.connection.execute(
                    "INSERT INTO change_requests(change_id,title,assurance_level,applicant_id,"
                    "snapshot_id,supersedes_change_id,idempotency_key,submitted_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        submission.change_id,
                        submission.title,
                        submission.assurance_level,
                        actor_id,
                        snapshot_id,
                        submission.supersedes_change_id,
                        submission.idempotency_key,
                        now,
                    ),
                )
                for item in submission.equipment:
                    self.connection.execute(
                        "INSERT INTO change_equipment(change_id,equipment_id,model,version,quantity) "
                        "VALUES(?,?,?,?,?)",
                        (submission.change_id, item.equipment_id, item.model, item.version, item.quantity),
                    )
                for point in submission.coordinates:
                    self.connection.execute(
                        "INSERT INTO change_coordinates(change_id,point_id,latitude,longitude) "
                        "VALUES(?,?,?,?)",
                        (
                            submission.change_id,
                            point.point_id,
                            decimal_text(point.latitude),
                            decimal_text(point.longitude),
                        ),
                    )
                for point in submission.commissioning_curve:
                    self.connection.execute(
                        "INSERT INTO change_commissioning(change_id,curve_date,cumulative_mw) "
                        "VALUES(?,?,?)",
                        (submission.change_id, point.curve_date, decimal_text(point.cumulative_mw)),
                    )
                for demand in submission.demands:
                    self.connection.execute(
                        "INSERT INTO change_demands(change_id,pool_id,amount) VALUES(?,?,?)",
                        (submission.change_id, demand.pool_id, decimal_text(demand.amount)),
                    )
                for window in submission.construction_windows:
                    self.connection.execute(
                        "INSERT INTO change_windows(change_id,window_id,starts_at,ends_at) "
                        "VALUES(?,?,?,?)",
                        (submission.change_id, window.window_id, window.starts_at, window.ends_at),
                    )
                self.connection.execute(
                    "INSERT INTO change_rollback_plans(change_id,summary,steps_json) VALUES(?,?,?)",
                    (
                        submission.change_id,
                        submission.rollback_plan.summary,
                        canonical_json(list(submission.rollback_plan.steps)),
                    ),
                )
                for step in submission.execution_steps:
                    self.connection.execute(
                        "INSERT INTO change_steps(change_id,step_id,description) VALUES(?,?,?)",
                        (submission.change_id, step.step_id, step.description),
                    )
                if superseded is not None:
                    marks = ",".join("?" for _ in SUPERSEDABLE_STATES)
                    cursor = self.connection.execute(
                        f"UPDATE change_requests SET state='superseded',revision=revision+1 "
                        f"WHERE change_id=? AND state IN ({marks})",
                        (submission.supersedes_change_id, *SUPERSEDABLE_STATES),
                    )
                    if cursor.rowcount != 1:
                        raise InvalidState("原申请当前状态不可修订")
                    self.connection.execute(
                        "UPDATE reservations SET state='released',released_at=? "
                        "WHERE change_id=? AND state='held'",
                        (now, submission.supersedes_change_id),
                    )
                    self._audit(
                        "change",
                        submission.supersedes_change_id,
                        "change.superseded",
                        actor_id,
                        {"successor_change_id": submission.change_id},
                    )
                assessment = self._compute_assessment(submission, snapshot_id, pools)
                assessment_id = self._record_assessment(submission.change_id, snapshot_id, assessment)
                response = {
                    "change_id": submission.change_id,
                    "state": "submitted",
                    "revision": 1,
                    "snapshot_id": snapshot_id,
                    "assessment": {"assessment_id": assessment_id, **assessment},
                }
                self.connection.execute(
                    "INSERT INTO change_idempotency(scope,idempotency_key,request_sha256,response_json,"
                    "created_at) VALUES('change',?,?,?,?)",
                    (submission.idempotency_key, request_digest, canonical_json(response), now),
                )
                self._audit(
                    "change",
                    submission.change_id,
                    "change.submitted",
                    actor_id,
                    {
                        "snapshot_id": snapshot_id,
                        "fits": assessment["fits"],
                        "supersedes_change_id": submission.supersedes_change_id,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("变更编号或幂等键冲突") from exc
        return response

    # ------------------------------------------------------------------
    # 审批：施工时段、资源预留和撤回方案作为整体由非申请人审批
    # ------------------------------------------------------------------

    def _latest_assessment_id(self, change_id: str) -> int:
        row = self.connection.execute(
            "SELECT assessment_id FROM assessments WHERE change_id=? ORDER BY assessment_id DESC LIMIT 1",
            (change_id,),
        ).fetchone()
        if row is None:
            raise InvalidState("变更缺少影响核算")
        return int(row["assessment_id"])

    def _decide(
        self,
        actor_id: str,
        change_id: str,
        expected_revision: int,
    ) -> sqlite3.Row:
        change = self._load_change(change_id)
        if change["applicant_id"] == actor_id:
            raise Forbidden("审批人不能是申请人")
        if change["state"] != "submitted" or change["revision"] != expected_revision:
            raise InvalidState("变更申请不是当前待审批版本")
        return change

    def approve_change(
        self,
        actor_id: str,
        change_id: str,
        expected_revision: int,
        reason: str = "",
    ) -> dict[str, Any]:
        self._require(actor_id, "change.approve")
        change = self._decide(actor_id, change_id, expected_revision)
        demands = self.connection.execute(
            "SELECT pool_id,amount FROM change_demands WHERE change_id=? ORDER BY pool_id",
            (change_id,),
        ).fetchall()
        window_rows = self.connection.execute(
            "SELECT starts_at,ends_at FROM change_windows WHERE change_id=? ORDER BY window_id",
            (change_id,),
        ).fetchall()
        own_windows = [(row["starts_at"], row["ends_at"]) for row in window_rows]
        now = self._now()
        with transaction(self.connection, immediate=True):
            pools: dict[str, sqlite3.Row] = {}
            for demand in demands:
                pool = self.connection.execute(
                    "SELECT * FROM resource_pools WHERE pool_id=?", (demand["pool_id"],)
                ).fetchone()
                if pool is None or pool["state"] != "active":
                    raise Conflict(f"资源池不可用: {demand['pool_id']}")
                pools[demand["pool_id"]] = pool
            shortfalls = capacity_shortfalls(
                capacities={pool_id: Decimal(row["capacity"]) for pool_id, row in pools.items()},
                pool_kinds={pool_id: row["kind"] for pool_id, row in pools.items()},
                demands=[PoolDemand(row["pool_id"], Decimal(row["amount"])) for row in demands],
                held_by_others=self._held_loads(change_id),
                own_windows=own_windows,
            )
            if shortfalls:
                raise Conflict(f"资源池余量不足: {','.join(shortfalls)}")
            for demand in demands:
                self.connection.execute(
                    "INSERT INTO reservations(change_id,pool_id,amount,held_at) VALUES(?,?,?,?)",
                    (change_id, demand["pool_id"], demand["amount"], now),
                )
            assessment_id = self._latest_assessment_id(change_id)
            self.connection.execute(
                "INSERT INTO decisions(change_id,decision,reason,snapshot_id,assessment_id,actor_id,"
                "created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    change_id,
                    "approved",
                    reason.strip() or "同意",
                    change["snapshot_id"],
                    assessment_id,
                    actor_id,
                    now,
                ),
            )
            cursor = self.connection.execute(
                "UPDATE change_requests SET state='approved',revision=revision+1 "
                "WHERE change_id=? AND state='submitted' AND revision=?",
                (change_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("变更申请不是当前待审批版本")
            self._audit(
                "change",
                change_id,
                "change.approved",
                actor_id,
                {"snapshot_id": change["snapshot_id"], "assessment_id": assessment_id},
            )
        return {
            "change_id": change_id,
            "state": "approved",
            "revision": expected_revision + 1,
            "reservations": [
                {"pool_id": row["pool_id"], "amount": row["amount"], "state": "held"}
                for row in demands
            ],
        }

    def reject_change(
        self,
        actor_id: str,
        change_id: str,
        expected_revision: int,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "change.approve")
        if not reason.strip():
            raise ValidationFailed("驳回原因不能为空")
        change = self._decide(actor_id, change_id, expected_revision)
        with transaction(self.connection, immediate=True):
            assessment_id = self._latest_assessment_id(change_id)
            self.connection.execute(
                "INSERT INTO decisions(change_id,decision,reason,snapshot_id,assessment_id,actor_id,"
                "created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    change_id,
                    "rejected",
                    reason.strip(),
                    change["snapshot_id"],
                    assessment_id,
                    actor_id,
                    self._now(),
                ),
            )
            cursor = self.connection.execute(
                "UPDATE change_requests SET state='rejected',revision=revision+1 "
                "WHERE change_id=? AND state='submitted' AND revision=?",
                (change_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("变更申请不是当前待审批版本")
            self._audit("change", change_id, "change.rejected", actor_id, {"reason": reason.strip()})
        return {"change_id": change_id, "state": "rejected", "revision": expected_revision + 1}

    def withdraw_change(
        self, actor_id: str, change_id: str, expected_revision: int
    ) -> dict[str, Any]:
        self._require(actor_id, "change.withdraw")
        change = self._load_change(change_id)
        if change["applicant_id"] != actor_id:
            raise Forbidden("只有申请人可以撤回变更")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE change_requests SET state='withdrawn',revision=revision+1 "
                "WHERE change_id=? AND state='submitted' AND revision=?",
                (change_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("变更申请不是当前可撤回版本")
            self._audit("change", change_id, "change.withdrawn", actor_id, {})
        return {"change_id": change_id, "state": "withdrawn", "revision": expected_revision + 1}

    # ------------------------------------------------------------------
    # 现场回执、回退与人工接管
    # ------------------------------------------------------------------

    def record_receipt(
        self, actor_id: str, change_id: str, raw: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(actor_id, "receipt.write")
        receipt = ReceiptInput.from_dict(raw)
        change = self._load_change(change_id)
        if change["state"] not in ("approved", "in_progress"):
            raise InvalidState("变更当前状态不接受现场回执")
        step = self.connection.execute(
            "SELECT step_id FROM change_steps WHERE change_id=? AND step_id=?",
            (change_id, receipt.step_id),
        ).fetchone()
        if step is None:
            raise NotFound("现场步骤不存在")
        if self.connection.execute(
            "SELECT 1 FROM step_receipts WHERE receipt_ref=?", (receipt.receipt_ref,)
        ).fetchone() is not None:
            raise Conflict("回执编号已使用")
        if self.connection.execute(
            "SELECT 1 FROM step_receipts WHERE change_id=? AND step_id=?",
            (change_id, receipt.step_id),
        ).fetchone() is not None:
            raise Conflict("现场步骤已有回执")
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO step_receipts(change_id,step_id,outcome,note,receipt_ref,actor_id,"
                "created_at) VALUES(?,?,?,?,?,?,?)",
                (change_id, receipt.step_id, receipt.outcome, receipt.note, receipt.receipt_ref, actor_id, now),
            )
            if receipt.outcome == "failed":
                new_state = "failed"
            else:
                remaining = self.connection.execute(
                    "SELECT step_id FROM change_steps WHERE change_id=? EXCEPT "
                    "SELECT step_id FROM step_receipts WHERE change_id=?",
                    (change_id, change_id),
                ).fetchall()
                new_state = "completed" if not remaining else "in_progress"
            cursor = self.connection.execute(
                "UPDATE change_requests SET state=?,revision=revision+1 "
                "WHERE change_id=? AND state IN ('approved','in_progress')",
                (new_state, change_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("变更当前状态不接受现场回执")
            self._audit(
                "change",
                change_id,
                f"receipt.{receipt.outcome}",
                actor_id,
                {"step_id": receipt.step_id, "receipt_ref": receipt.receipt_ref, "state": new_state},
            )
        return {
            "change_id": change_id,
            "step_id": receipt.step_id,
            "outcome": receipt.outcome,
            "state": new_state,
            "revision": change["revision"] + 1,
        }

    def rollback_change(
        self,
        actor_id: str,
        change_id: str,
        expected_revision: int,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "change.rollback")
        if not reason.strip():
            raise ValidationFailed("回退原因不能为空")
        change = self._load_change(change_id)
        if change["state"] not in ROLLBACKABLE_STATES or change["revision"] != expected_revision:
            raise InvalidState("变更当前状态不可回退")
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE reservations SET state='released',released_at=? "
                "WHERE change_id=? AND state='held'",
                (now, change_id),
            )
            released = cursor.rowcount
            cursor = self.connection.execute(
                "UPDATE change_requests SET state='rolled_back',revision=revision+1 "
                "WHERE change_id=? AND revision=?",
                (change_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("变更当前状态不可回退")
            self._audit(
                "change",
                change_id,
                "change.rolled_back",
                actor_id,
                {"reason": reason.strip(), "released_reservations": released},
            )
        return {
            "change_id": change_id,
            "state": "rolled_back",
            "revision": expected_revision + 1,
            "released_reservations": released,
        }

    def takeover_change(
        self,
        actor_id: str,
        change_id: str,
        expected_revision: int,
        reason: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "change.takeover")
        if not reason.strip():
            raise ValidationFailed("接管原因不能为空")
        change = self._load_change(change_id)
        if change["state"] != "failed" or change["revision"] != expected_revision:
            raise InvalidState("只有失败的变更可以人工接管")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE change_requests SET state='manual_control',revision=revision+1 "
                "WHERE change_id=? AND state='failed' AND revision=?",
                (change_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("只有失败的变更可以人工接管")
            self._audit(
                "change",
                change_id,
                "change.manual_control",
                actor_id,
                {"reason": reason.strip(), "reservations": "held"},
            )
        return {"change_id": change_id, "state": "manual_control", "revision": expected_revision + 1}

    # ------------------------------------------------------------------
    # 查询端：说明每次决定依赖的快照、约束和冲突对象
    # ------------------------------------------------------------------

    def get_change(self, actor_id: str, change_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        change = self._load_change(change_id)
        equipment = self.connection.execute(
            "SELECT equipment_id,model,version,quantity FROM change_equipment WHERE change_id=? "
            "ORDER BY equipment_id",
            (change_id,),
        ).fetchall()
        coordinates = self.connection.execute(
            "SELECT point_id,latitude,longitude FROM change_coordinates WHERE change_id=? "
            "ORDER BY point_id",
            (change_id,),
        ).fetchall()
        curve = self.connection.execute(
            "SELECT curve_date,cumulative_mw FROM change_commissioning WHERE change_id=? "
            "ORDER BY curve_date",
            (change_id,),
        ).fetchall()
        demands = self.connection.execute(
            "SELECT pool_id,amount FROM change_demands WHERE change_id=? ORDER BY pool_id",
            (change_id,),
        ).fetchall()
        windows = self.connection.execute(
            "SELECT window_id,starts_at,ends_at FROM change_windows WHERE change_id=? ORDER BY window_id",
            (change_id,),
        ).fetchall()
        rollback = self.connection.execute(
            "SELECT summary,steps_json FROM change_rollback_plans WHERE change_id=?",
            (change_id,),
        ).fetchone()
        steps = self.connection.execute(
            "SELECT step_id,description FROM change_steps WHERE change_id=? ORDER BY step_id",
            (change_id,),
        ).fetchall()
        return {
            **dict(change),
            "equipment": [dict(row) for row in equipment],
            "coordinates": [dict(row) for row in coordinates],
            "commissioning_curve": [dict(row) for row in curve],
            "demands": [dict(row) for row in demands],
            "construction_windows": [dict(row) for row in windows],
            "rollback_plan": {
                "summary": rollback["summary"],
                "steps": json.loads(rollback["steps_json"]),
            },
            "execution_steps": [dict(row) for row in steps],
        }

    def change_lineage(self, actor_id: str, change_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        self._load_change(change_id)
        rows = self.connection.execute(
            "SELECT change_id,state,revision,supersedes_change_id,applicant_id,submitted_at "
            "FROM change_requests ORDER BY submitted_at,change_id"
        ).fetchall()
        by_id = {row["change_id"]: row for row in rows}
        root_id = change_id
        while by_id[root_id]["supersedes_change_id"] is not None:
            root_id = by_id[root_id]["supersedes_change_id"]
        chain: list[dict[str, Any]] = []
        current_id: str | None = root_id
        while current_id is not None:
            row = by_id[current_id]
            chain.append({
                "change_id": row["change_id"],
                "state": row["state"],
                "revision": row["revision"],
                "supersedes_change_id": row["supersedes_change_id"],
                "applicant_id": row["applicant_id"],
                "submitted_at": row["submitted_at"],
            })
            successors = [
                item for item in rows if item["supersedes_change_id"] == current_id
            ]
            current_id = successors[0]["change_id"] if successors else None
        return {"change_id": change_id, "root_change_id": root_id, "chain": chain}

    def explain_change(self, actor_id: str, change_id: str) -> dict[str, Any]:
        """还原一次变更决定的依据：设施快照、每项约束余量和冲突对象。"""

        self._require(actor_id, "report.read")
        change = self._load_change(change_id)
        snapshot = self.snapshot(actor_id, int(change["snapshot_id"]))
        assessment_row = self.connection.execute(
            "SELECT * FROM assessments WHERE change_id=? ORDER BY assessment_id DESC LIMIT 1",
            (change_id,),
        ).fetchone()
        assessment: dict[str, Any] | None = None
        if assessment_row is not None:
            constraints = self.connection.execute(
                "SELECT pool_id,kind,capacity,held_by_others,remaining,requested,fits "
                "FROM assessment_constraints WHERE assessment_id=? ORDER BY pool_id",
                (assessment_row["assessment_id"],),
            ).fetchall()
            conflicts = self.connection.execute(
                "SELECT other_change_id,pool_id,conflict_kind,amount FROM assessment_conflicts "
                "WHERE assessment_id=? ORDER BY other_change_id,pool_id,conflict_kind",
                (assessment_row["assessment_id"],),
            ).fetchall()
            assessment = {
                "assessment_id": assessment_row["assessment_id"],
                "snapshot_id": assessment_row["snapshot_id"],
                "fits": bool(assessment_row["fits"]),
                "computed_at": assessment_row["computed_at"],
                "constraints": [
                    {**dict(row), "fits": bool(row["fits"])} for row in constraints
                ],
                "conflicts": [dict(row) for row in conflicts],
            }
        decisions = self.connection.execute(
            "SELECT decision,reason,snapshot_id,assessment_id,actor_id,created_at FROM decisions "
            "WHERE change_id=? ORDER BY decision_id",
            (change_id,),
        ).fetchall()
        reservations = self.connection.execute(
            "SELECT pool_id,amount,state,held_at,released_at FROM reservations "
            "WHERE change_id=? ORDER BY pool_id",
            (change_id,),
        ).fetchall()
        receipts = self.connection.execute(
            "SELECT step_id,outcome,note,receipt_ref,actor_id,created_at FROM step_receipts "
            "WHERE change_id=? ORDER BY created_at,step_id",
            (change_id,),
        ).fetchall()
        return {
            "change_id": change_id,
            "state": change["state"],
            "revision": change["revision"],
            "applicant_id": change["applicant_id"],
            "assurance_level": change["assurance_level"],
            "snapshot": snapshot,
            "assessment": assessment,
            "decisions": [dict(row) for row in decisions],
            "reservations": [dict(row) for row in reservations],
            "receipts": [dict(row) for row in receipts],
            "lineage": self.change_lineage(actor_id, change_id)["chain"],
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM change_audit_events ORDER BY event_id"
        ).fetchall()
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
