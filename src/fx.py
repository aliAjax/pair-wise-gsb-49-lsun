"""币种资料、汇率快照与外币换算规则（纯函数，不依赖存储）。

汇率口径：rate 表示 1 单位外币兑换多少人民币（CNY per unit）。
报损、核定、结算各自冻结对应业务日期的汇率，已结算账单不再随新汇率变动。
"""
import datetime as _dt
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional

from .domain import DomainError, ValidationError

BASE_CCY = "CNY"
RATE_DATE_FMT = "%Y-%m-%d"
EPS = Decimal("0.01")

# 币种资料：ISO代码 -> 小数位/中文名
CURRENCIES: Dict[str, Dict[str, Any]] = {
    "CNY": {"digits": 2, "label": "人民币"},
    "USD": {"digits": 2, "label": "美元"},
    "EUR": {"digits": 2, "label": "欧元"},
    "GBP": {"digits": 2, "label": "英镑"},
    "JPY": {"digits": 0, "label": "日元"},
    "HKD": {"digits": 2, "label": "港币"},
    "SGD": {"digits": 2, "label": "新加坡元"},
    "AUD": {"digits": 2, "label": "澳元"},
    "CAD": {"digits": 2, "label": "加元"},
    "CHF": {"digits": 2, "label": "瑞士法郎"},
}


class FxRateMissing(DomainError):
    """业务日缺少该币种对人民币的汇率。"""
    status = 422
    code = "fx_rate_missing"

    def __init__(self, currency: str, rate_date: str, purpose: str = "") -> None:
        super().__init__(
            "缺少%s在%s的人民币汇率（%s）" % (currency, rate_date, purpose or "换算"),
            {"currency": currency, "rate_date": rate_date, "purpose": purpose},
        )


def today_rate_date() -> str:
    return _dt.datetime.now(_dt.timezone.utc).date().strftime(RATE_DATE_FMT)


def parse_rate_date(value: Any, key: str = "rate_date") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key)
    value = value.strip()
    try:
        parsed = _dt.datetime.strptime(value, RATE_DATE_FMT).date()
    except ValueError as exc:
        raise ValidationError("%s必须是YYYY-MM-DD日期" % key) from exc
    return parsed.strftime(RATE_DATE_FMT)


def known_currency(code: str) -> bool:
    return isinstance(code, str) and code.strip().upper() in CURRENCIES


def ensure_currency(code: Any, key: str = "currency") -> str:
    if not isinstance(code, str) or not code.strip():
        raise ValidationError("%s不能为空" % key)
    code = code.strip().upper()
    if code not in CURRENCIES:
        raise ValidationError("不支持的币种%s" % code)
    return code


def decimal(value: Any, key: str = "value") -> Decimal:
    if isinstance(value, bool):
        raise ValidationError("%s必须是数字" % key)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float, str)):
        try:
            return Decimal(str(value))
        except Exception as exc:
            raise ValidationError("%s必须是数字" % key) from exc
    raise ValidationError("%s必须是数字" % key)


def quantize_money(amount: Decimal, currency: str = BASE_CCY) -> Decimal:
    digits = int(CURRENCIES[currency]["digits"])
    quantum = Decimal(1).scaleb(-digits)
    return amount.quantize(quantum, rounding=ROUND_HALF_UP)


def money_float(amount: Decimal, currency: str = BASE_CCY) -> float:
    return float(quantize_money(amount, currency))


@dataclass(frozen=True)
class FxContext:
    """一次业务动作使用的汇率快照（币种、业务日期、CNY/外币汇率）。"""
    currency: str
    rate_date: str
    rate: Decimal
    purpose: str = ""

    def to_cny(self, foreign_amount: Decimal) -> Decimal:
        return decimal(foreign_amount) * self.rate

    def to_foreign(self, cny_amount: Decimal) -> Decimal:
        return decimal(cny_amount) / self.rate

    def as_snapshot(self) -> Dict[str, Any]:
        return {
            "currency": self.currency,
            "rate_date": self.rate_date,
            "rate": str(self.rate),
            "purpose": self.purpose,
        }


def base_context(purpose: str = "", rate_date: Optional[str] = None) -> FxContext:
    return FxContext(BASE_CCY, rate_date or today_rate_date(), Decimal("1"), purpose)


def build_context(currency: str, rate_date: str, rate: Any, purpose: str) -> FxContext:
    currency = ensure_currency(currency)
    date = parse_rate_date(rate_date, "rate_date")
    rate = decimal(rate, "rate")
    if rate <= 0:
        raise ValidationError("汇率必须大于0")
    if currency == BASE_CCY:
        rate = Decimal("1")
    return FxContext(currency=currency, rate_date=date, rate=rate, purpose=purpose)


def context_from_snapshot(snapshot: Dict[str, Any], purpose: str = "") -> FxContext:
    return build_context(
        snapshot["currency"], snapshot["rate_date"], snapshot["rate"], purpose or snapshot.get("purpose", "")
    )


def summarize_by_currency(lines: List[Dict[str, Any]], foreign_key: str, cny_key: str) -> List[Dict[str, Any]]:
    """按币种汇总：原币金额合计与折人民币合计并列展示。"""
    totals: Dict[str, Dict[str, Decimal]] = {}
    for line in lines:
        ccy = str(line.get("currency") or BASE_CCY)
        bucket = totals.setdefault(ccy, {"foreign": Decimal("0"), "cny": Decimal("0")})
        bucket["foreign"] += decimal(line.get(foreign_key, 0), foreign_key)
        bucket["cny"] += decimal(line.get(cny_key, 0), cny_key)
    result = []
    for ccy, bucket in sorted(totals.items()):
        result.append({
            "currency": ccy,
            "currency_label": CURRENCIES.get(ccy, {}).get("label", ccy),
            "foreign_total": money_float(bucket["foreign"], ccy),
            "cny_total": money_float(bucket["cny"]),
            "count": sum(1 for line in lines if str(line.get("currency") or BASE_CCY) == ccy),
        })
    return result
