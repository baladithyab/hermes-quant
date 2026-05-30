import io
import contextlib
import math
import warnings; warnings.filterwarnings('ignore')
import numpy as np

err=io.StringIO()
with contextlib.redirect_stderr(err):
    import yfinance as yf

basket=['AAL','ANET','ASTS','AXP','BA','CCL','CRC','CRH','CRS','CRSP','CSX','DAL','DASH','DXCM','EMR','EOG','EQT','ESTC','FDX','FERG','FLEX','FTI','GM','HCA','HOOD','IOT','LNG','META','MRK','MRNA','MSFT','NKE','NU','ODFL','OXY','PATH','PYPL','RRC','SCHW','SMCI','SOFI','TT','UNP','WELL','WULF','ZS']
syms=basket+['AMZN','SPY']
with contextlib.redirect_stderr(err):
    data=yf.download(syms, period='3y', interval='1d', auto_adjust=True, progress=False)['Close']
data=data.dropna()
rets=np.log(data/data.shift(1)).dropna()
bsk=[s for s in basket if s in rets.columns]
ann=252
def sharpe(r):
    sd=r.std()*math.sqrt(ann)
    return ((r.mean()*ann)-0.043)/sd if sd>0 else 0
def cagr(r):
    c=np.exp(r.cumsum()); return c.iloc[-1]**(ann/len(r))-1

weights=[0.0,0.05,0.10,0.15,0.20,0.25,0.30,0.40,0.50,0.70,1.0]

def analyze(rsub, label):
    ew=rsub[bsk].mean(axis=1)
    amzn=rsub['AMZN']
    res={}
    for w in weights:
        port=(1-w)*ew+w*amzn
        res[w]=sharpe(port)
    best=max(weights,key=lambda w:res[w])
    return res, best, sharpe(ew), len(rsub)

n=len(rets)
half=n//2
periods={
    'FULL (3y, 753d)': rets,
    'IS  first-half': rets.iloc[:half],
    'OOS second-half': rets.iloc[half:],
    'Y1 (oldest 252d)': rets.iloc[:252],
    'Y2 (middle 252d)': rets.iloc[252:504],
    'Y3 (newest, rest)': rets.iloc[504:],
}
print(f"{'PERIOD':<20}{'days':>5}{'base_Sh':>8}  Sharpe by AMZN weight (best marked *)")
print(f"{'':20}{'':5}{'':8}  "+" ".join(f"{int(w*100):>4}" for w in weights))
print('-'*100)
peaks={}
for label,rsub in periods.items():
    res,best,base,nd=analyze(rsub,label)
    peaks[label]=best
    cells=[]
    for w in weights:
        mark='*' if w==best else ' '
        cells.append(f"{res[w]:4.2f}{mark}")
    print(f"{label:<20}{nd:>5}{base:>8.2f}  "+" ".join(cells))

print("\n=== OVERFIT CHECK: is the peak weight stable across periods? ===")
for label,b in peaks.items():
    print(f"  {label:<20} peak @ {int(b*100):>3}%")
io_peak=peaks['IS  first-half']; oos_peak=peaks['OOS second-half']
print(f"\nIn-sample peak: {int(io_peak*100)}%   Out-of-sample peak: {int(oos_peak*100)}%")
# Cross-validation: pick peak on IS, evaluate that weight's Sharpe rank on OOS
res_oos,_,_,_=analyze(rets.iloc[half:],'oos')
oos_at_is_peak=res_oos[io_peak]
oos_best_sharpe=res_oos[oos_peak]
oos_base=analyze(rets.iloc[half:],'oos')[2]
print(f"\nApplying the IS-chosen {int(io_peak*100)}% weight to OOS:")
print(f"  OOS Sharpe at IS-peak weight: {oos_at_is_peak:.2f}")
print(f"  OOS Sharpe at OOS-peak weight: {oos_best_sharpe:.2f}  (best achievable OOS)")
print(f"  OOS base (0% AMZN):            {oos_base:.2f}")
degradation = oos_best_sharpe - oos_at_is_peak
print(f"  Degradation from picking on IS: {degradation:+.3f} Sharpe")
improve_vs_base = oos_at_is_peak - oos_base
print(f"  IS-peak weight still beats base OOS by: {improve_vs_base:+.3f} Sharpe")
print("\nVERDICT:", end=" ")
if improve_vs_base > 0 and abs(io_peak-oos_peak)<=0.20:
    print("ROBUST — adding AMZN helps OOS, peak weight stable within a band.")
elif improve_vs_base > 0:
    print("DIRECTIONALLY ROBUST — AMZN helps OOS, but exact peak weight drifts (use a RANGE not a point).")
else:
    print("FRAGILE — IS-chosen weight does not improve OOS. The peak was window-specific.")
