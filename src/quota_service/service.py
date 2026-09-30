"""名额管理服务层。

业务边界对应领域契约的五个状态：

- 申报：维护院校、申报、批次容量与最低保障；
- 试算：可创建任意多套不可变试算互相对比，不产生正式额度；
- 分配：选定一套试算发布，输入冻结，初始额度以分录入账；
- 确认：院校确认入选名额，放弃/少确认释放名额；
- 递补：按候补顺位与多维容量约束递补，无法承接的名额落入可解释的机动名额池。

正式额度的一切变化（放弃、资格撤销、机构间转让、递补、尾差回收）
都只表达为额度分录（``LedgerEntry``），余额恒等于分录金额之和。
"""
from __future__ import annotations

import copy
from typing import Any

from .engine import VACANT_HOLDER, VACANT_HOLDER_NAME, allocate
from .errors import ConflictError, FrozenError, NotFoundError, ValidationError
from .models import (
    Application,
    Batch,
    BatchStatus,
    CapacityRule,
    Confirmation,
    Dimension,
    EntrySource,
    Guarantee,
    Institution,
    LineStatus,
    LedgerEntry,
    TailRecord,
    Trial,
    new_id,
    now_iso,
)
from .store import JsonStore

INPUT_FROZEN_HINT = "方案已发布，申报、容量与保障等输入已冻结"


