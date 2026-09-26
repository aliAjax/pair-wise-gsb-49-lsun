import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, CapacityShortfall, Conflict, ValidationError


UW = Actor("uw-1", "underwriter")
CLAIMS = Actor("cl-1", "claims_officer")
FIN = Actor("fin-1", "finance")

# 层宽8,000,000 × 分保40% = 合约容量3,200,000元（人民币）
CONTRACT = {
    "event_id": "CAT-2026-01",
    "attachment": 1000000.0,
    "limit": 9000000.0,
    "cession_pct": 0.4,
    "loss_amount": 3000000.0,
    "reinstatement_pct": 0.15,
    "aggregate_prior": 0.0,
}
REPORT_DATE = "2026-08-01"
SETTLE_DATE = "2026-09-15"


class FxCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "fx.db")
        self.service = build_service(self.db_path)
        self.service.upsert_fx_rate(FIN, {"currency": "USD", "date": REPORT_DATE, "rate": 7.10, "source": "央行中间价"})
        self.service.upsert_fx_rate(FIN, {"currency": "USD", "date": SETTLE_DATE, "rate": 7.30, "source": "央行中间价"})

    def tearDown(self):
        self.temp.cleanup()

    def _create_bound(self, reference, data=None):
        record = self.service.create(UW, reference, data or dict(CONTRACT))
        return self.service.act(UW, record["id"], record["version"], "bind", {"underwriter_id": "UW-8"})

    def _submit_usd(self, record):
        return self.service.act(CLAIMS, record["id"], record["version"], "submit_claim", {
            "claim_number": "CLM-USD-1",
            "event_id": "CAT-2026-01",
            "currency": "USD",
            "reported_loss": 2000000.0,
            "loss_report_date": REPORT_DATE,
        })

    def _calculate(self, record, approved_loss=2000000.0):
        return self.service.act(CLAIMS, record["id"], record["version"], "calculate", {"approved_loss": approved_loss})

    def _settle(self, record, reference="PAY-USD-1"):
        return self.service.act(FIN, record["id"], record["version"], "settle", {
            "payment_reference": reference,
            "settle_date": SETTLE_DATE,
        })


class FxWorkflowTest(FxCase):
    def test_occupancy_frozen_at_report_date_and_bill_at_settle_date(self):
        record = self._submit_usd(self._create_bound("RI-USD-1"))
        record = self._calculate(record)
        payload = record["payload"]
        # 核定：按报损日汇率7.10冻结额度占用
        self.assertEqual(payload["loss_currency"], "USD")
        self.assertEqual(payload["recoverable_fc"], 400000.0)
        self.assertEqual(payload["recoverable_amount"], 2840000.0)
        self.assertEqual(payload["report_rate"]["rate"], 7.10)
        self.assertEqual(payload["report_rate"]["date"], REPORT_DATE)
        self.assertEqual(payload["reinstatement_premium"], 426000.0)

        record = self._settle(record)
        payload = record["payload"]
        # 结算：按结算日汇率7.30换算，汇率与账单一并冻结
        self.assertEqual(payload["settle_rate"]["rate"], 7.30)
        self.assertEqual(payload["settle_rate"]["date"], SETTLE_DATE)
        self.assertEqual(payload["payable_fc"], 400000.0)
        self.assertEqual(payload["payable_cny"], 2920000.0)
        self.assertEqual(payload["occupancy_cny"], 2840000.0)
        self.assertEqual(payload["fx_variance_cny"], 80000.0)

        bill = record["bill"]
        self.assertEqual(bill["currency"], "USD")
        self.assertEqual(bill["amount_fc"], 400000.0)
        self.assertEqual(bill["rate"], 7.30)
        self.assertEqual(bill["rate_date"], SETTLE_DATE)
        self.assertEqual(bill["amount_cny"], 2920000.0)
        self.assertEqual(bill["occupancy_cny"], 2840000.0)
        self.assertEqual(bill["fx_variance_cny"], 80000.0)
        self.assertEqual(bill["status"], "settled")

    def test_settled_bill_immune_to_new_rates(self):
        record = self._settle(self._calculate(self._submit_usd(self._create_bound("RI-USD-1"))))
        bill_id = record["bill"]["id"]
        # 结算日汇率事后被修正，已冻结账单与占用不变
        self.service.upsert_fx_rate(FIN, {"currency": "USD", "date": SETTLE_DATE, "rate": 7.35, "source": "修正"})
        reloaded = self.service.get_record(FIN, record["id"])
        self.assertEqual(reloaded["bill"]["id"], bill_id)
        self.assertEqual(reloaded["bill"]["rate"], 7.30)
        self.assertEqual(reloaded["bill"]["amount_cny"], 2920000.0)
        self.assertEqual(reloaded["payload"]["settle_rate"]["rate"], 7.30)
        self.assertEqual(reloaded["payload"]["recoverable_amount"], 2840000.0)

    def test_missing_rate_blocks_calculate(self):
        record = self.service.create(UW, "RI-EUR-1", dict(CONTRACT))
        record = self.service.act(UW, record["id"], record["version"], "bind", {"underwriter_id": "UW-8"})
        record = self.service.act(CLAIMS, record["id"], record["version"], "submit_claim", {
            "claim_number": "CLM-EUR-1",
            "event_id": "CAT-2026-01",
            "currency": "EUR",
            "reported_loss": 1000000.0,
            "loss_report_date": "2026-08-02",
        })
        with self.assertRaises(ValidationError) as ctx:
            self._calculate(record, 1000000.0)
        self.assertIn("EUR", str(ctx.exception))

    def test_foreign_claim_requires_report_date(self):
        record = self._create_bound("RI-USD-2")
        with self.assertRaises(ValidationError):
            self.service.act(CLAIMS, record["id"], record["version"], "submit_claim", {
                "claim_number": "CLM-USD-2",
                "event_id": "CAT-2026-01",
                "currency": "USD",
                "reported_loss": 1000000.0,
            })


