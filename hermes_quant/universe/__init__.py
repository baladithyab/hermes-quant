"""Universe scanner — produces the daily list of US equities for the daemon.

Replaces the hardcoded ~/.hermes/scripts/quant-universe-interim.txt with a
liquidity- and price-filtered scan against the Alpaca paper API.
"""

from hermes_quant.universe.alpaca_scanner import scan_universe

__all__ = ["scan_universe"]
