"""仅凭历史流水重算对账口径的纯函数（不依赖内存中的池子/持仓状态）。

口径与 amm_service.service.AmmService 的统计端点严格一致：
- 只消费 status == "success" 的 seed/add/remove/swap 流水；
  被拒绝记录与 policy/pause/resume 管理记录不参与任何金额口径。
- 全部整数运算，份额占比与手续费分成向下取整（与统计端点同一公式）。
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

SYSTEM_USER = "_system"  # 与 service._SYSTEM_USER 一致：seed 初始库存的持有人


@dataclass
class RecomputedPoolStats:
    """池子维度重算结果，字段名与 GET /pools/{id}/stats 对齐。"""

    pool_id: str = ""
    deposited_x: int = 0
    deposited_y: int = 0
    withdrawn_x: int = 0
    withdrawn_y: int = 0
    fee_x: int = 0
    fee_y: int = 0
    swap_in_x: int = 0
    swap_in_y: int = 0
    swap_out_x: int = 0
    swap_out_y: int = 0
    swap_count_x_to_y: int = 0
    swap_count_y_to_x: int = 0
    total_shares: int = 0
    reserve_x: int = 0
    reserve_y: int = 0

    def as_dict(self) -> dict:
        return {
            "pool_id": self.pool_id,
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


@dataclass
class RecomputedUserStats:
    """用户维度重算结果，字段名与 GET /pools/{id}/users/{u}/stats 对齐。"""

    pool_id: str = ""
    user: str = ""
    shares: int = 0
    contributed_x: int = 0
    contributed_y: int = 0
    withdrawn_x: int = 0
    withdrawn_y: int = 0
    total_shares: int = 0
    share_ppm: int = 0
    fee_share_x: int = 0
    fee_share_y: int = 0

    def as_dict(self) -> dict:
        return {
            "pool_id": self.pool_id,
            "user": self.user,
            "shares": self.shares,
            "total_shares": self.total_shares,
            "share_ppm": self.share_ppm,
            "fee_share_x": self.fee_share_x,
            "fee_share_y": self.fee_share_y,
            "contributed_x": self.contributed_x,
            "contributed_y": self.contributed_y,
            "withdrawn_x": self.withdrawn_x,
            "withdrawn_y": self.withdrawn_y,
        }


def recompute_from_records(
    records: Iterable[dict],
    *,
    pool_id: str | None = None,
) -> dict:
    """从 GET /history 的 item 视图重算全部池子/用户口径。

    参数
    ----
    records: 流水 item（dict，含 seq/op/status/pool_id/user/detail）。
        应传入目标池子的**完整流水**（不按 op/status 预过滤；成功的 seed
        记录是本金与初始份额的出处）；缺页会导致重算缺口径。
        记录无需预先排序，函数内部按 seq 归位。
    pool_id: 可选，只重算指定池子。

    返回
    ----
    {"pools": {pool_id: 池子统计 dict（字段同 GET /pools/{id}/stats）},
     "users": {pool_id: {user: 用户统计 dict（字段同用户统计端点）}}}

    口径规则
    --------
    - 只消费 status == "success" 的 seed/add/remove/swap；
      rejected 记录与 policy/pause/resume 不参与金额（但保留在流水里可查）。
    - 幂等重放不产生第二条流水，因此同一请求天然只被计入一次。
    - share_ppm / fee_share_* 与服务端点同一公式、同样向下取整。
    """
    pools: dict[str, RecomputedPoolStats] = {}
    # (pool_id, user) -> [shares, contributed_x, contributed_y,
    #                     withdrawn_x, withdrawn_y]
    user_acc: dict[tuple[str, str], list[int]] = {}

    def user_slot(pid: str, user: str) -> list[int]:
        return user_acc.setdefault((pid, user), [0, 0, 0, 0, 0])

    for r in sorted(records, key=lambda x: x.get("seq", 0)):
        if r.get("status") != "success":
            continue
        op = r.get("op")
        if op not in ("seed", "add", "remove", "swap"):
            continue
        pid = r["pool_id"]
        if pool_id is not None and pid != pool_id:
            continue
        p = pools.setdefault(pid, RecomputedPoolStats(pool_id=pid))
        d = r.get("detail", {})

        if op == "seed":
            rx, ry = d["reserve_x"], d["reserve_y"]
            minted = d.get("total_shares", 0)
            p.deposited_x += rx
            p.deposited_y += ry
            p.reserve_x += rx
            p.reserve_y += ry
            p.total_shares += minted
            slot = user_slot(pid, SYSTEM_USER)
            slot[0] += minted
            slot[1] += rx
            slot[2] += ry

        elif op == "add":
            ax, ay, minted = d["amount_x"], d["amount_y"], d["shares"]
            p.deposited_x += ax
            p.deposited_y += ay
            p.reserve_x += ax
            p.reserve_y += ay
            p.total_shares += minted
            slot = user_slot(pid, r["user"])
            slot[0] += minted
            slot[1] += ax
            slot[2] += ay

        elif op == "remove":
            ox, oy = d["amount_x"], d["amount_y"]
            burned = d["shares_burned"]
            p.withdrawn_x += ox
            p.withdrawn_y += oy
            p.reserve_x -= ox
            p.reserve_y -= oy
            p.total_shares -= burned
            slot = user_slot(pid, r["user"])
            slot[0] -= burned
            slot[3] += ox
            slot[4] += oy

        else:  # swap
            side = d["side"]
            ain, out, fee = d["amount_in"], d["amount_out"], d["fee_in"]
            if side == "x_to_y":
                p.swap_in_x += ain
                p.swap_out_y += out
                p.fee_x += fee
                p.swap_count_x_to_y += 1
                p.reserve_x += ain
                p.reserve_y -= out
            else:
                p.swap_in_y += ain
                p.swap_out_x += out
                p.fee_y += fee
                p.swap_count_y_to_x += 1
                p.reserve_y += ain
                p.reserve_x -= out

    users_by_pool: dict[str, dict[str, dict]] = {}
    for (pid, user), slot in user_acc.items():
        p = pools[pid]
        total = p.total_shares
        shares = slot[0]
        u = RecomputedUserStats(
            pool_id=pid, user=user, shares=shares,
            contributed_x=slot[1], contributed_y=slot[2],
            withdrawn_x=slot[3], withdrawn_y=slot[4],
            total_shares=total,
            share_ppm=shares * 1_000_000 // total if total else 0,
            fee_share_x=shares * p.fee_x // total if total else 0,
            fee_share_y=shares * p.fee_y // total if total else 0,
        )
        users_by_pool.setdefault(pid, {})[user] = u.as_dict()

    return {
        "pools": {pid: p.as_dict() for pid, p in pools.items()},
        "users": users_by_pool,
    }

