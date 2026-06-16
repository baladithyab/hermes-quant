"""Concurrent double-fire regression for the daily/hourly playbook tick.

Defect (RED-verified): the playbook raw-Alpaca fire path
(`place_paper_market_order` reached via `process_pair`) NEVER acquires the
react/paper.py tick-lock and rests its day-level idempotency solely on
`fired_today_pairs()` — a snapshot captured ONCE into `fired_set` in
`run_tick`, with the journal line appended only AFTER the order POST returns.

Two armed surfaces write the same `~/.hermes/quant/playbook/tick-journal.jsonl`
(CRON-REGISTRY job #6 daily `--armed` and job #10 hourly autonomous+armed).
When a long-running daily run is still in-flight (per-symbol blocking
`call_advisor`) and a second armed invocation starts, both read an identical
`fired_set` that lacks the not-yet-journaled `(symbol, play)`, both pass the
`(symbol, play) in fired_set` gate, and both POST a raw `/orders` market order
carrying NO `client_order_id` (so Alpaca cannot dedup) — two real paper orders
(double position) for one logical signal.

These tests assert the two complementary fixes:
  1. A deterministic `client_order_id` reaches the POST body so a broker that
     enforces order-id uniqueness rejects the duplicate even under a lockless
     race. The id must be identical for the same (date_et, symbol, play).
  2. A per-(symbol, play) advisory flock serializes read->POST->journal so the
     second concurrent run sees the journaled fire and skips — exactly ONE
     real POST happens.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

# Canonical source-of-truth lives in the repo at ops/scripts/. The deployed
# copy under ~/.hermes/scripts/ is a separate runtime artifact; the fix is
# committed to the repo source, so test against that.
_REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = _REPO_ROOT / "ops" / "scripts" / "quant-playbook-tick.py"
if not SCRIPT_PATH.exists():
    SCRIPT_PATH = _REPO_ROOT / "scripts" / "quant-playbook-tick.py"
if not SCRIPT_PATH.exists():
    SCRIPT_PATH = Path.home() / ".hermes" / "scripts" / "quant-playbook-tick.py"

pytestmark = pytest.mark.skipif(
    not SCRIPT_PATH.exists(),
    reason=f"quant-playbook-tick.py not found at {SCRIPT_PATH}",
)


@pytest.fixture
def tick_module(monkeypatch, tmp_path):
    """Load the script with HOME redirected to an isolated tmp_path."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".hermes" / "quant" / "watchlist").mkdir(parents=True)
    (fake_home / ".hermes" / "quant" / "playbook").mkdir(parents=True)
    (fake_home / ".hermes" / "secrets").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HERMES_QUANT_PLAYBOOK_TICK_MOCK", "1")

    sys.modules.pop("quant_playbook_tick", None)
    spec = importlib.util.spec_from_file_location("quant_playbook_tick", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mod.HERMES_HOME = fake_home / ".hermes"
    mod.QUANT_HOME = mod.HERMES_HOME / "quant"
    mod.WATCHLIST_PATH = mod.QUANT_HOME / "watchlist" / "play-fit.json"
    mod.HALT_MIRROR_PATH = mod.QUANT_HOME / "halt_state.json"
    mod.PLAYBOOK_DIR = mod.QUANT_HOME / "playbook"
    mod.JOURNAL_PATH = mod.PLAYBOOK_DIR / "tick-journal.jsonl"
    mod.SECRETS_PATH = mod.HERMES_HOME / "secrets" / "alpaca.env"
    return mod


# ---------------------------------------------------------------------------
# Fix (1): deterministic client_order_id reaches the broker POST body
# ---------------------------------------------------------------------------

def test_real_fire_passes_deterministic_client_order_id_to_broker(tick_module, monkeypatch):
    """A real (non-dry) FIRE must hand place_paper_market_order a deterministic
    client_order_id derived from (date_et, symbol, play), so two concurrent
    armed runs that both reach the POST produce the SAME id and the broker
    rejects the second as a duplicate.

    Pre-fix: process_pair calls place_paper_market_order WITHOUT a
    client_order_id (and the function had no such param), so the POST body
    omitted it -> broker cannot dedup -> double real order under a race.
    """
    captured: list = []

    def recording_order(symbol, notional, *, side="buy", client_order_id=None, **kw):
        captured.append(client_order_id)
        return {"id": f"srv-{len(captured)}", "client_order_id": client_order_id,
                "submitted_at": "2026-06-16T13:00:00Z"}

    monkeypatch.setattr(tick_module, "place_paper_market_order", recording_order)

    today_et = tick_module.today_et_date()
    rec = tick_module.process_pair(
        "AAPL", "swing", 0.9, today_et=today_et, tick_id="tickA",
        fired_set=set(), dry_run=False,
    )

    assert rec["decision"] == "fire"
    assert len(captured) == 1
    coid = captured[0]
    assert coid, f"client_order_id missing in the broker call: {coid!r}"
    # Deterministic per (date, symbol, play): the SAME logical signal from a
    # concurrent run yields the identical id, so the broker dedups it.
    assert coid == tick_module.build_client_order_id(today_et, "AAPL", "swing")
    assert tick_module.build_client_order_id(today_et, "AAPL", "swing") == \
        tick_module.build_client_order_id(today_et, "AAPL", "swing")
    # Human-auditable + broker-safe (charset, <=128 chars).
    assert "AAPL" in coid and "swing" in coid and today_et in coid
    assert len(coid) <= 128
    assert all(c.isalnum() or c in "._-" for c in coid)
    # The journal record surfaces the id actually sent.
    assert rec.get("client_order_id") == coid


def test_place_order_post_body_includes_client_order_id(tick_module, monkeypatch):
    """The broker-dedup half: place_paper_market_order must serialize the
    client_order_id into the POST body so Alpaca's order-uniqueness can reject
    a duplicate even under a fully lockless race.

    Pre-fix: the JSON body (lines ~235-241) had no client_order_id key.
    """
    tick_module.SECRETS_PATH.write_text(
        'export ALPACA_API_KEY_ID="k"\n'
        'export ALPACA_API_SECRET_KEY="s"\n'
        'export ALPACA_BASE_URL="https://paper.example/v2"\n'
    )

    seen_bodies: list = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"id":"srv-1","client_order_id":"echo"}'

    def fake_urlopen(req, timeout=None):
        seen_bodies.append(json.loads(req.data.decode("utf-8")))
        return _Resp()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    coid = tick_module.build_client_order_id("2026-06-16", "AAPL", "swing")
    tick_module.place_paper_market_order("AAPL", 1000.0, side="buy", client_order_id=coid)

    assert len(seen_bodies) == 1
    body = seen_bodies[0]
    assert body.get("client_order_id") == coid, (
        f"POST body must carry client_order_id for broker dedup; got {body!r}"
    )


