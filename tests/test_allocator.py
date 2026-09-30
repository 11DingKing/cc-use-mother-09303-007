"""分配引擎的算法回归测试。"""
from __future__ import annotations

import sys
import unittest
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quota_service.allocator import allocate
from quota_service.models import (
    Application,
    BatchSnapshot,
    Dimension,
    DimensionCapacity,
    DimensionKey,
    GuaranteeRule,
    QuotaError,
)


def make_app(iid: str, demand: int, *, country="泰国", itype="综合性大学",
             specialty="新能源", eligible=True, history=0, priority=0) -> Application:
    return Application(
        institution_id=iid, name=f"院校{iid}", country=country,
        institution_type=itype, specialty=specialty, demand=demand,
        eligible=eligible, history=Fraction(history), priority=priority,
    )


def snapshot(apps, total=100, capacities=(), guarantees=(), input_version=1) -> BatchSnapshot:
    return BatchSnapshot(
        total_capacity=total,
        capacities=tuple(capacities),
        guarantees=tuple(guarantees),
        applications=tuple(apps),
        input_version=input_version,
    )


class BasicAllocationTest(unittest.TestCase):
    def test_within_capacity_and_demand(self) -> None:
        apps = [make_app("A", 3), make_app("B", 4), make_app("C", 5)]
        r = allocate(snapshot(apps, total=100))
        self.assertEqual(r.total_selected, 12)
        self.assertEqual({k: r.selected[k] for k in "ABC"}, {"A": 3, "B": 4, "C": 5})
        self.assertTrue(all(v == "selected" for v in r.status.values()))

    def test_total_never_exceeds_capacity(self) -> None:
        apps = [make_app("A", 10), make_app("B", 10), make_app("C", 10)]
        r = allocate(snapshot(apps, total=10))
        self.assertEqual(sum(r.selected.values()), 10)
        self.assertLessEqual(r.total_selected, 10)

    def test_hamilton_largest_remainder_distribution(self) -> None:
        # 10 席 / 3 所同分院校 => 4,3,3，尾差 1 席给编号最小者
        apps = [make_app("A", 10), make_app("B", 10), make_app("C", 10)]
        r = allocate(snapshot(apps, total=10))
        self.assertEqual(r.selected["A"], 4)
        self.assertEqual(r.selected["B"], 3)
        self.assertEqual(r.selected["C"], 3)
        traces = [t for t in r.rounding_traces if t.awarded == 1 and t.dimension is None]
        self.assertEqual([t.institution_id for t in traces], ["A"])
        self.assertEqual(traces[0].exact_share, "10/3")
        self.assertEqual(traces[0].rounded_floor, 3)

    def test_every_rounding_remainder_lands_on_someone(self) -> None:
        # 7 席 / 3 所 => 7/3 = 2 又 1/3，一个尾差名额必须可解释
        apps = [make_app("A", 10), make_app("B", 10), make_app("C", 10)]
        r = allocate(snapshot(apps, total=7))
        awarded = sum(t.awarded for t in r.rounding_traces if t.dimension is None and t.awarded == 1)
        self.assertEqual(sum(r.selected.values()), 7)
        self.assertEqual(awarded, 1)
        for inst in "ABC":
            self.assertTrue(r.reasons[inst], f"{inst} 必须有入选原因")