class CapacityShortfallTest(FxCase):
    def test_shortfall_lists_fc_cny_and_gap(self):
        # 两案同事件，都在核定前创建；首案核定占用后，第二案核定时额度不足
        first = self._submit_usd(self._create_bound("RI-USD-A"))
        second = self._submit_usd(self._create_bound("RI-USD-B"))
        first = self._calculate(first)
        self.assertEqual(first["state"], "calculated")
        with self.assertRaises(CapacityShortfall) as ctx:
            self._calculate(second)
        details = ctx.exception.details
        # 容量3,200,000，首案占用2,840,000，余360,000；需2,840,000，缺口2,480,000
        self.assertEqual(details["capacity_cny"], 3200000.0)
        self.assertEqual(details["used_cny"], 2840000.0)
        self.assertEqual(details["available_cny"], 360000.0)
        self.assertEqual(details["required"]["amount_fc"], 400000.0)
        self.assertEqual(details["required"]["amount_cny"], 2840000.0)
        self.assertEqual(details["required"]["currency"], "USD")
        self.assertEqual(details["shortfall_cny"], 2480000.0)
        self.assertAlmostEqual(details["shortfall"]["amount_fc"], 2480000.0 / 7.10, places=2)
        self.assertEqual(details["shortfall"]["amount_cny"], 2480000.0)


class BatchTest(FxCase):
    def _settle_cny_claim(self):
        data = dict(CONTRACT)
        data["event_id"] = "CAT-2026-02"
        data["limit"] = 5000000.0
        record = self.service.create(UW, "RI-CNY-1", data)
        record = self.service.act(UW, record["id"], record["version"], "bind", {"underwriter_id": "UW-8"})
        record = self.service.act(CLAIMS, record["id"], record["version"], "submit_claim", {
            "claim_number": "CLM-CNY-1",
            "event_id": "CAT-2026-02",
        })
        record = self.service.act(CLAIMS, record["id"], record["version"], "calculate", {"approved_loss": 2800000.0})
        return self.service.act(FIN, record["id"], record["version"], "settle", {"payment_reference": "PAY-CNY-1"})

    def test_batch_summary_by_currency_and_restart_reconcile(self):
        usd_record = self._settle(self._calculate(self._submit_usd(self._create_bound("RI-USD-1"))))
        cny_record = self._settle_cny_claim()
        self.assertEqual(cny_record["bill"]["currency"], "CNY")
        self.assertEqual(cny_record["bill"]["rate"], 1.0)
        self.assertEqual(cny_record["bill"]["amount_cny"], 720000.0)

        batch = self.service.create_batch(FIN, "BATCH-2026-09", "九月巨灾结算批次")
        batch = self.service.add_batch_item(FIN, batch["id"], record_id=usd_record["id"])
        batch = self.service.add_batch_item(FIN, batch["id"], bill_id=cny_record["bill"]["id"])
        summary = {item["currency"]: item for item in batch["summary_by_currency"]}
        self.assertEqual(summary["USD"]["count"], 1)
        self.assertEqual(summary["USD"]["amount_fc"], 400000.0)
        self.assertEqual(summary["USD"]["amount_cny"], 2920000.0)
        self.assertEqual(summary["USD"]["fx_variance_cny"], 80000.0)
        self.assertEqual(summary["CNY"]["count"], 1)
        self.assertEqual(summary["CNY"]["amount_cny"], 720000.0)

        # 重复加入同一账单被拒绝
        with self.assertRaises(Conflict):
            self.service.add_batch_item(FIN, batch["id"], bill_id=cny_record["bill"]["id"])

        # 入批动作写入赔案审计时间线
        timeline = self.service.timeline(FIN, usd_record["id"])
        self.assertEqual(timeline[-1]["action"], "batched")

        # 服务重开后按案件与批次核对：账单冻结值与分币种汇总一致
        restarted = build_service(self.db_path)
        batch2 = restarted.get_batch(FIN, batch["id"])
        summary2 = {item["currency"]: item for item in batch2["summary_by_currency"]}
        self.assertEqual(summary2["USD"]["amount_cny"], 2920000.0)
        self.assertEqual(summary2["CNY"]["amount_cny"], 720000.0)
        reloaded = restarted.get_record(FIN, usd_record["id"])
        self.assertEqual(reloaded["bill"]["rate"], 7.30)
        self.assertEqual(reloaded["bill"]["amount_cny"], 2920000.0)
        self.assertEqual(reloaded["payload"]["recoverable_amount"], 2840000.0)


if __name__ == "__main__":
    unittest.main()
