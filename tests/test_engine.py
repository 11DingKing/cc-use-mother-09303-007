"""分配引擎测试：最低保障、历史修正、最大余数尾差、多维容量与可行性。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quota_service.engine import VACANT_HOLDER, allocate
from quota_service.errors import ValidationError
from quota_service.models import (
    Application,
    CapacityRule,
    Guarantee,
    Institution,
    LineStatus,
)


def inst(id: str, *, country="泰国", typ="小型院校", majors=("护理",), base=0.0, hist=0.0, eligible=True):
    return Institution(
        id=id, name=id, country=country, institution_type=typ,
        majors=list(majors), eligible=eligible,
        ineligible_reason="" if eligible else "审核未过",
        base_score=base, history_score=hist,
    )


def app(id: str, iid: str, major="护理", seats=5):
    return Application(id=id, institution_id=iid, major=major, seats=seats)


class EngineTest(unittest.TestCase):
    def test_hamilton_largest_remainder_and_traceable_tail(self):
        institutions = {f"i{k}": inst(f"i{k}", base=score)
                        for k, score in enumerate((70, 20, 10, 0), start=1)}
        applications = {f"a{k}": app(f"a{k}", f"i{k}", seats=10) for k in range(1, 5)}
        result = allocate(institutions, applications, [], [], 8)
        by = {l.application_id: l for l in result.lines}
        # 比例份额 5.6 / 1.6 / 0.8 / 0.0 → 整 5+1+0+0=6，两个尾差按余数 0.8>0.6 给 i3、i1
        self.assertEqual([by[f"a{k}"].allocated for k in range(1, 5)], [6, 1, 1, 0])
        self.assertEqual(result.total_allocated, 8)
        self.assertFalse(result.tail_records)
        tail_traces = [t for l in result.lines for t in l.traces if t.source == "rounding"]
        self.assertEqual(len(tail_traces), 2)
        self.assertTrue(all("顺位" in t.reason for t in tail_traces))

    def test_history_penalty_changes_order(self):
        institutions = {
            "a": inst("a", base=90, hist=100),   # 修正后 0 分
            "b": inst("b", base=10, hist=0),     # 10 分
        }
        applications = {"x": app("x", "a", seats=10), "y": app("y", "b", seats=10)}
        result = allocate(institutions, applications, [], [], 4, {"history_penalty_weight": 1.0})
        by = {l.application_id: l.allocated for l in result.lines}
        self.assertGreater(by["y"], by["x"])
        # 权重 0 时反转
        result0 = allocate(institutions, applications, [], [], 4, {"history_penalty_weight": 0.0})
        by0 = {l.application_id: l.allocated for l in result0.lines}
        self.assertGreater(by0["x"], by0["y"])

    def test_minimum_guarantee_reserved_first(self):
        # 小型院校基础分为 0，若无保障将颗粒无收
        institutions = {"big": inst("big", typ="大型院校", base=100),
                        "small": inst("small", typ="小型院校", base=0)}
        applications = {"ab": app("ab", "big", seats=10), "as": app("as", "small", seats=10)}
        guarantees = [Guarantee(id="g1", dimension="type", dim_value="小型院校", min_seats=3)]
        result = allocate(institutions, applications, [], guarantees, 10)
        by = {l.application_id: l for l in result.lines}
        self.assertEqual(by["as"].guaranteed, 3)
        self.assertGreaterEqual(by["as"].allocated, 3)
        self.assertTrue(any("最低保障" in r for r in by["as"].reasons))

    def test_infeasible_when_capacity_cannot_cover_guarantee(self):
        institutions = {"s": inst("s")}
        applications = {"a": app("a", "s", seats=10)}
        caps = [CapacityRule(dimension="type", dim_value="小型院校", seats=2)]
        guarantees = [Guarantee(id="g1", dimension="type", dim_value="小型院校", min_seats=5)]
        result = allocate(institutions, applications, caps, guarantees, 10)
        self.assertFalse(result.feasible)
        self.assertEqual(result.shortfalls[0].missing, 3)

    def test_infeasible_when_total_below_guarantee(self):
        institutions = {"s": inst("s")}
        applications = {"a": app("a", "s", seats=10)}
        guarantees = [Guarantee(id="g1", dimension="type", dim_value="小型院校", min_seats=5)]
        result = allocate(institutions, applications, [], guarantees, 3)
        self.assertFalse(result.feasible)
        self.assertEqual(result.shortfalls[0].missing, 2)
        self.assertIn("总名额", result.shortfalls[0].reason)

    def test_dimension_caps_are_hard_constraints(self):
        institutions = {
            "t1": inst("t1", country="泰国", typ="大型院校", base=50),
            "t2": inst("t2", country="泰国", typ="小型院校", base=49),
            "v1": inst("v1", country="越南", typ="大型院校", base=48),
        }
        applications = {f"a{k}": app(f"a{k}", f"t{k}" if k < 3 else "v1", seats=10)
                        for k in range(1, 4)}
        caps = [CapacityRule(dimension="country", dim_value="泰国", seats=3)]
        result = allocate(institutions, applications, caps, [], 20)
        usage_th = result.dimension_usage["country"].get("泰国", 0)
        self.assertLessEqual(usage_th, 3)
        # 泰国仅 3 席，其余名额因维度阻挡落入机动名额池
        self.assertTrue(result.tail_records)
        self.assertEqual(result.tail_records[0].holder, VACANT_HOLDER)
        self.assertIn("country=泰国", result.tail_records[0].reason)

    def test_unplaced_seats_when_demand_below_capacity(self):
        institutions = {"i": inst("i", base=10)}
        applications = {"a": app("a", "i", seats=3)}
        result = allocate(institutions, applications, [], [], 10)
        self.assertEqual(result.total_allocated, 3)
        self.assertEqual(result.tail_records[0].seats, 7)
        self.assertIn("需求", result.tail_records[0].reason)

    def test_ineligible_never_allocated_with_reason(self):
        institutions = {"ok": inst("ok", base=10),
                        "bad": inst("bad", base=999, eligible=False)}
        applications = {"a1": app("a1", "ok"), "a2": app("a2", "bad")}
        result = allocate(institutions, applications, [], [], 10)
        bad = next(l for l in result.lines if l.application_id == "a2")
        self.assertEqual(bad.allocated, 0)
        self.assertEqual(bad.status, LineStatus.REJECTED.value)
        self.assertTrue(any("资格" in r for r in bad.reasons))
        good = next(l for l in result.lines if l.application_id == "a1")
        self.assertEqual(good.allocated, 5)

    def test_waitlist_ranking_and_reasons(self):
        institutions = {f"i{k}": inst(f"i{k}", base=score)
                        for k, score in enumerate((90, 80, 70), start=1)}
        applications = {f"a{k}": app(f"a{k}", f"i{k}", seats=10) for k in range(1, 4)}
        result = allocate(institutions, applications, [], [], 6)
        ranks = {l.application_id: l.waitlist_rank for l in result.lines}
        self.assertEqual(ranks["a1"], 1)
        self.assertEqual(ranks["a2"], 2)
        self.assertEqual(ranks["a3"], 3)
        self.assertTrue(all("候补第" in r for l in result.lines for r in l.reasons if "候补" in r))

    def test_deterministic_on_ties(self):
        institutions = {"a": inst("a", base=5), "b": inst("b", base=5)}
        applications = {"a1": app("a1", "a", seats=10), "b1": app("b1", "b", seats=10)}
        first = allocate(institutions, applications, [], [], 3)
        second = allocate(institutions, applications, [], [], 3)
        self.assertEqual(
            [(l.application_id, l.allocated) for l in first.lines],
            [(l.application_id, l.allocated) for l in second.lines],
        )

    def test_input_validation(self):
        with self.assertRaises(ValidationError):
            allocate({}, {}, [], [], -1)
        institutions = {"i": inst("i", majors=("护理",))}
        with self.assertRaises(ValidationError):
            allocate(institutions, {"a": app("a", "i", major="工程", seats=2)}, [], [], 5)
        with self.assertRaises(ValidationError):
            allocate(institutions, {"a": app("a", "i", seats=0)}, [], [], 5)


if __name__ == "__main__":
    unittest.main()
