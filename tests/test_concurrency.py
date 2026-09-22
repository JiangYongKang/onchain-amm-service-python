"""并发兑换 / 并发提交：不互相覆盖、不重复扣减、不变量保持。"""

from concurrent.futures import ThreadPoolExecutor

from app.models import AddLiquidityRequest, RemoveLiquidityRequest, SwapRequest
from app.pool import PoolService


def _seed(svc, pool="p", rin=10_000_000, rout=10_000_000):
    svc.add_liquidity(
        AddLiquidityRequest(
            request_id=f"seed-{pool}", pool_id=pool, provider="lp",
            amount_in=rin, amount_out=rout,
        )
    )


def test_concurrent_swaps_never_overwrite_and_preserve_invariant():
    svc = PoolService()
    _seed(svc)
    n = 50
    amount_in = 10_000

    # 每个 request_id 预先取价；串行取价后再并发提交，
    # 只有与提交瞬间行情一致的报价成交，其余按 STALE_QUOTE 拒绝 ——
    # 无论成交几笔，池子守恒都必须成立。
    quotes = {i: svc.quote("p", amount_in) for i in range(n)}

    def do(i):
        req = SwapRequest(
            request_id=f"s{i}", pool_id="p", trader=f"t{i}",
            amount_in=amount_in, min_amount_out=1,
            quote_amount_out=quotes[i], quote_valid_until_ms=10**15,
        )
        try:
            return ("ok", svc.swap(req, now_ms=1_000).amount_out)
        except Exception as e:  # noqa: BLE001 - 测试中分类统计即可
            return ("rej", type(e).__name__)

    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(do, range(n)))

    oks = [r for r in results if r[0] == "ok"]
    st = svc.get_state("p")
    # 存量恒等式：成功笔数决定两边存量
    assert st.reserve_in == 10_000_000 + len(oks) * amount_in
    total_out = sum(out for _, out in oks)
    assert st.reserve_out == 10_000_000 - total_out
    # 不变量：K 不减
    assert st.reserve_in * st.reserve_out >= 10_000_000 * 10_000_000
    # 份额不受兑换影响
    assert st.total_shares == 10_000_000


def test_duplicate_concurrent_submissions_settle_once():
    svc = PoolService()
    _seed(svc)
    q = svc.quote("p", 20_000)
    req_kwargs = dict(
        request_id="same-id", pool_id="p", trader="t",
        amount_in=20_000, min_amount_out=1,
        quote_amount_out=q, quote_valid_until_ms=10**15,
    )

    def do(_i):
        # 每次构造独立对象，模拟同一笔请求重复到达
        try:
            return ("ok", svc.swap(SwapRequest(**req_kwargs), now_ms=1_000).amount_out)
        except Exception as e:  # noqa: BLE001
            return ("rej", type(e).__name__)

    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(do, range(40)))

    oks = [r for r in results if r[0] == "ok"]
    # 同一 request_id：恰好一笔生效，其余全部返回一致的幂等结果
    assert len(oks) == 40
    assert len({out for _, out in oks}) == 1
    st = svc.get_state("p")
    assert st.reserve_in == 10_000_000 + 20_000
    assert st.reserve_out == 10_000_000 - q


def test_concurrent_add_and_remove_shares_never_double_spend():
    svc = PoolService()
    _seed(svc)

    def add(i):
        return svc.add_liquidity(
            AddLiquidityRequest(
                request_id=f"add{i}", pool_id="p", provider=f"u{i % 5}",
                amount_in=10_000, amount_out=10_000,
            )
        ).shares_delta

    def over_redeem(i):
        # 每人只被注入一次（约 10000 份额），申请赎回远超持有：必须全部拒绝
        try:
            svc.remove_liquidity(
                RemoveLiquidityRequest(
                    request_id=f"rm{i}", pool_id="p",
                    provider=f"u{i % 5}", shares=10**12,
                )
            )
            return False
        except Exception:  # noqa: BLE001
            return True

    with ThreadPoolExecutor(max_workers=16) as ex:
        minted = list(ex.map(add, range(50)))
        rejected = list(ex.map(over_redeem, range(50)))

    assert all(rejected)  # 超持有赎回无一漏网
    st = svc.get_state("p")
    # 总份额 = 创世 + 全部并发铸出的份额之和，无凭空增发/消失
    assert st.total_shares == 10_000_000 + sum(minted)
    assert sum(st.balances.values()) == st.total_shares
