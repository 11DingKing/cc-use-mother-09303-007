"""HTTP 适配层（标准库，无第三方依赖）。

所有写接口接受 JSON 请求体，并支持用 ``If-Match: <version>`` 头做乐观并发控制；
业务错误统一映射为 4xx 与 ``{"error", "message"}`` 结构。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .errors import QuotaError
from .service import QuotaService
from .store import JsonStore

Json = dict[str, Any]


def _make_handler(service: QuotaService) -> type[BaseHTTPRequestHandler]:
    class QuotaHandler(BaseHTTPRequestHandler):
        server_version = "QuotaService/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
            return

        # -- 基础工具 ----------------------------------------------------
        def _read_json(self) -> Json:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise QuotaError("请求体不是合法 JSON", code="invalid_input") from exc
            if not isinstance(body, dict):
                raise QuotaError("请求体必须是 JSON 对象", code="invalid_input")
            if "if-match" in self.headers:
                body.setdefault("expected_version", int(self.headers["If-Match"]))
            return body

        def _send(self, status: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # 客户端已断开，无需再写响应

        def _handle(self, fn: Callable[[], Any], status: int = 200) -> None:
            try:
                self._send(status, fn())
            except QuotaError as exc:
                self._send(exc.status, exc.to_dict())
            except (ValueError, KeyError, TypeError) as exc:
                self._send(400, {"error": "invalid_input", "message": str(exc)})

        # -- 路由 --------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            query = parse_qs(urlsplit(self.path).query)

            def not_found() -> None:
                self._send(404, {"error": "not_found", "message": "未知路径"})

            if parts == ["batches"]:
                self._handle(lambda: {"batches": service.list_batches()})
            elif len(parts) == 2 and parts[0] == "batches":
                self._handle(lambda: service.get_batch(parts[1]))
            elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "trials":
                self._handle(lambda: {"trials": service.list_trials(parts[1])})
            elif len(parts) == 4 and parts[0] == "batches" and parts[2] == "trials":
                self._handle(lambda: service.get_trial(parts[1], parts[3]))
            elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "compare":
                ids = query.get("ids")
                id_list = ids[0].split(",") if ids else None
                self._handle(lambda: service.compare_trials(parts[1], id_list))
            elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "ledger":
                self._handle(lambda: service.ledger(parts[1]))
            elif len(parts) == 5 and parts[0] == "batches" and parts[2] == "institutions" and parts[4] == "view":
                self._handle(lambda: service.institution_view(parts[1], parts[3]))
            else:
                not_found()

        def do_POST(self) -> None:  # noqa: N802
            parts = [p for p in urlsplit(self.path).path.split("/") if p]

            try:
                body = self._read_json()  # 整个请求只读一次
            except QuotaError as exc:
                self._send(exc.status, exc.to_dict())
                return

            if parts == ["batches"]:
                self._handle(
                    lambda: service.create_batch(body["name"], int(body["total_seats"])),
                    status=201,
                )
            elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "phase-trial":
                self._handle(lambda: service.begin_trial_phase(parts[1]))
            elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "institutions":
                self._handle(
                    lambda: service.add_institution(
                        parts[1],
                        name=body["name"],
                        country=body["country"],
                        institution_type=body["institution_type"],
                        majors=body["majors"],
                        base_score=float(body.get("base_score", 0)),
                        history_score=float(body.get("history_score", 0)),
                        institution_id=body.get("id"),
                    ),
                    status=201,
                )
            elif len(parts) == 5 and parts[0] == "batches" and parts[2] == "institutions" and parts[4] == "eligibility":
                self._handle(
                    lambda: service.set_eligibility(
                        parts[1], parts[3], bool(body["eligible"]), body.get("reason", "")
                    )
                )
            elif len(parts) == 5 and parts[0] == "batches" and parts[2] == "institutions" and parts[4] == "history":
                self._handle(
                    lambda: service.set_history_score(parts[1], parts[3], float(body["history_score"]))
                )
            elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "applications":
                self._handle(
                    lambda: service.add_application(
                        parts[1], body["institution_id"], body["major"], int(body["seats"]), body.get("note", "")
                    ),
                    status=201,
                )
            elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "capacities":
                self._handle(lambda: service.set_capacities(parts[1], body["rules"]))
            elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "guarantees":
                self._handle(
                    lambda: service.add_guarantee(
                        parts[1], body["dimension"], body["dim_value"], int(body["min_seats"]), body.get("note", "")
                    ),
                    status=201,
                )
            elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "trials":
                self._handle(
                    lambda: service.create_trial(
                        parts[1],
                        label=body.get("label", ""),
                        history_penalty_weight=float(body.get("history_penalty_weight", 1.0)),
                        expected_version=body.get("expected_version"),
                    ),
                    status=201,
                )
            elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "publish":
                self._handle(lambda: service.publish(parts[1], body["trial_id"]))
            elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "confirmations":
                self._handle(
                    lambda: service.confirm(
                        parts[1],
                        body["application_id"],
                        bool(body["accepted"]),
                        int(body["seats"]) if body.get("seats") is not None else None,
                        expected_version=body.get("expected_version"),
                    )
                )
            elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "declines":
                self._handle(
                    lambda: service.decline(
                        parts[1], body["application_id"], int(body["seats"]), body.get("reason", "")
                    )
                )
            elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "revocations":
                self._handle(
                    lambda: service.revoke_eligibility(parts[1], body["institution_id"], body.get("reason", ""))
                )
            elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "transfers":
                self._handle(
                    lambda: service.transfer(
                        parts[1],
                        from_application_id=body["from_application_id"],
                        to_application_id=body["to_application_id"],
                        seats=int(body["seats"]),
                        reason=body.get("reason", ""),
                        expected_version=body.get("expected_version"),
                    )
                )
            elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "backfill-start":
                self._handle(lambda: service.start_backfill(parts[1]))
            elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "backfill-run":
                self._handle(lambda: service.run_backfill(parts[1]))
            elif len(parts) == 3 and parts[0] == "batches" and parts[2] == "close":
                self._handle(lambda: service.close_batch(parts[1]))
            else:
                self._send(404, {"error": "not_found", "message": "未知路径"})

    return QuotaHandler


def create_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    store = JsonStore(db_path)
    service = QuotaService(store)
    server = ThreadingHTTPServer((host, port), _make_handler(service))
    server.service = service  # type: ignore[attr-defined]
    return server


def serve(host: str = "127.0.0.1", port: int = 8080, db_path: str = "data/quota.json") -> None:
    server = create_server(host, port, db_path)
    print(f"名额管理服务监听 http://{host}:{port}（数据文件 {db_path}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
