"""测试夹具：可复现时钟、共享日志配置。"""
from __future__ import annotations

import logging

import pytest

from amm_service.service import AmmService
from amm_service.store import Store


class FakeClock:
    """可手动拨表的毫秒时钟。"""

    def __init__(self, start_ms: int = 1_000_000_000_000) -> None:
        self.t = start_ms

    def __call__(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def service(clock: FakeClock) -> AmmService:
    return AmmService(store=Store(), clock=clock)


def pytest_configure(config: object) -> None:
    # 单测日志：打印请求输入、池子前后状态与判定依据。
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
