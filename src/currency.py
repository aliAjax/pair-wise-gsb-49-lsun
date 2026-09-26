"""币种资料：币种目录、精度与汇率/账单数据类型。

合约额度以人民币(CNY)计价，海外巨灾赔案以外币报损。
本模块只提供静态资料与数据结构，不包含换算规则（见 src/fx.py）。
"""
from dataclasses import dataclass
from typing import Dict

from .domain import ValidationError


BASE_CURRENCY = "CNY"

# 币种目录：精度（小数位）与中文名称
CURRENCIES: Dict[str, Dict[str, object]] = {
    "CNY": {"precision": 2, "name": "人民币"},
    "USD": {"precision": 2, "name": "美元"},
    "EUR": {"precision": 2, "name": "欧元"},
    "GBP": {"precision": 2, "name": "英镑"},
    "HKD": {"precision": 2, "name": "港币"},
    "SGD": {"precision": 2, "name": "新加坡元"},
    "AUD": {"precision": 2, "name": "澳元"},
    "JPY": {"precision": 0, "name": "日元"},
}


def is_base(currency: str) -> bool:
    return currency == BASE_CURRENCY


def precision(currency: str) -> int:
    return int(CURRENCIES[currency]["precision"])


def currency_name(currency: str) -> str:
    return str(CURRENCIES[currency]["name"])


def require_currency(code: str) -> str:
    if not isinstance(code, str) or not code.strip():
        raise ValidationError("currency不能为空")
    code = code.strip().upper()
    if code not in CURRENCIES:
        raise ValidationError("不支持的币种%s" % code)
    return code


@dataclass(frozen=True)
class FxQuote:
    """某币种在某一日对人民币的汇率快照：1单位外币折合rate单位人民币。"""

    currency: str
    rate_date: str
    rate: float
    source: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "currency": self.currency,
            "date": self.rate_date,
            "rate": self.rate,
            "source": self.source,
            "base": BASE_CURRENCY,
        }
