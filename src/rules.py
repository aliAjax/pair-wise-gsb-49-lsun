"""再保险合约与巨灾暴露管理领域规则与状态转换。

外币口径：合约额度/起赔点以人民币计价；海外赔案用原币报损。
- submit_claim：记录原币报损金额与报损日（loss_date）。
- calculate（核定）：按报损日汇率折算，占用合约额度（recoverable_amount_cny 为占用口径）。
- settle（结算）：按结算日汇率重新换算，冻结当天汇率与账单；账单日后不随新汇率变动。
"""
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, text, text_list
from .fx import (
    BASE_CCY,
    CURRENCIES,
    FxContext,
    base_context,
    ensure_currency,
    money_float,
    parse_rate_date,
    quantize_money,
    today_rate_date,
)

CURRENCY_DIGITS = {code: info["digits"] for code, info in CURRENCIES.items()}


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
        contract_ccy = p.get("contract_currency", BASE_CCY)
        if contract_ccy is not None and not isinstance(contract_ccy, str):
            raise ValidationError("contract_currency必须是币种代码")
        contract_ccy = (contract_ccy or BASE_CCY).strip().upper()
        if contract_ccy != BASE_CCY:
            raise ValidationError("合约额度仅支持人民币计价")
        p["contract_currency"] = BASE_CCY
        if limit <= attachment:
            raise ValidationError("赔款限额必须高于起赔点")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        width = float(p["limit"]) - float(p["attachment"])
        retained_loss = max(0.0, float(p["loss_amount"]) - float(p["attachment"]))
        recovery = min(retained_loss, width) * float(p["cession_pct"])
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

    @staticmethod
    def _context(record_payload: Dict[str, Any], fx: Optional[FxContext], currency: str, purpose: str) -> FxContext:
        if fx is None or fx.currency == BASE_CCY:
            if currency != BASE_CCY:
                raise ValidationError("外币赔案需要%s的业务日汇率" % purpose)
            return base_context(purpose)
        if fx.currency != currency:
            raise ValidationError("汇率币种与赔案币种不一致")
        return FxContext(fx.currency, fx.rate_date, fx.rate, purpose)

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any], fx: Optional[FxContext] = None) -> Tuple[str, Dict[str, Any], str, Dict[str, Any]]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        fx_notes: Dict[str, Any] = {}
        summary = ""
        if action == "bind":
            changes["bound_by"] = text(data, "underwriter_id")
            summary = "再保合约已绑定"
        elif action == "submit_claim":
            changes["claim_number"] = text(data, "claim_number")
            changes["claim_event_id"] = text(data, "event_id")
            ccy = ensure_currency(data.get("loss_currency", BASE_CCY), "loss_currency")
            changes["loss_currency"] = ccy
            raw_loss = data.get("loss_amount", p.get("loss_amount"))
            loss = number({"loss_amount": raw_loss}, "loss_amount", 0)
            changes["loss_amount_foreign"] = round(loss, CURRENCY_DIGITS[ccy])
            if ccy == BASE_CCY:
                changes["loss_date"] = parse_rate_date(data["loss_date"], "loss_date") if data.get("loss_date") else today_rate_date()
            else:
                changes["loss_date"] = parse_rate_date(data.get("loss_date"), "loss_date")
                claim_fx = self._context(p, fx, ccy, "报损")
                changes["loss_fx"] = claim_fx.as_snapshot()
                changes["loss_amount_cny"] = money_float(claim_fx.to_cny(Decimal(str(loss))))
                fx_notes["loss_fx"] = claim_fx.as_snapshot()
            summary = "赔案已提交（原币%s报损）" % ccy
        elif action == "calculate":
            ccy = p.get("loss_currency", BASE_CCY)
            approved_foreign = number(data, "approved_loss", 0)
            changes["approved_loss_foreign"] = round(approved_foreign, CURRENCY_DIGITS[ccy])
            changes["approved_loss_currency"] = ccy
            if ccy == BASE_CCY:
                ctx = base_context("核定占用", p.get("loss_date"))
                approved_cny = Decimal(str(approved_foreign))
            else:
                ctx = self._context(p, fx, ccy, "核定占用")
                approved_cny = ctx.to_cny(Decimal(str(approved_foreign)))
                changes["occupancy_fx"] = ctx.as_snapshot()
                fx_notes["occupancy_fx"] = ctx.as_snapshot()
            attachment = Decimal(str(p["attachment"]))
            width = Decimal(str(p["layer_width"]))
            cession = Decimal(str(p["cession_pct"]))
            retained_cny = max(Decimal("0"), approved_cny - attachment)
            recovery_cny = min(retained_cny, width) * cession
            recovery_cny = quantize_money(recovery_cny)
            recovery_foreign = quantize_money(ctx.to_foreign(recovery_cny), ccy)
            reinstatement = quantize_money(recovery_cny * Decimal(str(p["reinstatement_pct"])))
            net = quantize_money(approved_cny - recovery_cny)
            changes["approved_loss"] = money_float(approved_cny)
            changes["approved_loss_cny"] = money_float(approved_cny)
            changes["recoverable_amount"] = float(recovery_cny)
            changes["recoverable_amount_cny"] = float(recovery_cny)
            changes["recoverable_amount_foreign"] = float(recovery_foreign)
            changes["reinstatement_premium"] = float(reinstatement)
            changes["net_retention"] = float(net)
            summary = "摊回金额已核定，按%s汇率%s占用人民币额度" % (ctx.rate_date, ctx.rate)
        elif action == "settle":
            ccy = p.get("loss_currency", BASE_CCY)
            occupied_cny = Decimal(str(p.get("recoverable_amount_cny", p.get("recoverable_amount", 0))))
            if occupied_cny <= 0:
                raise ValidationError("无可结算摊回")
            changes["payment_reference"] = text(data, "payment_reference")
            settle_date = parse_rate_date(data["settle_date"], "settle_date") if data.get("settle_date") else today_rate_date()
            if ccy == BASE_CCY:
                sctx = base_context("结算付款", settle_date)
            else:
                sctx = self._context(p, fx, ccy, "结算付款")
            # 原币摊回在核定时已冻结，按结算日汇率重新折人民币形成最终付款
            payment_foreign = quantize_money(Decimal(str(p.get("recoverable_amount_foreign", occupied_cny))), ccy)
            payment_cny = quantize_money(sctx.to_cny(payment_foreign))
            fx_delta = quantize_money(payment_cny - occupied_cny)
            bill = {
                "payment_reference": changes["payment_reference"],
                "currency": ccy,
                "settle_date": settle_date,
                "settlement_fx": sctx.as_snapshot(),
                "payment_amount_foreign": float(payment_foreign),
                "payment_amount_cny": float(payment_cny),
                "occupied_amount_cny": float(occupied_cny),
                "occupancy_fx": p.get("occupancy_fx"),
                "fx_delta_cny": float(fx_delta),
                "reinstatement_premium": p.get("reinstatement_premium"),
                "frozen": True,
            }
            changes["settle_date"] = settle_date
            changes["settlement_fx"] = sctx.as_snapshot()
            changes["payment_amount_foreign"] = float(payment_foreign)
            changes["payment_amount_cny"] = float(payment_cny)
            changes["fx_delta_cny"] = float(fx_delta)
            changes["bill"] = bill
            fx_notes["settlement_fx"] = sctx.as_snapshot()
            summary = "摊回赔款已结算，账单已按%s汇率冻结" % settle_date
        elif action == "reject":
            changes["reject_reason"] = text(data, "reject_reason")
            summary = "赔案已拒绝"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action), fx_notes
