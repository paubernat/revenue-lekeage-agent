"""Currency conversion using the rate for the date, or the closest earlier one."""
from datetime import date

from core import repository


class FxError(ValueError):
    pass


def fx_convert(amount: float, from_ccy: str, to_ccy: str, on_date: date | str) -> dict:
    on_date = date.fromisoformat(on_date) if isinstance(on_date, str) else on_date
    from_ccy, to_ccy = from_ccy.upper(), to_ccy.upper()
    if from_ccy == to_ccy:
        return {"amount": round(amount, 2), "rate": 1.0, "rate_date": str(on_date), "note": "same currency"}

    candidates = []  # (rate_date, effective_rate)
    for r in repository.get_exchange_rates():
        r_date = date.fromisoformat(r["date"])
        if r_date > on_date:
            continue
        if (r["from_currency"], r["to_currency"]) == (from_ccy, to_ccy):
            candidates.append((r_date, r["rate"]))
        elif (r["from_currency"], r["to_currency"]) == (to_ccy, from_ccy):
            candidates.append((r_date, 1 / r["rate"]))
    if not candidates:
        raise FxError(f"No {from_ccy}->{to_ccy} rate on or before {on_date}")

    rate_date, rate = max(candidates)
    result = {
        "amount": round(amount * rate, 2),
        "rate": round(rate, 6),
        "rate_date": str(rate_date),
        "calculation": f"{amount} {from_ccy} x {round(rate, 6)} = {round(amount * rate, 2)} {to_ccy}",
    }
    if rate_date != on_date:
        result["note"] = f"No rate for {on_date}; used closest earlier rate from {rate_date}"
    return result
