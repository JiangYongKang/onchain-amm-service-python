"""并发与重复提交：同池串行化、库存守恒、份额守恒、幂等不重复扣减。"""
from __future__ import annotations

import asyncio
import logging

import pytest

log = logging.getLogger("test.concurrency")


@pytest.mark.asyncio
async def test_concurrent_swaps_are_serialized_and_conserved(service):
    n = 100
    await service.seed_pool("c", "X", "Y", 10**9, 10**9)
    before = await service.get_pool("c")

    async def one(i: int) -> dict:
        return await service.swap(
            request_id=f"swap-{i}", pool_id="c", user=f"u{i}",
            side="x_to_y" if i % 2 == 0 else "y_to_x",
            amount_in=1000, min_out=0,
        )

    results = await asyncio.gather(*(one(i) for i in range(n)))
    after = await service.get_pool("c")

    out_x = sum(r["amount_out"] for r in results if r["side"] == "y_to_x")
    out_y = sum(r["amount_out"] for r in results if r["side"] == "x_to_y")
    in_x = 1000 * (n // 2)
    in_y = 1000 * (n // 2)

    # 库存净变化 == 累计投入 - 累计产出，无重复扣减/互相覆盖
    assert after["reserve_x"] - before["reserve_x"] == in_x - out_x
    assert after["reserve_y"] - before["reserve_y"] == in_y - out_y
    # 每笔成交都推进版本，恰好 n 个版本
    assert after["version"] == before["version"] + n
    # 恒定乘积不减少
    assert after["reserve_x"] * after["reserve_y"] >= before["reserve_x"] * before["reserve_y"]
    log.info(
        "concurrent swaps n=%s before=%s after=%s conservation=ok k_preserved=%s",
        n, before, after,
        after["reserve_x"] * after["reserve_y"] >= before["reserve_x"] * before["reserve_y"],
    )


@pytest.mark.asyncio
async def test_concurrent_duplicate_request_executes_once(service):
    await service.seed_pool("c", "X", "Y", 1_000_000, 1_000_000)
    before = await service.get_pool("c")
    kw = dict(request_id="once", pool_id="c", user="alice",
              side="x_to_y", amount_in=1000, min_out=0)

    results = await asyncio.gather(
        *[service.swap(**kw) for _ in range(20)],
        return_exceptions=True,
    )
    # 锁串行化后：一个真正执行，其余全部回放同一结果；无异常
    oks = [r for r in results if not isinstance(r, Exception)]
    assert len(oks) == 20
    assert len({r["amount_out"] for r in oks}) == 1
    assert sum(1 for r in oks if r["replayed"]) == 19

    after = await service.get_pool("c")
    assert after["version"] == before["version"] + 1  # 只成交一次
    assert after["reserve_x"] - before["reserve_x"] == 1000
    log.info("20 concurrent identical requests -> 1 execution, 19 replays; pool=%s", after)


@pytest.mark.asyncio
async def test_concurrent_mixed_success_and_failure(service):
    await service.seed_pool("c", "X", "Y", 1_000_000, 1_000_000)
    # 一半请求 min_out 高到必然失败
    async def one(i: int):
        return await service.swap(
            request_id=f"m-{i}", pool_id="c", user="u",
            side="x_to_y", amount_in=1000,
            min_out=0 if i % 2 == 0 else 10**9,
        )

    results = await asyncio.gather(*(one(i) for i in range(20)),
                                   return_exceptions=True)
    from amm_service.errors import SlippageExceeded

    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, SlippageExceeded)]
    assert len(successes) == 10 and len(failures) == 10

    after = await service.get_pool("c")
    # 失败请求不留部分状态：只有 10 笔成功成交改变库存/版本
    assert after["reserve_x"] == 1_000_000 + 10 * 1000
    assert after["version"] == 10
    log.info("mixed 20: 10 success + 10 rejected; version=%s reserves=(%s,%s)",
             after["version"], after["reserve_x"], after["reserve_y"])


@pytest.mark.asyncio
async def test_concurrent_liquidity_share_conservation(service):
    await service.seed_pool("c", "X", "Y", 0, 0)

    async def add(i: int):
        return await service.add_liquidity(
            request_id=f"add-{i}", pool_id="c", user=f"u{i}",
            amount_x=10_000, amount_y=10_000,
        )

    adds = await asyncio.gather(*(add(i) for i in range(20)))
    pool = await service.get_pool("c")
    positions = await asyncio.gather(
        *(service.get_position("c", f"u{i}") for i in range(20))
    )
    total_positions = sum(p["shares"] for p in positions)
    assert total_positions == pool["total_shares"] == sum(a["shares"] for a in adds)
    assert pool["reserve_x"] == pool["reserve_y"] == 200_000
    log.info("concurrent adds: minted=%s reserves=(%s,%s), shares conserved",
             pool["total_shares"], pool["reserve_x"], pool["reserve_y"])

    # 全部并发赎回，恰好把份额清零、库存取空（等比情形无 dust）
    async def remove(i: int):
        return await service.remove_liquidity(
            request_id=f"rm-{i}", pool_id="c", user=f"u{i}",
            shares=10_000,
        )

    rms = await asyncio.gather(*(remove(i) for i in range(20)))
    pool = await service.get_pool("c")
    assert pool["total_shares"] == 0
    assert pool["reserve_x"] == pool["reserve_y"] == 0
    assert sum(r["amount_x"] for r in rms) == 200_000
