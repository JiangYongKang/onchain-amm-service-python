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
    code = "POOL_ALREADY_EXISTS"


class PoolPaused(AmmError):
    code = "POOL_PAUSED"


class AmountLimitExceeded(AmmError):
    code = "AMOUNT_LIMIT_EXCEEDED"


class ReserveBufferBreach(AmmError):
    code = "RESERVE_BUFFER_BREACH"
