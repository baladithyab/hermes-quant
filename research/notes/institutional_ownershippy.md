---
title: institutional_ownership.py
id: institutional_ownershippy
tags:
- openbb
created: '2026-06-17T23:53:34.429227Z'
source: https://raw.githubusercontent.com/OpenBB-finance/OpenBB/main/openbb_platform/core/openbb_core/provider/standard_models/institutional_ownership.py
source_domain: raw.githubusercontent.com
fetched_at: '2026-06-17T23:53:34.429071Z'
fetch_provider: builtin
status: draft
type: note
tier: unknown
content_type: unknown
deprecated: false
---

"""Institutional Ownership Standard Model."""

from datetime import date as dateType

from openbb_core.provider.abstract.data import Data
from openbb_core.provider.abstract.query_params import QueryParams
from openbb_core.provider.utils.descriptions import (
    DATA_DESCRIPTIONS,
    QUERY_DESCRIPTIONS,
)
from pydantic import Field, field_validator

class InstitutionalOwnershipQueryParams(QueryParams):
    """Institutional Ownership Query."""

    symbol: str = Field(description=QUERY_DESCRIPTIONS.get("symbol", ""))

    @field_validator("symbol", mode="before", check_fields=False)
    @classmethod
    def to_upper(cls, v: str) -> str:
        """Convert field to uppercase."""
        return v.upper()

class InstitutionalOwnershipData(Data):
    """Institutional Ownership Data."""

    symbol: str = Field(description=DATA_DESCRIPTIONS.get("symbol", ""))
    cik: str | None = Field(
        default=None,
        description=DATA_DESCRIPTIONS.get("cik", ""),
    )
    date: dateType = Field(description=DATA_DESCRIPTIONS.get("date", ""))