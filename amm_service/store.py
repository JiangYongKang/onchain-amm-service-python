"""本地内存状态：池子、持仓与幂等记录，带每池并发互斥锁。

并发模型
--------
- 每个 pool_id 一把 asyncio.Lock：同一池子上的写请求在服务层串行提交，
  不同池子互不阻塞。
- 锁本身在全局 _guard 下惰性创建，仅在获取的瞬间短暂持锁。
- 幂等记录键为 (scope, request_id)，scope 区分 add/remove/swap 不同操作，
  避免不同接口复用同一 request_id 时互相串结果。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from .models import IdempotentResult, PoolState, Position


@dataclass
class Store:
    pools: dict[str, PoolState] = field(default_factory=dict)
    positions: dict[tuple[str, str], Position] = field(default_factory=dict)
    idempotency: dict[tuple[str, str], IdempotentResult] = field(default_factory=dict)
    pool_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _guard: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def pool_lock(self, pool_id: str) -> asyncio.Lock:
        """获取（或创建）指定池子的串行锁。"""
        async with self._guard:
            lock = self.pool_locks.get(pool_id)
            if lock is None:
                lock = asyncio.Lock()
                self.pool_locks[pool_id] = lock
            return lock

    def get_pool(self, pool_id: str) -> PoolState:
        pool = self.pools.get(pool_id)
        if pool is None:
            from .errors import PoolNotFound

            raise PoolNotFound(f"pool not found: {pool_id}")
        return pool

    def get_position(self, pool_id: str, user: str) -> Position:
        return self.positions.setdefault((pool_id, user), Position())

    def reset(self) -> None:
        self.pools.clear()
        self.positions.clear()
        self.idempotency.clear()
        self.pool_locks.clear()
