"""名额管理应用服务。

聚合批次输入、试算方案与正式额度，是 HTTP 层之下的唯一用例入口。

关键约定：

* ``DRAFT`` 状态下可以改输入（容量、资格、保障、申请、历史系数），
  所有改动只影响之后新建的试算；既有试算绑定各自的不可变快照。
* 多套试算可任意创建、对比，绝不触碰正式额度。
* ``publish`` 选定一套试算发布：批次进入 ``PUBLISHED``，输入冻结，
  正式 :class:`~quota_service.ledger.Ledger` 以初始分录建立。
* 发布之后的放弃、资格撤销、转让、递补全部通过分录调整，
  不再重算分配。
* 每个写操作都带批次版本号（乐观并发）：调用方持旧版本提交会得到
  ``VERSION_CONFLICT``；服务内部另用锁串行化，分录的容量校验
  在锁内完成，因此并发确认/递补不可能超发。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Callable, TypeVar

from .allocator import AllocationResult, allocate
from .ledger import Ledger
from .models import (
    Application,
    BatchSnapshot,
    BatchState,
    Dimension,
    DimensionCapacity,
    DimensionKey,
    FROZEN_STATES,
    GuaranteeRule,
    LedgerEntry,
    LedgerEntryKind,
    QuotaError,
)

T = TypeVar("T")


@dataclass
class Scenario:
    """一套试算：名称 + 输入快照 + 分配结果。"""

    scenario_id: str
    name: str
    snapshot: BatchSnapshot
    result: AllocationResult
    input_version: int


@dataclass
class _Batch:
    batch_id: str
    state: BatchState = BatchState.DRAFT
    total_capacity: int = 0
    capacities: dict[tuple[Dimension, str], DimensionCapacity] = field(default_factory=dict)
    guarantees: list[GuaranteeRule] = field(default_factory=list)
    applications: dict[str, Application] = field(default_factory=dict)
    input_version: int = 0
    version: int = 1                       # 乐观并发版本（写操作每次 +1）
    scenarios: dict[str, Scenario] = field(default_factory=dict)
    scenario_seq: int = 0
    published_id: str | None = None
    frozen: BatchSnapshot | None = None
    ledger: Ledger | None = None
    confirmations: set[str] = field(default_factory=set)
    revoked: dict[str, str] = field(default_factory=dict)  # 发布后撤销资格：院校 -> 原因
    relinquished: dict[str, int] = field(default_factory=dict)  # 主动放弃累计席位：院校 -> 数量
    lock: threading.RLock = field(default_factory=threading.RLock)


class QuotaService:
    def __init__(self) -> None:
        self._batches: dict[str, _Batch] = {}
        self._ids = 0
        self._global_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 批次与输入维护（仅 DRAFT）
    # ------------------------------------------------------------------ #

    def create_batch(self, name: str = "", total_capacity: int = 0) -> dict[str, str]:
        with self._global_lock:
            self._ids += 1
            batch_id = f"B{self._ids:04d}"
            batch = _Batch(batch_id=batch_id, total_capacity=int(total_capacity))
            if total_capacity < 0:
                raise QuotaError("INVALID_CAPACITY", "批次总容量不能为负")
            self._batches[batch_id] = batch
        return {"batch_id": batch_id, "name": name}

    def _get(self, batch_id: str) -> _Batch:
        batch = self._batches.get(batch_id)
        if batch is None:
            raise QuotaError("UNKNOWN_BATCH", f"未知批次：{batch_id}")
        return batch

    def _editable(self, batch: _Batch) -> None:
        if batch.state in FROZEN_STATES:
            raise QuotaError(
                "INPUT_FROZEN",
                f"批次已发布（当前状态 {batch.state.value}），容量、资格、保障与申请输入已冻结，"
                "变动只能通过额度分录进行",
            )

    def _mutate(self, batch: _Batch, expected_version: int | None, fn: Callable[[], T]) -> T:
        with batch.lock:
            if expected_version is not None and expected_version != batch.version:
                raise QuotaError(
                    "VERSION_CONFLICT",
                    f"批次版本已过期：你持有 {expected_version}，当前为 {batch.version}，请刷新后重试",
                )
            result = fn()
            batch.version += 1
            return result

    def set_total_capacity(self, batch_id: str, seats: int,
                           expected_version: int | None = None) -> dict:
        batch = self._get(batch_id)
        if seats < 0:
            raise QuotaError("INVALID_CAPACITY", "批次总容量不能为负")

        def do() -> None:
            self._editable(batch)
            batch.total_capacity = seats
            batch.input_version += 1

        self._mutate(batch, expected_version, do)
        return self.batch_view(batch_id)

    def set_dimension_capacity(self, batch_id: str, dimension: str, value: str,
                               seats: int | None, expected_version: int | None = None) -> dict:
        dim = _parse_dimension(dimension)
        key = DimensionKey(dim, value)
        cap = DimensionCapacity(key, seats)
        cap.validate()
        batch = self._get(batch_id)

        def do() -> None:
            self._editable(batch)
            # 与保障规则冲突时立即拒绝
            guaranteed = sum(g.seats for g in batch.guarantees if g.key == key)
            if seats is not None and guaranteed > seats:
                raise QuotaError(
                    "GUARANTEE_EXCEEDS_CAPACITY",
                    f"{key.as_string()} 已设最低保障 {guaranteed}，不能把容量降到 {seats}",
                )
            if seats is None:
                batch.capacities.pop((dim, value), None)
            else:
                batch.capacities[(dim, value)] = cap
            batch.input_version += 1

        self._mutate(batch, expected_version, do)
        return self.batch_view(batch_id)

    def set_guarantee(self, batch_id: str, dimension: str, value: str, seats: int,
                      expected_version: int | None = None) -> dict:
        dim = _parse_dimension(dimension)
        rule = GuaranteeRule(DimensionKey(dim, value), seats)
        rule.validate()
        batch = self._get(batch_id)

        def do() -> None:
            self._editable(batch)
            cap = batch.capacities.get((dim, value))
            if cap is not None and cap.capacity is not None and seats > cap.capacity:
                raise QuotaError(
                    "GUARANTEE_EXCEEDS_CAPACITY",
                    f"{rule.key.as_string()} 保障 {seats} 超过其容量 {cap.capacity}",
                )
            batch.guarantees = [g for g in batch.guarantees if g.key != rule.key]
            batch.guarantees.append(rule)
            batch.input_version += 1

        self._mutate(batch, expected_version, do)
        return self.batch_view(batch_id)

    def remove_guarantee(self, batch_id: str, dimension: str, value: str,
                         expected_version: int | None = None) -> dict:
        dim = _parse_dimension(dimension)
        batch = self._get(batch_id)

        def do() -> None:
            self._editable(batch)
            before = len(batch.guarantees)
            batch.guarantees = [
                g for g in batch.guarantees
                if not (g.key.dimension == dim and g.key.value == value)
            ]
            if len(batch.guarantees) == before:
                raise QuotaError("RULE_NOT_FOUND", f"{dim.value}={value} 未设置最低保障")
            batch.input_version += 1

        self._mutate(batch, expected_version, do)
        return self.batch_view(batch_id)

    def upsert_application(self, batch_id: str, data: dict,
                           expected_version: int | None = None) -> dict:
        app = Application.from_dict(data)
        batch = self._get(batch_id)

        def do() -> Application:
            self._editable(batch)
            batch.applications[app.institution_id] = app
            batch.input_version += 1
            return app

        self._mutate(batch, expected_version, do)
        return self.batch_view(batch_id)

    def set_eligibility(self, batch_id: str, institution_id: str, eligible: bool,
                        expected_version: int | None = None) -> dict:
        """申报阶段设置/撤销资格（发布后的资格撤销走 :meth:`revoke_after_publish`）。"""
        batch = self._get(batch_id)

        def do() -> None:
            self._editable(batch)
            app = self._app(batch, institution_id)
            batch.applications[institution_id] = Application(
                institution_id=app.institution_id, name=app.name, country=app.country,
                institution_type=app.institution_type, specialty=app.specialty,
                demand=app.demand, eligible=eligible, history=app.history, priority=app.priority,
            )
            batch.input_version += 1

        self._mutate(batch, expected_version, do)
        return self.batch_view(batch_id)

    def set_history(self, batch_id: str, institution_id: str, history: str,
                    expected_version: int | None = None) -> dict:
        """历史参与修正系数（非负有理数，如 '3' 或 '1/2'）。"""
        value = Fraction(str(history))
        if value < 0:
            raise QuotaError("INVALID_APPLICATION", "历史参与系数不能为负")
        batch = self._get(batch_id)

        def do() -> None:
            self._editable(batch)
            app = self._app(batch, institution_id)
            batch.applications[institution_id] = Application(
                institution_id=app.institution_id, name=app.name, country=app.country,
                institution_type=app.institution_type, specialty=app.specialty,
                demand=app.demand, eligible=app.eligible, history=value, priority=app.priority,
            )
            batch.input_version += 1

        self._mutate(batch, expected_version, do)
        return self.batch_view(batch_id)

    def _app(self, batch: _Batch, institution_id: str) -> Application:
        app = batch.applications.get(institution_id)
        if app is None:
            raise QuotaError("UNKNOWN_INSTITUTION", f"批次中不存在院校：{institution_id}")
        return app

    def _snapshot(self, batch: _Batch) -> BatchSnapshot:
        return BatchSnapshot(
            total_capacity=batch.total_capacity,
            capacities=tuple(sorted(batch.capacities.values(),
                                    key=lambda c: (c.key.dimension.value, c.key.value))),
            guarantees=tuple(sorted(batch.guarantees,
                                    key=lambda g: (g.key.dimension.value, g.key.value))),
            applications=tuple(sorted(batch.applications.values(), key=lambda a: a.institution_id)),
            input_version=batch.input_version,
        )

    # ------------------------------------------------------------------ #
    # 试算（不影响正式额度）
    # ------------------------------------------------------------------ #

    def run_scenario(self, batch_id: str, name: str = "",
                     expected_version: int | None = None) -> dict:
        batch = self._get(batch_id)

        def do() -> Scenario:
            self._editable(batch)
            if not batch.applications:
                raise QuotaError("NO_APPLICATIONS", "批次还没有任何院校申请，无法试算")
            batch.scenario_seq += 1
            sid = f"S{batch.scenario_seq:03d}"
            snapshot = self._snapshot(batch)
            scenario = Scenario(
                scenario_id=sid,
                name=name or f"试算方案 {sid}",
                snapshot=snapshot,
                result=allocate(snapshot),
                input_version=snapshot.input_version,
            )
            batch.scenarios[sid] = scenario
            return scenario

        scenario = self._mutate(batch, expected_version, do)
        return self.scenario_view(batch_id, scenario.scenario_id)

    def compare_scenarios(self, batch_id: str, scenario_ids: list[str]) -> dict:
        batch = self._get(batch_id)
        if len(scenario_ids) < 2:
            raise QuotaError("INVALID_COMPARISON", "至少需要两套试算才能对比")
        scenarios: list[Scenario] = []
        for sid in scenario_ids:
            s = batch.scenarios.get(sid)
            if s is None:
                raise QuotaError("UNKNOWN_SCENARIO", f"未知试算方案：{sid}")
            scenarios.append(s)
        ids = [s.scenario_id for s in scenarios]
        all_insts = sorted({a.institution_id for s in scenarios for a in s.snapshot.applications})
        rows = []
        for inst in all_insts:
            row = {"institution_id": inst, "schemes": {}}
            for s in scenarios:
                app = next((a for a in s.snapshot.applications if a.institution_id == inst), None)
                row["schemes"][s.scenario_id] = {
                    "selected": s.result.selected.get(inst, 0),
                    "status": s.result.status.get(inst, "not_in_input"),
                    "waitlist_position": s.result.waitlist_position.get(inst),
                    "demand": app.demand if app else None,
                }
            selections = [row["schemes"][sid]["selected"] for sid in ids]
            row["differs"] = len(set(selections)) > 1
            rows.append(row)
        return {
            "batch_id": batch_id,
            "schemes": [
                {
                    "scenario_id": s.scenario_id,
                    "name": s.name,
                    "input_version": s.input_version,
                    "total_selected": s.result.total_selected,
                    "total_capacity": s.result.total_capacity,
                    "guarantee_shortfalls": s.result.guarantee_shortfalls,
                    "unfilled": s.result.unfilled,
                    "rounding_trace_count": len(s.result.rounding_traces),
                    "waitlist_count": len(s.result.waitlist),
                }
                for s in scenarios
            ],
            "institutions": rows,
        }

    # ------------------------------------------------------------------ #
    # 发布与冻结
    # ------------------------------------------------------------------ #

    def publish(self, batch_id: str, scenario_id: str,
                expected_version: int | None = None) -> dict:
        batch = self._get(batch_id)

        def do() -> None:
            self._editable(batch)
            scenario = batch.scenarios.get(scenario_id)
            if scenario is None:
                raise QuotaError("UNKNOWN_SCENARIO", f"未知试算方案：{scenario_id}")
            if not batch.applications:
                raise QuotaError("NO_APPLICATIONS", "没有申请数据，不能发布")
            batch.frozen = scenario.snapshot
            batch.state = BatchState.PUBLISHED
            batch.published_id = scenario_id
            batch.ledger = Ledger(scenario.snapshot)
            batch.ledger.seed_initial(scenario.result.selected, scenario.result.reasons, batch.version + 1)
            batch.confirmations = set()

        self._mutate(batch, expected_version, do)
        return self.batch_view(batch_id)

    def close_batch(self, batch_id: str, expected_version: int | None = None) -> dict:
        batch = self._get(batch_id)

        def do() -> None:
            if batch.state == BatchState.DRAFT:
                raise QuotaError("NOT_PUBLISHED", "试算批次尚未发布，不能收尾")
            if batch.state == BatchState.CLOSED:
                raise QuotaError("ALREADY_CLOSED", "批次已收尾")
            batch.state = BatchState.CLOSED

        self._mutate(batch, expected_version, do)
        return self.batch_view(batch_id)

    # ------------------------------------------------------------------ #
    # 发布后：确认 / 放弃 / 撤销 / 转让 / 递补（全部分录）
    # ------------------------------------------------------------------ #

    def _live(self, batch: _Batch) -> tuple[BatchSnapshot, Ledger, Scenario]:
        if batch.state not in (BatchState.PUBLISHED, BatchState.CONFIRMED):
            raise QuotaError(
                "BATCH_NOT_LIVE",
                f"批次当前状态 {batch.state.value}，只有发布后的批次可以调整额度",
            )
        assert batch.frozen is not None and batch.ledger is not None and batch.published_id is not None
        return batch.frozen, batch.ledger, batch.scenarios[batch.published_id]

    def confirm(self, batch_id: str, institution_id: str,
                expected_version: int | None = None) -> dict:
        """院校确认接受当前持有的全部名额。"""
        batch = self._get(batch_id)

        def do() -> dict:
            snapshot, ledger, _ = self._live(batch)
            snapshot.app(institution_id)  # 校验院校存在
            if institution_id in batch.revoked:
                raise QuotaError("ELIGIBILITY_REVOKED", f"{institution_id} 资格已被撤销，不能确认")
            held = ledger.balances().get(institution_id, 0)
            if held <= 0:
                raise QuotaError("NOTHING_TO_CONFIRM", f"{institution_id} 当前没有入选名额，无法确认")
            batch.confirmations.add(institution_id)
            # 所有当前持有者都确认后进入“确认”状态
            holders = {i for i, n in ledger.balances().items() if n > 0}
            if holders and holders <= batch.confirmations:
                batch.state = BatchState.CONFIRMED
            return {"institution_id": institution_id, "confirmed_seats": held,
                    "state": batch.state.value}

        return self._mutate(batch, expected_version, do)

    def relinquish(self, batch_id: str, institution_id: str, seats: int,
                   reason: str = "", expected_version: int | None = None) -> dict:
        batch = self._get(batch_id)

        def do() -> LedgerEntry:
            _, ledger, _ = self._live(batch)
            entry = ledger.relinquish(institution_id, seats, reason, batch.version + 1)
            batch.confirmations.discard(institution_id)
            batch.relinquished[institution_id] = batch.relinquished.get(institution_id, 0) + seats
            if batch.state == BatchState.CONFIRMED:
                batch.state = BatchState.PUBLISHED  # 名额松动，重新等待递补确认
            return entry

        entry = self._mutate(batch, expected_version, do)
        return {"entry": _entry_dict(entry), "freed_seats": -entry.delta}

    def revoke_after_publish(self, batch_id: str, institution_id: str,
                             reason: str = "", expected_version: int | None = None) -> dict:
        """发布后资格撤销：分录收回全部名额，并在冻结视图上叠加撤销标记。"""
        batch = self._get(batch_id)

        def do() -> dict:
            snapshot, ledger, _ = self._live(batch)
            snapshot.app(institution_id)
            held = ledger.balances().get(institution_id, 0)
            entry = None
            if held > 0:
                entry = ledger.revoke(institution_id, reason, batch.version + 1)
            batch.revoked[institution_id] = reason or "资格撤销"
            batch.confirmations.discard(institution_id)
            if batch.state == BatchState.CONFIRMED:
                batch.state = BatchState.PUBLISHED
            return {"entry": _entry_dict(entry) if entry else None,
                    "freed_seats": held, "institution_id": institution_id}

        return self._mutate(batch, expected_version, do)

    def transfer(self, batch_id: str, sender: str, receiver: str, seats: int,
                 reason: str = "", expected_version: int | None = None) -> dict:
        batch = self._get(batch_id)

        def do() -> list[LedgerEntry]:
            snapshot, ledger, _ = self._live(batch)
            if receiver in batch.revoked:
                raise QuotaError("RECEIVER_INELIGIBLE", f"受让方 {receiver} 资格已撤销")
            receiver_app = snapshot.app(receiver)
            if not receiver_app.eligible:
                raise QuotaError("RECEIVER_INELIGIBLE", f"受让方 {receiver} 发布时即不具备资格")
            entries = ledger.transfer(sender, receiver, seats, reason,
                                      batch.version + 1, receiver_app.eligible)
            batch.confirmations.discard(sender)
            batch.confirmations.discard(receiver)
            if batch.state == BatchState.CONFIRMED:
                batch.state = BatchState.PUBLISHED
            return entries

        entries = self._mutate(batch, expected_version, do)
        return {"entries": [_entry_dict(e) for e in entries], "ref": entries[0].ref}

    def waitlist_candidate(self, batch_id: str) -> dict | None:
        """查看当前可递补的候补队首（不产生分录）。"""
        batch = self._get(batch_id)
        if batch.state not in (BatchState.PUBLISHED, BatchState.CONFIRMED):
            return None
        assert batch.frozen is not None and batch.ledger is not None and batch.published_id
        cand = self._next_waitlist(batch)
        return _candidate_dict(cand) if cand else None

    def _waitlist_block_reason(self, batch: _Batch, app: Application) -> str | None:
        """候补院校当前不能被递补的原因；可递补时返回 None。"""
        assert batch.frozen is not None and batch.ledger is not None
        snapshot, ledger = batch.frozen, batch.ledger
        balances = ledger.balances()
        need = app.demand - balances.get(app.institution_id, 0)
        if need <= 0:
            return None
        if ledger.total_held() >= snapshot.total_capacity:
            return "批次总容量已满，暂无释放名额"
        caps = {(c.key.dimension, c.key.value): c.capacity for c in snapshot.capacities}
        dim_usage = ledger.dimension_usage()
        for dim in (Dimension.COUNTRY, Dimension.INSTITUTION_TYPE, Dimension.SPECIALTY):
            cap = caps.get((dim, app.tag(dim)))
            if cap is not None and dim_usage.get(f"{dim.value}:{app.tag(dim)}", 0) >= cap:
                return (
                    f"受维度容量限制暂不能递补：{dim.value}={app.tag(dim)} "
                    f"已用满 {cap} 席，需等待该维度下有名额释放"
                )
        return None

    def _next_waitlist(self, batch: _Batch) -> tuple[Application, int] | None:
        """按发布时候补顺序找到第一个仍可递补且容量允许的院校。

        可递补条件：资格未被撤销、发布时合格、仍有未满足需求、
        当前总容量与三个维度容量均有余量。
        """
        assert batch.frozen is not None and batch.ledger is not None and batch.published_id
        snapshot, ledger = batch.frozen, batch.ledger
        scenario = batch.scenarios[batch.published_id]
        balances = ledger.balances()
        caps = {(c.key.dimension, c.key.value): c.capacity for c in snapshot.capacities}
        dim_usage = ledger.dimension_usage()
        for inst in scenario.result.waitlist:
            if inst in batch.revoked or inst in batch.relinquished:
                continue
            app = snapshot.app(inst)
            if not app.eligible:
                continue
            need = app.demand - balances.get(inst, 0)
            if need <= 0:
                continue
            if ledger.total_held() >= snapshot.total_capacity:
                continue
            ok = True
            for dim in (Dimension.COUNTRY, Dimension.INSTITUTION_TYPE, Dimension.SPECIALTY):
                cap = caps.get((dim, app.tag(dim)))
                if cap is not None and dim_usage.get(f"{dim.value}:{app.tag(dim)}", 0) + 1 > cap:
                    ok = False
                    break
            if ok:
                return app, need
        return None

    def promote_next(self, batch_id: str, seats: int | None = None, reason: str = "",
                     expected_version: int | None = None) -> dict:
        """递补候补队首；seats 缺省为其全部剩余缺口（受容量限制由账本拦截）。"""
        batch = self._get(batch_id)

        def do() -> dict:
            snapshot, ledger, scenario = self._live(batch)
            cand = self._next_waitlist(batch)
            if cand is None:
                raise QuotaError("NO_WAITLIST_CANDIDATE", "候补队列中没有可递补院校（队列空、资格均被撤销或容量已满）")
            app, need = cand
            # 请求数量受剩余缺口、总容量余量、各维度容量余量钳制
            caps = {(c.key.dimension, c.key.value): c.capacity for c in snapshot.capacities}
            dim_usage = ledger.dimension_usage()
            room = snapshot.total_capacity - ledger.total_held()
            for dim in (Dimension.COUNTRY, Dimension.INSTITUTION_TYPE, Dimension.SPECIALTY):
                cap = caps.get((dim, app.tag(dim)))
                if cap is not None:
                    room = min(room, cap - dim_usage.get(f"{dim.value}:{app.tag(dim)}", 0))
            n = min(seats if seats is not None else need, need, room)
            if n <= 0:
                raise QuotaError("NO_WAITLIST_CANDIDATE", "当前没有可递补的容量余量")
            pos = scenario.result.waitlist_position[app.institution_id]
            entry = ledger.promote(app.institution_id, n, pos, reason, batch.version + 1)
            batch.confirmations.discard(app.institution_id)
            if batch.state == BatchState.CONFIRMED:
                batch.state = BatchState.PUBLISHED
            return {"entry": _entry_dict(entry),
                    "institution": app.to_dict(),
                    "waitlist_position": pos,
                    "remaining_gap": need - n}

        return self._mutate(batch, expected_version, do)

    # ------------------------------------------------------------------ #
    # 查询视图
    # ------------------------------------------------------------------ #

    def list_batches(self) -> list[dict]:
        return [self._summary(self._get(bid)) for bid in sorted(self._batches)]

    def _summary(self, batch: _Batch) -> dict:
        return {
            "batch_id": batch.batch_id,
            "state": batch.state.value,
            "total_capacity": batch.total_capacity,
            "application_count": len(batch.applications),
            "scenario_count": len(batch.scenarios),
            "published_id": batch.published_id,
            "version": batch.version,
            "input_version": batch.input_version,
        }

    def batch_view(self, batch_id: str) -> dict:
        batch = self._get(batch_id)
        view = self._summary(batch)
        view.update(
            {
                "capacities": [
                    {"dimension": c.key.dimension.value, "value": c.key.value,
                     "capacity": c.capacity}
                    for c in sorted(batch.capacities.values(),
                                    key=lambda c: (c.key.dimension.value, c.key.value))
                ],
                "guarantees": [
                    {"dimension": g.key.dimension.value, "value": g.key.value, "seats": g.seats}
                    for g in sorted(batch.guarantees, key=lambda g: (g.key.dimension.value, g.key.value))
                ],
                "applications": [a.to_dict() for a in sorted(batch.applications.values(),
                                                             key=lambda a: a.institution_id)],
                "scenarios": [
                    {"scenario_id": sid, "name": s.name, "input_version": s.input_version}
                    for sid, s in sorted(batch.scenarios.items())
                ],
            }
        )
        if batch.state in FROZEN_STATES:
            assert batch.ledger is not None and batch.published_id
            view["published"] = {
                "scenario_id": batch.published_id,
                "frozen_input_version": batch.frozen.input_version if batch.frozen else None,
                "total_held": batch.ledger.total_held(),
                "confirmations": sorted(batch.confirmations),
                "revoked": dict(batch.revoked),
                "entries": [_entry_dict(e) for e in batch.ledger.entries],
            }
        return view

    def scenario_view(self, batch_id: str, scenario_id: str) -> dict:
        batch = self._get(batch_id)
        s = batch.scenarios.get(scenario_id)
        if s is None:
            raise QuotaError("UNKNOWN_SCENARIO", f"未知试算方案：{scenario_id}")
        apps = {a.institution_id: a for a in s.snapshot.applications}
        return {
            "batch_id": batch_id,
            "scenario_id": s.scenario_id,
            "name": s.name,
            "input_version": s.input_version,
            "is_published": batch.published_id == s.scenario_id,
            "summary": s.result.to_dict(),
            "institutions": [
                {
                    **s.result.outcome(apps[inst]).to_dict(),
                    "name": apps[inst].name,
                    "demand": apps[inst].demand,
                    "country": apps[inst].country,
                    "institution_type": apps[inst].institution_type,
                    "specialty": apps[inst].specialty,
                }
                for inst in sorted(apps)
            ],
        }

    def institution_view(self, batch_id: str, institution_id: str) -> dict:
        """每个机构都能看到的入选/未入选（及变动）原因视图。"""
        batch = self._get(batch_id)
        if batch.state in FROZEN_STATES:
            return self._published_institution_view(batch, institution_id)
        # 申报阶段：给出最新一套试算中的解释
        if not batch.scenarios:
            app = batch.applications.get(institution_id)
            if app is None:
                raise QuotaError("UNKNOWN_INSTITUTION", f"批次中不存在院校：{institution_id}")
            return {
                "batch_id": batch_id,
                "institution_id": institution_id,
                "phase": "draft",
                "application": app.to_dict(),
                "message": "尚无试算方案，结果将在试算后可见",
            }
        sid = sorted(batch.scenarios)[-1]
        s = batch.scenarios[sid]
        app = next((a for a in s.snapshot.applications if a.institution_id == institution_id), None)
        if app is None:
            raise QuotaError("UNKNOWN_INSTITUTION", f"该试算中不存在院校：{institution_id}")
        return {
            "batch_id": batch_id,
            "institution_id": institution_id,
            "phase": "draft",
            "based_on_scenario": sid,
            "application": app.to_dict(),
            "outcome": s.result.outcome(app).to_dict(),
        }

    def _published_institution_view(self, batch: _Batch, institution_id: str) -> dict:
        assert batch.frozen is not None and batch.ledger is not None and batch.published_id
        snapshot, ledger = batch.frozen, batch.ledger
        scenario = batch.scenarios[batch.published_id]
        app = next((a for a in snapshot.applications if a.institution_id == institution_id), None)
        if app is None:
            raise QuotaError("UNKNOWN_INSTITUTION", f"批次中不存在院校：{institution_id}")
        held = ledger.balances().get(institution_id, 0)
        my_entries = [_entry_dict(e) for e in ledger.entries if e.institution_id == institution_id]
        reasons = list(scenario.result.reasons.get(institution_id, []))
        for e in ledger.entries:
            if e.institution_id == institution_id and e.kind != LedgerEntryKind.INITIAL:
                reasons.append(f"额度调整（{e.kind.value}）：{e.delta:+d}，{e.reason}")
        if institution_id in batch.revoked:
            reasons.append(f"资格已撤销：{batch.revoked[institution_id]}")
        if institution_id in batch.relinquished:
            reasons.append(
                f"本机构已主动放弃 {batch.relinquished[institution_id]} 席，"
                "按规则退出本轮自动候补递补；如需恢复请联系名额管理办公室"
            )
        if held > 0 and institution_id in batch.confirmations:
            reasons.append("本机构已确认接受名额")
        # 候补相关解释
        wl_pos = scenario.result.waitlist_position.get(institution_id)
        cand = self._next_waitlist(batch)
        waitlist_note: str | None = None
        if (held < app.demand and institution_id not in batch.revoked
                and institution_id not in batch.relinquished
                and app.eligible and wl_pos is not None):
            if cand and cand[0].institution_id == institution_id:
                waitlist_note = "当前为候补队首，一旦有名额释放即可递补"
            else:
                block = self._waitlist_block_reason(batch, app)
                head = cand[0].institution_id if cand else None
                parts = [f"候补第 {wl_pos} 位"]
                if head:
                    parts.append(f"当前队首为 {head}，需等待其递补或放弃")
                if block:
                    parts.append(block)
                elif not head:
                    parts.append("暂无可释放名额")
                waitlist_note = "，".join(parts)
        if held <= 0 and (not app.eligible or institution_id in batch.revoked):
            status = "rejected"
        elif held >= app.demand:
            status = "selected"
        elif held > 0:
            status = "partially_selected"
        elif wl_pos is not None:
            status = "waitlisted"
        else:
            status = "rejected"
        return {
            "batch_id": batch.batch_id,
            "institution_id": institution_id,
            "phase": batch.state.value,
            "application": app.to_dict(),
            "published_scenario": batch.published_id,
            "held_seats": held,
            "status": status,
            "confirmed": institution_id in batch.confirmations,
            "initial_rank": scenario.result.rank.get(institution_id),
            "waitlist_position": wl_pos,
            "waitlist_note": waitlist_note,
            "ledger_entries": my_entries,
            "reasons": reasons,
        }


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------


def _parse_dimension(value: str) -> Dimension:
    try:
        return Dimension(value)
    except ValueError:
        try:
            return {"国家": Dimension.COUNTRY, "院校类型": Dimension.INSTITUTION_TYPE,
                    "专业方向": Dimension.SPECIALTY}[value]
        except KeyError:
            raise QuotaError("INVALID_DIMENSION", f"未知维度：{value}") from None


def _entry_dict(e: LedgerEntry) -> dict:
    return {
        "seq": e.seq,
        "kind": e.kind.value,
        "institution_id": e.institution_id,
        "delta": e.delta,
        "reason": e.reason,
        "batch_version": e.batch_version,
        "ref": e.ref,
        "counterparty": e.counterparty,
    }


def _candidate_dict(cand: tuple[Application, int]) -> dict:
    app, need = cand
    return {"institution": app.to_dict(), "remaining_gap": need}
