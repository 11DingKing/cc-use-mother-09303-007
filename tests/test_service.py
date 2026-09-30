"""服务层测试：状态机、冻结、额度分录、候补递补、转让与并发不超发。"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quota_service import QuotaService, JsonStore
from quota_service.errors import (
    ConflictError,
    FrozenError,
    NotFoundError,
    ValidationError,
)
from quota_service.models import BatchStatus, EntrySource


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self.svc = QuotaService(JsonStore(self.tmp.name))
        self._build()

    def tearDown(self) -> None:
        Path(self.tmp.name).unlink(missing_ok=True)

    # ---- 夹具 ----------------------------------------------------------
    def _build(self, total_seats: int = 12):
        s = self.svc
        self.bid = s.create_batch("测试批次", total_seats)["id"]
        self.big = s.add_institution(
            self.bid, name="大型C", country="泰国", institution_type="大型院校",
            majors=["护理"], base_score=95, history_score=30)["id"]
        self.small_a = s.add_institution(
            self.bid, name="小型A", country="泰国", institution_type="小型院校",
            majors=["护理"], base_score=80, history_score=10)["id"]
        self.small_b = s.add_institution(
            self.bid, name="小型B", country="越南", institution_type="小型院校",
            majors=["护理"], base_score=70)["id"]
        self.app_big = s.add_application(self.bid, self.big, "护理", 8)["id"]
        self.app_a = s.add_application(self.bid, self.small_a, "护理", 6)["id"]
        self.app_b = s.add_application(self.bid, self.small_b, "护理", 6)["id"]
        s.set_capacities(self.bid, [
            {"dimension": "country", "dim_value": "泰国", "seats": 8},
            {"dimension": "type", "dim_value": "小型院校", "seats": 8},
            {"dimension": "major", "dim_value": "护理", "seats": 12},
        ])
        s.add_guarantee(self.bid, "type", "小型院校", 3)

    def _publish(self, **trial_kw) -> str:
        tid = self.svc.create_trial(self.bid, **trial_kw)["id"]
        self.svc.publish(self.bid, tid)
        return tid

    def _balances(self, batch_id: str | None = None) -> dict[str, int]:
        return {r["application_id"]: r["balance"]
                for r in self.svc.ledger(batch_id or self.bid)["balances"]}

    # ---- 生命周期与冻结 --------------------------------------------------
    def test_draft_to_trial_status(self):
        self.assertEqual(self.svc.get_batch(self.bid)["status"], BatchStatus.DRAFT.value)
        self.svc.begin_trial_phase(self.bid)
        self.assertEqual(self.svc.get_batch(self.bid)["status"], BatchStatus.TRIAL.value)

    def test_inputs_freeze_after_publish(self):
        self._publish()
        with self.assertRaises(FrozenError):
            self.svc.add_application(self.bid, self.small_b, "护理", 1)
        with self.assertRaises(FrozenError):
            self.svc.set_capacities(self.bid, [])
        with self.assertRaises(FrozenError):
            self.svc.add_guarantee(self.bid, "country", "越南", 2)
        with self.assertRaises(FrozenError):
            self.svc.set_history_score(self.bid, self.big, 5)

    def test_trials_do_not_create_ledger_entries(self):
        t1 = self.svc.create_trial(self.bid, label="v1")["id"]
        t2 = self.svc.create_trial(self.bid, label="v2", history_penalty_weight=0.2)["id"]
        self.assertEqual(self.svc.ledger(self.bid)["entries"], [])
        cmp = self.svc.compare_trials(self.bid, [t1, t2])
        self.assertEqual(len(cmp["comparison_matrix"]), 3)
        self.assertEqual([t["label"] for t in cmp["trials"]], ["v1", "v2"])

    def test_infeasible_trial_cannot_publish(self):
        # 新增保底需求超过总名额
        self.svc.add_guarantee(self.bid, "country", "泰国", 12)
        tid = self.svc.create_trial(self.bid)["id"]
        self.assertFalse(self.svc.get_trial(self.bid, tid)["result"]["feasible"])
        with self.assertRaises(ConflictError):
            self.svc.publish(self.bid, tid)
        self.assertEqual(self.svc.get_batch(self.bid)["status"], BatchStatus.TRIAL.value)

    def test_publish_initial_entries_balance_to_allocation(self):
        tid = self._publish()
        result = self.svc.get_trial(self.bid, tid)["result"]
        balances = self._balances()
        self.assertEqual(sum(balances.values()), result["total_allocated"])
        for line in result["lines"]:
            if line["allocated"]:
                self.assertEqual(balances[line["application_id"]], line["allocated"])
        sources = {e["source"] for e in self.svc.ledger(self.bid)["entries"]}
        self.assertIn(EntrySource.INITIAL.value, sources)

    # ---- 确认 / 放弃 / 撤销 ----------------------------------------------
    def test_confirm_partial_releases_seats_and_backfill(self):
        self.svc.set_capacities(self.bid, [
            {"dimension": "country", "dim_value": "泰国", "seats": 12},
            {"dimension": "type", "dim_value": "小型院校", "seats": 12},
            {"dimension": "major", "dim_value": "护理", "seats": 20},
        ])
        self._publish()
        balances = self._balances()
        big_got = balances[self.app_big]
        self.assertGreaterEqual(big_got, 2)
        # 大型C 仅确认 big_got-1 人，释放 1 人
        r = self.svc.confirm(self.bid, self.app_big, True, seats=big_got - 1)
        self.assertEqual(r["released_seats"], 1)
        self.assertEqual(r["balance"], big_got - 1)

        bf = self.svc.start_backfill(self.bid)
        # 候补按顺位递补，总量守恒
        self.assertGreaterEqual(len(bf["promotions"]), 1)
        after = self._balances()
        self.assertEqual(sum(after.values()), sum(balances.values()))
        self.assertEqual(bf["released_pool"], 0)

    def test_decline_all_then_promotion_entries(self):
        # 放宽小型院校容量，确保放弃名额能被小型候补承接
        self.svc.set_capacities(self.bid, [
            {"dimension": "country", "dim_value": "泰国", "seats": 12},
            {"dimension": "type", "dim_value": "小型院校", "seats": 12},
            {"dimension": "major", "dim_value": "护理", "seats": 20},
        ])
        self._publish()
        before = sum(self._balances().values())
        self.svc.confirm(self.bid, self.app_big, False)
        bf = self.svc.run_backfill(self.bid)
        promoted_ids = {p["application_id"] for p in bf["promotions"]}
        self.assertTrue(promoted_ids)
        ledger = self.svc.ledger(self.bid)["entries"]
        self.assertTrue(any(e["source"] == EntrySource.WAITLIST_PROMOTION.value for e in ledger))
        self.assertTrue(any(e["source"] == EntrySource.DECLINE_RELEASE.value for e in ledger))
        self.assertEqual(sum(self._balances().values()), before)

    def test_double_confirm_rejected(self):
        self._publish()
        self.svc.confirm(self.bid, self.app_big, True)
        with self.assertRaises(ConflictError):
            self.svc.confirm(self.bid, self.app_big, True)

    def test_revoke_releases_all_and_invalidates_confirmation(self):
        self._publish()
        self.svc.confirm(self.bid, self.app_a, True)
        had = self._balances().get(self.app_a, 0)
        r = self.svc.revoke_eligibility(self.bid, self.small_a, "材料不实")
        self.assertEqual(r["released_seats"], had)
        self.assertEqual(self._balances().get(self.app_a, 0), 0)
        view = self.svc.institution_view(self.bid, self.small_a)
        self.assertFalse(view["institution"]["eligible"])
        self.assertTrue(all(l["live_status"] == "资格撤销" for l in view["lines"]))
        with self.assertRaises(ConflictError):
            self.svc.revoke_eligibility(self.bid, self.small_a, "再次撤销")

    def test_revoked_waitlist_candidate_is_skipped(self):
        # 小型A、小型B 同为候补时撤销顺位靠前的 A，B 应获得递补
        self._publish()
        ranks = {}
        res = self.svc.get_batch(self.bid)["published_result"]
        for line in res["lines"]:
            ranks[line["application_id"]] = line["waitlist_rank"]
        self.svc.confirm(self.bid, self.app_big, False)
        self.svc.revoke_eligibility(self.bid, self.small_a, "撤销")
        bf = self.svc.start_backfill(self.bid)
        promoted = {p["application_id"] for p in bf["promotions"]}
        self.assertIn(self.app_b, promoted)
        self.assertNotIn(self.app_a, promoted)

    # ---- 转让 ------------------------------------------------------------
    def test_transfer_pair_entries_and_capacity_guard(self):
        # 放宽泰国与小型院校容量，使同专业转让可行
        self.svc.set_capacities(self.bid, [
            {"dimension": "country", "dim_value": "泰国", "seats": 12},
            {"dimension": "type", "dim_value": "小型院校", "seats": 12},
            {"dimension": "major", "dim_value": "护理", "seats": 20},
        ])
        self._publish()
        balances = self._balances()
        seats = min(2, balances[self.app_big], 6 - balances.get(self.app_b, 0))
        r = self.svc.transfer(
            self.bid, from_application_id=self.app_big,
            to_application_id=self.app_b, seats=seats, reason="结对")
        self.assertEqual(r["balances"][self.app_big], balances[self.app_big] - seats)
        self.assertEqual(r["balances"][self.app_b], balances[self.app_b] + seats)
        ledger = self.svc.ledger(self.bid)["entries"]
        self.assertTrue(any(e["source"] == EntrySource.TRANSFER_OUT.value for e in ledger))
        self.assertTrue(any(e["source"] == EntrySource.TRANSFER_IN.value for e in ledger))

    def test_transfer_cannot_overdraw(self):
        self._publish()
        balances = self._balances()
        with self.assertRaises(ConflictError):
            self.svc.transfer(
                self.bid, from_application_id=self.app_big,
                to_application_id=self.app_b, seats=balances[self.app_big] + 1)

    def test_transfer_cannot_exceed_receiver_demand(self):
        # 把承接方 B 直接顶到需求上限后不能再接收
        self.svc.set_capacities(self.bid, [
            {"dimension": "country", "dim_value": "泰国", "seats": 12},
            {"dimension": "type", "dim_value": "小型院校", "seats": 12},
            {"dimension": "major", "dim_value": "护理", "seats": 20},
        ])
        self._publish()
        balances = self._balances()
        room_b = 6 - balances[self.app_b]
        if room_b > 0 and balances[self.app_big] >= room_b:
            self.svc.transfer(
                self.bid, from_application_id=self.app_big,
                to_application_id=self.app_b, seats=room_b)
            with self.assertRaises(ConflictError):
                self.svc.transfer(
                    self.bid, from_application_id=self.app_big,
                    to_application_id=self.app_b, seats=1)

    def test_transfer_blocked_by_dimension_capacity(self):
        # 默认夹具中小型院校容量将饱和：大型院校 → 小型B 的转让必须被拦截
        self._publish()
        balances = self._balances()
        self.assertLess(balances[self.app_b], 6)
        self.assertGreater(balances[self.app_big], 0)
        with self.assertRaises(ConflictError):
            self.svc.transfer(
                self.bid, from_application_id=self.app_big,
                to_application_id=self.app_b, seats=1)

    def test_transfer_requires_same_major(self):
        self.svc.add_institution(
            self.bid, name="小型E", country="越南", institution_type="小型院校",
            majors=["农业"], base_score=60)
        eid = next(iter(
            k for k, v in self.svc.get_batch(self.bid)["institutions"].items()
            if v["name"] == "小型E"))
        appe = self.svc.add_application(self.bid, eid, "农业", 4)["id"]
        self.svc.set_capacities(self.bid, [
            {"dimension": "country", "dim_value": "泰国", "seats": 20},
            {"dimension": "type", "dim_value": "小型院校", "seats": 20},
            {"dimension": "major", "dim_value": "护理", "seats": 20},
            {"dimension": "major", "dim_value": "农业", "seats": 20},
        ])
        self._publish()
        with self.assertRaises(ValidationError):
            self.svc.transfer(
                self.bid, from_application_id=self.app_big,
                to_application_id=appe, seats=1)

    # ---- 尾差可解释 ------------------------------------------------------
    def test_tail_seats_have_explainable_holder(self):
        tid = self.svc.create_trial(self.bid)["id"]
        result = self.svc.get_trial(self.bid, tid)["result"]
        if result["tail_records"]:
            rec = result["tail_records"][0]
            self.assertTrue(rec["seats"] > 0)
            self.assertTrue(rec["holder_name"])
            self.assertTrue(rec["reason"])
        # 每个入选名额都有 trace
        for line in result["lines"]:
            self.assertEqual(len(line["traces"]), line["allocated"])

    def test_unfillable_release_recovered_with_reason(self):
        # 仅一所合格候补院校且容量受限：释放名额无法承接时必须可解释回收
        bid = self.svc.create_batch("回收批次", 6)["id"]
        i1 = self.svc.add_institution(
            bid, name="甲", country="泰国", institution_type="大型院校",
            majors=["护理"], base_score=10)["id"]
        i2 = self.svc.add_institution(
            bid, name="乙", country="泰国", institution_type="大型院校",
            majors=["护理"], base_score=1)["id"]
        a1 = self.svc.add_application(bid, i1, "护理", 6)["id"]
        self.svc.add_application(bid, i2, "护理", 6)
        self.svc.set_capacities(bid, [
            {"dimension": "country", "dim_value": "泰国", "seats": 6}])
        tid = self.svc.create_trial(bid)["id"]
        self.svc.publish(bid, tid)
        self.svc.revoke_eligibility(bid, i2, "候补方也被撤销")
        self.svc.confirm(bid, a1, True, seats=3)
        bf = self.svc.run_backfill(bid)
        self.assertEqual(bf["released_pool"], 0)
        self.assertTrue(bf["vacant_records"])
        self.assertIn("无合格承接对象", bf["vacant_records"][-1]["reason"])

    # ---- 并发 ------------------------------------------------------------
    def test_concurrent_transfers_never_over_issue(self):
        bid = self.svc.create_batch("并发批次", 5)["id"]
        x = self.svc.add_institution(
            bid, name="X", country="泰国", institution_type="大型院校",
            majors=["护理"], base_score=10)["id"]
        y = self.svc.add_institution(
            bid, name="Y", country="越南", institution_type="大型院校",
            majors=["护理"], base_score=1)["id"]
        ax = self.svc.add_application(bid, x, "护理", 5)["id"]
        ay = self.svc.add_application(bid, y, "护理", 5)["id"]
        tid = self.svc.create_trial(bid)["id"]
        self.svc.publish(bid, tid)
        total = sum(self._balances(bid).values())
        x_seats = self._balances(bid)[ax]

        errors: list[Exception] = []

        def race():
            try:
                self.svc.transfer(
                    bid, from_application_id=ax, to_application_id=ay, seats=x_seats)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=race) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        balances = {r["application_id"]: r["balance"]
                    for r in self.svc.ledger(bid)["balances"]}
        self.assertEqual(sum(balances.values()), total)          # 总量守恒
        self.assertEqual(balances[ax], 0)
        self.assertEqual(balances[ay], total)                     # 只成功一次
        self.assertEqual(len(errors), 7)

    def test_optimistic_version_conflict(self):
        self._publish()
        version = self.svc.get_batch(self.bid)["version"]
        with self.assertRaises(ConflictError):
            self.svc.confirm(
                self.bid, self.app_big, True,
                expected_version=version - 1)

    # ---- 查询与解释 ------------------------------------------------------
    def test_institution_view_explains_every_application(self):
        self._publish()
        for iid in (self.big, self.small_a, self.small_b):
            view = self.svc.institution_view(self.bid, iid)
            self.assertTrue(view["lines"])
            for line in view["lines"]:
                self.assertTrue(line["reasons"], f"{iid} 缺少入选原因")
                self.assertIn(line["status"], ("入选", "部分入选", "候补", "未入选"))

    def test_close_requires_pool_drained(self):
        self._publish()
        self.svc.confirm(self.bid, self.app_big, True, seats=1)
        with self.assertRaises(ConflictError):
            self.svc.close_batch(self.bid)
        self.svc.start_backfill(self.bid)
        closed = self.svc.close_batch(self.bid)
        self.assertEqual(closed["status"], BatchStatus.CLOSED.value)

    def test_missing_batch_404(self):
        with self.assertRaises(NotFoundError):
            self.svc.get_batch("not-exist")


if __name__ == "__main__":
    unittest.main()
