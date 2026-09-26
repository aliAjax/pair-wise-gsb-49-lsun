"""业务用例编排、权限检查、汇率接入、额度缺口与批次核对。"""
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, ValidationError, text
from .fx import (
    BASE_CCY,
    FxContext,
    FxRateMissing,
    base_context,
    build_context,
    decimal,
    ensure_currency,
    parse_rate_date,
    quantize_money,
    summarize_by_currency,
    today_rate_date,
)
from .repository import Repository
from .rules import DomainRules

FX_ROLES = {"finance", "admin"}
BATCH_ROLES = {"finance", "admin"}
GAP_TOLERANCE = Decimal("0.01")


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    # ---- 汇率资料 ----
    def _fx_context(self, currency: str, rate_date: str, purpose: str) -> FxContext:
        currency = ensure_currency(currency)
        if currency == BASE_CCY:
            return base_context(purpose, rate_date)
        row = self.repository.get_fx_rate(currency, rate_date)
        if row is None:
            raise FxRateMissing(currency, rate_date, purpose)
        return build_context(currency, row["rate_date"], row["rate"], purpose)

    def upsert_rate(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        if actor.role not in FX_ROLES:
            raise PermissionDenied("角色无权维护汇率")
        currency = ensure_currency(data.get("currency"), "currency")
        if currency == BASE_CCY:
            raise ValidationError("人民币为基准币种，无需维护汇率")
        rate_date = parse_rate_date(data.get("rate_date"), "rate_date")
        rate = decimal(data.get("rate"), "rate")
        if rate <= 0:
            raise ValidationError("汇率必须大于0")
        source = str(data.get("source") or "").strip()
        row = self.repository.upsert_fx_rate(currency, rate_date, str(rate), source, actor.user_id)
        row["rate"] = float(Decimal(row["rate"]))
        return row

    def get_rate(self, actor: Actor, currency: str, rate_date: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        currency = ensure_currency(currency)
        row = self.repository.get_fx_rate(currency, rate_date)
        if row is None:
            raise FxRateMissing(currency, rate_date, "查询")
        row["rate"] = float(Decimal(row["rate"]))
        return row

    def list_rates(self, actor: Actor, currency: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        rows = self.repository.list_fx_rates(currency=currency, limit=limit)
        for row in rows:
            row["rate"] = float(Decimal(row["rate"]))
        return rows

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100, event_id: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit, event_id=event_id)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def _fx_for_action(self, action: str, record: Dict[str, Any], data: Dict[str, Any]) -> Optional[FxContext]:
        p = record["payload"]
        ccy = p.get("loss_currency", BASE_CCY) if action != "submit_claim" else ensure_currency(
            data.get("loss_currency", BASE_CCY), "loss_currency"
        )
        if ccy == BASE_CCY:
            return None
        if action == "submit_claim":
            rate_date = parse_rate_date(data.get("loss_date"), "loss_date")
            return self._fx_context(ccy, rate_date, "报损")
        if action == "calculate":
            rate_date = parse_rate_date(p.get("loss_date"), "loss_date")
            return self._fx_context(ccy, rate_date, "核定占用")
        if action == "settle":
            rate_date = parse_rate_date(data["settle_date"], "settle_date") if data.get("settle_date") else today_rate_date()
            return self._fx_context(ccy, rate_date, "结算付款")
        return None

    def _check_event_gap(self, current_id: int, event_id: str, stage: str, claim_cny: Decimal) -> None:
        """同一巨灾事件下，按人民币口径累计摊回与合约容量对比；不足时列出缺口。"""
        current = self.repository.get(current_id)
        cp = current["payload"]
        capacity = Decimal(str(cp.get("layer_width", 0))) * Decimal(str(cp.get("cession_pct", 0)))
        used = Decimal(str(cp.get("aggregate_prior", 0)))
        records = self.repository.list_records(limit=500, event_id=event_id)
        lines: List[Dict[str, Any]] = []
        for item in records:
            if item["id"] == current_id or item["state"] == "rejected":
                continue
            p = item["payload"]
            raw = p.get("payment_amount_cny", p.get("recoverable_amount_cny", p.get("recoverable_amount", 0)))
            used_d = quantize_money(decimal(raw))
            used += used_d
            lines.append({
                "record_id": item["id"],
                "reference": item["reference"],
                "currency": p.get("loss_currency", BASE_CCY),
                "foreign_amount": p.get("payment_amount_foreign", p.get("recoverable_amount_foreign", p.get("recoverable_amount", 0))),
                "cny_amount": float(used_d),
                "state": item["state"],
            })
        projected = quantize_money(claim_cny)
        available = quantize_money(capacity - used)
        gap = quantize_money(used + projected - capacity)
        lines.append({
            "record_id": current_id,
            "reference": current["reference"],
            "currency": cp.get("loss_currency", BASE_CCY),
            "foreign_amount": cp.get("approved_loss_foreign", cp.get("loss_amount_foreign", 0)),
            "cny_amount": float(projected),
            "state": current["state"],
            "current": True,
        })
        if gap > GAP_TOLERANCE:
            raise Conflict(
                "人民币额度不足：本币折人民币%s，缺口%s" % (projected, gap),
                {
                    "reason": "capacity_shortfall",
                    "stage": stage,
                    "event_id": event_id,
                    "currency": cp.get("loss_currency", BASE_CCY),
                    "foreign_amount": cp.get("approved_loss_foreign", cp.get("loss_amount_foreign", 0)),
                    "claimed_cny": float(projected),
                    "capacity_cny": float(quantize_money(capacity)),
                    "used_cny": float(quantize_money(used)),
                    "available_cny": float(available),
                    "gap_cny": float(gap),
                    "lines": lines,
                },
            )

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        fx = self._fx_for_action(action, record, data or {})
        new_state, new_payload, summary, fx_notes = self.rules.apply_action(record, action, data or {}, fx)
        if action in {"calculate", "settle"}:
            p_now = new_payload
            event_id = p_now.get("claim_event_id") or p_now.get("event_id")
            stage_cny = p_now.get("payment_amount_cny") if action == "settle" else p_now.get("recoverable_amount_cny")
            if event_id and stage_cny is not None:
                self._check_event_gap(record_id, event_id, "occupancy" if action == "calculate" else "settlement", Decimal(str(stage_cny)))
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state, "fx": fx_notes},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 结算批次 ----
    def create_batch(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        if actor.role not in BATCH_ROLES:
            raise PermissionDenied("角色无权创建批次")
        reference = text(data or {}, "reference")
        currency = ensure_currency(data.get("currency"), "currency")
        note = str((data or {}).get("note") or "").strip()
        return self.repository.create_batch(reference, currency, note, actor.user_id)

    def get_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_batch(batch_id)
        batch["items"] = self.repository.list_batch_items(batch_id)
        batch["summary"] = summarize_by_currency(batch["items"], "foreign_amount", "cny_amount")
        return batch

    def list_batches(self, actor: Actor, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batches = self.repository.list_batches(limit=limit)
        for batch in batches:
            items = self.repository.list_batch_items(batch["id"])
            batch["item_count"] = len(items)
            batch["summary"] = summarize_by_currency(items, "foreign_amount", "cny_amount")
        return batches

    def add_batch_item(self, actor: Actor, batch_id: int, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        if actor.role not in BATCH_ROLES:
            raise PermissionDenied("角色无权维护批次")
        return self.repository.add_batch_item(batch_id, record_id)

    def batches_for_record(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.batches_for_record(record_id)

    # ---- 核对 ----
    def reconcile_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        """冻结账单快照与案件当前账单逐项比对；已结算账单不应被新汇率改动。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_batch(batch_id)
        items = self.repository.list_batch_items(batch_id)
        keys = ("payment_amount_foreign", "payment_amount_cny", "occupied_amount_cny", "fx_delta_cny")
        details = []
        consistent = True
        for item in items:
            record = self.repository.get(item["record_id"])
            bill = record["payload"].get("bill") or {}
            diffs = {}
            for key in keys:
                snap = item["bill_snapshot"].get(key)
                live = bill.get(key)
                if snap != live:
                    diffs[key] = {"snapshot": snap, "current": live}
            snap_fx = item["bill_snapshot"].get("settlement_fx") or {}
            live_fx = bill.get("settlement_fx") or {}
            if snap_fx.get("rate") != live_fx.get("rate") or snap_fx.get("rate_date") != live_fx.get("rate_date"):
                diffs["settlement_fx"] = {"snapshot": snap_fx, "current": live_fx}
            ok = not diffs
            consistent = consistent and ok
            details.append({
                "record_id": item["record_id"],
                "reference": item.get("record_reference"),
                "currency": item["currency"],
                "foreign_amount": item["foreign_amount"],
                "cny_amount": item["cny_amount"],
                "rate_date": item["rate_date"],
                "rate": item["rate"],
                "consistent": ok,
                "diffs": diffs,
            })
        return {
            "batch_id": batch_id,
            "batch_reference": batch["reference"],
            "currency": batch["currency"],
            "consistent": consistent,
            "items": details,
            "summary": summarize_by_currency(details, "foreign_amount", "cny_amount"),
        }

    def reconcile_event(self, actor: Actor, event_id: str) -> Dict[str, Any]:
        """按巨灾事件核对每个案件的报损/占用/结算口径，按币种汇总。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        event_id = text({"event_id": event_id}, "event_id")
        records = self.repository.list_records(event_id=event_id, limit=500)
        lines = []
        for record in reversed(records):
            if record["state"] == "rejected":
                continue
            p = record["payload"]
            ccy = p.get("loss_currency", BASE_CCY)
            occupied = p.get("recoverable_amount_cny") if p.get("recoverable_amount_cny") is not None else (
                p.get("recoverable_amount") if record["state"] in {"calculated", "settled"} else None
            )
            payment_cny = p.get("payment_amount_cny")
            line = {
                "record_id": record["id"],
                "reference": record["reference"],
                "state": record["state"],
                "currency": ccy,
                "loss_date": p.get("loss_date"),
                "loss_fx": (p.get("loss_fx") or {}).get("rate"),
                "loss_foreign": p.get("loss_amount_foreign", p.get("loss_amount")),
                "loss_cny": p.get("loss_amount_cny", p.get("loss_amount")),
                "approved_foreign": p.get("approved_loss_foreign"),
                "occupancy_fx": (p.get("occupancy_fx") or {}).get("rate"),
                "occupied_foreign": p.get("recoverable_amount_foreign"),
                "occupied_cny": occupied,
                "settle_date": p.get("settle_date"),
                "settlement_fx": (p.get("settlement_fx") or {}).get("rate"),
                "payment_foreign": p.get("payment_amount_foreign"),
                "payment_cny": payment_cny,
                "fx_delta_cny": p.get("fx_delta_cny"),
                "bill_frozen": bool((p.get("bill") or {}).get("frozen")),
                "payment_reference": p.get("payment_reference"),
            }
            lines.append(line)
        active = [line for line in lines if line["occupied_cny"] is not None]
        total_used = sum(Decimal(str(line["payment_cny"] if line["payment_cny"] is not None else line["occupied_cny"])) for line in active)
        capacity = Decimal("0")
        for record in records:
            p = record["payload"]
            if p.get("claim_event_id") == event_id or p.get("event_id") == event_id:
                capacity = Decimal(str(p.get("layer_width", 0))) * Decimal(str(p.get("cession_pct", 0)))
                total_used += Decimal(str(p.get("aggregate_prior", 0)))
                break
        gap = quantize_money(total_used - capacity)
        summary_lines = []
        for line in active:
            summary_lines.append({
                "currency": line["currency"],
                "foreign": line["payment_foreign"] if line["payment_foreign"] is not None else (line["occupied_foreign"] or 0),
                "cny": line["payment_cny"] if line["payment_cny"] is not None else line["occupied_cny"],
            })
        return {
            "event_id": event_id,
            "lines": lines,
            "summary": summarize_by_currency(summary_lines, "foreign", "cny"),
            "capacity_cny": float(quantize_money(capacity)),
            "used_cny": float(quantize_money(total_used)),
            "available_cny": float(quantize_money(max(Decimal("0"), capacity - total_used))),
            "gap_cny": float(gap) if gap > 0 else 0.0,
        }
