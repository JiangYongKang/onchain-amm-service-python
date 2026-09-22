"""HTTP 接口的请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class SeedPoolRequest(BaseModel):
    pool_id: str
    token_x: str
    token_y: str
    reserve_x: int = Field(ge=0)
    reserve_y: int = Field(ge=0)
    fee_ppm: int = Field(default=3000, ge=0, le=1_000_000)


class AddLiquidityRequest(BaseModel):
    request_id: str
    pool_id: str
    user: str
    amount_x: int = Field(ge=0)
    amount_y: int = Field(ge=0)
    min_shares: int = Field(default=0, ge=0)
    deadline_ms: int = Field(default=0, ge=0)


class RemoveLiquidityRequest(BaseModel):
    request_id: str
    pool_id: str
    user: str
    shares: int = Field(ge=0)
    min_x: int = Field(default=0, ge=0)
    min_y: int = Field(default=0, ge=0)
    deadline_ms: int = Field(default=0, ge=0)


class SwapRequest(BaseModel):
    request_id: str
    pool_id: str
    user: str
    side: str
    amount_in: int = Field(ge=0)
    min_out: int = Field(ge=0)
    quoted_out: int | None = Field(default=None, ge=0)
    quote_version: int | None = Field(default=None, ge=0)
    deadline_ms: int = Field(default=0, ge=0)


class QuoteRequest(BaseModel):
    pool_id: str
    side: str
    amount_in: int = Field(ge=0)
