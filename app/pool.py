"""本地做市资金池：状态、份额记账与兑换的纯逻辑。

所有金额 / 份额均为非负整数（最小货币单位），从口径上杜绝浮点误差。
"""

from dataclasses import dataclass, field
from functools import wraps
from math import isqrt as _isqrt
from threading import RLock
import logging

from pydantic import BaseModel

from app.errors import ErrorCode, PoolError
from app.models import (
    AddLiquidityRequest,
    OperationResult,
    PoolState,
    RemoveLiquidityRequest,
    SwapRequest,
)

logger = logging.getLogger("amm")

# 兑换手续费点数：30 / 10000 = 0.30%
FEE_NUMERATOR = 30
FEE_DENOMINATOR = 10000


def _logged(fn):
    """统一记录判定日志：成功打印请求/前后状态/依据，失败打印可区分错误码。"""

    @wraps(fn)
    def wrapper(self, req, *args, **kwargs):
        label = fn.__name__
        extra = f" args={args} kwargs={kwargs}" if (args or kwargs) else ""
        try:
            result = fn(self, req, *args, **kwargs)
        except PoolError as exc:
            logger.info(
                "%s 拒绝 | request=%s%s | code=%s | basis=%s",
                label,
                req.model_dump_json(),
                extra,
                exc.code.value,
                exc.message,
            )
            raise
        logger.info(
            "%s 成功 | request=%s%s | before=%s | after=%s | basis=%s",
            label,
            req.model_dump_json(),
            extra,
            result.pool_before.model_dump_json(),
            result.pool_after.model_dump_json(),
            result.reason,
        )
        return result

    return wrapper


@dataclass
class _Pool:
    pool_id: str
    reserve_in: int = 0
    reserve_out: int = 0
    total_shares: int = 0
    balances: dict[str, int] = field(default_factory=dict)


