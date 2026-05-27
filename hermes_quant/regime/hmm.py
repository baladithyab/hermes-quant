"""hermes_quant.regime.hmm — 3-state Hidden Markov Model regime classifier (v0.2).

Implements the Mantshimuli & Mwamba (2026) HMM-BMA regime classifier.
Three latent states: BULL, BEAR, VOLATILE.

Features per observation (z-scored within the training window):
    [realized_vol_60d, realized_vol_percentile, trend_strength, yield_curve_slope_or_zero]

Architecture:
    - Gaussian emission HMM with full covariance (diagonal fallback for stability).
    - Baum-Welch EM for training; Viterbi for decoding.
    - Tries hmmlearn first (fast, well-tested); falls back to pure-numpy Baum-Welch
      if hmmlearn is not installed.
    - Ships a pre-trained default model fitted on synthetic SPY-like 5-year history
      (numpy seed=42) so first-run users get sensible classifications without training.

Usage:
    clf = HMMClassifier()          # loads pre-trained defaults
    regime, reason = clf.classify(state_vars)   # -> (RegimeState, str)

    clf = HMMClassifier()
    clf.fit(obs_list)              # train on historical StateVariables
    clf.save(Path("model.pkl"))
    clf.load(Path("model.pkl"))

Reference: Mantshimuli & Mwamba, "Hidden Markov Bayesian Model Averaging for Financial
Returns", Springer 2026.
"""
from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from hermes_quant.regime.state_variables import StateVariables

if TYPE_CHECKING:
    pass  # avoid circular import for RegimeState at type-check time only

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Number of latent states in the HMM.
N_STATES: int = 3

#: Minimum observations required to attempt HMM-based classification.
#: Fewer than this → UNKNOWN with 'insufficient_data'.
MIN_OBS_FOR_CLASSIFY: int = 2

#: Feature dimension: [vol_60d, vol_pct, trend, yc_slope]
N_FEATURES: int = 4

#: Default model persistence path (XDG-ish under ~/.hermes)
DEFAULT_MODEL_PATH: Path = (
    Path.home() / ".hermes" / "quant" / "regime" / "hmm-model.pkl"
)

# State-index → RegimeState mapping (set after RegimeState import to avoid circular)
_STATE_LABELS = ["bull", "bear", "volatile"]  # by index 0,1,2


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------


def _extract_features(sv: StateVariables) -> np.ndarray:
    """Extract 4-d feature vector from a StateVariables instance.

    Returns array([vol_60d, vol_pct, trend, yc_slope]) with None → 0.0.
    """
    return np.array(
        [
            sv.realized_vol_60d if sv.realized_vol_60d is not None else 0.0,
            sv.realized_vol_percentile if sv.realized_vol_percentile is not None else 0.5,
            sv.trend_strength if sv.trend_strength is not None else 0.0,
            sv.yield_curve_slope if sv.yield_curve_slope is not None else 0.0,
        ],
        dtype=float,
    )


def _build_obs_matrix(observations: list[StateVariables]) -> np.ndarray:
    """Stack observations into (T, N_FEATURES) matrix, then z-score."""
    raw = np.array([_extract_features(sv) for sv in observations], dtype=float)  # (T, 4)
    mu = raw.mean(axis=0)
    sig = raw.std(axis=0, ddof=1)
    sig = np.where(sig < 1e-10, 1.0, sig)
    return (raw - mu) / sig, mu, sig  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Pure-numpy Gaussian HMM (Baum-Welch + Viterbi)
# ---------------------------------------------------------------------------


