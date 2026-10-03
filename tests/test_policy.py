"""容量与熔断：单笔上限、缓冲水位、暂停/恢复、策略切换语义、重复初始化。"""
from __future__ import annotations

import asyncio
import logging

import pytest

from amm_service.errors import (
    AmountLimitExceeded,
    InvalidParameter,
    PoolAlreadyExists,
    PoolPaused,
    ReserveBufferBreach,
)

log = logging.getLogger("test.policy")


@pytest.mark.asyncio
async def test_swap_amount_limit_boundary(service):
    """业务：单笔兑换输入上限，等于上限可成交，超限以专属原因拒绝。"""
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    await service.set_pool_policy("p", max_swap_in=10_000)

    ok = await service.swap(request_id="at-limit", pool_id="p", user="a",
                            side="x_to_y", amount_in=10_000, min_out=0)
    assert ok["amount_out"] > 0
    with pytest.raises(AmountLimitExceeded) as e:
        await service.swap(request_id="over-limit", pool_id="p", user="a",
                           side="x_to_y", amount_in=10_001, min_out=0)
    assert e.value.code == "AMOUNT_LIMIT_EXCEEDED"

    # 超限请求不改变库存与版本，但留痕可查
    pool = await service.get_pool("p")
    assert pool["version"] == 1
    hist = await service.get_history(pool_id="p", op="swap", status="rejected")
    assert hist["total"] == 1
    assert hist["items"][0]["error_code"] == "AMOUNT_LIMIT_EXCEEDED"

    # 放宽限制后同一请求可成交（新请求按新策略判定）
    await service.set_pool_policy("p", max_swap_in=0)
    ok2 = await service.swap(request_id="after-lift", pool_id="p", user="a",
                             side="x_to_y", amount_in=10_001, min_out=0)
    assert ok2["amount_out"] > 0
    log.info("amount limit: ==limit accepted, >limit rejected with "
             "AMOUNT_LIMIT_EXCEEDED, lift policy -> accepted")


@pytest.mark.asyncio
async def test_reserve_buffer_boundary(service):
    """业务：缓冲水位，成交后输出侧库存低于水位即拒绝，恰好等于水位可成交。"""
    await service.seed_pool("p", "X", "Y", 100_000, 100_000)
    # 先询价算出 out，再设水位 = reserve_out - out（恰好不越界）
    q = await service.quote(pool_id="p", side="x_to_y", amount_in=10_000)
    out = q["amount_out"]
    await service.set_pool_policy("p", reserve_buffer=100_000 - out)
    ok = await service.swap(request_id="exact", pool_id="p", user="a",
                            side="x_to_y", amount_in=10_000, min_out=0)
    assert ok["amount_out"] == out  # 恰好剩水位：允许

    # 水位提高 1：同样的交易必然越界
    await service.set_pool_policy("p", reserve_buffer=100_000 - out + 1)
    with pytest.raises(ReserveBufferBreach) as e:
        await service.swap(request_id="breach", pool_id="p", user="a",
                           side="x_to_y", amount_in=10_000, min_out=0)
    assert e.value.code == "RESERVE_BUFFER_BREACH"
    stats = await service.get_pool_stats("p")
    assert stats["swap_count"] == 1  # 越界请求未成交、未产生手续费
    assert stats["fee_x"] == 30
    log.info("reserve buffer: exact-fit accepted, +1 buffer rejected with "
             "RESERVE_BUFFER_BREACH, no fee on breach")


@pytest.mark.asyncio
async def test_pause_blocks_all_three_ops_without_partial_state(service):
    """业务：暂停兑换/注入/赎回，期间请求明确失败且不留部分状态。"""
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    await service.add_liquidity(request_id="a0", pool_id="p", user="alice",
                                amount_x=1000, amount_y=1000)
    before = await service.get_pool("p")

    await service.set_pause("p", "swap", True)
    await service.set_pause("p", "add", True)
    await service.set_pause("p", "remove", True)

    with pytest.raises(PoolPaused) as e1:
        await service.swap(request_id="ps", pool_id="p", user="a",
                           side="x_to_y", amount_in=100, min_out=0)
    with pytest.raises(PoolPaused) as e2:
        await service.add_liquidity(request_id="pa", pool_id="p", user="a",
                                    amount_x=100, amount_y=100)
    with pytest.raises(PoolPaused) as e3:
        await service.remove_liquidity(request_id="pr", pool_id="p",
                                       user="alice", shares=1)
    assert all(e.value.code == "POOL_PAUSED" for e in (e1, e2, e3))

    after = await service.get_pool("p")
    # 暂停期间无任何部分生效的「资金状态」（暂停标记本身是预期变更）
    for k in ("reserve_x", "reserve_y", "total_shares", "version"):
        assert after[k] == before[k], k
    pos = await service.get_position("p", "alice")
    assert pos["shares"] == 1000  # 份额未被赎回

    hist = await service.get_history(pool_id="p", status="rejected",
                                     page_size=500)
    assert [i["error_code"] for i in hist["items"]] == ["POOL_PAUSED"] * 3
    log.info("pause: swap/add/remove all rejected with POOL_PAUSED, "
             "pool state and shares untouched, rejections traceable")


