"""变更流程服务的 SQLite 模式和事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS change_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('planner','engineer','field','approver','controller','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_pools (
    pool_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('collector_line','substation','vessel_window','rescue_coverage')),
    unit TEXT NOT NULL,
    capacity TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','suspended')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facility_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES change_users(user_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_entries (
    snapshot_id INTEGER NOT NULL REFERENCES facility_snapshots(snapshot_id),
    pool_id TEXT NOT NULL REFERENCES resource_pools(pool_id),
    capacity TEXT NOT NULL,
    pool_revision INTEGER NOT NULL,
    PRIMARY KEY(snapshot_id, pool_id)
);

CREATE TABLE IF NOT EXISTS change_requests (
    change_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    assurance_level TEXT NOT NULL CHECK(assurance_level IN ('A','B','C')),
    applicant_id TEXT NOT NULL REFERENCES change_users(user_id),
    state TEXT NOT NULL DEFAULT 'submitted'
        CHECK(state IN ('submitted','approved','in_progress','completed','failed',
                        'rejected','withdrawn','superseded','manual_control','rolled_back')),
    revision INTEGER NOT NULL DEFAULT 1,
    snapshot_id INTEGER NOT NULL REFERENCES facility_snapshots(snapshot_id),
    supersedes_change_id TEXT REFERENCES change_requests(change_id),
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_change_requests_supersedes
ON change_requests(supersedes_change_id);

CREATE TABLE IF NOT EXISTS change_equipment (
    change_id TEXT NOT NULL REFERENCES change_requests(change_id),
    equipment_id TEXT NOT NULL,
    model TEXT NOT NULL,
    version TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    PRIMARY KEY(change_id, equipment_id)
);

CREATE TABLE IF NOT EXISTS change_coordinates (
    change_id TEXT NOT NULL REFERENCES change_requests(change_id),
    point_id TEXT NOT NULL,
    latitude TEXT NOT NULL,
    longitude TEXT NOT NULL,
    PRIMARY KEY(change_id, point_id)
);

CREATE TABLE IF NOT EXISTS change_commissioning (
    change_id TEXT NOT NULL REFERENCES change_requests(change_id),
    curve_date TEXT NOT NULL,
    cumulative_mw TEXT NOT NULL,
    PRIMARY KEY(change_id, curve_date)
);

CREATE TABLE IF NOT EXISTS change_demands (
    change_id TEXT NOT NULL REFERENCES change_requests(change_id),
    pool_id TEXT NOT NULL REFERENCES resource_pools(pool_id),
    amount TEXT NOT NULL,
    PRIMARY KEY(change_id, pool_id)
);

CREATE TABLE IF NOT EXISTS change_windows (
    change_id TEXT NOT NULL REFERENCES change_requests(change_id),
    window_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    PRIMARY KEY(change_id, window_id)
);

CREATE TABLE IF NOT EXISTS change_rollback_plans (
    change_id TEXT PRIMARY KEY REFERENCES change_requests(change_id),
    summary TEXT NOT NULL,
    steps_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_steps (
    change_id TEXT NOT NULL REFERENCES change_requests(change_id),
    step_id TEXT NOT NULL,
    description TEXT NOT NULL,
    PRIMARY KEY(change_id, step_id)
);

CREATE TABLE IF NOT EXISTS assessments (
    assessment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id TEXT NOT NULL REFERENCES change_requests(change_id),
    snapshot_id INTEGER NOT NULL REFERENCES facility_snapshots(snapshot_id),
    fits INTEGER NOT NULL CHECK(fits IN (0,1)),
    computed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assessments_change
ON assessments(change_id, assessment_id);

CREATE TABLE IF NOT EXISTS assessment_constraints (
    assessment_id INTEGER NOT NULL REFERENCES assessments(assessment_id),
    pool_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    capacity TEXT NOT NULL,
    held_by_others TEXT NOT NULL,
    remaining TEXT NOT NULL,
    requested TEXT NOT NULL,
    fits INTEGER NOT NULL CHECK(fits IN (0,1)),
    PRIMARY KEY(assessment_id, pool_id)
);

CREATE TABLE IF NOT EXISTS assessment_conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_id INTEGER NOT NULL REFERENCES assessments(assessment_id),
    other_change_id TEXT NOT NULL,
    pool_id TEXT NOT NULL,
    conflict_kind TEXT NOT NULL CHECK(conflict_kind IN ('held_reservation','pending_request')),
    amount TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assessment_conflicts_assessment
ON assessment_conflicts(assessment_id, conflict_id);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id TEXT NOT NULL REFERENCES change_requests(change_id),
    pool_id TEXT NOT NULL REFERENCES resource_pools(pool_id),
    amount TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','released')),
    held_at TEXT NOT NULL,
    released_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_reservations_pool_state
ON reservations(pool_id, state);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id TEXT NOT NULL REFERENCES change_requests(change_id),
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected')),
    reason TEXT NOT NULL,
    snapshot_id INTEGER NOT NULL REFERENCES facility_snapshots(snapshot_id),
    assessment_id INTEGER NOT NULL REFERENCES assessments(assessment_id),
    actor_id TEXT NOT NULL REFERENCES change_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_change
ON decisions(change_id, decision_id);

CREATE TABLE IF NOT EXISTS step_receipts (
    change_id TEXT NOT NULL REFERENCES change_requests(change_id),
    step_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('succeeded','failed')),
    note TEXT NOT NULL,
    receipt_ref TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL REFERENCES change_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(change_id, step_id)
);

CREATE TABLE IF NOT EXISTS change_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS change_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_change_audit_entity
ON change_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
