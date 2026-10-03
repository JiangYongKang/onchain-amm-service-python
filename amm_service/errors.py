"""业务错误类型与可区分失败原因（错误码稳定，供 API 层与测试断言）。"""
from __future__ import annotations


class AmmError(Exception):
    """所有业务失败的基类，携带稳定可区分的错误码。"""

    code: str = "AMM_ERROR"

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class PoolNotFound(AmmError):
    code = "POOL_NOT_FOUND"


class InvalidParameter(AmmError):
    code = "INVALID_PARAMETER"


class RequestExpired(AmmError):
    code = "REQUEST_EXPIRED"


class StaleQuote(AmmError):
    code = "STALE_QUOTE"


class InsufficientLiquidity(AmmError):
    code = "INSUFFICIENT_LIQUIDITY"


class SlippageExceeded(AmmError):
    code = "SLIPPAGE_EXCEEDED"


class InsufficientShareBalance(AmmError):
    code = "INSUFFICIENT_SHARE_BALANCE"


class DuplicateRequestConflict(AmmError):
    code = "DUPLICATE_REQUEST_CONFLICT"


class PoolAlreadyExists(AmmError):
    """重复初始化同一池子且参数与既有池子不一致。"""

    code = "POOL_ALREADY_EXISTS"


class SwapAmountExceedsLimit(AmmError):
    """单笔兑换输入超过池子容量上限。"""

    code = "SWAP_AMOUNT_EXCEEDS_LIMIT"


class ReserveBufferViolation(AmmError):
    """成交后输出侧库存将跌破池子缓冲水位。"""

    code = "RESERVE_BUFFER_VIOLATION"


class PoolPaused(AmmError):
    """池子的对应操作已被管理员暂停。"""

    code = "POOL_PAUSED"
