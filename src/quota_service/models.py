"""领域模型与值对象。

全部值对象不可变；可变聚合由 :mod:`quota_service.service` 负责。
名额在任何计算路径中都使用整数个，分配过程中的份额用
:class:`fractions.Fraction` 精确表示，避免浮点尾差。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from fractions import Fraction
from typing import Any, Mapping


class QuotaError(Exception):
    """违反领域规则时抛出。

    ``code`` 是稳定的机器可读错误码，中文消息面向操作员。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


# ---------------------------------------------------------------------------
# 基础维度
# ---------------------------------------------------------------------------


class Dimension(str, Enum):
    """保障与容量的三个维度。"""

    COUNTRY = "country"
    INSTITUTION_TYPE = "institution_type"
    SPECIALTY = "specialty"


@dataclass(frozen=True)
class DimensionKey:
    """某个维度上的具体取值，如 ``specialty=新能源``。"""

    dimension: Dimension
    value: str

    def as_string(self) -> str:
        return f"{self.dimension.value}:{self.value}"


# ---------------------------------------------------------------------------
# 批次与配置
# ---------------------------------------------------------------------------


class BatchState(str, Enum):
    DRAFT = "draft"              # 申报：维护容量、资格、保障规则
    PUBLISHED = "published"      # 分配：方案已发布，输入冻结
    CONFIRMED = "confirmed"      # 确认：入选机构逐一确认
    CLOSED = "closed"            # 递补结束、批次收尾


# 发布之后禁止再改输入的状态集合
FROZEN_STATES = frozenset({BatchState.PUBLISHED, BatchState.CONFIRMED, BatchState.CLOSED})


@dataclass(frozen=True)
class DimensionCapacity:
    """维度取值容量，如 country=泰国 最多 8 人。

    capacity 为 None 表示该取值只声明不限制总量（纯保障声明也允许）。
    """

    key: DimensionKey
    capacity: int | None = None

    def validate(self) -> None:
        if not self.key.value:
            raise QuotaError("INVALID_DIMENSION_VALUE", "维度取值不能为空")
        if self.capacity is not None and self.capacity < 0:
            raise QuotaError("INVALID_CAPACITY", f"{self.key.as_string()} 容量不能为负")


@dataclass(frozen=True)
class GuaranteeRule:
    """最低保障规则：某维度取值下，入选总名额不得低于 ``seats``。

    仅当该取值下的申请总需求达到 seats 时保障才成立——保障不能
    凭空创造需求；需求不足时差额记为“需求不足，无法保障”，
    该数量不参与分配，解释中逐机构可查。
    """

    key: DimensionKey
    seats: int

    def validate(self) -> None:
        if not self.key.value:
            raise QuotaError("INVALID_DIMENSION_VALUE", "维度取值不能为空")
        if self.seats <= 0:
            raise QuotaError("INVALID_GUARANTEE", f"{self.key.as_string()} 保障名额必须为正整数")


