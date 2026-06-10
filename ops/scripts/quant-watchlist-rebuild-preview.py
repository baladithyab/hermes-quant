#!/usr/bin/env python3
"""Non-destructive watchlist rebuild PREVIEW.

Scores the full universe with the LIVE scorer and emits two candidate active
lists WITHOUT touching the live play-fit.json:

  play-fit.PREVIEW-50perplay.json  — top-50 per play (max_per_play=50 semantics)
  play-fit.PREVIEW-50total.json     — top-50 unique symbols globally (best score
                                       across any play), with their qualifying plays

Reads:  ~/.hermes/quant/universe/alpaca-daily.json   (full ~500 universe)
Writes: previews into ~/.hermes/quant/watchlist/
Silent-by-default-ish: prints a compact summary table at the end.
"""
from __future__ import annotations
import json, os, sys, warnings, math
from pathlib import Path
warnings.filterwarnings("ignore")

QH = Path.home() / ".hermes" / "quant"
UNIV = QH / "universe" / "alpaca-daily.json"
WLDIR = QH / "watchlist"
PLAYS = ["covered_call", "csp", "wheel", "leaps", "swing"]
ONBOARD_FLOOR = 0.65  # match evolve_watchlist default

from hermes_quant.playbook import scorers as S

_raw = json.loads(UNIV.read_text())["symbols"]
# universe entries are dicts {"symbol": "NVDA", ...}; extract the ticker string.
syms = [e["symbol"] if isinstance(e, dict) else e for e in _raw]
print(f"universe: {len(syms)} symbols; prewarming snapshot cache...", file=sys.stderr)
try:
    S.prewarm_snapshot_cache(syms, max_workers=12)
except Exception as e:  # noqa: BLE001
    print(f"prewarm warn: {e}", file=sys.stderr)

# score every (symbol, play)
scores: dict[str, dict[str, float]] = {}
for s in syms:
    row = {}
    for p in PLAYS:
        try:
            v = S.score_symbol(s, p)
        except Exception:
            v = float("nan")
        row[p] = v
    scores[s] = row

def above(v: float) -> bool:
    return isinstance(v, (int, float)) and not math.isnan(v) and v >= ONBOARD_FLOOR

# ---- 50 per play ----
perplay = {}
for p in PLAYS:
    ranked = sorted(((s, scores[s][p]) for s in syms if above(scores[s][p])),
                    key=lambda x: -x[1])[:50]
    perplay[p] = ranked
uniq_perplay = sorted({s for p in PLAYS for s, _ in perplay[p]})

# ---- 50 total unique (best score across any play) ----
best = []
for s in syms:
    vals = [(scores[s][p], p) for p in PLAYS if above(scores[s][p])]
    if vals:
        bv, bp = max(vals)
        best.append((s, bv, bp, [p for _, p in vals]))
best.sort(key=lambda x: -x[1])
top50_total = best[:50]

# write previews (simple shape; live file rebuild happens via evolve_watchlist
# once you confirm semantics — this is just for eyeballing)
(WLDIR / "play-fit.PREVIEW-50perplay.json").write_text(json.dumps({
    "preview": True, "semantics": "top-50 per play, onboard_floor=%.2f" % ONBOARD_FLOOR,
    "n_unique_total": len(uniq_perplay),
    "per_play": {p: [{"symbol": s, "score": round(v, 3)} for s, v in perplay[p]] for p in PLAYS},
    "unique_symbols": uniq_perplay,
}, indent=1))
(WLDIR / "play-fit.PREVIEW-50total.json").write_text(json.dumps({
    "preview": True, "semantics": "top-50 unique symbols globally by best play score",
    "n_unique_total": len(top50_total),
    "symbols": [{"symbol": s, "best_score": round(v, 3), "best_play": bp, "qualifying_plays": qp}
                for s, v, bp, qp in top50_total],
}, indent=1))

print("\n=== REBUILD PREVIEW (live play-fit.json NOT touched) ===")
print(f"50-per-play  : {len(uniq_perplay)} unique symbols total across 5 plays")
for p in PLAYS:
    print(f"  {p:14s} {len(perplay[p]):2d}  top: " +
          ", ".join(f"{s}:{v:.2f}" for s, v in perplay[p][:5]))
print(f"50-total     : {len(top50_total)} unique symbols")
print("  top 10: " + ", ".join(f"{s}({bp[:4]}):{v:.2f}" for s, v, bp, qp in top50_total[:10]))
print("\npreviews written:")
print("  " + str(WLDIR / "play-fit.PREVIEW-50perplay.json"))
print("  " + str(WLDIR / "play-fit.PREVIEW-50total.json"))