class QuotaService:
    def __init__(self, store: JsonStore) -> None:
        self.store = store

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _require_status(self, batch: Batch, *statuses: BatchStatus) -> None:
        current = BatchStatus(batch.status)
        if current not in statuses:
            allowed = "、".join(s.value for s in statuses)
            raise ConflictError(
                f"批次当前状态为「{batch.status}」，该操作要求状态为：{allowed}"
            )

    def _require_input_open(self, batch: Batch) -> None:
        if BatchStatus(batch.status) not in (BatchStatus.DRAFT, BatchStatus.TRIAL):
            raise FrozenError(INPUT_FROZEN_HINT)

    def _save(self, raw: dict[str, dict], batch: Batch, expected_version: int | None = None) -> None:
        if expected_version is not None and expected_version != batch.version:
            raise ConflictError(
                f"并发冲突：期望版本 {expected_version}，当前版本 {batch.version}"
            )
        batch.version += 1
        raw[batch.id] = batch.to_dict()

    def _get_institution(self, batch: Batch, institution_id: str) -> Institution:
        inst = batch.institutions.get(institution_id)
        if inst is None:
            raise NotFoundError(f"院校 {institution_id} 不存在")
        return inst

    def _get_application(self, batch: Batch, application_id: str) -> Application:
        app = batch.applications.get(application_id)
        if app is None:
            raise NotFoundError(f"申报 {application_id} 不存在")
        return app

    def _published_line(self, batch: Batch, application_id: str) -> dict[str, Any]:
        assert batch.published_result is not None
        for line in batch.published_result.lines:
            if line.application_id == application_id:
                return line.to_dict()
        raise NotFoundError(f"申报 {application_id} 不在正式方案中")

    def _add_entry(
        self,
        batch: Batch,
        *,
        institution_id: str,
        major: str,
        amount: int,
        source: str,
        reason: str,
        ref: str = "",
    ) -> LedgerEntry:
        batch.ledger_seq += 1
        entry = LedgerEntry(
            seq=batch.ledger_seq,
            institution_id=institution_id,
            major=major,
            amount=amount,
            source=source,
            reason=reason,
            ref=ref,
        )
        batch.ledger.append(entry)
        return entry

    def balances(self, batch: Batch) -> dict[str, int]:
        """各申报当前正式余额 = 关联分录金额之和。"""
        result: dict[str, int] = {}
        for entry in batch.ledger:
            if not entry.ref:
                continue  # 机动名额池回收分录不挂任何申报
            result[entry.ref] = result.get(entry.ref, 0) + entry.amount
        return result

    def _usage(self, batch: Batch, balances: dict[str, int]) -> dict[tuple[str, str], int]:
        """按当前余额计算三维实际占用。"""
        usage: dict[tuple[str, str], int] = {}
        for app_id, seats in balances.items():
            if seats <= 0:
                continue
            app = batch.applications.get(app_id)
            inst = batch.institutions.get(app.institution_id) if app else None
            if app is None or inst is None or not inst.eligible:
                continue
            for key in (
                ("country", inst.country),
                ("type", inst.institution_type),
                ("major", app.major),
            ):
                usage[key] = usage.get(key, 0) + seats
        return usage

    def _caps(self, batch: Batch) -> dict[tuple[str, str], int]:
        return {rule.key(): rule.seats for rule in batch.capacities}

    @staticmethod
    def _clamp_confirmation(batch: Batch, application_id: str, new_balance: int) -> None:
        """余额因放弃/转让减少后，已确认名额不得高于新余额。"""
        confirmed = batch.confirmations.get(application_id)
        if confirmed is not None and confirmed.seats > new_balance:
            confirmed.seats = new_balance

    def _released_pool(self, batch: Batch) -> int:
        """释放但尚未被递补或回收的名额数。"""
        released = sum(-e.amount for e in batch.ledger if e.source in (
            EntrySource.DECLINE_RELEASE.value,
            EntrySource.REVOKE_RELEASE.value,
        ))
        promoted = sum(e.amount for e in batch.ledger if e.source == EntrySource.WAITLIST_PROMOTION.value)
        recovered = sum(e.amount for e in batch.ledger if e.source == EntrySource.VACANT_RECOVERY.value)
        return released - promoted - recovered

    # ------------------------------------------------------------------ #
    # 批次生命周期
    # ------------------------------------------------------------------ #
    def create_batch(self, name: str, total_seats: int) -> dict[str, Any]:
        if not name.strip():
            raise ValidationError("批次名称不能为空")
        if total_seats <= 0:
            raise ValidationError("批次总名额必须为正整数")
        batch = Batch(id=new_id("batch"), name=name.strip(), total_seats=total_seats)
        with self.store.transaction() as raw:
            self._save(raw, batch)
        return {"id": batch.id, "status": batch.status, "version": batch.version}

    def list_batches(self) -> list[dict[str, Any]]:
        with self.store.transaction() as raw:
            return [
                {
                    "id": b["id"],
                    "name": b["name"],
                    "status": b["status"],
                    "total_seats": b["total_seats"],
                    "version": b.get("version", 0),
                }
                for b in sorted(raw.values(), key=lambda x: x["created_at"])
            ]

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        batch = self.store.get_batch(batch_id)
        return self._batch_view(batch)

    def begin_trial_phase(self, batch_id: str) -> dict[str, Any]:
        with self.store.transaction() as raw:
            batch = Batch.from_dict(self.store.get_batch_raw(raw, batch_id))
            self._require_status(batch, BatchStatus.DRAFT)
            batch.status = BatchStatus.TRIAL.value
            self._save(raw, batch)
            return {"id": batch.id, "status": batch.status, "version": batch.version}

    # ------------------------------------------------------------------ #
    # 申报阶段输入维护（发布前可改，发布后冻结）
    # ------------------------------------------------------------------ #
    def add_institution(
        self,
        batch_id: str,
        *,
        name: str,
        country: str,
        institution_type: str,
        majors: list[str],
        base_score: float = 0.0,
        history_score: float = 0.0,
        institution_id: str | None = None,
    ) -> dict[str, Any]:
        if not name.strip() or not country.strip() or not institution_type.strip():
            raise ValidationError("院校名称、国家、院校类型均不能为空")
        if not majors:
            raise ValidationError("至少填报一个专业方向")
        if base_score < 0 or history_score < 0:
            raise ValidationError("基础分与历史参与分不能为负")
        with self.store.transaction() as raw:
            batch = Batch.from_dict(self.store.get_batch_raw(raw, batch_id))
            self._require_input_open(batch)
            inst_id = institution_id or new_id("inst")
            if inst_id in batch.institutions:
                raise ConflictError(f"院校 {inst_id} 已存在")
            inst = Institution(
                id=inst_id,
                name=name.strip(),
                country=country.strip(),
                institution_type=institution_type.strip(),
                majors=sorted(m.strip() for m in majors if m.strip()),
                base_score=float(base_score),
                history_score=float(history_score),
            )
            batch.institutions[inst.id] = inst
            self._save(raw, batch)
            return inst.to_dict()

    def set_eligibility(
        self, batch_id: str, institution_id: str, eligible: bool, reason: str = ""
    ) -> dict[str, Any]:
        with self.store.transaction() as raw:
            batch = Batch.from_dict(self.store.get_batch_raw(raw, batch_id))
            self._require_input_open(batch)
            inst = self._get_institution(batch, institution_id)
            inst.eligible = eligible
            inst.ineligible_reason = "" if eligible else reason.strip() or "资格审核未通过"
            self._save(raw, batch)
            return inst.to_dict()

    def set_history_score(self, batch_id: str, institution_id: str, history_score: float) -> dict[str, Any]:
        if history_score < 0:
            raise ValidationError("历史参与分不能为负")
        with self.store.transaction() as raw:
            batch = Batch.from_dict(self.store.get_batch_raw(raw, batch_id))
            self._require_input_open(batch)
            inst = self._get_institution(batch, institution_id)
            inst.history_score = float(history_score)
            self._save(raw, batch)
            return inst.to_dict()

    def add_application(
        self, batch_id: str, institution_id: str, major: str, seats: int, note: str = ""
    ) -> dict[str, Any]:
        if seats <= 0:
            raise ValidationError("申报名额必须为正整数")
        with self.store.transaction() as raw:
            batch = Batch.from_dict(self.store.get_batch_raw(raw, batch_id))
            self._require_input_open(batch)
            inst = self._get_institution(batch, institution_id)
            if major not in inst.majors:
                raise ValidationError(f"专业方向 {major} 不在院校 {inst.name} 的备案方向内")
            for app in batch.applications.values():
                if app.institution_id == institution_id and app.major == major:
                    raise ConflictError(f"院校 {inst.name} 已申报专业方向 {major}")
            app = Application(
                id=new_id("app"),
                institution_id=institution_id,
                major=major,
                seats=seats,
                note=note.strip(),
            )
            batch.applications[app.id] = app
            self._save(raw, batch)
            return app.to_dict()

    def set_capacities(self, batch_id: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
        with self.store.transaction() as raw:
            batch = Batch.from_dict(self.store.get_batch_raw(raw, batch_id))
            self._require_input_open(batch)
            parsed: list[CapacityRule] = []
            seen: set[tuple[str, str]] = set()
            for item in rules:
                try:
                    dim = Dimension(item["dimension"]).value
                    value = str(item["dim_value"]).strip()
                    seats = int(item["seats"])
                except (KeyError, ValueError) as exc:
                    raise ValidationError(f"容量规则格式非法：{item}") from exc
                if not value:
                    raise ValidationError("容量维度取值不能为空")
                if seats < 0:
                    raise ValidationError("容量名额不能为负")
                if (dim, value) in seen:
                    raise ConflictError(f"容量规则重复：{dim}={value}")
                seen.add((dim, value))
                parsed.append(CapacityRule(dimension=dim, dim_value=value, seats=seats))
            batch.capacities = parsed
            self._save(raw, batch)
            return {"rules": [c.to_dict() for c in batch.capacities]}

    def add_guarantee(
        self, batch_id: str, dimension: str, dim_value: str, min_seats: int, note: str = ""
    ) -> dict[str, Any]:
        try:
            dim = Dimension(dimension).value
        except ValueError as exc:
            raise ValidationError(f"未知保障维度：{dimension}") from exc
        if min_seats <= 0:
            raise ValidationError("最低保障名额必须为正整数")
        with self.store.transaction() as raw:
            batch = Batch.from_dict(self.store.get_batch_raw(raw, batch_id))
            self._require_input_open(batch)
            g = Guarantee(
                id=new_id("gua"),
                dimension=dim,
                dim_value=dim_value.strip(),
                min_seats=min_seats,
                note=note.strip(),
            )
            batch.guarantees.append(g)
            self._save(raw, batch)
            return g.to_dict()

    # ------------------------------------------------------------------ #
    # 试算（不可变、可多套对比、不影响正式额度）
    # ------------------------------------------------------------------ #
    def _snapshot_inputs(self, batch: Batch) -> dict[str, Any]:
        return {
            "total_seats": batch.total_seats,
            "institutions": copy.deepcopy({k: v.to_dict() for k, v in batch.institutions.items()}),
            "applications": copy.deepcopy({k: v.to_dict() for k, v in batch.applications.items()}),
            "capacities": copy.deepcopy([c.to_dict() for c in batch.capacities]),
            "guarantees": copy.deepcopy([g.to_dict() for g in batch.guarantees]),
        }

    def create_trial(
        self,
        batch_id: str,
        *,
        label: str = "",
        history_penalty_weight: float = 1.0,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        with self.store.transaction() as raw:
            batch = Batch.from_dict(self.store.get_batch_raw(raw, batch_id))
            self._require_status(batch, BatchStatus.DRAFT, BatchStatus.TRIAL)
            if not batch.applications:
                raise ValidationError("尚无任何申报，无法试算")
            if history_penalty_weight < 0:
                raise ValidationError("历史参与修正权重不能为负")
            params = {"history_penalty_weight": history_penalty_weight}
            result = allocate(
                batch.institutions,
                batch.applications,
                batch.capacities,
                batch.guarantees,
                batch.total_seats,
                params,
            )
            if batch.status == BatchStatus.DRAFT.value:
                batch.status = BatchStatus.TRIAL.value
            trial = Trial(
                id=new_id("trial"),
                batch_id=batch.id,
                created_at=now_iso(),
                params=params,
                snapshot=self._snapshot_inputs(batch),
                result=result,
                label=label.strip(),
            )
            batch.trials[trial.id] = trial
            self._save(raw, batch, expected_version)
            return {"id": trial.id, "summary": self._trial_summary(trial), "version": batch.version}

    def list_trials(self, batch_id: str) -> list[dict[str, Any]]:
        batch = self.store.get_batch(batch_id)
        return [
            {"id": t.id, "label": t.label, "created_at": t.created_at, **self._trial_summary(t)}
            for t in sorted(batch.trials.values(), key=lambda x: x.created_at)
        ]

    def get_trial(self, batch_id: str, trial_id: str) -> dict[str, Any]:
        batch = self.store.get_batch(batch_id)
        trial = batch.trials.get(trial_id)
        if trial is None:
            raise NotFoundError(f"试算 {trial_id} 不存在")
        return trial.to_dict()

    def compare_trials(self, batch_id: str, trial_ids: list[str] | None = None) -> dict[str, Any]:
        """并排比较多套试算，便于方案决策。"""
        batch = self.store.get_batch(batch_id)
        ids = trial_ids or sorted(batch.trials, key=lambda tid: batch.trials[tid].created_at)
        if not ids:
            raise ValidationError("没有可比较的试算")
        plans: dict[str, dict[str, int]] = {}
        summaries = []
        for tid in ids:
            trial = batch.trials.get(tid)
            if trial is None:
                raise NotFoundError(f"试算 {tid} 不存在")
            summary = self._trial_summary(trial)
            summaries.append({"id": tid, "label": trial.label, **summary})
            # 同一院校可能有多条专业方向申报，名额须累加而非覆盖
            plan: dict[str, int] = {}
            for line in trial.result.lines:
                plan[line.institution_id] = plan.get(line.institution_id, 0) + line.allocated
            plans[tid] = plan
        institution_ids = sorted(
            {iid for plan in plans.values() for iid in plan}
            | {iid for iid in batch.institutions}
        )
        matrix = [
            {
                "institution_id": iid,
                "institution_name": batch.institutions[iid].name,
                "allocations": {tid: plans[tid].get(iid, 0) for tid in ids},
            }
            for iid in institution_ids if iid in batch.institutions
        ]
        return {"trials": summaries, "comparison_matrix": matrix}

    @staticmethod
    def _trial_summary(trial: Trial) -> dict[str, Any]:
        result = trial.result
        status_count: dict[str, int] = {}
        for line in result.lines:
            status_count[line.status] = status_count.get(line.status, 0) + 1
        return {
            "feasible": result.feasible,
            "total_capacity": result.total_capacity,
            "total_allocated": result.total_allocated,
            "vacant_seats": result.total_capacity - result.total_allocated,
            "shortfall_count": len(result.shortfalls),
            "status_count": status_count,
            "params": trial.params,
        }

    # ------------------------------------------------------------------ #
    # 方案发布：冻结输入、初始额度入账
    # ------------------------------------------------------------------ #
    def publish(self, batch_id: str, trial_id: str) -> dict[str, Any]:
        with self.store.transaction() as raw:
            batch = Batch.from_dict(self.store.get_batch_raw(raw, batch_id))
            self._require_status(batch, BatchStatus.DRAFT, BatchStatus.TRIAL)
            trial = batch.trials.get(trial_id)
            if trial is None:
                raise NotFoundError(f"试算 {trial_id} 不存在")
            if not trial.result.feasible:
                raise ConflictError(
                    "试算存在未落实的最低保障缺口，方案不可发布："
                    + "；".join(
                        f"{s.dimension}={s.dim_value} 缺口 {s.missing} 人（院校 {s.institution_id}）"
                        for s in trial.result.shortfalls
                    )
                )
            batch.status = BatchStatus.PUBLISHED.value
            batch.published_trial_id = trial_id
            batch.published_result = trial.result
            batch.published_at = now_iso()
            batch.ledger_seq = 0
            batch.ledger = []
            for line in sorted(trial.result.lines, key=lambda x: x.application_id):
                if line.allocated <= 0:
                    continue
                app = batch.applications[line.application_id]
                source = EntrySource.GUARANTEE.value if line.allocated == line.guaranteed and line.guaranteed else EntrySource.INITIAL.value
                reason = "方案发布初始额度"
                if line.guaranteed:
                    reason += f"（含最低保障 {line.guaranteed} 人）"
                self._add_entry(
                    batch,
                    institution_id=line.institution_id,
                    major=app.major,
                    amount=line.allocated,
                    source=source,
                    reason=reason,
                    ref=line.application_id,
                )
            self._save(raw, batch)
            return {
                "id": batch.id,
                "status": batch.status,
                "published_trial_id": trial_id,
                "published_at": batch.published_at,
                "initial_allocated": trial.result.total_allocated,
                "vacant_seats": trial.result.total_capacity - trial.result.total_allocated,
                "version": batch.version,
            }

    # ------------------------------------------------------------------ #
    # 确认、放弃、撤销、转让：全部通过额度分录调整
    # ------------------------------------------------------------------ #
    def confirm(
        self,
        batch_id: str,
        application_id: str,
        accepted: bool,
        seats: int | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        with self.store.transaction() as raw:
            batch = Batch.from_dict(self.store.get_batch_raw(raw, batch_id))
            self._require_status(
                batch,
                BatchStatus.PUBLISHED,
                BatchStatus.CONFIRMING,
                BatchStatus.BACKFILLING,
            )
            app = self._get_application(batch, application_id)
            inst = self._get_institution(batch, app.institution_id)
            if not inst.eligible:
                raise ConflictError(f"院校 {inst.name} 资格已被撤销，不能确认")
            balances = self.balances(batch)
            current = balances.get(application_id, 0)
            if current <= 0:
                raise ConflictError("该申报当前没有可确认的名额")
            if application_id in batch.confirmations:
                raise ConflictError("该申报已完成确认，不能重复确认")

            accept_seats = current if accepted and seats is None else (seats or 0)
            if accepted:
                if accept_seats <= 0 or accept_seats > current:
                    raise ValidationError(f"确认名额须在 1..{current} 之间")
            else:
                accept_seats = 0

            released = current - accept_seats
            if released > 0:
                self._add_entry(
                    batch,
                    institution_id=inst.id,
                    major=app.major,
                    amount=-released,
                    source=EntrySource.DECLINE_RELEASE.value,
                    reason=(
                        f"院校确认放弃 {released} 个名额"
                        if accept_seats == 0
                        else f"院校仅确认 {accept_seats}/{current} 人，少确认 {released} 人"
                    ),
                    ref=application_id,
                )
            batch.confirmations[application_id] = Confirmation(
                application_id=application_id,
                institution_id=inst.id,
                confirmed=True,
                seats=accept_seats,
            )
            if batch.status == BatchStatus.PUBLISHED.value:
                batch.status = BatchStatus.CONFIRMING.value
            self._save(raw, batch, expected_version)
            return {
                "application_id": application_id,
                "accepted_seats": accept_seats,
                "released_seats": released,
                "balance": accept_seats,
                "released_pool": self._released_pool(batch),
                "version": batch.version,
            }

    def decline(
        self, batch_id: str, application_id: str, seats: int, reason: str = ""
    ) -> dict[str, Any]:
        """确认截止前主动放弃尚未确认的名额。"""
        if seats <= 0:
            raise ValidationError("放弃名额必须为正整数")
        with self.store.transaction() as raw:
            batch = Batch.from_dict(self.store.get_batch_raw(raw, batch_id))
            self._require_status(batch, BatchStatus.PUBLISHED, BatchStatus.CONFIRMING)
            app = self._get_application(batch, application_id)
            inst = self._get_institution(batch, app.institution_id)
            current = self.balances(batch).get(application_id, 0)
            if seats > current:
                raise ConflictError(f"当前余额仅 {current} 个，最多放弃 {current} 个")
            self._add_entry(
                batch,
                institution_id=inst.id,
                major=app.major,
                amount=-seats,
                source=EntrySource.DECLINE_RELEASE.value,
                reason=f"院校主动放弃 {seats} 个名额" + (f"：{reason.strip()}" if reason.strip() else ""),
                ref=application_id,
            )
            self._clamp_confirmation(batch, application_id, current - seats)
            self._save(raw, batch)
            return {
                "application_id": application_id,
                "released_seats": seats,
                "released_pool": self._released_pool(batch),
                "version": batch.version,
            }

    def revoke_eligibility(
        self, batch_id: str, institution_id: str, reason: str
    ) -> dict[str, Any]:
        """资格撤销：院校全部正式名额立即通过撤销分录释放。"""
        with self.store.transaction() as raw:
            batch = Batch.from_dict(self.store.get_batch_raw(raw, batch_id))
            self._require_status(
                batch,
                BatchStatus.PUBLISHED,
                BatchStatus.CONFIRMING,
                BatchStatus.BACKFILLING,
            )
            inst = self._get_institution(batch, institution_id)
            if not inst.eligible:
                raise ConflictError(f"院校 {inst.name} 已处于资格撤销状态")
            balances = self.balances(batch)
            released_total = 0
            for app in batch.applications.values():
                if app.institution_id != institution_id:
                    continue
                seats = balances.get(app.id, 0)
                if seats > 0:
                    self._add_entry(
                        batch,
                        institution_id=inst.id,
                        major=app.major,
                        amount=-seats,
                        source=EntrySource.REVOKE_RELEASE.value,
                        reason=f"资格撤销，强制释放 {seats} 个名额：{reason.strip() or '未注明原因'}",
                        ref=app.id,
                    )
                    released_total += seats
                # 撤销覆盖此前确认
                batch.confirmations.pop(app.id, None)
            inst.eligible = False
            inst.ineligible_reason = reason.strip() or "资格在批次执行期间被撤销"
            self._save(raw, batch)
            return {
                "institution_id": institution_id,
                "released_seats": released_total,
                "released_pool": self._released_pool(batch),
                "version": batch.version,
            }

    def transfer(
        self,
        batch_id: str,
        *,
        from_application_id: str,
        to_application_id: str,
        seats: int,
        reason: str = "",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """机构间转让：划出/划入成对分录，三维容量与承接方需求上限同时校验，杜绝超发。"""
        if seats <= 0:
            raise ValidationError("转让名额必须为正整数")
        with self.store.transaction() as raw:
            batch = Batch.from_dict(self.store.get_batch_raw(raw, batch_id))
            self._require_status(
                batch,
                BatchStatus.PUBLISHED,
                BatchStatus.CONFIRMING,
                BatchStatus.BACKFILLING,
            )
            src_app = self._get_application(batch, from_application_id)
            dst_app = self._get_application(batch, to_application_id)
            src_inst = self._get_institution(batch, src_app.institution_id)
            dst_inst = self._get_institution(batch, dst_app.institution_id)
            if src_app.institution_id == dst_app.institution_id:
                raise ValidationError("转让双方必须是不同院校")
            if not src_inst.eligible or not dst_inst.eligible:
                raise ConflictError("转让双方院校均须具备资格")
            if src_app.major != dst_app.major:
                raise ValidationError("机构间转让只能在同一专业方向内进行")
            if to_application_id in batch.confirmations and batch.confirmations[to_application_id].seats > dst_app.seats:
                raise ConflictError("承接方该方向已确认名额超过其申报需求")

            balances = self.balances(batch)
            src_balance = balances.get(from_application_id, 0)
            dst_balance = balances.get(to_application_id, 0)
            if seats > src_balance:
                raise ConflictError(f"划出方当前余额仅 {src_balance} 个，最多转让 {src_balance} 个")
            if dst_balance + seats > dst_app.seats:
                raise ConflictError(
                    f"承接方申报需求 {dst_app.seats} 人，当前 {dst_balance} 人，"
                    f"最多再接收 {max(0, dst_app.seats - dst_balance)} 人"
                )

            # 以“划出后 + 划入”的净占用校验：同专业转让的 major 维度净变化为 0。
            usage = self._usage(batch, balances)
            caps = self._caps(batch)
            projected = dict(usage)
            for key in (
                ("country", src_inst.country),
                ("type", src_inst.institution_type),
                ("major", src_app.major),
            ):
                projected[key] = projected.get(key, 0) - seats
            for key in (
                ("country", dst_inst.country),
                ("type", dst_inst.institution_type),
                ("major", dst_app.major),
            ):
                projected[key] = projected.get(key, 0) + seats
            for key, value in projected.items():
                if key in caps and value > caps[key]:
                    raise ConflictError(
                        f"转让后维度 {key[0]}={key[1]} 占用将达 {value}，"
                        f"超过容量 {caps[key]}，转让被拦截"
                    )

            note = reason.strip()
            self._add_entry(
                batch,
                institution_id=src_inst.id,
                major=src_app.major,
                amount=-seats,
                source=EntrySource.TRANSFER_OUT.value,
                reason=f"向 {dst_inst.name} 划出 {seats} 个名额" + (f"：{note}" if note else ""),
                ref=from_application_id,
            )
            self._add_entry(
                batch,
                institution_id=dst_inst.id,
                major=dst_app.major,
                amount=seats,
                source=EntrySource.TRANSFER_IN.value,
                reason=f"接收 {src_inst.name} 划入 {seats} 个名额" + (f"：{note}" if note else ""),
                ref=to_application_id,
            )
            self._clamp_confirmation(batch, from_application_id, src_balance - seats)
            self._save(raw, batch, expected_version)
            return {
                "from_application_id": from_application_id,
                "to_application_id": to_application_id,
                "seats": seats,
                "balances": {
                    from_application_id: src_balance - seats,
                    to_application_id: dst_balance + seats,
                },
                "version": batch.version,
            }

    # ------------------------------------------------------------------ #
    # 候补递补
    # ------------------------------------------------------------------ #
    def start_backfill(self, batch_id: str) -> dict[str, Any]:
        with self.store.transaction() as raw:
            batch = Batch.from_dict(self.store.get_batch_raw(raw, batch_id))
            self._require_status(
                batch,
                BatchStatus.PUBLISHED,
                BatchStatus.CONFIRMING,
                BatchStatus.BACKFILLING,
            )
            batch.status = BatchStatus.BACKFILLING.value
            self._save(raw, batch)
        return self.run_backfill(batch_id)

    def run_backfill(self, batch_id: str) -> dict[str, Any]:
        """按候补顺位递补全部当前可释放名额；无人承接则回收入机动名额池。

        处于分配/确认态时自动转入递补态，便于在最后一次放弃或撤销后直接补录。
        """
        promotions: list[dict[str, Any]] = []
        with self.store.transaction() as raw:
            batch = Batch.from_dict(self.store.get_batch_raw(raw, batch_id))
            self._require_status(
                batch,
                BatchStatus.PUBLISHED,
                BatchStatus.CONFIRMING,
                BatchStatus.BACKFILLING,
            )
            batch.status = BatchStatus.BACKFILLING.value
            assert batch.published_result is not None

            waitlist = sorted(
                (
                    line for line in batch.published_result.lines
                    if line.waitlist_rank is not None
                ),
                key=lambda x: (x.waitlist_rank, x.application_id),
            )

            while True:
                pool = self._released_pool(batch)
                if pool <= 0:
                    break
                balances = self.balances(batch)
                usage = self._usage(batch, balances)
                caps = self._caps(batch)
                picked = None
                for line in waitlist:
                    app = batch.applications.get(line.application_id)
                    inst = batch.institutions.get(line.institution_id) if app else None
                    if app is None or inst is None:
                        continue
                    blockers = self._candidate_blockers(
                        batch, line, app, inst, balances, usage, caps
                    )
                    if not blockers:
                        picked = (line, app, inst)
                        break
                if picked is None:
                    # 无合格承接对象：尾差名额落到可解释的机动名额池
                    reason = self._why_no_candidate(batch, waitlist, balances, usage, caps)
                    self._add_entry(
                        batch,
                        institution_id=VACANT_HOLDER,
                        major="*",
                        amount=pool,
                        source=EntrySource.VACANT_RECOVERY.value,
                        reason=reason,
                        ref="",
                    )
                    batch.vacant_records.append(
                        TailRecord(
                            seats=pool,
                            holder=VACANT_HOLDER,
                            holder_name=VACANT_HOLDER_NAME,
                            reason=reason,
                        )
                    )
                    break

                line, app, inst = picked
                self._add_entry(
                    batch,
                    institution_id=inst.id,
                    major=app.major,
                    amount=1,
                    source=EntrySource.WAITLIST_PROMOTION.value,
                    reason=(
                        f"候补第 {line.waitlist_rank} 位递补入选 1 人"
                        f"（多维容量校验通过）"
                    ),
                    ref=app.id,
                )
                batch.promoted.add(app.id)
                promotions.append(
                    {
                        "application_id": app.id,
                        "institution_id": inst.id,
                        "waitlist_rank": line.waitlist_rank,
                        "seats": 1,
                    }
                )
            self._save(raw, batch)
            return {
                "status": batch.status,
                "promotions": promotions,
                "released_pool": self._released_pool(batch),
                "vacant_records": [r.to_dict() for r in batch.vacant_records],
                "version": batch.version,
            }

    def _candidate_blockers(
        self,
        batch: Batch,
        line: Any,
        app: Application,
        inst: Institution,
        balances: dict[str, int],
        usage: dict[tuple[str, str], int],
        caps: dict[tuple[str, str], int],
    ) -> list[str]:
        """返回某候补行当前不能承接 1 个名额的全部原因；空列表表示可承接。

        选择递补对象与生成“为何无人承接”的解释共用本方法，保证口径一致。
        """
        blockers: list[str] = []
        if not inst.eligible:
            blockers.append("资格已撤销")
            return blockers
        current = balances.get(app.id, 0)
        # 已确认的申报以确认数为有效需求上限：自己放弃的名额不能递补回自己
        confirmation = batch.confirmations.get(app.id)
        if confirmation is not None:
            effective_demand = confirmation.seats
            demand_note = f"仅确认接受 {confirmation.seats} 人"
        else:
            effective_demand = app.seats
            demand_note = f"申报需求 {app.seats} 人"
        if current >= effective_demand:
            blockers.append(f"有效需求已满足（当前 {current} 人，{demand_note}）")
            return blockers
        for key in (
            ("country", inst.country),
            ("type", inst.institution_type),
            ("major", app.major),
        ):
            if key in caps and usage.get(key, 0) + 1 > caps[key]:
                blockers.append(f"维度 {key[0]}={key[1]} 容量 {caps[key]} 已满")
        return blockers

    def _why_no_candidate(
        self,
        batch: Batch,
        waitlist: list,
        balances: dict[str, int],
        usage: dict[tuple[str, str], int],
        caps: dict[tuple[str, str], int],
    ) -> str:
        """无任何候补可承接时，逐位说明每个候补被挡下的原因。"""
        details: list[str] = []
        for line in waitlist:
            app = batch.applications.get(line.application_id)
            inst = batch.institutions.get(line.institution_id) if app else None
            if app is None or inst is None:
                continue
            blockers = self._candidate_blockers(
                batch, line, app, inst, balances, usage, caps
            )
            if blockers:
                details.append(
                    f"候补第 {line.waitlist_rank} 位（{inst.name}/{app.major}）："
                    + "、".join(blockers)
                )
        if not details:
            return "释放名额无合格承接对象，回收为机动名额：候补队列已无未满足的申报"
        return "释放名额无合格承接对象，回收为机动名额：" + "；".join(details)

    def close_batch(self, batch_id: str) -> dict[str, Any]:
        with self.store.transaction() as raw:
            batch = Batch.from_dict(self.store.get_batch_raw(raw, batch_id))
            self._require_status(
                batch,
                BatchStatus.PUBLISHED,
                BatchStatus.CONFIRMING,
                BatchStatus.BACKFILLING,
            )
            if self._released_pool(batch) > 0:
                raise ConflictError("仍有释放名额未递补或回收，请先执行递补")
            batch.status = BatchStatus.CLOSED.value
            self._save(raw, batch)
            return {"id": batch.id, "status": batch.status, "version": batch.version}

    # ------------------------------------------------------------------ #
    # 查询视图：余额、分录、每个机构都能看到入选/未入选原因
    # ------------------------------------------------------------------ #
    def ledger(self, batch_id: str) -> dict[str, Any]:
        batch = self.store.get_batch(batch_id)
        balances = self.balances(batch)
        rows = []
        for app_id, seats in sorted(balances.items()):
            app = batch.applications.get(app_id)
            if app is None:
                continue
            inst = batch.institutions[app.institution_id]
            rows.append(
                {
                    "application_id": app_id,
                    "institution_id": inst.id,
                    "institution_name": inst.name,
                    "major": app.major,
                    "balance": seats,
                }
            )
        return {
            "entries": [e.to_dict() for e in sorted(batch.ledger, key=lambda x: x.seq)],
            "balances": rows,
            "released_pool": self._released_pool(batch),
        }

    def institution_view(self, batch_id: str, institution_id: str) -> dict[str, Any]:
        """院校视角：正式方案中该院校每行申报的入选/候补/未入选结论与完整原因。"""
        batch = self.store.get_batch(batch_id)
        inst = self._get_institution(batch, institution_id)
        balances = self.balances(batch)
        lines: list[dict[str, Any]] = []
        if batch.published_result is not None:
            for line in batch.published_result.lines:
                if line.institution_id != institution_id:
                    continue
                app = batch.applications.get(line.application_id)
                view = line.to_dict()
                view["current_balance"] = balances.get(line.application_id, 0)
                confirmation = batch.confirmations.get(line.application_id)
                view["confirmed_seats"] = confirmation.seats if confirmation else None
                view["promoted"] = line.application_id in batch.promoted
                # 运行期状态覆盖发布状态
                if not inst.eligible:
                    view["live_status"] = "资格撤销"
                elif line.application_id in batch.promoted and view["current_balance"] >= (app.seats if app else 0):
                    view["live_status"] = LineStatus.SELECTED.value + "（递补足额）"
                elif line.application_id in batch.promoted:
                    view["live_status"] = LineStatus.PARTIAL.value + "（递补中）"
                else:
                    view["live_status"] = line.status
                lines.append(view)
        return {
            "batch_id": batch.id,
            "batch_status": batch.status,
            "institution": inst.to_dict(),
            "lines": lines,
            "vacant_records": [r.to_dict() for r in batch.vacant_records],
        }

    def _batch_view(self, batch: Batch) -> dict[str, Any]:
        data = batch.to_dict()
        data["released_pool"] = self._released_pool(batch) if batch.published_result else 0
        data["trial_count"] = len(batch.trials)
        return data
