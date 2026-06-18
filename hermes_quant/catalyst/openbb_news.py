"""hermes_quant.catalyst.openbb_news — OpenBB news as an ALTERNATIVE feed (ob4).

ADR-0100 (ob4): an additional headline source for the catalyst
classify -> propagate -> synthesize pipeline. ``obb.news.*`` (company / world
news) rows are mapped into the SAME ``CatalystItem`` shape ``catalyst.ingest``
produces, so the downstream ``classify_headline`` / ``propagate`` / synthesize
path consumes OpenBB news identically to the Google-News RSS feed. This does
NOT replace the existing feed — it is an ADDITIONAL source.

NO-LOOKAHEAD (the cardinal rail)
--------------------------------
Each news row carries its publication instant (``date`` / ``published_at``),
which becomes ``CatalystItem.published_at`` — the packet ``asof`` fidelity
anchor. The read is WINDOW-PINNED on ``as_of``: a story published AFTER asof is
DROPPED (publishing at-or-before asof is the bound). A row with NO parseable
publish timestamp is DROPPED (it can't anchor a packet, mirroring
``ingest.parse_gn_rss``) — never defaulted to now().

DEFAULT-OFF
-----------
Gated on ``HERMES_QUANT_OPENBB`` (default ``'0'``) — the OpenBB news source
rides ob1's OpenBB enablement toggle. With the flag unset NO openbb import
happens (lazy import lives ONLY inside the ``obb`` property) and the catalyst
pipeline is unchanged — byte-identical no-op for a venv without openbb.

SILENCE-BY-DEFAULT vs FAIL-CLOSED
---------------------------------
Like the existing free-feed ingesters, a dead/erroring OpenBB news feed is
NON-FATAL: ``ingest_openbb_news`` returns ``([], latency)`` rather than crashing
the daily run (it is an ADDITIONAL source — the primary feed must still run).
The flag-off / openbb-missing conditions are surfaced via the ``obb`` property's
clear ``DataProviderError`` only when a fetch is actually attempted with the
flag on.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from hermes_quant.catalyst.ingest import CatalystItem, dedupe_items
from hermes_quant.protocol import DataProviderError

logger = logging.getLogger(__name__)

# Reuse ob1's OpenBB vendor flag (default-OFF). A quoted-literal default so the
# flag-inventory scanner counts it.
OPENBB_ENABLE_FLAG = "HERMES_QUANT_OPENBB"


def _openbb_flag_enabled() -> bool:
    """True iff HERMES_QUANT_OPENBB is set truthy (default-OFF)."""
    return os.environ.get(OPENBB_ENABLE_FLAG, "0") not in ("", "0", "false", "False")


def _parse_published(s: Any) -> datetime | None:
    """Parse an OpenBB news publish timestamp to tz-aware UTC; None on failure.

    OpenBB returns ISO-8601 strings (or already-``datetime`` values). A naive
    result is assumed UTC only as an explicit last resort. Returns None on any
    unparseable value (the row is then dropped — never defaulted to now()).
    """
    if s is None:
        return None
    if isinstance(s, datetime):
        dt: datetime | None = s
    else:
        text = str(s).strip()
        if not text:
            return None
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            dt = None
        if dt is None:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
    if dt is None:
        return None
    if dt.tzinfo is None:  # naive -> assume UTC (made explicit)
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class OpenBBNews:
    """OpenBB news -> CatalystItem adapter (ob4, ADR-0100).

    ``fetch(query, as_of)`` routes ``obb.news.company`` (symbol-scoped) or
    ``obb.news.world`` (broad), maps the rows to ``CatalystItem``s, and applies
    the ``as_of`` no-lookahead window (published <= as_of). The result feeds the
    SAME catalyst classify -> propagate -> synthesize path as the RSS feed.

    DEFAULT-OFF: gated on ``HERMES_QUANT_OPENBB``. With the flag unset the obb
    SDK is never imported; a fetch attempt fails closed at the ``obb`` property.

    Args:
        obb: test seam — an object exposing ``.news.company(...)`` /
            ``.news.world(...)`` (returns an OBBject with ``.to_dataframe()`` /
            ``.results``, a DataFrame, or a list of row dicts). When None the
            real ``openbb.obb`` is lazy-imported on first fetch (flag-gated).
        require_flag: if True (default), fetches require ``HERMES_QUANT_OPENBB``
            truthy. Set False only in offline tests injecting ``obb`` directly.
    """

    name = "openbb_news"

    def __init__(self, *, obb: Any = None, require_flag: bool = True):
        # Injected seam (None -> lazy real import). NEVER import openbb here.
        self._obb: Any = obb
        self._require_flag = require_flag

    @property
    def obb(self) -> Any:
        """Lazy-resolve the OpenBB SDK client (fail-closed when flag off)."""
        if self._require_flag and not _openbb_flag_enabled():
            raise DataProviderError(
                f"OpenBB news source is disabled; set {OPENBB_ENABLE_FLAG}=1 to "
                "enable (default-OFF per ADR-0100)."
            )
        if self._obb is None:
            try:
                from openbb import obb as _obb  # lazy: optional heavy dep
            except ImportError as e:
                raise DataProviderError(
                    "openbb not installed but HERMES_QUANT_OPENBB is set; "
                    "install hermes-quant[openbb] (pip install 'hermes-quant[openbb]')."
                ) from e
            self._obb = _obb
        return self._obb

    def fetch(
        self,
        query: str,
        *,
        symbol: str | None = None,
        as_of: datetime | None = None,
    ) -> list[CatalystItem]:
        """Fetch OpenBB news as CatalystItems, window-pinned to ``as_of``.

        Routes ``obb.news.company(symbol=...)`` when ``symbol`` is given, else
        ``obb.news.world()`` for the broad feed. Maps each row to a
        ``CatalystItem`` and DROPS any story whose publish instant is after
        ``as_of`` (no-lookahead) or unparseable (can't anchor a packet).

        Raises:
            DataProviderError: flag off, openbb missing, or transient API error
                (the public ``ingest_openbb_news`` wrapper catches these so a
                dead feed is non-fatal).
        """
        if symbol:
            resp = self.obb.news.company(symbol=symbol)
        else:
            resp = self.obb.news.world()
        rows = self._to_rows(resp)
        return self._map_items(rows, query=query, as_of=as_of)

    @staticmethod
    def _to_rows(resp: Any) -> list[dict]:
        """Coerce an OBBject / DataFrame / list to a list of row dicts."""
        if resp is None:
            return []
        if isinstance(resp, list):
            return [r for r in resp if isinstance(r, dict)]
        # DataFrame
        try:
            import pandas as pd

            if isinstance(resp, pd.DataFrame):
                return resp.to_dict("records")
        except ImportError:  # pragma: no cover - pandas is a hard dep
            pass
        to_df = getattr(resp, "to_dataframe", None)
        if callable(to_df):
            return to_df().to_dict("records")
        results = getattr(resp, "results", None)
        if results is not None:
            return [dict(r) for r in results]
        raise DataProviderError(
            "openbb news response is neither a list, DataFrame, nor an OBBject "
            "with .to_dataframe()/.results"
        )

    @staticmethod
    def _map_items(
        rows: list[dict], *, query: str, as_of: datetime | None
    ) -> list[CatalystItem]:
        """Map OpenBB news rows to CatalystItems with the as_of no-lookahead window."""
        cutoff = None
        if as_of is not None:
            cutoff = as_of if as_of.tzinfo is not None else as_of.replace(tzinfo=timezone.utc)
            cutoff = cutoff.astimezone(timezone.utc)

        items: list[CatalystItem] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            lm = {str(k).lower(): v for k, v in r.items()}
            title = str(lm.get("title") or "").strip()
            pub = _parse_published(
                lm.get("date")
                if lm.get("date") is not None
                else lm.get("published_at")
                if lm.get("published_at") is not None
                else lm.get("published")
            )
            if not title or pub is None:
                # No title or no parseable timestamp -> cannot anchor a packet.
                continue
            # NO-LOOKAHEAD: a story published AFTER asof is not knowable -> DROP.
            if cutoff is not None and pub > cutoff:
                continue
            link = str(lm.get("url") or lm.get("link") or "").strip()
            source = str(lm.get("source") or lm.get("provider") or "openbb").strip()
            items.append(
                CatalystItem(
                    title=title,
                    published_at=pub,
                    source=source,
                    link=link,
                    query=query,
                )
            )
        return items


def ingest_openbb_news(
    query: str,
    *,
    symbol: str | None = None,
    as_of: datetime | None = None,
    obb: Any = None,
    require_flag: bool = True,
    dedupe: bool = True,
) -> tuple[list[CatalystItem], float]:
    """Ingest OpenBB news for ``query`` as CatalystItems. Returns (items, latency).

    Mirrors ``catalyst.ingest.ingest_query``'s contract: a dead/erroring feed is
    NON-FATAL — returns ``([], latency)`` rather than crashing the daily run (an
    ADDITIONAL source must not break the primary feed). The result is dedup'd
    (earliest-published copy survives, lookahead-honest) so it can be merged with
    the RSS feed before classify -> propagate -> synthesize.

    The flag-off / openbb-missing conditions raise inside ``OpenBBNews.fetch``;
    here they are caught (non-fatal) — the read simply yields no items when the
    OpenBB source is disabled, leaving the existing feed and pipeline unchanged.
    """
    news = OpenBBNews(obb=obb, require_flag=require_flag)
    t0 = time.monotonic()
    try:
        items = news.fetch(query, symbol=symbol, as_of=as_of)
    except Exception as e:  # noqa: BLE001 - feed/flag failure is non-fatal
        logger.warning("catalyst.openbb_news: fetch failed for %r: %s", query, e)
        return [], time.monotonic() - t0
    latency = time.monotonic() - t0
    if dedupe:
        items = dedupe_items(items)
    return items, latency


__all__ = [
    "OpenBBNews",
    "ingest_openbb_news",
    "OPENBB_ENABLE_FLAG",
]
