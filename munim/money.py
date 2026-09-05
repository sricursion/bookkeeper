"""Money is an integer number of minor units everywhere: paise for INR, the
unit Razorpay's API uses; cents for the euro amounts in the benchmark.

Rupees use Indian digit grouping (lakh, crore): 1234567 paise is ₹12,345.67
and 123456789 paise is ₹12,34,567.89. Other currencies group by thousands.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

MINOR_PER_MAJOR = 100

SYMBOLS = {"INR": "₹", "EUR": "€", "USD": "$", "GBP": "£"}


def rupees(paise: int) -> str:
    """Format an integer number of paise as rupees with Indian grouping."""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(int(paise)), MINOR_PER_MAJOR)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups) + "," + tail
    return f"{sign}₹{digits}.{frac:02d}"


def format_amount(minor: int, currency: str | None) -> str:
    """Format minor units in the given currency. INR gets Indian grouping."""
    code = (currency or "INR").upper()
    if code == "INR":
        return rupees(minor)
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(int(minor)), MINOR_PER_MAJOR)
    symbol = SYMBOLS.get(code)
    if symbol:
        return f"{sign}{symbol}{whole:,}.{frac:02d}"
    return f"{sign}{code} {whole:,}.{frac:02d}"


def paise_from_rupees(amount: int | float | str) -> int:
    """Convert a rupee amount to integer paise, rounding half up."""
    value = (Decimal(str(amount)) * MINOR_PER_MAJOR).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(value)


minor_from_major = paise_from_rupees


def is_valid_amount(value: object) -> bool:
    """True for a positive integer amount. Booleans are not amounts."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
