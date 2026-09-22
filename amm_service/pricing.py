"""纯函数计价口径：恒定乘积做市，整数最小单位记账，舍入方向明确。

口径总览
--------
- 所有金额均为整数（代币最小单位，如 wei 级），不使用浮点，杜绝二进制误差。
- 兑换：恒定乘积 x*y=k，手续费从输入侧内扣（fee_ppm / 1_000_000）。
  * 输出向下取整（对用户有利方向取保守值，多留在池内，k 永不减少）。
- 首次注入：按 min(x,y) 铸造份额（要求两者相等由服务层保证；若不等则取较小值，
  多出部分作为捐赠留存，份额绝不超过较小侧）。
- 后续注入：shares = min(dx * S / Rx, dy * S / Ry)，向下取整。
  取两侧比例的较小值，保证不会因舍入而凭空增发份额。
- 赎回：(dx = shares * Rx / S, dy = shares * Ry / S)，均向下取整；
  dust（余数）留存池内，总份额严格减少 shares。
"""
from __future__ import annotations

FEE_DENOMINATOR = 1_000_000


def amount_out(amount_in: int, reserve_in: int, reserve_out: int, fee_ppm: int) -> int:
    """给定输入数量，返回可获得的输出数量（向下取整）。

    amount_in_after_fee = amount_in * (1e6 - fee_ppm)
    out = reserve_out * amount_in_after_fee //
          (reserve_in * 1e6 + amount_in_after_fee)
    """
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    in_after_fee = amount_in * (FEE_DENOMINATOR - fee_ppm)
    numerator = reserve_out * in_after_fee
    denominator = reserve_in * FEE_DENOMINATOR + in_after_fee
    return numerator // denominator


def shares_for_first_deposit(dx: int, dy: int) -> int:
    """首次注入的份额口径：min(dx, dy)。

    服务层会要求首次注入两侧相等；若调用方传入不等值，取较小侧，
    保证份额不超过任何一侧的投入。
    """
    return min(dx, dy)


def shares_for_deposit(
    dx: int,
    dy: int,
    reserve_x: int,
    reserve_y: int,
    total_shares: int,
) -> int:
    """后续注入的份额：两侧按比例折算后取较小值，整体向下取整。"""
    sx = dx * total_shares // reserve_x
    sy = dy * total_shares // reserve_y
    return min(sx, sy)


def amounts_for_withdrawal(
    shares: int,
    reserve_x: int,
    reserve_y: int,
    total_shares: int,
) -> tuple[int, int]:
    """按份额比例赎回，两侧分别向下取整，dust 留存池内。"""
    out_x = shares * reserve_x // total_shares
    out_y = shares * reserve_y // total_shares
    return out_x, out_y
