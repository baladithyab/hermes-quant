---
title: equity_historical.py
id: equity_historicalpy-3
tags:
- openbb
created: '2026-06-17T23:52:43.387575Z'
source: https://raw.githubusercontent.com/OpenBB-finance/OpenBB/main/openbb_platform/core/openbb_core/provider/standard_models/equity_historical.py
source_domain: raw.githubusercontent.com
fetched_at: '2026-06-17T23:52:43.387428Z'
fetch_provider: builtin
status: draft
type: note
tier: unknown
content_type: unknown
deprecated: false
---

"""Equity Historical Price Standard Model."""

from datetime import (
    date as dateType,
    datetime,
)

from openbb_core.provider.abstract.data import Data
from openbb_core.provider.abstract.query_params import QueryParams
from openbb_core.provider.utils.descriptions import (
    DATA_DESCRIPTIONS,
    QUERY_DESCRIPTIONS,
)
from pydantic import Field, field_validator

class EquityHistoricalQueryParams(QueryParams):
    """Equity Historical Price Query."""

    symbol: str = Field(description=QUERY_DESCRIPTIONS.get("symbol", ""))
    start_date: dateType | None = Field(
        default=None,
        description=QUERY_DESCRIPTIONS.get("start_date", ""),
    )
    end_date: dateType | None = Field(
        default=None,
        description=QUERY_DESCRIPTIONS.get("end_date", ""),
    )

    @field_validator("symbol", mode="before", check_fields=False)
    @classmethod
    def to_upper(cls, v: str) -> str:
        """Convert field to uppercase."""
        return v.upper()

class EquityHistoricalData(Data):
    """Equity Historical Price Data."""

    date: dateType | datetime = Field(description=DATA_DESCRIPTIONS.get("date", ""))
    open: float = Field(description=DATA_DESCRIPTIONS.get("open", ""))
    high: float = Field(description=DATA_DESCRIPTIONS.get("high", ""))
    low: float = Field(description=DATA_DESCRIPTIONS.get("low", ""))
    close: float = Field(description=DATA_DESCRIPTIONS.get("close", ""))
    volume: float | int | None = Field(
        default=None, description=DATA_DESCRIPTIONS.get("volume", "")
    )
    vwap: float | None = Field(
        default=None, description=DATA_DESCRIPTIONS.get("vwap", "")
    )

    @field_validator("date", mode="before", check_fields=False)
    @classmethod
    def date_validate(cls, v):
        """Return formatted datetime."""
        # pylint: disable=import-outside-toplevel
        from dateutil import parser

        if ":" in str(v):
            return parser.isoparse(str(v))
        return parser.parse(str(v)).date()