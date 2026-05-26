"""hermes_quant.evaluation.cv — Purged walk-forward cross-validation.

Per ADR-0019 §D2. Walk-forward train/val/test windows with an embargo
buffer between train and val to prevent leakage from features that lag
their underlying signal. Source: López de Prado, "Advances in Financial
Machine Learning" (2018), Chapter 7.

v0.3 ships the API + minimal tests; v0.4 RL training will be the primary
consumer when training the aggregator on rolling windows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import pandas as pd


@dataclass(frozen=True)
class WalkForwardSplit:
    """One train/val/test window from a PurgedWalkForward iterator."""

    train_start: pd.Timestamp
    train_end: pd.Timestamp  # exclusive of embargo region
    val_start: pd.Timestamp
    val_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    fold: int  # 0-indexed fold number

    def assert_no_leakage(self) -> None:
        """Per López de Prado §7.4: train_end < val_start, val_end <= test_start.

        Raise AssertionError if the split is internally inconsistent.
        """
        assert self.train_end < self.val_start, (
            f"train_end={self.train_end} must precede val_start={self.val_start} (embargo violated)"
        )
        assert self.val_end <= self.test_start, (
            f"val_end={self.val_end} must precede test_start={self.test_start}"
        )


class PurgedWalkForward:
    """Walk-forward cross-validator with embargo (López de Prado §7).

    Splits a timestamped DataFrame into n_splits folds. Each fold:
      - train: the earliest train_pct of the window minus embargo_pct
      - val:   the next val_pct
      - test:  the trailing test_pct

    The embargo is a buffer at the end of the train window dropped to
    prevent leakage when features have lag (e.g., 14-day moving average
    crosses can leak 13 days into the validation period).

    Args:
        n_splits: Number of walk-forward folds. Default 5.
        embargo_pct: Fraction of total window to drop at the end of each
            train period as the embargo buffer. Default 0.01 (1%).
        train_pct: Fraction of fold for training. Default 0.6.
        val_pct: Fraction of fold for validation. Default 0.2.
        Test pct is implied: 1 - train_pct - val_pct (default 0.2).

    Raises:
        ValueError: train_pct + val_pct >= 1.0 or any pct < 0.

    Reference: López de Prado, "Advances in Financial Machine Learning",
    Chapter 7 ("Cross-Validation in Finance").
    """

    def __init__(
        self,
        *,
        n_splits: int = 5,
        embargo_pct: float = 0.01,
        train_pct: float = 0.6,
        val_pct: float = 0.2,
    ):
        if train_pct + val_pct >= 1.0:
            raise ValueError(
                f"train_pct ({train_pct}) + val_pct ({val_pct}) must be < 1.0 "
                "to leave room for test"
            )
        if any(p < 0 for p in (embargo_pct, train_pct, val_pct)):
            raise ValueError("all percentages must be non-negative")
        if n_splits < 1:
            raise ValueError(f"n_splits must be >= 1, got {n_splits}")

        self.n_splits = n_splits
        self.embargo_pct = embargo_pct
        self.train_pct = train_pct
        self.val_pct = val_pct
        self.test_pct = 1.0 - train_pct - val_pct

    def split(self, df: pd.DataFrame) -> Iterator[WalkForwardSplit]:
        """Yield n_splits walk-forward splits from df.

        df must have a 'timestamp' column or a DatetimeIndex.
        """
        ts = self._extract_timestamps(df)
        if len(ts) < self.n_splits * 10:
            raise ValueError(
                f"DataFrame has {len(ts)} rows; need at least "
                f"{self.n_splits * 10} for {self.n_splits} folds"
            )

        total_start = ts.iloc[0]
        total_end = ts.iloc[-1]
        total_span = total_end - total_start

        # Each fold spans (1 / n_splits) of the total range, slid forward by
        # 1/n_splits each fold. Last fold reaches the end of the data.
        for fold in range(self.n_splits):
            fold_start = total_start + (total_span * fold / self.n_splits)
            fold_end = total_start + (total_span * (fold + 1) / self.n_splits)
            fold_span = fold_end - fold_start

            train_start = fold_start
            train_end_raw = fold_start + fold_span * self.train_pct
            embargo = fold_span * self.embargo_pct
            train_end = train_end_raw - embargo

            val_start = train_end_raw  # embargo is BETWEEN train and val
            val_end = val_start + fold_span * self.val_pct

            test_start = val_end
            test_end = fold_end

            split_obj = WalkForwardSplit(
                train_start=train_start,
                train_end=train_end,
                val_start=val_start,
                val_end=val_end,
                test_start=test_start,
                test_end=test_end,
                fold=fold,
            )
            split_obj.assert_no_leakage()
            yield split_obj

    @staticmethod
    def _extract_timestamps(df: pd.DataFrame) -> pd.Series:
        if "timestamp" in df.columns:
            return df["timestamp"].sort_values().reset_index(drop=True)
        if isinstance(df.index, pd.DatetimeIndex):
            return df.index.to_series().sort_values().reset_index(drop=True)
        raise ValueError("DataFrame needs a 'timestamp' column or DatetimeIndex")
