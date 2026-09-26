"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from . import fx
from .audit import AuditRecorder
from .currency import BASE_CURRENCY, FxQuote, is_base, require_currency
from .domain import Actor, CapacityShortfall, PermissionDenied, ValidationError, optional_text, text
from .repository import Repository
from .rules import DomainRules


# 核定时已占用合约额度的状态：额度占用在核定（calculate）时冻结
OCCUPANCY_STATES = {"calculated", "settled"}
FX_ROLES = {"finance", "admin"}
BATCH_ROLES = {"finance", "admin"}


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

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        bill = self.repository.get_bill_by_record(record_id)
        if bill is not None:
            record = dict(record)
            record["bill"] = bill
        return record

    # ---- 汇率上下文 ----

    def _fx_context(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """核定取报损日汇率，结算取结算日汇率；人民币无需登记汇率。"""
        currency = record["payload"].get("loss_currency", BASE_CURRENCY)
        if is_base(currency):
            return {"quote": fx.base_quote(), "settle_date": fx.today_utc()}
        if action == "calculate":
            rate_date = record["payload"].get("loss_report_date")
            if not rate_date:
                raise ValidationError("赔案缺少报损日期，无法取报损日汇率")
        else:
            rate_date = fx.parse_date(data.get("settle_date") or fx.today_utc(), "settle_date")
        quote = fx.resolve_quote(currency, rate_date, self.repository.get_fx_rate)
        return {"quote": quote, "settle_date": rate_date}

    def _check_capacity(self, record: Dict[str, Any], payload: Dict[str, Any], quote: FxQuote) -> None:
        """核定时校验合约额度：不足则报出原币金额、折人民币金额与缺口。"""
        event_id = payload.get("event_id")
        capacity_cny = float(payload["layer_width"]) * float(payload["cession_pct"])
        used_cny = float(payload.get("aggregate_prior", 0))
        for item in self.repository.list_records(limit=500):
            if item["id"] == record["id"] or item["payload"].get("event_id") != event_id:
                continue
            if item["state"] in OCCUPANCY_STATES:
                used_cny += float(item["payload"].get("recoverable_amount", 0))
        required_cny = float(payload["recoverable_amount"])
        available_cny = round(capacity_cny - used_cny, 2)
        if required_cny > available_cny + 0.01:
            shortfall_cny = round(required_cny - available_cny, 2)
            details = {
                "event_id": event_id,
                "currency": payload.get("loss_currency", BASE_CURRENCY),
                "capacity_cny": round(capacity_cny, 2),
                "used_cny": round(used_cny, 2),
                "available_cny": available_cny,
                "required": fx.shortfall_detail(float(payload.get("recoverable_fc", required_cny)), quote, required_cny),
                "required_cny": required_cny,
                "shortfall": fx.shortfall_detail(fx.from_cny(shortfall_cny, quote), quote, shortfall_cny),
                "shortfall_cny": shortfall_cny,
            }
            raise CapacityShortfall("合约额度不足：缺口人民币%s元" % shortfall_cny, details)

    def _bill_seed(self, record_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "claim_number": payload.get("claim_number", ""),
            "currency": payload.get("loss_currency", BASE_CURRENCY),
            "amount_fc": float(payload.get("payable_fc", 0)),
            "rate": float(payload.get("settle_rate", {}).get("rate", 1.0)),
            "rate_date": payload.get("settle_rate", {}).get("date", ""),
            "amount_cny": float(payload.get("payable_cny", 0)),
            "occupancy_cny": float(payload.get("occupancy_cny", 0)),
            "fx_variance_cny": float(payload.get("fx_variance_cny", 0)),
            "payment_reference": payload.get("payment_reference", ""),
            "snapshot": {
                "record_id": record_id,
                "event_id": payload.get("event_id"),
                "claim_number": payload.get("claim_number"),
                "currency": payload.get("loss_currency", BASE_CURRENCY),
                "approved_loss_fc": payload.get("approved_loss_fc"),
                "recoverable_fc": payload.get("recoverable_fc"),
                "report_rate": payload.get("report_rate"),
                "occupancy_cny": payload.get("occupancy_cny"),
                "settle_rate": payload.get("settle_rate"),
                "payable_fc": payload.get("payable_fc"),
                "payable_cny": payload.get("payable_cny"),
                "fx_variance_cny": payload.get("fx_variance_cny"),
                "payment_reference": payload.get("payment_reference"),
            },
        }

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        data = data or {}
        fx_ctx = self._fx_context(record, action, data) if action in {"calculate", "settle"} else None
        new_state, new_payload, summary = self.rules.apply_action(record, action, data, fx_ctx)
        bill_seed = None
        if action == "calculate":
            self._check_capacity(record, new_payload, (fx_ctx or {}).get("quote") or fx.base_quote())
        elif action == "settle":
            bill_seed = self._bill_seed(record_id, new_payload)
        updated = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data, "from": record["state"], "to": new_state},
            bill_seed=bill_seed,
        )
        if bill_seed is not None:
            updated = dict(updated)
            updated["bill"] = self.repository.get_bill_by_record(record_id)
        return updated

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 汇率管理 ----

    def upsert_fx_rate(self, actor: Actor, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role not in FX_ROLES:
            raise PermissionDenied("角色无权维护汇率")
        currency = require_currency(str((data or {}).get("currency", "")))
        if is_base(currency):
            raise ValidationError("人民币为基准币种，无需登记汇率")
        rate_date = fx.parse_date((data or {}).get("date"), "date")
        rate_value = (data or {}).get("rate")
        if isinstance(rate_value, bool) or not isinstance(rate_value, (int, float)) or float(rate_value) <= 0:
            raise ValidationError("rate必须是正数")
        source = optional_text(data or {}, "source", "")
        return self.repository.upsert_fx_rate(currency, rate_date, float(rate_value), source, actor.user_id)

    def list_fx_rates(self, actor: Actor, currency: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if currency:
            currency = require_currency(currency)
        return self.repository.list_fx_rates(currency=currency, limit=limit)

    # ---- 结算批次 ----

    def create_batch(self, actor: Actor, reference: str, title: str = "") -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role not in BATCH_ROLES:
            raise PermissionDenied("角色无权创建批次")
        reference = text({"reference": reference}, "reference")
        return self.repository.create_batch(reference, optional_text({"title": title}, "title", ""), actor.user_id)

    def list_batches(self, actor: Actor, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches(limit=limit)

    def add_batch_item(self, actor: Actor, batch_id: int, bill_id: int = None, record_id: int = None) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role not in BATCH_ROLES:
            raise PermissionDenied("角色无权维护批次")
        if bill_id is None:
            if record_id is None:
                raise ValidationError("bill_id或record_id至少提供一项")
            bill = self.repository.get_bill_by_record(int(record_id))
            if bill is None:
                raise ValidationError("该赔案尚未生成结算账单")
            bill_id = bill["id"]
        bill = self.repository.get_bill(int(bill_id))
        self.repository.add_batch_item(int(batch_id), int(bill_id))
        self.audit.note(bill["record_id"], actor.user_id, "batched", {"batch_id": int(batch_id), "bill_id": int(bill_id)})
        return self.get_batch(actor, int(batch_id))

    def get_batch(self, actor: Actor, batch_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        batch = self.repository.get_batch(int(batch_id))
        bills = self.repository.batch_items(int(batch_id))
        summary: Dict[str, Dict[str, Any]] = {}
        for bill in bills:
            currency = bill["currency"]
            bucket = summary.setdefault(currency, {"currency": currency, "count": 0, "amount_fc": 0.0, "amount_cny": 0.0, "fx_variance_cny": 0.0})
            bucket["count"] += 1
            bucket["amount_fc"] = round(bucket["amount_fc"] + float(bill["amount_fc"]), 6)
            bucket["amount_cny"] = round(bucket["amount_cny"] + float(bill["amount_cny"]), 2)
            bucket["fx_variance_cny"] = round(bucket["fx_variance_cny"] + float(bill["fx_variance_cny"]), 2)
        batch["bills"] = bills
        batch["summary_by_currency"] = sorted(summary.values(), key=lambda item: item["currency"])
        return batch
