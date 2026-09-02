"""Pure provider adapters.

These modules deliberately depend only on the standard library and the domain
objects.  Network clients belong above this boundary.
"""

from .eastmoney import EastmoneyQuoteProvider
from .normalization import ProviderNormalizationError
from .runtime import (
    AKShareQuoteProvider,
    AKShareSnapshotProvider,
    BaoStockTradingCalendar,
    ProviderRuntimeError,
    TushareDailyProvider,
)

__all__ = [
    "AKShareQuoteProvider",
    "AKShareSnapshotProvider",
    "BaoStockTradingCalendar",
    "EastmoneyQuoteProvider",
    "ProviderNormalizationError",
    "ProviderRuntimeError",
    "TushareDailyProvider",
]
