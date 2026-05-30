"""quant-catalyst-profitability.py — measure catalyst-signal edge on live data.

Joins the propagation log against realized yfinance forward returns, grouped by
relation class (brand_self consumer-trend vs sector edges), and prints whether each
class is paying. This is the feedback loop that decides whether to RAISE the
consumer-trend confidence haircut or prune the edges — verify profitability, never assume.
"""
from __future__ import annotations

import contextlib
import io
import sys
from datetime import date, timedelta
from pathlib import Path

_VENV = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if _VENV.exists() and sys.executable != str(_VENV):
    import os; os.execv(str(_VENV), [str(_VENV), __file__, *sys.argv[1:]])

from hermes_quant.catalyst.profitability import format_report, measure_profitability

_FWD_WINDOW_DAYS = 21  # ~1 month forward; catalyst horizon is days-to-weeks


def _yf_forward_return(symbol: str, asof: date) -> float | None:
    import warnings; warnings.filterwarnings("ignore")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            import pandas as pd
            import yfinance as yf
            df = yf.download(symbol, start=str(asof - timedelta(days=4)),
                             end=str(asof + timedelta(days=_FWD_WINDOW_DAYS + 6)),
                             interval="1d", auto_adjust=True, progress=False)
        except Exception:
            return None
    if df is None or len(df) == 0:
        return None
    close = df["Close"]
    if hasattr(close, "columns"):
        close = close.iloc[:, 0]
    close = close.dropna()
    import pandas as pd
    entry = close.index[close.index >= pd.Timestamp(asof)]
    if len(entry) == 0:
        return None
    entry_px = float(close.loc[entry[0]])
    exit_target = entry[0] + pd.Timedelta(days=_FWD_WINDOW_DAYS)
    ex = close.index[close.index >= exit_target]
    exit_px = float(close.loc[ex[0]]) if len(ex) else float(close.iloc[-1])
    return (exit_px / entry_px - 1) * 100


def main() -> int:
    # Cap fetches: measuring every sector edge every run is slow + redundant. The
    # decision this loop informs is the CONSUMER-TREND haircut, so cap rows; the
    # cron accumulates verdicts over days. max_rows bounds the yfinance fan-out.
    stats = measure_profitability(_yf_forward_return, max_rows=120)
    report = format_report(stats)
    # silence-by-default: if no scored data, stay quiet (no_agent cron)
    if not stats:
        return 0
    print("📊 " + report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
