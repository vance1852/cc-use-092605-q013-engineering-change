"""确定性的约束余量与冲突计算。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping, Sequence


ZERO = Decimal("0")
VESSEL_WINDOW_KIND = "vessel_window"


def quantize_amount(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def windows_overlap(
    left: tuple[str, str],
    right: tuple[str, str],
) -> bool:
    """两个半开施工时段 [start, end) 是否相交。"""

    left_start, left_end = _parse(left[0]), _parse(left[1])
    right_start, right_end = _parse(right[0]), _parse(right[1])
    return left_start < right_end and right_start < left_end


@dataclass(frozen=True, slots=True)
class PoolDemand:
    pool_id: str
    requested: Decimal


@dataclass(frozen=True, slots=True)
class ContendingLoad:
    """其他申请对某资源池的占用或在审需求。"""

    change_id: str
    pool_id: str
    amount: Decimal
    windows: tuple[tuple[str, str], ...]


def _time_bound(kind: str) -> bool:
    return kind == VESSEL_WINDOW_KIND


def _relevant(
    load: ContendingLoad,
    kind: str,
    own_windows: Sequence[tuple[str, str]],
) -> bool:
    if not _time_bound(kind):
        return True
    return any(windows_overlap(own, other) for own in own_windows for other in load.windows)


def assess_constraints(
    *,
    capacities: Mapping[str, Decimal],
    pool_kinds: Mapping[str, str],
    demands: Iterable[PoolDemand],
    held_by_others: Iterable[ContendingLoad],
    pending_by_others: Iterable[ContendingLoad],
    own_windows: Sequence[tuple[str, str]],
) -> dict[str, object]:
    """按快照容量计算每项约束的剩余量，并列出与其他申请的冲突对象。

    返回 {"fits": bool, "constraints": [...], "conflicts": [...]}；结果按
    pool_id、对方申请号排序，同一输入永远得到同一输出。
    """

    held = sorted(held_by_others, key=lambda item: (item.change_id, item.pool_id))
    pending = sorted(pending_by_others, key=lambda item: (item.change_id, item.pool_id))
    constraints: list[dict[str, object]] = []
    conflicts: list[dict[str, object]] = []
    fits = True
    for demand in sorted(demands, key=lambda item: item.pool_id):
        pool_id = demand.pool_id
        if pool_id not in capacities:
            raise ValueError(f"快照缺少资源池 {pool_id}")
        kind = pool_kinds[pool_id]
        relevant_held = [
            load for load in held
            if load.pool_id == pool_id and _relevant(load, kind, own_windows)
        ]
        held_total = sum((load.amount for load in relevant_held), ZERO)
        capacity = capacities[pool_id]
        remaining = quantize_amount(capacity - held_total)
        requested = quantize_amount(demand.requested)
        constraint_fits = requested <= remaining
        fits = fits and constraint_fits
        constraints.append({
            "pool_id": pool_id,
            "kind": kind,
            "capacity": decimal_text(quantize_amount(capacity)),
            "held_by_others": decimal_text(quantize_amount(held_total)),
            "remaining": decimal_text(remaining),
            "requested": decimal_text(requested),
            "fits": constraint_fits,
        })
        for load in relevant_held:
            conflicts.append({
                "other_change_id": load.change_id,
                "pool_id": pool_id,
                "conflict_kind": "held_reservation",
                "amount": decimal_text(quantize_amount(load.amount)),
            })
        for load in pending:
            if load.pool_id == pool_id and _relevant(load, kind, own_windows):
                conflicts.append({
                    "other_change_id": load.change_id,
                    "pool_id": pool_id,
                    "conflict_kind": "pending_request",
                    "amount": decimal_text(quantize_amount(load.amount)),
                })
    conflicts.sort(key=lambda item: (item["other_change_id"], item["pool_id"], item["conflict_kind"]))
    return {"fits": fits, "constraints": constraints, "conflicts": conflicts}


def capacity_shortfalls(
    *,
    capacities: Mapping[str, Decimal],
    pool_kinds: Mapping[str, str],
    demands: Iterable[PoolDemand],
    held_by_others: Iterable[ContendingLoad],
    own_windows: Sequence[tuple[str, str]],
) -> list[str]:
    """审批占定前的实时复核：返回余量不足的资源池编号列表。"""

    held = list(held_by_others)
    shortfalls: list[str] = []
    for demand in sorted(demands, key=lambda item: item.pool_id):
        pool_id = demand.pool_id
        if pool_id not in capacities:
            shortfalls.append(pool_id)
            continue
        kind = pool_kinds[pool_id]
        held_total = sum(
            (load.amount for load in held
             if load.pool_id == pool_id and _relevant(load, kind, own_windows)),
            ZERO,
        )
        if demand.requested > capacities[pool_id] - held_total:
            shortfalls.append(pool_id)
    return shortfalls
