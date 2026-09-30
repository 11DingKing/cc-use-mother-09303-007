"""名额管理服务端的 HTTP 接口（标准库实现，零第三方依赖）。

路由见模块底部 ``ROUTES``；所有写接口都接受 ``version`` 字段做乐观
并发控制，响应统一带 ``batch_version``。线程模型为
:class:`http.server.ThreadingHTTPServer``，并发写在服务层按批次加锁
串行化，容量校验在锁内完成。

启动：``python3 -m quota_service.server [--host 127.0.0.1] [--port 8000]``
"""
from __future__ import annotations

import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .models import QuotaError
from .service import QuotaService


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return str(obj)


class QuotaHandler(BaseHTTPRequestHandler):
    service: QuotaService = QuotaService()

    # ----- HTTP 基础 ---------------------------------------------------------

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise QuotaError("INVALID_JSON", f"请求体不是合法 JSON：{exc}") from None
        if not isinstance(data, dict):
            raise QuotaError("INVALID_JSON", "请求体必须是 JSON 对象")
        return data

    def _send(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, exc: QuotaError) -> None:
        status = {
            "VERSION_CONFLICT": HTTPStatus.CONFLICT,
            "UNKNOWN_BATCH": HTTPStatus.NOT_FOUND,
            "UNKNOWN_SCENARIO": HTTPStatus.NOT_FOUND,
            "UNKNOWN_INSTITUTION": HTTPStatus.NOT_FOUND,
            "RULE_NOT_FOUND": HTTPStatus.NOT_FOUND,
            "NO_WAITLIST_CANDIDATE": HTTPStatus.CONFLICT,
            "INPUT_FROZEN": HTTPStatus.CONFLICT,
            "OVERSELL": HTTPStatus.CONFLICT,
            "DIMENSION_OVERSELL": HTTPStatus.CONFLICT,
            "NEGATIVE_BALANCE": HTTPStatus.CONFLICT,
        }.get(exc.code, HTTPStatus.BAD_REQUEST)
        self._send({"error": exc.to_dict()}, status)

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静一点
        return

    # ----- 路由分发 ----------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch(write=False)

    def do_POST(self) -> None:
        self._dispatch(write=True)

    def do_PUT(self) -> None:
        self._dispatch(write=True)

    def do_DELETE(self) -> None:
        self._dispatch(write=True)

    def _dispatch(self, write: bool) -> None:
        try:
            body = self._read_body() if write else {}
            parts = urlsplit(self.path)
            path, query = parts.path, parse_qs(parts.query)
            for pattern, methods, fn in ROUTES:
                m = re.fullmatch(pattern, path)
                if m and self.command in methods:
                    result = fn(self, m.groupdict(), body, query)
                    self._send(result)
                    return
            self._send({"error": {"code": "NOT_FOUND", "message": f"无此接口：{self.command} {path}"}},
                       HTTPStatus.NOT_FOUND)
        except QuotaError as exc:
            self._send_error(exc)
        except Exception as exc:  # noqa: BLE001 - 边界层兜底
            self._send({"error": {"code": "INTERNAL", "message": f"服务内部错误：{exc}"}},
                       HTTPStatus.INTERNAL_SERVER_ERROR)

    # ----- 版本回传 ----------------------------------------------------------

    def _with_version(self, batch_id: str, result: dict | None = None) -> dict:
        version = self.service.batch_view(batch_id)["version"]
        out = result if result is not None else {}
        out["batch_version"] = version
        return out

    # ----- 具体端到端用例 -----------------------------------------------------

    def create_batch(self, _p: dict, body: dict, _q: dict) -> dict:
        out = self.service.create_batch(
            name=str(body.get("name", "")),
            total_capacity=int(body.get("total_capacity", 0)),
        )
        return self._with_version(out["batch_id"], out)

    def list_batches(self, _p: dict, _b: dict, _q: dict) -> dict:
        return {"batches": self.service.list_batches()}

    def get_batch(self, p: dict, _b: dict, _q: dict) -> dict:
        return self.service.batch_view(p["bid"])

    def set_capacity(self, p: dict, body: dict, _q: dict) -> dict:
        self.service.set_total_capacity(p["bid"], int(body["seats"]), body.get("version"))
        return self._with_version(p["bid"])

    def set_dimension(self, p: dict, body: dict, _q: dict) -> dict:
        self.service.set_dimension_capacity(
            p["bid"], str(body["dimension"]), str(body["value"]),
            body.get("capacity"), body.get("version"),
        )
        return self._with_version(p["bid"])

    def set_guarantee(self, p: dict, body: dict, _q: dict) -> dict:
        self.service.set_guarantee(
            p["bid"], str(body["dimension"]), str(body["value"]),
            int(body["seats"]), body.get("version"),
        )
        return self._with_version(p["bid"])

    def remove_guarantee(self, p: dict, _b: dict, q: dict) -> dict:
        self.service.remove_guarantee(
            p["bid"], str(q["dimension"][0]), str(q["value"][0]),
            int(q["version"][0]) if q.get("version") else None,
        )
        return self._with_version(p["bid"])

    def upsert_application(self, p: dict, body: dict, _q: dict) -> dict:
        self.service.upsert_application(p["bid"], body, body.get("version"))
        return self._with_version(p["bid"])

    def set_eligibility(self, p: dict, body: dict, _q: dict) -> dict:
        self.service.set_eligibility(p["bid"], str(body["institution_id"]),
                                     bool(body["eligible"]), body.get("version"))
        return self._with_version(p["bid"])

    def set_history(self, p: dict, body: dict, _q: dict) -> dict:
        self.service.set_history(p["bid"], str(body["institution_id"]),
                                 str(body.get("history", "0")), body.get("version"))
        return self._with_version(p["bid"])

    def run_scenario(self, p: dict, body: dict, _q: dict) -> dict:
        view = self.service.run_scenario(p["bid"], str(body.get("name", "")), body.get("version"))
        return self._with_version(p["bid"], view)

    def get_scenario(self, p: dict, _b: dict, _q: dict) -> dict:
        return self.service.scenario_view(p["bid"], p["sid"])

    def compare(self, p: dict, body: dict, _q: dict) -> dict:
        ids = body.get("scenario_ids")
        if not isinstance(ids, list):
            raise QuotaError("INVALID_COMPARISON", "scenario_ids 必须是列表")
        return self.service.compare_scenarios(p["bid"], [str(x) for x in ids])

    def publish(self, p: dict, body: dict, _q: dict) -> dict:
        self.service.publish(p["bid"], str(body["scenario_id"]), body.get("version"))
        return self._with_version(p["bid"], self.service.batch_view(p["bid"]))

    def close(self, p: dict, body: dict, _q: dict) -> dict:
        self.service.close_batch(p["bid"], body.get("version"))
        return self._with_version(p["bid"], self.service.batch_view(p["bid"]))

    def confirm(self, p: dict, body: dict, _q: dict) -> dict:
        out = self.service.confirm(p["bid"], str(body["institution_id"]), body.get("version"))
        return self._with_version(p["bid"], out)

    def relinquish(self, p: dict, body: dict, _q: dict) -> dict:
        out = self.service.relinquish(
            p["bid"], str(body["institution_id"]), int(body["seats"]),
            str(body.get("reason", "")), body.get("version"),
        )
        return self._with_version(p["bid"], out)

    def revoke(self, p: dict, body: dict, _q: dict) -> dict:
        out = self.service.revoke_after_publish(
            p["bid"], str(body["institution_id"]),
            str(body.get("reason", "")), body.get("version"),
        )
        return self._with_version(p["bid"], out)

    def transfer(self, p: dict, body: dict, _q: dict) -> dict:
        out = self.service.transfer(
            p["bid"], str(body["sender"]), str(body["receiver"]),
            int(body["seats"]), str(body.get("reason", "")), body.get("version"),
        )
        return self._with_version(p["bid"], out)

    def waitlist_next(self, p: dict, _b: dict, _q: dict) -> dict:
        cand = self.service.waitlist_candidate(p["bid"])
        return {"candidate": cand}

    def promote(self, p: dict, body: dict, _q: dict) -> dict:
        out = self.service.promote_next(
            p["bid"],
            seats=body.get("seats"),
            reason=str(body.get("reason", "")),
            expected_version=body.get("version"),
        )
        return self._with_version(p["bid"], out)

    def institution(self, p: dict, _b: dict, _q: dict) -> dict:
        return self.service.institution_view(p["bid"], p["iid"])


