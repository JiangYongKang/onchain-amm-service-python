"""应用服务层：注入/赎回流动性、带保护兑换、幂等与并发编排。

关键保证
--------
1. 并发：同一 pool_id 的写操作在同一把 asyncio.Lock 下串行提交；
   状态在原对象上完成全部校验后才落账并 version += 1，
   任一校验抛出异常时本方法尚未改动任何余额/份额，失败不留部分状态。
2. 幂等：(scope, request_id) 命中已处理记录时原样回放成功结果或失败原因；
   同 id 但业务要素不同（指纹不一致）按 DUPLICATE_REQUEST_CONFLICT 拒绝。
3. 失败原因可区分：PoolNotFound / InvalidParameter / RequestExpired /
   StaleQuote / InsufficientLiquidity / SlippageExceeded /
   InsufficientShareBalance / DuplicateRequestConflict。
4. 兑换保护校验顺序：参数 -> 过期(deadline) -> 报价陈旧(quoted_out/版本) ->
   流动性不足(输出为 0) -> 最小成交(min_out)，各自独立错误码。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable

from .errors import (
    AmmError,
    DuplicateRequestConflict,
    InsufficientLiquidity,
    InsufficientShareBalance,
    InvalidParameter,
    PoolNotFound,
    RequestExpired,
    SlippageExceeded,
    StaleQuote,
)
from .models import IdempotentResult, PoolState
from . import pricing
from .store import Store

logger = logging.getLogger("amm.service")

VALID_SIDES = ("x_to_y", "y_to_x")
_SYSTEM_USER = "_system"  # seed 出来的初始库存对应的份额持有人

_REPLAYABLE: dict[str, type[AmmError]] = {
    "POOL_NOT_FOUND": PoolNotFound,
    "INVALID_PARAMETER": InvalidParameter,
    "REQUEST_EXPIRED": RequestExpired,
    "STALE_QUOTE": StaleQuote,
    "INSUFFICIENT_LIQUIDITY": InsufficientLiquidity,
    "SLIPPAGE_EXCEEDED": SlippageExceeded,
    "INSUFFICIENT_SHARE_BALANCE": InsufficientShareBalance,
}


def _fingerprint(parts: object) -> str:
    raw = json.dumps(parts, separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _log_event(event: str, **fields: object) -> None:
    """结构化判定日志：单测中可看到请求输入、池子前后状态与判定依据。"""
    logger.info("%s %s", event, json.dumps(fields, default=str, sort_keys=True))


class AmmService:
    def __init__(self, store: Store | None = None,
                 clock: Callable[[], int] | None = None) -> None:
        self.store = store or Store()
        # 时间源可注入，毫秒时间戳；默认取墙上时钟，便于复现过期场景。
        self._clock = clock or (lambda: int(time.time() * 1000))

    def now_ms(self) -> int:
        return self._clock()

    # ------------------------------------------------------------------ #
    # 管理 / 查询
    # ------------------------------------------------------------------ #
    async def seed_pool(self, pool_id: str, token_x: str, token_y: str,
                        reserve_x: int, reserve_y: int, fee_ppm: int = 3000) -> dict:
        """以本地库存播种池子（模拟初始存量）。

        两侧均为正时，按 min(x,y) 给系统账户铸造份额，池子立即可供兑换；
        两侧为 0 时池子未激活，等待首笔流动性注入；不允许只给单侧库存。
        """
        if not pool_id or not token_x or not token_y:
            raise InvalidParameter("pool_id/token_x/token_y must be non-empty")
        if token_x == token_y:
            raise InvalidParameter("token_x and token_y must differ")
        if (reserve_x > 0) != (reserve_y > 0):
            raise InvalidParameter("seed reserves must be both positive or both zero")
        if not (0 <= fee_ppm < 1_000_000):
            raise InvalidParameter("fee_ppm must be within [0, 1_000_000)")

        lock = await self.store.pool_lock(pool_id)
        async with lock:
            pool = PoolState(
                pool_id=pool_id, token_x=token_x, token_y=token_y,
                reserve_x=reserve_x, reserve_y=reserve_y, fee_ppm=fee_ppm,
            )
            if reserve_x > 0:
                shares = pricing.shares_for_first_deposit(reserve_x, reserve_y)
                pool.total_shares = shares
                self.store.get_position(pool_id, _SYSTEM_USER).shares += shares
            self.store.pools[pool_id] = pool
            _log_event("seed_pool", pool_id=pool_id, after=self._pool_view(pool))
            return self._pool_view(pool)

    async def get_pool(self, pool_id: str) -> dict:
        return self._pool_view(self.store.get_pool(pool_id))

    async def get_position(self, pool_id: str, user: str) -> dict:
        self.store.get_pool(pool_id)
        pos = self.store.get_position(pool_id, user)
        return {"pool_id": pool_id, "user": user, "shares": pos.shares}

    async def quote(self, *, pool_id: str, side: str, amount_in: int) -> dict:
        if side not in VALID_SIDES:
            raise InvalidParameter(f"side must be one of {VALID_SIDES}")
        if amount_in <= 0:
            raise InvalidParameter("amount_in must be positive")
        pool = self.store.get_pool(pool_id)
        lock = await self.store.pool_lock(pool_id)
        async with lock:
            out = self._quote_amount(pool, side, amount_in)
            return {
                "pool_id": pool_id,
                "side": side,
                "amount_in": amount_in,
                "amount_out": out,
                "pool_version": pool.version,
                "reserve_in": pool.reserve_x if side == "x_to_y" else pool.reserve_y,
                "reserve_out": pool.reserve_y if side == "x_to_y" else pool.reserve_x,
                "fee_ppm": pool.fee_ppm,
            }

    async def reset(self) -> None:
        self.store.reset()

    # ------------------------------------------------------------------ #
    # 流动性
    # ------------------------------------------------------------------ #
    async def add_liquidity(self, *, request_id: str, pool_id: str, user: str,
                            amount_x: int, amount_y: int,
                            min_shares: int = 0, deadline_ms: int = 0) -> dict:
        fp = _fingerprint(
            ["add", pool_id, user, amount_x, amount_y, min_shares, deadline_ms]
        )

        async def action(pool: PoolState) -> dict:
            if not user:
                raise InvalidParameter("user must be non-empty")
            if amount_x <= 0 or amount_y <= 0:
                raise InvalidParameter("amount_x and amount_y must be positive")
            self._check_deadline(deadline_ms)
            before = pool.clone()

            if pool.total_shares == 0:
                if amount_x != amount_y:
                    raise InvalidParameter(
                        "first deposit requires amount_x == amount_y "
                        "(shares are minted as min(x,y))"
                    )
                shares = pricing.shares_for_first_deposit(amount_x, amount_y)
            else:
                shares = pricing.shares_for_deposit(
                    amount_x, amount_y, pool.reserve_x, pool.reserve_y,
                    pool.total_shares,
                )
            if shares <= 0:
                raise InvalidParameter("computed shares is zero; deposit too small")
            if shares < min_shares:
                raise SlippageExceeded(
                    f"minted shares {shares} below min_shares {min_shares}"
                )

            # 全部校验通过后一次性落账。
            pool.reserve_x += amount_x
            pool.reserve_y += amount_y
            pool.total_shares += shares
            pool.version += 1
            self.store.get_position(pool_id, user).shares += shares
            result = {"request_id": request_id, "shares": shares,
                      "replayed": False, "pool": self._pool_view(pool)}
            _log_event(
                "add_liquidity", decision="accepted", request_id=request_id,
                user=user,
                input={"amount_x": amount_x, "amount_y": amount_y,
                       "min_shares": min_shares, "deadline_ms": deadline_ms},
                before=self._pool_view(before), after=self._pool_view(pool),
                reason="shares=min(floor(dx*S/Rx),floor(dy*S/Ry))",
            )
            return result

        return await self._with_idempotency("add", request_id, pool_id, fp, action)

    async def remove_liquidity(self, *, request_id: str, pool_id: str, user: str,
                               shares: int, min_x: int = 0, min_y: int = 0,
                               deadline_ms: int = 0) -> dict:
        fp = _fingerprint(
            ["remove", pool_id, user, shares, min_x, min_y, deadline_ms]
        )

        async def action(pool: PoolState) -> dict:
            if not user:
                raise InvalidParameter("user must be non-empty")
            if shares <= 0:
                raise InvalidParameter("shares must be positive")
            self._check_deadline(deadline_ms)
            before = pool.clone()

            pos = self.store.get_position(pool_id, user)
            if pos.shares < shares:
                raise InsufficientShareBalance(
                    f"requested {shares} shares but holds {pos.shares}"
                )
            if shares > pool.total_shares:
                # 不变量防线：单个持仓不应超过总份额。
                raise InsufficientShareBalance("shares exceed pool total")

            out_x, out_y = pricing.amounts_for_withdrawal(
                shares, pool.reserve_x, pool.reserve_y, pool.total_shares
            )
            if out_x < min_x or out_y < min_y:
                raise SlippageExceeded(
                    f"withdrawal (x={out_x}, y={out_y}) below minimum "
                    f"(min_x={min_x}, min_y={min_y})"
                )
            # 全份额赎回时，若历史 dust 会导致「总份额 0 但库存非 0」，
            # 池子将无法再按比例记账，提前拒绝该边界请求。
            if shares == pool.total_shares and (
                pool.reserve_x - out_x > 0 or pool.reserve_y - out_y > 0
            ):
                raise InvalidParameter(
                    "full withdrawal would leave a non-empty dust pool; "
                    "withdraw fewer shares"
                )

            pool.reserve_x -= out_x
            pool.reserve_y -= out_y
            pool.total_shares -= shares
            pool.version += 1
            pos.shares -= shares
            result = {
                "request_id": request_id,
                "amount_x": out_x, "amount_y": out_y,
                "remaining_shares": pos.shares,
                "replayed": False, "pool": self._pool_view(pool),
            }
            _log_event(
                "remove_liquidity", decision="accepted", request_id=request_id,
                user=user,
                input={"shares": shares, "min_x": min_x, "min_y": min_y,
                       "deadline_ms": deadline_ms},
                before=self._pool_view(before), after=self._pool_view(pool),
                reason="out=floor(shares*R/S); shares burned exactly",
            )
            return result

        return await self._with_idempotency("remove", request_id, pool_id, fp, action)

    # ------------------------------------------------------------------ #
    # 兑换
    # ------------------------------------------------------------------ #
    async def swap(self, *, request_id: str, pool_id: str, user: str, side: str,
                   amount_in: int, min_out: int, quoted_out: int | None = None,
                   quote_version: int | None = None,
                   deadline_ms: int = 0) -> dict:
        fp = _fingerprint([
            "swap", pool_id, user, side, amount_in, min_out,
            quoted_out, quote_version, deadline_ms,
        ])

        async def action(pool: PoolState) -> dict:
            if not user:
                raise InvalidParameter("user must be non-empty")
            if side not in VALID_SIDES:
                raise InvalidParameter(f"side must be one of {VALID_SIDES}")
            if amount_in <= 0:
                raise InvalidParameter("amount_in must be positive")
            self._check_deadline(deadline_ms)
            before = pool.clone()

            reserve_in = pool.reserve_x if side == "x_to_y" else pool.reserve_y
            reserve_out = pool.reserve_y if side == "x_to_y" else pool.reserve_x
            out = pricing.amount_out(amount_in, reserve_in, reserve_out, pool.fee_ppm)

            # 报价保护：携带报价时，池子版本必须未变且输出必须与报价一致，
            # 否则说明行情已在报价后发生变化。
            if quoted_out is not None:
                version_changed = (
                    quote_version is not None and quote_version != pool.version
                )
                if version_changed or out != quoted_out:
                    raise StaleQuote(
                        f"quote stale: quoted_out={quoted_out} version="
                        f"{quote_version}, actual_out={out} "
                        f"version={pool.version}"
                    )
            # 流动性不足：输出侧库存为空或本次输出为 0。
            if reserve_out <= 0 or out <= 0:
                raise InsufficientLiquidity(
                    f"no liquidity to fill swap: reserve_out={reserve_out}, "
                    f"amount_out={out}"
                )
            # 最小成交保护。
            if out < min_out:
                raise SlippageExceeded(
                    f"amount_out {out} below min_out {min_out}"
                )

            if side == "x_to_y":
                pool.reserve_x += amount_in
                pool.reserve_y -= out
            else:
                pool.reserve_y += amount_in
                pool.reserve_x -= out
            pool.version += 1
            result = {
                "request_id": request_id,
                "side": side,
                "amount_in": amount_in,
                "amount_out": out,
                "replayed": False,
                "pool": self._pool_view(pool),
            }
            _log_event(
                "swap", decision="accepted", request_id=request_id, user=user,
                input={"side": side, "amount_in": amount_in, "min_out": min_out,
                       "quoted_out": quoted_out, "quote_version": quote_version,
                       "deadline_ms": deadline_ms},
                before=self._pool_view(before), after=self._pool_view(pool),
                reason=(f"out=floor(Rout*ain*(1e6-f)/(Rin*1e6+ain*(1e6-f)))"
                        f"={out}; k_after>=k_before"),
            )
            return result

        return await self._with_idempotency("swap", request_id, pool_id, fp, action)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    async def _with_idempotency(
        self,
        scope: str,
        request_id: str,
        pool_id: str,
        fingerprint: str,
        action: Callable[[PoolState], Awaitable[dict]],
    ) -> dict:
        if not request_id:
            raise InvalidParameter("request_id must be non-empty")
        key = (scope, request_id)
        lock = await self.store.pool_lock(pool_id)
        async with lock:
            existing = self.store.idempotency.get(key)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    _log_event(
                        "idempotency_conflict", decision="rejected",
                        request_id=request_id, scope=scope,
                        reason="same request_id reused with different parameters",
                    )
                    raise DuplicateRequestConflict(
                        f"request_id {request_id!r} already processed with "
                        f"different parameters"
                    )
                _log_event(
                    "idempotency_replay", decision="replayed",
                    request_id=request_id, scope=scope,
                    status=existing.status,
                )
                if existing.status == "success":
                    payload = dict(existing.payload)
                    payload["replayed"] = True
                    return payload
                exc_cls = _REPLAYABLE[existing.status.removeprefix("rejected:")]
                raise exc_cls(existing.payload.get("message", ""))

            # PoolNotFound 也必须幂等：先记录再回放。
            pool = self.store.pools.get(pool_id)
            if pool is None:
                self.store.idempotency[key] = self._rejection(
                    "POOL_NOT_FOUND", fingerprint, pool_id,
                    f"pool not found: {pool_id}",
                )
                raise PoolNotFound(f"pool not found: {pool_id}")

            try:
                result = await action(pool)
            except AmmError as exc:
                self.store.idempotency[key] = self._rejection(
                    exc.code, fingerprint, pool_id, str(exc),
                )
                _log_event(
                    f"{scope}_rejected", decision="rejected",
                    request_id=request_id, pool_id=pool_id,
                    error=exc.code, message=str(exc),
                )
                raise
            self.store.idempotency[key] = IdempotentResult(
                status="success", payload=dict(result),
                fingerprint=fingerprint, pool_id=pool_id,
            )
            return result

    def _rejection(self, code: str, fingerprint: str, pool_id: str,
                   message: str) -> IdempotentResult:
        return IdempotentResult(
            status=f"rejected:{code}",
            payload={"message": message},
            fingerprint=fingerprint,
            pool_id=pool_id,
        )

    def _check_deadline(self, deadline_ms: int) -> None:
        if deadline_ms and self.now_ms() > deadline_ms:
            raise RequestExpired(
                f"request expired at {deadline_ms}ms, now={self.now_ms()}ms"
            )

    def _quote_amount(self, pool: PoolState, side: str, amount_in: int) -> int:
        if side == "x_to_y":
            return pricing.amount_out(
                amount_in, pool.reserve_x, pool.reserve_y, pool.fee_ppm
            )
        return pricing.amount_out(
            amount_in, pool.reserve_y, pool.reserve_x, pool.fee_ppm
        )

    def _pool_view(self, pool: PoolState) -> dict:
        return {
            "pool_id": pool.pool_id,
            "token_x": pool.token_x,
            "token_y": pool.token_y,
            "reserve_x": pool.reserve_x,
            "reserve_y": pool.reserve_y,
            "total_shares": pool.total_shares,
            "fee_ppm": pool.fee_ppm,
            "version": pool.version,
        }
