"""变更影响评估的确定性计算。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence


ZERO = Decimal("0")


def quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ConstraintCapacity:
    constraint_id: str
    kind: str
    capacity: Decimal
    unit: str


@dataclass(frozen=True, slots=True)
class Demand:
    constraint_id: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class OwnWindow:
    window_id: str
    vessel_id: str
    starts_at: str
    ends_at: str


@dataclass(frozen=True, slots=True)
class WindowHold:
    request_id: str
    window_id: str
    vessel_id: str
    starts_at: str
    ends_at: str
    blocking: bool


@dataclass(frozen=True, slots=True)
class PendingDemand:
    request_id: str
    constraint_id: str
    amount: Decimal


def windows_overlap(a_starts: str, a_ends: str, b_starts: str, b_ends: str) -> bool:
    return a_starts < b_ends and b_starts < a_ends


def evaluate_impact(
    *,
    capacities: Mapping[str, ConstraintCapacity],
    reserved: Mapping[str, Decimal],
    demands: Sequence[Demand],
    own_windows: Sequence[OwnWindow],
    other_windows: Sequence[WindowHold],
    pending_demands: Sequence[PendingDemand],
) -> dict[str, object]:
    """按设施快照计算每项约束剩余量，并列出与其他申请的冲突。"""
    constraint_rows: list[dict[str, object]] = []
    conflicts: list[dict[str, object]] = []
    demanded_ids = {demand.constraint_id for demand in demands}
    for demand in demands:
        capacity = capacities.get(demand.constraint_id)
        if capacity is None:
            conflicts.append({
                "type": "unknown_constraint",
                "constraint_id": demand.constraint_id,
                "blocking": True,
                "detail": f"接入需求引用的约束 {demand.constraint_id} 不在设施快照中",
            })
            continue
        used = reserved.get(demand.constraint_id, ZERO)
        remaining_before = quantize_amount(capacity.capacity - used)
        remaining_after = quantize_amount(remaining_before - demand.amount)
        sufficient = remaining_after >= ZERO
        constraint_rows.append({
            "constraint_id": demand.constraint_id,
            "kind": capacity.kind,
            "unit": capacity.unit,
            "capacity": decimal_text(quantize_amount(capacity.capacity)),
            "reserved": decimal_text(quantize_amount(used)),
            "remaining_before": decimal_text(remaining_before),
            "required": decimal_text(quantize_amount(demand.amount)),
            "remaining_after": decimal_text(remaining_after),
            "sufficient": sufficient,
        })
        if not sufficient:
            conflicts.append({
                "type": "capacity_exceeded",
                "constraint_id": demand.constraint_id,
                "blocking": True,
                "detail": (
                    f"约束 {demand.constraint_id} 剩余 {decimal_text(remaining_before)} "
                    f"低于需求 {decimal_text(quantize_amount(demand.amount))}"
                ),
            })
    for own in own_windows:
        for other in other_windows:
            if own.vessel_id != other.vessel_id:
                continue
            if not windows_overlap(own.starts_at, own.ends_at, other.starts_at, other.ends_at):
                continue
            conflicts.append({
                "type": "vessel_overlap",
                "vessel_id": own.vessel_id,
                "window_id": own.window_id,
                "other_request_id": other.request_id,
                "other_window_id": other.window_id,
                "blocking": other.blocking,
                "detail": f"施工船 {own.vessel_id} 的窗口与申请 {other.request_id} 的窗口 {other.window_id} 重叠",
            })
    seen_pending: set[tuple[str, str]] = set()
    for pending in pending_demands:
        if pending.constraint_id not in demanded_ids:
            continue
        key = (pending.request_id, pending.constraint_id)
        if key in seen_pending:
            continue
        seen_pending.add(key)
        conflicts.append({
            "type": "pending_competition",
            "constraint_id": pending.constraint_id,
            "other_request_id": pending.request_id,
            "blocking": False,
            "detail": f"待审批申请 {pending.request_id} 同时竞争约束 {pending.constraint_id}",
        })
    outcome = "conflicted" if any(conflict["blocking"] for conflict in conflicts) else "clear"
    return {"constraints": constraint_rows, "conflicts": conflicts, "outcome": outcome}
