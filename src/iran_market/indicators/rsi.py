"""Wilder RSI helpers used to confirm directional signals."""

from __future__ import annotations


def rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    if period < 1:
        raise ValueError("RSI period must be >= 1")
    values: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return values

    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    def value(gain: float, loss: float) -> float:
        if loss == 0:
            return 50.0 if gain == 0 else 100.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    values[period] = value(average_gain, average_loss)
    for index in range(period + 1, len(closes)):
        change_index = index - 1
        average_gain = (average_gain * (period - 1) + gains[change_index]) / period
        average_loss = (average_loss * (period - 1) + losses[change_index]) / period
        values[index] = value(average_gain, average_loss)
    return values


def rsi_confirms(direction: str | None, current: float | None, previous: float | None) -> bool:
    if current is None or previous is None:
        return False
    if direction == "bullish":
        return current > 50 and current > previous
    if direction == "bearish":
        return current < 50 and current < previous
    return False


__all__ = ["rsi_confirms", "rsi_series"]