class HistoryCorrectionTest(unittest.TestCase):
    def test_high_history_gets_lower_share(self) -> None:
        # 权重 1 与 1/3，4 席 => 新院校 3，老院校 1
        apps = [make_app("NEW", 10, history=0), make_app("OLD", 10, history=2)]
        r = allocate(snapshot(apps, total=4))
        self.assertEqual(r.selected["NEW"], 3)
        self.assertEqual(r.selected["OLD"], 1)

    def test_history_breaks_waitlist_tie(self) -> None:
        apps = [make_app("OLD", 10, history=3), make_app("NEW", 10, history=0)]
        r = allocate(snapshot(apps, total=1))
        self.assertEqual(r.selected["NEW"], 1)
        self.assertEqual(r.selected["OLD"], 0)
        self.assertEqual(r.waitlist[0], "OLD")  # 老院校零名额，排在候补最前
        self.assertIn("NEW", r.waitlist)        # 新院校仍有缺口，随后候补
        # OLD 零名额，其原因中要点名历史参与系数
        self.assertTrue(any("历史参与系数" in x for x in r.reasons["OLD"]))

    def test_fractional_history_coefficient(self) -> None:
        apps = [make_app("A", 10, history=Fraction(1, 2)),
                make_app("B", 10, history=Fraction(1, 2))]
        r = allocate(snapshot(apps, total=3))
        self.assertEqual(sum(r.selected.values()), 3)
        self.assertEqual(set(r.selected.values()), {1, 2})


class GuaranteeTest(unittest.TestCase):
    def test_guarantee_reserves_seats_for_small_colleges(self) -> None:
        apps = [
            make_app("BIG", 10, itype="综合性大学"),
            make_app("S1", 5, itype="小型学院"),
            make_app("S2", 5, itype="小型学院"),
        ]
        rule = GuaranteeRule(DimensionKey(Dimension.INSTITUTION_TYPE, "小型学院"), 3)
        r = allocate(snapshot(apps, total=5, guarantees=[rule]))
        self.assertGreaterEqual(r.selected["S1"] + r.selected["S2"], 3)
        self.assertEqual(r.total_selected, 5)
        self.assertTrue(any("最低保障预留" in x for x in r.reasons["S1"] + r.reasons["S2"]))

    def test_guarantee_demand_shortfall_is_reported_not_invented(self) -> None:
        apps = [make_app("S1", 1, itype="小型学院")]
        rule = GuaranteeRule(DimensionKey(Dimension.INSTITUTION_TYPE, "小型学院"), 4)
        r = allocate(snapshot(apps, total=10, guarantees=[rule]))
        self.assertEqual(r.selected["S1"], 1)  # 不能凭空造名额
        self.assertEqual(len(r.guarantee_shortfalls), 1)
        sf = r.guarantee_shortfalls[0]
        self.assertEqual(sf["guaranteed"], 4)
        self.assertEqual(sf["filled"], 1)
        self.assertIn("需求不足", sf["reason"])

    def test_guarantee_limited_by_total_capacity(self) -> None:
        apps = [make_app("S1", 10, country="越南"), make_app("S2", 10, country="越南")]
        rule = GuaranteeRule(DimensionKey(Dimension.COUNTRY, "越南"), 5)
        r = allocate(snapshot(apps, total=3, guarantees=[rule]))
        self.assertEqual(r.total_selected, 3)
        self.assertEqual(r.guarantee_shortfalls[0]["filled"], 3)
        self.assertIn("容量限制", r.guarantee_shortfalls[0]["reason"])

    def test_guarantee_exceeding_dimension_capacity_rejected(self) -> None:
        cap = DimensionCapacity(DimensionKey(Dimension.COUNTRY, "越南"), 2)
        rule = GuaranteeRule(DimensionKey(Dimension.COUNTRY, "越南"), 5)
        with self.assertRaises(QuotaError) as ctx:
            allocate(snapshot([make_app("S1", 10, country="越南")],
                              total=10, capacities=[cap], guarantees=[rule]))
        self.assertEqual(ctx.exception.code, "GUARANTEE_EXCEEDS_CAPACITY")


