"""带成交保护的兑换：报价/最小成交量/有效期校验与不变量。"""

import pytest

from app.errors import ErrorCode, PoolError
from app.models import AddLiquidityRequest, SwapRequest
from app.pool import FEE_DENOMINATOR, FEE_NUMERATOR, PoolService


def _bootstrap():
    svc = PoolService()
    svc.add_liquidity(
        AddLiquidityRequest(
            request_id="seed", pool_id="p", provider="lp",
            amount_in=1_000_000, amount_out=1_000_000,
        )
    )
    return svc


def _swap_req(rid, amount_in, *, quote, min_out, valid_until, trader="t"):
    return SwapRequest(
        request_id=rid, pool_id="p", trader=trader, amount_in=amount_in,
        min_amount_out=min_out, quote_amount_out=quote,
        quote_valid_until_ms=valid_until,
    )


def test_swap_success_matches_quote_respects_min_and_grows_k():
    svc = _bootstrap()
    before = svc.get_state("p")
    q = svc.quote("p", 100_000)
    r = svc.swap(
        _swap_req("s1", 100_000, quote=q, min_out=q, valid_until=2_000),
        now_ms=1_000,
    )
    assert r.amount_out == q
    after = svc.get_state("p")
    assert after.reserve_in == before.reserve_in + 100_000
    assert after.reserve_out == before.reserve_out - q
    # 不变量：手续费使 K=reserve_in*reserve_out 严格不减
    assert after.reserve_in * after.reserve_out >= before.reserve_in * before.reserve_out


def test_swap_fee_formula_explained():
    svc = _bootstrap()
    amount_in = 100_000
    in_after_fee = amount_in - amount_in * FEE_NUMERATOR // FEE_DENOMINATOR
    expected = 1_000_000 * in_after_fee // (1_000_000 + in_after_fee)
    assert svc.quote("p", amount_in) == expected


def test_expired_request_rejected_distinctly():
    svc = _bootstrap()
    q = svc.quote("p", 1_000)
    before = svc.get_state("p")
    with pytest.raises(PoolError) as ei:
        svc.swap(
            _swap_req("e1", 1_000, quote=q, min_out=1, valid_until=999),
            now_ms=1_000,
        )
    assert ei.value.code is ErrorCode.REQUEST_EXPIRED
    assert svc.get_state("p") == before  # 无部分生效


def test_stale_quote_rejected_when_reserves_changed():
    svc = _bootstrap()
    stale_q = svc.quote("p", 100_000)
    # 另一笔成交先改变行情
    q2 = svc.quote("p", 50_000)
    svc.swap(
        _swap_req("move", 50_000, quote=q2, min_out=q2, valid_until=5_000),
        now_ms=1_000,
    )
    before = svc.get_state("p")
    with pytest.raises(PoolError) as ei:
        svc.swap(
            _swap_req("stale", 100_000, quote=stale_q, min_out=1, valid_until=5_000),
            now_ms=2_000,
        )
    assert ei.value.code is ErrorCode.STALE_QUOTE
    assert svc.get_state("p") == before


def test_slippage_rejected_when_output_below_min():
    svc = _bootstrap()
    q = svc.quote("p", 100_000)
    before = svc.get_state("p")
    with pytest.raises(PoolError) as ei:
        # 报价与当前行情一致（新鲜），但最小成交量高于实际成交
        svc.swap(
            _swap_req("sl", 100_000, quote=q, min_out=q + 1, valid_until=5_000),
            now_ms=1_000,
        )
    assert ei.value.code is ErrorCode.SLIPPAGE
    assert svc.get_state("p") == before


def test_insufficient_liquidity_rejected_for_dust_input():
    svc = _bootstrap()
    # 极小投入在当前存量下应得 0
    assert svc.quote("p", 1) == 0
    before = svc.get_state("p")
    with pytest.raises(PoolError) as ei:
        svc.swap(
            _swap_req("dust", 1, quote=0, min_out=0, valid_until=5_000),
            now_ms=1_000,
        )
    assert ei.value.code is ErrorCode.INSUFFICIENT_LIQUIDITY
    assert svc.get_state("p") == before


@pytest.mark.parametrize(
    "kwargs,code",
    [
        (dict(amount_in=0, quote=1, min_out=1, valid_until=5_000),
         ErrorCode.INVALID_PARAMETER),
        (dict(amount_in=100, quote=1, min_out=1, valid_until=0),
         ErrorCode.INVALID_PARAMETER),
    ],
)
def test_invalid_parameters_rejected(kwargs, code):
    svc = _bootstrap()
    before = svc.get_state("p")
    with pytest.raises(PoolError) as ei:
        svc.swap(_swap_req("bad", **kwargs), now_ms=1_000)
    assert ei.value.code is code
    assert svc.get_state("p") == before


def test_swap_on_empty_pool_rejected():
    svc = PoolService()
    with pytest.raises(PoolError) as ei:
        svc.swap(
            _swap_req("nopool", 100, quote=1, min_out=1, valid_until=5_000),
            now_ms=1_000,
        )
    assert ei.value.code is ErrorCode.POOL_NOT_FOUND


def test_swap_idempotent_replay_even_after_expiry():
    svc = _bootstrap()
    q = svc.quote("p", 100_000)
    req = _swap_req("idem", 100_000, quote=q, min_out=q, valid_until=5_000)
    r1 = svc.swap(req, now_ms=1_000)
    # 即使重放时本地时间已远超有效期，仍返回首次一致结论且不重复扣减
    r2 = svc.swap(req, now_ms=999_999_999)
    assert r1 == r2
    st = svc.get_state("p")
    assert st.reserve_in == 1_000_000 + 100_000
    assert st.reserve_out == 1_000_000 - q


def test_same_request_id_with_different_body_conflicts():
    svc = _bootstrap()
    q = svc.quote("p", 100_000)
    svc.swap(
        _swap_req("x", 100_000, quote=q, min_out=q, valid_until=5_000),
        now_ms=1_000,
    )
    before = svc.get_state("p")
    with pytest.raises(PoolError) as ei:
        svc.swap(
            _swap_req("x", 100_001, quote=q, min_out=q, valid_until=5_000),
            now_ms=1_000,
        )
    assert ei.value.code is ErrorCode.DUPLICATE_IDEMPOTENCY_CONFLICT
    assert svc.get_state("p") == before
