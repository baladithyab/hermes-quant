"""hermes_quant.playbook — Per-play watchlists, profiles, and fitness scorers.

This package houses two complementary subsystems:

1. **Watchlist evolution** (watchlist_evolution.py)
   The operator's long-running watchlist state. The five plays —
   covered_call, csp, wheel, leaps, swing — each maintain their own ranked
   watchlist. Symbols are onboarded when their per-play fitness score holds
   above a floor for K consecutive runs and evicted when score collapses or
   explicit eviction rules fire. State persists across daemon restarts at:

       ~/.hermes/quant/watchlist/play-fit.json
       ~/.hermes/quant/watchlist/journal.jsonl

2. **Per-play fitness scorers** (profiles.py + scorers.py)
   PlayProfile dataclasses encoding hard / soft / eviction rules, plus
   pure scoring functions:
       score_covered_call, score_csp, score_wheel, score_leaps, score_swing
   and a yfinance-backed snapshot builder, compute_play_snapshot.

The scorer is decoupled from the watchlist evolver — the evolver consumes
PlayFitness results but performs no data acquisition itself.

This package is **watchlist scoring only**. It does not size positions or
generate orders. Silence-by-default: missing inputs fail hard rules.
"""

from hermes_quant.playbook.direction_bias import (
    bias_allows_direction,
    compatible_plays,
    direction_play_compatible,
    play_bias,
)
from hermes_quant.playbook.profiles import (
    PROFILES,
    PlayProfile,
    profile_covered_call,
    profile_csp,
    profile_leaps,
    profile_swing,
    profile_wheel,
)
from hermes_quant.playbook.scorers import (
    PlayFitness,
    compute_play_snapshot,
    prewarm_snapshot_cache,
    score_all,
    score_covered_call,
    score_csp,
    score_leaps,
    score_play,
    score_swing,
    score_symbol,
    score_wheel,
)
from hermes_quant.playbook.watchlist_evolution import (
    PLAY_NAMES,
    WatchlistEntry,
    evolve_watchlist,
    get_active_watchlist,
)

__all__ = [
    # watchlist evolution
    "PLAY_NAMES",
    "WatchlistEntry",
    "evolve_watchlist",
    "get_active_watchlist",
    # direction-vs-play-bias compatibility (B04 / A5)
    "bias_allows_direction",
    "compatible_plays",
    "direction_play_compatible",
    "play_bias",
    # fitness scoring
    "PROFILES",
    "PlayFitness",
    "PlayProfile",
    "compute_play_snapshot",
    "prewarm_snapshot_cache",
    "profile_covered_call",
    "profile_csp",
    "profile_leaps",
    "profile_swing",
    "profile_wheel",
    "score_all",
    "score_covered_call",
    "score_csp",
    "score_leaps",
    "score_play",
    "score_swing",
    "score_symbol",
    "score_wheel",
]
