"""HTTP 端到端：运营策略、流水历史与统计查询接口。"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import amm_service.api as api_mod
from main import app

log = logging.getLogger("test.api_ops")


@pytest.fixture
def client():
    api_mod.service.store.reset()
    with TestClient(app) as c:
        yield c


def _seed(client, rx=1_000_000, ry=1_000_000):
    r = client.post("/admin/pools", json={
        "pool_id": "p", "token_x": "X", "token_y": "Y",
        "reserve_x": rx, "reserve_y": ry,
    })
    assert r.status_code == 200, r.text


def _swap(client, rid, amount=1000):
    return client.post("/swaps", json={
        "request_id": rid, "pool_id": "p", "user": "alice",
        "side": "x_to_y", "amount_in": amount, "min_out": 0,
    })


def test_policy_pause_resume_over_http(client):
    """策略设置/熔断/恢复：超限与暂停返回可区分错误码。"""
    _seed(client)
    r = client.put("/admin/pools/p/policy", json={"max_swap_in": 500})
    assert r.json()["result"]["max_swap_in"] == 500

    r = _swap(client, "over", 501)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "SWAP_AMOUNT_EXCEEDS_LIMIT"

    client.post("/admin/pools/p/pause", json={"ops": ["swap"]})
    r = _swap(client, "paused", 100)
    assert r.json()["error"]["code"] == "POOL_PAUSED"

    client.post("/admin/pools/p/resume", json={"ops": ["swap"]})
    r = _swap(client, "ok", 100)
    assert r.status_code == 200, r.text

    policy = client.get("/admin/pools/p/policy").json()["result"]
    assert policy["paused_ops"] == [] and policy["max_swap_in"] == 500
    log.info("http policy: limit/pause/resume codes distinguishable")


def test_history_and_stats_over_http(client):
    """流水分页与池子/用户统计接口。"""
    _seed(client)
    client.post("/liquidity/add", json={
        "request_id": "a1", "pool_id": "p", "user": "bob",
        "amount_x": 100_000, "amount_y": 100_000,
    })
    _swap(client, "s1")
    _swap(client, "s1")  # 幂等重放，不新增流水
    _swap(client, "s2", 100_000)  # 大额成交，手续费足以分成到非零

    h = client.get("/history", params={"pool_id": "p", "page_size": 10})
    body = h.json()["result"]
    assert body["total"] == 3  # add + 2 swaps
    assert body["items"][0]["request_id"] == "s2"  # 最新在前

    # 非法分页参数 -> 422 INVALID_PARAMETER
    bad = client.get("/history", params={"page": 0})
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "INVALID_PARAMETER"

    st = client.get("/pools/p/stats").json()["result"]
    assert st["swap_count"] == 2 and st["fee_x"] > 0
    assert st["deposited_x"] == 100_000

    us = client.get("/pools/p/users/bob/stats").json()["result"]
    assert us["shares"] == 100_000
    assert us["fee_entitled_x"] > 0
    assert us["share_ppm"] == 100_000 * 1_000_000 // 1_100_000
    log.info("http history/stats: total=%s fee_x=%s bob entitled=%s",
             body["total"], st["fee_x"], us["fee_entitled_x"])


def test_reseed_over_http(client):
    """重复初始化：同参返回 created=false，异参 422。"""
    _seed(client)
    r = client.post("/admin/pools", json={
        "pool_id": "p", "token_x": "X", "token_y": "Y",
        "reserve_x": 1_000_000, "reserve_y": 1_000_000,
    })
    assert r.status_code == 200
    assert r.json()["result"]["created"] is False

    r = client.post("/admin/pools", json={
        "pool_id": "p", "token_x": "X", "token_y": "Y",
        "reserve_x": 5_000_000, "reserve_y": 5_000_000,
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "POOL_ALREADY_EXISTS"
    pool = client.get("/pools/p").json()["result"]
    assert pool["reserve_x"] == 1_000_000  # 未被叠加
    log.info("http reseed: idempotent replay / POOL_ALREADY_EXISTS")
