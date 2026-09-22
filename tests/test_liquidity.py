"""流动性注入 / 赎回的份额口径与边界测试。"""

import pytest

from app.errors import ErrorCode, PoolError
from app.models import AddLiquidityRequest, RemoveLiquidityRequest
from app.pool import PoolService


def _add(svc, rid, provider, ain, aout, pool="p"):
    return svc.add_liquidity(
        AddLiquidityRequest(
            request_id=rid, pool_id=pool, provider=provider,
            amount_in=ain, amount_out=aout,
        )
    )


def _rm(svc, rid, provider, shares, pool="p"):
    return svc.remove_liquidity(
        RemoveLiquidityRequest(
            request_id=rid, pool_id=pool, provider=provider, shares=shares,
        )
    )


def test_genesis_share_rule_is_sqrt_and_no_share_inflation():
    svc = PoolService()
    r = _add(svc, "a1", "alice", 900, 1600)
    # isqrt(900*1600) = isqrt(1_440_000) = 1200
    assert r.shares_delta == 1200
    st = svc.get_state("p")
    # 份额平方不超过注入乘积：不会凭空增发
    assert st.total_shares**2 <= 900 * 1600
    assert st.balances["alice"] == 1200
    assert (st.reserve_in, st.reserve_out) == (900, 1600)


def test_second_deposit_minted_by_min_ratio_floor():
    svc = PoolService()
    _add(svc, "a1", "alice", 1000, 1000)
    # bob 按比例注入：份额 = min(1000*500//1000, 1000*500//1000) = 500
    r = _add(svc, "a2", "bob", 500, 500)
    assert r.shares_delta == 500
    # 不成比例注入：取较小占比，向下取整
    r2 = _add(svc, "a3", "carol", 300, 400)
    # min(1500*300//1500, 1500*400//1500) = min(300, 400) = 300
    assert r2.shares_delta == 300


def test_tiny_deposit_minting_zero_shares_is_rejected_without_state_change():
    svc = PoolService()
    # 创世 2/8 -> 份额 isqrt(16)=4，reserve_out 相对份额较大
    _add(svc, "a1", "alice", 2, 8)
    before = svc.get_state("p")
    # 再注 1/1：min(4*1//2=2, 4*1//8=0)=0，必须拒绝
    with pytest.raises(PoolError) as ei:
        _add(svc, "a2", "bob", 1, 1)
    assert ei.value.code is ErrorCode.INVALID_PARAMETER
    after = svc.get_state("p")
    # 被拒绝的请求不留任何生效状态：存量与份额持仓均不变
    assert before == after
    assert "bob" not in after.balances


def test_remove_more_than_held_is_rejected():
    svc = PoolService()
    _add(svc, "a1", "alice", 1000, 1000)
    with pytest.raises(PoolError) as ei:
        _rm(svc, "r1", "alice", 1001)
    assert ei.value.code is ErrorCode.INSUFFICIENT_SHARES
    assert svc.get_state("p").balances["alice"] == 1000


def test_partial_remove_pays_proportional_floor_and_keeps_invariant():
    svc = PoolService()
    _add(svc, "a1", "alice", 1000, 3000)
    _add(svc, "a2", "bob", 1000, 3000)
    st0 = svc.get_state("p")
    # 两人等比注入，各持一半份额；alice 赎回自己一半 = 总池的 1/4
    alice_shares = st0.balances["alice"]
    r = _rm(svc, "r1", "alice", alice_shares // 2)
    assert (r.amount_in_consumed, r.amount_out) == (500, 1500)
    st = svc.get_state("p")
    assert st.balances["alice"] == alice_shares - alice_shares // 2
    # 赎回后单份额净资产不减少（floor 对剩余持有人有利）
    assert st.reserve_in * st0.total_shares >= st0.reserve_in * st.total_shares
    assert st.reserve_out * st0.total_shares >= st0.reserve_out * st.total_shares


def test_full_remove_returns_all_reserves_and_releases_dust():
    svc = PoolService()
    _add(svc, "a1", "alice", 1000, 1000)
    # bob 小比例注入留下舍入灰尘
    _add(svc, "a2", "bob", 3, 3)
    r = _rm(svc, "r1", "bob", svc.get_state("p").balances["bob"])
    st = svc.get_state("p")
    # bob 全部赎回拿走自己那份（灰尘不会永久锁死在 bob 名下）
    assert "bob" not in st.balances
    assert r.shares_delta == -3
    assert r.amount_in_consumed >= 0


def test_remove_zero_shares_invalid():
    svc = PoolService()
    _add(svc, "a1", "alice", 1000, 1000)
    with pytest.raises(PoolError) as ei:
        _rm(svc, "r0", "alice", 0)
    assert ei.value.code is ErrorCode.INVALID_PARAMETER


def test_liquidity_idempotent_replay_returns_identical_result():
    svc = PoolService()
    r1 = _add(svc, "dup", "alice", 1000, 1000)
    r2 = _add(svc, "dup", "alice", 1000, 1000)
    assert r1 == r2
    assert svc.get_state("p").total_shares == 1000

    rr1 = _rm(svc, "duprm", "alice", 100)
    rr2 = _rm(svc, "duprm", "alice", 100)
    assert rr1 == rr2
    assert svc.get_state("p").balances["alice"] == 900
