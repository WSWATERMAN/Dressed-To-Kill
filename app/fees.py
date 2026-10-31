"""Paper-only estimates based on Kalshi's published event-contract fee schedule."""
from decimal import Decimal, ROUND_CEILING

CENT = Decimal("0.01")


def estimate_fee(ticker: str, price: Decimal, quantity: int, fee_type: str) -> dict:
    """Estimate one-side fee; market-specific schedule/fill behavior may be unknown."""
    if fee_type not in {"taker", "maker"}:
        raise ValueError("Choose taker or maker fee estimate")
    if not isinstance(price, Decimal) or not price.is_finite() or not Decimal("0") < price < Decimal("1"):
        raise ValueError("Price must be a finite decimal between 0 and 1")
    if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= 1000:
        raise ValueError("Quantity must be between 1 and 1,000 contracts")
    if not isinstance(ticker, str) or not ticker:
        raise ValueError("Market ticker is required")

    # The public fee schedule identifies these product-specific markets, but the
    # app cannot reliably determine the applicable market rule from public data.
    upper = ticker.upper()
    if upper.startswith(("INX", "NASDAQ100")):
        return {"fee": None, "fee_type": fee_type, "fee_status": "unavailable",
                "fee_note": "This product has a special fee schedule not modeled by the app."}

    # Paper orders do not know their actual maker/taker execution. This estimate
    # uses the selected schedule formula and is explicitly labeled accordingly.
    rate = Decimal("0.07") if fee_type == "taker" else Decimal("0.0175")
    fee = (rate * quantity * price * (Decimal("1") - price)).quantize(CENT, rounding=ROUND_CEILING)
    return {"fee": fee, "fee_type": fee_type, "fee_status": "estimated",
            "fee_note": f"Estimated {fee_type} fee; actual execution and market schedule may differ."}
