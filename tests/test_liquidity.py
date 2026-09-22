"""流动性注入/赎回：份额口径、边界、拒绝原因、幂等。"""
from __future__ import annotations

import logging

import pytest

from amm_service.errors import (
    DuplicateRequestConflict,
    InsufficientShareBalance,
    InvalidParameter,
    RequestExpired,
    SlippageExceeded,
)

log = logging.getLogger("test.liquidity")


@pytest.mark.asyncio
async def test_first_deposit_requires_balanced_amounts(service):
    await service.seed_pool("p", "X", "Y", 0, 0)
    with pytest.raises(InvalidParameter) as ei:
        await service.add_liquidity(
            request_id="a1", pool_id="p", user="bob",
            amount_x=100, amount_y=120,
        )
    assert ei.value.code == "INVALID_PARAMETER"
    log.info("first-deposit-unbalanced rejected code=%s (no state applied)",
             ei.value.code)
    pool = await service.get_pool("p")
    assert pool["total_shares"] == 0
    assert pool["reserve_x"] == 0 and pool["reserve_y"] == 0


@pytest.mark.asyncio
async def test_deposit_withdrawal_roundtrip_and_share_conservation(service):
    await service.seed_pool("p", "X", "Y", 0, 0)
    r1 = await service.add_liquidity(
        request_id="a1", pool_id="p", user="bob", amount_x=100_000, amount_y=100_000
    )
    assert r1["shares"] == 100_000
    log.info("after first deposit pool=%s", r1["pool"])

    r2 = await service.add_liquidity(
        request_id="a2", pool_id="p", user="carol", amount_x=50_000, amount_y=50_000
    )
    assert r2["shares"] == 50_000

    # bob 赎回一半：严格按比例
    w = await service.remove_liquidity(
        request_id="w1", pool_id="p", user="bob", shares=50_000
    )
    log.info("withdraw half result=%s pool=%s",
             {k: w[k] for k in ("amount_x", "amount_y", "remaining_shares")}, w["pool"])
    assert w["amount_x"] == 50_000
    assert w["amount_y"] == 50_000
    assert w["remaining_shares"] == 50_000

    # 持仓总和 == 总份额（份额守恒）
    pb = await service.get_position("p", "bob")
    pc = await service.get_position("p", "carol")
    pool = await service.get_pool("p")
    assert pb["shares"] + pc["shares"] == pool["total_shares"]


@pytest.mark.asyncio
async def test_uneven_deposit_mints_conservative_shares(service):
    await service.seed_pool("p", "X", "Y", 100_000, 100_000)
    before = await service.get_pool("p")
    # 投入 x=10k(10%), y=20k(20%)：按较小侧仅铸 10k 份额，y 多出部分捐赠留存
    r = await service.add_liquidity(
        request_id="a9", pool_id="p", user="dave",
        amount_x=10_000, amount_y=20_000,
    )
    after = await service.get_pool("p")
    log.info("uneven deposit: before=%s after=%s minted=%s (min-ratio rule)",
             before, after, r["shares"])
    assert r["shares"] == 10_000
    assert after["reserve_x"] == 110_000
    assert after["reserve_y"] == 120_000


@pytest.mark.asyncio
async def test_over_redeem_is_rejected_without_partial_effect(service):
    await service.seed_pool("p", "X", "Y", 0, 0)
    await service.add_liquidity(
        request_id="a1", pool_id="p", user="bob", amount_x=1000, amount_y=1000
    )
    before = await service.get_pool("p")
    with pytest.raises(InsufficientShareBalance) as ei:
        await service.remove_liquidity(
            request_id="r1", pool_id="p", user="bob", shares=10_000
        )
    assert ei.value.code == "INSUFFICIENT_SHARE_BALANCE"
    after = await service.get_pool("p")
    log.info("over-redeem rejected code=%s before=%s after=%s (identical)",
             ei.value.code, before, after)
    assert before == after
    pos = await service.get_position("p", "bob")
    assert pos["shares"] == 1000


@pytest.mark.asyncio
async def test_min_shares_slippage_rejection(service):
    await service.seed_pool("p", "X", "Y", 0, 0)
    with pytest.raises(SlippageExceeded) as ei:
        await service.add_liquidity(
            request_id="a1", pool_id="p", user="bob",
            amount_x=1000, amount_y=1000, min_shares=1_000_000,
        )
    assert ei.value.code == "SLIPPAGE_EXCEEDED"


@pytest.mark.asyncio
async def test_expired_request_rejected(service, clock):
    await service.seed_pool("p", "X", "Y", 0, 0)
    with pytest.raises(RequestExpired) as ei:
        await service.add_liquidity(
            request_id="a1", pool_id="p", user="bob",
            amount_x=1, amount_y=1, deadline_ms=clock.t - 1,
        )
    assert ei.value.code == "REQUEST_EXPIRED"


@pytest.mark.asyncio
async def test_repeated_add_is_idempotent_and_conflict_distinguishable(service):
    await service.seed_pool("p", "X", "Y", 0, 0)
    kw = dict(request_id="dup", pool_id="p", user="bob",
              amount_x=1000, amount_y=1000)
    r1 = await service.add_liquidity(**kw)
    r2 = await service.add_liquidity(**kw)
    assert r1["shares"] == r2["shares"] == 1000
    assert r2["replayed"] is True
    pool = await service.get_pool("p")
    assert pool["total_shares"] == 1000  # 没有重复铸造
    log.info("duplicate add replayed consistently: shares=%s pool=%s",
             r2["shares"], pool)

    # 同一 request_id 换参数 => 冲突拒绝
    with pytest.raises(DuplicateRequestConflict) as ei:
        await service.add_liquidity(
            request_id="dup", pool_id="p", user="bob",
            amount_x=2000, amount_y=2000,
        )
    assert ei.value.code == "DUPLICATE_REQUEST_CONFLICT"
    # 冲突拒绝不得改动状态
    assert (await service.get_pool("p"))["total_shares"] == 1000


@pytest.mark.asyncio
async def test_seed_validation(service):
    with pytest.raises(InvalidParameter):
        await service.seed_pool("p", "X", "X", 1, 1)
    with pytest.raises(InvalidParameter):
        await service.seed_pool("p2", "X", "Y", 100, 0)
