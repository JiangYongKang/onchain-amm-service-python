"""HTTP 接口层：把资金池能力挂到 FastAPI 应用上。

时钟可注入：默认取 wall clock（毫秒），测试可传固定时钟以复现过期判定。
"""

import logging
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.errors import ErrorCode, PoolError
from app.models import (
    AddLiquidityRequest,
    OperationResult,
    PoolState,
    RemoveLiquidityRequest,
    SwapRequest,
)
from app.pool import PoolService

logger = logging.getLogger("amm")


def _ms_clock() -> int:
    return time.time_ns() // 1_000_000


def _log_result(prefix: str, req: object, result: OperationResult) -> None:
    """打印请求输入、池子前后状态与判定依据，便于测试与审计复现。"""
    logger.info(
        "%s | request=%s | before=%s | after=%s | basis=%s",
        prefix,
        req.model_dump_json(),
        result.pool_before.model_dump_json(),
        result.pool_after.model_dump_json(),
        result.reason,
    )


def create_app(service: PoolService | None = None, clock=_ms_clock) -> FastAPI:
    service = service or PoolService()
    app = FastAPI(title="本地做市资金池服务", version="0.1.0")

    @app.exception_handler(PoolError)
    async def _pool_error_handler(_request: Request, exc: PoolError):
        logger.info("拒绝 | code=%s | message=%s", exc.code.value, exc.message)
        status_code = 404 if exc.code is ErrorCode.POOL_NOT_FOUND else 409
        return JSONResponse(
            status_code=status_code,
            content={"error": {"code": exc.code.value, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(_request: Request, exc: RequestValidationError):
        logger.info("拒绝 | code=%s | detail=%s", ErrorCode.INVALID_PARAMETER.value, exc.errors())
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": ErrorCode.INVALID_PARAMETER.value,
                    "message": "请求参数非法",
                    "detail": exc.errors(),
                }
            },
        )

    @app.get("/")
    def read_root():
        return {"service": "local-amm", "status": "ok"}

    @app.get("/pools/{pool_id}", response_model=PoolState)
    def get_pool(pool_id: str):
        return service.get_state(pool_id)

    @app.post("/pools/add-liquidity", response_model=OperationResult)
    def add_liquidity(req: AddLiquidityRequest):
        result = service.add_liquidity(req)
        _log_result("注入流动性成功", req, result)
        return result

    @app.post("/pools/remove-liquidity", response_model=OperationResult)
    def remove_liquidity(req: RemoveLiquidityRequest):
        result = service.remove_liquidity(req)
        _log_result("赎回流动性成功", req, result)
        return result

    @app.post("/pools/swap", response_model=OperationResult)
    def swap(req: SwapRequest):
        now_ms = clock()
        result = service.swap(req, now_ms=now_ms)
        _log_result(f"兑换成功 now_ms={now_ms}", req, result)
        return result

    return app
