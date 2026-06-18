"""aegis-ag03 — hierarchical partial pooling + warm-up honesty for (analyst, regime) skill.

ADR-0096 Gate 3. THE PROBLEM this module fixes:

The BMA weight is ``confidence × Brier-skill × regime``. The per-(analyst × regime)
skill estimate is, under forward-only interday settlement, populated cell-by-cell
over MONTHS — and rare regimes (VOLATILE / BEAR_WEAK) may never fill. A flat
per-cell point estimate therefore reads a thin ``2/2-correct`` cell as a confident
~1.0 skill, silently presenting noise as a track record. There is no honesty signal
that the headline weighting mechanism is statistically meaningless during the long
warm-up.

This module implements **hierarchical partial pooling** (empirical-Bayes /
James–Stein-style shrinkage) of the (analyst, regime) skill estimate:

    cell estimate  →  analyst-level estimate  →  global prior

The shrinkage intensity is driven by the cell's *effective sample count* ``n``:

  * thin / empty cell (small n) → shrink HARD toward the analyst-then-global level;
  * well-populated cell (large n) → trust the cell's own empirical estimate.

and **warm-up honesty**: while a cell's effective-n is below ``warmup_n``, the cell
is in WARM-UP — its weight is pulled toward UNIFORM (near-equal across analysts)
rather than toward a noisy point estimate, and the warm-up band is LABELLED (not
silent) so a status/diagnostics consumer can see it.

Design constraints (money-software posture):
  * Pure-Python, deterministic, offline. No new heavy dependency.
  * NEVER-AMPLIFY: pooling can only down-weight or equalize a cell toward a prior;
    it never pushes a cell above its own flat empirical estimate unless the prior it
    shrinks toward is itself higher (a genuine higher-skill prior). It behaves like a
    shrinkage estimator, not an amplifier (see :func:`pooled_skill`).
  * GRACEFUL EPOCH: cells key on an OPTIONAL ``epoch`` (a model-id / provenance tag).
    With no epoch the whole history is one epoch; a model change opens a fresh epoch
    so the analyst re-enters warm-up rather than inheriting a prior model's record.

The shrinkage weight uses the standard reliability form

    w(n) = n / (n + k)

where ``k`` (``shrinkage_k``) is the pseudo-count strength of the parent level. This
is the cold-start 0.20-shrinkage idiom generalized: at ``n = 0`` the cell is the
parent exactly; as ``n → ∞`` it is the cell exactly. Reusing the cold-start
intuition, the default ``shrinkage_k`` is chosen so a brand-new cell starts fully
pooled and only earns autonomy as evidence accrues.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Default pseudo-count strength of the PARENT level in the cell→parent shrinkage
# reliability weight w(n) = n / (n + k). With k = 8, a cell needs ~8 effective
# observations to be pooled halfway between the parent and its own estimate, and
# ~24 to sit 75% on its own estimate. Mirrors the cold-start "treat thin data
# skeptically" idiom (ColdStartCalibrator's Beta(2,5) total pseudo-count = 7).
DEFAULT_SHRINKAGE_K = 8.0

# A cell with fewer than this many EFFECTIVE observations is in the WARM-UP band:
# its weight is pulled toward UNIFORM (near-equal across analysts) rather than
# toward a noisy point estimate, and it is LABELLED as warm-up in diagnostics.
# 30 mirrors BMAAggregator.n_min_observations (the existing "below this, use
# uniform weights — avoid noisy posteriors" threshold).
DEFAULT_WARMUP_N = 30.0

# Within the warm-up band, how strongly the cell's weight is pulled toward the
# uniform target. 1.0 = fully uniform while warming up; 0.0 = no uniform pull
# (pure parent-shrinkage). Default fully uniform: an unproven cell contributes
# the honest "no differential evidence" weight, not a thin point estimate.
DEFAULT_WARMUP_UNIFORM_PULL = 1.0


@dataclass
class PoolingCell:
    """A (analyst, regime, epoch) Beta-binomial correctness cell.

    ``alpha`` / ``beta`` accumulate directional-correctness counts (alpha = wins,
    beta = losses). ``epoch`` is the model-id / provenance tag the cell belongs
    to; a model change bumps the epoch so a fresh cell is opened (the analyst
    re-enters warm-up).
    """

    analyst: str
    regime: str
    epoch: str = ""
    alpha: float = 0.0
    beta: float = 0.0

    @property
    def n(self) -> float:
        """Effective observation count for this cell (wins + losses, no prior)."""
        return float(self.alpha + self.beta)

    @property
    def empirical_accuracy(self) -> float | None:
        """Flat per-cell empirical directional accuracy, or None when n == 0.

        This is the *flat per-cell Brier-skill* the pooling replaces. A thin
        ``2/2`` cell returns 1.0 here — which is exactly the noise the headline
        weighting must NOT present as a confident track record.
        """
        if self.n <= 0.0:
            return None
        return float(self.alpha / self.n)


@dataclass
class HierarchicalPooler:
    """Empirical-Bayes partial pooling of (analyst, regime) skill cells.

    Maintains per-(analyst, regime, epoch) correctness cells and produces a
    pooled skill estimate per cell that shrinks cell → analyst → global. Also
    surfaces effective-n and a warm-up flag per cell for honest diagnostics.

    All accumulation is additive and cheap; this object is consulted ONLY when
    the HERMES_QUANT_HIERARCHICAL_POOLING flag is set (the flag gate lives in the
    BMA aggregator), so its mere presence never changes the default-OFF path.
    """

    prior_alpha: float = 5.0
    prior_beta: float = 5.0
    shrinkage_k: float = DEFAULT_SHRINKAGE_K
    warmup_n: float = DEFAULT_WARMUP_N
    warmup_uniform_pull: float = DEFAULT_WARMUP_UNIFORM_PULL
    # keyed by (analyst, regime, epoch)
    _cells: dict[tuple[str, str, str], PoolingCell] = field(default_factory=dict)
    # the epoch currently active per analyst (advances on a model-id change)
    _epoch_of: dict[str, str] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Accumulation
    # ------------------------------------------------------------------

    @property
    def _global_prior_mean(self) -> float:
        return float(self.prior_alpha / (self.prior_alpha + self.prior_beta))

    def _epoch_for(self, analyst: str, model_id: str | None) -> str:
        """Resolve the active epoch for an analyst, opening a fresh one on model change.

        With ``model_id is None`` the analyst stays in a single default epoch (the
        graceful no-provenance path). When a model-id is supplied that differs
        from the analyst's currently-active one, the epoch advances — the analyst
        re-enters warm-up against a fresh cell rather than inheriting the prior
        model's track record.
        """
        if model_id is None:
            return self._epoch_of.get(analyst, "")
        prev = self._epoch_of.get(analyst)
        if prev != model_id:
            self._epoch_of[analyst] = model_id
        return model_id

    def touch_cell(
        self,
        analyst: str,
        regime: str,
        *,
        model_id: str | None = None,
    ) -> None:
        """Register a cell (effective_n stays 0) so diagnostics surface it as cold.

        Used by the aggregator at DECISION time so that an analyst active in a
        regime that has not settled yet still appears in status as a cold,
        warm-up cell — honest "this (analyst, regime) is unproven" rather than
        silently absent. Idempotent: never adds observations, never advances an
        existing cell's counts. It DOES resolve/advance the epoch (a fresh
        model-id opens a fresh cold cell), mirroring observe().
        """
        epoch = self._epoch_for(analyst, model_id)
        key = (analyst, regime, epoch)
        if key not in self._cells:
            self._cells[key] = PoolingCell(analyst=analyst, regime=regime, epoch=epoch)

    def observe(
        self,
        analyst: str,
        regime: str,
        correct: bool,
        *,
        model_id: str | None = None,
    ) -> None:
        """Fold one settled directional-correctness sample into the cell.

        ``model_id`` is an optional provenance / deliberation tag. When present
        and different from the analyst's active epoch, a fresh epoch opens (see
        :meth:`_epoch_for`). When absent the single default epoch is used.
        """
        epoch = self._epoch_for(analyst, model_id)
        key = (analyst, regime, epoch)
        cell = self._cells.get(key)
        if cell is None:
            cell = PoolingCell(analyst=analyst, regime=regime, epoch=epoch)
            self._cells[key] = cell
        if correct:
            cell.alpha += 1.0
        else:
            cell.beta += 1.0

    # ------------------------------------------------------------------
    # Pooled estimates
    # ------------------------------------------------------------------

    def _analyst_level_estimate(self, analyst: str, epoch: str) -> tuple[float, float]:
        """Pool every (regime) cell for one analyst-epoch into an analyst-level
        skill estimate, itself shrunk toward the global prior by total n.

        Returns ``(analyst_mean, analyst_n)`` where ``analyst_mean`` is the
        analyst's across-regime directional accuracy shrunk toward the global
        prior mean, and ``analyst_n`` is the analyst's total effective count in
        this epoch.
        """
        wins = 0.0
        n = 0.0
        for (a, _r, e), cell in self._cells.items():
            if a == analyst and e == epoch:
                wins += cell.alpha
                n += cell.n
        prior_mean = self._global_prior_mean
        if n <= 0.0:
            return prior_mean, 0.0
        raw = wins / n
        # Shrink the analyst-level estimate toward the global prior by its own n.
        w = n / (n + self.shrinkage_k)
        analyst_mean = w * raw + (1.0 - w) * prior_mean
        return float(analyst_mean), float(n)

    def cell_diagnostics(
        self,
        analyst: str,
        regime: str,
        *,
        model_id: str | None = None,
        epoch: str | None = None,
        n_active_analysts: int = 1,
    ) -> dict:
        """Full pooled diagnostics for one (analyst, regime) cell.

        Returns a dict with:
          ``effective_n``           — the cell's effective observation count;
          ``flat_estimate``         — the flat per-cell empirical accuracy (None if n==0);
          ``analyst_level``         — the analyst-across-regime shrunk estimate;
          ``global_prior``          — the global prior mean;
          ``pooled_skill``          — the partial-pooled skill estimate that REPLACES
                                      the flat estimate as the weight driver;
          ``warmup``                — True iff the cell is below the warm-up threshold;
          ``uniform_target``        — the near-uniform skill target used while warming up;
          ``epoch``                 — the resolved epoch tag for the (analyst, model_id).

        ``n_active_analysts`` is the count of analysts contributing to the current
        decision; it parameterizes the uniform target.

        ``epoch`` (review-fix): when provided, the diagnostics are read for that
        LITERAL epoch, bypassing ``_resolve_epoch_readonly``. This is required by the
        status iterator, which already holds each cell's own epoch key: round-tripping
        an empty epoch ('') through ``model_id=(epoch or None)`` would re-resolve to the
        analyst's LATEST model-id and MISREPORT a pre-provenance cell's effective_n (the
        load-bearing honesty field). With ``epoch=None`` the legacy ``model_id`` path is
        used (callers that only know the model-id, e.g. the live weight read).

        Read-only: resolves the epoch WITHOUT advancing it (so calling diagnostics
        does not mutate epoch state). Use :meth:`observe` to advance epochs.
        """
        epoch = epoch if epoch is not None else self._resolve_epoch_readonly(analyst, model_id)
        key = (analyst, regime, epoch)
        cell = self._cells.get(key)
        effective_n = cell.n if cell is not None else 0.0
        flat = cell.empirical_accuracy if cell is not None else None

        analyst_mean, _analyst_n = self._analyst_level_estimate(analyst, epoch)
        global_mean = self._global_prior_mean

        pooled = pooled_skill(
            cell_wins=cell.alpha if cell is not None else 0.0,
            cell_n=effective_n,
            analyst_mean=analyst_mean,
            shrinkage_k=self.shrinkage_k,
        )

        warmup = effective_n < self.warmup_n
        uniform_target = uniform_skill_target(global_mean, n_active_analysts)
        if warmup:
            # Pull the pooled skill toward the uniform target by warmup_uniform_pull.
            pull = float(np.clip(self.warmup_uniform_pull, 0.0, 1.0))
            pooled = (1.0 - pull) * pooled + pull * uniform_target

        return {
            "effective_n": float(effective_n),
            "flat_estimate": flat,
            "analyst_level": float(analyst_mean),
            "global_prior": float(global_mean),
            "pooled_skill": float(pooled),
            "warmup": bool(warmup),
            "uniform_target": float(uniform_target),
            "epoch": epoch,
        }

    def _resolve_epoch_readonly(self, analyst: str, model_id: str | None) -> str:
        """Resolve the epoch a (analyst, model_id) maps to WITHOUT mutating state.

        Mirrors :meth:`_epoch_for` but never advances ``_epoch_of`` — so
        diagnostics / weight reads are side-effect-free. With ``model_id`` present
        the epoch IS that model-id (a fresh model_id naturally maps to a brand-new,
        empty cell — warm-up — even before the first observe()).
        """
        if model_id is None:
            return self._epoch_of.get(analyst, "")
        return model_id

    def pooled_weight(
        self,
        analyst: str,
        regime: str,
        *,
        model_id: str | None = None,
        n_active_analysts: int = 1,
    ) -> float:
        """The pooled (analyst, regime) skill estimate to USE as the weight driver."""
        return float(
            self.cell_diagnostics(
                analyst,
                regime,
                model_id=model_id,
                n_active_analysts=n_active_analysts,
            )["pooled_skill"]
        )


def uniform_skill_target(global_prior_mean: float, n_active_analysts: int) -> float:
    """The near-uniform skill target a warm-up cell is pulled toward.

    Expressed on the SKILL scale (directional accuracy in [0, 1]), the honest
    "no differential evidence" target is the global prior mean — every analyst
    looks equally (un)skilled, so downstream the per-analyst weights are near-equal
    (uniform), exactly the safe default the pre-calibration uniform-weight path
    already uses. ``n_active_analysts`` is accepted for API symmetry / future
    1/N-on-the-weight-scale variants and to keep the call site explicit; the
    skill-scale target itself is the prior mean regardless of N (equal across
    analysts ⇒ uniform weights after normalization).
    """
    return float(np.clip(global_prior_mean, 0.0, 1.0))


def pooled_skill(
    *,
    cell_wins: float,
    cell_n: float,
    analyst_mean: float,
    shrinkage_k: float,
) -> float:
    """Partial-pooled cell skill: shrink the cell estimate toward the parent.

    James–Stein / empirical-Bayes reliability form::

        w      = cell_n / (cell_n + shrinkage_k)        # cell self-trust
        flat   = cell_wins / cell_n                     # flat per-cell estimate
        pooled = w * flat + (1 - w) * analyst_mean

    Properties (the NEVER-AMPLIFY guarantees, asserted by the tests):
      * ``cell_n == 0`` → pooled == analyst_mean exactly (a fresh cell IS the parent).
      * thin cell (small n, e.g. 2/2 → flat 1.0) → pooled lands materially BELOW the
        flat 1.0, pulled toward analyst_mean. It is shrinkage, never amplification:
        pooled is a convex combination of ``flat`` and ``analyst_mean``, so it can
        never exceed ``max(flat, analyst_mean)`` — it only rises above ``flat`` when
        the *parent* prior is genuinely higher-skill, never as free amplification.
      * well-populated cell (large n) → pooled ≈ flat (the cell earns its autonomy).
    """
    n = float(cell_n)
    if n <= 0.0:
        return float(np.clip(analyst_mean, 0.0, 1.0))
    flat = cell_wins / n
    w = n / (n + float(shrinkage_k))
    pooled = w * flat + (1.0 - w) * float(analyst_mean)
    return float(np.clip(pooled, 0.0, 1.0))
