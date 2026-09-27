"""Iranian financial-market data toolkit.

Layered architecture (each layer only depends on the ones above it):

    providers -> normalization -> storage -> candle engine -> indicator consumers
"""

__version__ = "0.1.0"
