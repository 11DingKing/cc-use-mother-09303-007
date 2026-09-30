"""端到端情景演示：用内存服务走通一个完整批次。

运行：``python3 tools/demo.py``

演示要点：
1. 维护总容量、维度容量、最低保障与历史参与系数；
2. 跑两套试算并对比，正式额度不受影响；
3. 发布后输入冻结；
4. 放弃名额走额度分录，放弃者退出本轮自动递补，后续候补递补；
5. 机构间转让成对落账；
6. 每所院校都能查询自己入选/未入选的原因。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quota_service.service import QuotaService


def show(title: str, payload) -> None:
    print(f"\n===== {title} =====")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main() -> None:
    svc = QuotaService()

    bid = svc.create_batch("联合师资培训（第 7 期）", total_capacity=8)["batch_id"]

    # 维度容量：国家与专业方向
    svc.set_dimension_capacity(bid, "country", "泰国", 8)
    svc.set_dimension_capacity(bid, "specialty", "新能源", 8)
    # 最低保障：小型院校至少 3 席（吸取“小型院校全部落选”的教训）
    svc.set_guarantee(bid, "institution_type", "小型学院", 3)

    # 院校申请：两所小型学院历史参与为 0；大型大学按历史参与区分
    for iid, name, itype, demand, history in [
        ("BIG1", "国立示范大学", "综合性大学", 5, 3),
        ("BIG2", "理工联合大学", "综合性大学", 4, 2),
        ("BIG3", "东部交通大学", "综合性大学", 3, 1),
        ("SM1", "山村师范学院", "小型学院", 3, 0),
        ("SM2", "河畔职业学院", "小型学院", 2, 0),
    ]:
        svc.upsert_application(bid, {
            "institution_id": iid, "name": name, "country": "泰国",
            "institution_type": itype, "specialty": "新能源",
            "demand": demand, "history": history,
        })

    # 两套试算：容量 8 与容量 7，互相对比，不产生正式额度
    s1 = svc.run_scenario(bid, "容量8席")["scenario_id"]
    svc.set_total_capacity(bid, 7)
    s2 = svc.run_scenario(bid, "容量7席")["scenario_id"]
    svc.set_total_capacity(bid, 8)  # 改回 8；s1/s2 绑定的快照不受影响

    show("两套试算对比", svc.compare_scenarios(bid, [s1, s2]))

    # 发布方案一，输入冻结
    svc.publish(bid, s1)
    show("发布后的正式分录", svc.batch_view(bid)["published"]["entries"])

    ids = ("BIG1", "BIG2", "BIG3", "SM1", "SM2")
    held = {i: svc.institution_view(bid, i)["held_seats"] for i in ids}
    print("发布后持有：", held)

    # BIG3 放弃其仅有的 1 席；它退出本轮自动递补，队首顺延
    svc.relinquish(bid, "BIG3", 1, "外派教师行程冲突")
    cand = svc.waitlist_candidate(bid)
    show("BIG3 放弃后的候补队首", cand)
    promoted = svc.promote_next(bid, reason="放弃名额触发自动递补")
    print(f"递补给：{promoted['entry']['institution_id']} {promoted['entry']['delta']} 席")

    # 机构间转让 1 席（SM2 让给仍有缺口的 BIG1）
    held = {i: svc.institution_view(bid, i)["held_seats"] for i in ids}
    sender = "SM2" if held["SM2"] >= 1 and held["BIG1"] < 5 else "SM1"
    svc.transfer(bid, sender, "BIG1", 1, "校际协作让渡")

    show("BIG3 视角（放弃原因 + 退出自动递补说明）", svc.institution_view(bid, "BIG3"))
    show("SM1 视角（保障入选，原因可解释）", svc.institution_view(bid, "SM1"))

    final = svc.batch_view(bid)
    show("最终分录流水", final["published"]["entries"])
    assert final["published"]["total_held"] <= 8, "调整后仍不得超发"
    print("\n演示完成：小型学院获保障，试算互不影响，放弃/递补/转让全走分录，总持有未超容量。")


if __name__ == "__main__":
    main()
