"""手续费与做市收益对账：全部数字可从流水重新推导（纯函数）。

口径
----
- 手续费按输入侧代币计量：fee = amount_in - floor(amount_in * (1e6 - fee_ppm) / 1e6)。
  因此 fee + effective_in == amount_in 恒成立：舍入只影响「手续费/有效输入」的划分，
  不会凭空多出或少掉资产；手续费留存池内，体现为恒定乘积 k 的增长。
- 池子统计只累计 status == "success" 的 swap 流水；被拒绝的请求不产生手续费。
- 用户应得手续费分成 = floor(池子累计手续费 * 用户份额 / 总份额)，两侧分别计算、
  向下取整；取整余量视为未分配 dust 留存池内，任何用户的应得不会超过池子实收。
- 用户累计贡献 = 该用户全部成功注入流水的 amount_x / amount_y 之和。
"""
from __future__ import annotations

from collections.abc import Iterable

from .models import LedgerEntry
from .pricing import FEE_DENOMINATOR


def fee_for_swap(amount_in: int, fee_ppm: int) -> int:
    """单笔兑换的输入侧手续费（最小单位，非负，fee <= amount_in）。"""
    effective_in = amount_in * (FEE_DENOMINATOR - fee_ppm) // FEE_DENOMINATOR
    return amount_in - effective_in


def pool_trading_stats(entries: Iterable[LedgerEntry], pool_id: str) -> dict:
    """池子维度：两侧累计手续费、成交笔数、成交总量（按输入/输出分列）。"""
    fee_x = fee_y = 0
    count = 0
    vol_in_x = vol_in_y = 0
    vol_out_x = vol_out_y = 0
    for e in entries:
        if e.pool_id != pool_id or e.op != "swap" or e.status != "success":
            continue
        d = e.detail
        fee = fee_for_swap(d["amount_in"], d["fee_ppm"])
        count += 1
        if d["side"] == "x_to_y":
            fee_x += fee
            vol_in_x += d["amount_in"]
            vol_out_y += d["amount_out"]
        else:
            fee_y += fee
            vol_in_y += d["amount_in"]
            vol_out_x += d["amount_out"]
    return {
        "pool_id": pool_id,
        "fee_x": fee_x,
        "fee_y": fee_y,
        "swap_count": count,
        "volume_in_x": vol_in_x,
        "volume_in_y": vol_in_y,
        "volume_out_x": vol_out_x,
        "volume_out_y": vol_out_y,
    }


def user_contributions(
    entries: Iterable[LedgerEntry], pool_id: str
) -> dict[str, dict]:
    """用户维度累计贡献：成功注入的两侧总量与注入笔数。"""
    out: dict[str, dict] = {}
    for e in entries:
        if e.pool_id != pool_id or e.op != "add" or e.status != "success":
            continue
        c = out.setdefault(
            e.user, {"deposited_x": 0, "deposited_y": 0, "add_count": 0}
        )
        c["deposited_x"] += e.detail["amount_x"]
        c["deposited_y"] += e.detail["amount_y"]
        c["add_count"] += 1
    return out


def user_fee_entitlement(fee_total: int, shares: int, total_shares: int) -> int:
    """按份额占比折算的应得手续费，向下取整（余量留存池内）。"""
    if total_shares <= 0 or shares <= 0:
        return 0
    return fee_total * shares // total_shares
