"""名额分配引擎（纯函数，无副作用）。

流程：

1. 资格过滤——无资格院校直接落选，原因记录在案。
2. 可行性校验——总容量及各维度容量不得为负；保障规则与容量的
   冲突在此暴露（如同一维度取值下保障合计超过其容量）。
3. 保障预留——按维度逐项预占保障名额；需求不足时据实预占并
   记录缺口，绝不凭空造名额。
4. 竞争分配——剩余名额按历史修正权重用最大余额法
   （Hamilton 法）逐席分发；每所院校不超过申请需求，
   各维度容量在每一席发放时都被检查。
5. 尾差追踪——所有取整尾差都通过 :class:`RoundingTrace`
   落到具体院校并附带规则说明。
6. 候补排名——未满足院校按统一排名键排序，给出候补位次与
   逐机构原因。

本模块不做并发控制；调用方（service 层）持锁串行化。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .models import (
    Application,
    BatchSnapshot,
    Dimension,
    InstitutionOutcome,
    QuotaError,
    RoundingTrace,
)

DIMENSIONS = (Dimension.COUNTRY, Dimension.INSTITUTION_TYPE, Dimension.SPECIALTY)


@dataclass
class AllocationResult:
    """一次试算/发布的完整结果。"""

    selected: dict[str, int]
    status: dict[str, str]            # selected / partially_selected / waitlisted / rejected
    waitlist: list[str]
    waitlist_position: dict[str, int]
    rank: dict[str, int]
    reasons: dict[str, list[str]]
    rounding_traces: list[RoundingTrace]
    guarantee_shortfalls: list[dict]
    unfilled: list[dict]
    dimension_usage: dict[str, int]
    total_capacity: int
    total_selected: int

    def outcome(self, app: Application) -> InstitutionOutcome:
        return InstitutionOutcome(
            institution_id=app.institution_id,
            selected=self.selected.get(app.institution_id, 0),
            status=self.status.get(app.institution_id, "rejected"),
            rank=self.rank.get(app.institution_id),
            waitlist_position=self.waitlist_position.get(app.institution_id),
            reasons=tuple(self.reasons.get(app.institution_id, [])),
        )

    def to_dict(self) -> dict:
        return {
            "total_capacity": self.total_capacity,
            "total_selected": self.total_selected,
            "dimension_usage": self.dimension_usage,
            "guarantee_shortfalls": self.guarantee_shortfalls,
            "unfilled": self.unfilled,
            "rounding_traces": [t.to_dict() for t in self.rounding_traces],
            "waitlist": self.waitlist,
        }


# ---------------------------------------------------------------------------
# 分配过程中的可变记账
# ---------------------------------------------------------------------------


class _Group:
    """一所院校在本次分配中的运行状态。"""

    __slots__ = ("app", "weight", "allocated", "guaranteed")

    def __init__(self, app: Application) -> None:
        self.app = app
        self.weight = Fraction(1, 1 + app.history)  # 历史参与越多，权重越低
        self.allocated = 0
        self.guaranteed = 0                        # 其中来自保障预留的部分

    @property
    def remaining_demand(self) -> int:
        return self.app.demand - self.allocated


class _Plan:
    def __init__(self, snapshot: BatchSnapshot) -> None:
        self.snapshot = snapshot
        self.eligible = list(snapshot.eligible_apps())
        self.groups = {a.institution_id: _Group(a) for a in self.eligible}
        self.traces: list[RoundingTrace] = []
        self.shortfalls: list[dict] = []
        self.reasons: dict[str, list[str]] = {
            a.institution_id: [] for a in self.eligible
        }

    def total_allocated(self) -> int:
        return sum(g.allocated for g in self.groups.values())

    def dim_used(self, dim: Dimension, value: str) -> int:
        return sum(
            g.allocated for g in self.groups.values() if g.app.tag(dim) == value
        )

    def dim_remaining(self, app: Application) -> int:
        """院校再拿名额时，总容量与三个维度容量给出的最小余量。"""
        caps = {(c.key.dimension, c.key.value): c.capacity for c in self.snapshot.capacities}
        limit = self.snapshot.total_capacity - self.total_allocated()
        for dim in DIMENSIONS:
            cap = caps.get((dim, app.tag(dim)))
            if cap is not None:
                limit = min(limit, cap - self.dim_used(dim, app.tag(dim)))
        return limit

    def award(self, g: _Group, n: int) -> None:
        g.allocated += n

    def distribute(
        self,
        members: list[_Group],
        seats: int,
        trace_dim: Dimension | None,
        trace_value: str | None,
        reason_label: str,
        guarantee: bool,
    ) -> None:
        """在 members 之间用最大余额法分发 seats 个名额。

        每席都受申请需求与各维度容量约束；席位不足（容量受限）时
        据实分发并生成被抽离的尾差记录。
        """
        members = [m for m in members if m.remaining_demand > 0]
        if not members or seats <= 0:
            return 0
        weight_sum = sum((m.weight for m in members), Fraction(0))
        exact = {
            m.app.institution_id: (
                Fraction(seats) * m.weight / weight_sum if weight_sum > 0 else Fraction(0)
            )
            for m in members
        }
        floors = {k: int(v) for k, v in exact.items()}
        credited = {m.app.institution_id: 0 for m in members}

        # 先按整数份额发放（受需求与维度余量约束）
        for m in sorted(members, key=lambda m: m.app.institution_id):
            want = min(floors[m.app.institution_id], m.remaining_demand, self.dim_remaining(m.app))
            if want > 0:
                self.award(m, want)
                credited[m.app.institution_id] += want

        # 尾差逐席发给“余额（精确份额−已发）”最大者
        unplaced = 0
        while sum(credited.values()) < seats:
            feasible = [
                m
                for m in members
                if m.remaining_demand > 0 and self.dim_remaining(m.app) > 0
            ]
            if not feasible:
                unplaced = seats - sum(credited.values())
                break
            choice = min(
                feasible,
                # 余额大优先；余额相同：历史参与少者、priority 小者、编号小者
                key=lambda m: (
                    -(exact[m.app.institution_id] - credited[m.app.institution_id]),
                    m.app.history,
                    m.app.priority,
                    m.app.institution_id,
                ),
            )
            self.award(choice, 1)
            credited[choice.app.institution_id] += 1

        # 尾差解释
        for m in members:
            inst = m.app.institution_id
            got = credited[inst]
            ex = exact[inst]
            if got > floors[inst] and ex.denominator != 1:
                self.traces.append(
                    RoundingTrace(
                        institution_id=inst,
                        dimension=trace_dim,
                        value=trace_value,
                        awarded=1,
                        exact_share=str(ex),
                        rounded_floor=floors[inst],
                        rule=(
                            f"{reason_label}：精确份额 {ex} 下取整为 {floors[inst]}，"
                            f"按最大余额法取得小数尾差 1 席（历史修正权重 {m.weight}）"
                        ),
                    )
                )
            if got < floors[inst]:
                self.traces.append(
                    RoundingTrace(
                        institution_id=inst,
                        dimension=trace_dim,
                        value=trace_value,
                        awarded=-1,
                        exact_share=str(ex),
                        rounded_floor=floors[inst],
                        rule=f"{reason_label}：受申请需求或维度容量限制，整数份额 {floors[inst]} 未取满",
                    )
                )
            if got > 0:
                if guarantee:
                    m.guaranteed += got
                self.reasons.setdefault(inst, []).append(
                    f"{reason_label} {got} 席（精确份额 {ex}）"
                )
            elif ex.denominator != 1:
                self.traces.append(
                    RoundingTrace(
                        institution_id=inst,
                        dimension=trace_dim,
                        value=trace_value,
                        awarded=0,
                        exact_share=str(ex),
                        rounded_floor=floors[inst],
                        rule=f"{reason_label}：精确份额 {ex} 不足 1 席，本轮未获名额",
                    )
                )
        return unplaced


def _validate_inputs(snapshot: BatchSnapshot) -> None:
    if snapshot.total_capacity < 0:
        raise QuotaError("INVALID_CAPACITY", "批次总容量不能为负")
    ids = [a.institution_id for a in snapshot.applications]
    if len(ids) != len(set(ids)):
        raise QuotaError("DUPLICATE_APPLICATION", "同一批次中院校编号不能重复")
    for a in snapshot.applications:
        a.validate()
    for c in snapshot.capacities:
        c.validate()
    for g in snapshot.guarantees:
        g.validate()

    caps = {(c.key.dimension, c.key.value): c.capacity for c in snapshot.capacities}
    sums: dict[tuple[Dimension, str], int] = {}
    for g in snapshot.guarantees:
        sums[(g.key.dimension, g.key.value)] = sums.get((g.key.dimension, g.key.value), 0) + g.seats
    for (dim, value), need in sums.items():
        cap = caps.get((dim, value))
        if cap is not None and need > cap:
            raise QuotaError(
                "GUARANTEE_EXCEEDS_CAPACITY",
                f"维度 {dim.value}={value} 的最低保障合计 {need} 超过其容量 {cap}",
            )


def _rank_key(app: Application):
    """候补/竞争决胜的统一排名键：历史参与少者、priority 小者、编号小者。"""
    return (app.history, app.priority, app.institution_id)


def allocate(snapshot: BatchSnapshot) -> AllocationResult:
    """对不可变快照执行完整分配，返回可解释结果。"""
    _validate_inputs(snapshot)
    plan = _Plan(snapshot)

    selected: dict[str, int] = {}
    status: dict[str, str] = {}
    reasons: dict[str, list[str]] = plan.reasons

    # 无资格院校：直接落选，不进入任何分配环节
    for a in snapshot.applications:
        if not a.eligible:
            selected[a.institution_id] = 0
            status[a.institution_id] = "rejected"
            reasons[a.institution_id] = ["不具备入选资格，未进入分配"]

    # ---- 第 3 步：保障预留（按维度/取值/名额排序，保证确定性） -------------
    for rule in sorted(
        snapshot.guarantees,
        key=lambda g: (g.key.dimension.value, g.key.value, g.seats),
    ):
        dim, value = rule.key.dimension, rule.key.value
        members = [plan.groups[a.institution_id] for a in plan.eligible if a.tag(dim) == value]
        covered_before = sum(m.allocated for m in members)
        demand_left = sum(m.remaining_demand for m in members)
        give = max(0, min(rule.seats - covered_before, demand_left))
        plan.distribute(
            members=members,
            seats=give,
            trace_dim=dim,
            trace_value=value,
            reason_label=f"最低保障预留（{dim.value}={value}）",
            guarantee=True,
        )
        covered_after = sum(m.allocated for m in members)
        if covered_after < rule.seats:
            if covered_before + demand_left < rule.seats:
                reason = "该维度取值下合格申请需求不足，保障缺口无法填补"
            else:
                reason = "受批次总容量或其他维度容量限制，保障缺口无法填补"
            plan.shortfalls.append(
                {
                    "dimension": dim.value,
                    "value": value,
                    "guaranteed": rule.seats,
                    "filled": covered_after,
                    "reason": reason,
                }
            )

    # ---- 第 4 步：竞争分配 -------------------------------------------------
    left = snapshot.total_capacity - plan.total_allocated()
    pool = [plan.groups[a.institution_id] for a in plan.eligible]
    unplaced = plan.distribute(
        members=pool,
        seats=left,
        trace_dim=None,
        trace_value=None,
        reason_label="竞争分配",
        guarantee=False,
    )

    unfilled: list[dict] = []
    if unplaced > 0:
        # 有名额发不出去：归因到饱和的维度容量，或所有需求已满足后的正常结余
        caps = {(c.key.dimension, c.key.value): c.capacity for c in snapshot.capacities}
        saturated: list[str] = []
        for dim in DIMENSIONS:
            for value in {a.tag(dim) for a in plan.eligible}:
                cap = caps.get((dim, value))
                if cap is None or cap < 0:
                    continue
                used = plan.dim_used(dim, value)
                blocked = sum(
                    plan.groups[a.institution_id].remaining_demand
                    for a in plan.eligible if a.tag(dim) == value
                )
                if used >= cap and blocked > 0:
                    saturated.append(f"{dim.value}={value} 容量 {cap} 已满，该维度下仍有 {blocked} 席需求被阻塞")
        if saturated:
            unfilled.append({
                "seats": unplaced,
                "reason": "剩余名额受维度容量限制无法分配：" + "；".join(saturated),
            })
        else:
            unfilled.append({
                "seats": unplaced,
                "reason": "所有合格申请需求均已满足，容量正常结余（尾差不硬塞给任何机构）",
            })

    # ---- 第 6 步：排名、状态、候补与逐机构原因 -----------------------------
    ranked = sorted(plan.eligible, key=_rank_key)
    rank = {a.institution_id: i + 1 for i, a in enumerate(ranked)}

    zero = [a for a in ranked if plan.groups[a.institution_id].allocated == 0]
    partial = [a for a in ranked if 0 < plan.groups[a.institution_id].allocated < a.demand]
    full = [a for a in ranked if plan.groups[a.institution_id].allocated >= a.demand]

    for a in zero:
        selected[a.institution_id] = 0
        status[a.institution_id] = "waitlisted"
    for a in partial:
        selected[a.institution_id] = plan.groups[a.institution_id].allocated
        status[a.institution_id] = "partially_selected"
    for a in full:
        selected[a.institution_id] = plan.groups[a.institution_id].allocated
        status[a.institution_id] = "selected"

    waitlist = [a.institution_id for a in zero + partial]
    waitlist_position = {inst: i for i, inst in enumerate(waitlist, start=1)}

    for a in zero:
        g = plan.groups[a.institution_id]
        tail = f"，剩余容量已分尽，进入候补第 {waitlist_position[a.institution_id]} 位"
        if unplaced > 0:
            tail = f"，{unfilled[0]['reason']}，进入候补第 {waitlist_position[a.institution_id]} 位"
        reasons[a.institution_id].append(
            f"竞争排名第 {rank[a.institution_id]} 位"
            f"（历史参与系数 {a.history}，权重 {g.weight}）" + tail
        )
    for a in partial:
        inst = a.institution_id
        got = selected[inst]
        reasons[inst].append(
            f"满足 {got}/{a.demand} 席，缺口 {a.demand - got} 席；"
            f"竞争排名第 {rank[inst]} 位，进入候补第 {waitlist_position[inst]} 位"
        )
    for a in full:
        reasons[a.institution_id].append(
            f"申请 {a.demand} 席全部满足，竞争排名第 {rank[a.institution_id]} 位"
        )
    if unplaced > 0:
        for a in partial:
            reasons[a.institution_id].append(unfilled[0]["reason"])

    dimension_usage: dict[str, int] = {}
    for dim in DIMENSIONS:
        for value in {a.tag(dim) for a in plan.eligible}:
            used = plan.dim_used(dim, value)
            if used:
                dimension_usage[f"{dim.value}:{value}"] = used

    return AllocationResult(
        selected=selected,
        status=status,
        waitlist=waitlist,
        waitlist_position=waitlist_position,
        rank=rank,
        reasons=reasons,
        rounding_traces=plan.traces,
        guarantee_shortfalls=plan.shortfalls,
        unfilled=unfilled,
        dimension_usage=dimension_usage,
        total_capacity=snapshot.total_capacity,
        total_selected=plan.total_allocated(),
    )
