"""额度分录账本测试：不超发、不出现负余额、转让守恒、维度约束。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quota_service.ledger import Ledger
from quota_service.models import (
    Application,
    BatchSnapshot,
    Dimension,
    DimensionCapacity,
    DimensionKey,
    LedgerEntryKind,
    QuotaError,
)


def make_app(iid: str, demand: int, *, country="泰国", itype="综合性大学", specialty="新能源"):
    return Application(iid, f"院校{iid}", country, itype, specialty, demand)


def snapshot(apps, total=10, capacities=()):
    return BatchSnapshot(total, tuple(capacities), (), tuple(apps), 1)


class LedgerSeedTest(unittest.TestCase):
    def test_seed_and_balances(self) -> None:
        apps = [make_app("A", 5), make_app("B", 5)]
        ledger = Ledger(snapshot(apps, total=8))
        ledger.seed_initial({"A": 4, "B": 4}, {"A": ["x"], "B": ["y"]}, 1)
        self.assertEqual(ledger.balances(), {"A": 4, "B": 4})
        self.assertEqual(ledger.total_held(), 8)
        self.assertTrue(all(e.kind == LedgerEntryKind.INITIAL for e in ledger.entries))

    def test_seed_oversell_rejected(self) -> None:
        apps = [make_app("A", 5)]
        ledger = Ledger(snapshot(apps, total=3))
        with self.assertRaises(QuotaError) as ctx:
            ledger.seed_initial({"A": 4}, {}, 1)
        self.assertEqual(ctx.exception.code, "OVERSELL")
        self.assertEqual(ledger.entries, [])  # 未落任何账


class RelinquishRevokeTest(unittest.TestCase):
    def _seeded(self):
        apps = [make_app("A", 5), make_app("B", 5)]
        ledger = Ledger(snapshot(apps, total=10))
        ledger.seed_initial({"A": 5, "B": 5}, {}, 1)
        return apps, ledger

    def test_relinquish_cannot_exceed_holding(self) -> None:
        _, ledger = self._seeded()
        with self.assertRaises(QuotaError) as ctx:
            ledger.relinquish("A", 6, "多放弃", 2)
        self.assertEqual(ctx.exception.code, "NEGATIVE_BALANCE")
        self.assertEqual(ledger.balances()["A"], 5)

    def test_relinquish_then_promote_conserves(self) -> None:
        _, ledger = self._seeded()
        e1 = ledger.relinquish("A", 2, "放弃2席", 2)
        self.assertEqual(e1.delta, -2)
        e2 = ledger.promote("B", 2, 1, "递补", 3)
        self.assertEqual(e2.kind, LedgerEntryKind.WAITLIST_PROMOTE)
        self.assertEqual(ledger.total_held(), 10)

    def test_revoke_takes_all(self) -> None:
        _, ledger = self._seeded()
        e = ledger.revoke("B", "资格撤销", 2)
        self.assertEqual(e.delta, -5)
        self.assertEqual(ledger.balances()["B"], 0)
        self.assertEqual(ledger.total_held(), 5)

    def test_revoke_with_nothing(self) -> None:
        apps = [make_app("A", 5), make_app("C", 5)]
        ledger = Ledger(snapshot(apps, total=5))
        ledger.seed_initial({"A": 5}, {}, 1)
        with self.assertRaises(QuotaError) as ctx:
            ledger.revoke("C", "撤销", 2)
        self.assertEqual(ctx.exception.code, "NOTHING_TO_REVOKE")


class TransferTest(unittest.TestCase):
    def test_transfer_pair_conserves_and_links(self) -> None:
        apps = [make_app("A", 5), make_app("B", 5)]
        ledger = Ledger(snapshot(apps, total=10))
        ledger.seed_initial({"A": 5, "B": 3}, {}, 1)
        entries = ledger.transfer("A", "B", 2, "校际支援", 2, True)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].ref, entries[1].ref)
        self.assertEqual(entries[0].counterparty, "B")
        self.assertEqual(ledger.balances(), {"A": 3, "B": 5})
        self.assertEqual(ledger.total_held(), 8)

    def test_transfer_cannot_overdraw_sender(self) -> None:
        apps = [make_app("A", 5), make_app("B", 10)]
        ledger = Ledger(snapshot(apps, total=6))
        ledger.seed_initial({"A": 1, "B": 5}, {}, 1)
        with self.assertRaises(QuotaError) as ctx:
            ledger.transfer("A", "B", 2, "", 2, True)
        self.assertEqual(ctx.exception.code, "NEGATIVE_BALANCE")

    def test_transfer_cannot_exceed_receiver_demand(self) -> None:
        apps = [make_app("A", 5), make_app("B", 2)]
        ledger = Ledger(snapshot(apps, total=7))
        ledger.seed_initial({"A": 5, "B": 2}, {}, 1)
        with self.assertRaises(QuotaError) as ctx:
            ledger.transfer("A", "B", 1, "", 2, True)
        self.assertEqual(ctx.exception.code, "TRANSFER_EXCEEDS_DEMAND")

    def test_transfer_to_ineligible_rejected(self) -> None:
        apps = [Application("A", "甲", "泰国", "综合", "新能源", 5),
                Application("B", "乙", "泰国", "综合", "新能源", 5, eligible=False)]
        ledger = Ledger(snapshot(apps, total=10))
        ledger.seed_initial({"A": 5}, {}, 1)
        with self.assertRaises(QuotaError) as ctx:
            ledger.transfer("A", "B", 1, "", 2, False)
        self.assertEqual(ctx.exception.code, "RECEIVER_INELIGIBLE")


class DimensionConstraintTest(unittest.TestCase):
    def test_promote_respects_dimension_capacity(self) -> None:
        apps = [make_app("T", 5, country="泰国"), make_app("V", 5, country="越南")]
        cap = DimensionCapacity(DimensionKey(Dimension.COUNTRY, "泰国"), 5)
        ledger = Ledger(snapshot(apps, total=10, capacities=[cap]))
        ledger.seed_initial({"T": 5, "V": 3}, {}, 1)
        with self.assertRaises(QuotaError) as ctx:
            ledger.promote("T", 1, 1, "超维度", 2)
        self.assertEqual(ctx.exception.code, "DIMENSION_OVERSELL")
        # 越南院校递补不受泰国容量影响
        ledger.promote("V", 2, 1, "正常递补", 2)
        self.assertEqual(ledger.total_held(), 10)


if __name__ == "__main__":
    unittest.main()
