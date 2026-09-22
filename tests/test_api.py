"""HTTP 端到端：通过对外接口验证成功响应与可区分错误码。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import amm_service.api as api_mod
from main import app


@pytest.fixture
def client():
    api_mod.service.reset()
    with TestClient(app) as c:
        yield c


def _seed(client, rx=1_000_000, ry=1_000_000):
    r = client.post("/admin/pools", json={
        "pool_id": "p", "token_x": "X", "token_y": "Y",
        "reserve_x": rx, "reserve_y": ry,
    })
    assert r.status_code == 200, r.text


def test_full_flow_over_http(client):
    _seed(client)
    # 报价
    q = client.post("/quotes", json={
        "pool_id": "p", "side": "x_to_y", "amount_in": 1000
    }).json()["result"]
    assert q["amount_out"] == 996

    # 带保护成交
    r = client.post("/swaps", json={
        "request_id": "s1", "pool_id": "p", "user": "alice",
        "side": "x_to_y", "amount_in": 1000, "min_out": q["amount_out"],
        "quoted_out": q["amount_out"], "quote_version": q["pool_version"],
    })
    assert r.status_code == 200
    assert r.json()["result"]["amount_out"] == 996

    # 幂等重放
    r2 = client.post("/swaps", json={
        "request_id": "s1", "pool_id": "p", "user": "alice",
        "side": "x_to_y", "amount_in": 1000, "min_out": q["amount_out"],
        "quoted_out": q["amount_out"], "quote_version": q["pool_version"],
    })
    assert r2.json()["result"]["replayed"] is True

    pool = client.get("/pools/p").json()["result"]
    assert pool["version"] == 1  # 重放不产生第二次成交


def test_error_codes_are_distinguishable_over_http(client):
    _seed(client)
    cases = [
        ({"request_id": "e1", "pool_id": "p", "user": "a",
          "side": "bad", "amount_in": 1, "min_out": 0}, "INVALID_PARAMETER"),
        ({"request_id": "e2", "pool_id": "p", "user": "a",
          "side": "x_to_y", "amount_in": 1000, "min_out": 0,
          "deadline_ms": 1}, "REQUEST_EXPIRED"),
        ({"request_id": "e3", "pool_id": "p", "user": "a",
          "side": "x_to_y", "amount_in": 1000, "min_out": 10**9},
         "SLIPPAGE_EXCEEDED"),
        ({"request_id": "e4", "pool_id": "p", "user": "a",
          "side": "x_to_y", "amount_in": 1000, "min_out": 0,
          "quoted_out": 5, "quote_version": 0}, "STALE_QUOTE"),
    ]
    for body, expected_code in cases:
        r = client.post("/swaps", json=body)
        assert r.status_code == 422, body
        assert r.json()["error"]["code"] == expected_code, body

    # 不存在的池子
    r = client.post("/swaps", json={
        "request_id": "e5", "pool_id": "nope", "user": "a",
        "side": "x_to_y", "amount_in": 1, "min_out": 0})
    assert r.json()["error"]["code"] == "POOL_NOT_FOUND"


def test_liquidity_and_insufficient_shares_over_http(client):
    client.post("/admin/pools", json={
        "pool_id": "p", "token_x": "X", "token_y": "Y",
        "reserve_x": 0, "reserve_y": 0})
    r = client.post("/liquidity/add", json={
        "request_id": "a1", "pool_id": "p", "user": "bob",
        "amount_x": 1000, "amount_y": 1000})
    assert r.json()["result"]["shares"] == 1000

    r = client.post("/liquidity/remove", json={
        "request_id": "r1", "pool_id": "p", "user": "bob", "shares": 1_000_000})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INSUFFICIENT_SHARE_BALANCE"


def test_insufficient_liquidity_on_empty_pool_http(client):
    client.post("/admin/pools", json={
        "pool_id": "p", "token_x": "X", "token_y": "Y",
        "reserve_x": 0, "reserve_y": 0})
    r = client.post("/swaps", json={
        "request_id": "z1", "pool_id": "p", "user": "a",
        "side": "x_to_y", "amount_in": 1000, "min_out": 0})
    assert r.json()["error"]["code"] == "INSUFFICIENT_LIQUIDITY"
