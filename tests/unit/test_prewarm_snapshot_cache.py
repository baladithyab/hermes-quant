"""Unit tests for ``hermes_quant.playbook.scorers.prewarm_snapshot_cache``.

The prewarm helper fans yfinance HTTP fetches across a thread pool to keep
the watchlist evolution cron under its hard timeout wall. These tests mock
out the underlying ``compute_play_snapshot`` so they run offline and fast.
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime
from threading import Lock
from unittest.mock import patch

import pytest

import hermes_quant.playbook.scorers as scorers_module
from hermes_quant.playbook.scorers import (
    _SNAPSHOT_CACHE,
    prewarm_snapshot_cache,
    score_symbol,
)


@pytest.fixture(autouse=True)
def _clear_snapshot_cache():
    """Reset the module-level cache between tests to keep them independent."""
    _SNAPSHOT_CACHE.clear()
    yield
    _SNAPSHOT_CACHE.clear()


def _fake_snapshot(symbol: str, asof) -> dict:  # type: ignore[no-untyped-def]
    """Return a minimal valid snapshot for a symbol."""
    return {
        "symbol": symbol,
        "asof": asof.isoformat() if hasattr(asof, "isoformat") else str(asof),
        "last_close": 100.0,
        "market_cap_usd": 1_000_000_000.0,
        "quote_type": "EQUITY",
    }


# --------------------------------------------------------------------------- #
# Happy-path
# --------------------------------------------------------------------------- #


def test_empty_input_returns_zero_summary():
    summary = prewarm_snapshot_cache([])
    assert summary == {"prewarmed": 0, "skipped": 0, "errors": 0, "elapsed_s": 0.0}


def test_populates_cache_with_uppercase_keys():
    with patch.object(scorers_module, "compute_play_snapshot", side_effect=_fake_snapshot):
        summary = prewarm_snapshot_cache(["aapl", "MSFT", "tsla"])

    assert summary["prewarmed"] == 3
    assert summary["errors"] == 0

    today_key = datetime.now(UTC).strftime("%Y-%m-%d")
    assert ("AAPL", today_key) in _SNAPSHOT_CACHE
    assert ("MSFT", today_key) in _SNAPSHOT_CACHE
    assert ("TSLA", today_key) in _SNAPSHOT_CACHE


def test_idempotent_skips_already_cached():
    """Calling prewarm twice should skip the already-cached entries on call 2."""
    with patch.object(scorers_module, "compute_play_snapshot", side_effect=_fake_snapshot):
        first = prewarm_snapshot_cache(["AAPL", "MSFT"])
        second = prewarm_snapshot_cache(["AAPL", "MSFT", "TSLA"])

    assert first["prewarmed"] == 2
    assert first["skipped"] == 0

    assert second["prewarmed"] == 1  # only TSLA is new
    assert second["skipped"] == 2


def test_dedupes_input_within_one_call():
    with patch.object(scorers_module, "compute_play_snapshot", side_effect=_fake_snapshot) as mock_cps:
        summary = prewarm_snapshot_cache(["AAPL", "aapl", "AAPL"])

    assert summary["prewarmed"] == 1
    assert mock_cps.call_count == 1


def test_explicit_asof_date_is_used_in_cache_key():
    target_date = date(2026, 1, 15)
    with patch.object(scorers_module, "compute_play_snapshot", side_effect=_fake_snapshot):
        summary = prewarm_snapshot_cache(["AAPL"], asof=target_date)

    assert summary["prewarmed"] == 1
    assert ("AAPL", "2026-01-15") in _SNAPSHOT_CACHE


def test_explicit_asof_datetime_is_used_in_cache_key():
    target_dt = datetime(2026, 1, 15, 14, 30, tzinfo=UTC)
    with patch.object(scorers_module, "compute_play_snapshot", side_effect=_fake_snapshot):
        summary = prewarm_snapshot_cache(["AAPL"], asof=target_dt)

    assert summary["prewarmed"] == 1
    assert ("AAPL", "2026-01-15") in _SNAPSHOT_CACHE


# --------------------------------------------------------------------------- #
# Failure handling — silence-by-default
# --------------------------------------------------------------------------- #


def test_per_symbol_failure_does_not_propagate():
    """A worker raising must not crash the prewarm or the other symbols."""

    def _flaky(symbol, asof):  # type: ignore[no-untyped-def]
        if symbol == "BROKEN":
            raise RuntimeError("simulated yfinance 404")
        return _fake_snapshot(symbol, asof)

    with patch.object(scorers_module, "compute_play_snapshot", side_effect=_flaky):
        summary = prewarm_snapshot_cache(["AAPL", "BROKEN", "MSFT"])

    assert summary["prewarmed"] == 2
    assert summary["errors"] == 1
    today_key = datetime.now(UTC).strftime("%Y-%m-%d")
    assert ("AAPL", today_key) in _SNAPSHOT_CACHE
    assert ("MSFT", today_key) in _SNAPSHOT_CACHE
    # BROKEN must NOT be cached — score_symbol will retry it serially.
    assert ("BROKEN", today_key) not in _SNAPSHOT_CACHE


def test_non_string_inputs_are_skipped_silently():
    with patch.object(scorers_module, "compute_play_snapshot", side_effect=_fake_snapshot):
        summary = prewarm_snapshot_cache(["AAPL", "", None, 42, "MSFT"])  # type: ignore[list-item]

    assert summary["prewarmed"] == 2  # only AAPL, MSFT


# --------------------------------------------------------------------------- #
# Concurrency — the actual reason this helper exists
# --------------------------------------------------------------------------- #


def test_runs_concurrently_not_serially():
    """With max_workers=N and per-symbol sleep S, total wall time should be
    roughly ``ceil(count/N) * S`` not ``count * S``. We use a generous
    factor-of-2 slack to absorb scheduler jitter on busy CI."""

    sleep_per_symbol = 0.05
    n_symbols = 20
    workers = 10
    expected_serial = n_symbols * sleep_per_symbol  # 1.0s
    expected_parallel = (n_symbols / workers) * sleep_per_symbol  # 0.1s

    def _slow_snapshot(symbol, asof):  # type: ignore[no-untyped-def]
        time.sleep(sleep_per_symbol)
        return _fake_snapshot(symbol, asof)

    with patch.object(scorers_module, "compute_play_snapshot", side_effect=_slow_snapshot):
        t0 = time.perf_counter()
        summary = prewarm_snapshot_cache(
            [f"SYM{i}" for i in range(n_symbols)],
            max_workers=workers,
        )
        elapsed = time.perf_counter() - t0

    assert summary["prewarmed"] == n_symbols
    # Must be substantially faster than serial; allow 2x parallel for slack.
    assert elapsed < expected_serial / 2, (
        f"prewarm ran in {elapsed:.3f}s, expected roughly "
        f"{expected_parallel:.3f}s parallel vs {expected_serial:.3f}s serial"
    )


def test_thread_safety_no_lost_writes():
    """Run a high-contention prewarm and verify every symbol made it into
    the cache. Catches a regression where the dict is mutated without a
    lock and the cache underflows."""
    n_symbols = 100
    barrier = Lock()  # use a Lock as a no-op contention focal point

    def _contentious_snapshot(symbol, asof):  # type: ignore[no-untyped-def]
        with barrier:
            pass  # fight for the lock; otherwise tiny work
        return _fake_snapshot(symbol, asof)

    symbols = [f"S{i:03d}" for i in range(n_symbols)]
    with patch.object(scorers_module, "compute_play_snapshot", side_effect=_contentious_snapshot):
        summary = prewarm_snapshot_cache(symbols, max_workers=16)

    assert summary["prewarmed"] == n_symbols
    today_key = datetime.now(UTC).strftime("%Y-%m-%d")
    for sym in symbols:
        assert (sym, today_key) in _SNAPSHOT_CACHE


# --------------------------------------------------------------------------- #
# Integration with score_symbol
# --------------------------------------------------------------------------- #


def test_prewarm_then_score_symbol_uses_cache():
    """After prewarm, score_symbol must not call compute_play_snapshot again."""
    with patch.object(scorers_module, "compute_play_snapshot", side_effect=_fake_snapshot) as mock_cps:
        prewarm_snapshot_cache(["AAPL"])
        # Reset call count after prewarm so we measure score_symbol behavior.
        mock_cps.reset_mock()

        # Call score_symbol multiple times for the same symbol across plays.
        for play in ("covered_call", "csp", "wheel", "leaps", "swing"):
            score_symbol("AAPL", play)

    # compute_play_snapshot should NOT have been called by score_symbol —
    # all 5 calls hit the cache populated by prewarm.
    assert mock_cps.call_count == 0


def test_snapshot_cache_lock_exists_and_is_a_lock():
    """B14(b): the module exposes a real threading lock guarding the cache."""
    import threading

    assert hasattr(scorers_module, "_SNAPSHOT_CACHE_LOCK")
    lock = scorers_module._SNAPSHOT_CACHE_LOCK
    # A threading.Lock instance has acquire/release and works as a CM.
    assert hasattr(lock, "acquire") and hasattr(lock, "release")
    with lock:
        pass  # acquirable + releasable
    # Sanity: it is the lock primitive type, not an RLock or arbitrary object.
    assert isinstance(lock, type(threading.Lock()))


def test_concurrent_score_symbol_no_cache_corruption(monkeypatch):
    """B14(b): many threads calling score_symbol for the SAME symbol/day under
    contention must converge on one consistent cache entry — no torn writes,
    no lost cache, no crash. Regression for the unlocked read-miss-then-write
    that two overlapping crons could interleave."""
    import threading

    n_threads = 32
    # A barrier maximizes the chance every thread reaches the read-miss at the
    # same instant, so the (previously unguarded) write race is exercised hard.
    start = threading.Barrier(n_threads)
    build_count = {"n": 0}
    count_lock = threading.Lock()

    def _counting_snapshot(symbol, asof):  # type: ignore[no-untyped-def]
        with count_lock:
            build_count["n"] += 1
        return _fake_snapshot(symbol, asof)

    results: list[float] = []
    results_lock = threading.Lock()

    def _worker():
        start.wait()
        val = score_symbol("AAPL", "covered_call")
        with results_lock:
            results.append(val)

    with patch.object(scorers_module, "compute_play_snapshot", side_effect=_counting_snapshot):
        threads = [threading.Thread(target=_worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # Every worker returned a float (no exception escaped under contention).
    assert len(results) == n_threads
    assert all(isinstance(v, float) for v in results)

    # Exactly one cache entry exists for (AAPL, today) — not a torn/duplicated
    # state. The cache holds a single, well-formed snapshot.
    today_key = datetime.now(UTC).strftime("%Y-%m-%d")
    cached = _SNAPSHOT_CACHE.get(("AAPL", today_key))
    assert cached is not None
    assert cached["symbol"] == "AAPL"


def test_env_var_overrides_default_workers(monkeypatch):
    """HERMES_QUANT_PREWARM_WORKERS env var should override the 12 default."""
    monkeypatch.setenv("HERMES_QUANT_PREWARM_WORKERS", "3")
    captured: dict = {}

    real_executor = scorers_module.ThreadPoolExecutor

    def _capturing_executor(*args, **kwargs):
        captured["max_workers"] = kwargs.get("max_workers")
        return real_executor(*args, **kwargs)

    with patch.object(scorers_module, "compute_play_snapshot", side_effect=_fake_snapshot), \
         patch.object(scorers_module, "ThreadPoolExecutor", side_effect=_capturing_executor):
        prewarm_snapshot_cache(["AAPL", "MSFT", "TSLA"])

    assert captured["max_workers"] == 3


def test_invalid_env_var_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("HERMES_QUANT_PREWARM_WORKERS", "not-a-number")
    captured: dict = {}

    real_executor = scorers_module.ThreadPoolExecutor

    def _capturing_executor(*args, **kwargs):
        captured["max_workers"] = kwargs.get("max_workers")
        return real_executor(*args, **kwargs)

    with patch.object(scorers_module, "compute_play_snapshot", side_effect=_fake_snapshot), \
         patch.object(scorers_module, "ThreadPoolExecutor", side_effect=_capturing_executor):
        prewarm_snapshot_cache(["AAPL"])

    assert captured["max_workers"] == scorers_module._DEFAULT_PREWARM_WORKERS
