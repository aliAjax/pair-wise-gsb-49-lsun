import json
import tempfile
import threading
import unittest
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

from app import build_service
from src.http_api import create_server


class HttpFxTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db = str(Path(self.temp.name) / "http.db")
        service = build_service(db)
        self.server = create_server("127.0.0.1", 0, service, Path(__file__).resolve().parent.parent / "static")
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, path, body=None, role="finance"):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"X-User-Id": "u1", "X-Role": role, "X-Org": "o1"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read().decode("utf-8")
        conn.close()
        return response.status, json.loads(raw) if raw else {}

    def test_fx_endpoints(self):
        status, body = self.request("POST", "/api/fx/rates", {"data": {"currency": "usd", "rate_date": "2026-03-01", "rate": "7.10"}})
        self.assertEqual(status, 201)
        self.assertEqual(body["currency"], "USD")
        status, body = self.request("GET", "/api/fx/rates/USD/2026-03-05")
        self.assertEqual(status, 200)
        self.assertEqual(body["rate_date"], "2026-03-01")
        status, body = self.request("GET", "/api/fx/rates/EUR/2026-03-05")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "fx_rate_missing")
        self.assertEqual(body["details"]["currency"], "EUR")

    def test_gap_error_body_and_batch_flow(self):
        self.request("POST", "/api/fx/rates", {"data": {"currency": "USD", "rate_date": "2026-03-01", "rate": "7.10"}})
        self.request("POST", "/api/fx/rates", {"data": {"currency": "USD", "rate_date": "2026-09-20", "rate": "7.25"}})
        contract = {'event_id': 'CAT-WEB', 'attachment': 1000000.0, 'limit': 5000000.0,
                    'cession_pct': 1.0, 'loss_amount': 0.0, 'reinstatement_pct': 0.1,
                    'aggregate_prior': 100000.0}
        status, record = self.request("POST", "/api/records", {"reference": "RI-WEB-1", "data": contract}, role="underwriter")
        self.assertEqual(status, 201)
        rid, version = record["id"], record["version"]
        status, record = self.request("POST", f"/api/records/{rid}/actions/bind",
                                      {"expected_version": version, "data": {"underwriter_id": "UW"}}, role="underwriter")
        version = record["version"]
        status, record = self.request("POST", f"/api/records/{rid}/actions/submit_claim", {
            "expected_version": version,
            "data": {"claim_number": "C1", "event_id": "CAT-WEB", "loss_currency": "USD",
                     "loss_amount": 710000.0, "loss_date": "2026-03-02"}}, role="claims_officer")
        self.assertEqual(status, 200)
        version = record["version"]
        status, body = self.request("POST", f"/api/records/{rid}/actions/calculate",
                                    {"expected_version": version, "data": {"approved_loss": 710000.0}},
                                    role="claims_officer")
        self.assertEqual(status, 409)
        self.assertEqual(body["details"]["reason"], "capacity_shortfall")
        self.assertAlmostEqual(body["details"]["gap_cny"], 100000.0)

        # 事件核对接口
        status, event = self.request("GET", "/api/reconcile/event?event_id=CAT-WEB")
        self.assertEqual(status, 200)
        self.assertEqual(len(event["lines"]), 1)

        # 批次路由存在
        status, batches = self.request("GET", "/api/batches")
        self.assertEqual(status, 200)
        self.assertEqual(batches["items"], [])

    def test_static_claim_page(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/claim.html")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.getheader("Content-Type"))
        conn.close()


if __name__ == "__main__":
    unittest.main()
