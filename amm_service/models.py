"""领域模型：池子状态、份额持仓与行情报价（整数量，最小单位记账）。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PoolState:
    pool_id: str
    token_x: str
    token_y: str
    reserve_x: int = 0
    reserve_y: int = 0
    total_shares: int = 0
    fee_ppm: int = 3000  # 手续费，百万分比，3000 = 0.30%
    version: int = 0    # 乐观版本号，用于并发检测/可观测

    def clone(self) -> "PoolState":
        return PoolState(
            pool_id=self.pool_id,
            token_x=self.token_x,
            token_y=self.token_y,
            reserve_x=self.reserve_x,
            reserve_y=self.reserve_y,
            total_shares=self.total_shares,
            fee_ppm=self.fee_ppm,
            version=self.version,
        )


@dataclass
class Position:
    """单个用户在单个池子里的份额持仓。"""

    shares: int = 0


@dataclass
class Quote:
    """一次确定性报价：给定输入与池子版本，算出期望输出。"""

    pool_id: str
    side: str  # "x_to_y" | "y_to_x"
    amount_in: int
    amount_out: int
    min_out: int
    deadline_ms: int
    pool_version: int


@dataclass
class IdempotentResult:
    """幂等记录：同一 request_id 的重复请求直接回放该结果。"""

    status: str  # "success" | "rejected:<code>"
    payload: dict = field(default_factory=dict)
    fingerprint: str = ""  # 请求要素摘要，用于识别同 id 不同参的冲突重试
    pool_id: str = ""


@dataclass
class PoolPolicy:
    """池子运营策略：容量上限、缓冲水位与熔断开关。

    - max_swap_in：单笔兑换输入上限（输入侧最小单位），0 表示不限。
    - reserve_buffer：输出侧库存缓冲水位，成交后 reserve_out 不得低于该值，
      0 表示不启用。
    - paused_ops：被熔断的操作集合，取值 "swap" | "add" | "remove"。
    """

    max_swap_in: int = 0
    reserve_buffer: int = 0
    paused_ops: frozenset[str] = frozenset()

    def is_paused(self, op: str) -> bool:
        return op in self.paused_ops


@dataclass
class LedgerEntry:
    """交易流水：每笔被受理的请求（成功或被拒）恰好记录一条。

    seq 为全局单调序号；同一 (op, request_id) 只会产生一条记录，
    幂等回放不新增流水。
    """

    seq: int
    ts_ms: int
    pool_id: str
    user: str
    op: str  # "add" | "remove" | "swap"
    request_id: str
    status: str  # "success" | "rejected"
    error_code: str = ""  # 拒绝时的稳定错误码，成功时为空
    detail: dict = field(default_factory=dict)  # 业务要素：金额、份额、方向等
