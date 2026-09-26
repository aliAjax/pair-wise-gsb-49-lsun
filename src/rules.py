"""再保险合约与巨灾暴露管理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Optional, Tuple

from . import fx
from .currency import BASE_CURRENCY, FxQuote, is_base, require_currency
from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "quoted"
CREATE_ROLES = {'underwriter'}
ACTION_ROLES = {'bind': {'underwriter'}, 'submit_claim': {'claims_officer'}, 'calculate': {'claims_officer'}, 'settle': {'finance'}, 'reject': {'finance', 'claims_officer'}}
TRANSITIONS = {'bind': {'quoted': 'bound'}, 'submit_claim': {'bound': 'claim_submitted'}, 'calculate': {'claim_submitted': 'calculated'}, 'settle': {'calculated': 'settled'}, 'reject': {'claim_submitted': 'rejected', 'calculated': 'rejected'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "event_id")
        attachment = number(p, "attachment", 0)
        limit = number(p, "limit", 0)
        number(p, "cession_pct", 0, 1)
        number(p, "loss_amount", 0)
        number(p, "reinstatement_pct", 0, 1)
        number(p, "aggregate_prior", 0)
        if limit <= attachment:
            raise ValidationError("赔款限额必须高于起赔点")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        width = float(p["limit"]) - float(p["attachment"])
        retained_loss = max(0.0, float(p["loss_amount"]) - float(p["attachment"]))
        recovery = min(retained_loss, width) * float(p["cession_pct"])
        p["loss_currency"] = BASE_CURRENCY
        p["layer_width"] = round(width, 2)
        p["recoverable_amount"] = round(recovery, 2)
        p["reinstatement_premium"] = round(recovery * float(p["reinstatement_pct"]), 2)
        p["net_retention"] = round(float(p["loss_amount"]) - recovery, 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        event_id = payload.get("event_id")
        used = float(payload.get("aggregate_prior", 0))
        for item in existing:
            if item["state"] in {"rejected"} or item["payload"].get("event_id") != event_id:
                continue
            used += float(item["payload"].get("recoverable_amount", 0))
        capacity = float(payload["layer_width"]) * float(payload["cession_pct"])
        projected = min(max(0.0, float(payload["loss_amount"]) - float(payload["attachment"])), float(payload["layer_width"])) * float(payload["cession_pct"])
        if used + projected > capacity + 0.01:
            raise Conflict("同一事件累计摊回超过再保容量")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any],
                     fx_ctx: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "bind":
            changes["bound_by"] = text(data, "underwriter_id")
            summary = "再保合约已绑定"
        elif action == "submit_claim":
            changes["claim_number"] = text(data, "claim_number")
            changes["claim_event_id"] = text(data, "event_id")
            currency = require_currency(optional_text(data, "currency", p.get("loss_currency", BASE_CURRENCY)) or BASE_CURRENCY)
            changes["loss_currency"] = currency
            if data.get("reported_loss") is not None:
                changes["reported_loss_fc"] = number(data, "reported_loss", 0)
            if data.get("loss_report_date") is not None and str(data.get("loss_report_date")).strip():
                changes["loss_report_date"] = fx.parse_date(data.get("loss_report_date"), "loss_report_date")
            if not is_base(currency) and not changes.get("loss_report_date"):
                raise ValidationError("外币报损必须提供loss_report_date")
            summary = "赔案已提交"
        elif action == "calculate":
            loss_fc = number(data, "approved_loss", 0)
            currency = p.get("loss_currency", BASE_CURRENCY)
            quote = (fx_ctx or {}).get("quote") or fx.base_quote()
            result = fx.assessment(loss_fc, float(p["attachment"]), float(p["layer_width"]), float(p["cession_pct"]), currency, quote)
            changes["approved_loss_fc"] = loss_fc
            changes["approved_loss"] = result["approved_loss_cny"]
            changes["recoverable_fc"] = result["recoverable_fc"]
            changes["recoverable_amount"] = result["occupancy_cny"]
            changes["reinstatement_premium"] = round(result["occupancy_cny"] * float(p["reinstatement_pct"]), 2)
            changes["report_rate"] = fx.quote_payload(result["quote"])
            summary = "摊回金额已计算"
        elif action == "settle":
            if float(p["recoverable_amount"]) <= 0:
                raise ValidationError("无可结算摊回")
            changes["payment_reference"] = text(data, "payment_reference")
            currency = p.get("loss_currency", BASE_CURRENCY)
            quote = (fx_ctx or {}).get("quote") or fx.base_quote()
            settle_date = (fx_ctx or {}).get("settle_date") or fx.today_utc()
            result = fx.settlement(float(p.get("recoverable_fc", p["recoverable_amount"])), quote)
            changes["settle_date"] = settle_date
            changes["settle_rate"] = fx.quote_payload(result["quote"])
            changes["payable_fc"] = result["payable_fc"]
            changes["payable_cny"] = result["payable_cny"]
            changes["occupancy_cny"] = float(p["recoverable_amount"])
            changes["fx_variance_cny"] = round(result["payable_cny"] - float(p["recoverable_amount"]), 2)
            summary = "摊回赔款已结算"
        elif action == "reject":
            changes["reject_reason"] = text(data, "reject_reason")
            summary = "赔案已拒绝"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
