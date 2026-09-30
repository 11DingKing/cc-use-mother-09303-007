"""领域模型：批次、院校、容量、保障、申报、试算结果与额度分录。

状态码与领域契约 ``domain/contract.json`` 中的五个状态一一对应，
``CLOSED`` 是递补结束后的归档状态。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Dimension(str, Enum):
    """多维配额的三个保障维度。"""

    COUNTRY = "country"
    INSTITUTION_TYPE = "type"
    MAJOR = "major"


class BatchStatus(str, Enum):
    DRAFT = "申报"
    TRIAL = "试算"
    PUBLISHED = "分配"
    CONFIRMING = "确认"
    BACKFILLING = "递补"
    CLOSED = "归档"


# 方案发布前允许编辑输入的状态
INPUT_OPEN_STATUSES = {BatchStatus.DRAFT, BatchStatus.TRIAL}
# 名额生命周期开始（额度分录生效）后的状态
LIVE_STATUSES = {
    BatchStatus.PUBLISHED,
    BatchStatus.CONFIRMING,
    BatchStatus.BACKFILLING,
}


class LineStatus(str, Enum):
    SELECTED = "入选"
    PARTIAL = "部分入选"
    WAITLISTED = "候补"
    REJECTED = "未入选"


class EntrySource(str, Enum):
    INITIAL = "initial"                    # 方案发布的初始额度
    GUARANTEE = "guarantee"                # 最低保障预留（明细说明用）
    DECLINE_RELEASE = "decline_release"    # 院校放弃释放
    REVOKE_RELEASE = "revoke_release"      # 资格撤销释放
    TRANSFER_OUT = "transfer_out"          # 机构间转让划出
    TRANSFER_IN = "transfer_in"            # 机构间转让划入
    WAITLIST_PROMOTION = "waitlist_promotion"  # 候补递补
    ROUNDING = "rounding"                  # 舍入尾差调整
    VACANT_RECOVERY = "vacant_recovery"    # 无合格承接对象，尾差/名额回收


@dataclass
class Institution:
    id: str
    name: str
    country: str
    institution_type: str
    majors: list[str]
    eligible: bool = True
    ineligible_reason: str = ""
    # 基础评分（办学需求、项目匹配度等，由主办方在申报阶段录入）
    base_score: float = 0.0
    # 历史参与修正分：历史参与越多，本次综合分扣减越多
    history_score: float = 0.0
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Institution":
        return cls(**data)


@dataclass
class Application:
    """院校的一条专业方向申报（需求行）。"""

    id: str
    institution_id: str
    major: str
    seats: int
    note: str = ""
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Application":
        return cls(**data)


@dataclass
class CapacityRule:
    """单维度容量，例如 country=泰国 12 人、type=小型院校 20 人、major=护理 8 人。"""

    dimension: str
    dim_value: str
    seats: int

    def key(self) -> tuple[str, str]:
        return self.dimension, self.dim_value

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CapacityRule":
        return cls(**data)


@dataclass
class Guarantee:
    """最低保障：匹配该维度的合格院校，每条申报至少保障 ``min_seats`` 个名额。"""

    id: str
    dimension: str
    dim_value: str
    min_seats: int
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Guarantee":
        return cls(**data)


@dataclass
class SeatTrace:
    """单个名额的可解释去向，尾差名额必须带顺位与余数说明。"""

    index: int
    source: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResultLine:
    """试算/正式结果中一所院校一个专业方向的完整结论。"""

    application_id: str
    institution_id: str
    institution_name: str
    major: str
    requested: int
    weight: float                       # 历史修正后的综合分
    base_score: float
    history_penalty: float
    target: float                       # 按权重应得的理论份额
    guaranteed: int                     # 其中因最低保障得到的名额
    allocated: int
    status: str
    waitlist_rank: int | None = None
    reasons: list[str] = field(default_factory=list)
    traces: list[SeatTrace] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["traces"] = [t.to_dict() for t in self.traces]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResultLine":
        data = dict(data)
        data["traces"] = [SeatTrace(**t) for t in data.get("traces", [])]
        return cls(**data)


@dataclass
class Shortfall:
    """保障无法满足时的缺口说明（容量配置本身不可行）。"""

    guarantee_id: str
    dimension: str
    dim_value: str
    institution_id: str
    missing: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TailRecord:
    """舍入尾差/无法承接名额的可解释去向。"""

    seats: int
    holder: str           # 承接对象标识：vacant_recovery（机动名额池）等
    holder_name: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TrialResult:
    lines: list[ResultLine]
    shortfalls: list[Shortfall]
    tail_records: list[TailRecord]
    feasible: bool
    total_capacity: int
    total_allocated: int
    dimension_usage: dict[str, dict[str, int]]
    generated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lines": [line.to_dict() for line in self.lines],
            "shortfalls": [asdict(s) for s in self.shortfalls],
            "tail_records": [asdict(t) for t in self.tail_records],
            "feasible": self.feasible,
            "total_capacity": self.total_capacity,
            "total_allocated": self.total_allocated,
            "dimension_usage": self.dimension_usage,
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrialResult":
        return cls(
            lines=[ResultLine.from_dict(item) for item in data["lines"]],
            shortfalls=[Shortfall(**item) for item in data.get("shortfalls", [])],
            tail_records=[TailRecord(**item) for item in data.get("tail_records", [])],
            feasible=data["feasible"],
            total_capacity=data["total_capacity"],
            total_allocated=data["total_allocated"],
            dimension_usage=data.get("dimension_usage", {}),
            generated_at=data.get("generated_at", now_iso()),
        )


@dataclass
class Trial:
    """不可变试算：创建时对输入做快照，多套试算互不影响、也不影响正式额度。"""

    id: str
    batch_id: str
    created_at: str
    params: dict[str, Any]
    # 输入快照（院校/申报/容量/保障的深拷贝）
    snapshot: dict[str, Any]
    result: TrialResult
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "batch_id": self.batch_id,
            "created_at": self.created_at,
            "params": self.params,
            "snapshot": self.snapshot,
            "result": self.result.to_dict(),
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Trial":
        return cls(
            id=data["id"],
            batch_id=data["batch_id"],
            created_at=data["created_at"],
            params=data.get("params", {}),
            snapshot=data.get("snapshot", {}),
            result=TrialResult.from_dict(data["result"]),
            label=data.get("label", ""),
        )


@dataclass
class LedgerEntry:
    """额度分录。余额 = 同院校分录金额之和；正式额度的一切变化都只通过分录表达。"""

    seq: int
    institution_id: str
    major: str
    amount: int                         # 正数划入，负数划出
    source: str
    reason: str
    ref: str = ""                       # 关联申报/对手院校/原分录
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LedgerEntry":
        return cls(**data)


@dataclass
class Confirmation:
    """院校确认记录。"""

    application_id: str
    institution_id: str
    confirmed: bool
    seats: int
    at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Batch:
    """名额批次聚合根：配置、试算、正式结果、额度分录与确认状态。"""

    id: str
    name: str
    status: str = BatchStatus.DRAFT.value
    total_seats: int = 0
    # 配置
    institutions: dict[str, Institution] = field(default_factory=dict)
    applications: dict[str, Application] = field(default_factory=dict)
    capacities: list[CapacityRule] = field(default_factory=list)
    guarantees: list[Guarantee] = field(default_factory=list)
    # 试算（不产生正式额度）
    trials: dict[str, Trial] = field(default_factory=dict)
    # 发布后的正式数据
    published_trial_id: str | None = None
    published_result: TrialResult | None = None
    published_at: str | None = None
    ledger_seq: int = 0
    ledger: list[LedgerEntry] = field(default_factory=list)
    confirmations: dict[str, Confirmation] = field(default_factory=dict)
    promoted: set[str] = field(default_factory=set)        # 已递补的候补申报 id
    rejected_apps: set[str] = field(default_factory=set)   # 已拒绝（无需确认）的入选行
    # 发布后释放名额无人承接时的可解释记录（与试算尾差记录同源不同时）
    vacant_records: list[TailRecord] = field(default_factory=list)
    version: int = 0                                       # 乐观锁版本
    created_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "total_seats": self.total_seats,
            "institutions": {k: v.to_dict() for k, v in self.institutions.items()},
            "applications": {k: v.to_dict() for k, v in self.applications.items()},
            "capacities": [c.to_dict() for c in self.capacities],
            "guarantees": [g.to_dict() for g in self.guarantees],
            "trials": {k: v.to_dict() for k, v in self.trials.items()},
            "published_trial_id": self.published_trial_id,
            "published_result": self.published_result.to_dict() if self.published_result else None,
            "published_at": self.published_at,
            "ledger_seq": self.ledger_seq,
            "ledger": [e.to_dict() for e in self.ledger],
            "confirmations": {k: v.to_dict() for k, v in self.confirmations.items()},
            "promoted": sorted(self.promoted),
            "rejected_apps": sorted(self.rejected_apps),
            "vacant_records": [r.to_dict() for r in self.vacant_records],
            "version": self.version,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Batch":
        return cls(
            id=data["id"],
            name=data["name"],
            status=data.get("status", BatchStatus.DRAFT.value),
            total_seats=data.get("total_seats", 0),
            institutions={
                k: Institution.from_dict(v) for k, v in data.get("institutions", {}).items()
            },
            applications={
                k: Application.from_dict(v) for k, v in data.get("applications", {}).items()
            },
            capacities=[CapacityRule.from_dict(c) for c in data.get("capacities", [])],
            guarantees=[Guarantee.from_dict(g) for g in data.get("guarantees", [])],
            trials={k: Trial.from_dict(v) for k, v in data.get("trials", {}).items()},
            published_trial_id=data.get("published_trial_id"),
            published_result=(
                TrialResult.from_dict(data["published_result"])
                if data.get("published_result")
                else None
            ),
            published_at=data.get("published_at"),
            ledger_seq=data.get("ledger_seq", 0),
            ledger=[LedgerEntry.from_dict(e) for e in data.get("ledger", [])],
            confirmations={
                k: Confirmation(**v) for k, v in data.get("confirmations", {}).items()
            },
            promoted=set(data.get("promoted", [])),
            rejected_apps=set(data.get("rejected_apps", [])),
            vacant_records=[TailRecord(**r) for r in data.get("vacant_records", [])],
            version=data.get("version", 0),
            created_at=data.get("created_at", now_iso()),
        )
