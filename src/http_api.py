"""HTTP 路由与统一错误输出。"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from .domain import Actor, DomainError, PermissionDenied, ValidationError


RECORD_RE = re.compile(r"^/api/records/(\d+)$")
ACTION_RE = re.compile(r"^/api/records/(\d+)/actions/([a-z_]+)$")
AUDIT_RE = re.compile(r"^/api/records/(\d+)/audit$")
BATCH_RE = re.compile(r"^/api/batches/(\d+)$")
BATCH_ITEMS_RE = re.compile(r"^/api/batches/(\d+)/records/(\d+)$")
BATCH_RECONCILE_RE = re.compile(r"^/api/batches/(\d+)/reconcile$")
RATE_RE = re.compile(r"^/api/fx/rates/([A-Za-z]{3})/(\d{4}-\d{2}-\d{2})$")


def make_handler(service: Any, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        server_version = "reinsurance-exposure/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _actor(self) -> Actor:
            user_id = self.headers.get("X-User-Id", "").strip()
            role = self.headers.get("X-Role", "").strip()
            if not user_id or not role:
                raise PermissionDenied("缺少X-User-Id或X-Role")
            return Actor(user_id=user_id, role=role, organization=self.headers.get("X-Org", ""))

        def _body(self) -> Dict[str, Any]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValidationError("Content-Length无效") from exc
            if length > 1024 * 1024:
                raise ValidationError("请求体过大")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体必须是JSON") from exc
            if not isinstance(data, dict):
                raise ValidationError("JSON顶层必须是对象")
            return data

        def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
            if content_type.startswith("application/json"):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            else:
                body = payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_error(self, exc: Exception) -> None:
            if isinstance(exc, DomainError):
                payload = {"error": exc.code, "message": str(exc)}
                if getattr(exc, "details", None):
                    payload["details"] = exc.details
                self._send(exc.status, payload)
            else:
                self._send(500, {"error": "internal_error", "message": "服务内部错误"})

        def _static(self, parsed_path: str) -> bool:
            if parsed_path in ("/", "/index.html"):
                page = (static_dir / "index.html").read_bytes()
                self._send(200, page, "text/html; charset=utf-8")
                return True
            if parsed_path == "/claim.html":
                page = (static_dir / "claim.html").read_bytes()
                self._send(200, page, "text/html; charset=utf-8")
                return True
            return False

        def do_GET(self) -> None:
            try:
                parsed = urlparse(self.path)
                if parsed.path == "/health":
                    self._send(200, {"status": "ok", "service": "reinsurance-exposure", "database": service.repository.health()})
                    return
                if self._static(parsed.path):
                    return
                query = parse_qs(parsed.query)
                if parsed.path == "/api/records":
                    records = service.list_records(
                        self._actor(),
                        state=query.get("state", [None])[0],
                        limit=int(query.get("limit", ["100"])[0]),
                        event_id=query.get("event_id", [None])[0],
                    )
                    self._send(200, {"items": records})
                    return
                if parsed.path == "/api/fx/rates":
                    rows = service.list_rates(
                        self._actor(),
                        currency=query.get("currency", [None])[0],
                        limit=int(query.get("limit", ["200"])[0]),
                    )
                    self._send(200, {"items": rows})
                    return
                match = RATE_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_rate(self._actor(), match.group(1).upper(), match.group(2)))
                    return
                if parsed.path == "/api/batches":
                    self._send(200, {"items": service.list_batches(self._actor(), int(query.get("limit", ["100"])[0]))})
                    return
                match = BATCH_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_batch(self._actor(), int(match.group(1))))
                    return
                match = BATCH_RECONCILE_RE.match(parsed.path)
                if match:
                    self._send(200, service.reconcile_batch(self._actor(), int(match.group(1))))
                    return
                if parsed.path == "/api/reconcile/event":
                    event_id = query.get("event_id", [None])[0]
                    self._send(200, service.reconcile_event(self._actor(), event_id))
                    return
                match = RECORD_RE.match(parsed.path)
                if match:
                    self._send(200, service.get_record(self._actor(), int(match.group(1))))
                    return
                match = AUDIT_RE.match(parsed.path)
                if match:
                    record_id = int(match.group(1))
                    self._send(200, {"items": service.timeline(self._actor(), record_id),
                                     "batches": service.batches_for_record(self._actor(), record_id)})
                    return
                if parsed.path == "/api/stats":
                    self._send(200, service.stats(self._actor()))
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                body = self._body()
                if parsed.path == "/api/records":
                    record = service.create(self._actor(), body.get("reference", ""), body.get("data", {}))
                    self._send(201, record)
                    return
                if parsed.path == "/api/fx/rates":
                    self._send(201, service.upsert_rate(self._actor(), body.get("data", body)))
                    return
                if parsed.path == "/api/batches":
                    self._send(201, service.create_batch(self._actor(), body.get("data", body)))
                    return
                match = BATCH_ITEMS_RE.match(parsed.path)
                if match:
                    item = service.add_batch_item(self._actor(), int(match.group(1)), int(match.group(2)))
                    self._send(201, item)
                    return
                match = ACTION_RE.match(parsed.path)
                if match:
                    version = body.get("expected_version")
                    if not isinstance(version, int):
                        raise ValidationError("expected_version必须是整数")
                    record = service.act(self._actor(), int(match.group(1)), version, match.group(2), body.get("data", {}))
                    self._send(200, record)
                    return
                self._send(404, {"error": "not_found", "message": "路径不存在"})
            except Exception as exc:
                self._handle_error(exc)

    return Handler


def create_server(host: str, port: int, service: Any, static_dir: Path) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service, static_dir))
