import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError
from src.fx import FxRateMissing


def contract(event="CAT-2026-FX", prior=0.0):
    return {'event_id': event, 'attachment': 1000000.0, 'limit': 5000000.0,
            'cession_pct': 1.0, 'loss_amount': 0.0, 'reinstatement_pct': 0.1,
            'aggregate_prior': prior}


class FxWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "fx.db")
        self.service = build_service(self.db)
        self.uw = Actor("uw1", "underwriter")
        self.clm = Actor("clm1", "claims_officer")
        self.fin = Actor("fin1", "finance")

    def tearDown(self):
        self.temp.cleanup()

    def _rate(self, ccy, date, rate):
        return self.service.upsert_rate(self.fin, {"currency": ccy, "rate_date": date, "rate": str(rate), "source": "test"})

    def _create_bound(self, reference, event="CAT-2026-FX", prior=0.0):
        record = self.service.create(self.uw, reference, contract(event, prior))
        return self.service.act(self.uw, record["id"], record["version"], "bind", {"underwriter_id": "UW-1"})

    def _submitted(self, reference, ccy="USD", loss=500000.0, loss_date="2026-03-02", event="CAT-2026-FX", prior=0.0):
        record = self._create_bound(reference, event, prior)
        return self.service.act(self.clm, record["id"], record["version"], "submit_claim", {
            "claim_number": reference + "-CLM", "event_id": event,
            "loss_currency": ccy, "loss_amount": loss, "loss_date": loss_date})

    def _settle(self, record, approved, pay_ref, settle_date="2026-09-21"):
        record = self.service.act(self.clm, record["id"], record["version"], "calculate", {"approved_loss": approved})
        return self.service.act(self.fin, record["id"], record["version"], "settle",
                                {"payment_reference": pay_ref, "settle_date": settle_date})

    def test_fx_rate_lookup_uses_latest_not_later_than_business_date(self):
        self._rate("USD", "2026-03-01", "7.10")
        self._rate("USD", "2026-03-10", "7.25")
        rate = self.service.get_rate(self.fin, "USD", "2026-03-05")
        self.assertEqual(rate["rate_date"], "2026-03-01")
        self.assertEqual(float(rate["rate"]), 7.10)
        with self.assertRaises(FxRateMissing):
            self.service.get_rate(self.fin, "USD", "2026-02-28")

    def test_rate_upsert_keeps_one_row_per_currency_date(self):
        self._rate("EUR", "2026-03-01", "7.80")
        self._rate("EUR", "2026-03-01", "7.82")
        rows = self.service.list_rates(self.fin, currency="EUR")
        self.assertEqual(len(rows), 1)
        self.assertEqual(float(rows[0]["rate"]), 7.82)

    def test_only_finance_can_manage_rates(self):
        with self.assertRaises(PermissionDenied):
            self.service.upsert_rate(self.clm, {"currency": "USD", "rate_date": "2026-03-01", "rate": 7.1})

    def test_missing_rate_blocks_submit(self):
        record = self._create_bound("RI-FX-1")
        with self.assertRaises(FxRateMissing):
            self.service.act(self.clm, record["id"], record["version"], "submit_claim", {
                "claim_number": "C1", "event_id": "CAT-2026-FX",
                "loss_currency": "USD", "loss_amount": 1000, "loss_date": "2026-03-02"})

    def test_full_fx_workflow_occupancy_vs_settlement(self):
        self._rate("USD", "2026-03-01", "7.10")
        self._rate("USD", "2026-09-20", "7.25")
        record = self._submitted("RI-FX-1", loss=500000.0)
        p = record["payload"]
        self.assertEqual(p["loss_currency"], "USD")
        self.assertEqual(p["loss_fx"]["rate"], "7.10")
        self.assertAlmostEqual(p["loss_amount_cny"], 3550000.0, places=2)

        record = self.service.act(self.clm, record["id"], record["version"], "calculate", {"approved_loss": 500000.0})
        p = record["payload"]
        # 500,000*7.10 - 1,000,000 = 2,550,000 占用人民币；原币摊回 = 2,550,000/7.10
        self.assertAlmostEqual(p["recoverable_amount_cny"], 2550000.0, places=2)
        self.assertAlmostEqual(p["recoverable_amount_foreign"], 359154.93, places=2)
        self.assertEqual(p["occupancy_fx"]["rate_date"], "2026-03-01")

        record = self.service.act(self.fin, record["id"], record["version"], "settle",
                                  {"payment_reference": "PAY-FX-1", "settle_date": "2026-09-21"})
        p = record["payload"]
        self.assertAlmostEqual(p["payment_amount_cny"], 2603873.24, places=2)
        self.assertAlmostEqual(p["fx_delta_cny"], 53873.24, places=2)
        self.assertEqual(p["settlement_fx"]["rate"], "7.25")
        self.assertTrue(p["bill"]["frozen"])
        self.assertEqual(p["bill"]["payment_reference"], "PAY-FX-1")

    def test_settled_bill_does_not_change_with_new_rate(self):
        self._rate("USD", "2026-03-01", "7.10")
        self._rate("USD", "2026-09-20", "7.25")
        record = self._submitted("RI-FX-2", loss=500000.0)
        record = self._settle(record, 500000.0, "PAY-FX-2")
        bill = dict(record["payload"]["bill"])
        self._rate("USD", "2026-10-01", "7.40")
        again = self.service.get_record(self.fin, record["id"])
        self.assertEqual(again["payload"]["bill"], bill)
        self.assertEqual(again["state"], "settled")

    def test_capacity_gap_at_calculation_lists_foreign_cny_and_gap(self):
        # 事件前已累计占用100,000；本币核定折后层内摊回4,000,000，超出可用3,900,000
        self._rate("USD", "2026-03-01", "7.10")
        record = self._submitted("RI-FX-3", loss=710000.0, prior=100000.0)
        with self.assertRaises(Conflict) as caught:
            self.service.act(self.clm, record["id"], record["version"], "calculate", {"approved_loss": 710000.0})
        details = caught.exception.details
        self.assertEqual(details["reason"], "capacity_shortfall")
        self.assertEqual(details["stage"], "occupancy")
        self.assertEqual(details["currency"], "USD")
        self.assertAlmostEqual(details["foreign_amount"], 710000.0)
        self.assertAlmostEqual(details["capacity_cny"], 4000000.0)
        self.assertAlmostEqual(details["used_cny"], 100000.0)
        self.assertAlmostEqual(details["available_cny"], 3900000.0)
        self.assertAlmostEqual(details["claimed_cny"], 4000000.0)
        self.assertAlmostEqual(details["gap_cny"], 100000.0)
        self.assertIn("lines", details)
        # 核定未通过：状态与占用均未落库
        self.assertEqual(self.service.get_record(self.fin, record["id"])["state"], "claim_submitted")

    def test_settlement_gap_when_rate_moves_up(self):
        self._rate("USD", "2026-03-01", "7.10")
        self._rate("USD", "2026-09-20", "7.25")
        # 核定占用 700,000*7.10 - 1,000,000 = 3,970,000，在容量内
        record = self._submitted("RI-FX-4", loss=700000.0)
        record = self.service.act(self.clm, record["id"], record["version"], "calculate", {"approved_loss": 700000.0})
        self.assertAlmostEqual(record["payload"]["recoverable_amount_cny"], 3970000.0, places=2)
        with self.assertRaises(Conflict) as caught:
            self.service.act(self.fin, record["id"], record["version"], "settle",
                             {"payment_reference": "PAY-FX-4", "settle_date": "2026-09-21"})
        details = caught.exception.details
        self.assertEqual(details["stage"], "settlement")
        # 3,970,000 / 7.10 * 7.25 = 4,053,873.24，缺口 53,873.24
        self.assertAlmostEqual(details["gap_cny"], 53873.24, places=2)
        self.assertEqual(self.service.get_record(self.fin, record["id"])["state"], "calculated")

    def test_batch_groups_by_currency_and_reconciles_after_restart(self):
        self._rate("USD", "2026-03-01", "7.10")
        self._rate("USD", "2026-09-20", "7.25")
        first = self._settle(self._submitted("RI-FX-A", loss=300000.0), 300000.0, "PAY-A")
        second = self._settle(self._submitted("RI-FX-B", loss=200000.0), 200000.0, "PAY-B")

        with self.assertRaises(ValidationError):
            self.service.create_batch(self.fin, {"reference": "B-USD", "currency": "XXX"})
        batch = self.service.create_batch(self.fin, {"reference": "B-USD-2026-09", "currency": "USD", "note": "9月美元批次"})
        self.service.add_batch_item(self.fin, batch["id"], first["id"])
        self.service.add_batch_item(self.fin, batch["id"], second["id"])
        with self.assertRaises(Conflict):
            self.service.add_batch_item(self.fin, batch["id"], first["id"])

        view = self.service.get_batch(self.fin, batch["id"])
        self.assertEqual(len(view["items"]), 2)
        self.assertEqual(view["summary"][0]["currency"], "USD")
        self.assertEqual(view["summary"][0]["count"], 2)
        # 原币摊回 1,130,000/7.10 + 420,000/7.10
        self.assertAlmostEqual(view["summary"][0]["foreign_total"], 218309.86, places=2)
        self.assertAlmostEqual(view["summary"][0]["cny_total"], 1582746.48, places=2)

        # 重开服务（同一数据库文件），按批次与按事件核对均可用
        restarted = build_service(self.db)
        recon = restarted.reconcile_batch(self.fin, batch["id"])
        self.assertTrue(recon["consistent"])
        event = restarted.reconcile_event(self.fin, "CAT-2026-FX")
        self.assertEqual(len(event["lines"]), 2)
        usd = [s for s in event["summary"] if s["currency"] == "USD"][0]
        self.assertEqual(usd["count"], 2)
        # 后续录入新汇率不影响已冻结账单与批次核对
        restarted.upsert_rate(self.fin, {"currency": "USD", "rate_date": "2026-12-01", "rate": "7.50", "source": "test"})
        recon_after = restarted.reconcile_batch(self.fin, batch["id"])
        self.assertTrue(recon_after["consistent"])
        self.assertEqual(restarted.batches_for_record(self.fin, first["id"])[0]["reference"], "B-USD-2026-09")

    def test_batch_rejects_wrong_currency(self):
        self._rate("USD", "2026-03-01", "7.10")
        self._rate("USD", "2026-09-20", "7.25")
        record = self._settle(self._submitted("RI-FX-C", loss=300000.0), 300000.0, "PAY-C")
        batch = self.service.create_batch(self.fin, {"reference": "B-EUR", "currency": "EUR"})
        with self.assertRaises(Conflict):
            self.service.add_batch_item(self.fin, batch["id"], record["id"])

    def test_jpy_uses_zero_decimals(self):
        self._rate("JPY", "2026-03-01", "0.048")
        record = self._submitted("RI-FX-J", ccy="JPY", loss=100000000, loss_date="2026-03-02")
        self.assertEqual(record["payload"]["loss_amount_foreign"], 100000000)
        record = self.service.act(self.clm, record["id"], record["version"], "calculate", {"approved_loss": 100000000})
        # 折人民币 4,800,000 - 1,000,000 = 3,800,000；原币金额按日元取整
        self.assertEqual(record["payload"]["recoverable_amount_cny"], 3800000.0)
        self.assertEqual(record["payload"]["recoverable_amount_foreign"], float(round(3800000 / 0.048)))


if __name__ == "__main__":
    unittest.main()