H = Callable[[QuotaHandler, dict, dict, dict], dict]
ROUTES: list[tuple[str, frozenset[str], H]] = [
    (r"/batches", frozenset({"POST"}), QuotaHandler.create_batch),
    (r"/batches", frozenset({"GET"}), QuotaHandler.list_batches),
    (r"/batches/(?P<bid>[^/]+)", frozenset({"GET"}), QuotaHandler.get_batch),
    (r"/batches/(?P<bid>[^/]+)/capacity", frozenset({"PUT"}), QuotaHandler.set_capacity),
    (r"/batches/(?P<bid>[^/]+)/dimensions", frozenset({"PUT"}), QuotaHandler.set_dimension),
    (r"/batches/(?P<bid>[^/]+)/guarantees", frozenset({"PUT"}), QuotaHandler.set_guarantee),
    (r"/batches/(?P<bid>[^/]+)/guarantees", frozenset({"DELETE"}), QuotaHandler.remove_guarantee),
    (r"/batches/(?P<bid>[^/]+)/applications", frozenset({"PUT"}), QuotaHandler.upsert_application),
    (r"/batches/(?P<bid>[^/]+)/eligibility", frozenset({"POST"}), QuotaHandler.set_eligibility),
    (r"/batches/(?P<bid>[^/]+)/history", frozenset({"POST"}), QuotaHandler.set_history),
    (r"/batches/(?P<bid>[^/]+)/scenarios", frozenset({"POST"}), QuotaHandler.run_scenario),
    (r"/batches/(?P<bid>[^/]+)/scenarios/(?P<sid>[^/]+)", frozenset({"GET"}), QuotaHandler.get_scenario),
    (r"/batches/(?P<bid>[^/]+)/compare", frozenset({"POST"}), QuotaHandler.compare),
    (r"/batches/(?P<bid>[^/]+)/publish", frozenset({"POST"}), QuotaHandler.publish),
    (r"/batches/(?P<bid>[^/]+)/close", frozenset({"POST"}), QuotaHandler.close),
    (r"/batches/(?P<bid>[^/]+)/confirm", frozenset({"POST"}), QuotaHandler.confirm),
    (r"/batches/(?P<bid>[^/]+)/relinquish", frozenset({"POST"}), QuotaHandler.relinquish),
    (r"/batches/(?P<bid>[^/]+)/revoke", frozenset({"POST"}), QuotaHandler.revoke),
    (r"/batches/(?P<bid>[^/]+)/transfer", frozenset({"POST"}), QuotaHandler.transfer),
    (r"/batches/(?P<bid>[^/]+)/waitlist/next", frozenset({"GET"}), QuotaHandler.waitlist_next),
    (r"/batches/(?P<bid>[^/]+)/promote", frozenset({"POST"}), QuotaHandler.promote),
    (r"/batches/(?P<bid>[^/]+)/institutions/(?P<iid>[^/]+)", frozenset({"GET"}), QuotaHandler.institution),
]


def build_server(host: str = "127.0.0.1", port: int = 8000,
                 service: QuotaService | None = None) -> ThreadingHTTPServer:
    service = service or QuotaService()

    class _BoundHandler(QuotaHandler):
        pass

    _BoundHandler.service = service
    return ThreadingHTTPServer((host, port), _BoundHandler)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="国际培训名额配置服务端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    httpd = build_server(args.host, args.port)
    print(f"名额管理服务已启动：http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
