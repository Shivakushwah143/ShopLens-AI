from __future__ import annotations


def evaluate_deal(current_price: float, historical_prices: list[float]) -> str:
    """Compare *current_price* against *historical_prices* and return a verdict.

    Returns one of ``"GOOD"``, ``"AVERAGE"``, or ``"OVERPRICED"``.
    """
    if not historical_prices:
        return "AVERAGE"

    avg = sum(historical_prices) / len(historical_prices)
    mn = min(historical_prices)
    mx = max(historical_prices)
    spread = mx - mn if mx != mn else 1.0

    # Within 5 % of historical minimum → good deal
    if current_price <= mn * 1.05:
        return "GOOD"

    # Above 90 % of the price range → overpriced
    if current_price >= mn + 0.9 * spread:
        return "OVERPRICED"

    return "AVERAGE"
