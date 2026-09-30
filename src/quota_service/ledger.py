"""额度分录账本。

方案发布之后原始输入冻结，放弃、资格撤销、机构间转让、候补递补
都不得直接改写分配结果，而是追加不可变的带符号分录。账本在每次
追加时重算余额并校验总容量与各维度容量，从机制上保证：

* 任何机构余额不会为负（放弃/撤销不能超过其当前持有）；
* 全批次持有总量永远不超过批次总容量（不会超发）；
* 每个维度取值下的持有量不超过其维度容量；
* 转让由转出/转入两笔同号引用的分录构成，总量守恒。
"""
from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass

from .models import (
    Application,
    BatchSnapshot,
    Dimension,
    LedgerEntry,
    LedgerEntryKind,
    QuotaError,
)

DIMENSIONS = (Dimension.COUNTRY, Dimension.INSTITUTION_TYPE, Dimension.SPECIALTY)


class Ledger:
    def __init__(self, snapshot: BatchSnapshot) -> None:
        self._snapshot = snapshot
        self._entries: list[LedgerEntry] = []
        self._seq = itertools.count(1)

    # ----- 读视图 -----------------------------------------------------------

    @property
    def entries(self) -> list[LedgerEntry]:
        return list(self._entries)

    def balances(self) -> dict[str, int]:
        balances: dict[str, int] = {a.institution_id: 0 for a in self._snapshot.applications}
        for e in self._entries:
            balances[e.institution_id] = balances.get(e.institution_id, 0) + e.delta
        return balances

    def dimension_usage(self) -> dict[str, int]:
        """按当前余额重算每个维度取值的实际占用。"""
        balances = self.balances()
        usage: dict[str, int] = {}
        for app in self._snapshot.applications:
            n = balances.get(app.institution_id, 0)
            if n <= 0:
                continue
            for dim in DIMENSIONS:
                key = f"{dim.value}:{app.tag(dim)}"
                usage[key] = usage.get(key, 0) + n
        return usage

    def total_held(self) -> int:
        return sum(self.balances().values())

    # ----- 内部校验与追加 ----------------------------------------------------

    def _app(self, institution_id: str) -> Application:
        try:
            return self._snapshot.app(institution_id)
        except QuotaError:
            raise QuotaError("UNKNOWN_INSTITUTION", f"批次中不存在院校：{institution_id}") from None

    def _check_capacity(self, balances: dict[str, int], action: str) -> None:
        total = sum(balances.values())
        if total > self._snapshot.total_capacity:
            raise QuotaError(
                "OVERSELL",
                f"{action}后总入选 {total} 将超过批次总容量 {self._snapshot.total_capacity}",
            )
        caps = {(c.key.dimension, c.key.value): c.capacity for c in self._snapshot.capacities}
        for dim in DIMENSIONS:
            used: dict[str, int] = {}
            for app in self._snapshot.applications:
                n = balances.get(app.institution_id, 0)
                if n > 0:
                    used[app.tag(dim)] = used.get(app.tag(dim), 0) + n
            for value, n in used.items():
                cap = caps.get((dim, value))
                if cap is not None and n > cap:
                    raise QuotaError(
                        "DIMENSION_OVERSELL",
                        f"{action}后维度 {dim.value}={value} 占用 {n} 超过容量 {cap}",
                    )

    def _append(self, kind: LedgerEntryKind, institution_id: str, delta: int,
                reason: str, version: int, ref: str | None = None,
                counterparty: str | None = None) -> LedgerEntry:
        if delta == 0:
            raise QuotaError("INVALID_ENTRY", "分录变动数量不能为 0")
        entry = LedgerEntry(
            seq=next(self._seq),
            kind=kind,
            institution_id=institution_id,
            delta=delta,
            reason=reason,
            batch_version=version,
            ref=ref,
            counterparty=counterparty,
        )
        self._entries.append(entry)
        return entry

    def _apply(self, pending: list[tuple], action: str) -> None:
        """在副本余额上试算全部待写分录，任一违规则整体拒绝、不落账。"""
        balances = self.balances()
        for institution_id, delta in pending:
            balances[institution_id] = balances.get(institution_id, 0) + delta
            if balances[institution_id] < 0:
                current = self.balances().get(institution_id, 0)
                raise QuotaError(
                    "NEGATIVE_BALANCE",
                    f"{action}将使 {institution_id} 的持有名额为负（当前 {current}，变动 {delta}）",
                )
        self._check_capacity(balances, action)

    # ----- 发布初始化 -------------------------------------------------------

    def seed_initial(self, selected: dict[str, int], reasons: dict[str, list[str]],
                     version: int) -> list[LedgerEntry]:
        if self._entries:
            raise QuotaError("LEDGER_NOT_EMPTY", "正式额度分录已存在，不能重复初始化")
        written: list[LedgerEntry] = []
        pending: list[tuple[str, int]] = []
        for inst, n in selected.items():
            if n > 0:
                pending.append((inst, n))
        self._apply([(i, d) for i, d in pending], "方案发布")
        for inst, n in sorted(pending):
            why = "；".join(reasons.get(inst, [])) or "方案发布入选"
            written.append(self._append(LedgerEntryKind.INITIAL, inst, n, why, version))
        return written

    # ----- 发布后调整 --------------------------------------------------------

    def relinquish(self, institution_id: str, seats: int, reason: str, version: int) -> LedgerEntry:
        self._app(institution_id)
        if seats <= 0:
            raise QuotaError("INVALID_ENTRY", "放弃名额必须为正整数")
        self._apply([(institution_id, -seats)], "放弃")
        return self._append(
            LedgerEntryKind.RELINQUISH, institution_id, -seats,
            reason or "院校放弃名额", version,
        )

    def revoke(self, institution_id: str, reason: str, version: int) -> LedgerEntry:
        """资格撤销：收回该机构当前持有的全部名额。"""
        self._app(institution_id)
        held = self.balances().get(institution_id, 0)
        if held == 0:
            raise QuotaError("NOTHING_TO_REVOKE", f"{institution_id} 当前没有可撤销的名额")
        self._apply([(institution_id, -held)], "资格撤销")
        return self._append(
            LedgerEntryKind.REVOKE, institution_id, -held,
            reason or "资格撤销，收回全部名额", version,
        )

    def transfer(self, sender: str, receiver: str, seats: int, reason: str,
                 version: int, receiver_eligible: bool) -> list[LedgerEntry]:
        """机构间转让：转出与转入成对落账，引用号相同。"""
        self._app(sender)
        self._app(receiver)
        if sender == receiver:
            raise QuotaError("INVALID_TRANSFER", "不能向本机构转让")
        if seats <= 0:
            raise QuotaError("INVALID_TRANSFER", "转让名额必须为正整数")
        if not receiver_eligible:
            raise QuotaError("RECEIVER_INELIGIBLE", f"受让方 {receiver} 不具备资格，不能接收入选名额")
        receiver_app = self._app(receiver)
        held_receiver = self.balances().get(receiver, 0)
        if held_receiver + seats > receiver_app.demand:
            raise QuotaError(
                "TRANSFER_EXCEEDS_DEMAND",
                f"受让方 {receiver} 申请需求仅 {receiver_app.demand}，"
                f"当前持有 {held_receiver}，不能再接收 {seats}",
            )
        ref = f"tx-{uuid.uuid4().hex[:12]}"
        self._apply([(sender, -seats), (receiver, seats)], "机构间转让")
        why = reason or "机构间名额转让"
        out_e = self._append(LedgerEntryKind.TRANSFER_OUT, sender, -seats,
                             f"{why}（转让给 {receiver}）", version, ref=ref, counterparty=receiver)
        in_e = self._append(LedgerEntryKind.TRANSFER_IN, receiver, seats,
                            f"{why}（由 {sender} 转入）", version, ref=ref, counterparty=sender)
        return [out_e, in_e]

    def promote(self, institution_id: str, seats: int, waitlist_position: int,
                reason: str, version: int) -> LedgerEntry:
        self._app(institution_id)
        if seats <= 0:
            raise QuotaError("INVALID_ENTRY", "递补名额必须为正整数")
        self._apply([(institution_id, seats)], "候补递补")
        return self._append(
            LedgerEntryKind.WAITLIST_PROMOTE, institution_id, seats,
            reason or f"按候补顺序第 {waitlist_position} 位递补", version,
        )