class DimensionCapacityTest(unittest.TestCase):
    def test_dimension_capacity_enforced(self) -> None:
        apps = [
            make_app("T1", 10, country="泰国"),
            make_app("T2", 10, country="泰国"),
            make_app("V1", 10, country="越南"),
        ]
        cap = DimensionCapacity(DimensionKey(Dimension.COUNTRY, "泰国"), 2)
        r = allocate(snapshot(apps, total=10, capacities=[cap]))
        self.assertEqual(r.selected["T1"] + r.selected["T2"], 2)
        self.assertEqual(r.dimension_usage["country:泰国"], 2)
        self.assertEqual(r.total_selected, 10)

    def test_multi_dimension_caps_intersect(self) -> None:
        apps = [
            make_app("T1", 10, country="泰国", itype="小型学院", specialty="新能源"),
            make_app("V1", 10, country="越南", itype="综合性大学", specialty="农业"),
        ]
        caps = [
            DimensionCapacity(DimensionKey(Dimension.COUNTRY, "泰国"), 1),
            DimensionCapacity(DimensionKey(Dimension.SPECIALTY, "新能源"), 1),
        ]
        r = allocate(snapshot(apps, total=10, capacities=caps))
        self.assertLessEqual(r.selected["T1"], 1)

    def test_unfilled_explained_when_dimension_blocks(self) -> None:
        apps = [
            make_app("T1", 10, country="泰国"),
            make_app("V1", 10, country="越南"),
        ]
        cap = DimensionCapacity(DimensionKey(Dimension.COUNTRY, "越南"), 0)
        r = allocate(snapshot(apps, total=10, capacities=[cap]))
        self.assertEqual(r.total_selected, 10)
        self.assertEqual(len(r.unfilled), 0)

    def test_unfilled_reported_with_reason(self) -> None:
        # 总容量 10，两所都属饱和维度（各 cap 1），总需求 20；
        # 维度约束下只能放 2 席，其余 8 席归因明确
        apps = [
            make_app("T1", 10, country="泰国"),
            make_app("V1", 10, country="越南"),
        ]
        caps = [
            DimensionCapacity(DimensionKey(Dimension.COUNTRY, "泰国"), 1),
            DimensionCapacity(DimensionKey(Dimension.COUNTRY, "越南"), 1),
        ]
        r = allocate(snapshot(apps, total=10, capacities=caps))
        self.assertEqual(r.total_selected, 2)
        self.assertEqual(r.unfilled[0]["seats"], 8)
        self.assertIn("维度容量", r.unfilled[0]["reason"])
        # 零名额院校的原因里也要有同样的归因
        v1 = next(x for x in r.reasons["V1"] if "无法分配" in x)
        self.assertIsNotNone(v1)


class EligibilityTest(unittest.TestCase):
    def test_ineligible_rejected_with_reason(self) -> None:
        apps = [make_app("OK", 10), make_app("BAD", 10, eligible=False)]
        r = allocate(snapshot(apps, total=10))
        self.assertEqual(r.selected["BAD"], 0)
        self.assertEqual(r.status["BAD"], "rejected")
        self.assertIn("不具备入选资格", r.reasons["BAD"][0])
        self.assertEqual(r.selected["OK"], 10)

    def test_duplicate_application_rejected(self) -> None:
        apps = [make_app("A", 1), make_app("A", 1)]
        with self.assertRaises(QuotaError) as ctx:
            allocate(snapshot(apps, total=2))
        self.assertEqual(ctx.exception.code, "DUPLICATE_APPLICATION")


class WaitlistTest(unittest.TestCase):
    def test_waitlist_order_and_positions(self) -> None:
        apps = [make_app("A", 10, priority=0),
                make_app("B", 10, priority=0),
                make_app("C", 10, priority=0)]
        r = allocate(snapshot(apps, total=2))
        # A、B 各 1 席（部分满足），C 零名额；零名额者排在候补最前
        self.assertEqual(r.waitlist, ["C", "A", "B"])
        self.assertEqual(r.waitlist_position["C"], 1)
        self.assertEqual(r.status["C"], "waitlisted")
        self.assertEqual(r.status["A"], "partially_selected")

    def test_each_institution_gets_explanation(self) -> None:
        apps = [make_app("A", 10), make_app("B", 10), make_app("C", 10)]
        r = allocate(snapshot(apps, total=2))
        for inst, reasons in r.reasons.items():
            self.assertTrue(reasons, f"{inst} 缺少原因说明")
            self.assertTrue(any(str(r.rank[inst]) in x or "满足" in x for x in reasons))


if __name__ == "__main__":
    unittest.main()