class PoolService:
    """线程安全的资金池服务：并发请求经由同一把锁串行化提交。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._pools: dict[str, _Pool] = {}
        # request_id -> (请求指纹, 首次执行的结论)
        self._results: dict[str, tuple[str, OperationResult]] = {}

    # ---- 内部工具 -------------------------------------------------------

    @staticmethod
    def _fingerprint(kind: str, req: BaseModel) -> str:
        return f"{kind}|" + ",".join(
            f"{k}={v}" for k, v in sorted(req.model_dump().items()) if k != "request_id"
        )

    def _replay(self, request_id: str, fingerprint: str) -> OperationResult | None:
        """重复请求走幂等：指纹一致返回首次结论，不一致则拒绝。"""
        seen = self._results.get(request_id)
        if seen is None:
            return None
        first_fp, first_result = seen
        if first_fp != fingerprint:
            raise PoolError(
                ErrorCode.DUPLICATE_IDEMPOTENCY_CONFLICT,
                f"request_id={request_id} 已用于另一请求体",
            )
        return first_result

    def _record(self, request_id: str, fingerprint: str, result: OperationResult) -> OperationResult:
        self._results[request_id] = (fingerprint, result)
        return result

    # ---- 快照与取池 -----------------------------------------------------

    def _snapshot(self, pool: _Pool) -> PoolState:
        return PoolState(
            pool_id=pool.pool_id,
            reserve_in=pool.reserve_in,
            reserve_out=pool.reserve_out,
            total_shares=pool.total_shares,
            balances=dict(pool.balances),
        )

    def _get_pool(self, pool_id: str) -> _Pool:
        pool = self._pools.get(pool_id)
        if pool is None:
            raise PoolError(ErrorCode.POOL_NOT_FOUND, f"池子不存在: {pool_id}")
        return pool

    # ---- 写操作 ---------------------------------------------------------

    @_logged
    def add_liquidity(self, req: AddLiquidityRequest) -> OperationResult:
        fp = self._fingerprint("add_liquidity", req)
        with self._lock:
            replay = self._replay(req.request_id, fp)
            if replay is not None:
                return replay

            if req.amount_in <= 0 or req.amount_out <= 0:
                raise PoolError(
                    ErrorCode.INVALID_PARAMETER,
                    "注入数量必须为正整数 (amount_in>0 且 amount_out>0)",
                )

            pool = self._pools.get(req.pool_id)
            created = pool is None
            if created:
                pool = _Pool(pool_id=req.pool_id)
                self._pools[req.pool_id] = pool
            before = self._snapshot(pool)

            if pool.total_shares == 0:
                # 创世注入：份额 = 两种资产数量的几何平均向下取整，
                # 保证 shares^2 <= in*out，份额不会凭空增发。
                shares = _isqrt(req.amount_in * req.amount_out)
                why = f"创世注入 shares=isqrt({req.amount_in}*{req.amount_out})={shares}"
            else:
                # 后续注入：按两种代币占比的较小值向下取整，
                # 保证单份额净资产不被稀释，灰尘留存池内归全体份额持有人。
                shares = min(
                    pool.total_shares * req.amount_in // pool.reserve_in,
                    pool.total_shares * req.amount_out // pool.reserve_out,
                )
                why = (
                    f"按占比 min("
                    f"{pool.total_shares}*{req.amount_in}//{pool.reserve_in}, "
                    f"{pool.total_shares}*{req.amount_out}//{pool.reserve_out})={shares}"
                )

            if shares <= 0:
                # 尚未落盘任何状态：拒绝不留痕
                if created:
                    del self._pools[req.pool_id]
                raise PoolError(
                    ErrorCode.INVALID_PARAMETER,
                    f"注入数量过小，按口径铸出的份额为 0（{why}）",
                )

            pool.reserve_in += req.amount_in
            pool.reserve_out += req.amount_out
            pool.total_shares += shares
            pool.balances[req.provider] = pool.balances.get(req.provider, 0) + shares

            after = self._snapshot(pool)
            result = OperationResult(
                request_id=req.request_id,
                status="ok",
                kind="add_liquidity",
                shares_delta=shares,
                amount_in_consumed=req.amount_in,
                amount_out=req.amount_out,
                pool_before=before,
                pool_after=after,
                reason=why,
            )
            return self._record(req.request_id, fp, result)

    @_logged
    def remove_liquidity(self, req: RemoveLiquidityRequest) -> OperationResult:
        fp = self._fingerprint("remove_liquidity", req)
        with self._lock:
            replay = self._replay(req.request_id, fp)
            if replay is not None:
                return replay

            if req.shares <= 0:
                raise PoolError(ErrorCode.INVALID_PARAMETER, "赎回份额必须为正整数 (shares>0)")

            pool = self._get_pool(req.pool_id)
            if pool.total_shares == 0:
                raise PoolError(ErrorCode.POOL_NOT_INITIALIZED, f"池子尚未初始化: {req.pool_id}")

            held = pool.balances.get(req.provider, 0)
            if held < req.shares:
                raise PoolError(
                    ErrorCode.INSUFFICIENT_SHARES,
                    f"持有份额 {held} < 申请赎回 {req.shares}",
                )

            before = self._snapshot(pool)

            if req.shares == pool.total_shares:
                # 全部赎回：取走全部存量，避免末位灰尘永久锁死。
                pay_in, pay_out = pool.reserve_in, pool.reserve_out
                why = f"全部赎回：取走全部存量 {pay_in}/{pay_out}"
            else:
                # 部分赎回：按占比向下取整；舍入灰尘留存池内。
                pay_in = pool.reserve_in * req.shares // pool.total_shares
                pay_out = pool.reserve_out * req.shares // pool.total_shares
                why = (
                    f"部分赎回：{pool.reserve_in}*{req.shares}//{pool.total_shares}={pay_in}, "
                    f"{pool.reserve_out}*{req.shares}//{pool.total_shares}={pay_out}"
                )
                if pay_in <= 0 or pay_out <= 0:
                    # 尚未改动任何状态：拒绝，避免份额被零对价销毁。
                    raise PoolError(
                        ErrorCode.INVALID_PARAMETER,
                        f"赎回份额过小，按口径某一代币应得为 0（{why}）",
                    )

            pool.reserve_in -= pay_in
            pool.reserve_out -= pay_out
            pool.total_shares -= req.shares
            new_held = held - req.shares
            if new_held == 0:
                del pool.balances[req.provider]
            else:
                pool.balances[req.provider] = new_held

            after = self._snapshot(pool)
            result = OperationResult(
                request_id=req.request_id,
                status="ok",
                kind="remove_liquidity",
                shares_delta=-req.shares,
                amount_in_consumed=pay_in,
                amount_out=pay_out,
                pool_before=before,
                pool_after=after,
                reason=why,
            )
            return self._record(req.request_id, fp, result)

    @_logged
    def swap(self, req: SwapRequest, now_ms: int) -> OperationResult:
        # 指纹只基于请求体（不含 now_ms）：迟到的重复请求仍返回首次结论。
        fp = self._fingerprint("swap", req)
        with self._lock:
            replay = self._replay(req.request_id, fp)
            if replay is not None:
                return replay

            # 1) 参数合法性
            if req.amount_in <= 0:
                raise PoolError(ErrorCode.INVALID_PARAMETER, "兑换投入必须为正整数 (amount_in>0)")
            if req.quote_valid_until_ms <= 0:
                raise PoolError(
                    ErrorCode.INVALID_PARAMETER,
                    "报价有效期必须为正整数 (quote_valid_until_ms>0)",
                )

            pool = self._get_pool(req.pool_id)

            # 2) 请求过期（本地时间超过报价有效期即拒绝）
            if now_ms > req.quote_valid_until_ms:
                raise PoolError(
                    ErrorCode.REQUEST_EXPIRED,
                    f"请求已过期：now={now_ms} > valid_until={req.quote_valid_until_ms}",
                )

            if pool.total_shares == 0 or pool.reserve_in == 0 or pool.reserve_out == 0:
                raise PoolError(ErrorCode.POOL_NOT_INITIALIZED, f"池子尚未注入流动性: {req.pool_id}")

            # 3) 恒定乘积做市：out = reserve_out * in_after_fee /
            #    (reserve_in + in_after_fee)，向下取整。
            in_after_fee = req.amount_in - req.amount_in * FEE_NUMERATOR // FEE_DENOMINATOR
            amount_out = (
                pool.reserve_out * in_after_fee // (pool.reserve_in + in_after_fee)
            )
            if amount_out <= 0:
                raise PoolError(
                    ErrorCode.INSUFFICIENT_LIQUIDITY,
                    f"流动性不足：投入 {req.amount_in} 在当前存量下应得 0",
                )

            # 4) 成交保护 A：实际成交不得优于/劣于报价口径 —— 必须等于报价。
            #    行情（存量）在报价后发生变化即判定报价失效，要求重新取价。
            if amount_out != req.quote_amount_out:
                raise PoolError(
                    ErrorCode.STALE_QUOTE,
                    f"报价已失效：报价 {req.quote_amount_out} != 按当前存量计算 {amount_out}",
                )

            # 5) 成交保护 B：最小成交量。
            if amount_out < req.min_amount_out:
                raise PoolError(
                    ErrorCode.SLIPPAGE,
                    f"成交 {amount_out} 低于最小成交量 {req.min_amount_out}",
                )

            before = self._snapshot(pool)
            why = (
                f"恒定乘积：in_after_fee={in_after_fee} "
                f"out={pool.reserve_out}*{in_after_fee}//"
                f"({pool.reserve_in}+{in_after_fee})={amount_out}"
            )

            pool.reserve_in += req.amount_in
            pool.reserve_out -= amount_out

            after = self._snapshot(pool)
            # 不变量自检：手续费使乘积严格不减；此处仅断言，不向用户承诺内部细节。
            k_before = before.reserve_in * before.reserve_out
            k_after = after.reserve_in * after.reserve_out
            assert k_after >= k_before, f"兑换破坏不变量: {k_before} -> {k_after}"

            result = OperationResult(
                request_id=req.request_id,
                status="ok",
                kind="swap",
                shares_delta=0,
                amount_in_consumed=req.amount_in,
                amount_out=amount_out,
                pool_before=before,
                pool_after=after,
                reason=why,
            )
            return self._record(req.request_id, fp, result)

    # ---- 读操作 ---------------------------------------------------------

    def get_state(self, pool_id: str) -> PoolState:
        with self._lock:
            return self._snapshot(self._get_pool(pool_id))

    def quote(self, pool_id: str, amount_in: int) -> int:
        """按当前存量试算兑换输出数量（纯计算，不改状态）。"""
        if amount_in <= 0:
            raise PoolError(ErrorCode.INVALID_PARAMETER, "兑换投入必须为正整数 (amount_in>0)")
        with self._lock:
            pool = self._get_pool(pool_id)
            if pool.total_shares == 0:
                raise PoolError(ErrorCode.POOL_NOT_INITIALIZED, f"池子尚未注入流动性: {pool_id}")
            in_after_fee = amount_in - amount_in * FEE_NUMERATOR // FEE_DENOMINATOR
            return pool.reserve_out * in_after_fee // (pool.reserve_in + in_after_fee)
