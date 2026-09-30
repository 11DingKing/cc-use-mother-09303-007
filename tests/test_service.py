"""服务层生命周期测试：试算隔离、发布冻结、分录调整、并发不超发、逐机构解释。"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quota_service.models import BatchState, LedgerEntryKind, QuotaError
from quota_service.service import QuotaService


def build_populated_batch(svc: QuotaService, total=6, small_guarantee=None) -> str:
    bid = svc.create_batch("联合师资培训", total)["batch_id"]
    svc.set_dimension_capacity(bid, "institution_type", "小型学院", 6)
    if small_guarantee is not None:
        svc.set_guarantee(bid, "institution_type", "小型学院", small_guarantee)
    apps = [
        ("BIG1", "大型大学", "综合性大学", 5, 3),
        ("BIG2", "理工大学", "综合性大学", 5, 2),
        ("SM1", "山村学院", "小型学院", 3, 0),
        ("SM2", "河畔学院", "小型学院", 3, 0),
    ]
    for iid, name, itype, demand, history in apps:
        svc.upsert_application(bid, {
            "institution_id": iid, "name": name, "country": "泰国",
            "institution_type": itype, "specialty": "新能源",
            "demand": demand, "history": history,
        })
    return bid


class ScenarioIsolationTest(unittest.TestCase):
    def test_scenarios_do_not_touch_each_other_or_formal(self) -> None:
        svc = QuotaService()
        bid = build_populated_batch(svc, total=6)
        s1 = svc.run_scenario(bid, "方案一")["scenario_id"]
        before = svc.scenario_view(bid, s1)["summary"]["total_selected"]
        # 改输入后再试算：旧方案不变，新方案反映新输入
        svc.set_total_capacity(bid, 4)
        s2 = svc.run_scenario(bid, "方案二")["scenario_id"]
        self.assertEqual(svc.scenario_view(bid, s1)["summary"]["total_capacity"], 6)
        self.assertEqual(svc.scenario_view(bid, s1)["summary"]["total_selected"], before)
        self.assertEqual(svc.scenario_view(bid, s2)["summary"]["total_capacity"], 4)
        # 试算不产生任何正式额度
        self.assertNotIn("published", svc.batch_view(bid))

    def test_compare_scenarios_highlights_differences(self) -> None:
        svc = QuotaService()
        bid = build_populated_batch(svc, total=6)
        s1 = svc.run_scenario(bid)["scenario_id"]
        svc.set_total_capacity(bid, 5)
        s2 = svc.run_scenario(bid)["scenario_id"]
        cmp = svc.compare_scenarios(bid, [s1, s2])
        self.assertTrue(any(row["differs"] for row in cmp["institutions"]))
        with self.assertRaises(QuotaError):
            svc.compare_scenarios(bid, [s1])


class PublishFreezeTest(unittest.TestCase):
    def test_input_frozen_after_publish(self) -> None:
        svc = QuotaService()
        bid = build_populated_batch(svc)
        sid = svc.run_scenario(bid)["scenario_id"]
        svc.publish(bid, sid)
        view = svc.batch_view(bid)
        self.assertEqual(view["state"], BatchState.PUBLISHED.value)
        for call in (
            lambda: svc.set_total_capacity(bid, 99),
            lambda: svc.upsert_application(bid, {"institution_id": "X", "name": "x",
                                                 "country": "c", "institution_type": "t",
                                                 "specialty": "s", "demand": 1}),
            lambda: svc.set_guarantee(bid, "country", "越南", 2),
            lambda: svc.run_scenario(bid),
        ):
            with self.assertRaises(QuotaError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "INPUT_FROZEN")

    def test_small_colleges_protected_in_published_plan(self) -> None:
        svc = QuotaService()
        bid = build_populated_batch(svc, total=6, small_guarantee=3)
        sid = svc.run_scenario(bid)["scenario_id"]
        svc.publish(bid, sid)
        view = svc.batch_view(bid)
        held = view["published"]["total_held"]
        self.assertEqual(held, 6)
        sm = svc.institution_view(bid, "SM1")["held_seats"] + \
            svc.institution_view(bid, "SM2")["held_seats"]
        self.assertGreaterEqual(sm, 3)


class LedgerAdjustmentFlowTest(unittest.TestCase):
    def _published(self, svc, total=8):
        bid = build_populated_batch(svc, total=total)
        sid = svc.run_scenario(bid)["scenario_id"]
        svc.publish(bid, sid)
        return bid

    def test_relinquish_promote_flow(self) -> None:
        svc = QuotaService()
        bid = self._published(svc)
        view = svc.batch_view(bid)
        # 找一个有持有名额的机构放弃
        balances = {}
        for e in view["published"]["entries"]:
            balances[e["institution_id"]] = balances.get(e["institution_id"], 0) + e["delta"]
        holder = next(i for i, n in balances.items() if n > 0)
        held_before = balances[holder]
        svc.relinquish(bid, holder, 1, "出国冲突")
        cand = svc.waitlist_candidate(bid)
        self.assertIsNotNone(cand)
        promoted = svc.promote_next(bid)
        self.assertEqual(promoted["entry"]["kind"], LedgerEntryKind.WAITLIST_PROMOTE.value)
        self.assertEqual(svc.batch_view(bid)["published"]["total_held"], sum(balances.values()))
        # 放弃者余额减 1
        v2 = svc.batch_view(bid)
        new_bal = sum(e["delta"] for e in v2["published"]["entries"]
                      if e["institution_id"] == holder)
        self.assertEqual(new_bal, held_before - 1)

    def test_revoke_after_publish_frees_all_and_blocks_confirm(self) -> None:
        svc = QuotaService()
        bid = self._published(svc)
        held = {i: svc.institution_view(bid, i)["held_seats"]
                for i in ("BIG1", "BIG2", "SM1", "SM2")}
        target = next(i for i, n in held.items() if n > 0)
        svc.revoke_after_publish(bid, target, "材料造假")
        self.assertEqual(svc.institution_view(bid, target)["held_seats"], 0)
        with self.assertRaises(QuotaError) as ctx:
            svc.confirm(bid, target)
        self.assertEqual(ctx.exception.code, "ELIGIBILITY_REVOKED")
        view = svc.institution_view(bid, target)
        self.assertTrue(any("资格已撤销" in r for r in view["reasons"]))

    def test_transfer_pair(self) -> None:
        svc = QuotaService()
        bid = self._published(svc)
        held = {i: svc.institution_view(bid, i)["held_seats"]
                for i in ("BIG1", "BIG2", "SM1", "SM2")}
        sender = next(i for i, n in held.items() if n >= 1)
        receiver = next(i for i, n in held.items() if i != sender and n < 3)
        total_before = svc.batch_view(bid)["published"]["total_held"]
        out = svc.transfer(bid, sender, receiver, 1, "协作让渡")
        self.assertEqual(len(out["entries"]), 2)
        self.assertEqual(svc.batch_view(bid)["published"]["total_held"], total_before)
        self.assertEqual(svc.institution_view(bid, receiver)["held_seats"], held[receiver] + 1)


class OptimisticLockTest(unittest.TestCase):
    def test_stale_version_rejected(self) -> None:
        svc = QuotaService()
        bid = build_populated_batch(svc)
        v = svc.batch_view(bid)["version"]
        svc.set_total_capacity(bid, 7)  # 版本前进
        with self.assertRaises(QuotaError) as ctx:
            svc.set_total_capacity(bid, 8, expected_version=v)
        self.assertEqual(ctx.exception.code, "VERSION_CONFLICT")
        # 持新版本成功
        v2 = svc.batch_view(bid)["version"]
        svc.set_total_capacity(bid, 8, expected_version=v2)

    def test_concurrent_relinquish_and_promote_cannot_oversell(self) -> None:
        """并发放弃 + 并发递补：最终持有始终不超过容量，且不低于容量−释放数。"""
        svc = QuotaService()
        bid = build_populated_batch(svc, total=10)
        svc.publish(bid, svc.run_scenario(bid)["scenario_id"])
        held = {i: svc.institution_view(bid, i)["held_seats"]
                for i in ("BIG1", "BIG2", "SM1", "SM2")}
        holder = max(held, key=lambda i: held[i])
        seats = held[holder]
        barrier = threading.Barrier(seats + 2)

        def abandon() -> None:
            barrier.wait()
            try:
                svc.relinquish(bid, holder, 1, "并发放弃")
            except QuotaError:
                pass  # 其他人可能已放弃完

        def grab() -> None:
            barrier.wait()
            for _ in range(20):
                try:
                    svc.promote_next(bid)
                except QuotaError:
                    return

        threads = [threading.Thread(target=abandon) for _ in range(seats)]
        threads += [threading.Thread(target=grab), threading.Thread(target=grab)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = svc.batch_view(bid)["published"]["total_held"]
        self.assertLessEqual(total, 10)
        self.assertGreaterEqual(total, 10 - seats)
        # 所有机构余额非负
        for iid in ("BIG1", "BIG2", "SM1", "SM2"):
            self.assertGreaterEqual(svc.institution_view(bid, iid)["held_seats"], 0)

    def test_concurrent_promotions_fill_without_oversell(self) -> None:
        """一次释放多席后，多个线程同时递补：恰好补满且绝不超发。"""
        svc = QuotaService()
        bid = build_populated_batch(svc, total=10)
        svc.publish(bid, svc.run_scenario(bid)["scenario_id"])
        held = {i: svc.institution_view(bid, i)["held_seats"]
                for i in ("BIG1", "BIG2", "SM1", "SM2")}
        holder = max(held, key=lambda i: held[i])
        svc.relinquish(bid, holder, held[holder], "整批放弃")
        freed = held[holder]

        errors: list[QuotaError] = []

        def grab() -> None:
            try:
                svc.promote_next(bid)
            except QuotaError as e:
                errors.append(e)

        threads = [threading.Thread(target=grab) for _ in range(freed + 4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        total = svc.batch_view(bid)["published"]["total_held"]
        self.assertEqual(total, 10)  # 恰好补满
        self.assertTrue(all(e.code == "NO_WAITLIST_CANDIDATE" for e in errors))

    def test_relinquishing_institution_exits_automatic_waitlist(self) -> None:
        """主动放弃者不能立刻把自己放弃的席位递补回来。"""
        svc = QuotaService()
        bid = build_populated_batch(svc, total=10)
        svc.publish(bid, svc.run_scenario(bid)["scenario_id"])
        held = {i: svc.institution_view(bid, i)["held_seats"]
                for i in ("BIG1", "BIG2", "SM1", "SM2")}
        giver = min(held, key=lambda i: held[i])
        svc.relinquish(bid, giver, 1, "放弃")
        cand = svc.waitlist_candidate(bid)
        if cand is not None:
            self.assertNotEqual(cand["institution"]["institution_id"], giver)
        view = svc.institution_view(bid, giver)
        self.assertTrue(any("退出本轮自动候补递补" in r for r in view["reasons"]))


class ExplanationTest(unittest.TestCase):
    def test_every_institution_sees_reasons_after_publish(self) -> None:
        svc = QuotaService()
        bid = build_populated_batch(svc, total=6, small_guarantee=3)
        svc.publish(bid, svc.run_scenario(bid)["scenario_id"])
        for iid in ("BIG1", "BIG2", "SM1", "SM2"):
            view = svc.institution_view(bid, iid)
            self.assertTrue(view["reasons"], f"{iid} 必须能看到原因")
            self.assertIn(view["status"],
                          {"selected", "partially_selected", "waitlisted", "rejected"})

    def test_draft_view_before_scenario(self) -> None:
        svc = QuotaService()
        bid = build_populated_batch(svc)
        view = svc.institution_view(bid, "SM1")
        self.assertEqual(view["phase"], "draft")
        self.assertIn("message", view)

    def test_waitlist_blocked_by_dimension_explained(self) -> None:
        """总容量有余量，但某维度容量为 0：该维度院校落选且原因可解释。"""
        svc = QuotaService()
        bid = svc.create_batch("维度阻塞", 5)["batch_id"]
        svc.set_dimension_capacity(bid, "country", "越南", 0)
        svc.upsert_application(bid, {"institution_id": "T1", "name": "泰校",
                                     "country": "泰国", "institution_type": "综合性大学",
                                     "specialty": "新能源", "demand": 2})
        svc.upsert_application(bid, {"institution_id": "V1", "name": "越校",
                                     "country": "越南", "institution_type": "综合性大学",
                                     "specialty": "新能源", "demand": 2})
        svc.publish(bid, svc.run_scenario(bid)["scenario_id"])
        view = svc.institution_view(bid, "V1")
        self.assertEqual(view["held_seats"], 0)
        self.assertEqual(view["status"], "waitlisted")
        self.assertIsNone(svc.waitlist_candidate(bid))
        self.assertIn("维度容量", view["waitlist_note"])


if __name__ == "__main__":
    unittest.main()
