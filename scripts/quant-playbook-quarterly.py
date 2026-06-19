#!/usr/bin/env python3
"""quant-playbook-quarterly.py — Quarterly portfolio review (ADR-0035 wave 4).

Schedule: first Monday of January, April, July, October at 06:30 PT (09:30 ET).
  Cron expression: '30 6 1-7 1,4,7,10 1'
  Standard cron idiom for first-Monday-of-quarter:
    - day-of-month in 1..7 (only the first week of the month)
    - day-of-week = 1 (Monday)
    - month-of-year in {1, 4, 7, 10} (Jan, Apr, Jul, Oct)
    - 30 6 = 06:30 in the cron host's local TZ (Pacific)

Posture: READ-MOSTLY.
  - Never places orders.
  - Computes portfolio metrics from executions.jsonl + yfinance marks.
  - Flags factor-exposure breaches.
  - LOGS rebalance proposals (close-overweight, scale-underweight) but does
    NOT auto-fire them — quarterly cadence + manual confirmation is the
    appropriate ergonomic for this much capital sitting in a position.
  - Emits a markdown report to ~/.hermes/quant/quarterly-reports/<YYYYQ#>.md
    AND prints it to stdout so the cron can deliver the same content to
    Discord verbatim (no_agent mode).
  - Halt-state guard: if a system-wide halt is active, emit a halt notice
    and skip the metric computation. Quarterly review is informational —
    no point fighting a halted account.

Flags:
  --dry-run    Read mock portfolio (tests/quarterly_mock_portfolio.json or
               --portfolio-file path) instead of executions.jsonl. Always
               safe; does not write the report file unless --write is set.
  --portfolio-file PATH
               Override the positions source. JSON format:
               {"cash": <float>, "positions": [{"symbol", "qty", "cost_basis"}]}
  --write      In dry-run, also write the report file (otherwise stdout-only).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# Use the hermes-agent venv where yfinance is installed.
# Only re-exec when run as a script (__name__ == "__main__"); never on import,
# so unit tests can importlib this module without triggering os.execv.
HERMES_VENV_PY = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python3"
if (
    __name__ == "__main__"
    and HERMES_VENV_PY.exists()
    and Path(sys.executable).resolve() != HERMES_VENV_PY.resolve()
    # Don't re-exec if we're already inside the same venv tree (handles
    # python vs python3 symlink differences).
    and not str(Path(sys.executable).resolve()).startswith(
        str((HERMES_VENV_PY.parent).resolve())
    )
):
    os.execv(str(HERMES_VENV_PY), [str(HERMES_VENV_PY), __file__, *sys.argv[1:]])

# Silence noisy third-party loggers — yfinance and curl-cffi tend to emit
# warnings on missing-bar / unstable-network paths that aren't actionable.
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("yfinance", "peewee", "urllib3", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.ERROR)

logger = logging.getLogger("quant-playbook-quarterly")

# ---------- paths ----------
HERMES_HOME = Path.home() / ".hermes"
QUANT_HOME = HERMES_HOME / "quant"
EXECUTIONS_PATH = QUANT_HOME / "executions.jsonl"
HALT_MIRROR_PATH = QUANT_HOME / "halt_state.json"
REPORT_DIR = QUANT_HOME / "quarterly-reports"
SECTOR_CACHE_PATH = QUANT_HOME / "cache" / "sector-beta-cache.json"
REBALANCE_LOG_PATH = QUANT_HOME / "quarterly-rebalance-proposals.jsonl"

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# ---------- factor-exposure thresholds (ADR-0035 §Quarterly review) ----------
SECTOR_CONCENTRATION_LIMIT = 0.30   # Any sector > 30% NAV → flag
PORTFOLIO_BETA_LO = 0.5             # < 0.5 → flag (under-exposed)
PORTFOLIO_BETA_HI = 1.5             # > 1.5 → flag (over-exposed)
NET_DOLLAR_LIMIT = 0.60             # Net dollar exposure > 60% NAV → flag
TOP_POSITION_LIMIT = 0.15           # Top-1 position > 15% NAV → flag
TOP_N_TABLE = 10                    # Show top N positions in the report


# ---------- utilities ----------
def utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def quarter_label(now: datetime | None = None) -> str:
    """Return the current quarter label, e.g. '2026Q2'."""
    now = now or datetime.now(UTC).astimezone(ET)
    q = (now.month - 1) // 3 + 1
    return f"{now.year}Q{q}"


def append_rebalance_log(record: dict[str, Any]) -> None:
    """Append-only JSONL log of proposed rebalance actions. Never raises."""
    record.setdefault("ts", utcnow_iso())
    REBALANCE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(REBALANCE_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        sys.stderr.write(f"rebalance log write failed: {e}\n")


# ---------- halt-state fail-closed gate ----------
def read_active_halts() -> list[dict]:
    """Read ~/.hermes/quant/halt_state.json. Returns active halts (empty = OK).

    Wave 1d fix (2026-05-27): filter to equity-relevant halts only.
    Crypto-only halts (asset_class='crypto') do not affect the quarterly
    equity portfolio review. Only account-wide ('*', None) or 'equity'
    class halts abort the quarterly review.
    """
    if not HALT_MIRROR_PATH.exists():
        return []
    try:
        data = json.loads(HALT_MIRROR_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return [{"reason": f"halt_state.json corrupt: {e}", "scope": "fail-closed"}]
    if not isinstance(data, list):
        return []
    equity_halts = []
    for h in data:
        ac = h.get("asset_class")
        # Only halt equity quarterly review for account-wide or equity halts.
        if ac in (None, "*", "equity"):
            equity_halts.append(h)
    return equity_halts


def is_first_monday_of_quarter(now: datetime | None = None) -> bool:
    """True if `now` (in ET) is the first Monday of Jan/Apr/Jul/Oct.

    Defensive: traditional cron treats DOM and DOW as OR when both are
    restricted (POSIX), so '30 6 1-7 1,4,7,10 1' may fire on every
    Monday of those months OR every day in 1-7 of those months. We
    guard inside the script: only proceed if it's actually the first
    Monday of a quarter month.
    """
    now = now or datetime.now(UTC).astimezone(ET)
    if now.month not in (1, 4, 7, 10):
        return False
    if now.weekday() != 0:  # 0 = Monday
        return False
    if now.day > 7:  # only the first week
        return False
    return True


# ---------- positions ----------
@dataclass
class Position:
    symbol: str
    qty: float
    cost_basis: float          # total cost paid (USD)
    last_price: float = 0.0    # mark-to-market price
    sector: str = "Unknown"
    beta: float = 1.0

    @property
    def market_value(self) -> float:
        return self.qty * self.last_price

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_basis

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.cost_basis == 0:
            return 0.0
        return (self.market_value - self.cost_basis) / abs(self.cost_basis)


# cs14/cs17 schema-drift family (ar15): the LIVE producer
# (hermes_quant.react.base.ExecutionRecord -> hermes_quant.react.paper._record_to_dict)
# emits a record shape the OLD `schema_version == 1` + `side`/`qty` filter CANNOT read.
# A real record carries NO `schema_version` key at all (so `.get(...)` is None != 1 and
# every live fill was DROPPED), NO `side`, and NO `qty` — instead it has a signed
# NAV-fraction `target_position_pct` (and `fill_size_pct`), a `fill_price`, an
# `asof_execution`, a `reactor_name`, and an optional `reactor_metadata.quantity`
# (the authoritative signed absolute share count from det-equity / live broker
# reconciliation). The result was a silently-EMPTY book -> cash-only NAV -> ZERO
# factor/sector/beta breach proposals: a fail-open risk surface.
#
# This mirrors the cs14 weekly-exit loader remediation
# (hermes_quant.daemon.portfolio_loader.reconstruct_portfolio absolute-target path):
# reconstruct positions from the signed NAV-fraction target via LATEST-TARGET-per-symbol
# semantics (later fills SUPERSEDE earlier ones — they do NOT delta-sum, which would
# dual-ledger inflate per ADR-0091), deriving share qty = target_pct * NAV / fill_price
# (reactor_metadata.quantity preferred when present), and folding the cost-basis cash
# leg out of cash so NAV stays coherent. The legacy int-1 side/qty path is preserved
# for hand-rolled / crypto-settlement records.
EQUITY_FILL_REACTORS = frozenset({"paper", "deterministic-equity", "alpaca_paper"})


def _is_absolute_target_record(rec: dict[str, Any]) -> bool:
    """True iff `rec` is a live absolute-target ExecutionRecord shape.

    The producer emits no `schema_version` key (reads back as None/absent); some
    forward-compat writers may stamp an explicit non-int sentinel. The legacy
    int-1 settlement shape (`schema_version == 1` with explicit side/qty) is
    DISJOINT and handled by the legacy branch. A record with a signed
    `target_position_pct` and no explicit int-1 sentinel is an absolute-target
    record.
    """
    sv = rec.get("schema_version")
    if sv == 1:
        return False
    return "target_position_pct" in rec


def load_positions_from_executions() -> tuple[float, list[Position]]:
    """Reconstruct positions from executions.jsonl.

    Returns (cash, positions). cash defaults to $100k initial - net flows.
    Consumes BOTH the legacy int-1 hand-rolled side/qty shape AND the real
    live producer absolute-target shape (signed target_position_pct). Aggregates
    legacy records by symbol; reconstructs absolute-target records via
    latest-target-per-symbol semantics. Mark prices and sector/beta are filled
    in by enrich_positions().
    """
    initial_cash = 100_000.0
    cash = initial_cash
    qty_by_sym: dict[str, float] = defaultdict(float)
    cost_by_sym: dict[str, float] = defaultdict(float)
    last_fill_by_sym: dict[str, float] = {}
    # cs14: latest absolute-target record per symbol (max asof_execution wins).
    abs_latest: dict[str, dict] = {}

    if not EXECUTIONS_PATH.exists():
        return cash, []

    try:
        with open(EXECUTIONS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # ── ABSOLUTE-TARGET path (real live producer shape) ──────────
                if _is_absolute_target_record(rec):
                    # Only admit equity-fill reactors; never silently absorb
                    # crypto / other-class records into the equity review.
                    if rec.get("reactor_name") not in EQUITY_FILL_REACTORS:
                        continue
                    asset = rec.get("asset")
                    if not asset:
                        continue
                    ts = rec.get("asof_execution") or rec.get("asof") or ""
                    prior = abs_latest.get(asset)
                    prior_ts = (
                        (prior.get("asof_execution") or prior.get("asof") or "")
                        if prior is not None
                        else None
                    )
                    if prior is None or ts >= prior_ts:
                        abs_latest[asset] = rec
                    continue

                # ── LEGACY int-1 path (hand-rolled side/qty settlement shape) ─
                try:
                    sym = rec["asset"]
                    side = rec["side"]
                    qty = float(rec["qty"])
                    fill = float(rec["fill_price"])
                    fees = float(rec.get("fees", 0.0))
                except (KeyError, ValueError, TypeError):
                    continue

                signed_qty = qty if side == "buy" else -qty
                notional = signed_qty * fill
                cash -= notional
                cash -= fees
                qty_by_sym[sym] += signed_qty
                cost_by_sym[sym] += notional
                last_fill_by_sym[sym] = fill
    except OSError as e:
        sys.stderr.write(f"executions.jsonl read failed: {e}\n")
        return initial_cash, []

    positions = [
        Position(
            symbol=sym,
            qty=q,
            cost_basis=cost_by_sym[sym],
            last_price=last_fill_by_sym.get(sym, 0.0),
        )
        for sym, q in qty_by_sym.items()
        if abs(q) > 1e-9
    ]

    # cs14: reconstruct one Position per symbol from its LATEST absolute target.
    # Derivation (mirrors portfolio_loader.reconstruct_portfolio absolute path):
    #   * reactor_metadata.quantity (authoritative signed share count) when present
    #   * else qty = target_position_pct * NAV / entry_price (NAV-fraction shares)
    # entry_price = slipped fill_price, else decision_price. The cost-basis cash
    # leg is folded out of cash so NAV = cash + sum(qty*mark) stays coherent.
    for asset, rec in abs_latest.items():
        try:
            target_pct = float(rec.get("target_position_pct"))
        except (TypeError, ValueError):
            continue
        if abs(target_pct) < 1e-12:
            # Latest target is flat -> the position is closed. Drop it.
            continue

        try:
            entry_price = float(rec.get("fill_price"))
        except (TypeError, ValueError):
            entry_price = 0.0
        if entry_price <= 0.0:
            try:
                entry_price = float(rec.get("decision_price"))
            except (TypeError, ValueError):
                entry_price = 0.0
        if entry_price <= 0.0:
            sys.stderr.write(
                f"absolute-target record for {asset} has no usable entry price; "
                f"skipped\n"
            )
            continue

        meta = rec.get("reactor_metadata") or {}
        meta_qty = meta.get("quantity")
        if meta_qty is not None:
            try:
                qty = float(meta_qty)
            except (TypeError, ValueError):
                qty = (target_pct * initial_cash) / entry_price
        else:
            qty = (target_pct * initial_cash) / entry_price

        if abs(qty) < 1e-9:
            continue

        cost = qty * entry_price
        cash -= cost
        positions.append(
            Position(
                symbol=asset,
                qty=qty,
                cost_basis=cost,
                last_price=entry_price,
            )
        )

    return cash, positions


def load_positions_from_file(path: Path) -> tuple[float, list[Position]]:
    """Load positions from a fixed JSON file (for dry-run / mock testing)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    cash = float(data.get("cash", 0.0))
    positions = []
    for p in data.get("positions", []):
        positions.append(Position(
            symbol=p["symbol"],
            qty=float(p["qty"]),
            cost_basis=float(p["cost_basis"]),
            last_price=float(p.get("last_price", 0.0)),
            sector=p.get("sector", "Unknown"),
            beta=float(p.get("beta", 1.0)),
        ))
    return cash, positions


# ---------- yfinance enrichment with disk cache ----------
def _load_cache() -> dict[str, dict]:
    if not SECTOR_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(SECTOR_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache: dict[str, dict]) -> None:
    SECTOR_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        SECTOR_CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError as e:
        sys.stderr.write(f"sector cache write failed: {e}\n")


def enrich_positions(positions: list[Position], use_network: bool = True) -> None:
    """Fill in last_price, sector, beta from yfinance. Cache sector/beta on disk.

    Cache TTL is implicit (one quarter): we only invalidate when the user
    deletes the cache file. yfinance .info is hammered enough that a
    same-quarter re-run reuses cached sector/beta.

    Mutates positions in-place.
    """
    if not use_network or not positions:
        return

    cache = _load_cache()
    cache_dirty = False

    try:
        import yfinance as yf
    except ImportError:
        sys.stderr.write("yfinance not installed; skipping enrichment\n")
        return

    for pos in positions:
        sym = pos.symbol
        try:
            tkr = yf.Ticker(sym)

            # last_price: prefer fast_info if available; else fall back to info
            try:
                fast = tkr.fast_info
                price = float(fast.get("last_price") or fast.get("lastPrice") or 0.0)
            except Exception:
                price = 0.0
            if price <= 0:
                # cached prices age fast — always re-fetch
                try:
                    info = tkr.info
                    price = float(info.get("regularMarketPrice") or info.get("currentPrice") or 0.0)
                except Exception:
                    price = 0.0
            if price > 0:
                pos.last_price = price
            elif pos.last_price <= 0:
                # last resort: use cost basis ÷ qty as approximate mark
                pos.last_price = abs(pos.cost_basis / pos.qty) if pos.qty else 0.0

            # sector + beta: cached
            entry = cache.get(sym, {})
            if "sector" not in entry or "beta" not in entry:
                try:
                    info = tkr.info
                    sector = info.get("sector") or "Unknown"
                    beta_raw = info.get("beta")
                    beta = float(beta_raw) if beta_raw is not None else 1.0
                except Exception:
                    sector, beta = "Unknown", 1.0
                entry = {"sector": sector, "beta": beta, "fetched_at": utcnow_iso()}
                cache[sym] = entry
                cache_dirty = True

            pos.sector = entry.get("sector") or "Unknown"
            pos.beta = float(entry.get("beta") or 1.0)
        except Exception as e:
            sys.stderr.write(f"enrichment failed for {sym}: {e}\n")
            continue

    if cache_dirty:
        _save_cache(cache)


# ---------- metric computation (pure, IO-free, unit-tested) ----------
@dataclass
class PortfolioMetrics:
    nav: float = 0.0
    cash: float = 0.0
    gross_dollar_exposure: float = 0.0   # sum |market_value|
    net_dollar_exposure: float = 0.0     # sum signed market_value
    weighted_beta: float = 0.0           # market_value-weighted beta
    beta_dollar_delta: float = 0.0       # sum (market_value × beta)
    sector_breakdown: dict[str, float] = field(default_factory=dict)  # sector → $
    top_position_weight: float = 0.0     # max position $/NAV
    top_position_symbol: str = ""
    theta_per_day: float = 0.0           # 0 until options support lands
    vega_per_dollar_nav: float = 0.0     # 0 until options support lands
    flags: list[str] = field(default_factory=list)
    rebalance_proposals: list[dict[str, Any]] = field(default_factory=list)


def compute_metrics(cash: float, positions: list[Position]) -> PortfolioMetrics:
    """Compute portfolio metrics from cash + enriched positions. Pure function."""
    m = PortfolioMetrics()
    m.cash = cash

    total_mv = sum(p.market_value for p in positions)
    m.nav = cash + total_mv

    if not positions or m.nav <= 0:
        return m

    m.gross_dollar_exposure = sum(abs(p.market_value) for p in positions)
    m.net_dollar_exposure = total_mv  # signed

    # weighted beta = sum(|mv| × beta) / sum(|mv|)
    beta_num = sum(abs(p.market_value) * p.beta for p in positions)
    if m.gross_dollar_exposure > 0:
        m.weighted_beta = beta_num / m.gross_dollar_exposure
    m.beta_dollar_delta = sum(p.market_value * p.beta for p in positions)

    # sector breakdown (sum signed mv per sector — short positions reduce a sector)
    sectors: dict[str, float] = defaultdict(float)
    for p in positions:
        sectors[p.sector or "Unknown"] += p.market_value
    m.sector_breakdown = dict(sectors)

    # top-1
    if positions:
        top = max(positions, key=lambda p: abs(p.market_value))
        m.top_position_symbol = top.symbol
        m.top_position_weight = abs(top.market_value) / m.nav

    # ---- factor-exposure flags ----
    for sector, mv in m.sector_breakdown.items():
        weight = abs(mv) / m.nav
        if weight > SECTOR_CONCENTRATION_LIMIT:
            m.flags.append(
                f"sector concentration: {sector} = {weight:.1%} (> {SECTOR_CONCENTRATION_LIMIT:.0%})"
            )
            # Rebalance proposal: scale the sector down to limit
            target_mv = SECTOR_CONCENTRATION_LIMIT * m.nav * (1 if mv > 0 else -1)
            reduce_by = mv - target_mv
            m.rebalance_proposals.append({
                "kind": "scale_down_sector",
                "sector": sector,
                "current_weight": weight,
                "target_weight": SECTOR_CONCENTRATION_LIMIT,
                "reduce_dollar": reduce_by,
                "rationale": f"sector exposure {weight:.1%} exceeds {SECTOR_CONCENTRATION_LIMIT:.0%} cap",
            })

    if m.weighted_beta < PORTFOLIO_BETA_LO:
        m.flags.append(
            f"portfolio beta low: {m.weighted_beta:.2f} (< {PORTFOLIO_BETA_LO})"
        )
        m.rebalance_proposals.append({
            "kind": "increase_beta",
            "current_beta": m.weighted_beta,
            "target_beta": (PORTFOLIO_BETA_LO + PORTFOLIO_BETA_HI) / 2,
            "rationale": "portfolio under-exposed to market direction",
        })
    elif m.weighted_beta > PORTFOLIO_BETA_HI:
        m.flags.append(
            f"portfolio beta high: {m.weighted_beta:.2f} (> {PORTFOLIO_BETA_HI})"
        )
        m.rebalance_proposals.append({
            "kind": "reduce_beta",
            "current_beta": m.weighted_beta,
            "target_beta": (PORTFOLIO_BETA_LO + PORTFOLIO_BETA_HI) / 2,
            "rationale": "portfolio over-exposed to market direction",
        })

    if abs(m.net_dollar_exposure) / m.nav > NET_DOLLAR_LIMIT:
        m.flags.append(
            f"net dollar exposure: {m.net_dollar_exposure / m.nav:+.1%} "
            f"(> ±{NET_DOLLAR_LIMIT:.0%})"
        )
        m.rebalance_proposals.append({
            "kind": "neutralize_net_exposure",
            "current_net": m.net_dollar_exposure,
            "current_net_pct_nav": m.net_dollar_exposure / m.nav,
            "target_net_pct_nav": NET_DOLLAR_LIMIT * 0.5,
            "rationale": "directional bias exceeds policy",
        })

    if m.top_position_weight > TOP_POSITION_LIMIT:
        m.flags.append(
            f"top-1 concentration: {m.top_position_symbol} = "
            f"{m.top_position_weight:.1%} (> {TOP_POSITION_LIMIT:.0%})"
        )
        m.rebalance_proposals.append({
            "kind": "trim_top_position",
            "symbol": m.top_position_symbol,
            "current_weight": m.top_position_weight,
            "target_weight": TOP_POSITION_LIMIT,
            "rationale": f"single-name concentration in {m.top_position_symbol}",
        })

    return m


# ---------- markdown report ----------
def render_report(
    qlabel: str,
    cash: float,
    positions: list[Position],
    metrics: PortfolioMetrics,
    *,
    halts: list[dict] | None = None,
) -> str:
    """Render the quarterly review as markdown. Pure (no IO)."""
    now = datetime.now(UTC).astimezone(ET)
    lines: list[str] = []
    lines.append(f"# Quarterly Portfolio Review — {qlabel}")
    lines.append("")
    lines.append(f"_Generated {now.strftime('%Y-%m-%d %H:%M %Z')} (ADR-0035 wave 4, read-mostly)_")
    lines.append("")

    if halts:
        lines.append("## ⛔ HALT STATE ACTIVE")
        for h in halts:
            lines.append(f"- {h.get('reason', '?')} (scope: {h.get('scope', '?')})")
        lines.append("")
        lines.append("Skipping metric computation — clear halt before re-running.")
        return "\n".join(lines) + "\n"

    if metrics.nav <= 0 and not positions:
        lines.append("## Portfolio is empty")
        lines.append("")
        lines.append(f"- Cash: **${cash:,.2f}**")
        lines.append(f"- Positions: 0")
        lines.append("")
        lines.append("No metrics to compute. (First-quarter run before any fills?)")
        return "\n".join(lines) + "\n"

    # -- summary block --
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **NAV**: ${metrics.nav:,.2f}")
    lines.append(f"- **Cash**: ${metrics.cash:,.2f} ({metrics.cash / metrics.nav:.1%} of NAV)")
    lines.append(f"- **Positions**: {len(positions)}")
    lines.append(f"- **Gross dollar exposure**: ${metrics.gross_dollar_exposure:,.2f}")
    lines.append(
        f"- **Net dollar exposure**: ${metrics.net_dollar_exposure:+,.2f} "
        f"({metrics.net_dollar_exposure / metrics.nav:+.1%} of NAV)"
    )
    lines.append(f"- **Weighted beta**: {metrics.weighted_beta:.2f}")
    lines.append(f"- **Beta-weighted $ delta**: ${metrics.beta_dollar_delta:+,.2f}")
    lines.append(f"- **Theta/day**: ${metrics.theta_per_day:,.2f} _(0 until options leg lands)_")
    lines.append(f"- **Vega/$NAV**: {metrics.vega_per_dollar_nav:.4f} _(0 until options leg lands)_")
    lines.append("")

    # -- top-N positions --
    sorted_pos = sorted(positions, key=lambda p: abs(p.market_value), reverse=True)
    top = sorted_pos[:TOP_N_TABLE]
    lines.append(f"## Top {len(top)} Positions (by abs market value)")
    lines.append("")
    lines.append("| Symbol | Sector | Qty | Mark | Mkt Val | % NAV | β | UPnL | UPnL % |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for p in top:
        lines.append(
            f"| {p.symbol} | {p.sector} | {p.qty:,.4g} | "
            f"${p.last_price:,.2f} | ${p.market_value:,.0f} | "
            f"{p.market_value / metrics.nav:.1%} | {p.beta:.2f} | "
            f"${p.unrealized_pnl:+,.0f} | {p.unrealized_pnl_pct:+.1%} |"
        )
    lines.append("")

    # -- sector breakdown --
    lines.append("## Sector Breakdown")
    lines.append("")
    lines.append("| Sector | $ Exposure | % NAV |")
    lines.append("|---|---:|---:|")
    for sector, mv in sorted(metrics.sector_breakdown.items(), key=lambda kv: -abs(kv[1])):
        lines.append(f"| {sector} | ${mv:+,.0f} | {mv / metrics.nav:+.1%} |")
    lines.append("")

    # -- flags --
    lines.append("## Factor-Exposure Flags")
    lines.append("")
    if not metrics.flags:
        lines.append("✅ No flags. Portfolio within policy on all four factors:")
        lines.append(f"- sector concentration ≤ {SECTOR_CONCENTRATION_LIMIT:.0%}")
        lines.append(f"- {PORTFOLIO_BETA_LO} ≤ portfolio β ≤ {PORTFOLIO_BETA_HI}")
        lines.append(f"- |net dollar exposure| ≤ {NET_DOLLAR_LIMIT:.0%} of NAV")
        lines.append(f"- top-1 position ≤ {TOP_POSITION_LIMIT:.0%} of NAV")
    else:
        for f in metrics.flags:
            lines.append(f"- ⚠️ {f}")
    lines.append("")

    # -- rebalance proposals (LOG ONLY) --
    lines.append("## Recommended Actions (LOG ONLY — manual confirmation required)")
    lines.append("")
    if not metrics.rebalance_proposals:
        lines.append("_No rebalance actions proposed this quarter._")
    else:
        lines.append(
            "These proposals are logged to "
            f"`{REBALANCE_LOG_PATH}` for review. "
            "Per ADR-0035, the quarterly review **does not auto-fire** rebalances; "
            "the user reviews and approves any executions through the HITL queue."
        )
        lines.append("")
        for i, prop in enumerate(metrics.rebalance_proposals, 1):
            kind = prop.get("kind", "?")
            rationale = prop.get("rationale", "")
            lines.append(f"{i}. **{kind}** — {rationale}")
            for k, v in prop.items():
                if k in ("kind", "rationale"):
                    continue
                if isinstance(v, float):
                    if "weight" in k or "pct" in k:
                        lines.append(f"   - {k}: {v:.1%}")
                    elif "dollar" in k or k in ("current_net", "reduce_dollar"):
                        lines.append(f"   - {k}: ${v:+,.0f}")
                    else:
                        lines.append(f"   - {k}: {v:.4f}")
                else:
                    lines.append(f"   - {k}: {v}")
    lines.append("")
    lines.append("---")
    lines.append("_Next quarterly review: first Monday of next quarter at 06:30 PT._")

    return "\n".join(lines) + "\n"


# ---------- main ----------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="use mock portfolio file")
    ap.add_argument("--portfolio-file", type=Path, help="JSON portfolio file")
    ap.add_argument("--write", action="store_true",
                    help="write the markdown report file (always on outside dry-run)")
    ap.add_argument("--no-network", action="store_true",
                    help="skip yfinance enrichment (offline / test mode)")
    ap.add_argument("--force", action="store_true",
                    help="skip the first-Monday-of-quarter date guard")
    args = ap.parse_args()

    qlabel = quarter_label()

    # Date-guard: cron's POSIX OR-semantics on dom/dow means our cron line
    # ('30 6 1-7 1,4,7,10 1') may fire on EVERY Monday of those months OR
    # EVERY day 1-7 of those months. The script must self-gate to the
    # actual first Monday of the quarter. Dry-run / --force bypass this.
    if not args.dry_run and not args.force and not is_first_monday_of_quarter():
        # Silent exit — cron treats empty stdout as "nothing to deliver".
        sys.stderr.write(
            f"quarterly review skipped: not the first Monday of a quarter "
            f"(today={datetime.now(UTC).astimezone(ET).strftime('%Y-%m-%d %a')})\n"
        )
        return 0

    halts = read_active_halts()

    # Load positions
    if args.portfolio_file:
        cash, positions = load_positions_from_file(args.portfolio_file)
        use_network = not args.no_network
    elif args.dry_run:
        # Synthesize a tiny mock so the script always produces output.
        cash = 50_000.0
        positions = []
        use_network = False
    else:
        cash, positions = load_positions_from_executions()
        use_network = not args.no_network

    # Enrich (mark prices, sector, beta) — only if positions came from journal
    # and a portfolio-file did not pre-populate sector/beta.
    needs_enrichment = use_network and any(
        not p.sector or p.sector == "Unknown" or p.last_price <= 0 for p in positions
    )
    if needs_enrichment:
        enrich_positions(positions, use_network=True)

    # Compute metrics
    metrics = compute_metrics(cash, positions)

    # Log rebalance proposals (durable record), even if quarterly cadence is
    # informational. The user's HITL queue can pick these up later.
    for prop in metrics.rebalance_proposals:
        rec = {
            "quarter": qlabel,
            "proposal": prop,
            "nav": metrics.nav,
            "scope": "quarterly_review",
        }
        append_rebalance_log(rec)

    # Render report
    md = render_report(qlabel, cash, positions, metrics, halts=halts)

    # Persist
    should_write = (not args.dry_run) or args.write
    if should_write and not halts:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = REPORT_DIR / f"{qlabel}.md"
        out_path.write_text(md, encoding="utf-8")
        # Tag stderr for the cron operator audit; stdout is the Discord payload.
        sys.stderr.write(f"quarterly report written to {out_path}\n")

    # Print to stdout — this becomes the Discord message in no_agent cron mode.
    sys.stdout.write(md)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        # On exception, still emit something so the cron has signal.
        sys.stderr.write(f"quarterly review FAILED: {exc}\n")
        traceback.print_exc(file=sys.stderr)
        sys.stdout.write(
            f"# Quarterly Portfolio Review — FAILED\n\n"
            f"Exception: `{type(exc).__name__}: {exc}`\n\n"
            f"See cron stderr for traceback.\n"
        )
        sys.exit(1)
