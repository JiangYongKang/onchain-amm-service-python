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
    AmountLimitExceeded,
    DuplicateRequestConflict,
    InsufficientLiquidity,
    InsufficientShareBalance,
    InvalidParameter,
    PoolAlreadyExists,
    PoolNotFound,
    PoolPaused,
    RequestExpired,
    ReserveBufferBreach,
    SlippageExceeded,
    StaleQuote,
)
from .models import IdempotentResult, LedgerRecord, PoolState
from . import pricing
from . import reconcile
from .store import Store

logger = logging.getLogger("amm.service")

VALID_SIDES = ("x_to_y", "y_to_x")
VALID_OPS = ("seed", "add", "remove", "swap", "policy", "pause", "resume")
_SYSTEM_USER = "_system"  # seed 出来的初始库存对应的份额持有人
_ADMIN_USER = "_admin"    # 管理类操作（seed/policy/pause/resume）流水上的操作人

_REPLAYABLE: dict[str, type[AmmError]] = {
    "POOL_NOT_FOUND": PoolNotFound,
    "INVALID_PARAMETER": InvalidParameter,
    "REQUEST_EXPIRED": RequestExpired,
    "STALE_QUOTE": StaleQuote,
    "INSUFFICIENT_LIQUIDITY": InsufficientLiquidity,
    "SLIPPAGE_EXCEEDED": SlippageExceeded,
    "INSUFFICIENT_SHARE_BALANCE": InsufficientShareBalance,
    "POOL_PAUSED": PoolPaused,
    "AMOUNT_LIMIT_EXCEEDED": AmountLimitExceeded,
    "RESERVE_BUFFER_BREACH": ReserveBufferBreach,
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

        重复初始化防护：池子已存在时——
        - 代币对与手续费率相同：视为幂等重试，原样返回当前状态
          （带 "already_exists": true），不改动存量与份额；
        - 配置不同：以 POOL_ALREADY_EXISTS 明确拒绝并记入流水。
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
            existing = self.store.pools.get(pool_id)
            if existing is not None:
                if (existing.token_x, existing.token_y, existing.fee_ppm) == (
                    token_x, token_y, fee_ppm
                ):
                    view = self._pool_view(existing)
                    view["already_exists"] = True
                    _log_event("seed_pool", decision="replayed", pool_id=pool_id,
                               reason="pool already initialized with same config; "
                                      "state returned unchanged")
                    return view
                exc = PoolAlreadyExists(
                    f"pool {pool_id} already exists with different config: "
                    f"({existing.token_x},{existing.token_y},fee={existing.fee_ppm})"
                )
                self._record(pool_id, _ADMIN_USER, "seed", "", "rejected",
                             exc.code, {"token_x": token_x, "token_y": token_y,
                                        "fee_ppm": fee_ppm})
                _log_event("seed_pool", decision="rejected", pool_id=pool_id,
                           error=exc.code)
                raise exc

            pool = PoolState(
                pool_id=pool_id, token_x=token_x, token_y=token_y,
                reserve_x=reserve_x, reserve_y=reserve_y, fee_ppm=fee_ppm,
            )
            if reserve_x > 0:
                shares = pricing.shares_for_first_deposit(reserve_x, reserve_y)
                pool.total_shares = shares
                pos = self.store.get_position(pool_id, _SYSTEM_USER)
                pos.shares += shares
                pos.contributed_x += reserve_x
                pos.contributed_y += reserve_y
            # seed 初始库存属于「存进来的本金」。
            pool.deposited_x = reserve_x
            pool.deposited_y = reserve_y
            self.store.pools[pool_id] = pool
            self._record(pool_id, _ADMIN_USER, "seed", "", "success", "",
                         {"reserve_x": reserve_x, "reserve_y": reserve_y,
                          "fee_ppm": fee_ppm, "total_shares": pool.total_shares})
            _log_event("seed_pool", pool_id=pool_id, after=self._pool_view(pool))
            return self._pool_view(pool)

    async def get_pool(self, pool_id: str) -> dict:
        return self._pool_view(self.store.get_pool(pool_id))

    async def set_pool_policy(self, pool_id: str, *,
                              max_swap_in: int | None = None,
                              reserve_buffer: int | None = None) -> dict:
        """设置容量策略：单笔兑换输入上限 / 输出侧缓冲水位（None 表示不变）。

        只影响策略生效后新到达的请求；已接受（已落账）的请求不受影响，
        幂等重放仍返回首次结果。策略切换本身不改动任何存量与份额。
        """
        for name, v in (("max_swap_in", max_swap_in),
                        ("reserve_buffer", reserve_buffer)):
            if v is not None and v < 0:
                raise InvalidParameter(f"{name} must be >= 0")
        lock = await self.store.pool_lock(pool_id)
        async with lock:
            pool = self.store.get_pool(pool_id)
            before = {"max_swap_in": pool.max_swap_in,
                      "reserve_buffer": pool.reserve_buffer}
            if max_swap_in is not None:
                pool.max_swap_in = max_swap_in
            if reserve_buffer is not None:
                pool.reserve_buffer = reserve_buffer
            after = {"max_swap_in": pool.max_swap_in,
                     "reserve_buffer": pool.reserve_buffer}
            self._record(pool_id, _ADMIN_USER, "policy", "", "success", "",
                         {"before": before, "after": after})
            _log_event("set_pool_policy", pool_id=pool_id,
                       before=before, after=after)
            return self._pool_view(pool)

    async def set_pause(self, pool_id: str, op: str, paused: bool) -> dict:
        """暂停/恢复某类操作：op ∈ {"swap", "add", "remove"}。

        暂停只影响之后的请求判定；不改变存量、份额与既有幂等记录，
        恢复后第一笔交易从当前存量继续。
        """
        if op not in ("swap", "add", "remove"):
            raise InvalidParameter("op must be one of ('swap', 'add', 'remove')")
        lock = await self.store.pool_lock(pool_id)
        async with lock:
            pool = self.store.get_pool(pool_id)
            setattr(pool, f"{op}_paused", paused)
            self._record(pool_id, _ADMIN_USER,
                         "pause" if paused else "resume", "", "success", "",
                         {"op": op, "paused": paused})
            _log_event("set_pause", pool_id=pool_id, op=op, paused=paused)
            return self._pool_view(pool)

    async def get_position(self, pool_id: str, user: str) -> dict:
        self.store.get_pool(pool_id)
        pos = self.store.get_position(pool_id, user)
        return {"pool_id": pool_id, "user": user, "shares": pos.shares}

    async def get_pool_stats(self, pool_id: str) -> dict:
        """池子维度对账视图：本金 / 手续费 / 成交笔数与成交量。

        全部计数器只增且可从流水重推；恒等式（测试强制断言）：
            reserve == deposited - withdrawn + swap_in - swap_out   （按侧）
        """
        pool = self.store.get_pool(pool_id)
        return {
            "pool_id": pool_id,
            # 手续费（输入侧内扣，按代币方向累计）
            "fee_x": pool.fee_x,
            "fee_y": pool.fee_y,
            # 成交笔数与成交量（按方向）
            "swap_count_x_to_y": pool.swap_count_x_to_y,
            "swap_count_y_to_x": pool.swap_count_y_to_x,
            "swap_count": pool.swap_count_x_to_y + pool.swap_count_y_to_x,
            "swap_in_x": pool.swap_in_x,
            "swap_in_y": pool.swap_in_y,
            "swap_out_x": pool.swap_out_x,
            "swap_out_y": pool.swap_out_y,
            # 本金：用户（含 seed 系统账户）存进来与赎回去的累计
            "deposited_x": pool.deposited_x,
            "deposited_y": pool.deposited_y,
            "withdrawn_x": pool.withdrawn_x,
            "withdrawn_y": pool.withdrawn_y,
            "principal_x": pool.deposited_x - pool.withdrawn_x,
            "principal_y": pool.deposited_y - pool.withdrawn_y,
        }

    async def get_user_stats(self, pool_id: str, user: str) -> dict:
        """用户维度：份额占比、应得手续费分成（向下取整）与累计贡献。

        应得分成 = floor(用户份额 * 池子累计手续费 / 总份额)，按当前持仓
        快照计算；舍入余量留在池内未分配，不会凭空多出资产。
        """
        pool = self.store.get_pool(pool_id)
        pos = self.store.get_position(pool_id, user)
        total = pool.total_shares
        fee_share_x = pos.shares * pool.fee_x // total if total else 0
        fee_share_y = pos.shares * pool.fee_y // total if total else 0
        share_ppm = pos.shares * 1_000_000 // total if total else 0
        return {
            "pool_id": pool_id,
            "user": user,
            "shares": pos.shares,
            "total_shares": total,
            "share_ppm": share_ppm,  # 份额占比，百万分比
            "fee_share_x": fee_share_x,
            "fee_share_y": fee_share_y,
            "contributed_x": pos.contributed_x,
            "contributed_y": pos.contributed_y,
            "withdrawn_x": pos.withdrawn_x,
            "withdrawn_y": pos.withdrawn_y,
        }

    MAX_PAGE_SIZE = 500

    async def get_history(self, *, pool_id: str | None = None,
                          user: str | None = None, op: str | None = None,
                          status: str | None = None,
                          since_ms: int | None = None,
                          until_ms: int | None = None,
                          page: int = 1, page_size: int = 50) -> dict:
        """分页查询交易流水（成功与失败都可查）。

        过滤条件：池子 / 用户 / 操作类型 / 状态 / 时间范围（闭区间）。
        返回的 total 恒等于过滤后的记录总数；分页参数非法直接拒绝。
        """
        if page < 1:
            raise InvalidParameter("page must be >= 1")
        if not (1 <= page_size <= self.MAX_PAGE_SIZE):
            raise InvalidParameter(
                f"page_size must be within [1, {self.MAX_PAGE_SIZE}]"
            )
        if op is not None and op not in VALID_OPS:
            raise InvalidParameter(f"op must be one of {VALID_OPS}")
        if status is not None and status not in ("success", "rejected"):
            raise InvalidParameter("status must be 'success' or 'rejected'")
        for name, v in (("since_ms", since_ms), ("until_ms", until_ms)):
            if v is not None and v < 0:
                raise InvalidParameter(f"{name} must be >= 0")
        if since_ms is not None and until_ms is not None and since_ms > until_ms:
            raise InvalidParameter("since_ms must be <= until_ms")

        items = [
            r for r in self.store.ledger
            if (pool_id is None or r.pool_id == pool_id)
            and (user is None or r.user == user)
            and (op is None or r.op == op)
            and (status is None or r.status == status)
            and (since_ms is None or r.ts_ms >= since_ms)
            and (until_ms is None or r.ts_ms <= until_ms)
        ]
        total = len(items)
        start = (page - 1) * page_size
        page_items = items[start:start + page_size]
        result = {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [self._record_view(r) for r in page_items],
        }
        _log_event(
            "get_history", filters={"pool_id": pool_id, "user": user, "op": op,
                                    "status": status, "since_ms": since_ms,
                                    "until_ms": until_ms},
            page=page, page_size=page_size, total=total,
            returned=len(page_items),
        )
        return result

    @staticmethod
    def _record_view(r: LedgerRecord) -> dict:
        return {
            "seq": r.seq,
            "ts_ms": r.ts_ms,
            "pool_id": r.pool_id,
            "user": r.user,
            "op": r.op,
            "request_id": r.request_id,
            "status": r.status,
            "error_code": r.error_code,
            "detail": r.detail,
        }

    async def replay_from_history(self, pool_id: str | None = None
                                  ) -> reconcile.LedgerReplay:
        """仅凭 GET /history 的分页流水重算池子/用户全部累计口径。

        对账入口：逐页拉全（含成功与被拒记录，重算时后者自动跳过），
        按 seq fold。pool_id 给定时只取该池流水（跨池 request_id 不冲突，
        因为幂等键带 scope、流水按池过滤即可）。
        """
        page_size = self.MAX_PAGE_SIZE
        page = 1
        items: list[dict] = []
        while True:
            hist = await self.get_history(
                pool_id=pool_id, page=page, page_size=page_size,
            )
            items.extend(hist["items"])
            if page * page_size >= hist["total"]:
                break
            page += 1
        return reconcile.replay_ledger(items)

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

    def reset(self) -> None:
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
            # 熔断：注入被暂停时明确拒绝，不留部分状态。
            if pool.add_paused:
                raise PoolPaused(f"liquidity adds are paused for pool {pool_id}")
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
            pool.deposited_x += amount_x
            pool.deposited_y += amount_y
            pool.version += 1
            pos = self.store.get_position(pool_id, user)
            pos.shares += shares
            pos.contributed_x += amount_x
            pos.contributed_y += amount_y
            result = {"request_id": request_id, "shares": shares,
                      # 追加（向后兼容）：两侧实际存入金额，使流水足以重算
                      # 池子本金与用户累计贡献；既有字段含义不变。
                      "amount_x": amount_x, "amount_y": amount_y,
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

        return await self._with_idempotency("add", request_id, pool_id, user, fp, action)

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
            # 熔断：赎回被暂停时明确拒绝，不留部分状态。
            if pool.remove_paused:
                raise PoolPaused(
                    f"liquidity removes are paused for pool {pool_id}"
                )
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
            pool.withdrawn_x += out_x
            pool.withdrawn_y += out_y
            pool.version += 1
            pos.shares -= shares
            pos.withdrawn_x += out_x
            pos.withdrawn_y += out_y
            result = {
                "request_id": request_id,
                "amount_x": out_x, "amount_y": out_y,
                # 追加（向后兼容）：实际烧掉的份额，使流水足以重算
                # 用户持仓、累计赎回与池子总份额；既有字段含义不变。
                "shares_burned": shares,
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

        return await self._with_idempotency("remove", request_id, pool_id, user, fp, action)

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
            # 熔断：兑换被暂停时明确拒绝，不留部分状态。
            if pool.swap_paused:
                raise PoolPaused(f"swaps are paused for pool {pool_id}")
            self._check_deadline(deadline_ms)
            # 容量：单笔输入上限（0 表示不限）。
            if pool.max_swap_in and amount_in > pool.max_swap_in:
                raise AmountLimitExceeded(
                    f"amount_in {amount_in} exceeds per-swap limit "
                    f"{pool.max_swap_in}"
                )
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
            # 缓冲水位：成交后输出侧库存不得低于设定水位（0 表示不限）。
            if pool.reserve_buffer and reserve_out - out < pool.reserve_buffer:
                raise ReserveBufferBreach(
                    f"swap would leave reserve_out={reserve_out - out} below "
                    f"buffer {pool.reserve_buffer}"
                )
            # 最小成交保护。
            if out < min_out:
                raise SlippageExceeded(
                    f"amount_out {out} below min_out {min_out}"
                )

            # 手续费口径：effective_in = floor(amount_in * (1e6 - f) / 1e6)，
            # fee = amount_in - effective_in，全部留在输入侧库存中。
            fee = amount_in - pricing.effective_input(amount_in, pool.fee_ppm)
            if side == "x_to_y":
                pool.reserve_x += amount_in
                pool.reserve_y -= out
                pool.swap_in_x += amount_in
                pool.swap_out_y += out
                pool.fee_x += fee
                pool.swap_count_x_to_y += 1
            else:
                pool.reserve_y += amount_in
                pool.reserve_x -= out
                pool.swap_in_y += amount_in
                pool.swap_out_x += out
                pool.fee_y += fee
                pool.swap_count_y_to_x += 1
            pool.version += 1
            result = {
                "request_id": request_id,
                "side": side,
                "amount_in": amount_in,
                "amount_out": out,
                "fee_in": fee,
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

        return await self._with_idempotency("swap", request_id, pool_id, user, fp, action)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    async def _with_idempotency(
        self,
        scope: str,
        request_id: str,
        pool_id: str,
        user: str,
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
                    # 同 id 不同参：一笔独立（但非法）的请求，留痕可查。
                    exc = DuplicateRequestConflict(
                        f"request_id {request_id!r} already processed with "
                        f"different parameters"
                    )
                    self._record(pool_id, user, scope, request_id, "rejected",
                                 exc.code, {"reason": "fingerprint mismatch"})
                    _log_event(
                        "idempotency_conflict", decision="rejected",
                        request_id=request_id, scope=scope,
                        reason="same request_id reused with different parameters",
                    )
                    raise exc
                _log_event(
                    "idempotency_replay", decision="replayed",
                    request_id=request_id, scope=scope,
                    status=existing.status,
                )
                # 幂等重放：不产生第二条流水。
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
                self._record(pool_id, user, scope, request_id, "rejected",
                             "POOL_NOT_FOUND", {})
                raise PoolNotFound(f"pool not found: {pool_id}")

            try:
                result = await action(pool)
            except AmmError as exc:
                self.store.idempotency[key] = self._rejection(
                    exc.code, fingerprint, pool_id, str(exc),
                )
                self._record(pool_id, user, scope, request_id, "rejected",
                             exc.code, {"message": str(exc)})
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
            self._record(
                pool_id, user, scope, request_id, "success",
                "", {k: v for k, v in result.items()
                     if k not in ("pool", "replayed", "request_id")},
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

    def _record(self, pool_id: str, user: str, op: str, request_id: str,
                status: str, error_code: str, detail: dict) -> None:
        """追加一条流水（调用方须持有对应池子锁）。"""
        self.store.append_ledger(LedgerRecord(
            seq=0, ts_ms=self.now_ms(), pool_id=pool_id, user=user, op=op,
            request_id=request_id, status=status, error_code=error_code,
            detail=detail,
        ))

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
            "max_swap_in": pool.max_swap_in,
            "reserve_buffer": pool.reserve_buffer,
            "swap_paused": pool.swap_paused,
            "add_paused": pool.add_paused,
            "remove_paused": pool.remove_paused,
        }
