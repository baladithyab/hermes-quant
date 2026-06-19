---
title: balance_sheet.py
id: balance_sheetpy
tags:
- openbb
created: '2026-06-17T23:54:04.281532Z'
source: https://raw.githubusercontent.com/OpenBB-finance/OpenBB/main/openbb_platform/core/openbb_core/provider/standard_models/balance_sheet.py
source_domain: raw.githubusercontent.com
fetched_at: '2026-06-17T23:54:04.281418Z'
fetch_provider: builtin
status: draft
type: note
tier: unknown
content_type: unknown
deprecated: false
---

"""Balance Sheet Standard Model."""

from datetime import date as dateType

from openbb_core.provider.abstract.data import Data
from openbb_core.provider.abstract.query_params import QueryParams
from openbb_core.provider.utils.descriptions import (
    QUERY_DESCRIPTIONS,
)
from pydantic import Field, NonNegativeInt, field_validator

class BalanceSheetQueryParams(QueryParams):
    """Balance Sheet Query."""

    symbol: str = Field(description=QUERY_DESCRIPTIONS.get("symbol", ""))
    limit: NonNegativeInt | None = Field(
        default=None, description=QUERY_DESCRIPTIONS.get("limit", "")
    )

    @field_validator("symbol", mode="before", check_fields=False)
    @classmethod
    def to_upper(cls, v: str):
        """Convert field to uppercase."""
        return v.upper()

class BalanceSheetData(Data):
    """Balance Sheet Data."""

    period_ending: dateType = Field(description="The end date of the reporting period.")
    fiscal_period: str | None = Field(
        description="The fiscal period of the report.", default=None
    )
    fiscal_year: int | None = Field(
        description="The fiscal year of the fiscal period.", default=None
    )