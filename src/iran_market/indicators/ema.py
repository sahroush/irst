"""Exponential moving average for the daily EMA screens."""

from __future__ import annotations


def ema_series(closes: list[float], period: int) -> list[float | None]:
    """EMA seeded with the SMA of the first `period` closes, None before."""
    if period < 1:
        raise ValueError("EMA period must be >= 1")
    values: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return values
    alpha = 2.0 / (period + 1)
    values[period - 1] = sum(closes[:period]) / period
    for index in range(period, len(closes)):
        values[index] = closes[index] * alpha + values[index - 1] * (1.0 - alpha)
    return values


__all__ = ["ema_series"]
