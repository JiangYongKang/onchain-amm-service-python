"""仅凭历史流水的对账重算（纯函数，无外部状态）。

所有统计接口报出的口径都必须等于把成功流水按 seq 顺序逐笔 fold 的结果；
被拒绝的记录不参与任何累计。整数金额求和，舍入方向与接口完全一致
（分成/占比均为向下取整），不引入浮点。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ReplayedPosition:
    """从流水重算出的单个用户累计口径。"""

    shares: int = 0           # 当前持仓（注入铸出 − 赎回烧掉）
    contributed_x: int = 0    # 累计注入
    contributed_y: int = 0
    withdrawn_x: int = 0      # 累计赎回取出
    withdrawn_y: int = 0


@dataclass
class LedgerReplay:
    """按 seq 顺序 fold 全部成功流水后的快照。"""

    total_shares: int = 0
    deposited_x: int = 0
    deposited_y: int = 0
    withdrawn_x: int = 0
    withdrawn_y: int = 0
    swap_in_x: int = 0
    swap_in_y: int = 0
    swap_out_x: int = 0
    swap_out_y: int = 0
    fee_x: int = 0
    fee_y: int = 0
    swap_count_x_to_y: int = 0
    swap_count_y_to_x: int = 0
    positions: dict[str, ReplayedPosition] = field(default_factory=dict)

    def position(self, user: str) -> ReplayedPosition:
        return self.positions.get(user, ReplayedPosition())

    def pool_stats_view(self) -> dict:
        """与 GET /pools/{id}/stats 逐字段同名的重算结果。"""
        return {
            "fee_x": self.fee_x,
            "fee_y": self.fee_y,
            "swap_count_x_to_y": self.swap_count_x_to_y,
            "swap_count_y_to_x": self.swap_count_y_to_x,
            "swap_count": self.swap_count_x_to_y + self.swap_count_y_to_x,
            "swap_in_x": self.swap_in_x,
            "swap_in_y": self.swap_in_y,
            "swap_out_x": self.swap_out_x,
            "swap_out_y": self.swap_out_y,
            "deposited_x": self.deposited_x,
            "deposited_y": self.deposited_y,
            "withdrawn_x": self.withdrawn_x,
            "withdrawn_y": self.withdrawn_y,
            "principal_x": self.deposited_x - self.withdrawn_x,
            "principal_y": self.deposited_y - self.withdrawn_y,
        }

    def user_stats_view(self, user: str) -> dict:
        """与 GET /pools/{id}/users/{user}/stats 的累计/份额字段同名。

        fee_share_x/y 与 share_ppm 都按 fold 出的当前持仓与池子累计手续费
        快照、以与接口相同的 floor 口径现算。
        """
        pos = self.position(user)
        total = self.total_shares
        return {
            "user": user,
            "shares": pos.shares,
            "total_shares": total,
            "share_ppm": pos.shares * 1_000_000 // total if total else 0,
            "fee_share_x": pos.shares * self.fee_x // total if total else 0,
            "fee_share_y": pos.shares * self.fee_y // total if total else 0,
            "contributed_x": pos.contributed_x,
            "contributed_y": pos.contributed_y,
            "withdrawn_x": pos.withdrawn_x,
            "withdrawn_y": pos.withdrawn_y,
        }


_SYSTEM_USER = "_system"  # 与 service 层 seed 份额持有人保持一致


def replay_ledger(items: list[dict]) -> LedgerReplay:
    """把流水（get_history 的 items，含 seed/add/remove/swap）fold 成快照。

    只消费 status == "success" 的记录；rejected（含失败重放、同 id 冲突、
    池不存在）一律跳过。按 seq 升序处理，保证与真实提交顺序一致。
    明细字段口径：
      seed:   detail.reserve_x/reserve_y/total_shares
      add:    detail.amount_x/amount_y/shares（实际存入与铸出份额）
      remove: detail.shares_burned/amount_x/amount_y（烧掉份额与取出两侧）
      swap:   detail.side/amount_in/amount_out/fee_in
    """
    replay = LedgerReplay()

    def pos_of(user: str) -> ReplayedPosition:
        pos = replay.positions.get(user)
        if pos is None:
            pos = ReplayedPosition()
            replay.positions[user] = pos
        return pos

    for rec in sorted(items, key=lambda r: r["seq"]):
        if rec["status"] != "success":
            continue
        op = rec["op"]
        d = rec["detail"]
        if op == "seed":
            rx, ry = d["reserve_x"], d["reserve_y"]
            replay.deposited_x += rx
            replay.deposited_y += ry
            minted = d["total_shares"]
            replay.total_shares += minted
            if minted:
                sp = pos_of(_SYSTEM_USER)
                sp.shares += minted
                sp.contributed_x += rx
                sp.contributed_y += ry
        elif op == "add":
            ax, ay, shares = d["amount_x"], d["amount_y"], d["shares"]
            replay.deposited_x += ax
            replay.deposited_y += ay
            replay.total_shares += shares
            p = pos_of(rec["user"])
            p.shares += shares
            p.contributed_x += ax
            p.contributed_y += ay
        elif op == "remove":
            burned, ox, oy = d["shares_burned"], d["amount_x"], d["amount_y"]
            replay.withdrawn_x += ox
            replay.withdrawn_y += oy
            replay.total_shares -= burned
            p = pos_of(rec["user"])
            p.shares -= burned
            p.withdrawn_x += ox
            p.withdrawn_y += oy
        elif op == "swap":
            ain, aout, fee = d["amount_in"], d["amount_out"], d["fee_in"]
            if d["side"] == "x_to_y":
                replay.swap_in_x += ain
                replay.swap_out_y += aout
                replay.fee_x += fee
                replay.swap_count_x_to_y += 1
            else:
                replay.swap_in_y += ain
                replay.swap_out_x += aout
                replay.fee_y += fee
                replay.swap_count_y_to_x += 1
        # policy / pause / resume 不动账，无字段可累计。
    return replay
