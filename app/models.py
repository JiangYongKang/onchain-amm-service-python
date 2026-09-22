"""对外接口的数据模型（请求 / 响应 / 快照）。"""

from pydantic import BaseModel, Field



class PoolState(BaseModel):
    """池子的完整可观测状态。"""

    pool_id: str
    reserve_in: int = 0
    reserve_out: int = 0
    total_shares: int = 0
    balances: dict[str, int] = Field(default_factory=dict)


class AddLiquidityRequest(BaseModel):
    request_id: str = Field(min_length=1)
    pool_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    amount_in: int = Field(ge=0)
    amount_out: int = Field(ge=0)


class RemoveLiquidityRequest(BaseModel):
    request_id: str = Field(min_length=1)
    pool_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    shares: int = Field(ge=0)


class SwapRequest(BaseModel):
    request_id: str = Field(min_length=1)
    pool_id: str = Field(min_length=1)
    trader: str = Field(min_length=1)
    amount_in: int = Field(ge=0)
    min_amount_out: int = Field(ge=0)
    quote_amount_out: int = Field(ge=0)
    quote_valid_until_ms: int = Field(ge=0)


class OperationResult(BaseModel):
    """所有写操作统一返回：幂等成功 / 已存在结果 均落在这里。"""

    request_id: str
    status: str  # "ok"
    kind: str    # add_liquidity / remove_liquidity / swap
    shares_delta: int = 0
    amount_in_consumed: int = 0
    amount_out: int = 0
    pool_before: PoolState
    pool_after: PoolState
    reason: str = ""
