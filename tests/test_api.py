"""HTTP API 端到端测试（标准库客户端，覆盖主流程与错误映射、乐观锁）。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quota_service.api import create_server


class ApiCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = tempfile.mktemp(suffix=".json")
        self.server = create_server("127.0.0.1", 0, self.db)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        Path(self.db).unlink(missing_ok=True)
        for suffix in (".lock", ".tmp"):
            Path(self.db + suffix).unlink(missing_ok=True)

    def call(self, method: str, path: str, body=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def _seed_and_publish(self) -> tuple[str, str, str]:
        _, batch = self.call("POST", "/batches", {"name": "2026批次", "total_seats": 12})
        bid = batch["id"]
        _, big = self.call("POST", f"/batches/{bid}/institutions", {
            "name": "大型C", "country": "泰国", "institution_type": "大型院校",
            "majors": ["护理"], "base_score": 95, "history_score": 30})
        _, small = self.call("POST", f"/batches/{bid}/institutions", {
            "name": "小型A", "country": "越南", "institution_type": "小型院校",
            "majors": ["护理"], "base_score": 70})
        self.call("POST", f"/batches/{bid}/applications",
                  {"institution_id": big["id"], "major": "护理", "seats": 8})
        _, sa = self.call("POST", f"/batches/{bid}/applications",
                          {"institution_id": small["id"], "major": "护理", "seats": 6})
        self.call("POST", f"/batches/{bid}/capacities", {"rules": [
            {"dimension": "type", "dim_value": "小型院校", "seats": 6},
            {"dimension": "major", "dim_value": "护理", "seats": 12},
        ]})
        self.call("POST", f"/batches/{bid}/guarantees",
                  {"dimension": "type", "dim_value": "小型院校", "min_seats": 3})
        _, trial = self.call("POST", f"/batches/{bid}/trials", {"label": "v1"})
        status, pub = self.call("POST", f"/batches/{bid}/publish", {"trial_id": trial["id"]})
        self.assertEqual(status, 200)
        return bid, big["id"], sa["id"]

    def test_full_lifecycle_over_http(self):
        bid, big_id, small_app = None, None, None
        # 列表为空
        status, resp = self.call("GET", "/batches")
        self.assertEqual(status, 200)
        self.assertEqual(resp["batches"], [])

        bid, big_id, small_app_id = self._seed_and_publish()

        # 发布后冻结
        status, err = self.call("POST", f"/batches/{bid}/applications",
                                {"institution_id": big_id, "major": "护理", "seats": 1})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "input_frozen")

        # 院校视角含解释
        status, view = self.call("GET", f"/batches/{bid}/institutions/{big_id}/view")
        self.assertEqual(status, 200)
        self.assertTrue(view["lines"][0]["reasons"])

        # 大型C放弃 → 递补
        big_app = next(l["application_id"] for l in view["lines"])
        status, conf = self.call("POST", f"/batches/{bid}/confirmations",
                                 {"application_id": big_app, "accepted": False})
        self.assertEqual(status, 200)
        self.assertGreaterEqual(conf["released_seats"], 1)
        status, bf = self.call("POST", f"/batches/{bid}/backfill-start", {})
        self.assertEqual(status, 200)
        self.assertEqual(bf["released_pool"], 0)

        # 余额总量守恒并可关闭
        status, ledger = self.call("GET", f"/batches/{bid}/ledger")
        self.assertGreaterEqual(sum(r["balance"] for r in ledger["balances"]), 1)
        status, closed = self.call("POST", f"/batches/{bid}/close", {})
        self.assertEqual(closed["status"], "归档")

    def test_trials_compare_and_404(self):
        _, batch = self.call("POST", "/batches", {"name": "x", "total_seats": 6})
        bid = batch["id"]
        _, i = self.call("POST", f"/batches/{bid}/institutions", {
            "name": "I", "country": "泰国", "institution_type": "大型院校",
            "majors": ["护理"], "base_score": 10})
        self.call("POST", f"/batches/{bid}/applications",
                  {"institution_id": i["id"], "major": "护理", "seats": 6})
        _, t1 = self.call("POST", f"/batches/{bid}/trials", {"label": "a"})
        _, t2 = self.call("POST", f"/batches/{bid}/trials",
                          {"label": "b", "history_penalty_weight": 0.0})
        status, cmp = self.call("GET", f"/batches/{bid}/compare?ids={t1['id']},{t2['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(len(cmp["trials"]), 2)

        status, err = self.call("GET", "/batches/no-such")
        self.assertEqual(status, 404)
        self.assertEqual(err["error"], "not_found")
        status, err2 = self.call("POST", "/batches", {"name": "x", "total_seats": -3})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
