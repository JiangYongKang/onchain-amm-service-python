"""业务错误类型与可区分的失败原因编码。"""

from enum import Enum


class ErrorCode(str, Enum):
    """所有可对外暴露的失败原因，每种拒绝路径对应一个独立编码。"""

    POOL_NOT_FOUND = "POOL_NOT_FOUND"
    INVALID_PARAMETER = "INVALID_PARAMETER"
    POOL_NOT_INITIALIZED = "POOL_NOT_INITIALIZED"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    SLIPPAGE = "SLIPPAGE"
    REQUEST_EXPIRED = "REQUEST_EXPIRED"
    INSUFFICIENT_SHARES = "INSUFFICIENT_SHARES"
    STALE_QUOTE = "STALE_QUOTE"
    DUPLICATE_IDEMPOTENCY_CONFLICT = "DUPLICATE_IDEMPOTENCY_CONFLICT"


class PoolError(Exception):
    """业务规则违反时抛出，携带稳定的错误码与可读说明。"""

    def __init__(self, code: ErrorCode, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code.value}] {message}")
