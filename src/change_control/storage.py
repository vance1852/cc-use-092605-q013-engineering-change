"""工程变更服务的 SQLite 模式和事务辅助。"""

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
    role TEXT NOT NULL CHECK(role IN ('engineer','approver','operator','planner','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS facility_snapshots (
    snapshot_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES change_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, revision)
);

CREATE TABLE IF NOT EXISTS snapshot_constraints (
    snapshot_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    constraint_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('collector_line','substation_access','rescue_coverage')),
    capacity TEXT NOT NULL,
    unit TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, revision, constraint_id),
    FOREIGN KEY(snapshot_id, revision) REFERENCES facility_snapshots(snapshot_id, revision)
);

CREATE TABLE IF NOT EXISTS change_requests (
    request_id TEXT PRIMARY KEY,
    root_request_id TEXT NOT NULL,
    supersedes_request_id TEXT REFERENCES change_requests(request_id),
    design_revision INTEGER NOT NULL,
    title TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    equipment_json TEXT NOT NULL,
    coordinates_json TEXT NOT NULL,
    commissioning_json TEXT NOT NULL,
    guarantee_level TEXT NOT NULL CHECK(guarantee_level IN ('standard','elevated','critical')),
    access_json TEXT NOT NULL,
    rollback_json TEXT NOT NULL,
    steps_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'evaluated'
        CHECK(state IN ('evaluated','approved','rejected','withdrawn','superseded',
                        'in_execution','blocked','rolled_back','manual_hold','completed')),
    revision INTEGER NOT NULL DEFAULT 1,
    decided_by TEXT REFERENCES change_users(user_id),
    decided_at TEXT,
    decision_rationale TEXT,
    decision_evaluation_id INTEGER,
    closed_at TEXT,
    submitted_by TEXT NOT NULL REFERENCES change_users(user_id),
    submitted_at TEXT NOT NULL,
    UNIQUE(root_request_id, design_revision)
);

CREATE INDEX IF NOT EXISTS idx_change_requests_state
ON change_requests(state);

CREATE INDEX IF NOT EXISTS idx_change_requests_supersedes
ON change_requests(supersedes_request_id);

CREATE TABLE IF NOT EXISTS change_evaluations (
    evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES change_requests(request_id),
    purpose TEXT NOT NULL CHECK(purpose IN ('submission','approval')),
    snapshot_id TEXT NOT NULL,
    snapshot_revision INTEGER NOT NULL,
    input_sha256 TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('clear','conflicted')),
    constraints_json TEXT NOT NULL,
    conflicts_json TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_change_evaluations_request
ON change_evaluations(request_id, evaluation_id);

CREATE TABLE IF NOT EXISTS change_reservations (
    reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES change_requests(request_id),
    constraint_id TEXT NOT NULL,
    amount TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'held' CHECK(state IN ('held','consumed','released')),
    created_at TEXT NOT NULL,
    released_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_change_reservations_constraint
ON change_reservations(constraint_id, state);

CREATE TABLE IF NOT EXISTS change_windows (
    request_id TEXT NOT NULL REFERENCES change_requests(request_id),
    window_id TEXT NOT NULL,
    vessel_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    PRIMARY KEY(request_id, window_id)
);

CREATE INDEX IF NOT EXISTS idx_change_windows_vessel
ON change_windows(vessel_id, starts_at, ends_at);

CREATE TABLE IF NOT EXISTS change_steps (
    request_id TEXT NOT NULL REFERENCES change_requests(request_id),
    seq INTEGER NOT NULL,
    description TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','succeeded','failed')),
    receipt_note TEXT,
    reported_by TEXT REFERENCES change_users(user_id),
    reported_at TEXT,
    PRIMARY KEY(request_id, seq)
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