@pytest.mark.asyncio
async def test_resume_continues_from_correct_state(service):
    """业务：恢复后第一笔交易从正确存量继续，不变量保持。"""
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    await service.swap(request_id="s1", pool_id="p", user="a",
                       side="x_to_y", amount_in=1000, min_out=0)
    mid = await service.get_pool("p")

    await service.set_pause("p", "swap", True)
    with pytest.raises(PoolPaused):
        await service.swap(request_id="s2", pool_id="p", user="a",
                           side="x_to_y", amount_in=1000, min_out=0)
    await service.set_pause("p", "swap", False)

    # 恢复后的第一笔：从暂停前的真实存量继续定价（与恢复后报价一致）
    q = await service.quote(pool_id="p", side="x_to_y", amount_in=1000)
    assert q["pool_version"] == mid["version"]  # 暂停期间版本未动
    r = await service.swap(request_id="s3", pool_id="p", user="a",
                           side="x_to_y", amount_in=1000, min_out=0,
                           quoted_out=q["amount_out"],
                           quote_version=q["pool_version"])
    assert r["amount_out"] == q["amount_out"]
    after = await service.get_pool("p")
    assert after["reserve_x"] == mid["reserve_x"] + 1000
    assert after["reserve_y"] == mid["reserve_y"] - q["amount_out"]
    assert after["version"] == mid["version"] + 1
    assert after["reserve_x"] * after["reserve_y"] >= 1_000_000 * 1_000_000
    log.info("resume: first swap out=%s from pre-pause reserves, "
             "version=%s, k preserved", r["amount_out"], after["version"])


@pytest.mark.asyncio
async def test_accepted_request_not_rejudged_after_policy_change(service):
    """业务：已接受的请求不被策略切换改判——幂等重放仍返回原结果。"""
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    kw = dict(request_id="committed", pool_id="p", user="a",
              side="x_to_y", amount_in=5000, min_out=0)
    first = await service.swap(**kw)

    # 事后把上限调到该笔以下、并暂停兑换：已成交请求不受影响
    await service.set_pool_policy("p", max_swap_in=100)
    await service.set_pause("p", "swap", True)
    replay = await service.swap(**kw)
    assert replay["replayed"] is True
    assert replay["amount_out"] == first["amount_out"]

    stats = await service.get_pool_stats("p")
    assert stats["swap_count"] == 1  # 没有被改判成第二笔或撤销
    log.info("policy change after commit: replay returns original result, "
             "no re-judgement, stats unchanged")


@pytest.mark.asyncio
async def test_pause_is_per_pool(service):
    """业务：暂停只作用于目标池子，其他池子不受影响。"""
    await service.seed_pool("p1", "X", "Y", 1_000_000, 1_000_000)
    await service.seed_pool("p2", "X", "Y", 1_000_000, 1_000_000)
    await service.set_pause("p1", "swap", True)

    with pytest.raises(PoolPaused):
        await service.swap(request_id="x1", pool_id="p1", user="a",
                           side="x_to_y", amount_in=100, min_out=0)
    ok = await service.swap(request_id="x2", pool_id="p2", user="a",
                            side="x_to_y", amount_in=100, min_out=0)
    assert ok["amount_out"] > 0
    log.info("pause scoped per pool: p1 paused, p2 unaffected")


@pytest.mark.asyncio
async def test_policy_validation(service):
    """业务：非法策略与暂停参数明确拒绝。"""
    await service.seed_pool("p", "X", "Y", 1000, 1000)
    with pytest.raises(InvalidParameter):
        await service.set_pool_policy("p", max_swap_in=-1)
    with pytest.raises(InvalidParameter):
        await service.set_pool_policy("p", reserve_buffer=-5)
    with pytest.raises(InvalidParameter):
        await service.set_pause("p", "bogus", True)
    log.info("policy validation: negative limits and bogus op rejected")


@pytest.mark.asyncio
async def test_reseed_same_config_returns_existing_state(service):
    """业务：同配置重复初始化原样返回既有状态，不叠加存量与份额。"""
    first = await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    await service.swap(request_id="s1", pool_id="p", user="a",
                       side="x_to_y", amount_in=1000, min_out=0)
    mid = await service.get_pool("p")

    again = await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    assert again["already_exists"] is True
    assert again["reserve_x"] == mid["reserve_x"]  # 未被叠加
    assert again["reserve_y"] == mid["reserve_y"]
    assert again["total_shares"] == mid["total_shares"]
    assert again["version"] == mid["version"]
    assert first["total_shares"] == 1_000_000
    log.info("reseed same config: state returned unchanged, no stacking of "
             "reserves/shares")


@pytest.mark.asyncio
async def test_reseed_different_config_rejected(service):
    """业务：不同配置的重复初始化明确拒绝，且留痕可查。"""
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    with pytest.raises(PoolAlreadyExists) as e:
        await service.seed_pool("p", "X", "Z", 1_000_000, 1_000_000)
    assert e.value.code == "POOL_ALREADY_EXISTS"
    with pytest.raises(PoolAlreadyExists):
        await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000,
                                fee_ppm=5000)

    pool = await service.get_pool("p")
    assert pool["reserve_x"] == 1_000_000 and pool["total_shares"] == 1_000_000
    hist = await service.get_history(pool_id="p", op="seed", status="rejected")
    assert hist["total"] == 2
    log.info("reseed different config: POOL_ALREADY_EXISTS, state intact, "
             "rejections recorded")


@pytest.mark.asyncio
async def test_concurrent_reseed_initializes_once(service):
    """并发：并发重复初始化只生效一次，份额与库存不叠加。"""
    results = await asyncio.gather(*[
        service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
        for _ in range(20)
    ])
    created = [r for r in results if not r.get("already_exists")]
    replayed = [r for r in results if r.get("already_exists")]
    assert len(created) == 1 and len(replayed) == 19

    pool = await service.get_pool("p")
    assert pool["reserve_x"] == pool["reserve_y"] == 1_000_000
    assert pool["total_shares"] == 1_000_000
    pos = await service.get_position("p", "_system")
    assert pos["shares"] == 1_000_000
    seeds = await service.get_history(pool_id="p", op="seed", status="success")
    assert seeds["total"] == 1
    log.info("20 concurrent seeds -> 1 created + 19 replays, reserves/shares "
             "not stacked")
