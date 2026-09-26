"""汇率换算规则：取数、换算、核定占用与结算冻结。

约定：
- 汇率为 1 单位外币折合人民币(CNY)的数量；
- 核定按报损日汇率占用合约额度（人民币口径）；
- 结算按结算日汇率换算应付人民币，并把当天汇率与账单一并冻结；
- 本模块只做纯计算，不访问存储。
"""
from datetime import datetime, timezone
from typing import Dict, Optional

from .currency import BASE_CURRENCY, FxQuote, is_base, precision, require_currency
from .domain import ValidationError


def round_money(amount: float, currency: str) -> float:
    return round(float(amount), precision(currency))


def base_quote() -> FxQuote:
    return FxQuote(currency=BASE_CURRENCY, rate_date="", rate=1.0, source="base")


def parse_date(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s不能为空" % field)
    text = value.strip()
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValidationError("%s必须是YYYY-MM-DD格式" % field) from exc
    return text


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def resolve_quote(currency: str, rate_date: str, lookup) -> FxQuote:
    """按币种与日期取汇率；人民币恒为1，外币缺汇率时报错并指明币种与日期。"""
    currency = require_currency(currency)
    if is_base(currency):
        return base_quote()
    row = lookup(currency, rate_date)
    if row is None:
        raise ValidationError("缺少%s在%s的汇率，请先登记" % (currency, rate_date))
    return FxQuote(currency=currency, rate_date=rate_date, rate=float(row["rate"]), source=str(row.get("source", "")))


def to_cny(amount_fc: float, quote: FxQuote) -> float:
    return round_money(float(amount_fc) * float(quote.rate), BASE_CURRENCY)


def from_cny(amount_cny: float, quote: FxQuote) -> float:
    if float(quote.rate) <= 0:
        raise ValidationError("汇率必须为正数")
    return round_money(float(amount_cny) / float(quote.rate), quote.currency)


def layer_recovery(loss_fc: float, attachment: float, width: float, cession_pct: float, currency: str) -> float:
    """按合约层规则计算原币摊回：min(max(0, 损失-起赔点), 层宽) × 分保比例。"""
    excess = max(0.0, float(loss_fc) - float(attachment))
    return round_money(min(excess, float(width)) * float(cession_pct), currency)


def quote_payload(quote: FxQuote) -> Dict[str, object]:
    return quote.as_dict()


def assessment(approved_loss_fc: float, attachment: float, width: float, cession_pct: float,
               currency: str, quote: FxQuote) -> Dict[str, object]:
    """核定结果：原币摊回 + 按报损日汇率冻结的额度占用（人民币）。"""
    recovery_fc = layer_recovery(approved_loss_fc, attachment, width, cession_pct, currency)
    return {
        "recoverable_fc": recovery_fc,
        "occupancy_cny": to_cny(recovery_fc, quote),
        "approved_loss_cny": to_cny(approved_loss_fc, quote),
        "quote": quote,
    }


def settlement(recoverable_fc: float, quote: FxQuote) -> Dict[str, object]:
    """结算结果：按结算日汇率换算应付人民币，随账单冻结。"""
    return {
        "payable_fc": round_money(recoverable_fc, quote.currency),
        "payable_cny": to_cny(recoverable_fc, quote),
        "quote": quote,
    }


def shortfall_detail(amount_fc: float, quote: Optional[FxQuote], amount_cny: Optional[float] = None) -> Dict[str, object]:
    """额度缺口展示：原币金额与折人民币金额；人民币金额已知时直接采用，避免回算误差。"""
    detail: Dict[str, object] = {"amount_fc": round_money(amount_fc, quote.currency if quote else BASE_CURRENCY)}
    if quote is not None:
        detail["amount_cny"] = round_money(amount_cny, BASE_CURRENCY) if amount_cny is not None else to_cny(amount_fc, quote)
        detail["currency"] = quote.currency
        detail["rate"] = quote.rate
        detail["rate_date"] = quote.rate_date
    return detail
