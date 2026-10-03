"""FastAPI 路由：把对外 HTTP 接口映射到 AmmService，并统一错误响应。"""
from __future__ import annotations

from fastapi import APIRouter

from .schemas import (
    AddLiquidityRequest,
    PauseRequest,
    PoolPolicyRequest,
    QuoteRequest,
    RemoveLiquidityRequest,
    SeedPoolRequest,
    SwapRequest,
)
from .service import AmmService

router = APIRouter()
service = AmmService()


@router.post("/admin/pools")
async def seed_pool(req: SeedPoolRequest) -> dict:
    result = await service.seed_pool(
        pool_id=req.pool_id, token_x=req.token_x, token_y=req.token_y,
        reserve_x=req.reserve_x, reserve_y=req.reserve_y, fee_ppm=req.fee_ppm,
    )
    return {"ok": True, "result": result}


@router.post("/liquidity/add")
async def add_liquidity(req: AddLiquidityRequest) -> dict:
    result = await service.add_liquidity(
        request_id=req.request_id, pool_id=req.pool_id, user=req.user,
        amount_x=req.amount_x, amount_y=req.amount_y,
        min_shares=req.min_shares, deadline_ms=req.deadline_ms,
    )
    return {"ok": True, "result": result}


@router.post("/liquidity/remove")
async def remove_liquidity(req: RemoveLiquidityRequest) -> dict:
    result = await service.remove_liquidity(
        request_id=req.request_id, pool_id=req.pool_id, user=req.user,
        shares=req.shares, min_x=req.min_x, min_y=req.min_y,
        deadline_ms=req.deadline_ms,
    )
    return {"ok": True, "result": result}


@router.post("/quotes")
async def quote(req: QuoteRequest) -> dict:
    result = await service.quote(
        pool_id=req.pool_id, side=req.side, amount_in=req.amount_in
    )
    return {"ok": True, "result": result}


@router.post("/swaps")
async def swap(req: SwapRequest) -> dict:
    result = await service.swap(
        request_id=req.request_id, pool_id=req.pool_id, user=req.user,
        side=req.side, amount_in=req.amount_in, min_out=req.min_out,
        quoted_out=req.quoted_out, quote_version=req.quote_version,
        deadline_ms=req.deadline_ms,
    )
    return {"ok": True, "result": result}


@router.get("/pools/{pool_id}")
async def get_pool(pool_id: str) -> dict:
    return {"ok": True, "result": await service.get_pool(pool_id)}


@router.get("/pools/{pool_id}/positions/{user}")
async def get_position(pool_id: str, user: str) -> dict:
    return {"ok": True, "result": await service.get_position(pool_id, user)}


@router.post("/admin/reset")
async def reset() -> dict:
    await service.reset()
    return {"ok": True}


# --------------------------------------------------------------------- #
# 运营策略：容量上限 / 缓冲水位 / 熔断
# --------------------------------------------------------------------- #
@router.put("/admin/pools/{pool_id}/policy")
async def set_pool_policy(pool_id: str, req: PoolPolicyRequest) -> dict:
    result = await service.set_pool_policy(
        pool_id, max_swap_in=req.max_swap_in, reserve_buffer=req.reserve_buffer,
    )
    return {"ok": True, "result": result}


@router.get("/admin/pools/{pool_id}/policy")
async def get_pool_policy(pool_id: str) -> dict:
    return {"ok": True, "result": await service.get_pool_policy(pool_id)}


@router.post("/admin/pools/{pool_id}/pause")
async def pause_pool(pool_id: str, req: PauseRequest) -> dict:
    return {"ok": True, "result": await service.pause_pool(pool_id, req.ops)}


@router.post("/admin/pools/{pool_id}/resume")
async def resume_pool(pool_id: str, req: PauseRequest) -> dict:
    return {"ok": True, "result": await service.resume_pool(pool_id, req.ops)}


# --------------------------------------------------------------------- #
# 流水历史与手续费对账
# --------------------------------------------------------------------- #
@router.get("/history")
async def get_history(pool_id: str | None = None, user: str | None = None,
                      op: str | None = None, since_ms: int | None = None,
                      until_ms: int | None = None,
                      page: int = 1, page_size: int = 50) -> dict:
    result = await service.get_history(
        pool_id=pool_id, user=user, op=op,
        since_ms=since_ms, until_ms=until_ms,
        page=page, page_size=page_size,
    )
    return {"ok": True, "result": result}


@router.get("/pools/{pool_id}/stats")
async def get_pool_stats(pool_id: str) -> dict:
    return {"ok": True, "result": await service.get_pool_stats(pool_id)}


@router.get("/pools/{pool_id}/users/{user}/stats")
async def get_user_stats(pool_id: str, user: str) -> dict:
    return {"ok": True, "result": await service.get_user_stats(pool_id, user)}
