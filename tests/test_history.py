"""交易流水历史：成功/失败均留痕、过滤与分页、幂等不重复记录。

覆盖业务：
- 每笔成功/失败操作恰好一条流水，失败原因可区分；
- 按池子/用户/操作类型/时间范围过滤，分页 total 与过滤结果一致；
- 分页参数非法明确拒绝；
- 重复提交（幂等回放/冲突）不产生第二条记录。
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from amm_service.errors import (
    DuplicateRequestConflict,
    InsufficientShareBalance,
    InvalidParameter,
    PoolNotFound,
    SlippageExceeded,
)

log = logging.getLogger("test.history")


async def _seeded(service):
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)


@pytest.mark.asyncio
async def test_success_and_failure_both_recorded_with_distinct_reasons(service):
    """成功与各类失败都留痕，error_code 可区分。"""
    await _seeded(service)
    await service.swap(request_id="ok1", pool_id="p", user="alice",
                       side="x_to_y", amount_in=1000, min_out=0)
    with pytest.raises(SlippageExceeded):
        await service.swap(request_id="bad1", pool_id="p", user="alice",
                           side="x_to_y", amount_in=1000, min_out=10**9)
    with pytest.raises(InsufficientShareBalance):
        await service.remove_liquidity(request_id="bad2", pool_id="p",
                                       user="alice", shares=1)
    with pytest.raises(PoolNotFound):
        await service.swap(request_id="bad3", pool_id="ghost", user="alice",
                           side="x_to_y", amount_in=1, min_out=0)

    h = await service.get_history(pool_id="p")
    assert h["total"] == 3
    by_req = {e["request_id"]: e for e in h["items"]}
    assert by_req["ok1"]["status"] == "success" and by_req["ok1"]["error_code"] == ""
    assert by_req["bad1"]["error_code"] == "SLIPPAGE_EXCEEDED"
    assert by_req["bad2"]["error_code"] == "INSUFFICIENT_SHARE_BALANCE"
    ghost = await service.get_history(pool_id="ghost")
    assert ghost["items"][0]["error_code"] == "POOL_NOT_FOUND"
    log.info("history recorded: %s",
             {r: e["error_code"] for r, e in by_req.items()})


@pytest.mark.asyncio
async def test_duplicate_request_creates_single_record(service):
    """幂等回放与冲突重试都不新增流水。"""
    await _seeded(service)
    kw = dict(request_id="dup", pool_id="p", user="alice",
              side="x_to_y", amount_in=1000, min_out=0)
    await service.swap(**kw)
    await service.swap(**kw)  # 回放
    with pytest.raises(DuplicateRequestConflict):
        await service.swap(**{**kw, "amount_in": 2000})  # 冲突
    # 失败请求的重复提交同样只留一条
    with pytest.raises(SlippageExceeded):
        await service.swap(request_id="rej", pool_id="p", user="alice",
                           side="x_to_y", amount_in=1000, min_out=10**9)
    with pytest.raises(SlippageExceeded):
        await service.swap(request_id="rej", pool_id="p", user="alice",
                           side="x_to_y", amount_in=1000, min_out=10**9)

    h = await service.get_history(pool_id="p")
    assert h["total"] == 2
    req_ids = sorted(e["request_id"] for e in h["items"])
    assert req_ids == ["dup", "rej"]
    log.info("idempotent replay/conflict: total=%s (expected 2)", h["total"])


@pytest.mark.asyncio
async def test_filter_by_user_op_and_time_range(service):
    """按用户/操作类型/时间范围过滤，结果与预期一致。"""
    await _seeded(service)
    clock = service._clock
    await service.add_liquidity(request_id="a1", pool_id="p", user="alice",
                                amount_x=10_000, amount_y=10_000)
    clock.advance(1000)
    t0 = clock()
    await service.swap(request_id="s1", pool_id="p", user="bob",
                       side="x_to_y", amount_in=1000, min_out=0)
    clock.advance(1000)
    await service.swap(request_id="s2", pool_id="p", user="alice",
                       side="y_to_x", amount_in=1000, min_out=0)
    clock.advance(1000)
    t1 = clock()

    by_user = await service.get_history(pool_id="p", user="alice")
    assert by_user["total"] == 2
    assert {e["op"] for e in by_user["items"]} == {"add", "swap"}

    by_op = await service.get_history(pool_id="p", op="swap")
    assert by_op["total"] == 2

    by_time = await service.get_history(pool_id="p", since_ms=t0, until_ms=t1)
    assert by_time["total"] == 2  # 闭区间，含两端
    assert {e["request_id"] for e in by_time["items"]} == {"s1", "s2"}
    log.info("filters user/op/time: totals=%s/%s/%s",
             by_user["total"], by_op["total"], by_time["total"])


@pytest.mark.asyncio
async def test_pagination_total_matches_filtered_set(service):
    """分页：total 与过滤全集一致，各页并集恰好覆盖全集且不重叠。"""
    await _seeded(service)
    for i in range(25):
        await service.swap(request_id=f"s{i}", pool_id="p", user="alice",
                           side="x_to_y", amount_in=100, min_out=0)
    seen: list[str] = []
    for page in (1, 2, 3):
        h = await service.get_history(pool_id="p", op="swap",
                                      page=page, page_size=10)
        assert h["total"] == 25
        seen.extend(e["request_id"] for e in h["items"])
    assert len(seen) == 25 and len(set(seen)) == 25
    # 默认最新在前
    first = await service.get_history(pool_id="p", page=1, page_size=1)
    assert first["items"][0]["request_id"] == "s24"
    log.info("pagination: 3 pages x 10 cover 25 unique entries, newest first")


@pytest.mark.asyncio
async def test_invalid_pagination_rejected(service):
    """分页参数非法：page<1、page_size 越界、时间范围倒置均明确拒绝。"""
    await _seeded(service)
    for kwargs in (
        {"page": 0}, {"page": -1}, {"page_size": 0}, {"page_size": 501},
        {"since_ms": 2000, "until_ms": 1000}, {"op": "bogus"},
    ):
        with pytest.raises(InvalidParameter):
            await service.get_history(pool_id="p", **kwargs)
    log.info("invalid pagination params rejected with INVALID_PARAMETER")


@pytest.mark.asyncio
async def test_concurrent_requests_ledger_matches_idempotency(service):
    """并发重复提交：流水条数与幂等记录一致，同一请求只有一条。"""
    await _seeded(service)
    kw = dict(request_id="c1", pool_id="p", user="alice",
              side="x_to_y", amount_in=1000, min_out=0)
    await asyncio.gather(*(service.swap(**kw) for _ in range(20)))
    # 20 个不同请求并发
    await asyncio.gather(*(
        service.swap(request_id=f"n{i}", pool_id="p", user="alice",
                     side="x_to_y", amount_in=100, min_out=0)
        for i in range(30)
    ))
    h = await service.get_history(pool_id="p", page_size=500)
    assert h["total"] == 31  # 1 + 30，重复提交不新增
    assert len({e["seq"] for e in h["items"]}) == 31  # seq 唯一单调
    log.info("concurrent: ledger total=%s unique seqs, duplicates collapsed",
             h["total"])
