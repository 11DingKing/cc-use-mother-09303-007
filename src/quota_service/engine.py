"""名额分配引擎（纯函数、可重放、无副作用）。

规则要点：

1. **资格过滤**：不合格院校的申报直接记为“未入选”并给出原因，不参与排序。
2. **历史参与修正**：综合分 = 基础分 − 历史参与分 × 修正权重，分高者优先，
   同分时按申报编号兜底，结果完全确定。
3. **最低保障优先**：匹配保障规则的申报先预留保底名额（取适用保障中的最高档），
   预留阶段同时校验总名额与多维容量；容量无法支撑保障时输出 ``shortfalls`` 并
   判定不可行，禁止发布。
4. **最大余数法（Hamilton）**：保障之后的余量按综合分比例分配，先取整数部分，
   余数名额严格按小数部分顺位派发；每个尾差名额都在 ``SeatTrace`` 中留下
   余数与顺位，派不出去的名额进入“机动名额池”并给出原因。
5. **多维容量硬约束**：country / type / major 三维容量在每个名额派发时同时校验。
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .errors import ValidationError
from .models import (
    Application,
    CapacityRule,
    Guarantee,
    Institution,
    LineStatus,
    ResultLine,
    SeatTrace,
    Shortfall,
    TailRecord,
    TrialResult,
)

VACANT_HOLDER = "vacant_recovery"
VACANT_HOLDER_NAME = "机动名额池"

DEFAULT_HISTORY_WEIGHT = 1.0


@dataclass
class _WorkingLine:
    result: ResultLine
    application: Application
    institution: Institution
    weight: float
    dims: list[tuple[str, str]]
    floor_guarantee: Guarantee | None = None


def _line_dims(app: Application, inst: Institution) -> list[tuple[str, str]]:
    return [
        ("country", inst.country),
        ("type", inst.institution_type),
        ("major", app.major),
    ]


def allocate(
    institutions: dict[str, Institution],
    applications: dict[str, Application],
    capacities: list[CapacityRule],
    guarantees: list[Guarantee],
    total_seats: int,
    params: dict[str, Any] | None = None,
) -> TrialResult:
    """执行一次完整试算。输入不会被修改。"""
    params = params or {}
    history_weight = float(params.get("history_penalty_weight", DEFAULT_HISTORY_WEIGHT))
    if total_seats < 0:
        raise ValidationError("批次总名额不能为负")
    if history_weight < 0:
        raise ValidationError("历史参与修正权重不能为负")

    caps: dict[tuple[str, str], int] = {rule.key(): rule.seats for rule in capacities}
    for (dim, value), seats in caps.items():
        if seats < 0:
            raise ValidationError(f"容量不能为负：{dim}={value}")
    usage: dict[tuple[str, str], int] = {key: 0 for key in caps}
    placed = 0

    lines: list[_WorkingLine] = []
    rejected: list[ResultLine] = []

    # ---- 1. 建线、资格过滤、综合分计算 -----------------------------------
    for app in sorted(applications.values(), key=lambda a: a.id):
        inst = institutions.get(app.institution_id)
        if inst is None:
            raise ValidationError(f"申报 {app.id} 关联的院校不存在")
        if app.major not in inst.majors:
            raise ValidationError(
                f"申报 {app.id} 的专业方向 {app.major} 不在院校 {inst.name} 的备案方向内"
            )
        if app.seats <= 0:
            raise ValidationError(f"申报 {app.id} 的名额需求必须为正整数")

        penalty = history_weight * inst.history_score
        weight = max(0.0, inst.base_score - penalty)
        result = ResultLine(
            application_id=app.id,
            institution_id=inst.id,
            institution_name=inst.name,
            major=app.major,
            requested=app.seats,
            weight=round(weight, 6),
            base_score=inst.base_score,
            history_penalty=round(penalty, 6),
            target=0.0,
            guaranteed=0,
            allocated=0,
            status=LineStatus.REJECTED.value,
            reasons=[],
        )
        line = _WorkingLine(
            result=result,
            application=app,
            institution=inst,
            weight=weight,
            dims=_line_dims(app, inst),
        )
        if not inst.eligible:
            result.reasons.append(
                f"资格不合格：{inst.ineligible_reason or '院校资格已被标记为不合格'}"
            )
            rejected.append(result)
            continue

        result.reasons.append(
            f"综合分 {weight:.2f} = 基础分 {inst.base_score:.2f}"
            f" − 历史参与分 {inst.history_score:.2f} × 修正权重 {history_weight:.2f}"
        )
        matched = [
            g for g in guarantees if (g.dimension, g.dim_value) in set(line.dims)
        ]
        if matched:
            line.floor_guarantee = max(matched, key=lambda g: g.min_seats)
        lines.append(line)

    # ---- 2. 派发原语：总名额 + 三维容量同时校验 ---------------------------
    def can_take(line: _WorkingLine, seats: int = 1) -> bool:
        if placed + seats > total_seats:
            return False
        if line.result.allocated + seats > line.application.seats:
            return False
        return all(
            usage[key] + seats <= caps[key] for key in line.dims if key in caps
        )

    def take(line: _WorkingLine, source: str, reason: str) -> None:
        nonlocal placed
        line.result.allocated += 1
        placed += 1
        for key in line.dims:
            if key in caps:
                usage[key] += 1
        line.result.traces.append(
            SeatTrace(index=line.result.allocated, source=source, reason=reason)
        )

    def block_reasons(line: _WorkingLine) -> list[str]:
        """当前名额派不下去时，列出所有触发的约束（解释缺口与尾差用）。"""
        reasons: list[str] = []
        if placed >= total_seats:
            reasons.append("批次总名额已用尽")
        if line.result.allocated >= line.application.seats:
            reasons.append("该申报需求已满足")
        for key in line.dims:
            if key in caps and usage[key] >= caps[key]:
                reasons.append(f"维度容量 {key[0]}={key[1]} 已达上限 {caps[key]} 人")
        return reasons

    # ---- 3. 最低保障预留（高分者先落位，结果确定）-------------------------
    guaranteed_lines = [ln for ln in lines if ln.floor_guarantee is not None]
    guaranteed_lines.sort(key=lambda ln: (-ln.weight, ln.application.id))
    for line in guaranteed_lines:
        g = line.floor_guarantee
        assert g is not None
        floor = min(g.min_seats, line.application.seats)
        line.result.reasons.append(
            f"适用最低保障（{g.dimension}={g.dim_value}，保底 {g.min_seats} 人）"
        )
        for _ in range(floor):
            if not can_take(line):
                line.result.reasons.append(
                    "保底名额未能全部落实：" + "、".join(block_reasons(line))
                )
                break
            take(
                line,
                source="guarantee",
                reason=f"最低保障预留：{g.dimension}={g.dim_value} 保底名额",
            )
        line.result.guaranteed = line.result.allocated

    shortfalls: list[Shortfall] = []
    for line in guaranteed_lines:
        g = line.floor_guarantee
        assert g is not None
        floor = min(g.min_seats, line.application.seats)
        missing = floor - line.result.allocated
        if missing > 0:
            shortfalls.append(
                Shortfall(
                    guarantee_id=g.id,
                    dimension=g.dimension,
                    dim_value=g.dim_value,
                    institution_id=line.institution.id,
                    missing=missing,
                    reason=(
                        f"{g.dimension}={g.dim_value} 保底 {floor} 人仅落实 "
                        f"{line.result.allocated} 人；受限原因："
                        + "、".join(block_reasons(line))
                    ),
                )
            )

    # ---- 4. 最大余数法分配余量（Fraction 精确计算，杜绝浮点尾差错位）------
    residual = total_seats - placed
    exact_target: dict[str, Fraction] = {}
    # 权重已在结果行四舍五入到 6 位小数，这里按十进制精确解析
    exact_weights = {
        line.application.id: Fraction(f"{line.weight:.6f}") for line in lines
    }
    total_weight_exact = sum(exact_weights.values(), Fraction(0))
    if residual > 0 and (lines or rejected):
        if total_weight_exact > 0:
            for line in lines:
                exact_target[line.application.id] = (
                    Fraction(residual)
                    * exact_weights[line.application.id]
                    / total_weight_exact
                )
        elif lines:
            # 全部综合分为 0 时等份额，仍保证确定性
            for line in lines:
                exact_target[line.application.id] = Fraction(residual, len(lines))
        for line in lines:
            line.result.target = float(exact_target.get(line.application.id, 0.0))

        # 4a. 整数部分（按综合分确定的顺序派发，容量不足即止）
        for line in sorted(lines, key=lambda ln: (-ln.weight, ln.application.id)):
            exact = exact_target.get(line.application.id, Fraction(0))
            whole = exact.numerator // exact.denominator
            for _ in range(whole):
                if not can_take(line):
                    break
                take(
                    line,
                    source="initial",
                    reason=(
                        f"按综合分份额应得 {line.result.target:.3f} 人，派发整数部分名额"
                    ),
                )

        # 4b. 尾差名额：按小数部分精确顺位，每轮每行至多 1 个。
        tail_rank = 0
        while placed < total_seats:
            candidates = [
                ln
                for ln in lines
                if can_take(ln) and ln.result.allocated < ln.application.seats
            ]
            if not candidates:
                break

            def tail_key(ln: _WorkingLine) -> tuple:
                exact = exact_target.get(ln.application.id, Fraction(0))
                fractional = exact - (exact.numerator // exact.denominator)
                return (-fractional, -ln.weight, ln.application.id)

            candidates.sort(key=tail_key)
            for line in candidates:
                if placed >= total_seats:
                    break
                exact = exact_target.get(line.application.id, Fraction(0))
                floor_part = exact.numerator // exact.denominator
                frac = exact - floor_part
                tail_rank += 1
                take(
                    line,
                    source="rounding",
                    reason=(
                        f"舍入尾差名额：份额 {float(exact):.3f} 的小数部分 "
                        f"{float(frac):.3f}，尾差顺位第 {tail_rank} 位"
                    ),
                )

    # ---- 5. 派不出去的名额：尾差落入可解释对象 ----------------------------
    tail_records: list[TailRecord] = []
    leftover = total_seats - placed
    if leftover > 0:
        saturated = sorted(
            f"{dim}={value}"
            for (dim, value), cap in caps.items()
            if sum(
                ln.result.allocated for ln in lines if (dim, value) in ln.dims
            )
            >= cap
        )
        demand_left = sum(
            ln.application.seats - ln.result.allocated
            for ln in lines
            if ln.result.allocated < ln.application.seats
        )
        if demand_left == 0:
            detail = "所有合格申报需求均已满足（申报总需求小于总名额）"
        elif saturated:
            detail = f"剩余申报均被已饱和的维度容量阻挡（{'、'.join(saturated)}）"
        else:
            detail = "批次总名额之外的约束导致无合格承接对象"
        tail_records.append(
            TailRecord(
                seats=leftover,
                holder=VACANT_HOLDER,
                holder_name=VACANT_HOLDER_NAME,
                reason=f"名额无合格承接对象，回收为机动名额：{detail}",
            )
        )

    # ---- 6. 入选状态、候补顺位与解释 ---------------------------------------
    unsatisfied = [ln for ln in lines if ln.result.allocated < ln.application.seats]
    unsatisfied.sort(key=lambda ln: (-ln.weight, ln.application.id))
    for rank, line in enumerate(unsatisfied, start=1):
        line.result.waitlist_rank = rank
        line.result.reasons.append(
            f"候补第 {rank} 位：综合分 {line.weight:.2f}，需求 "
            f"{line.application.seats} 人仅满足 {line.result.allocated} 人，"
            "等待放弃/资格撤销释放或机构间转让后递补"
        )

    all_lines: list[ResultLine] = []
    for line in lines:
        r = line.result
        if r.allocated == 0:
            r.status = LineStatus.WAITLISTED.value
        elif r.allocated < r.requested:
            r.status = LineStatus.PARTIAL.value
            r.reasons.append(
                f"部分入选：已得 {r.allocated}/{r.requested} 人，"
                f"其中保障 {r.guaranteed} 人"
            )
        else:
            r.status = LineStatus.SELECTED.value
            r.reasons.append(
                f"足额入选 {r.allocated} 人"
                + (f"，其中保障 {r.guaranteed} 人" if r.guaranteed else "")
            )
        all_lines.append(r)
    all_lines.extend(rejected)
    all_lines.sort(key=lambda r: (r.institution_id, r.application_id))

    dimension_usage: dict[str, dict[str, int]] = {"country": {}, "type": {}, "major": {}}
    for line in lines:
        for dim, value in line.dims:
            bucket = dimension_usage.setdefault(dim, {})
            bucket[value] = bucket.get(value, 0) + line.result.allocated

    return TrialResult(
        lines=all_lines,
        shortfalls=shortfalls,
        tail_records=tail_records,
        feasible=not shortfalls,
        total_capacity=total_seats,
        total_allocated=placed,
        dimension_usage=dimension_usage,
    )