@dataclass(frozen=True)
class Application:
    """院校参训申请（批次输入）。

    demand      申请名额数（整数）。
    eligible    是否具备入选资格（资格撤销后置 False）。
    history     历史参与系数：非负有理数，0 表示从未参与的新院校，
                越大表示历史上参与越多，分配权重按 1/(1+history)
                折减，同分排序时也靠后。
    tags        院校在三个维度上的取值。
    priority    协调员设置的同分兜底次序，小者优先。
    """

    institution_id: str
    name: str
    country: str
    institution_type: str
    specialty: str
    demand: int
    eligible: bool = True
    history: Fraction = Fraction(0)
    priority: int = 0

    def validate(self) -> None:
        if not self.institution_id:
            raise QuotaError("INVALID_APPLICATION", "院校编号不能为空")
        for label, value in (
            ("名称", self.name),
            ("国家", self.country),
            ("院校类型", self.institution_type),
            ("专业方向", self.specialty),
        ):
            if not value:
                raise QuotaError("INVALID_APPLICATION", f"{self.institution_id} 的{label}不能为空")
        if self.demand <= 0:
            raise QuotaError("INVALID_APPLICATION", f"{self.institution_id} 申请名额必须为正整数")
        if self.history < 0:
            raise QuotaError("INVALID_APPLICATION", f"{self.institution_id} 历史参与系数不能为负")

    def tag(self, dimension: Dimension) -> str:
        return {
            Dimension.COUNTRY: self.country,
            Dimension.INSTITUTION_TYPE: self.institution_type,
            Dimension.SPECIALTY: self.specialty,
        }[dimension]

    def to_dict(self) -> dict[str, Any]:
        return {
            "institution_id": self.institution_id,
            "name": self.name,
            "country": self.country,
            "institution_type": self.institution_type,
            "specialty": self.specialty,
            "demand": self.demand,
            "eligible": self.eligible,
            "history": str(self.history),
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Application":
        try:
            app = cls(
                institution_id=str(data["institution_id"]),
                name=str(data["name"]),
                country=str(data["country"]),
                institution_type=str(data["institution_type"]),
                specialty=str(data["specialty"]),
                demand=int(data["demand"]),
                eligible=bool(data.get("eligible", True)),
                history=Fraction(str(data.get("history", 0))),
                priority=int(data.get("priority", 0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise QuotaError("INVALID_APPLICATION", f"申请数据格式有误：{exc}") from exc
        app.validate()
        return app


# ---------------------------------------------------------------------------
# 额度分录（发布后所有变动都通过分录完成，不改原始输入）
# ---------------------------------------------------------------------------


class LedgerEntryKind(str, Enum):
    INITIAL = "initial"          # 方案发布的初始入选
    RELINQUISH = "relinquish"    # 院校放弃
    REVOKE = "revoke"            # 资格撤销
    TRANSFER_OUT = "transfer_out"  # 机构间转让：转出
    TRANSFER_IN = "transfer_in"    # 机构间转让：转入
    WAITLIST_PROMOTE = "waitlist_promote"  # 候补递补
    ADJUSTMENT = "adjustment"    # 协调员手工修正（需备注）


@dataclass(frozen=True)
class LedgerEntry:
    """一条不可变额度分录。

    delta 为带符号整数：正表示增加该校入选名额，负表示减少。
    放弃/撤销/转让产生的减少必须由另一笔分录（递补/转入）平衡，
    批次总入选数因此始终可审计且不会超过总容量。
    """

    seq: int
    kind: LedgerEntryKind
    institution_id: str
    delta: int
    reason: str
    batch_version: int
    ref: str | None = None          # 关联分录/候补位次编号
    counterparty: str | None = None  # 转让对手方


@dataclass(frozen=True)
class RoundingTrace:
    """舍入尾差的可解释记录：一个尾差名额具体判给/抽离了谁。"""

    institution_id: str
    dimension: Dimension | None
    value: str | None
    awarded: int  # 1 表示拿到尾差名额，-1 表示被抽走
    exact_share: str
    rounded_floor: int
    rule: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "institution_id": self.institution_id,
            "dimension": self.dimension.value if self.dimension else None,
            "dimension_value": self.value,
            "awarded": self.awarded,
            "exact_share": self.exact_share,
            "rounded_floor": self.rounded_floor,
            "rule": self.rule,
        }


@dataclass(frozen=True)
class InstitutionOutcome:
    """单个机构在某套方案中的结果与原因。"""

    institution_id: str
    selected: int
    status: str  # selected / partially_selected / waitlisted / rejected
    rank: int | None
    waitlist_position: int | None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "institution_id": self.institution_id,
            "status": self.status,
            "selected": self.selected,
            "rank": self.rank,
            "waitlist_position": self.waitlist_position,
            "reasons": list(self.reasons),
        }


# ---------------------------------------------------------------------------
# 输入快照（试算与发布都基于不可变快照）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatchSnapshot:
    """批次输入的不可变快照。试算之间、正式方案之间互不影响。"""

    total_capacity: int
    capacities: tuple[DimensionCapacity, ...]
    guarantees: tuple[GuaranteeRule, ...]
    applications: tuple[Application, ...]
    input_version: int

    def app(self, institution_id: str) -> Application:
        for a in self.applications:
            if a.institution_id == institution_id:
                return a
        raise QuotaError("UNKNOWN_INSTITUTION", f"未知院校：{institution_id}")

    def demand_for(self, dimension: Dimension, value: str) -> int:
        return sum(
            a.demand for a in self.applications if a.eligible and a.tag(dimension) == value
        )

    def eligible_apps(self) -> tuple[Application, ...]:
        return tuple(a for a in self.applications if a.eligible)
