"""HTTP 接口端到端测试（真实起服，随机端口，零依赖）。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quota_service.server import build_server
from quota_service.service import QuotaService


class HttpSession:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, method: str, path: str, body: dict | None = None, expect_error: bool = False):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            payload = json.loads(exc.read().decode("utf-8"))
            if expect_error:
                return exc.code, payload
            raise AssertionError(f"{method} {path} 意外失败 {exc.code}: {payload}") from None


def _setup_scenario(http: HttpSession) -> str:
    _, out = http.call("POST", "/batches", {"name": "联合师资", "total_capacity": 6})
    bid = out["batch_id"]
    http.call("PUT", f"/batches/{bid}/dimensions",
              {"dimension": "institution_type", "value": "小型学院", "capacity": 6})
    http.call("PUT", f"/batches/{bid}/guarantees",
              {"dimension": "institution_type", "value": "小型学院", "seats": 3})
    for iid, name, itype, demand, history in [
        ("BIG1", "大型大学", "综合性大学", 5, 3),
        ("BIG2", "理工大学", "综合性大学", 5, 2),
        ("SM1", "山村学院", "小型学院", 3, 0),
        ("SM2", "河畔学院", "小型学院", 3, 0),
    ]:
        http.call("PUT", f"/batches/{bid}/applications", {
            "institution_id": iid, "name": name, "country": "泰国",
            "institution_type": itype, "specialty": "新能源",
            "demand": demand, "history": history,
        })
    return bid


class ServerLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = build_server("127.0.0.1", 0, QuotaService())
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.http = HttpSession(f"http://127.0.0.1:{cls.port}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def test_full_lifecycle_over_http(self) -> None:
        http = self.http
        bid = _setup_scenario(http)

        # 两套试算并对比
        _, s1 = http.call("POST", f"/batches/{bid}/scenarios", {"name": "方案A"})
        _, s2 = http.call("POST", f"/batches/{bid}/scenarios", {"name": "方案B"})
        self.assertEqual(s1["scenario_id"], "S001")
        _, cmp = http.call("POST", f"/batches/{bid}/compare",
                           {"scenario_ids": ["S001", "S002"]})
        self.assertEqual(len(cmp["institutions"]), 4)

        # 尾差解释存在且可序列化
        _, view = http.call("GET", f"/batches/{bid}/scenarios/S001")
        self.assertTrue(view["summary"]["total_selected"] <= 6)

        # 发布
        _, pub = http.call("POST", f"/batches/{bid}/publish", {"scenario_id": "S001"})
        self.assertEqual(pub["state"], "published")
        v1 = pub["batch_version"]

        # 发布后改输入被冻结拦截（409）
        code, err = http.call("PUT", f"/batches/{bid}/capacity",
                              {"seats": 99, "version": v1}, expect_error=True)
        self.assertEqual(code, 409)
        self.assertEqual(err["error"]["code"], "INPUT_FROZEN")

        # 每所机构都能看到原因
        for iid in ("BIG1", "BIG2", "SM1", "SM2"):
            _, iv = http.call("GET", f"/batches/{bid}/institutions/{iid}")
            self.assertTrue(iv["reasons"])

        # 持有者确认；放弃一席
        _, batch = http.call("GET", f"/batches/{bid}")
        holders = {}
        for e in batch["published"]["entries"]:
            holders[e["institution_id"]] = holders.get(e["institution_id"], 0) + e["delta"]
        holder = next(i for i, n in holders.items() if n > 0)
        _, conf = http.call("POST", f"/batches/{bid}/confirm",
                            {"institution_id": holder, "version": v1})
        v2 = conf["batch_version"]
        _, rel = http.call("POST", f"/batches/{bid}/relinquish",
                           {"institution_id": holder, "seats": 1,
                            "reason": "教师行程冲突", "version": v2})
        self.assertEqual(rel["freed_seats"], 1)

        # 查看候补队首并递补
        _, wl = http.call("GET", f"/batches/{bid}/waitlist/next")
        self.assertIsNotNone(wl["candidate"])
        v3 = rel["batch_version"]
        _, prom = http.call("POST", f"/batches/{bid}/promote", {"version": v3})
        self.assertEqual(prom["entry"]["kind"], "waitlist_promote")

        # 总持有不超过容量
        _, batch2 = http.call("GET", f"/batches/{bid}")
        self.assertLessEqual(batch2["published"]["total_held"], 6)

    def test_optimistic_version_conflict_over_http(self) -> None:
        http = self.http
        bid = _setup_scenario(http)
        _, batch = http.call("GET", f"/batches/{bid}")
        stale = batch["version"]
        http.call("PUT", f"/batches/{bid}/capacity", {"seats": 5, "version": stale})
        code, err = http.call("PUT", f"/batches/{bid}/capacity",
                              {"seats": 4, "version": stale}, expect_error=True)
        self.assertEqual(code, 409)
        self.assertEqual(err["error"]["code"], "VERSION_CONFLICT")

    def test_dimension_guarantee_conflict_rejected(self) -> None:
        http = self.http
        _, out = http.call("POST", "/batches", {"total_capacity": 10})
        bid = out["batch_id"]
        http.call("PUT", f"/batches/{bid}/dimensions",
                  {"dimension": "country", "value": "越南", "capacity": 2})
        code, err = http.call("PUT", f"/batches/{bid}/guarantees",
                              {"dimension": "country", "value": "越南", "seats": 5},
                              expect_error=True)
        self.assertEqual(code, 400)
        self.assertEqual(err["error"]["code"], "GUARANTEE_EXCEEDS_CAPACITY")

    def test_delete_guarantee_via_query_string(self) -> None:
        http = self.http
        _, out = http.call("POST", "/batches", {"total_capacity": 10})
        bid = out["batch_id"]
        http.call("PUT", f"/batches/{bid}/guarantees",
                  {"dimension": "specialty", "value": "农业", "seats": 2})
        req = urllib.request.Request(
            f"{http.base}/batches/{bid}/guarantees?dimension=specialty&value="
            + urllib.parse.quote("农业"),
            method="DELETE",
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
        _, batch = http.call("GET", f"/batches/{bid}")
        self.assertEqual(batch["guarantees"], [])

    def test_unknown_routes_and_entities(self) -> None:
        http = self.http
        code, _ = http.call("GET", "/nonsense", {}, expect_error=True)
        self.assertEqual(code, 404)
        code, err = http.call("GET", "/batches/B9999", {}, expect_error=True)
        self.assertEqual(code, 404)
        self.assertEqual(err["error"]["code"], "UNKNOWN_BATCH")


if __name__ == "__main__":
    unittest.main()
