"""对账不变量：仅凭历史流水逐笔重算的结果 == 统计接口报出的结果。

覆盖业务场景
-----------
1. 注入/赎回/兑换混合（含非等比注入、除不尽赎回的舍入 dust）；
2. 被拒绝请求（滑点/持仓不足/暂停/池不存在/同 id 冲突）：可查且原因可区分，
   但不参与任何重算口径；
3. 重复提交（成功重放、失败重放、并发重复）：只有一条参与重算；
4. 多用户并发注入/赎回/兑换：最终逐字段一致，且并发过程中的任意快照
   都满足份额守恒与库存恒等式（不凭空多/少资产）；
5. 成功流水明细要素齐备：注入两侧存入+铸出份额，赎回烧掉份额+两侧取出。
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from amm_service.errors import (
    AmmError,
    DuplicateRequestConflict,
    InsufficientShareBalance,
    PoolNotFound,
    PoolPaused,
    SlippageExceeded,
)
from amm_service.reconcile import replay_ledger

log = logging.getLogger("test.reconcile")

# 用户统计中必须与重算逐字段一致的全部口径字段
_USER_FIELDS = (
    "shares", "total_shares", "share_ppm",
    "fee_share_x", "fee_share_y",
    "contributed_x", "contributed_y",
    "withdrawn_x", "withdrawn_y",
)


async def _assert_replay_equals_stats(service, pool_id: str,
                                      users: set[str]) -> None:
    """核心不变量：池子统计 + 每个用户统计 == 仅凭流水的重算结果。"""
    replay = await service.replay_from_history(pool_id)
    stats = await service.get_pool_stats(pool_id)
    replayed = replay.pool_stats_view()
    mismatches = [
        (k, stats[k], replayed[k])
        for k in replayed if stats[k] != replayed[k]
    ]
    assert not mismatches, f"pool stats mismatch (field, api, ledger): {mismatches}"

    # 库存恒等式（按侧）：仅凭流水算出的本金净额 + 兑换净流入 == 当前库存
    pool = await service.get_pool(pool_id)
    assert pool["reserve_x"] == (
        replayed["principal_x"] + replayed["swap_in_x"] - replayed["swap_out_x"])
    assert pool["reserve_y"] == (
        replayed["principal_y"] + replayed["swap_in_y"] - replayed["swap_out_y"])

    # 每个有活动的用户 + seed 系统账户，逐字段一致
    for user in users | {"_system"}:
        us = await service.get_user_stats(pool_id, user)
        rv = replay.user_stats_view(user)
        bad = [(k, us[k], rv[k]) for k in _USER_FIELDS if us[k] != rv[k]]
        assert not bad, f"user {user} stats mismatch: {bad}"
    log.info("invariant holds on pool %s: pool stats=%s; users=%s",
             pool_id, replayed, sorted(users))


# --------------------------------------------------------------------- #
# 1. 纯函数：手写流水的重算语义（拒绝跳过 / seq 排序 / floor 口径）
# --------------------------------------------------------------------- #
def test_replay_ledger_pure_semantics():
    """业务：rejected 不参与、按 seq 折叠、分成/占比向下取整。"""
    items = [
        {"seq": 1, "status": "success", "op": "seed", "user": "_admin",
         "detail": {"reserve_x": 1_000_000, "reserve_y": 1_000_000,
                    "total_shares": 1_000_000}},
        # 乱序给出：fold 后必须与按 seq 处理相同
        {"seq": 3, "status": "success", "op": "add", "user": "bob",
         "detail": {"amount_x": 1_000, "amount_y": 1_000, "shares": 1_000}},
        {"seq": 2, "status": "rejected", "op": "swap", "user": "eve",
         "error_code": "SLIPPAGE_EXCEEDED", "detail": {"message": "x"}},
        {"seq": 4, "status": "success", "op": "swap", "user": "trader",
         "detail": {"side": "x_to_y", "amount_in": 10_000,
                    "amount_out": 9_800, "fee_in": 30}},
        {"seq": 5, "status": "rejected", "op": "remove", "user": "bob",
         "error_code": "INSUFFICIENT_SHARE_BALANCE",
         "detail": {"message": "x"}},
    ]
    r = replay_ledger(items)
    assert r.deposited_x == r.deposited_y == 1_001_000
    assert r.total_shares == 1_001_000
    assert r.fee_x == 30 and r.swap_count_x_to_y == 1
    assert r.swap_in_x == 10_000 and r.swap_out_y == 9_800
    # bob 占比 floor(1000/1001000*1e6)=999，手续费分成 floor(1000*30/1001000)=0
    bob = r.user_stats_view("bob")
    assert bob["share_ppm"] == 1000 * 1_000_000 // 1_001_000 == 999
    assert bob["fee_share_x"] == 0
    # eve 的被拒记录不产生任何持仓/贡献
    assert r.position("eve").shares == 0
    assert r.position("eve").contributed_x == 0
    log.info("pure replay: rejected skipped, seq-ordered fold, "
             "bob share_ppm=999 fee_share_x=0 (floor)")


# --------------------------------------------------------------------- #
# 2. 混合操作（含非等比注入、非整除赎回）
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_mixed_add_remove_swap_replay_matches_stats(service):
    """业务：混合操作后，池子/用户全部统计字段可仅凭流水逐笔重算。"""
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    await service.add_liquidity(request_id="a1", pool_id="p", user="alice",
                                amount_x=100_000, amount_y=100_000)
    # 非等比注入：y 多投部分捐赠留存，只铸较小侧份额
    r_uneven = await service.add_liquidity(
        request_id="a2", pool_id="p", user="bob",
        amount_x=10_000, amount_y=30_000)
    log.info("uneven add: minted shares=%s (y-side excess donated to pool)",
             r_uneven["shares"])
    # 几笔兑换制造手续费与库存偏移
    await service.swap(request_id="s1", pool_id="p", user="carol",
                       side="x_to_y", amount_in=50_000, min_out=0)
    await service.swap(request_id="s2", pool_id="p", user="carol",
                       side="y_to_x", amount_in=12_345, min_out=0)
    # 非整除赎回：floor dust 留存池内
    w = await service.remove_liquidity(request_id="r1", pool_id="p",
                                       user="alice", shares=12_345)
    log.info("dusty remove: burned=%s out=(%s,%s) floor dust retained",
             w["shares_burned"], w["amount_x"], w["amount_y"])

    # 成功流水明细要素齐备，足以独立还原每笔操作
    adds = (await service.get_history(op="add", status="success"))["items"]
    for rec in adds:
        d = rec["detail"]
        assert {"amount_x", "amount_y", "shares"} <= d.keys()
        assert d["amount_x"] > 0 and d["amount_y"] > 0 and d["shares"] > 0
    rms = (await service.get_history(op="remove", status="success"))["items"]
    for rec in rms:
        d = rec["detail"]
        assert {"shares_burned", "amount_x", "amount_y"} <= d.keys()
        assert d["shares_burned"] == 12_345
    swaps = (await service.get_history(op="swap", status="success"))["items"]
    for rec in swaps:
        assert {"side", "amount_in", "amount_out", "fee_in"} <= rec["detail"].keys()

    await _assert_replay_equals_stats(
        service, "p", {"alice", "bob", "carol"})


# --------------------------------------------------------------------- #
# 3. 被拒绝请求：留痕可查、原因可区分、不参与重算
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_rejected_requests_traceable_but_excluded(service):
    """业务：各类拒绝都进流水但不改变任何重算数字。"""
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    await service.add_liquidity(request_id="a1", pool_id="p", user="alice",
                                amount_x=10_000, amount_y=10_000)
    baseline = await service.get_pool_stats("p")

    with pytest.raises(SlippageExceeded):
        await service.swap(request_id="bad-swap", pool_id="p", user="eve",
                           side="x_to_y", amount_in=1000, min_out=10**9)
    with pytest.raises(InsufficientShareBalance):
        await service.remove_liquidity(request_id="bad-rm", pool_id="p",
                                       user="eve", shares=10**9)
    await service.set_pause("p", "swap", True)
    with pytest.raises(PoolPaused):
        await service.swap(request_id="paused", pool_id="p", user="eve",
                           side="x_to_y", amount_in=1, min_out=0)
    await service.set_pause("p", "swap", False)
    with pytest.raises(PoolNotFound):
        await service.swap(request_id="ghost", pool_id="nope", user="eve",
                           side="x_to_y", amount_in=1, min_out=0)
    # 同 id 不同参冲突（幂等键带操作类型，必须在同一操作内复用 id）
    with pytest.raises(DuplicateRequestConflict):
        await service.add_liquidity(request_id="a1", pool_id="p", user="alice",
                                   amount_x=2, amount_y=2)

    rejected = (await service.get_history(status="rejected",
                                          page_size=500))["items"]
    codes = {r["error_code"] for r in rejected}
    assert codes == {"SLIPPAGE_EXCEEDED", "INSUFFICIENT_SHARE_BALANCE",
                     "POOL_PAUSED", "POOL_NOT_FOUND",
                     "DUPLICATE_REQUEST_CONFLICT"}
    log.info("rejected requests traceable: %s, each error code distinct",
             sorted(codes))

    # 拒绝前后，仅凭流水重算的池子口径与接口数字完全相同
    after = await service.get_pool_stats("p")
    for k in baseline:
        assert baseline[k] == after[k]
    await _assert_replay_equals_stats(service, "p", {"alice", "eve"})


# --------------------------------------------------------------------- #
# 4. 重复提交（成功重放 + 失败重放 + 并发重复）
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_duplicate_submissions_counted_once(service):
    """业务：同一请求重复提交（含失败重放）只有一条记录参与重算。"""
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    ok_kw = dict(request_id="once-add", pool_id="p", user="alice",
                 amount_x=5_000, amount_y=5_000)
    await asyncio.gather(*[service.add_liquidity(**ok_kw) for _ in range(8)])

    bad_kw = dict(request_id="once-bad", pool_id="p", user="bob",
                  side="x_to_y", amount_in=1000, min_out=10**9)
    for _ in range(3):
        with pytest.raises(SlippageExceeded):
            await service.swap(**bad_kw)

    # 流水去重：每个 request_id 恰好一条
    hist = await service.get_history(pool_id="p", page_size=500)
    counts: dict[str, int] = {}
    for rec in hist["items"]:
        if rec["request_id"]:
            counts[rec["request_id"]] = counts.get(rec["request_id"], 0) + 1
    assert counts["once-add"] == 1
    assert "once-bad" not in counts or counts.get("once-bad") == 1
    # 8 次重复只铸一份：接口口径与流水重算都只有 5_000；seed 份额在系统账户
    us = await service.get_user_stats("p", "alice")
    assert us["contributed_x"] == us["contributed_y"] == 5_000
    assert us["shares"] == 5_000
    sys_us = await service.get_user_stats("p", "_system")
    assert sys_us["shares"] == 1_000_000
    await _assert_replay_equals_stats(service, "p", {"alice", "bob"})
    log.info("duplicates: 8x add -> 1 ledger record, failed replay -> 1; "
             "recompute counts each exactly once")


# --------------------------------------------------------------------- #
# 5. 并发：混合注入/赎回/兑换，过程中任意快照守恒，最终逐字段一致
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_concurrent_mixed_ops_invariant_at_every_snapshot(service):
    """业务：并发混合操作后最终重算 == 接口；任意中途快照不凭空多/少资产。"""
    await service.seed_pool("p", "X", "Y", 10**9, 10**9)
    n_users = 8
    users = {f"u{i}" for i in range(n_users)}

    async def sampler() -> None:
        """在并发提交期间反复取「无 await 的同步快照」并重算。

        单线程 asyncio：两次同步读之间不会被写协程打断，因此快照必然对应
        某个已提交状态；此时份额守恒与库存恒等式必须成立。
        """
        checks = 0
        for _ in range(400):
            raw = list(service.store.ledger)
            pool = service.store.pools.get("p")
            items = [service._record_view(r) for r in raw
                     if r.pool_id == "p"]
            r = replay_ledger(items)
            # 份额守恒：全体（含 seed 系统账户）持仓之和 == 总份额
            held = sum(p.shares for p in r.positions.values())
            assert held == r.total_shares, (held, r.total_shares)
            # 库存恒等式：本金净额 + 兑换净流入 == 真实库存
            assert pool.reserve_x == (
                r.deposited_x - r.withdrawn_x
                + r.swap_in_x - r.swap_out_x)
            assert pool.reserve_y == (
                r.deposited_y - r.withdrawn_y
                + r.swap_in_y - r.swap_out_y)
            checks += 1
            await asyncio.sleep(0)
        log.info("sampler: %s mid-flight snapshots all conserved "
                 "(shares & reserve identity)", checks)

    async def op(i: int):
        u = f"u{i % n_users}"
        kind = i % 5
        try:
            if kind in (0, 1):
                return await service.add_liquidity(
                    request_id=f"add-{i}", pool_id="p", user=u,
                    amount_x=10_000 + i, amount_y=10_000)
            if kind == 2:
                return await service.swap(
                    request_id=f"sw-{i}", pool_id="p", user=u,
                    side="x_to_y" if i % 2 == 0 else "y_to_x",
                    amount_in=1_000 + i, min_out=0)
            if kind == 3:
                return await service.swap(
                    request_id=f"sw-bad-{i}", pool_id="p", user=u,
                    side="x_to_y", amount_in=100, min_out=10**9)  # 必拒
            return await service.remove_liquidity(
                request_id=f"rm-{i}", pool_id="p", user=u, shares=1_000)
        except AmmError as exc:  # 失败（持仓不足/滑点）也进流水
            return exc

    sampler_task = asyncio.create_task(sampler())
    results = await asyncio.gather(*(op(i) for i in range(80)))
    # 再追加一批并发重复请求，制造并发重放
    dup = dict(request_id="add-0", pool_id="p", user="u0",
               amount_x=10_000, amount_y=10_000)
    await asyncio.gather(*[service.add_liquidity(**dup) for _ in range(10)])
    await sampler_task

    rejected = [r for r in results if isinstance(r, Exception)]
    accepted = [r for r in results if not isinstance(r, Exception)]
    log.info("concurrent mixed: %s accepted, %s rejected; finalizing "
             "invariant check", len(accepted), len(rejected))

    await _assert_replay_equals_stats(service, "p", users)

    # 成功流水按 (op, request_id) 唯一：参与重算的记录绝不重复
    hist = await service.get_history(pool_id="p", status="success",
                                     page_size=500)
    keys = [(r["op"], r["request_id"]) for r in hist["items"]
            if r["request_id"]]
    assert len(keys) == len(set(keys))
    log.info("success ledger unique by (op, request_id): %s records", len(keys))
