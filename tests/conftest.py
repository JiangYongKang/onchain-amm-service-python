"""测试夹具：开启 AMM 判定日志（请求输入 / 池子前后状态 / 判定依据）。"""

import logging

import pytest


@pytest.fixture(autouse=True)
def _enable_amm_logs(caplog):
    caplog.set_level(logging.INFO, logger="amm")
    yield
