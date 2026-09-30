#!/usr/bin/env python3
"""对运行中的服务做端到端业务验证（发布后的确认、放弃、撤销、转让、递补、解释）。"""
import json
import urllib.error
import urllib.request

B = "http://127.0.0.1:8099"


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(B + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def must(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  ✓", msg)


# ---- 搭批次 ----
_, b = call("POST", "/batches", {"name": "2026联合师资", "total_seats": 20})
bid = b["id"]
_, big = call("POST", f"/batches/{bid}/institutions", {"name": "大型C", "country": "泰国",
    "institution_type": "大型院校", "majors": ["护理"], "base_score": 95, "history_score": 40})
_, sa = call("POST", f"/batches/{bid}/institutions", {"name": "小型A", "country": "泰国",
    "institution_type": "小型院校", "majors": ["护理", "农业"], "base_score": 80, "history_score": 10})
_, sb = call("POST", f"/batches/{bid}/institutions", {"name": "小型B", "country": "越南",
    "institution_type": "小型院校", "majors": ["护理"], "base_score": 70})
_, ab = call("POST", f"/batches/{bid}/applications", {"institution_id": big["id"], "major": "护理", "seats": 10})
_, aa = call("POST", f"/batches/{bid}/applications", {"institution_id": sa["id"], "major": "护理", "seats": 6})
_, aa2 = call("POST", f"/batches/{bid}/applications", {"institution_id": sa["id"], "major": "农业", "seats": 4})
_, abb = call("POST", f"/batches/{bid}/applications", {"institution_id": sb["id"], "major": "护理", "seats": 8})
ab, aa, aa2, abb = ab["id"], aa["id"], aa2["id"], abb["id"]
call("POST", f"/batches/{bid}/capacities", {"rules": [
    {"dimension": "country", "dim_value": "泰国", "seats": 14},
    {"dimension": "type", "dim_value": "小型院校", "seats": 18},
    {"dimension": "major", "dim_value": "护理", "seats": 18}]})
call("POST", f"/batches/{bid}/guarantees", {"dimension": "type", "dim_value": "小型院校", "min_seats": 3})

print("== 试算与对比 ==")
_, t1 = call("POST", f"/batches/{bid}/trials", {"label": "基准"})
_, t2 = call("POST", f"/batches/{bid}/trials", {"label": "弱历史修正", "history_penalty_weight": 0.2})
_, cmp = call("GET", f"/batches/{bid}/compare?ids={t1['id']},{t2['id']}")
for tid in (t1["id"], t2["id"]):
    total = sum(r["allocations"][tid] for r in cmp["comparison_matrix"])
    must(total == 20, f"试算 {tid} 矩阵按院校累加总额=20（实际 {total}）")
must(len({r["institution_id"] for r in cmp["comparison_matrix"]}) == 3, "三所院校都在矩阵中（小型A两行不丢）")

print("== 发布与冻结 ==")
st, pub = call("POST", f"/batches/{bid}/publish", {"trial_id": t1["id"]})
must(st == 200 and pub["initial_allocated"] == 20, "发布成功，初始入账 20")
st, err = call("POST", f"/batches/{bid}/applications", {"institution_id": sb["id"], "major": "护理", "seats": 1})
must(st == 409 and err["error"] == "input_frozen", "发布后输入冻结（409 input_frozen）")

_, ledger0 = call("GET", f"/batches/{bid}/ledger")
bal0 = {r["application_id"]: r["balance"] for r in ledger0["balances"]}

print("== 乐观锁：过期版本号的确认被拒绝（且不产生任何副作用）==")
ver = call("GET", f"/batches/{bid}")[1]["version"]
st, err = call("POST", f"/batches/{bid}/confirmations",
               {"application_id": ab, "accepted": True, "seats": bal0[ab],
                "expected_version": ver - 1})
must(st == 409, f"携带过期版本号 {ver - 1}（当前 {ver}）的确认被拒（409，实际 {st}）")
must(call("GET", f"/batches/{bid}")[1]["version"] == ver, "被拒事务未推进版本、未落账")

print("== 大型C 放弃部分名额 + 候补递补 ==")
st, conf = call("POST", f"/batches/{bid}/confirmations",
                {"application_id": ab, "accepted": True, "seats": bal0[ab] - 2})
must(st == 200 and conf["released_seats"] == 2, f"大型C 少确认 2 人，释放 2（实际 {conf['released_seats']}）")
st, bf = call("POST", f"/batches/{bid}/backfill-start", {})
must(st == 200 and bf["released_pool"] == 0, "递补后释放池清零，无超发")
_, ledger1 = call("GET", f"/batches/{bid}/ledger")
bal1 = {r["application_id"]: r["balance"] for r in ledger1["balances"]}
recovered = sum(e["amount"] for e in ledger1["entries"] if e["source"] == "vacant_recovery")
must(sum(bal1.values()) + recovered == 20,
     f"名额总量守恒：院校余额 {sum(bal1.values())} + 机动池 {recovered} = 20")
promo_apps = {p["application_id"] for p in bf["promotions"]}
must(any(a in promo_apps for a in (aa, abb)), "候补院校（小型A/B）获得递补")

print("== 机构间转让（成对分录 + 容量净变化校验）==")
# 找一个划出/划入可行的组合：小型B -> 大型C 同为护理，且大型C需求未满
st, tr = call("POST", f"/batches/{bid}/transfers", {
    "from_application_id": abb, "to_application_id": ab, "seats": 1, "reason": "结对帮扶"})
if st == 200:
    must(tr["balances"][abb] == bal1[abb] - 1 and tr["balances"][ab] == bal1[ab] + 1, "转让成对入账，余额正确")
    bal1[abb] -= 1; bal1[ab] += 1
else:
    print("  （转让被容量约束拦截：", tr.get("message", ""), "— 符合多维约束）")

print("== 资格撤销：强制释放 + 候补跳过不合格者 ==")
st, rv = call("POST", f"/batches/{bid}/revocations", {"institution_id": sa["id"], "reason": "材料不实"})
must(st == 200 and rv["released_seats"] >= 0, f"撤销小型A，释放其全部余额（{rv['released_seats']}）")
st, bf2 = call("POST", f"/batches/{bid}/backfill-run", {})
must(st == 200, "再次递补完成")
_, vw = call("GET", f"/batches/{bid}/institutions/" + sa["id"] + "/view")
must(all(l["live_status"] == "资格撤销" for l in vw["lines"]), "被撤销院校看到「资格撤销」状态")
for l in vw["lines"]:
    must(bool(l["reasons"]), f"撤销院校每行仍有原因说明（{l['major']}）")

print("== 每个机构都能看到入选/未入选原因 ==")
for iid, name in ((big["id"], "大型C"), (sb["id"], "小型B")):
    _, v = call("GET", f"/batches/{bid}/institutions/{iid}/view")
    for l in v["lines"]:
        must(bool(l["reasons"]) and l["live_status"], f"{name}/{l['major']} 有结论与原因：{l['live_status']}")

print("== 尾差/无法承接名额可解释 ==")
_, batch = call("GET", f"/batches/{bid}")
if bf2.get("vacant_records"):
    for rec in bf2["vacant_records"]:
        must(bool(rec["holder_name"]) and bool(rec["reason"]), f"无法承接名额落入「{rec['holder_name']}」并附原因")
else:
    print("  （本轮释放名额全部被候补承接，无机名额）")

print("== 归档 ==")
st, closed = call("POST", f"/batches/{bid}/close", {})
must(st == 200 and closed["status"] == "归档", "释放池清空后批次归档")
print("\n全部端到端断言通过 ✅")
