"""海上工程变更申请的输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
POOL_KINDS = {"collector_line", "substation", "vessel_window", "rescue_coverage"}
ASSURANCE_LEVELS = {"A", "B", "C"}
RECEIPT_OUTCOMES = {"succeeded", "failed"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def required_list(value: object, field: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise ValidationFailed(f"{field} 必须是非空数组")
    return value


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationFailed(f"{field} 必须是对象")
    return value


@dataclass(frozen=True, slots=True)
class PoolRegistration:
    pool_id: str
    name: str
    kind: str
    unit: str
    capacity: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "PoolRegistration":
        kind = required_text(raw.get("kind"), "kind", 32)
        if kind not in POOL_KINDS:
            raise ValidationFailed("kind 必须是 collector_line、substation、vessel_window 或 rescue_coverage")
        return cls(
            pool_id=identifier(raw.get("pool_id"), "pool_id"),
            name=required_text(raw.get("name"), "name"),
            kind=kind,
            unit=required_text(raw.get("unit"), "unit", 16),
            capacity=decimal_value(raw.get("capacity"), "capacity", minimum=Decimal("0.001")),
        )


@dataclass(frozen=True, slots=True)
class EquipmentItem:
    equipment_id: str
    model: str
    version: str
    quantity: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EquipmentItem":
        quantity = raw.get("quantity", 1)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValidationFailed("设备 quantity 必须是正整数")
        return cls(
            equipment_id=identifier(raw.get("equipment_id"), "equipment_id"),
            model=required_text(raw.get("model"), "model", 64),
            version=required_text(raw.get("version"), "version", 64),
            quantity=quantity,
        )


@dataclass(frozen=True, slots=True)
class CoordinatePoint:
    point_id: str
    latitude: Decimal
    longitude: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CoordinatePoint":
        return cls(
            point_id=identifier(raw.get("point_id"), "point_id"),
            latitude=decimal_value(
                raw.get("latitude"), "latitude", minimum=Decimal("-90"), maximum=Decimal("90")
            ),
            longitude=decimal_value(
                raw.get("longitude"), "longitude", minimum=Decimal("-180"), maximum=Decimal("180")
            ),
        )


@dataclass(frozen=True, slots=True)
class CommissioningPoint:
    curve_date: str
    cumulative_mw: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CommissioningPoint":
        curve_date = required_text(raw.get("date"), "date", 10)
        try:
            curve_date = date.fromisoformat(curve_date).isoformat()
        except ValueError as exc:
            raise ValidationFailed("投运曲线 date 必须是 YYYY-MM-DD 日期") from exc
        return cls(
            curve_date=curve_date,
            cumulative_mw=decimal_value(
                raw.get("cumulative_mw"), "cumulative_mw", minimum=Decimal("0.001")
            ),
        )


@dataclass(frozen=True, slots=True)
class ConstructionWindow:
    window_id: str
    starts_at: str
    ends_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ConstructionWindow":
        starts_at = required_text(raw.get("starts_at"), "starts_at", 40)
        ends_at = required_text(raw.get("ends_at"), "ends_at", 40)
        try:
            start = parse_utc(starts_at, "starts_at")
            end = parse_utc(ends_at, "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end <= start:
            raise ValidationFailed("施工时段 ends_at 必须晚于 starts_at")
        return cls(
            window_id=identifier(raw.get("window_id"), "window_id"),
            starts_at=starts_at,
            ends_at=ends_at,
        )


@dataclass(frozen=True, slots=True)
class Demand:
    pool_id: str
    amount: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Demand":
        return cls(
            pool_id=identifier(raw.get("pool_id"), "pool_id"),
            amount=decimal_value(raw.get("amount"), "amount", minimum=Decimal("0.001")),
        )


@dataclass(frozen=True, slots=True)
class RollbackPlan:
    summary: str
    steps: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RollbackPlan":
        steps = required_list(raw.get("steps"), "撤回方案 steps")
        return cls(
            summary=required_text(raw.get("summary"), "撤回方案 summary"),
            steps=tuple(required_text(item, "撤回方案步骤", 256) for item in steps),
        )


@dataclass(frozen=True, slots=True)
class ExecutionStep:
    step_id: str
    description: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ExecutionStep":
        return cls(
            step_id=identifier(raw.get("step_id"), "step_id"),
            description=required_text(raw.get("description"), "description"),
        )


@dataclass(frozen=True, slots=True)
class ChangeSubmission:
    change_id: str
    title: str
    assurance_level: str
    equipment: tuple[EquipmentItem, ...]
    coordinates: tuple[CoordinatePoint, ...]
    commissioning_curve: tuple[CommissioningPoint, ...]
    demands: tuple[Demand, ...]
    construction_windows: tuple[ConstructionWindow, ...]
    rollback_plan: RollbackPlan
    execution_steps: tuple[ExecutionStep, ...]
    idempotency_key: str
    supersedes_change_id: str | None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ChangeSubmission":
        equipment = tuple(
            EquipmentItem.from_dict(_mapping(item, "equipment 元素"))
            for item in required_list(raw.get("equipment"), "equipment")
        )
        equipment_ids = [item.equipment_id for item in equipment]
        if len(set(equipment_ids)) != len(equipment_ids):
            raise ValidationFailed("设备清单 equipment_id 不能重复")
        coordinates = tuple(
            CoordinatePoint.from_dict(_mapping(item, "coordinates 元素"))
            for item in required_list(raw.get("coordinates"), "coordinates")
        )
        point_ids = [item.point_id for item in coordinates]
        if len(set(point_ids)) != len(point_ids):
            raise ValidationFailed("坐标 point_id 不能重复")
        curve = tuple(
            CommissioningPoint.from_dict(_mapping(item, "commissioning_curve 元素"))
            for item in required_list(raw.get("commissioning_curve"), "commissioning_curve")
        )
        ordered = sorted(curve, key=lambda item: item.curve_date)
        if [item.curve_date for item in ordered] != [item.curve_date for item in curve]:
            raise ValidationFailed("预计投运曲线必须按日期升序")
        if len({item.curve_date for item in curve}) != len(curve):
            raise ValidationFailed("预计投运曲线日期不能重复")
        for earlier, later in zip(curve, curve[1:]):
            if later.cumulative_mw < earlier.cumulative_mw:
                raise ValidationFailed("预计投运曲线累计容量不能下降")
        demands = tuple(
            Demand.from_dict(_mapping(item, "demands 元素"))
            for item in required_list(raw.get("demands"), "demands")
        )
        demand_pools = [item.pool_id for item in demands]
        if len(set(demand_pools)) != len(demand_pools):
            raise ValidationFailed("接入需求 pool_id 不能重复")
        windows = tuple(
            ConstructionWindow.from_dict(_mapping(item, "construction_windows 元素"))
            for item in required_list(raw.get("construction_windows"), "construction_windows")
        )
        window_ids = [item.window_id for item in windows]
        if len(set(window_ids)) != len(window_ids):
            raise ValidationFailed("施工时段 window_id 不能重复")
        steps = tuple(
            ExecutionStep.from_dict(_mapping(item, "execution_steps 元素"))
            for item in required_list(raw.get("execution_steps"), "execution_steps")
        )
        step_ids = [item.step_id for item in steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValidationFailed("现场步骤 step_id 不能重复")
        assurance_level = required_text(raw.get("assurance_level"), "assurance_level", 4).upper()
        if assurance_level not in ASSURANCE_LEVELS:
            raise ValidationFailed("assurance_level 必须是 A、B 或 C")
        supersedes = raw.get("supersedes_change_id")
        return cls(
            change_id=identifier(raw.get("change_id"), "change_id"),
            title=required_text(raw.get("title"), "title"),
            assurance_level=assurance_level,
            equipment=equipment,
            coordinates=coordinates,
            commissioning_curve=curve,
            demands=demands,
            construction_windows=windows,
            rollback_plan=RollbackPlan.from_dict(_mapping(raw.get("rollback_plan"), "rollback_plan")),
            execution_steps=steps,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
            supersedes_change_id=None if supersedes is None else identifier(supersedes, "supersedes_change_id"),
        )


@dataclass(frozen=True, slots=True)
class ReceiptInput:
    step_id: str
    outcome: str
    note: str
    receipt_ref: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ReceiptInput":
        outcome = required_text(raw.get("outcome"), "outcome", 16)
        if outcome not in RECEIPT_OUTCOMES:
            raise ValidationFailed("outcome 必须是 succeeded 或 failed")
        return cls(
            step_id=identifier(raw.get("step_id"), "step_id"),
            outcome=outcome,
            note=required_text(raw.get("note"), "note"),
            receipt_ref=identifier(raw.get("receipt_ref"), "receipt_ref"),
        )
