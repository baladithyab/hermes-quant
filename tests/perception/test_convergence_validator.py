"""PDR-3 ConvergenceValidator pure-function unit eval (plan §5.1).

Taxonomy + n_independent (origin) counting + shared-upstream police. PURE +
offline + deterministic — no flag read here (the flag lives at the synthesize
call site). n_independent (origins), not item count, is the gating quantity.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from hermes_quant.catalyst.ingest import CatalystItem
from hermes_quant.perception.convergence import (
    CONVERGENCE_MIN_FAMILIES,
    ConvergenceResult,
    source_family,
    validate_convergence,
)

_ASOF = dt.datetime(2024, 6, 1, tzinfo=dt.UTC)


def _item(symbol: str, source: str, *, published_at: dt.datetime | None = None) -> CatalystItem:
    return CatalystItem(
        title=f"{symbol} trend headline",
        published_at=published_at or _ASOF,
        source=source,
        link="n/a",
        query="convergence-test",
    )


def _reddit_item(symbol: str, **kw) -> CatalystItem:
    return _item(symbol, "reddit/r/stocks (score=10 c=2)", **kw)


def _trends_item(symbol: str, **kw) -> CatalystItem:
    return _item(symbol, "google_trends/US", **kw)


def _news_item(symbol: str, publisher: str, **kw) -> CatalystItem:
    return _item(symbol, publisher, **kw)


def _signeval_item(symbol: str, **kw) -> CatalystItem:
    return _item(symbol, "sign-eval", **kw)


def _webtraffic_item(symbol: str, **kw) -> CatalystItem:
    return _item(symbol, "web_traffic/similarweb", **kw)


# -- the taxonomy + origin counting -----------------------------------------


def test_two_independent_families_validates():
    items = [_reddit_item("CELH"), _trends_item("CELH")]
    r = validate_convergence(items)
    assert r.validated and r.n_independent == 2 and "reddit" in r.families


def test_single_family_does_not_validate():
    # ten reddit posts != convergence (one family vote, rule 1)
    items = [_reddit_item("CELH") for _ in range(10)]
    r = validate_convergence(items)
    assert not r.validated and r.n_independent == 1 and r.n_items == 10


def test_unknown_source_never_counts():
    # sign-eval is unknown -> only reddit counts -> single source
    items = [_reddit_item("CELH"), _signeval_item("CELH")]
    r = validate_convergence(items)
    assert not r.validated and r.n_independent == 1


def test_shared_upstream_flagged_but_news_collapses():
    # two GN-RSS items, one a PRNewswire republish -> still ONE news_rss family
    items = [_news_item("CELH", "Reuters"), _news_item("CELH", "PRNewswire")]
    r = validate_convergence(items)
    assert r.n_families == 1 and r.n_independent == 1
    assert any("PRNewswire" in s for s in r.shared_upstream_collapsed)


def test_three_families_validates_and_orders_families():
    items = [_reddit_item("CELH"), _trends_item("CELH"), _news_item("CELH", "CNBC")]
    r = validate_convergence(items)
    assert r.validated and r.n_independent == 3 and r.n_families == 3
    assert r.families == ("google_trends", "news_rss", "reddit")  # sorted


def test_source_family_taxonomy():
    assert source_family("reddit/r/stocks (score=5 c=2)") == "reddit"
    assert source_family("google_trends/US") == "google_trends"
    assert source_family("Reuters") == "news_rss"
    assert source_family("sign-eval") == "unknown"
    assert source_family("phase0-label") == "unknown"
    assert source_family("") == "unknown"
    assert source_family("web_traffic/similarweb") == "web_traffic"


def test_b08_origin_collapse_reserved(monkeypatch):
    # when web_traffic shares the Google origin it must NOT double-count.
    import hermes_quant.perception.convergence as c

    monkeypatch.setitem(c._FAMILY_ORIGIN, "web_traffic", "google")
    items = [_trends_item("CELH"), _webtraffic_item("CELH")]  # both Google origin
    r = validate_convergence(items)
    assert r.n_families == 2  # two distinct family LABELS
    assert r.n_independent == 1  # ONE origin -> not validated
    assert not r.validated


def test_origin_layer_noop_today():
    # without the B08 monkeypatch, google_trends + web_traffic are distinct origins
    items = [_trends_item("CELH"), _webtraffic_item("CELH")]
    r = validate_convergence(items)
    assert r.n_independent == 2 and r.validated


def test_empty_set_does_not_validate():
    r = validate_convergence([])
    assert not r.validated and r.n_items == 0 and r.n_independent == 0


def test_min_families_param_is_honored():
    items = [_reddit_item("CELH"), _trends_item("CELH"), _news_item("CELH", "CNBC")]
    assert validate_convergence(items, min_families=3).validated
    assert not validate_convergence(items, min_families=4).validated


def test_as_evidence_is_json_shaped():
    items = [_reddit_item("CELH"), _trends_item("CELH")]
    ev = validate_convergence(items).as_evidence()
    assert ev["validated"] is True
    assert ev["n_independent"] == 2
    assert ev["families"] == ["google_trends", "reddit"]
    assert ev["min_families"] == CONVERGENCE_MIN_FAMILIES
    assert isinstance(ev["families"], list) and isinstance(ev["shared_upstream_collapsed"], list)


def test_result_is_frozen():
    r = validate_convergence([_reddit_item("CELH")])
    assert isinstance(r, ConvergenceResult)
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.validated = True  # type: ignore[misc]
