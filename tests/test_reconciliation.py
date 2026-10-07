"""对账不变量：仅凭历史流水逐笔重算的结果 == 统计接口报出的结果。

覆盖业务
--------
1. 注入/赎回/兑换混合后，池子统计（本金/手续费/流量/笔数）逐字段相等；
2. 每个用户的份额、累计贡献、累计赎回、份额占比与手续费分成逐字段相等
   （分成向下取整，与端点同一公式）；
3. 仅凭流水还原的总份额/用户持仓满足份额守恒，流量净额还原出真实库存；
4. 被拒绝请求仍可在流水里查到且 error_code 可区分，但不参与任何口径；
5. 重复提交（成功/失败重放）只留一条参与重算的记录；
6. 多笔并发注入/赎回/兑换后，最终重算与接口一致；
7. 任何中间时刻重算都不凭空多/少资产（中途快照 + 最终快照）；
8. 成功明细自带还原要素（注入两侧金额+份额；赎回销毁份额+两侧取出）。
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from amm_service.errors import (
    InsufficientShareBalance,
    SlippageExceeded,
)
from amm_service.reconciliation import SYSTEM_USER, recompute_from_records

log = logging.getLogger("test.reconciliation")


async def _fetch_full_ledger(service, pool_id: str) -> list[dict]:
    """翻页拉全某池子的完整流水（不过滤 op/status：seed 是本金出处）。"""
    items: list[dict] = []
    page = 1
    while True:
        h = await service.get_history(pool_id=pool_id, page=page, page_size=500)
        items.extend(h["items"])
        if len(items) >= h["total"]:
            return items
        page += 1


async def _assert_recompute_equals_service(service, pool_id: str) -> dict:
    """核心不变量：流水重算 == 池子统计接口 == 每个用户统计接口。"""
    records = await _fetch_full_ledger(service, pool_id)
    rc = recompute_from_records(records, pool_id=pool_id)
    pool_stats = await service.get_pool_stats(pool_id)
    pool_view = await service.get_pool(pool_id)
    rp = rc["pools"][pool_id]

    # 1) 池子维度逐字段一致（含 principal_* / fee_* / swap_* / 笔数）
    assert rp == pool_stats, f"pool stats mismatch:\n{rp}\n{pool_stats}"

    # 2) 仅凭流水还原库存：本金净额 + 兑换净流入 == 真实库存
    assert (rp["principal_x"] + rp["swap_in_x"] - rp["swap_out_x"]
            == pool_view["reserve_x"])
    assert (rp["principal_y"] + rp["swap_in_y"] - rp["swap_out_y"]
            == pool_view["reserve_y"])

    # 3) 仅凭流水还原总份额：用户持仓之和 == 池子总份额（份额守恒）
    total_from_users = sum(u["shares"] for u in rc["users"][pool_id].values())
    assert total_from_users == pool_view["total_shares"]

    # 4) 用户维度逐字段一致（份额/贡献/赎回/占比/手续费分成，取整口径一致）
    for user, ru in rc["users"][pool_id].items():
        live = await service.get_user_stats(pool_id, user)
        assert ru == live, f"user {user} stats mismatch:\n{ru}\n{live}"

    log.info(
        "recompute==service pool=%s principal=(%s,%s) fee=(%s,%s) "
        "swap_count=%s reserves=(%s,%s) total_shares=%s users=%s",
        pool_id, rp["principal_x"], rp["principal_y"], rp["fee_x"], rp["fee_y"],
        rp["swap_count"], pool_view["reserve_x"], pool_view["reserve_y"],
        pool_view["total_shares"], sorted(rc["users"][pool_id]),
    )
    return rc


@pytest.mark.asyncio
async def test_mixed_add_remove_swap_recompute_matches_stats(service):
    """业务：注入/赎回/兑换混合 + 非等比注入，重算结果与接口逐字段一致。

    预期：本金随注入增加、随赎回减少；非等比注入的多余侧金额计入本金但
    不增发份额；两笔兑换在两侧分别累计 fee/流量；所有数字都能逐笔追溯。
    """
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    a1 = await service.add_liquidity(
        request_id="a1", pool_id="p", user="alice",
        amount_x=10_000, amount_y=10_000)
    # 非等比：x 投 10%、y 投 20%，铸份额按较小侧 = 10_000
    a2 = await service.add_liquidity(
        request_id="a2", pool_id="p", user="bob",
        amount_x=10_000, amount_y=20_000)
    assert a2["shares"] == 10_000
    await service.swap(request_id="s1", pool_id="p", user="trader",
                       side="x_to_y", amount_in=50_000, min_out=0)
    await service.swap(request_id="s2", pool_id="p", user="trader",
                       side="y_to_x", amount_in=12_345, min_out=0)
    r1 = await service.remove_liquidity(
        request_id="r1", pool_id="p", user="alice", shares=4_000)
    log.info("mixed ops: add alice minted=%s, add bob minted=%s (uneven), "
             "remove alice burned=4000 out=(%s,%s)",
             a1["shares"], a2["shares"], r1["amount_x"], r1["amount_y"])
    await _assert_recompute_equals_service(service, "p")


@pytest.mark.asyncio
async def test_success_ledger_detail_carries_full_restoration_elements(service):
    """业务：成功明细带齐还原要素（注入：两侧金额+铸出份额；赎回：销毁份额+两侧数量）。

    预期：add detail 含 shares/amount_x/amount_y；
    remove detail 含 shares_burned/amount_x/amount_y；
    swap detail 含 side/amount_in/amount_out/fee_in。
    """
    await service.seed_pool("p", "X", "Y", 0, 0)
    await service.add_liquidity(request_id="a1", pool_id="p", user="u",
                                amount_x=123, amount_y=123)
    await service.swap(request_id="s1", pool_id="p", user="t",
                       side="x_to_y", amount_in=37, min_out=0)
    await service.remove_liquidity(request_id="r1", pool_id="p", user="u",
                                   shares=50)
    records = await _fetch_full_ledger(service, "p")
    by = {(r["op"], r["request_id"]): r for r in records}
    add_d = by[("add", "a1")]["detail"]
    rm_d = by[("remove", "r1")]["detail"]
    sw_d = by[("swap", "s1")]["detail"]
    assert (add_d["shares"], add_d["amount_x"], add_d["amount_y"]) == (123, 123, 123)
    assert rm_d["shares_burned"] == 50
    assert rm_d["amount_x"] >= 0 and rm_d["amount_y"] >= 0
    assert sw_d["side"] == "x_to_y"
    assert sw_d["amount_in"] == 37 and sw_d["amount_out"] >= 0
    assert sw_d["fee_in"] == 37 - 37 * (1_000_000 - 3000) // 1_000_000
    log.info("restoration elements present: add=%s remove=%s swap=%s",
             {k: add_d[k] for k in ("shares", "amount_x", "amount_y")},
             {k: rm_d[k] for k in ("shares_burned", "amount_x", "amount_y")},
             {k: sw_d[k] for k in ("side", "amount_in", "amount_out", "fee_in")})


@pytest.mark.asyncio
async def test_rejected_requests_traceable_but_excluded_from_totals(service):
    """业务：被拒绝的注入/赎回/兑换留痕可查、原因可区分，但不参与重算口径。

    预期：拒绝前后池子/用户统计完全不变；rejected 记录的 error_code 各不相同；
    重算时仅消费 success 记录，结果与接口一致。
    """
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    await service.add_liquidity(request_id="a1", pool_id="p", user="alice",
                                amount_x=10_000, amount_y=10_000)
    await _assert_recompute_equals_service(service, "p")  # 拒绝前基线

    with pytest.raises(SlippageExceeded):
        await service.swap(request_id="bad-swap", pool_id="p", user="x",
                           side="x_to_y", amount_in=10_000, min_out=10**9)
    with pytest.raises(InsufficientShareBalance):
        await service.remove_liquidity(request_id="bad-rm", pool_id="p",
                                       user="alice", shares=10**9)
    with pytest.raises(SlippageExceeded):
        await service.add_liquidity(request_id="bad-add", pool_id="p",
                                    user="bob", amount_x=1, amount_y=1,
                                    min_shares=10**9)

    rejected = await service.get_history(pool_id="p", status="rejected",
                                         page_size=500)
    codes = {r["request_id"]: r["error_code"] for r in rejected["items"]}
    assert codes == {"bad-swap": "SLIPPAGE_EXCEEDED",
                     "bad-rm": "INSUFFICIENT_SHARE_BALANCE",
                     "bad-add": "SLIPPAGE_EXCEEDED"}
    log.info("rejected ops traceable with distinct error_code: %s", codes)
    await _assert_recompute_equals_service(service, "p")  # 拒绝后口径不变


@pytest.mark.asyncio
async def test_duplicate_submissions_counted_once_in_recompute(service):
    """业务：同参数重复提交（含失败重放）在流水中只留一条参与重算的记录。

    预期：成功注入/赎回/兑换各重复 N 次，本金、份额、手续费、笔数都只计一次；
    失败请求重复提交也只留一条 rejected；重算 == 接口。
    """
    await service.seed_pool("p", "X", "Y", 1_000_000, 1_000_000)
    add_kw = dict(request_id="dup-add", pool_id="p", user="alice",
                  amount_x=5_000, amount_y=5_000)
    adds = await asyncio.gather(*[service.add_liquidity(**add_kw)
                                  for _ in range(8)])
    assert sum(1 for r in adds if r["replayed"]) == 7

    swap_kw = dict(request_id="dup-swap", pool_id="p", user="alice",
                   side="x_to_y", amount_in=1000, min_out=0)
    swaps = await asyncio.gather(*[service.swap(**swap_kw) for _ in range(6)])
    assert sum(1 for r in swaps if r["replayed"]) == 5

    rm_kw = dict(request_id="dup-rm", pool_id="p", user="alice", shares=1000)
    rms = await asyncio.gather(*[service.remove_liquidity(**rm_kw)
                                 for _ in range(4)])
    assert sum(1 for r in rms if r["replayed"]) == 3

    # 失败重放：同参数重复 5 次，只留一条 rejected
    # amount_in=1000 产生真实输出，但 min_out=10^9 必然触发滑点拒绝
    for _ in range(5):
        with pytest.raises(SlippageExceeded):
            await service.swap(request_id="dup-bad", pool_id="p", user="alice",
                               side="x_to_y", amount_in=1000, min_out=10**9)

    records = await _fetch_full_ledger(service, "p")
    counts: dict[tuple[str, str], int] = {}
    for r in records:
        counts[(r["op"], r["request_id"])] = counts.get(
            (r["op"], r["request_id"]), 0) + 1
    assert counts[("add", "dup-add")] == 1
    assert counts[("swap", "dup-swap")] == 1
    assert counts[("remove", "dup-rm")] == 1
    assert counts[("swap", "dup-bad")] == 1
    log.info("duplicates (8 add/6 swap/4 remove/5 failed) -> exactly one "
             "ledger record each; replayed responses detected")
    await _assert_recompute_equals_service(service, "p")


@pytest.mark.asyncio
async def test_concurrent_mixed_ops_final_recompute_matches(service):
    """业务：多笔并发注入/赎回/兑换（含必然失败项）后，最终重算与接口一致。

    预期：锁串行化使每笔成功操作恰好落一次账；失败项不影响任何口径；
    最终总份额、库存、本金、手续费、每个用户的累计贡献/赎回全部可从流水还原。
    """
    await service.seed_pool("p", "X", "Y", 0, 0)
    # 先并发首注（只有第一笔是首注；其余按当时比例正常注入）
    add_users = [f"lp{i}" for i in range(10)]

    async def add_one(i: int):
        return await service.add_liquidity(
            request_id=f"add-{i}", pool_id="p", user=add_users[i],
            amount_x=10_000, amount_y=10_000)

    adds = await asyncio.gather(*(add_one(i) for i in range(10)),
                                return_exceptions=True)
    assert sum(1 for r in adds if not isinstance(r, Exception)) == 10

    # 并发两向兑换 + 一半必然滑点失败
    async def swap_one(i: int):
        return await service.swap(
            request_id=f"sw-{i}", pool_id="p", user=f"tr{i}",
            side="x_to_y" if i % 2 == 0 else "y_to_x",
            amount_in=2_000, min_out=0 if i % 2 == 0 else 10**9)

    swaps = await asyncio.gather(*(swap_one(i) for i in range(40)),
                                 return_exceptions=True)
    ok_swaps = [r for r in swaps if not isinstance(r, Exception)]
    assert len(ok_swaps) == 20

    # 并发赎回（每个 LP 赎回自己一半份额）
    async def rm_one(i: int):
        return await service.remove_liquidity(
            request_id=f"rm-{i}", pool_id="p", user=add_users[i], shares=5_000)

    rms = await asyncio.gather(*(rm_one(i) for i in range(10)),
                               return_exceptions=True)
    assert sum(1 for r in rms if not isinstance(r, Exception)) == 10

    rc = await _assert_recompute_equals_service(service, "p")
    # 用户口径：10 个 LP；seed 为 (0,0)，_system 全 0（与端点惰性建位语义一致）；
    # 失败/纯兑换用户不留下任何用户口径。
    assert set(rc["users"]["p"]) == set(add_users) | {SYSTEM_USER}
    assert rc["users"]["p"][SYSTEM_USER]["shares"] == 0
    log.info("concurrent mixed: 10 adds + 20/40 swaps + 10 removes; "
             "recompute matches service exactly")


@pytest.mark.asyncio
async def test_recompute_at_intermediate_points_never_creates_assets(service):
    """业务：任何中间时刻只凭已有流水重算，都与当时接口一致（不凭空多/少）。

    预期：每个检查点（被拒绝前后、重复提交前后）重算 == 接口，
    说明流水在时间轴上的任何前缀都是自洽的。
    """
    await service.seed_pool("p", "X", "Y", 10**7, 10**7)
    checkpoints = 0
    for i in range(6):
        await service.add_liquidity(request_id=f"a{i}", pool_id="p",
                                    user=f"u{i % 3}",
                                    amount_x=3_000 + i, amount_y=3_000)
        await service.swap(request_id=f"s{i}", pool_id="p", user="trader",
                           side="x_to_y" if i % 2 else "y_to_x",
                           amount_in=500 + i, min_out=0)
        if i % 2 == 0:
            await service.remove_liquidity(request_id=f"r{i}", pool_id="p",
                                           user=f"u{i % 3}", shares=100)
        # 必然失败请求穿插其中（1000 输入有真实输出，min_out 高到必然滑点）
        with pytest.raises(SlippageExceeded):
            await service.swap(request_id=f"bad{i}", pool_id="p", user="x",
                               side="x_to_y", amount_in=1000, min_out=10**9)
        # 重复重放前一个成功请求
        if i > 0:
            await service.swap(request_id=f"s{i - 1}", pool_id="p",
                               user="trader",
                               side="x_to_y" if (i - 1) % 2 else "y_to_x",
                               amount_in=500 + i - 1, min_out=0)
        await _assert_recompute_equals_service(service, "p")
        checkpoints += 1
    log.info("intermediate recompute self-consistent at %s checkpoints "
             "(rejects/replays interleaved, no asset created or lost)",
             checkpoints)


@pytest.mark.asyncio
async def test_fee_share_floor_rounding_matches_service(service):
    """业务：手续费分成向下取整，重算与接口连 dust 取整结果都一致。

    预期：构造不能整除的手续费/份额比例，重算的 fee_share_* 与接口相等；
    全体用户分成之和 <= 池子累计手续费（舍入余量留存池内）。
    """
    await service.seed_pool("p", "X", "Y", 0, 0)
    await service.add_liquidity(request_id="a1", pool_id="p", user="alice",
                                amount_x=100_000, amount_y=100_000)
    await service.add_liquidity(request_id="a2", pool_id="p", user="bob",
                                amount_x=30_000, amount_y=30_000)
    await service.swap(request_id="s1", pool_id="p", user="t",
                       side="x_to_y", amount_in=10_000, min_out=0)
    rc = await _assert_recompute_equals_service(service, "p")
    stats = await service.get_pool_stats("p")
    total_fee = stats["fee_x"]
    share_sum = sum(u["fee_share_x"] for u in rc["users"]["p"].values())
    assert share_sum <= total_fee  # floor 余量留存
    log.info("floor rounding: pool fee_x=%s, allocated=%s, undistributed "
             "dust stays in pool", total_fee, total_fee - share_sum)


@pytest.mark.asyncio
async def test_seed_principal_and_system_position_are_ledger_derivable(service):
    """业务：seed 初始库存计入本金且归属系统账户，全部可从 seed 流水还原。

    预期：未发生任何用户操作时，重算 principal_* == seed 两侧库存；
    _system 用户的 contributed_* 与铸出份额可逐笔对到 seed 明细。
    """
    await service.seed_pool("p", "X", "Y", 777_000, 777_000)
    records = await _fetch_full_ledger(service, "p")
    seed = next(r for r in records if r["op"] == "seed")
    assert seed["detail"]["reserve_x"] == 777_000
    assert seed["detail"]["reserve_y"] == 777_000
    rc = await _assert_recompute_equals_service(service, "p")
    sysu = rc["users"]["p"][SYSTEM_USER]
    assert sysu["contributed_x"] == sysu["contributed_y"] == 777_000
    assert sysu["shares"] == seed["detail"]["total_shares"] == 777_000
    log.info("seed principal derivable: _system contributed=(777000,777000) "
             "shares=%s", sysu["shares"])
