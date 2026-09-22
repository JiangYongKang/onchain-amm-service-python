"""计价口径单测：舍入方向与份额边界。"""
from __future__ import annotations

from amm_service import pricing


def test_amount_out_basic_constant_product():
    # 1000 输入、0.3% 手续费、等比百万库存 => 996 输出
    out = pricing.amount_out(1000, 1_000_000, 1_000_000, 3000)
    assert out == 996


def test_amount_out_rounds_down_and_preserves_k():
    rx = ry = 1_000_000
    ain = 333
    fee = 3000
    out = pricing.amount_out(ain, rx, ry, fee)
    # 必须等于理论公式的精确向下取整值
    eff = ain * (1_000_000 - fee)
    expected = ry * eff // (rx * 1_000_000 + eff)
    assert out == expected
    assert expected != ry * eff / (rx * 1_000_000 + eff)  # 本例确实发生了舍入
    # k 只能因手续费增加，不能减少
    assert (rx + ain) * (ry - out) >= rx * ry
    # 向上取整值（out+1）会超过理论真实输出
    assert out + 1 > ry * eff / (rx * 1_000_000 + eff)


def test_amount_out_zero_inputs():
    assert pricing.amount_out(0, 100, 100, 3000) == 0
    assert pricing.amount_out(10, 0, 100, 3000) == 0
    assert pricing.amount_out(10, 100, 0, 3000) == 0


def test_no_fee_pool_is_pure_constant_product():
    # 无手续费：out = Rout*ain/(Rin+ain)，向下取整
    out = pricing.amount_out(1000, 1_000_000, 1_000_000, 0)
    assert out == 1000 * 1_000_000 // 1_001_000


def test_first_deposit_shares_are_min_of_sides():
    assert pricing.shares_for_first_deposit(100, 100) == 100
    # 不等值时取较小侧，份额绝不超过较小投入
    assert pricing.shares_for_first_deposit(100, 150) == 100


def test_deposit_shares_floor_and_no_inflation():
    # 总份额 3，库存 10/10，投入 3/3 => 每侧 floor(3*3/10)=0 => 0 份额
    assert pricing.shares_for_deposit(3, 3, 10, 10, 3) == 0
    # 非等比投入取较小比例侧
    shares = pricing.shares_for_deposit(50_000, 999_999, 100_000, 100_000, 100_000)
    assert shares == 50_000
    # 两侧 floor 后取 min，保证份额不被多铸
    shares = pricing.shares_for_deposit(7, 7, 10, 10, 10)
    assert shares == 7  # floor(7*10/10)=7


def test_withdrawal_floors_and_dust_stays():
    # 1/3 份额、10/10 库存 => floor(3*10/10)=3,3
    assert pricing.amounts_for_withdrawal(3, 10, 10, 10) == (3, 3)
    # 除不尽：1 份额、10/10 库存、总份额 3 => 3,3，dust 留存
    assert pricing.amounts_for_withdrawal(1, 10, 10, 3) == (3, 3)
    # 全额赎回清空
    assert pricing.amounts_for_withdrawal(10, 10, 10, 10) == (10, 10)
