"""手续费对账与做市收益：本金/手续费分列、可从流水重推、用户分成守恒。

覆盖业务：
- 池子维度：两侧累计手续费、成交笔数、成交总量；
- 用户维度：份额占比、应得手续费分成、累计贡献；
- 舍入守恒：fee + effective_in == amount_in，分成总和不超过池子实收；
- 并发：并发成交后统计与流水重推结果最终一致；
- 幂等：重复请求只计一次手续费；被拒绝的请求不产生手续费。
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from amm_service import stats as stats_mod
from amm_service.errors import SlippageExceeded

log = logging.getLogger("test.fees_stats")

FEE_PPM = 3000


async def _seeded(service, rx=1_000_000, ry=1_000_000):
    await service.seed_pool("p", "X", "Y", rx, ry, fee_ppm=FEE_PPM)


@pytest.mark.asyncio
async def test_fee_split_never_creates_or_destroys_assets(service):
    """舍入口径：fee + effective_in == amount_in 对任意输入成立。"""
    for amount_in in (1, 2, 3, 7, 999, 1000, 123_456_789):
        fee = stats_mod.fee_for_swap(amount_in, FEE_PPM)
        effective = amount_in * (1_000_000 - FEE_PPM) // 1_000_000
        assert fee >= 0 and fee <= amount_in
        assert fee + effective == amount_in
    log.info("fee rounding conserved: fee+effective==amount_in for all samples")


@pytest.mark.asyncio
async def test_pool_stats_distinguish_principal_and_fees(service):
    """本金（净存入）与手续费分列；统计与流水重推一致。"""
    await _seeded(service)
    await service.add_liquidity(request_id="a1", pool_id="p", user="alice",
                                amount_x=100_000, amount_y=100_000)
    await service.swap(request_id="s1", pool_id="p", user="bob",
                       side="x_to_y", amount_in=10_000, min_out=0)
    await service.swap(request_id="s2", pool_id="p", user="carol",
                       side="y_to_x", amount_in=20_000, min_out=0)

    st = await service.get_pool_stats("p")
    # 手续费口径：fee = amount_in - floor(amount_in*(1e6-f)/1e6)
    exp_fee_x = stats_mod.fee_for_swap(10_000, FEE_PPM)
    exp_fee_y = stats_mod.fee_for_swap(20_000, FEE_PPM)
    assert st["fee_x"] == exp_fee_x > 0
    assert st["fee_y"] == exp_fee_y > 0
    assert st["swap_count"] == 2
    assert st["volume_in_x"] == 10_000 and st["volume_in_y"] == 20_000
    # 本金：seed 不计入流水，注入流水只有 alice 一笔
    assert st["deposited_x"] == 100_000 and st["withdrawn_x"] == 0
    # 库存恒等式：reserve == seed + deposited - withdrawn + vol_in - vol_out
    assert st["reserve_x"] == 1_000_000 + 100_000 + 10_000 - st["volume_out_x"]
    assert st["reserve_y"] == 1_000_000 + 100_000 + 20_000 - st["volume_out_y"]

    # 从流水重新推导，结果必须一致
    rederived = stats_mod.pool_trading_stats(service.store.ledger, "p")
    for k, v in rederived.items():
        assert st[k] == v
    log.info("pool stats expected fee_x=%s fee_y=%s got %s/%s; rederived ok",
             exp_fee_x, exp_fee_y, st["fee_x"], st["fee_y"])


@pytest.mark.asyncio
async def test_user_stats_share_ratio_entitlement_and_contribution(service):
    """用户份额占比/应得手续费/累计贡献；分成总和不超过池子实收。"""
    await _seeded(service)
    await service.add_liquidity(request_id="a1", pool_id="p", user="alice",
                                amount_x=500_000, amount_y=500_000)
    await service.swap(request_id="s1", pool_id="p", user="bob",
                       side="x_to_y", amount_in=30_000, min_out=0)

    alice = await service.get_user_stats("p", "alice")
    system = await service.get_user_stats("p", "_system")
    assert alice["shares"] == 500_000
    assert alice["total_shares"] == 1_500_000
    # 占比：500_000 / 1_500_000 = 333333 ppm（向下取整）
    assert alice["share_ppm"] == 333_333
    assert alice["deposited_x"] == 500_000 and alice["add_count"] == 1

    pool_stats = await service.get_pool_stats("p")
    # 分成按份额折算、向下取整；全体用户分成之和 <= 池子实收手续费
    total_entitled = (
        alice["fee_entitled_x"] + system["fee_entitled_x"]
    )
    assert 0 < total_entitled <= pool_stats["fee_x"]
    assert alice["fee_entitled_x"] == (
        pool_stats["fee_x"] * 500_000 // 1_500_000
    )
    # 非 LP（bob 只兑换）没有分成
    bob = await service.get_user_stats("p", "bob")
    assert bob["shares"] == 0 and bob["fee_entitled_x"] == 0
    log.info(
        "user stats alice share_ppm=%s entitled_x=%s; pool fee_x=%s; sum<=total ok",
        alice["share_ppm"], alice["fee_entitled_x"], pool_stats["fee_x"],
    )


@pytest.mark.asyncio
async def test_rejected_swap_produces_no_fee(service):
    """被拒绝的请求不产生手续费、不计成交笔数。"""
    await _seeded(service)
    with pytest.raises(SlippageExceeded):
        await service.swap(request_id="bad", pool_id="p", user="bob",
                           side="x_to_y", amount_in=10_000, min_out=10**9)
    st = await service.get_pool_stats("p")
    assert st["swap_count"] == 0
    assert st["fee_x"] == 0 and st["fee_y"] == 0
    log.info("rejected swap: fee=0 count=0 as expected")


@pytest.mark.asyncio
async def test_duplicate_request_counts_fee_once(service):
    """同一 request_id 重复提交：手续费与成交笔数只计一次。"""
    await _seeded(service)
    kw = dict(request_id="dup", pool_id="p", user="bob",
              side="x_to_y", amount_in=10_000, min_out=0)
    r1 = await service.swap(**kw)
    r2 = await service.swap(**kw)
    assert r2["replayed"] is True
    st = await service.get_pool_stats("p")
    assert st["swap_count"] == 1
    assert st["fee_x"] == stats_mod.fee_for_swap(10_000, FEE_PPM)
    log.info("duplicate request: swap_count=1 fee counted once")


@pytest.mark.asyncio
async def test_concurrent_swaps_stats_eventually_consistent(service):
    """并发成交：手续费/笔数/成交量最终与流水重推一致，且等于逐笔之和。"""
    await _seeded(service, 10**9, 10**9)
    n = 60

    async def one(i: int) -> None:
        await service.swap(
            request_id=f"cs-{i}", pool_id="p", user=f"u{i}",
            side="x_to_y" if i % 2 == 0 else "y_to_x",
            amount_in=1000 + i, min_out=0,
        )

    await asyncio.gather(*(one(i) for i in range(n)))
    st = await service.get_pool_stats("p")
    assert st["swap_count"] == n
    expect_fee_x = sum(
        stats_mod.fee_for_swap(1000 + i, FEE_PPM) for i in range(0, n, 2)
    )
    expect_fee_y = sum(
        stats_mod.fee_for_swap(1000 + i, FEE_PPM) for i in range(1, n, 2)
    )
    assert st["fee_x"] == expect_fee_x
    assert st["fee_y"] == expect_fee_y
    # 与流水重推一致
    rederived = stats_mod.pool_trading_stats(service.store.ledger, "p")
    for k, v in rederived.items():
        assert st[k] == v
    log.info(
        "concurrent n=%s fee_x=%s fee_y=%s count=%s consistent with ledger",
        n, st["fee_x"], st["fee_y"], st["swap_count"],
    )
