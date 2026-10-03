"""交易流水历史：成功/失败留痕、多维过滤、分页一致性、幂等不重复记录。"""
from __future__ import annotations

import logging

import pytest

from amm_service.errors import (
    InsufficientShareBalance,
    InvalidParameter,
    PoolNotFound,
    SlippageExceeded,
)

log = logging.getLogger("test.history")


async def _make_activity(service) -> None:
    """构造混合活动：2 笔成功兑换 + 1 笔失败兑换 + 1 笔注入 + 1 笔失败赎回。"""
    await service.seed_pool("p1", "X", "Y", 1_000_000, 1_000_000)
    await service.seed_pool("p2", "X", "Y", 500_000, 500_000)
    await service.swap(request_id="s1", pool_id="p1", user="alice",
                       side="x_to_y", amount_in=1000, min_out=0)
    await service.swap(request_id="s2", pool_id="p1", user="bob",
                       side="y_to_x", amount_in=2000, min_out=0)
    with pytest.raises(SlippageExceeded):
        await service.swap(request_id="s3", pool_id="p1", user="alice",
                           side="x_to_y", amount_in=1000, min_out=10**9)
    await service.add_liquidity(request_id="a1", pool_id="p2", user="alice",
                                amount_x=1000, amount_y=1000)
    with pytest.raises(InsufficientShareBalance):
        await service.remove_liquidity(request_id="r1", pool_id="p2",
                                       user="carol", shares=10**9)


