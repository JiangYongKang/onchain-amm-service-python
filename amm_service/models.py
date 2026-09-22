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
