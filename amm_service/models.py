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

    # ---- 对账计数器（只增，全部可从流水重推） ----
    deposited_x: int = 0   # 流动性注入累计（本金，含 seed 初始库存）
    deposited_y: int = 0
    withdrawn_x: int = 0   # 赎回累计
    withdrawn_y: int = 0
    swap_in_x: int = 0     # 兑换累计输入（按代币方向）
    swap_in_y: int = 0
    swap_out_x: int = 0    # 兑换累计输出
    swap_out_y: int = 0
    fee_x: int = 0         # 兑换累计手续费（输入侧内扣，按代币方向）
    fee_y: int = 0
    swap_count_x_to_y: int = 0  # 成交笔数（按方向）
    swap_count_y_to_x: int = 0

    # ---- 容量与熔断策略（管理员可调；0 表示不限制） ----
    max_swap_in: int = 0      # 单笔兑换输入上限，0 = 不限
    reserve_buffer: int = 0   # 缓冲水位：兑换后输出侧库存不得低于该值
    swap_paused: bool = False
    add_paused: bool = False
    remove_paused: bool = False

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
            deposited_x=self.deposited_x,
            deposited_y=self.deposited_y,
            withdrawn_x=self.withdrawn_x,
            withdrawn_y=self.withdrawn_y,
            swap_in_x=self.swap_in_x,
            swap_in_y=self.swap_in_y,
            swap_out_x=self.swap_out_x,
            swap_out_y=self.swap_out_y,
            fee_x=self.fee_x,
            fee_y=self.fee_y,
            swap_count_x_to_y=self.swap_count_x_to_y,
            swap_count_y_to_x=self.swap_count_y_to_x,
            max_swap_in=self.max_swap_in,
            reserve_buffer=self.reserve_buffer,
            swap_paused=self.swap_paused,
            add_paused=self.add_paused,
            remove_paused=self.remove_paused,
        )


@dataclass
class Position:
    """单个用户在单个池子里的份额持仓与累计贡献。"""

    shares: int = 0
    contributed_x: int = 0  # 累计注入（本金贡献）
    contributed_y: int = 0
    withdrawn_x: int = 0    # 累计赎回
    withdrawn_y: int = 0


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
class LedgerRecord:
    """交易流水：每笔成功/失败操作（含管理操作）留一条可追溯记录。

    同一 (op, request_id) 只记一条；幂等重放不产生新记录。
    """

    seq: int            # 全局单调序号（写入顺序）
    ts_ms: int          # 处理时刻（服务时钟，毫秒）
    pool_id: str
    user: str
    op: str             # "seed" | "add" | "remove" | "swap" | "policy" | "pause" | "resume"
    request_id: str     # 业务幂等键；管理类操作为空串
    status: str         # "success" | "rejected"
    error_code: str = ""  # 失败原因码；成功为空串
    detail: dict = field(default_factory=dict)  # 金额/手续费/判定要素等
