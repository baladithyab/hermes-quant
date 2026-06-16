"""quantcore.mask — leakage-masked replay (B-31, arch §4.4).

Model-weight lookahead is the binding honesty constraint for a Claude-powered
advisor: a frontier model has near-perfect recall of pre-cutoff prices and
narratives and performs "motivated reasoning" (working backward from memorized
outcomes) that is INVISIBLE from the rationale (Lopez-Lira 2504.14765). asof-
honesty on the *data* pipeline does nothing against it — the leak is in the
weights. KTD-Fin (2605.28359 §4.2) showed that anonymizing tickers/dates drives
a memory-only agent to 0.00% voluntary cash (it cannot act), proving the edge
rode on memorized identity.

This module implements KTD-Fin's masking discipline so any historical replay is
interpretable:

  * an EPISODE-LEVEL alias map: real tickers -> opaque ids, absolute dates ->
    relative day-indices, **stable within an episode, reshuffled across episodes**
    (a global static map would just be re-memorized — the reshuffle is the
    security property);
  * the un-mask-on-query / re-mask-on-return tool shim, so the committee never
    sees a real ticker or date even transiently through a tool result;
  * a DETERMINISTIC de-anonymization probe (a correlation/k-NN attacker over
    return fingerprints) with a BASELINE-AWARE assertion — recovery must be within
    a small margin of chance, not merely "small" (a 500-name universe gives ~1%
    top-5 for free).

Policy (rail R9): masked mode is MANDATORY for any number used in a config/weight
decision; bright-mode results are quarantined diagnostics. Forward-only paper
trading remains the gold-standard track record; replay is only ever a sanity check.

stdlib + numpy only.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable

# A ticker token: 1-5 uppercase letters, optional .X suffix, on word boundaries.
_TICKER_RE = re.compile(r"\b[A-Z]{1,5}(?:\.[A-Z])?\b")
# ISO-8601 date or datetime.
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b")

MaskLevel = str  # "bright" | "stock_blind" | "date_blind" | "blinded"
_LEVELS = ("bright", "stock_blind", "date_blind", "blinded")


def _day_key(d: Any) -> str:
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    s = str(d)
    return s[:10]  # YYYY-MM-DD prefix


@dataclass
class AliasCodec:
    """Bidirectional real<->alias codec for ONE episode.

    Reshuffle per episode by passing a distinct ``seed`` (e.g. an episode index
    or a hash of the episode's date range). Never reuse one codec across episodes
    in a way that would let the model align aliases — that defeats the mask.
    """

    level: MaskLevel = "blinded"
    t2a: dict[str, str] = None  # type: ignore[assignment]
    a2t: dict[str, str] = None  # type: ignore[assignment]
    d2i: dict[str, str] = None  # type: ignore[assignment]
    i2d: dict[str, str] = None  # type: ignore[assignment]

    @classmethod
    def build(
        cls,
        tickers: Iterable[str],
        dates: Iterable[Any],
        *,
        seed: int,
        level: MaskLevel = "blinded",
    ) -> "AliasCodec":
        if level not in _LEVELS:
            raise ValueError(f"level must be one of {_LEVELS}, got {level!r}")
        tickers = sorted({str(t).upper() for t in tickers})
        days = sorted({_day_key(d) for d in dates})
        rng = random.Random(seed)
        perm = list(range(len(tickers)))
        rng.shuffle(perm)  # per-episode reshuffle — the security property
        # zero-pad BOTH alias namespaces so no alias is a substring of another
        # (un-padded "day_1" clobbers "day_10" on naive replace — review P1).
        t2a = {t: f"ASSET_{perm[i]:06d}" for i, t in enumerate(tickers)}
        d2i = {d: f"day_{i:06d}" for i, d in enumerate(days)}
        return cls(
            level=level,
            t2a=t2a,
            a2t={v: k for k, v in t2a.items()},
            d2i=d2i,
            i2d={v: k for k, v in d2i.items()},
        )

    # -- token-level -------------------------------------------------------

    def _ticker_pattern(self):
        """Match ONLY known-universe tickers (exact alternation), not any all-caps
        token. A blind \\b[A-Z]{1,5}\\b matches prose ('ON','ALL','A') and would
        mask English words that happen to be tickers (review P1). Longest-first so
        a short ticker can't shadow a longer one."""
        if not self.t2a:
            return None
        toks = sorted((re.escape(t) for t in self.t2a), key=len, reverse=True)
        return re.compile(r"\b(?:" + "|".join(toks) + r")\b")

    def _mask_str(self, s: str) -> str:
        out = s
        if self.level in ("stock_blind", "blinded"):
            pat = self._ticker_pattern()
            if pat is not None:
                out = pat.sub(lambda m: self.t2a[m.group(0)], out)
        if self.level in ("date_blind", "blinded"):
            out = _ISO_DATE_RE.sub(
                lambda m: self.d2i.get(_day_key(m.group(0)), m.group(0)), out
            )
        return out

    def _unmask_str(self, s: str) -> str:
        out = s
        # longest-first so e.g. day_000001 is replaced before day_000010 cannot
        # be partially consumed (defense-in-depth on top of zero-padding)
        for alias in sorted(self.a2t, key=len, reverse=True):
            out = out.replace(alias, self.a2t[alias])
        for idx in sorted(self.i2d, key=len, reverse=True):
            out = out.replace(idx, self.i2d[idx])
        return out

    # -- recursive over JSON-like structures (keys AND values) -------------

    def mask(self, obj: Any) -> Any:
        return self._walk(obj, self._mask_str)

    def unmask(self, obj: Any) -> Any:
        return self._walk(obj, self._unmask_str)

    def _walk(self, obj: Any, fn) -> Any:
        if isinstance(obj, str):
            return fn(obj)
        if isinstance(obj, dict):
            return {fn(k) if isinstance(k, str) else k: self._walk(v, fn) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return type(obj)(self._walk(v, fn) for v in obj)
        if isinstance(obj, (datetime, date)) and fn is self._mask_str:
            # mask a datetime/date object by its day key if known
            return self.d2i.get(_day_key(obj), obj) if self.level in ("date_blind", "blinded") else obj
        return obj  # numbers/bools/None pass through unchanged (signal preserved)


def masked_tool(real_tool, codec: AliasCodec):
    """Wrap a data tool so the agent only ever sees aliases.

    un-mask on query (so the real data layer is hit) -> call -> re-mask on return
    (so no real ticker/date reaches the model, even transiently). KTD-Fin §3.3.
    """

    def wrapper(**masked_kwargs):
        real_kwargs = codec.unmask(masked_kwargs)
        result = real_tool(**real_kwargs)
        return codec.mask(result)

    return wrapper


# -- de-anonymization probe (deterministic correlation attacker) -----------


def _pearson(a, b) -> float:
    import numpy as np

    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a, b = a[:n], b[:n]
    sa, sb = a.std(), b.std()
    if sa < 1e-12 or sb < 1e-12:
        return 0.0
    return float(((a - a.mean()) * (b - b.mean())).mean() / (sa * sb))


def deanon_probe(
    masked_series: dict[str, list[float]],
    reference_panel: dict[str, list[float]],
    *,
    k: int = 5,
) -> dict[str, float]:
    """Deterministic attacker: match each masked return series to its closest
    real series in a reference panel by Pearson correlation. Returns top-1 and
    top-k recovery rates. Catches numeric-fingerprint leakage an LLM attacker
    would miss, and is reproducible in CI (no model call).

    ``masked_series``: alias -> return series (what the agent could see).
    ``reference_panel``: real_ticker -> return series (the attacker's prior).
    Truth is recovered via the alias suffix mapping the caller knows; here we
    score using a provided ``truth`` embedded as the panel keys matching the
    unmasked identity — callers pass an aligned panel keyed by the TRUE ticker
    and an alias->true map is not needed because we compare ranks per alias.
    """
    import numpy as np  # noqa: F401  (used by _pearson)

    refs = list(reference_panel.items())
    tk1 = tk_k = 0
    n = 0
    for alias, series in masked_series.items():
        # the true ticker is encoded by the caller as alias.split("::")[-1]
        true = alias.split("::")[-1]
        scored = sorted(
            ((_pearson(series, rseries), rtick) for rtick, rseries in refs),
            key=lambda x: -x[0],
        )
        ranked = [t for _, t in scored]
        if ranked and ranked[0] == true:
            tk1 += 1
        if true in ranked[:k]:
            tk_k += 1
        n += 1
    n = max(n, 1)
    return {"tk1": tk1 / n, "tk_k": tk_k / n, "n": n, "k": k}


def assert_mask_not_leaky(
    probe: dict[str, float], *, universe_size: int, eps: float = 0.02
) -> None:
    """Baseline-aware CI assertion (KTD-Fin §4.3). Recovery must be within ``eps``
    of chance, not merely 'small': a large universe gives low recovery for free.
    """
    base_tk1 = 1.0 / max(universe_size, 1)
    base_tkk = min(1.0, probe.get("k", 5) / max(universe_size, 1))
    if probe["tk1"] - base_tk1 > eps:
        raise AssertionError(
            f"mask leaks: top-1 recovery {probe['tk1']:.3f} exceeds chance "
            f"{base_tk1:.3f} by > {eps}"
        )
    if probe["tk_k"] - base_tkk > eps:
        raise AssertionError(
            f"mask leaks: top-{probe.get('k',5)} recovery {probe['tk_k']:.3f} "
            f"exceeds chance {base_tkk:.3f} by > {eps}"
        )
