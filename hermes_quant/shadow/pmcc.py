"""hermes_quant.shadow.pmcc — multi-leg PMCC shadow position tracker (ADR-0029 gap).

The Poor-Man's Covered Call (deep-ITM LEAPS long + rolling short call) cannot be
EXECUTED yet — the multi-leg options reactor is the ADR-0029 gap; PaperReactor is
equity-only. So we track it as a SHADOW STRUCTURE: a recorded 2-leg position that is
marked-to-model (Black-Scholes) on demand, so when the multi-leg reactor lands we have
a documented, daily-marked counterfactual to validate the live fills against.

Why a dedicated tracker (not ShadowAccount): ShadowAccount's schema is
(ticker, quantity, avg_entry_price) — single-leg equity. A PMCC's economics live in
the RELATIONSHIP between two option legs (net theta, net delta, upside cap, breakeven),
which a single-ticker row cannot represent without lying about the greeks the structure
exists to manage. This tracker records both legs + marks the spread.

NOT a reactor. Writes nothing to executions.jsonl / state.db. Pure shadow:
~/.hermes/quant/shadow/pmcc-positions.jsonl (append-only) + on-demand mark.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from hermes_quant.home import quant_home as _resolve_quant_home

logger = logging.getLogger(__name__)

_DEFAULT_STORE = _resolve_quant_home() / "shadow" / "pmcc-positions.jsonl"
_RISK_FREE = 0.043  # matches the analysis assumption; refresh from a curve later.


# --------------------------------------------------------------------------- #
# Black-Scholes (vendored-thin; the optlib kernel is the production path once
# multi-leg lands, but this tracker stays dependency-light for shadow marking).
# --------------------------------------------------------------------------- #
def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_call(S: float, K: float, T: float, sigma: float, r: float = _RISK_FREE) -> float:  # noqa: N803  (S/K/T = standard Black-Scholes notation)
    if T <= 0:
        return max(S - K, 0.0)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)


def bs_call_delta(S: float, K: float, T: float, sigma: float, r: float = _RISK_FREE) -> float:  # noqa: N803
    if T <= 0:
        return 1.0 if S > K else 0.0
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1)


def bs_call_theta_day(S: float, K: float, T: float, sigma: float, r: float = _RISK_FREE) -> float:  # noqa: N803
    """Per-DAY theta (calendar) of a long call. Negative = decay."""
    if T <= 1.0 / 365.0:
        return 0.0
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    term1 = -(S * _norm_pdf(d1) * sigma) / (2 * math.sqrt(T))
    theta_annual = term1 - r * K * math.exp(-r * T) * _norm_cdf(d2)
    return theta_annual / 365.0


# --------------------------------------------------------------------------- #
# PMCC position model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class OptionLeg:
    side: Literal["long", "short"]
    expiry: str            # ISO date
    strike: float
    entry_premium: float   # per share
    entry_iv: float        # implied vol at entry (decimal)
    contracts: int = 1


@dataclass(frozen=True)
class PMCCPosition:
    symbol: str
    opened_at: str               # ISO datetime (PUB/decision time — fidelity anchor)
    long_leg: OptionLeg          # deep-ITM LEAPS call
    short_leg: OptionLeg         # near-dated OTM call (the rolling cover)
    spot_at_open: float
    note: str = ""
    structure: str = "poor_mans_covered_call"

    def net_debit(self) -> float:
        """Capital outlay at open = long cost - short credit, each leg valued by ITS OWN
        contract count × 100 shares/contract.

        ar26: the prior form ``(long_premium - short_premium) * 100 * long.contracts``
        scaled the SHORT credit by the LONG leg's contract count, so on a RATIO PMCC
        (long.contracts != short.contracts) the entry basis disagreed with
        ``mark_pmcc``, which already values each leg by its own contracts
        (``short_v`` uses ``short_leg.contracts`` at pmcc.py:151). That mismatch biased
        ``unrealized_pnl = net_value - net_debit()`` by short_credit×(long_contracts -
        short_contracts) × 100. Per-leg valuation makes the entry basis consistent with
        the mark; for the common 1:1 PMCC this is byte-identical."""
        long_cost = self.long_leg.entry_premium * 100 * self.long_leg.contracts
        short_credit = self.short_leg.entry_premium * 100 * self.short_leg.contracts
        return long_cost - short_credit

    def to_dict(self) -> dict:
        d = asdict(self)
        d["net_debit"] = round(self.net_debit(), 2)
        return d


@dataclass(frozen=True)
class PMCCMark:
    asof: str
    spot: float
    long_value: float
    short_value: float
    net_value: float          # long_value - short_value (what the spread is worth now)
    unrealized_pnl: float     # net_value - net_debit_at_open
    net_delta: float          # long_delta - short_delta (per contract, x100 = $ per $1 move)
    net_theta_day: float      # long_theta - short_theta (>0 means we COLLECT net theta)
    long_dte: int
    short_dte: int

    def to_dict(self) -> dict:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def _dte(expiry: str, asof: date) -> int:
    return (date.fromisoformat(expiry) - asof).days


def mark_pmcc(
    pos: PMCCPosition,
    *,
    spot: float,
    asof: date | None = None,
    long_iv: float | None = None,
    short_iv: float | None = None,
) -> PMCCMark:
    """Mark-to-model the PMCC at ``spot``/``asof``. IVs default to entry IVs.

    Net theta sign convention: long leg theta is negative (we pay), short leg theta
    is negative for the option but we are SHORT it (we collect), so
    net_theta_day = long_theta - short_theta. A deep-ITM-LEAPS-long + near-ATM-short
    PMCC is typically NET-POSITIVE theta (the short bleed we collect exceeds the slow
    LEAPS bleed) — the whole point of the structure.
    """
    asof = asof or datetime.now(UTC).date()
    liv = long_iv if long_iv is not None else pos.long_leg.entry_iv
    siv = short_iv if short_iv is not None else pos.short_leg.entry_iv
    n = pos.long_leg.contracts

    lt = max(_dte(pos.long_leg.expiry, asof), 0) / 365.0
    st = max(_dte(pos.short_leg.expiry, asof), 0) / 365.0

    long_v = bs_call(spot, pos.long_leg.strike, lt, liv) * 100 * n
    short_v = bs_call(spot, pos.short_leg.strike, st, siv) * 100 * pos.short_leg.contracts
    net_v = long_v - short_v

    long_d = bs_call_delta(spot, pos.long_leg.strike, lt, liv) * 100 * n
    short_d = bs_call_delta(spot, pos.short_leg.strike, st, siv) * 100 * pos.short_leg.contracts
    net_delta = long_d - short_d

    long_th = bs_call_theta_day(spot, pos.long_leg.strike, lt, liv) * 100 * n
    short_th = bs_call_theta_day(spot, pos.short_leg.strike, st, siv) * 100 * pos.short_leg.contracts
    net_theta = long_th - short_th  # short_th is negative; minus a negative = collect

    return PMCCMark(
        asof=asof.isoformat(),
        spot=round(spot, 2),
        long_value=round(long_v, 2),
        short_value=round(short_v, 2),
        net_value=round(net_v, 2),
        unrealized_pnl=round(net_v - pos.net_debit(), 2),
        net_delta=round(net_delta, 2),
        net_theta_day=round(net_theta, 2),
        long_dte=_dte(pos.long_leg.expiry, asof),
        short_dte=_dte(pos.short_leg.expiry, asof),
    )


def record_pmcc(pos: PMCCPosition, *, path: Path | None = None) -> int:
    """Append a PMCC shadow position to the tracker JSONL. Returns count written."""
    p = path or _DEFAULT_STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(pos.to_dict(), default=str) + "\n")
            f.flush()
    except OSError as e:  # noqa: BLE001
        logger.warning("shadow.pmcc: record failed: %s", e)
        return 0
    return 1


def load_pmcc_positions(*, path: Path | None = None) -> list[PMCCPosition]:
    """Load tracked PMCC shadow positions. Returns [] on missing/error."""
    p = path or _DEFAULT_STORE
    if not p.exists():
        return []
    out: list[PMCCPosition] = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            if not isinstance(raw, dict):
                continue  # valid JSON but not an object (corrupt/partial append) — skip
            out.append(PMCCPosition(
                symbol=raw["symbol"],
                opened_at=raw["opened_at"],
                long_leg=OptionLeg(**{k: raw["long_leg"][k] for k in
                                      ("side", "expiry", "strike", "entry_premium", "entry_iv", "contracts")}),
                short_leg=OptionLeg(**{k: raw["short_leg"][k] for k in
                                       ("side", "expiry", "strike", "entry_premium", "entry_iv", "contracts")}),
                spot_at_open=raw["spot_at_open"],
                note=raw.get("note", ""),
                structure=raw.get("structure", "poor_mans_covered_call"),
            ))
    except (OSError, KeyError, json.JSONDecodeError) as e:
        logger.warning("shadow.pmcc: load failed: %s", e)
        return []
    return out