@pytest.mark.asyncio
async def test_success_and_failure_are_traceable(service):
    """业务：成功与失败操作都留痕，失败原因可区分。"""
    await _make_activity(service)
    hist = await service.get_history(page_size=500)
    # 2 seed + 3 swap + 1 add + 1 remove = 7
    assert hist["total"] == 7
    by_key = {(r["op"], r["request_id"]): r for r in hist["items"]}
    assert by_key[("swap", "s1")]["status"] == "success"
    assert by_key[("swap", "s1")]["error_code"] == ""
    assert by_key[("swap", "s3")]["status"] == "rejected"
    assert by_key[("swap", "s3")]["error_code"] == "SLIPPAGE_EXCEEDED"
    assert by_key[("remove", "r1")]["error_code"] == "INSUFFICIENT_SHARE_BALANCE"
    # 成功记录的 detail 含成交金额与手续费，可逐笔重推
    assert by_key[("swap", "s1")]["detail"]["amount_out"] > 0
    assert by_key[("swap", "s1")]["detail"]["fee_in"] == 3
    # 序号全局单调
    seqs = [r["seq"] for r in hist["items"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    log.info("traceability: %s records, success/rejected distinguishable, "
             "seq monotonic", hist["total"])


@pytest.mark.asyncio
async def test_filters_by_pool_user_op_status(service):
    """业务：按池子/用户/操作类型/状态过滤，total 与过滤结果一致。"""
    await _make_activity(service)

    r = await service.get_history(pool_id="p1", page_size=500)
    assert r["total"] == 4  # seed + 3 swap
    assert all(i["pool_id"] == "p1" for i in r["items"])

    r = await service.get_history(user="alice", page_size=500)
    assert r["total"] == 3  # s1 + s3 + a1
    assert all(i["user"] == "alice" for i in r["items"])

    r = await service.get_history(op="swap", status="rejected", page_size=500)
    assert r["total"] == 1
    assert r["items"][0]["error_code"] == "SLIPPAGE_EXCEEDED"

    r = await service.get_history(pool_id="p2", user="alice", op="add",
                                  status="success", page_size=500)
    assert r["total"] == 1 and r["items"][0]["request_id"] == "a1"
    log.info("filters: pool/user/op/status composable, totals match")


@pytest.mark.asyncio
async def test_time_range_filter(service, clock):
    """业务：按时间范围（闭区间）过滤。"""
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    t0 = clock.t
    clock.advance(1000)
    await service.swap(request_id="s1", pool_id="p", user="a",
                       side="x_to_y", amount_in=1000, min_out=0)
    t1 = clock.t
    clock.advance(1000)
    await service.swap(request_id="s2", pool_id="p", user="a",
                       side="x_to_y", amount_in=1000, min_out=0)
    t2 = clock.t

    r = await service.get_history(op="swap", since_ms=t1, until_ms=t1)
    assert r["total"] == 1 and r["items"][0]["request_id"] == "s1"
    r = await service.get_history(op="swap", since_ms=t0, until_ms=t2)
    assert r["total"] == 2
    r = await service.get_history(op="swap", until_ms=t0)
    assert r["total"] == 0
    log.info("time range: [%s,%s] closed interval, boundary inclusive", t0, t2)


@pytest.mark.asyncio
async def test_pagination_total_and_slicing(service):
    """业务：分页切片正确，每页 total 都等于过滤结果总数。"""
    await service.seed_pool("p", "X", "Y", 10**9, 10**9)
    for i in range(25):
        await service.swap(request_id=f"s{i}", pool_id="p", user="a",
                           side="x_to_y", amount_in=100, min_out=0)

    seen: list[dict] = []
    for page in (1, 2, 3):
        r = await service.get_history(op="swap", page=page, page_size=10)
        assert r["total"] == 25  # 每页 total 都是过滤后的真实总数
        seen.extend(r["items"])
    assert len(seen) == 25
    assert [i["request_id"] for i in seen] == [f"s{i}" for i in range(25)]

    # 超出末页：空页但 total 不变
    r = await service.get_history(op="swap", page=4, page_size=10)
    assert r["total"] == 25 and r["items"] == []
    log.info("pagination: 25 items / size 10 -> 3 pages, total stable=25")


@pytest.mark.asyncio
async def test_invalid_pagination_rejected(service):
    """业务：非法分页/过滤参数明确拒绝。"""
    await service.seed_pool("p", "X", "Y", 1000, 1000)
    cases = [
        dict(page=0), dict(page=-1),
        dict(page_size=0), dict(page_size=-5),
        dict(page_size=service.MAX_PAGE_SIZE + 1),
        dict(op="bogus"), dict(status="bogus"),
        dict(since_ms=-1), dict(since_ms=10, until_ms=5),
    ]
    for kw in cases:
        with pytest.raises(InvalidParameter):
            await service.get_history(**kw)
    log.info("invalid pagination/filter params: %s cases all rejected "
             "with INVALID_PARAMETER", len(cases))


@pytest.mark.asyncio
async def test_duplicate_request_creates_no_second_record(service):
    """幂等：重复提交（含失败重放）不产生第二条流水。"""
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    kw = dict(request_id="dup", pool_id="p", user="a",
              side="x_to_y", amount_in=1000, min_out=0)
    await service.swap(**kw)
    await service.swap(**kw)  # 成功重放
    await service.swap(**kw)

    bad = dict(request_id="bad1", pool_id="p", user="a",
               side="x_to_y", amount_in=1000, min_out=10**9)
    for _ in range(3):
        with pytest.raises(SlippageExceeded):
            await service.swap(**bad)  # 失败重放

    with pytest.raises(PoolNotFound):
        await service.swap(request_id="ghost", pool_id="nope", user="a",
                           side="x_to_y", amount_in=1, min_out=0)
    with pytest.raises(PoolNotFound):
        await service.swap(request_id="ghost", pool_id="nope", user="a",
                           side="x_to_y", amount_in=1, min_out=0)

    hist = await service.get_history(op="swap", page_size=500)
    assert hist["total"] == 3  # dup + bad1 + ghost 各一条
    counts = {}
    for item in hist["items"]:
        counts[item["request_id"]] = counts.get(item["request_id"], 0) + 1
    assert counts == {"dup": 1, "bad1": 1, "ghost": 1}
    log.info("idempotency: replays (success/failure/not-found) leave exactly "
             "one ledger record each")