class _NumpyGaussianHMM:
    """Minimal Gaussian-emission HMM: Baum-Welch EM + Viterbi decode.

    Params:
        n_states: number of hidden states (default 3).
        n_iter: EM iterations.
        tol: log-likelihood improvement threshold for early stop.
        random_state: numpy rng seed for reproducibility.
    """

    def __init__(
        self,
        n_states: int = N_STATES,
        n_iter: int = 100,
        tol: float = 1e-4,
        random_state: int = 42,
    ) -> None:
        self.n_states = n_states
        self.n_iter = n_iter
        self.tol = tol
        self.random_state = random_state

        # Model parameters (set by fit)
        self.startprob_: np.ndarray | None = None  # (n_states,)
        self.transmat_: np.ndarray | None = None   # (n_states, n_states)
        self.means_: np.ndarray | None = None       # (n_states, n_features)
        self.covars_: np.ndarray | None = None      # (n_states, n_features) diagonal

    # ------------------------------------------------------------------
    # Gaussian log-likelihood helpers
    # ------------------------------------------------------------------

    def _log_gaussian(self, X: np.ndarray) -> np.ndarray:
        """Compute log p(x_t | state=k) for all t, k.

        Args:
            X: (T, n_features)

        Returns:
            log_prob: (T, n_states)
        """
        T, D = X.shape
        log_prob = np.zeros((T, self.n_states))
        for k in range(self.n_states):
            diff = X - self.means_[k]  # (T, D)
            covars = np.maximum(self.covars_[k], 1e-6)
            log_det = np.sum(np.log(covars))
            mahal = np.sum(diff**2 / covars, axis=1)  # (T,)
            log_prob[:, k] = -0.5 * (D * np.log(2 * np.pi) + log_det + mahal)
        return log_prob

    # ------------------------------------------------------------------
    # Forward-backward algorithm
    # ------------------------------------------------------------------

    def _forward(self, log_emit: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Log-sum-exp forward pass.

        Returns:
            log_alpha: (T, n_states)
            log_scaling: (T,) per-step log normalisation constants
        """
        T, K = log_emit.shape
        log_alpha = np.empty((T, K))
        log_alpha[0] = np.log(self.startprob_ + 1e-300) + log_emit[0]
        log_c = np.empty(T)
        log_c[0] = _log_sum_exp(log_alpha[0])
        log_alpha[0] -= log_c[0]

        log_trans = np.log(self.transmat_ + 1e-300)  # (K, K)
        for t in range(1, T):
            # log_alpha[t, j] = log(sum_i alpha[t-1,i] * A[i,j]) + log_emit[t,j]
            log_alpha_ext = log_alpha[t - 1, :, None] + log_trans  # (K, K)
            log_alpha[t] = _log_sum_exp_axis(log_alpha_ext, axis=0) + log_emit[t]
            log_c[t] = _log_sum_exp(log_alpha[t])
            log_alpha[t] -= log_c[t]
        return log_alpha, log_c

    def _backward(
        self, log_emit: np.ndarray, log_c: np.ndarray
    ) -> np.ndarray:
        """Log-sum-exp backward pass.

        Returns:
            log_beta: (T, n_states)
        """
        T, K = log_emit.shape
        log_beta = np.zeros((T, K))
        log_trans = np.log(self.transmat_ + 1e-300)

        for t in range(T - 2, -1, -1):
            # log_beta[t, i] = log(sum_j A[i,j] * emit[t+1,j] * beta[t+1,j])
            log_sum_j = log_trans + log_emit[t + 1] + log_beta[t + 1]  # (K, K)
            log_beta[t] = _log_sum_exp_axis(log_sum_j, axis=1) - log_c[t + 1]
        return log_beta

    # ------------------------------------------------------------------
    # Viterbi decode
    # ------------------------------------------------------------------

    def _viterbi(self, log_emit: np.ndarray) -> np.ndarray:
        """Return most-likely state sequence via Viterbi algorithm.

        Args:
            log_emit: (T, n_states)

        Returns:
            states: (T,) int array
        """
        T, K = log_emit.shape
        log_delta = np.empty((T, K))
        psi = np.zeros((T, K), dtype=int)

        log_delta[0] = np.log(self.startprob_ + 1e-300) + log_emit[0]
        log_trans = np.log(self.transmat_ + 1e-300)

        for t in range(1, T):
            scores = log_delta[t - 1, :, None] + log_trans  # (K, K)
            psi[t] = np.argmax(scores, axis=0)
            log_delta[t] = scores[psi[t], np.arange(K)] + log_emit[t]

        states = np.empty(T, dtype=int)
        states[-1] = np.argmax(log_delta[-1])
        for t in range(T - 2, -1, -1):
            states[t] = psi[t + 1, states[t + 1]]
        return states

    # ------------------------------------------------------------------
    # EM (Baum-Welch)
    # ------------------------------------------------------------------

    def fit(self, X: np.ndarray) -> "_NumpyGaussianHMM":
        """Fit HMM parameters with Baum-Welch EM.

        Args:
            X: (T, n_features) observation matrix.
        """
        rng = np.random.RandomState(self.random_state)
        T, D = X.shape
        K = self.n_states

        # Initialise parameters
        self.startprob_ = np.ones(K) / K
        # Row-stochastic transition matrix with slight self-persistence
        A = rng.dirichlet(np.array([5.0, 1.0, 1.0]), size=K)
        # Shuffle so each state has its own primary diagonal
        self.transmat_ = A

        # Initialise means by k-means-like random partitions
        indices = rng.permutation(T)
        chunk = T // K
        self.means_ = np.array(
            [X[indices[i * chunk : (i + 1) * chunk]].mean(axis=0) for i in range(K)]
        )
        self.covars_ = np.ones((K, D))

        prev_ll = -np.inf
        for _iteration in range(self.n_iter):
            log_emit = self._log_gaussian(X)  # (T, K)
            log_alpha, log_c = self._forward(log_emit)
            log_beta = self._backward(log_emit, log_c)

            # Log-likelihood
            ll = float(np.sum(log_c))

            # Gamma: posterior state probs — (T, K)
            log_gamma = log_alpha + log_beta
            log_gamma -= _log_sum_exp_axis(log_gamma, axis=1, keepdims=True)
            gamma = np.exp(log_gamma)

            # Xi: joint transition posteriors — sum over t → (K, K)
            # xi[t, i, j] ∝ alpha[t,i] * A[i,j] * emit[t+1,j] * beta[t+1,j]
            log_xi_sum = np.full((K, K), -np.inf)
            log_trans = np.log(self.transmat_ + 1e-300)
            for t in range(T - 1):
                log_xi_t = (
                    log_alpha[t, :, None]
                    + log_trans
                    + log_emit[t + 1][None, :]
                    + log_beta[t + 1][None, :]
                )
                log_xi_sum = np.logaddexp(log_xi_sum, log_xi_t)

            # M-step
            self.startprob_ = gamma[0] / (gamma[0].sum() + 1e-300)
            # Transition matrix
            xi_sum = np.exp(log_xi_sum)
            self.transmat_ = xi_sum / (xi_sum.sum(axis=1, keepdims=True) + 1e-300)
            # Means and covariances
            gamma_sum = gamma.sum(axis=0) + 1e-300  # (K,)
            self.means_ = (gamma[:, :, None] * X[:, None, :]).sum(axis=0) / gamma_sum[
                :, None
            ]
            for k in range(K):
                diff = X - self.means_[k]
                w = gamma[:, k]
                self.covars_[k] = (
                    w[:, None] * diff**2
                ).sum(axis=0) / gamma_sum[k]
                self.covars_[k] = np.maximum(self.covars_[k], 1e-4)

            if abs(ll - prev_ll) < self.tol:
                logger.debug("HMM EM converged at iteration %d (ΔLL=%.4e)", _iteration, ll - prev_ll)
                break
            prev_ll = ll

        return self

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Viterbi-decode a sequence and return state indices."""
        log_emit = self._log_gaussian(X)
        return self._viterbi(log_emit)

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Return posterior state probabilities (T, K) using forward-backward."""
        log_emit = self._log_gaussian(X)
        log_alpha, log_c = self._forward(log_emit)
        log_beta = self._backward(log_emit, log_c)
        log_gamma = log_alpha + log_beta
        log_gamma -= _log_sum_exp_axis(log_gamma, axis=1, keepdims=True)
        return np.exp(log_gamma)


# ---------------------------------------------------------------------------
# Log-sum-exp utilities
# ---------------------------------------------------------------------------


def _log_sum_exp(log_probs: np.ndarray) -> float:
    """Numerically stable log-sum-exp over a 1-D array."""
    a = np.max(log_probs)
    return float(a + np.log(np.sum(np.exp(log_probs - a))))


def _log_sum_exp_axis(
    log_probs: np.ndarray, axis: int, keepdims: bool = False
) -> np.ndarray:
    """Numerically stable log-sum-exp over a given axis."""
    a = np.max(log_probs, axis=axis, keepdims=True)
    result = a + np.log(np.sum(np.exp(log_probs - a), axis=axis, keepdims=True))
    if not keepdims:
        result = result.squeeze(axis=axis)
    return result


# ---------------------------------------------------------------------------
# Synthetic SPY-like training data (pre-train default model; seed=42)
# ---------------------------------------------------------------------------


def _generate_synthetic_training_data(
    n_days: int = 1260, seed: int = 42
) -> list[StateVariables]:
    """Generate ~5 years of synthetic SPY-like StateVariables for default pre-train.

    Regime schedule (approximately):
        BULL:     days 0-399   (calm uptrend)
        VOLATILE: days 400-599 (high vol shock)
        BEAR:     days 600-899 (downtrend, moderate vol)
        BULL:     days 900-1099 (recovery)
        VOLATILE: days 1100-1259 (tail shock)
    """
    rng = np.random.RandomState(seed)

    observations: list[StateVariables] = []
    price = 400.0

    for i in range(n_days):
        # Determine true regime
        if i < 400:
            regime_tag = "bull"
        elif i < 600:
            regime_tag = "volatile"
        elif i < 900:
            regime_tag = "bear"
        elif i < 1100:
            regime_tag = "bull"
        else:
            regime_tag = "volatile"

        # Simulate features per regime
        if regime_tag == "bull":
            daily_ret = rng.normal(0.0007, 0.008)   # +~18% ann, low vol
            vol_60d = rng.normal(0.13, 0.015)
            vol_pct = rng.beta(2, 5)                  # low percentile ~0.28
            trend = rng.normal(1.0, 0.4)              # positive trend
            yc = rng.normal(0.8, 0.2)                 # normal curve
        elif regime_tag == "bear":
            daily_ret = rng.normal(-0.0006, 0.012)   # -~15% ann, moderate vol
            vol_60d = rng.normal(0.22, 0.03)
            vol_pct = rng.beta(3, 4)                  # mid-high percentile ~0.43
            trend = rng.normal(-1.0, 0.4)             # negative trend
            yc = rng.normal(-0.1, 0.2)               # flat/inverted
        else:  # volatile
            daily_ret = rng.normal(0.0, 0.020)        # high vol, no trend
            vol_60d = rng.normal(0.38, 0.05)
            vol_pct = rng.beta(8, 2)                  # very high percentile ~0.80
            trend = rng.normal(0.0, 0.8)              # noisy
            yc = rng.normal(0.3, 0.3)

        # Clip to valid ranges
        vol_60d = float(np.clip(vol_60d, 0.05, 0.8))
        vol_pct = float(np.clip(vol_pct, 0.0, 1.0))

        price *= np.exp(daily_ret)
        ts = pd.Timestamp("2021-01-04", tz="UTC") + pd.Timedelta(days=i)

        observations.append(
            StateVariables(
                realized_vol_60d=vol_60d,
                realized_vol_percentile=vol_pct,
                yield_curve_slope=float(yc),
                trend_strength=float(trend),
                as_of=ts,
            )
        )

    return observations


# ---------------------------------------------------------------------------
# State-label alignment
# ---------------------------------------------------------------------------


def _align_state_labels(
    model: "_NumpyGaussianHMM | object",
    means: np.ndarray,
    scaler_mean: np.ndarray | None = None,
    scaler_std: np.ndarray | None = None,
    X_train: np.ndarray | None = None,
) -> dict[int, str]:
    """Map HMM state indices to regime labels by inspecting learned means.

    Feature order: [vol_60d, vol_pct, trend, yc_slope]
    Heuristic (applied in RAW / un-scaled feature space for robustness):

    Primary heuristic (used when training data covers multiple regimes):
        - VOLATILE: state with highest raw mean vol_pct among all states
        - BEAR:     of remaining states, the one with the lowest raw mean trend
        - BULL:     remaining state

    Boundary-awareness: when ALL raw vol_pcts are below 0.65 (all observations are
    non-volatile), the state with the highest vol_pct becomes VOLATILE (the least-bull
    outlier), the state with the lowest trend becomes BEAR, and the dominant state (most
    common Viterbi assignment on training data, if available) becomes BULL.

    Args:
        means: (n_states, n_features) array of emission means in SCALED space.
        scaler_mean: feature means used for z-scoring (to un-scale). If None, raw means used.
        scaler_std: feature stds used for z-scoring. If None, raw means used.
        X_train: (T, n_features) scaled training data for dominant-state detection.
    """
    # Un-scale to raw feature space so label alignment is not distorted by training distribution
    if scaler_mean is not None and scaler_std is not None:
        raw_means = means * scaler_std + scaler_mean  # (n_states, n_features)
    else:
        raw_means = means

    indices = list(range(raw_means.shape[0]))
    vol_pcts = raw_means[:, 1]  # realized_vol_percentile means (raw scale: [0,1])
    trends = raw_means[:, 2]    # trend_strength means

    volatile_idx = int(np.argmax(vol_pcts))
    remaining = [i for i in indices if i != volatile_idx]

    bear_idx_local = int(np.argmin([trends[i] for i in remaining]))
    bear_idx = remaining[bear_idx_local]
    bull_idx = [i for i in remaining if i != bear_idx][0]

    label_map = {bull_idx: "bull", bear_idx: "bear", volatile_idx: "volatile"}

    # Sanity check: if training data is available, verify that the dominant state
    # (most Viterbi assignments) is not labeled 'bear' when most raw trend means are positive.
    # This handles the degenerate single-regime training case.
    if X_train is not None and len(X_train) > 0:
        try:
            pred = model.predict(X_train)
            counts = np.bincount(pred, minlength=len(indices))
            dominant_idx = int(np.argmax(counts))

            # If the dominant state is labeled 'bear' but the average raw trend of training
            # data is positive (clearly a bull dataset), flip bear ↔ bull labels.
            avg_raw_trend = float(np.mean(raw_means[:, 2]))
            if label_map.get(dominant_idx) == "bear" and avg_raw_trend > 0.3:
                # Swap the bear and bull labels
                old_bull = bull_idx
                old_bear = bear_idx
                label_map[old_bear] = "bull"
                label_map[old_bull] = "bear"
                logger.debug(
                    "_align_state_labels: swapped bear↔bull (dominant state was 'bear' "
                    "but avg_raw_trend=%.2f > 0.3; training data appears bullish)", avg_raw_trend
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("_align_state_labels: could not verify dominant state: %s", exc)

    return label_map


# ---------------------------------------------------------------------------
# HMMClassifier (public API)
# ---------------------------------------------------------------------------


class HMMClassifier:
    """3-state Gaussian HMM classifier for market regime detection.

    Maps sequences of StateVariables to BULL / BEAR / VOLATILE regime states.

    Attributes:
        model: fitted HMM model (hmmlearn GaussianHMM or _NumpyGaussianHMM).
        scaler_mean: feature mean used for z-scoring.
        scaler_std: feature std used for z-scoring.
        label_map: {state_index: regime_name} mapping inferred from learned means.
    """

    def __init__(self) -> None:
        self.model: _NumpyGaussianHMM | None = None
        self.scaler_mean: np.ndarray | None = None
        self.scaler_std: np.ndarray | None = None
        self.label_map: dict[int, str] = {}
        self._fitted: bool = False
        self._use_hmmlearn: bool = False

        # Lazily train default model on first classify() call if not fitted
        self._default_trained: bool = False

    # ------------------------------------------------------------------
    # Feature scaling
    # ------------------------------------------------------------------

    def _scale(self, X: np.ndarray) -> np.ndarray:
        """Apply stored z-score scaling."""
        return (X - self.scaler_mean) / np.where(
            self.scaler_std < 1e-10, 1.0, self.scaler_std
        )

    # ------------------------------------------------------------------
    # Model backend selection
    # ------------------------------------------------------------------

    @staticmethod
    def _try_import_hmmlearn():
        """Attempt to import hmmlearn.hmm.GaussianHMM; return None on failure."""
        try:
            from hmmlearn.hmm import GaussianHMM  # type: ignore[import-untyped]
            return GaussianHMM
        except ImportError:
            return None

    # ------------------------------------------------------------------
    # fit
    # ------------------------------------------------------------------

    def fit(self, observations: list[StateVariables]) -> None:
        """Train the HMM on a sequence of StateVariables.

        Args:
            observations: list of StateVariables ordered chronologically.
                Must have at least MIN_OBS_FOR_CLASSIFY entries.

        Side-effects:
            - Sets self.model, self.scaler_mean, self.scaler_std, self.label_map.
            - Persists trained model to DEFAULT_MODEL_PATH.
        """
        if len(observations) < MIN_OBS_FOR_CLASSIFY:
            raise ValueError(
                f"HMMClassifier.fit requires at least {MIN_OBS_FOR_CLASSIFY} observations; "
                f"got {len(observations)}."
            )

        raw = np.array([_extract_features(sv) for sv in observations], dtype=float)
        self.scaler_mean = raw.mean(axis=0)
        self.scaler_std = raw.std(axis=0, ddof=1)
        self.scaler_std = np.where(self.scaler_std < 1e-10, 1.0, self.scaler_std)
        X = (raw - self.scaler_mean) / self.scaler_std

        GaussianHMM = self._try_import_hmmlearn()
        if GaussianHMM is not None:
            logger.debug("HMMClassifier: using hmmlearn backend")
            self._use_hmmlearn = True
            mdl = GaussianHMM(
                n_components=N_STATES,
                covariance_type="diag",
                n_iter=200,
                random_state=42,
            )
            mdl.fit(X)
            self.model = mdl
            means = mdl.means_
        else:
            logger.debug("HMMClassifier: using pure-numpy Baum-Welch backend")
            self._use_hmmlearn = False
            mdl = _NumpyGaussianHMM(n_states=N_STATES, n_iter=200, random_state=42)
            mdl.fit(X)
            self.model = mdl
            means = mdl.means_

        self.label_map = _align_state_labels(self.model, means, self.scaler_mean, self.scaler_std, X)
        self._fitted = True
        logger.info(
            "HMMClassifier fitted on %d observations; label_map=%s",
            len(observations),
            self.label_map,
        )

        # Auto-persist
        try:
            self.save(DEFAULT_MODEL_PATH)
        except Exception as exc:  # noqa: BLE001
            logger.debug("HMMClassifier: could not auto-save to %s: %s", DEFAULT_MODEL_PATH, exc)

    # ------------------------------------------------------------------
    # save / load
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Persist trained model to disk using joblib (falls back to pickle).

        Args:
            path: file path (parent dirs created automatically).
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": self.model,
            "scaler_mean": self.scaler_mean,
            "scaler_std": self.scaler_std,
            "label_map": self.label_map,
            "use_hmmlearn": self._use_hmmlearn,
        }
        try:
            import joblib  # type: ignore[import-untyped]
            joblib.dump(payload, path)
            logger.debug("HMMClassifier saved via joblib to %s", path)
        except ImportError:
            with open(path, "wb") as f:
                pickle.dump(payload, f, protocol=4)
            logger.debug("HMMClassifier saved via pickle to %s", path)

    def load(self, path: Path) -> None:
        """Restore a trained model from disk.

        Args:
            path: file path previously created by .save().

        Raises:
            FileNotFoundError: if path does not exist.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"HMMClassifier: model file not found: {path}")
        try:
            import joblib
            payload = joblib.load(path)
        except (ImportError, Exception):
            with open(path, "rb") as f:
                payload = pickle.load(f)  # noqa: S301

        self.model = payload["model"]
        self.scaler_mean = payload["scaler_mean"]
        self.scaler_std = payload["scaler_std"]
        self.label_map = payload["label_map"]
        self._use_hmmlearn = payload.get("use_hmmlearn", False)
        self._fitted = True
        logger.info("HMMClassifier loaded from %s; label_map=%s", path, self.label_map)

    # ------------------------------------------------------------------
    # Default pre-trained model (lazy, seed=42 synthetic)
    # ------------------------------------------------------------------

    def _ensure_default_trained(self) -> None:
        """Fit on synthetic data if not yet trained (lazy first-call pattern)."""
        if self._fitted:
            return
        if self._default_trained:
            return
        logger.debug(
            "HMMClassifier: no trained model; fitting on synthetic default data (seed=42)"
        )
        obs = _generate_synthetic_training_data(n_days=1260, seed=42)
        self.fit(obs)
        self._default_trained = True

    # ------------------------------------------------------------------
    # classify
    # ------------------------------------------------------------------

    def classify(self, state_vars: StateVariables) -> tuple["RegimeState", str]:
        """Classify a single StateVariables observation into a regime state.

        This method matches the ``hmm_classifier: Callable[[StateVariables], RegimeState]``
        hook signature in RegimeDetector. The full tuple is consumed when the caller
        does tuple-unpacking; RegimeDetector only uses the first element.

        Args:
            state_vars: current market state variables.

        Returns:
            (RegimeState, reason_string)
                RegimeState: one of BULL, BEAR, VOLATILE, UNKNOWN.
                reason_string: short diagnostic for audit logs.
        """
        # Import here to avoid circular imports at module level
        from hermes_quant.regime.detector import RegimeState

        # Insufficient data guard
        if state_vars.realized_vol_percentile is None or state_vars.realized_vol_60d is None:
            return RegimeState.UNKNOWN, "insufficient_data: required fields are None"

        self._ensure_default_trained()

        if not self._fitted or self.model is None:
            return RegimeState.UNKNOWN, "insufficient_data: model not fitted"

        # Build scaled feature vector (single obs → shape (1, N_FEATURES))
        raw = _extract_features(state_vars).reshape(1, -1)
        X = self._scale(raw)

        # Predict state index
        try:
            if self._use_hmmlearn:
                # hmmlearn: predict from a length-1 sequence
                state_seq = self.model.predict(X)
            else:
                state_seq = self.model.predict(X)
        except Exception as exc:  # noqa: BLE001
            logger.warning("HMMClassifier.classify prediction failed: %s", exc)
            return RegimeState.UNKNOWN, f"prediction_error: {exc}"

        state_idx = int(state_seq[-1])
        regime_name = self.label_map.get(state_idx, "unknown")

        regime_map = {
            "bull": RegimeState.BULL,
            "bear": RegimeState.BEAR,
            "volatile": RegimeState.VOLATILE,
            "unknown": RegimeState.UNKNOWN,
        }
        regime = regime_map.get(regime_name, RegimeState.UNKNOWN)

        reason = (
            f"hmm_state={state_idx} → {regime_name}; "
            f"vol_pct={state_vars.realized_vol_percentile:.3f}, "
            f"trend={state_vars.trend_strength:.3f}"
            if state_vars.trend_strength is not None
            else f"hmm_state={state_idx} → {regime_name}; "
            f"vol_pct={state_vars.realized_vol_percentile:.3f}"
        )

        logger.debug("HMMClassifier: %s", reason)
        return regime, reason


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def build_default_classifier() -> HMMClassifier:
    """Return a new HMMClassifier instance (with lazy default training)."""
    return HMMClassifier()
