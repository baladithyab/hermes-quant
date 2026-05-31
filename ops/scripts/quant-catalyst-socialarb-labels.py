# Build the labeled social-arb eval set with REAL forward returns from yfinance.
# Cases from the research (Camillo's documented social-arb wins). Each: entity, the
# approximate date the social trend surfaced (NOT when it peaked), the ticker, and we
# compute the realized forward return over a holding window from the NEXT bar.
import io
import contextlib
import datetime as dt
import json
import pathlib
import warnings; warnings.filterwarnings('ignore')

# N13: write the VERSIONED fixture (committed, offline-deterministic), NEVER /tmp.
# Resolved repo-relative from this script (ops/scripts/ -> repo root -> tests/fixtures).
LABELS_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "tests" / "fixtures" / "socialarb" / "camillo_labels.json"
)
err=io.StringIO()
with contextlib.redirect_stderr(err):
    import yfinance as yf
import pandas as pd

# (label, ticker, surfaced_date, headline_text, fwd_window_days)
# surfaced_date = when the consumer trend became observable (Camillo's entry-ish).
# We use generous forward windows because social-arb is a weeks-to-months thesis, not intraday.
cases = [
    ("Celsius TikTok energy-drink craze", "CELH", "2021-03-01",
     "Celsius energy drink goes viral on TikTok as sales surge among Gen Z", 120),
    ("Crocs pandemic resurgence", "CROX", "2020-06-01",
     "Crocs sales soar as healthcare workers and celebrity collabs drive viral demand", 150),
    ("Dorel bike shortage (pandemic)", "DIIBF", "2020-04-15",
     "Pandemic bike shortage sends demand soaring for bicycle maker Dorel", 180),
    ("Tapestry / Coach bag demand", "TPR", "2023-08-01",
     "Coach handbags surge in popularity on social media driving Tapestry sales", 120),
    ("Newell / Elmer's slime craze", "NWL", "2017-09-01",
     "Elmer's glue sales jump as DIY slime craze goes viral with kids", 120),
]

def fwd_return(ticker, start, window):
    s=dt.date.fromisoformat(start)
    end=s+dt.timedelta(days=window+10)
    buf=io.StringIO()
    with contextlib.redirect_stderr(buf):
        try:
            df=yf.download(ticker, start=str(s-dt.timedelta(days=7)), end=str(end), interval='1d', auto_adjust=True, progress=False)
        except Exception as ex:
            return None,None,str(ex)
    if df is None or len(df)==0:
        return None,None,"no data"
    close=df['Close']
    if hasattr(close,'columns'): close=close.iloc[:,0]
    close=close.dropna()
    # entry = first bar ON or AFTER surfaced_date (next tradeable bar = lookahead-honest)
    entry_idx=close.index[close.index>=pd.Timestamp(s)]
    if len(entry_idx)==0: return None,None,"no entry bar"
    entry_px=float(close.loc[entry_idx[0]])
    # exit = bar ~window days later
    exit_target=entry_idx[0]+pd.Timedelta(days=window)
    exit_idx=close.index[close.index>=exit_target]
    if len(exit_idx)==0:
        exit_px=float(close.iloc[-1])  # use last available
    else:
        exit_px=float(close.loc[exit_idx[0]])
    ret=(exit_px/entry_px-1)*100
    return ret, (entry_px,exit_px,str(entry_idx[0].date())), None

out=[]
for label,tk,d,headline,win in cases:
    ret,px,errmsg=fwd_return(tk,d,win)
    out.append(dict(label=label,ticker=tk,date=d,headline=headline,window=win,fwd_return_pct=ret,detail=px,err=errmsg))
    if ret is not None:
        print(f"{tk:7s} {d}  +{win}d  ret={ret:+7.1f}%  entry/exit={px[0]:.2f}->{px[1]:.2f} @ {px[2]}")
    else:
        print(f"{tk:7s} {d}  FAILED: {errmsg}")
LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
json.dump(out, open(LABELS_PATH, 'w'), indent=2)
print(f"\nsaved {LABELS_PATH}")
