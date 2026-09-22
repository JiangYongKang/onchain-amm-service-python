"""HTTP 端到端：状态码、可区分错误码、端到端幂等。"""

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.pool import PoolService


@pytest.fixture()
def client():
    svc = PoolService()
    clock = {"now": 1_000}
    app = create_app(svc, clock=lambda: clock["now"])
    return TestClient(app), svc, clock


def _seed(client):
    c, _svc, _clock = client
    r = c.post("/pools/add-liquidity", json={
        "request_id": "seed", "pool_id": "p", "provider": "lp",
        "amount_in": 1_000_000, "amount_out": 1_000_000,
    })
    assert r.status_code == 200


def test_full_lifecycle_over_http(client):
    c, svc, _clock = client
    _seed(client)

    q = svc.quote("p", 100_000)
    payload = {
        "request_id": "s1", "pool_id": "p", "trader": "t",
        "amount_in": 100_000, "min_amount_out": q,
        "quote_amount_out": q, "quote_valid_until_ms": 2_000,
    }
    r1 = c.post("/pools/swap", json=payload)
    assert r1.status_code == 200
    assert r1.json()["amount_out"] == q

    # 端到端幂等：重复提交返回一致结果，不重复扣减
    r2 = c.post("/pools/swap", json=payload)
    assert r2.status_code == 200
    assert r2.json() == r1.json()

    st = c.get("/pools/p").json()
    assert st["reserve_in"] == 1_100_000
    assert st["reserve_out"] == 1_000_000 - q


def test_http_error_codes_are_distinguishable(client):
    c, svc, clock = client
    _seed(client)
    q = svc.quote("p", 100_000)

    def swap(**over):
        body = {
            "request_id": over.pop("rid"), "pool_id": "p", "trader": "t",
            "amount_in": 100_000, "min_amount_out": 1,
            "quote_amount_out": q, "quote_valid_until_ms": 5_000,
        }
        body.update(over)
        return c.post("/pools/swap", json=body)

    # 过期
    clock["now"] = 9_000
    r = swap(rid="expired")
    assert r.status_code == 409 and r.json()["error"]["code"] == "REQUEST_EXPIRED"
    clock["now"] = 1_000

    # 滑点（先取新鲜报价再抬高 min）
    r = swap(rid="slip", min_amount_out=10**12)
    assert r.status_code == 409 and r.json()["error"]["code"] == "SLIPPAGE"

    # 参数非法（pydantic 层）
    r = c.post("/pools/swap", json={"request_id": "bad", "pool_id": "p"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_PARAMETER"

    # 池子不存在
    assert c.get("/pools/ghost").status_code == 404
    assert c.get("/pools/ghost").json()["error"]["code"] == "POOL_NOT_FOUND"


def test_remove_over_held_rejected_over_http(client):
    c, _svc, _clock = client
    _seed(client)
    r = c.post("/pools/remove-liquidity", json={
        "request_id": "rm1", "pool_id": "p", "provider": "lp", "shares": 10**12,
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "INSUFFICIENT_SHARES"
