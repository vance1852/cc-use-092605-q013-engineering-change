"""工程变更申请与设施快照的领域输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import parse_utc, utc_text
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
CONSTRAINT_KINDS = {"collector_line", "substation_access", "rescue_coverage"}
CONSTRAINT_UNITS = {"collector_line": "MW", "substation_access": "MW", "rescue_coverage": "slot"}
GUARANTEE_LEVELS = {"standard", "elevated", "critical"}
RESCUE_REQUIRED_LEVELS = {"elevated", "critical"}


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


def positive_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationFailed(f"{field} 必须是正整数")
    return value


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValidationFailed(f"{field} 必须是 YYYY-MM-DD 日期") from exc


def required_list(value: object, field: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise ValidationFailed(f"{field} 必须是非空数组")
    return value


@dataclass(frozen=True, slots=True)
class SnapshotConstraint:
    constraint_id: str
    kind: str
    capacity: Decimal
    unit: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SnapshotConstraint":
        kind = required_text(raw.get("kind"), "kind", 32)
        if kind not in CONSTRAINT_KINDS:
            raise ValidationFailed("kind 必须是 collector_line、substation_access 或 rescue_coverage")
        return cls(
            constraint_id=identifier(raw.get("constraint_id"), "constraint_id"),
            kind=kind,
            capacity=decimal_value(raw.get("capacity"), "capacity", minimum=Decimal("0.001")),
            unit=CONSTRAINT_UNITS[kind],
        )


def parse_snapshot_constraints(value: object) -> tuple[SnapshotConstraint, ...]:
    constraints = [SnapshotConstraint.from_dict(item) for item in required_list(value, "constraints")]
    ids = [item.constraint_id for item in constraints]
    if len(set(ids)) != len(ids):
        raise ValidationFailed("设施快照约束编号重复")
    return tuple(constraints)


@dataclass(frozen=True, slots=True)
class EquipmentItem:
    equipment_id: str
    model: str
    rated_mw: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "EquipmentItem":
        return cls(
            equipment_id=identifier(raw.get("equipment_id"), "equipment_id"),
            model=required_text(raw.get("model"), "model", 64),
            rated_mw=decimal_value(raw.get("rated_mw"), "rated_mw", minimum=Decimal("0.001")),
        )


@dataclass(frozen=True, slots=True)
class Coordinate:
    equipment_id: str
    latitude: Decimal
    longitude: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Coordinate":
        return cls(
            equipment_id=identifier(raw.get("equipment_id"), "equipment_id"),
            latitude=decimal_value(raw.get("latitude"), "latitude", minimum=Decimal("-90"), maximum=Decimal("90")),
            longitude=decimal_value(raw.get("longitude"), "longitude", minimum=Decimal("-180"), maximum=Decimal("180")),
        )


@dataclass(frozen=True, slots=True)
class CommissioningPoint:
    date: str
    cumulative_mw: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CommissioningPoint":
        return cls(
            date=date_text(raw.get("date"), "date"),
            cumulative_mw=decimal_value(raw.get("cumulative_mw"), "cumulative_mw", minimum=Decimal("0")),
        )


@dataclass(frozen=True, slots=True)
class AccessRequirement:
    constraint_id: str
    amount: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AccessRequirement":
        return cls(
            constraint_id=identifier(raw.get("constraint_id"), "constraint_id"),
            amount=decimal_value(raw.get("amount"), "amount", minimum=Decimal("0.001")),
        )


@dataclass(frozen=True, slots=True)
class ConstructionWindow:
    window_id: str
    vessel_id: str
    starts_at: str
    ends_at: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ConstructionWindow":
        try:
            start = parse_utc(required_text(raw.get("starts_at"), "starts_at", 40), "starts_at")
            end = parse_utc(required_text(raw.get("ends_at"), "ends_at", 40), "ends_at")
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        if end <= start:
            raise ValidationFailed("施工窗口 ends_at 必须晚于 starts_at")
        return cls(
            window_id=identifier(raw.get("window_id"), "window_id"),
            vessel_id=identifier(raw.get("vessel_id"), "vessel_id"),
            starts_at=utc_text(start),
            ends_at=utc_text(end),
        )


@dataclass(frozen=True, slots=True)
class PlanStep:
    seq: int
    description: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], field: str) -> "PlanStep":
        return cls(
            seq=positive_integer(raw.get("seq"), f"{field}.seq"),
            description=required_text(raw.get("description"), f"{field}.description", 512),
        )


@dataclass(frozen=True, slots=True)
class ChangePayload:
    request_id: str
    title: str
    snapshot_id: str
    equipment: tuple[EquipmentItem, ...]
    coordinates: tuple[Coordinate, ...]
    commissioning_curve: tuple[CommissioningPoint, ...]
    guarantee_level: str
    access_requirements: tuple[AccessRequirement, ...]
    construction_windows: tuple[ConstructionWindow, ...]
    execution_steps: tuple[PlanStep, ...]
    rollback_plan: tuple[PlanStep, ...]
    supersedes_request_id: str | None
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ChangePayload":
        if not isinstance(raw, Mapping):
            raise ValidationFailed("变更申请必须是 JSON 对象")
        equipment = tuple(EquipmentItem.from_dict(item) for item in required_list(raw.get("equipment"), "equipment"))
        equipment_ids = [item.equipment_id for item in equipment]
        if len(set(equipment_ids)) != len(equipment_ids):
            raise ValidationFailed("设备清单存在重复设备编号")
        coordinates = tuple(Coordinate.from_dict(item) for item in required_list(raw.get("coordinates"), "coordinates"))
        coordinate_ids = [item.equipment_id for item in coordinates]
        if len(set(coordinate_ids)) != len(coordinate_ids) or set(coordinate_ids) != set(equipment_ids):
            raise ValidationFailed("每台设备必须且只能有一条坐标")
        curve = tuple(
            CommissioningPoint.from_dict(item)
            for item in required_list(raw.get("commissioning_curve"), "commissioning_curve")
        )
        curve_dates = [point.date for point in curve]
        if len(set(curve_dates)) != len(curve_dates) or curve_dates != sorted(curve_dates):
            raise ValidationFailed("预计投运曲线日期必须严格递增")
        cumulative = [point.cumulative_mw for point in curve]
        if cumulative != sorted(cumulative):
            raise ValidationFailed("预计投运曲线累计容量不能下降")
        total_rated = sum((item.rated_mw for item in equipment), Decimal("0"))
        if cumulative[-1] != total_rated:
            raise ValidationFailed("预计投运曲线终点必须等于设备总额定容量")
        guarantee_level = required_text(raw.get("guarantee_level"), "guarantee_level", 16)
        if guarantee_level not in GUARANTEE_LEVELS:
            raise ValidationFailed("guarantee_level 必须是 standard、elevated 或 critical")
        access = tuple(
            AccessRequirement.from_dict(item)
            for item in required_list(raw.get("access_requirements"), "access_requirements")
        )
        constraint_ids = [item.constraint_id for item in access]
        if len(set(constraint_ids)) != len(constraint_ids):
            raise ValidationFailed("接入需求存在重复约束编号")
        windows = tuple(
            ConstructionWindow.from_dict(item)
            for item in required_list(raw.get("construction_windows"), "construction_windows")
        )
        window_ids = [window.window_id for window in windows]
        if len(set(window_ids)) != len(window_ids):
            raise ValidationFailed("施工窗口编号重复")
        by_vessel: dict[str, list[ConstructionWindow]] = {}
        for window in windows:
            by_vessel.setdefault(window.vessel_id, []).append(window)
        for vessel_windows in by_vessel.values():
            ordered = sorted(vessel_windows, key=lambda item: item.starts_at)
            for left, right in zip(ordered, ordered[1:]):
                if right.starts_at < left.ends_at:
                    raise ValidationFailed("同一施工船的施工窗口不能重叠")
        steps = tuple(
            PlanStep.from_dict(item, "execution_steps")
            for item in required_list(raw.get("execution_steps"), "execution_steps")
        )
        rollback = tuple(
            PlanStep.from_dict(item, "rollback_plan")
            for item in required_list(raw.get("rollback_plan"), "rollback_plan")
        )
        for plan, field in ((steps, "execution_steps"), (rollback, "rollback_plan")):
            seqs = [step.seq for step in plan]
            if len(set(seqs)) != len(seqs):
                raise ValidationFailed(f"{field} 步骤序号重复")
        supersedes = raw.get("supersedes_request_id")
        return cls(
            request_id=identifier(raw.get("request_id"), "request_id"),
            title=required_text(raw.get("title"), "title"),
            snapshot_id=identifier(raw.get("snapshot_id"), "snapshot_id"),
            equipment=equipment,
            coordinates=coordinates,
            commissioning_curve=curve,
            guarantee_level=guarantee_level,
            access_requirements=access,
            construction_windows=windows,
            execution_steps=steps,
            rollback_plan=rollback,
            supersedes_request_id=None if supersedes is None else identifier(supersedes, "supersedes_request_id"),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )
