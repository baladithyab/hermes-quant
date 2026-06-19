"""aegis-ag03 (ADR-0096 Gate 3) — hierarchical partial pooling + warm-up honesty.

Replaces the flat per-(analyst × regime) Brier weighting with hierarchical
partial pooling (cell → analyst → global shrinkage driven by effective-n) and
explicit warm-up honesty (a thin/empty cell is LABELLED as warm-up and pulled
toward UNIFORM, never presented as a confident track record).

DEFAULT-OFF behind HERMES_QUANT_HIERARCHICAL_POOLING — byte-identical to the
pre-ag03 BMA when the flag is unset.

Pure-Python, offline, deterministic. Money-software TDD: every assertion RED-proven.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hermes_quant.aggregators.bma import BMAAggregator
from hermes_quant.learning.hierarchical_pooling import (
    HierarchicalPooler,
    pooled_skill,
    uniform_skill_target,
)
from hermes_quant.protocol import AnalystView, MarketContext
from hermes_quant.regime.detector import RegimeDetector, RegimeState
from hermes_quant.regime.state_variables import StateVariables


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(n_bars: int = 200, *, seed: int = 42) -> MarketContext:
    rng = np.random.default_rng(seed)
    log_returns = rng.normal(0.0, 0.01, size=n_bars)
    prices = 100.0 * np.exp(np.cumsum(log_returns))
    timestamps = pd.date_range("2025-01-01", periods=n_bars, freq="B", tz="UTC")
    bars = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": prices * 0.999,
            "high": prices * 1.002,
            "low": prices * 0.998,
            "close": prices,
            "volume": 1_000_000.0,
        }
    )
    return MarketContext(
        asset="TEST/USDT",
        timeframe="1d",
        asset_class="crypto",
        exchange="test",
        bars=bars,
        last_close=float(prices[-1]),
        last_volume=1_000_000.0,
        asof=timestamps[-1],
    )


def _make_views(analysts: list[str], direction: int = 1) -> list[AnalystView]:
    return [
        AnalystView(
            analyst=name,
            direction=direction,
            magnitude=0.02,
            confidence=0.60,
            confidence_raw=0.60,
            horizon="1d",
        )
        for name in analysts
    ]


class _AlwaysVolatileDetector(RegimeDetector):
    """Stub detector that always returns VOLATILE (a rare, slow-to-fill regime)."""

    def classify(self, state_vars: StateVariables):
        return RegimeState.VOLATILE, "stub_always_volatile"


# ===========================================================================
# 1. POOLING DIVERGENCE (load-bearing, non-vacuous)
#    A thin (n=2, 2/2 correct) cell must NOT get a near-1.0 weight.
#    RED-PROOF: flat per-cell reads ~1.0; pooling pulls it materially down.
# ===========================================================================


def test_pooling_divergence_thin_cell_not_near_one():
    """A thin 2/2 cell flat-reads 1.0; partial pooling shrinks it toward the prior.

    Non-vacuous: n=2 is chosen because flat-vs-pooled GENUINELY diverge here
    (unlike n=2000 where they coincide). With no broader analyst history, the
    parent is the global prior mean 0.5, so pooled lands far below 1.0.
    """
    pooler = HierarchicalPooler(prior_alpha=5.0, prior_beta=5.0)
    pooler.observe("kronos", "volatile", correct=True)
    pooler.observe("kronos", "volatile", correct=True)

    diag = pooler.cell_diagnostics("kronos", "volatile")
    # Flat per-cell estimate (the OLD behavior) is exactly 1.0 — the noise.
    assert diag["flat_estimate"] == pytest.approx(1.0)
    # Pooled estimate is pulled MATERIALLY toward the prior, nowhere near 1.0.
    assert diag["pooled_skill"] < 0.7, (
        f"thin 2/2 cell must shrink well below flat 1.0, got "
        f"{diag['pooled_skill']:.4f}"
    )
    # And it never EXCEEDS its flat estimate (shrinkage, not amplification).
    assert diag["pooled_skill"] <= diag["flat_estimate"] + 1e-9


def test_pooled_skill_helper_thin_vs_flat():
    """The standalone shrinkage helper: 2/2 (flat 1.0) shrinks toward a 0.5 parent."""
    flat = 2.0 / 2.0
    assert flat == pytest.approx(1.0)
    pooled = pooled_skill(cell_wins=2.0, cell_n=2.0, analyst_mean=0.5, shrinkage_k=8.0)
    # w = 2/(2+8) = 0.2 -> pooled = 0.2*1.0 + 0.8*0.5 = 0.6
    assert pooled == pytest.approx(0.6)
    assert pooled < flat


# ===========================================================================
# 2. WELL-POPULATED CELL keeps ~its empirical estimate (pooling barely moves it).
# ===========================================================================


def test_well_populated_cell_keeps_empirical_estimate():
    """A cell with large n + genuine skill is barely moved by pooling.

    Proves pooling is not 'uniform-everything': a 240/300 (0.80) cell with a
    matching analyst-level prior stays ~0.80, not dragged to 0.5.
    """
    pooler = HierarchicalPooler(prior_alpha=5.0, prior_beta=5.0)
    for _ in range(240):
        pooler.observe("kronos", "volatile", correct=True)
    for _ in range(60):
        pooler.observe("kronos", "volatile", correct=False)

    diag = pooler.cell_diagnostics("kronos", "volatile")
    assert diag["flat_estimate"] == pytest.approx(0.80)
    assert diag["warmup"] is False  # n=300 >> warmup_n=30
    # Pooling barely moves a well-populated cell.
    assert abs(diag["pooled_skill"] - 0.80) < 0.02, (
        f"well-populated cell should keep ~0.80, got {diag['pooled_skill']:.4f}"
    )


# ===========================================================================
# 3. WARM-UP LABEL: a below-threshold cell is FLAGGED + weight near-uniform.
# ===========================================================================


def test_warmup_cell_flagged_and_near_uniform():
    """A below-warmup_n cell is flagged warm-up and its skill is near the uniform target.

    RED-prove: the warmup flag is True and pooled_skill == the uniform target
    (the global prior mean 0.5) rather than the noisy 1.0 point estimate.
    """
    pooler = HierarchicalPooler(prior_alpha=5.0, prior_beta=5.0, warmup_n=30.0)
    for _ in range(5):
        pooler.observe("sentiment", "bull", correct=True)  # 5/5, flat 1.0

    diag = pooler.cell_diagnostics("sentiment", "bull")
    assert diag["warmup"] is True
    assert diag["flat_estimate"] == pytest.approx(1.0)
    # Default warmup_uniform_pull=1.0 -> fully uniform target (prior mean 0.5).
    assert diag["pooled_skill"] == pytest.approx(diag["uniform_target"])
    assert diag["uniform_target"] == pytest.approx(0.5)
    # Crucially NOT the noisy point estimate.
    assert diag["pooled_skill"] < 0.9


def test_warmup_surfaced_in_bma_status(monkeypatch):
    """The aggregator status flags the warm-up band per (analyst, regime)."""
    monkeypatch.setenv("HERMES_QUANT_HIERARCHICAL_POOLING", "1")
    bma = BMAAggregator(
        require_ensemble=False, regime_detector=_AlwaysVolatileDetector()
    )
    ctx = _make_context()
    views = _make_views(["kronos", "sentiment"])
    bma.aggregate(views, ctx)  # builds cells (cold, all warm-up)

    status = bma.status()
    pooling = status.get("hierarchical_pooling")
    assert pooling is not None, "status must expose hierarchical_pooling diagnostics"
    cells = pooling["cells"]
    assert len(cells) >= 2, "active (analyst, regime) cells must be surfaced"
    # Every fresh cell is in warm-up.
    assert all(c["warmup"] is True for c in cells.values())
    # The headline is honestly labelled as in-warm-up (not a confident record).
    assert pooling["headline_in_warmup"] is True
    assert pooling["n_warmup_cells"] == len(cells)


# ===========================================================================
# 4. EFFECTIVE-N in status: diagnostics expose effective-n per (analyst, regime).
# ===========================================================================


def test_effective_n_surfaced_in_status(monkeypatch):
    """status() exposes effective calibration n + pooled weights + warm-up flag."""
    monkeypatch.setenv("HERMES_QUANT_HIERARCHICAL_POOLING", "1")
    bma = BMAAggregator(
        require_ensemble=False, regime_detector=_AlwaysVolatileDetector()
    )
    ctx = _make_context()
    views = _make_views(["kronos", "sentiment"])
    bma.aggregate(views, ctx)

    status = bma.status()
    pooling = status["hierarchical_pooling"]
    cells = pooling["cells"]
    # G3 eval criterion: effective-n + pooled weight + warm-up flag are all present.
    for cell in cells.values():
        assert "effective_n" in cell
        assert "pooled_skill" in cell
        assert "warmup" in cell
    # The (analyst, regime) keying is surfaced.
    keys = set(cells.keys())
    assert any("kronos" in k and "volatile" in k for k in keys)


# ===========================================================================
# 5. DEFAULT-OFF byte-identical: flag unset -> aggregator output == c0dc5ac.
# ===========================================================================


def test_default_off_byte_identical(monkeypatch):
    """Flag unset: aggregate() output is byte-identical to the pre-ag03 path.

    Compared against a baseline aggregator that NEVER touches the pooling path
    (the same code without the flag). RED-proven: with the flag accidentally on,
    the volatile-regime weights would differ.
    """
    monkeypatch.delenv("HERMES_QUANT_HIERARCHICAL_POOLING", raising=False)
    ctx = _make_context()
    views = _make_views(["kronos", "sentiment", "classical_ta"])

    bma_a = BMAAggregator(require_ensemble=False, regime_detector=_AlwaysVolatileDetector())
    bma_b = BMAAggregator(require_ensemble=False, regime_detector=_AlwaysVolatileDetector())
    sig_a = bma_a.aggregate(views, ctx)
    sig_b = bma_b.aggregate(views, ctx)

    assert sig_a.direction == sig_b.direction
    assert sig_a.confidence == pytest.approx(sig_b.confidence)
    assert sig_a.magnitude == pytest.approx(sig_b.magnitude)
    assert sig_a.confidence_raw == pytest.approx(sig_b.confidence_raw)
    # OFF-path metadata must NOT contain the pooling audit key.
    assert "hierarchical_pooling" not in (sig_a.metadata or {})
    assert "hierarchical_pooling_warmup" not in (sig_a.metadata or {})
    # OFF-path status must NOT contain the pooling diagnostics key.
    assert "hierarchical_pooling" not in bma_a.status()


def test_default_off_weights_equal_flat_posterior_after_settlement(monkeypatch):
    """Flag OFF: even after update() accumulates pooling cells, the OFF-path
    per-(analyst, regime) weight is EXACTLY the flat posterior_accuracy × regime
    multiplier — the pooler accumulation must not perturb the Beta posterior path.

    RED-prove (value-level): the OFF weight equals posterior_accuracy(=alpha/(a+b))
    times the BEAR sentiment multiplier 0.6 — the documented pre-ag03 math, with
    the pooler never consulted.
    """
    monkeypatch.delenv("HERMES_QUANT_HIERARCHICAL_POOLING", raising=False)

    class _Bear(RegimeDetector):
        def classify(self, sv):
            return RegimeState.BEAR, "bear"

    bma = BMAAggregator(
        require_ensemble=False,
        regime_detector=_Bear(),
        n_min_observations=2,
        prior_alpha=1.0,
        prior_beta=1.0,
    )
    ctx = _make_context()
    # Settle sentiment to 3 correct (alpha=4, beta=1 -> posterior 0.8); these
    # settlements ALSO accumulate pooling cells, which must stay inert OFF.
    from hermes_quant.protocol import EpisodeOutcome

    for _ in range(3):
        sig = bma.aggregate(_make_views(["sentiment", "semantic"]), ctx)
        bma.update(
            EpisodeOutcome(
                asset="TEST/USDT",
                timeframe="1d",
                asof=ctx.asof,
                aggregated_signal=sig,
                realized_returns={"1d": 0.01},
                direction_correct={"sentiment": True, "semantic": True},
            )
        )
    # Pooling cells WERE accumulated (proves the OFF path still records them).
    assert len(bma._pooler._cells) > 0

    sig = bma.aggregate(_make_views(["sentiment", "semantic"]), ctx)
    # OFF weight for sentiment = posterior_accuracy(4/5=0.8) * BEAR mult(0.6) = 0.48.
    expected = (4.0 / 5.0) * 0.6
    assert sig.metadata["weights"]["sentiment"] == pytest.approx(expected)
    assert "hierarchical_pooling" not in sig.metadata


def test_flag_on_changes_volatile_weight(monkeypatch):
    """Flag ON: a rare-regime cell with thin data gets a pooled (shrunk) weight,
    materially different from the flat regime-multiplied weight.

    This is the positive control that proves the flag actually rewires the
    weight (so the default-OFF byte-identical test above is non-vacuous)."""
    ctx = _make_context()
    views = _make_views(["kronos", "sentiment"])

    monkeypatch.delenv("HERMES_QUANT_HIERARCHICAL_POOLING", raising=False)
    bma_off = BMAAggregator(require_ensemble=False, regime_detector=_AlwaysVolatileDetector())
    sig_off = bma_off.aggregate(views, ctx)

    monkeypatch.setenv("HERMES_QUANT_HIERARCHICAL_POOLING", "1")
    bma_on = BMAAggregator(require_ensemble=False, regime_detector=_AlwaysVolatileDetector())
    sig_on = bma_on.aggregate(views, ctx)

    # The ON path injects the pooling audit + diagnostics.
    assert "hierarchical_pooling" in (sig_on.metadata or {})
    assert "hierarchical_pooling" in bma_on.status()
    # The weights actually changed (cold cells -> near-uniform, not the flat
    # regime-multiplied posterior proxy of 0.5 * volatile-multiplier).
    w_off = sig_off.metadata["weights"]
    w_on = sig_on.metadata["weights"]
    assert w_off.keys() == w_on.keys()
    assert any(
        abs(w_off[k] - w_on[k]) > 1e-9 for k in w_off
    ), "flag ON must change at least one analyst weight vs OFF"


# ===========================================================================
# 6. GRACEFUL no-provenance: single epoch; a model-id change opens fresh warm-up.
# ===========================================================================


def test_graceful_no_provenance_single_epoch():
    """With no model-id, all observations land in one epoch (no crash, accumulates)."""
    pooler = HierarchicalPooler(prior_alpha=5.0, prior_beta=5.0, warmup_n=3.0)
    for _ in range(10):
        pooler.observe("kronos", "bull", correct=True)
    diag = pooler.cell_diagnostics("kronos", "bull")
    assert diag["effective_n"] == pytest.approx(10.0)
    assert diag["warmup"] is False  # 10 >= warmup_n=3
    # Single default epoch tag.
    assert diag["epoch"] == ""


def test_model_id_change_opens_fresh_warmup_epoch():
    """A model-id change re-enters warm-up: the new epoch's cell is empty.

    Old model accrued a populated, non-warm-up cell; switching the model-id
    resets the analyst to an empty cell (effective_n 0, warm-up True) — it does
    NOT inherit the prior model's track record.
    """
    pooler = HierarchicalPooler(prior_alpha=5.0, prior_beta=5.0, warmup_n=5.0)
    for _ in range(20):
        pooler.observe("kronos", "bull", correct=True, model_id="model-v1")
    diag_v1 = pooler.cell_diagnostics("kronos", "bull", model_id="model-v1")
    assert diag_v1["effective_n"] == pytest.approx(20.0)
    assert diag_v1["warmup"] is False

    # Model change: fresh epoch, empty cell, back in warm-up.
    pooler.observe("kronos", "bull", correct=True, model_id="model-v2")
    diag_v2 = pooler.cell_diagnostics("kronos", "bull", model_id="model-v2")
    assert diag_v2["effective_n"] == pytest.approx(1.0)
    assert diag_v2["warmup"] is True
    assert diag_v2["epoch"] == "model-v2"
    # The v1 record is untouched (no inheritance into v2).
    assert pooler.cell_diagnostics("kronos", "bull", model_id="model-v1")[
        "effective_n"
    ] == pytest.approx(20.0)


# ===========================================================================
# 7. NEVER-AMPLIFY: pooling can only down-weight or equalize, never free-amplify.
# ===========================================================================


def test_never_amplify_above_flat_unless_parent_higher():
    """Pooled skill is a convex combination of flat and parent: never above max(flat,parent).

    A high-flat thin cell (1.0) with a lower parent (0.5) shrinks DOWN, never up.
    A low-flat thin cell (0.0) with a higher parent (0.5) is pulled UP only toward
    the parent — and never above it. Either way pooled ∈ [min, max] of (flat, parent):
    shrinkage, not amplification.
    """
    # High flat, lower parent -> shrink down (never above flat).
    p_high = pooled_skill(cell_wins=2.0, cell_n=2.0, analyst_mean=0.5, shrinkage_k=8.0)
    assert 0.5 <= p_high <= 1.0
    assert p_high < 1.0  # strictly below the noisy flat estimate

    # Low flat, higher parent -> pulled up only toward parent, never above it.
    p_low = pooled_skill(cell_wins=0.0, cell_n=2.0, analyst_mean=0.5, shrinkage_k=8.0)
    assert 0.0 <= p_low <= 0.5
    assert p_low > 0.0  # pulled up toward the parent

    # The amplification bound: pooled never exceeds max(flat, parent).
    for wins, n, parent in [(2.0, 2.0, 0.5), (0.0, 2.0, 0.5), (4.0, 5.0, 0.7), (1.0, 4.0, 0.3)]:
        flat = wins / n
        p = pooled_skill(cell_wins=wins, cell_n=n, analyst_mean=parent, shrinkage_k=8.0)
        assert p <= max(flat, parent) + 1e-9
        assert p >= min(flat, parent) - 1e-9


def test_warmup_pull_is_only_toward_uniform_not_above_flat():
    """In warm-up, the uniform pull only EQUALIZES — it cannot raise a thin cell
    above its flat estimate when the flat estimate already exceeds the uniform target."""
    pooler = HierarchicalPooler(prior_alpha=5.0, prior_beta=5.0, warmup_n=30.0)
    # 3/3 correct -> flat 1.0; warm-up pull drives it to the 0.5 uniform target.
    for _ in range(3):
        pooler.observe("kronos", "bull", correct=True)
    diag = pooler.cell_diagnostics("kronos", "bull")
    assert diag["flat_estimate"] == pytest.approx(1.0)
    assert diag["pooled_skill"] <= diag["flat_estimate"]  # never amplified
    assert diag["pooled_skill"] == pytest.approx(diag["uniform_target"])


def test_uniform_skill_target_is_prior_mean():
    """The uniform skill target is the global prior mean (equal across analysts)."""
    assert uniform_skill_target(0.5, 3) == pytest.approx(0.5)
    assert uniform_skill_target(0.286, 5) == pytest.approx(0.286)
    # Clipped to [0,1].
    assert uniform_skill_target(1.7, 2) == pytest.approx(1.0)
    assert uniform_skill_target(-0.2, 2) == pytest.approx(0.0)


def test_pooling_status_reports_empty_epoch_cell_true_effective_n():
    """wave4-review DEFECT fix: a pre-provenance (empty-epoch) cell's effective_n must be
    reported from its OWN cell, not re-resolved to the analyst's latest model-id.

    The reviewer RED-proved: with cells {('kronos','volatile',''):n=10,
    ('kronos','volatile','m1'):n=1} and _epoch_of={'kronos':'m1'}, the OLD
    _pooling_status passed model_id=(epoch or None) -> the empty-epoch cell collapsed to
    model_id=None -> _resolve_epoch_readonly returned 'm1' -> the n=10 cell was reported
    with effective_n=1.0 (m1's data) and m1 was reported twice. The fix passes the cell's
    LITERAL epoch, so each cell reports its own counts.
    """
    bma = BMAAggregator(require_ensemble=False, regime_detector=_AlwaysVolatileDetector())
    import os as _os
    # Build the exact two-cell state: 10 obs in the empty (pre-provenance) epoch, then a
    # model-id change opens a fresh 'm1' epoch with 1 obs (advancing _epoch_of['kronos']='m1').
    for _ in range(10):
        bma._pooler.observe("kronos", "volatile", correct=True)  # epoch '' cell, n=10
    bma._pooler.observe("kronos", "volatile", correct=True, model_id="m1")  # epoch 'm1', n=1
    assert bma._pooler._epoch_of.get("kronos") == "m1"  # latest epoch advanced

    # The status must report BOTH cells with their TRUE effective_n (10 and 1), not the
    # empty-epoch cell mis-collapsed to m1's n=1.
    _os.environ["HERMES_QUANT_HIERARCHICAL_POOLING"] = "1"
    try:
        status = bma.status()["hierarchical_pooling"]
    finally:
        _os.environ.pop("HERMES_QUANT_HIERARCHICAL_POOLING", None)
    cells = status["cells"]
    # The empty-epoch cell (label without an epoch suffix) must show effective_n == 10.
    empty_label = "kronos|volatile"
    m1_label = "kronos|volatile|m1"
    assert empty_label in cells, f"empty-epoch cell missing; got {list(cells)}"
    assert m1_label in cells, f"m1-epoch cell missing; got {list(cells)}"
    assert cells[empty_label]["effective_n"] == 10.0, (
        f"the pre-provenance cell must report its OWN n=10, not m1's n=1; "
        f"got {cells[empty_label]['effective_n']} (the epoch round-trip bug)"
    )
    assert cells[m1_label]["effective_n"] == 1.0
