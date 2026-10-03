"""容量与熔断：单笔上限、缓冲水位、暂停/恢复、重复初始化防护。

覆盖业务：
- max_swap_in / reserve_buffer 超限以独立错误码拒绝且不改账；
- 暂停期间 swap/add/remove 明确失败、不留部分状态，流水记录 POOL_PAUSED；
- 策略切换不改判已接受请求（幂等回放原结果）；
- 恢复后第一笔从正确存量继续（不变量：k 不减、版本连续）；
- 重复 seed：同参幂等返回既有状态，异参明确拒绝，存量/份额不被叠加。
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from amm_service.errors import (
    InvalidParameter,
    PoolAlreadyExists,
    PoolPaused,
    ReserveBufferViolation,
    SwapAmountExceedsLimit,
)

log = logging.getLogger("test.policy")


async def _seeded(service, rx=1_000_000, ry=1_000_000):
    await service.seed_pool("p", "X", "Y", rx, ry)


@pytest.mark.asyncio
async def test_max_swap_in_limit(service):
    """单笔输入超限：SWAP_AMOUNT_EXCEEDS_LIMIT，池子状态不变。"""
    await _seeded(service)
    await service.set_pool_policy("p", max_swap_in=5000)
    before = await service.get_pool("p")
    with pytest.raises(SwapAmountExceedsLimit):
        await service.swap(request_id="over", pool_id="p", user="a",
                           side="x_to_y", amount_in=5001, min_out=0)
    after = await service.get_pool("p")
    assert after == before  # 不留部分状态
    # 边界：恰好等于上限可成交
    r = await service.swap(request_id="edge", pool_id="p", user="a",
                           side="x_to_y", amount_in=5000, min_out=0)
    assert r["amount_out"] > 0
    # 流水里两种结局都可查且原因可区分
    h = await service.get_history(pool_id="p", op="swap")
    codes = {e["request_id"]: e["error_code"] for e in h["items"]}
    assert codes["over"] == "SWAP_AMOUNT_EXCEEDS_LIMIT"
    assert codes["edge"] == ""
    log.info("max_swap_in: over-limit rejected, boundary accepted, codes=%s", codes)


@pytest.mark.asyncio
async def test_reserve_buffer_violation(service):
    """缓冲水位：成交后输出侧库存跌破水位即拒绝。"""
    await _seeded(service, 100_000, 100_000)
    await service.set_pool_policy("p", reserve_buffer=90_000)
    before = await service.get_pool("p")
    # 大额兑换会把 reserve_y 打到 90_000 以下
    with pytest.raises(ReserveBufferViolation):
        await service.swap(request_id="drain", pool_id="p", user="a",
                           side="x_to_y", amount_in=100_000, min_out=0)
    after = await service.get_pool("p")
    assert after == before
    # 小额兑换不触碰水位，正常成交
    await service.swap(request_id="small", pool_id="p", user="a",
                       side="x_to_y", amount_in=100, min_out=0)
    log.info("reserve_buffer: draining swap rejected, small swap accepted")


@pytest.mark.asyncio
async def test_pause_blocks_ops_without_partial_state(service):
    """暂停期间请求明确失败且不改账；未暂停的操作不受影响。"""
    await _seeded(service)
    await service.pause_pool("p", ["swap"])
    before = await service.get_pool("p")
    with pytest.raises(PoolPaused) as exc:
        await service.swap(request_id="p1", pool_id="p", user="a",
                           side="x_to_y", amount_in=100, min_out=0)
    assert exc.value.code == "POOL_PAUSED"
    after = await service.get_pool("p")
    assert after == before
    # add/remove 未暂停，仍可用
    await service.add_liquidity(request_id="p2", pool_id="p", user="a",
                                amount_x=1000, amount_y=1000)
    # 全部暂停后 add/remove 也被拒
    await service.pause_pool("p", ["add", "remove"])
    with pytest.raises(PoolPaused):
        await service.add_liquidity(request_id="p3", pool_id="p", user="a",
                                    amount_x=1000, amount_y=1000)
    with pytest.raises(PoolPaused):
        await service.remove_liquidity(request_id="p4", pool_id="p",
                                       user="a", shares=1)
    h = await service.get_history(pool_id="p")
    paused = [e for e in h["items"] if e["error_code"] == "POOL_PAUSED"]
    assert len(paused) == 3
    log.info("pause: swap/add/remove rejected while paused, no partial state")


@pytest.mark.asyncio
async def test_resume_continues_from_correct_reserves(service):
    """恢复后第一笔从正确存量继续：k 不减、版本连续、成交可对账。"""
    await _seeded(service)
    await service.swap(request_id="pre", pool_id="p", user="a",
                       side="x_to_y", amount_in=1000, min_out=0)
    await service.pause_pool("p", ["swap"])
    with pytest.raises(PoolPaused):
        await service.swap(request_id="during", pool_id="p", user="a",
                           side="x_to_y", amount_in=1000, min_out=0)
    mid = await service.get_pool("p")
    await service.resume_pool("p", ["swap"])
    r = await service.swap(request_id="post", pool_id="p", user="a",
                           side="x_to_y", amount_in=1000, min_out=0)
    after = r["pool"]
    # 恢复后的成交基于暂停前的存量：版本只 +1（暂停期间的拒绝不推进版本）
    assert after["version"] == mid["version"] + 1
    assert after["reserve_x"] == mid["reserve_x"] + 1000
    assert after["reserve_x"] * after["reserve_y"] >= (
        mid["reserve_x"] * mid["reserve_y"]
    )
    log.info("resume: version %s->%s, k preserved, reserves consistent",
             mid["version"], after["version"])


@pytest.mark.asyncio
async def test_policy_change_does_not_rejudge_accepted_requests(service):
    """策略切换后，已接受请求的幂等回放仍返回首次成功结果。"""
    await _seeded(service)
    kw = dict(request_id="accepted", pool_id="p", user="a",
              side="x_to_y", amount_in=10_000, min_out=0)
    r1 = await service.swap(**kw)
    # 收紧策略：同样参数现在会被拒，但已接受的请求回放原结果
    await service.set_pool_policy("p", max_swap_in=1000)
    await service.pause_pool("p", ["swap"])
    r2 = await service.swap(**kw)
    assert r2["replayed"] is True
    assert r2["amount_out"] == r1["amount_out"]
    log.info("policy change: accepted request replays original result, "
             "amount_out=%s", r2["amount_out"])


@pytest.mark.asyncio
async def test_pause_is_atomic_under_concurrency(service):
    """并发下暂停：已受理的成交完成，之后的一律 POOL_PAUSED，无中间态。"""
    await _seeded(service, 10**9, 10**9)

    async def swap_many() -> list:
        out = []
        for i in range(50):
            try:
                await service.swap(request_id=f"c{i}", pool_id="p", user="a",
                                   side="x_to_y", amount_in=100, min_out=0)
                out.append("ok")
            except PoolPaused:
                out.append("paused")
        return out

    async def pauser() -> None:
        await asyncio.sleep(0)
        await service.pause_pool("p", ["swap"])

    results, _ = await asyncio.gather(swap_many(), pauser())
    # 结果必须是「一段 ok 后全 paused」，不允许 paused 之后又 ok
    if "paused" in results:
        first_paused = results.index("paused")
        assert all(r == "paused" for r in results[first_paused:])
    pool = await service.get_pool("p")
    ok_count = results.count("ok")
    # 版本推进数 == 成功笔数，暂停的拒绝不改账
    assert pool["version"] == ok_count
    log.info("concurrent pause: ok=%s paused=%s version=%s consistent",
             ok_count, results.count("paused"), pool["version"])


@pytest.mark.asyncio
async def test_policy_validation(service):
    """策略参数非法：负数上限/水位、空 ops、未知 op 均拒绝。"""
    await _seeded(service)
    with pytest.raises(InvalidParameter):
        await service.set_pool_policy("p", max_swap_in=-1)
    with pytest.raises(InvalidParameter):
        await service.set_pool_policy("p", reserve_buffer=-5)
    with pytest.raises(InvalidParameter):
        await service.pause_pool("p", [])
    with pytest.raises(InvalidParameter):
        await service.pause_pool("p", ["bogus"])
    log.info("policy validation: invalid params rejected")


@pytest.mark.asyncio
async def test_reseed_same_params_returns_existing_state(service):
    """重复初始化（同参）：原样返回既有状态，不叠加存量/份额。"""
    await _seeded(service)
    first = await service.get_pool("p")
    again = await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    assert again["created"] is False
    after = await service.get_pool("p")
    assert after == first
    assert after["total_shares"] == 1_000_000  # 未被叠加
    log.info("reseed same params: created=False, state unchanged")


@pytest.mark.asyncio
async def test_reseed_different_params_rejected(service):
    """重复初始化（异参）：POOL_ALREADY_EXISTS，既有状态不变。"""
    await _seeded(service)
    before = await service.get_pool("p")
    for kwargs in (
        dict(token_x="X", token_y="Y", reserve_x=2_000_000,
             reserve_y=2_000_000),
        dict(token_x="X", token_y="Z", reserve_x=1_000_000,
             reserve_y=1_000_000),
    ):
        with pytest.raises(PoolAlreadyExists):
            await service.seed_pool("p", fee_ppm=3000, **kwargs)
    after = await service.get_pool("p")
    assert after == before
    log.info("reseed different params: POOL_ALREADY_EXISTS, state unchanged")


@pytest.mark.asyncio
async def test_reseed_after_trades_never_resets_reserves(service):
    """成交之后重复 seed（即便同参）：存量已变，明确拒绝而非重置。"""
    await _seeded(service)
    await service.swap(request_id="s1", pool_id="p", user="a",
                       side="x_to_y", amount_in=1000, min_out=0)
    with pytest.raises(PoolAlreadyExists):
        await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    pool = await service.get_pool("p")
    assert pool["reserve_x"] == 1_001_000  # 成交结果保留
    log.info("reseed after trades: rejected, traded reserves preserved")
