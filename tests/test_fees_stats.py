"""手续费对账与用户统计：本金/手续费分离、逐笔流水可重推、并发一致性。"""
from __future__ import annotations

import asyncio
import logging

import pytest

from amm_service import pricing
from amm_service.errors import SlippageExceeded

log = logging.getLogger("test.fees_stats")


async def _ledger_swaps(service, pool_id: str) -> list[dict]:
    """从流水重推：成功兑换的逐笔明细。"""
    hist = await service.get_history(pool_id=pool_id, op="swap",
                                     status="success", page_size=500)
    return hist["items"]


async def _assert_stats_match_ledger(service, pool_id: str) -> None:
    """核心不变量：池子统计 == 逐笔流水重推结果；库存恒等式成立。"""
    stats = await service.get_pool_stats(pool_id)
    pool = await service.get_pool(pool_id)
    swaps = await _ledger_swaps(service, pool_id)

    fee_x = sum(s["detail"]["fee_in"] for s in swaps
                if s["detail"]["side"] == "x_to_y")
    fee_y = sum(s["detail"]["fee_in"] for s in swaps
                if s["detail"]["side"] == "y_to_x")
    in_x = sum(s["detail"]["amount_in"] for s in swaps
               if s["detail"]["side"] == "x_to_y")
    in_y = sum(s["detail"]["amount_in"] for s in swaps
               if s["detail"]["side"] == "y_to_x")
    out_x = sum(s["detail"]["amount_out"] for s in swaps
                if s["detail"]["side"] == "y_to_x")
    out_y = sum(s["detail"]["amount_out"] for s in swaps
                if s["detail"]["side"] == "x_to_y")

    assert stats["fee_x"] == fee_x and stats["fee_y"] == fee_y
    assert stats["swap_in_x"] == in_x and stats["swap_in_y"] == in_y
    assert stats["swap_out_x"] == out_x and stats["swap_out_y"] == out_y
    assert stats["swap_count_x_to_y"] == sum(
        1 for s in swaps if s["detail"]["side"] == "x_to_y")
    assert stats["swap_count_y_to_x"] == sum(
        1 for s in swaps if s["detail"]["side"] == "y_to_x")

    # 库存恒等式：reserve == 本金净额 + 兑换净流入（不凭空多/少资产）
    assert pool["reserve_x"] == (
        stats["deposited_x"] - stats["withdrawn_x"]
        + stats["swap_in_x"] - stats["swap_out_x"]
    )
    assert pool["reserve_y"] == (
        stats["deposited_y"] - stats["withdrawn_y"]
        + stats["swap_in_y"] - stats["swap_out_y"]
    )


@pytest.mark.asyncio
async def test_fee_accounting_per_side_and_principal(service):
    """业务：两侧兑换后，手续费/笔数/成交量按方向累计，本金与手续费分离。"""
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000, fee_ppm=3000)

    r1 = await service.swap(request_id="s1", pool_id="p", user="a",
                            side="x_to_y", amount_in=10_000, min_out=0)
    r2 = await service.swap(request_id="s2", pool_id="p", user="b",
                            side="y_to_x", amount_in=20_000, min_out=0)

    stats = await service.get_pool_stats("p")
    # 手续费口径：fee = amount_in - floor(amount_in * (1e6 - f) / 1e6)
    exp_fee_x = 10_000 - pricing.effective_input(10_000, 3000)
    exp_fee_y = 20_000 - pricing.effective_input(20_000, 3000)
    assert r1["fee_in"] == exp_fee_x == 30
    assert r2["fee_in"] == exp_fee_y == 60
    assert stats["fee_x"] == exp_fee_x and stats["fee_y"] == exp_fee_y
    assert stats["swap_count_x_to_y"] == 1
    assert stats["swap_count_y_to_x"] == 1
    assert stats["swap_in_x"] == 10_000 and stats["swap_in_y"] == 20_000
    # 本金：seed 初始库存记为系统账户本金，兑换不改变本金口径
    assert stats["principal_x"] == 1_000_000
    assert stats["principal_y"] == 1_000_000
    await _assert_stats_match_ledger(service, "p")
    log.info("fee per side: fee_x=%s fee_y=%s; principal=(%s,%s); "
             "ledger-derivable=ok",
             stats["fee_x"], stats["fee_y"],
             stats["principal_x"], stats["principal_y"])


