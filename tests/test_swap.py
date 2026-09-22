"""兑换：成交保护四要素、失败原因区分、不变量、幂等回放。"""
from __future__ import annotations

import logging

import pytest

from amm_service.errors import (
    DuplicateRequestConflict,
    InsufficientLiquidity,
    InvalidParameter,
    PoolNotFound,
    RequestExpired,
    SlippageExceeded,
    StaleQuote,
)

log = logging.getLogger("test.swap")


def _k(pool: dict) -> int:
    return pool["reserve_x"] * pool["reserve_y"]


@pytest.mark.asyncio
async def test_swap_success_preserves_invariant_and_logs(service, caplog):
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    before = await service.get_pool("p")
    r = await service.swap(
        request_id="s1", pool_id="p", user="alice",
        side="x_to_y", amount_in=1000, min_out=900,
    )
    after = await service.get_pool("p")
    assert r["amount_out"] == 996
    # 库存变化与输出严格一致
    assert after["reserve_x"] - before["reserve_x"] == 1000
    assert before["reserve_y"] - after["reserve_y"] == 996
    # 恒定乘积不减少（手续费使其严格增大）
    assert _k(after) > _k(before)
    # 日志中必须包含判定依据与前后状态
    assert any("swap" in rec.message and "accepted" in rec.message
               for rec in caplog.records)
    log.info("successful swap input=%s before=%s after=%s reason=constant-product",
             {"amount_in": 1000, "min_out": 900}, before, after)


@pytest.mark.asyncio
async def test_swap_min_out_slippage_is_distinct(service):
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    before = await service.get_pool("p")
    with pytest.raises(SlippageExceeded) as ei:
        await service.swap(
            request_id="s2", pool_id="p", user="alice",
            side="x_to_y", amount_in=1000, min_out=100_000,
        )
    assert ei.value.code == "SLIPPAGE_EXCEEDED"
    assert (await service.get_pool("p")) == before  # 无部分生效
    log.info("min_out rejection: code=%s pool unchanged=%s", ei.value.code, before)


@pytest.mark.asyncio
async def test_swap_expired_is_distinct(service, clock):
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    with pytest.raises(RequestExpired) as ei:
        await service.swap(
            request_id="s3", pool_id="p", user="alice",
            side="x_to_y", amount_in=1000, min_out=0,
            deadline_ms=clock.t - 1,
        )
    assert ei.value.code == "REQUEST_EXPIRED"


@pytest.mark.asyncio
async def test_swap_invalid_params_are_distinct(service):
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    with pytest.raises(InvalidParameter) as ei:
        await service.swap(
            request_id="s4", pool_id="p", user="alice",
            side="north", amount_in=1000, min_out=0,
        )
    assert ei.value.code == "INVALID_PARAMETER"
    with pytest.raises(InvalidParameter):
        await service.swap(
            request_id="s5", pool_id="p", user="alice",
            side="x_to_y", amount_in=0, min_out=0,
        )


@pytest.mark.asyncio
async def test_swap_pool_not_found_and_is_idempotent(service):
    with pytest.raises(PoolNotFound) as ei:
        await service.swap(
            request_id="s6", pool_id="ghost", user="alice",
            side="x_to_y", amount_in=1, min_out=0,
        )
    assert ei.value.code == "POOL_NOT_FOUND"
    # 不存在池子的重复请求同样幂等回放同一错误
    with pytest.raises(PoolNotFound) as ei2:
        await service.swap(
            request_id="s6", pool_id="ghost", user="alice",
            side="x_to_y", amount_in=1, min_out=0,
        )
    assert ei2.value.code == "POOL_NOT_FOUND"


@pytest.mark.asyncio
async def test_insufficient_liquidity_on_empty_output_side(service):
    # 未激活池子（零库存）无法兑换
    await service.seed_pool("p", "X", "Y", 0, 0)
    with pytest.raises(InsufficientLiquidity) as ei:
        await service.swap(
            request_id="s7", pool_id="p", user="alice",
            side="x_to_y", amount_in=1000, min_out=0,
        )
    assert ei.value.code == "INSUFFICIENT_LIQUIDITY"


@pytest.mark.asyncio
async def test_quote_protection_accepts_fresh_quote(service):
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    q = await service.quote(pool_id="p", side="x_to_y", amount_in=1000)
    r = await service.swap(
        request_id="s8", pool_id="p", user="alice", side="x_to_y",
        amount_in=1000, min_out=q["amount_out"],
        quoted_out=q["amount_out"], quote_version=q["pool_version"],
    )
    assert r["amount_out"] == q["amount_out"]


@pytest.mark.asyncio
async def test_quote_protection_rejects_stale_version_and_changed_price(service):
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    q = await service.quote(pool_id="p", side="x_to_y", amount_in=1000)
    # 别人先成交，池子版本/行情变化
    await service.swap(
        request_id="other", pool_id="p", user="bob",
        side="x_to_y", amount_in=500_000, min_out=0,
    )
    before = await service.get_pool("p")
    with pytest.raises(StaleQuote) as ei:
        await service.swap(
            request_id="s9", pool_id="p", user="alice", side="x_to_y",
            amount_in=1000, min_out=0,
            quoted_out=q["amount_out"], quote_version=q["pool_version"],
        )
    assert ei.value.code == "STALE_QUOTE"
    after = await service.get_pool("p")
    assert before == after  # 被拒后状态不变
    log.info("stale quote rejected code=%s quote=%s actual pool unchanged=%s",
             ei.value.code, q, after)


@pytest.mark.asyncio
async def test_swap_idempotent_replay_and_conflict(service):
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    kw = dict(request_id="s10", pool_id="p", user="alice",
              side="x_to_y", amount_in=1000, min_out=0)
    r1 = await service.swap(**kw)
    r2 = await service.swap(**kw)
    assert r1["amount_out"] == r2["amount_out"]
    assert r2["replayed"] is True
    assert r2["pool"]["version"] == r1["pool"]["version"]  # 未二次扣减

    with pytest.raises(DuplicateRequestConflict):
        await service.swap(
            request_id="s10", pool_id="p", user="alice",
            side="y_to_x", amount_in=1000, min_out=0,
        )


@pytest.mark.asyncio
async def test_rejected_swap_replays_same_reason(service):
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    kw = dict(request_id="s11", pool_id="p", user="alice",
              side="x_to_y", amount_in=1000, min_out=10**9)
    for _ in range(2):
        with pytest.raises(SlippageExceeded) as ei:
            await service.swap(**kw)
        assert ei.value.code == "SLIPPAGE_EXCEEDED"
    # 两次拒绝都没有动库存
    pool = await service.get_pool("p")
    assert pool["reserve_x"] == 1_000_000 and pool["reserve_y"] == 1_000_000
    assert pool["version"] == 0