# ---------------------------------------------------------------------------
# Fix (2): advisory flock serializes the concurrent read->POST->journal
# ---------------------------------------------------------------------------

def test_concurrent_armed_runs_fire_exactly_once(tick_module, monkeypatch):
    """Two concurrent armed run_tick() invocations sharing one empty journal
    must place EXACTLY ONE real order for a single (symbol, play).

    Pre-fix: the read-once fired_set + post-POST journal append + lockless raw
    POST means both threads pass the gate and both POST -> two real orders.
    """
    _write_watchlist(tick_module, [("AAPL", "swing", 0.9)])

    post_count = {"n": 0}
    barrier = threading.Barrier(2, timeout=10)
    order_lock = threading.Lock()

    def slow_order(symbol, notional, *, side="buy", client_order_id=None, **kw):
        # Block both threads at the POST boundary so the lockless TOCTOU window
        # is maximally open: both have read fired_set, neither has journaled.
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        with order_lock:
            post_count["n"] += 1
            n = post_count["n"]
        return {"id": f"srv-{n}", "client_order_id": client_order_id,
                "submitted_at": "2026-06-16T13:00:00Z"}

    monkeypatch.setattr(tick_module, "place_paper_market_order", slow_order)

    results: list = [None, None]

    def run(idx):
        results[idx] = tick_module.run_tick(dry_run=False)

    t0 = threading.Thread(target=run, args=(0,))
    t1 = threading.Thread(target=run, args=(1,))
    t0.start()
    t1.start()
    t0.join(timeout=15)
    t1.join(timeout=15)

    assert post_count["n"] == 1, (
        f"expected exactly ONE real broker POST for one (symbol,play); "
        f"got {post_count['n']} (concurrent double-fire)"
    )
    # And the journal must record exactly one real fire.
    rows = [json.loads(line) for line in tick_module.JOURNAL_PATH.read_text().splitlines() if line.strip()]
    real_fires = [r for r in rows if r.get("decision") == "fire" and not r.get("dry_run")]
    assert len(real_fires) == 1, f"journal shows {len(real_fires)} real fires, expected 1"


def _write_watchlist(mod, pairs):
    plays_dict: dict[str, list] = {}
    for sym, play, score in pairs:
        plays_dict.setdefault(play, []).append({
            "symbol": sym, "play": play, "state": "active", "last_score": score,
            "extras": {}, "last_seen_at": "2026-06-16T00:00:00+00:00",
            "onboarded_at": "2026-06-15T00:00:00+00:00", "eviction_reason": None,
        })
    mod.WATCHLIST_PATH.write_text(json.dumps({
        "as_of": "2026-06-16T00:00:00+00:00",
        "plays": plays_dict,
    }))