@pytest.mark.asyncio
async def test_user_stats_share_ratio_fee_entitlement_contribution(service):
    """业务：用户份额占比、应得手续费分成（向下取整）与累计贡献。"""
    await service.seed_pool("p", "X", "Y", 0, 0)
    await service.add_liquidity(request_id="a1", pool_id="p", user="alice",
                                amount_x=100_000, amount_y=100_000)
    await service.add_liquidity(request_id="a2", pool_id="p", user="bob",
                                amount_x=100_000, amount_y=100_000)
    # 产生手续费
    await service.swap(request_id="s1", pool_id="p", user="trader",
                       side="x_to_y", amount_in=50_000, min_out=0)

    stats = await service.get_pool_stats("p")
    alice = await service.get_user_stats("p", "alice")
    bob = await service.get_user_stats("p", "bob")

    assert alice["shares"] == bob["shares"] == 100_000
    assert alice["share_ppm"] == 500_000  # 各占 50%
    assert alice["contributed_x"] == alice["contributed_y"] == 100_000
    # 应得分成 = floor(shares * fee / total_shares)，两人相等且总额不超池子累计
    assert alice["fee_share_x"] == stats["fee_x"] // 2
    assert alice["fee_share_x"] + bob["fee_share_x"] <= stats["fee_x"]
    assert alice["fee_share_y"] == 0  # 只有 x 侧手续费
    log.info("user stats: alice share_ppm=%s fee_share_x=%s (pool fee_x=%s), "
             "entitlement rounding dust stays in pool",
             alice["share_ppm"], alice["fee_share_x"], stats["fee_x"])

    # 赎回后贡献与赎回累计更新
    await service.remove_liquidity(request_id="r1", pool_id="p", user="bob",
                                   shares=50_000)
    bob2 = await service.get_user_stats("p", "bob")
    assert bob2["shares"] == 50_000
    assert bob2["withdrawn_x"] > 0 and bob2["withdrawn_y"] > 0
    await _assert_stats_match_ledger(service, "p")


@pytest.mark.asyncio
async def test_rejected_swap_generates_no_fee(service):
    """业务：被拒绝的兑换不产生手续费、不改变成交统计。"""
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    with pytest.raises(SlippageExceeded):
        await service.swap(request_id="bad", pool_id="p", user="a",
                           side="x_to_y", amount_in=10_000, min_out=10**9)
    stats = await service.get_pool_stats("p")
    assert stats["fee_x"] == 0 and stats["swap_count"] == 0
    assert stats["swap_in_x"] == 0
    # 拒绝记录仍可在流水中查到，但不参与成功重推
    hist = await service.get_history(pool_id="p", op="swap", status="rejected")
    assert hist["total"] == 1
    assert hist["items"][0]["error_code"] == "SLIPPAGE_EXCEEDED"
    await _assert_stats_match_ledger(service, "p")
    log.info("rejected swap: no fee, no count; rejection still traceable "
             "in ledger with error_code")


@pytest.mark.asyncio
async def test_concurrent_swaps_stats_consistent_with_ledger(service):
    """并发：100 笔并发兑换后，手续费与成交统计最终一致且可从流水重推。"""
    n = 100
    await service.seed_pool("p", "X", "Y", 10**9, 10**9)

    async def one(i: int):
        return await service.swap(
            request_id=f"sw-{i}", pool_id="p", user=f"u{i}",
            side="x_to_y" if i % 2 == 0 else "y_to_x",
            amount_in=1000, min_out=0)

    results = await asyncio.gather(*(one(i) for i in range(n)))
    stats = await service.get_pool_stats("p")
    assert stats["swap_count"] == n
    assert stats["fee_x"] + stats["fee_y"] == sum(r["fee_in"] for r in results)
    # 每笔 fee_in 与理论口径一致
    for r in results:
        assert r["fee_in"] == 1000 - pricing.effective_input(1000, 3000)
    await _assert_stats_match_ledger(service, "p")
    log.info("concurrent n=%s: fee_total=%s count=%s, stats == ledger "
             "recomputation, reserve identity holds",
             n, stats["fee_x"] + stats["fee_y"], stats["swap_count"])


@pytest.mark.asyncio
async def test_duplicate_request_counted_once_in_stats(service):
    """幂等：同一 request_id 重复提交，手续费与成交笔数只计一次。"""
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    kw = dict(request_id="once", pool_id="p", user="a",
              side="x_to_y", amount_in=1000, min_out=0)
    results = await asyncio.gather(*[service.swap(**kw) for _ in range(10)])
    assert sum(1 for r in results if r["replayed"]) == 9

    stats = await service.get_pool_stats("p")
    assert stats["swap_count"] == 1
    assert stats["fee_x"] == 3
    # 流水也只有一条成功记录
    swaps = await _ledger_swaps(service, "p")
    assert len(swaps) == 1
    await _assert_stats_match_ledger(service, "p")
    log.info("10 concurrent duplicates -> 1 swap in stats and ledger, "
             "fee counted once")
