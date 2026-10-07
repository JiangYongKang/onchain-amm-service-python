"""新运营能力的 HTTP 端到端：统计、流水查询、容量与熔断管理接口。"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

import amm_service.api as api_mod
from main import app

log = logging.getLogger("test.ops_api")


@pytest.fixture
def client():
    api_mod.service.reset()
    with TestClient(app) as c:
        yield c


def _seed(client, pool_id="p", reserve=1_000_000):
    r = client.post("/admin/pools", json={
        "pool_id": pool_id, "token_x": "X", "token_y": "Y",
        "reserve_x": reserve, "reserve_y": reserve})
    assert r.status_code == 200, r.text


def _swap(client, request_id, amount_in=1000, min_out=0, pool_id="p"):
    return client.post("/swaps", json={
        "request_id": request_id, "pool_id": pool_id, "user": "alice",
        "side": "x_to_y", "amount_in": amount_in, "min_out": min_out})


def test_stats_and_user_stats_over_http(client):
    """业务：HTTP 查询池子手续费/成交统计与用户份额/分成。"""
    _seed(client)
    _swap(client, "s1")

    stats = client.get("/pools/p/stats").json()["result"]
    assert stats["fee_x"] == 3 and stats["swap_count"] == 1
    assert stats["principal_x"] == 1_000_000

    user = client.get("/pools/p/users/_system/stats").json()["result"]
    assert user["share_ppm"] == 1_000_000  # 系统账户持有全部份额
    assert user["fee_share_x"] == 3
    log.info("http stats: fee_x=%s count=%s; _system fee_share=%s",
             stats["fee_x"], stats["swap_count"], user["fee_share_x"])


def test_history_over_http_with_pagination(client):
    """业务：HTTP 流水查询，过滤+分页，total 与过滤结果一致。"""
    _seed(client)
    for i in range(5):
        _swap(client, f"s{i}")
    _swap(client, "bad", min_out=10**9)  # 失败一笔

    r = client.get("/history", params={"pool_id": "p", "op": "swap",
                                       "page": 1, "page_size": 4})
    body = r.json()["result"]
    assert body["total"] == 6 and len(body["items"]) == 4
    r2 = client.get("/history", params={"pool_id": "p", "op": "swap",
                                        "page": 2, "page_size": 4})
    body2 = r2.json()["result"]
    assert body2["total"] == 6 and len(body2["items"]) == 2

    r3 = client.get("/history", params={"status": "rejected"})
    assert r3.json()["result"]["total"] == 1
    assert r3.json()["result"]["items"][0]["error_code"] == "SLIPPAGE_EXCEEDED"

    # 非法分页参数 -> 422 INVALID_PARAMETER
    r4 = client.get("/history", params={"page": 0})
    assert r4.status_code == 422
    assert r4.json()["error"]["code"] == "INVALID_PARAMETER"
    r5 = client.get("/history", params={"page_size": 0})
    assert r5.status_code == 422
    log.info("http history: 6 swaps (1 rejected), pages 4+2, total stable; "
             "invalid page/page_size -> 422")


def test_policy_and_pause_over_http(client):
    """业务：HTTP 设置容量上限与暂停/恢复，错误码可区分。"""
    _seed(client)
    r = client.post("/admin/pools/p/policy", json={"max_swap_in": 500})
    assert r.json()["result"]["max_swap_in"] == 500

    r = _swap(client, "over", amount_in=501)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "AMOUNT_LIMIT_EXCEEDED"

    r = client.post("/admin/pools/p/pause", json={"op": "swap"})
    assert r.json()["result"]["swap_paused"] is True
    r = _swap(client, "paused", amount_in=100)
    assert r.json()["error"]["code"] == "POOL_PAUSED"

    r = client.post("/admin/pools/p/resume", json={"op": "swap"})
    assert r.json()["result"]["swap_paused"] is False
    r = _swap(client, "ok-after", amount_in=100)
    assert r.json()["ok"] is True

    # 非法暂停对象
    r = client.post("/admin/pools/p/pause", json={"op": "bogus"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_PARAMETER"
    log.info("http policy: limit/pause/resume effective, codes "
             "AMOUNT_LIMIT_EXCEEDED / POOL_PAUSED distinguishable")


def test_recompute_endpoint_matches_stats_over_http(client):
    """业务：HTTP 流水重算端点与 stats/user-stats 逐字段一致（含拒绝不参与）。"""
    _seed(client)
    r = client.post("/liquidity/add", json={
        "request_id": "a1", "pool_id": "p", "user": "alice",
        "amount_x": 10_000, "amount_y": 10_000})
    assert r.status_code == 200
    # 注入成功明细：两侧金额 + 铸出份额都可在流水里看到
    detail = r.json()["result"]
    assert detail["shares"] == 10_000
    assert detail["amount_x"] == detail["amount_y"] == 10_000
    _swap(client, "s1", amount_in=10_000)
    r = client.post("/liquidity/remove", json={
        "request_id": "r1", "pool_id": "p", "user": "alice", "shares": 4_000})
    assert r.status_code == 200
    # 赎回成功明细：销毁份额 + 两侧取出数量
    rm = r.json()["result"]
    assert rm["shares_burned"] == 4_000 and rm["amount_x"] > 0 and rm["amount_y"] > 0
    # 一笔被拒绝兑换：留痕但不参与口径
    assert _swap(client, "bad", amount_in=1000, min_out=10**9).status_code == 422

    stats = client.get("/pools/p/stats").json()["result"]
    rc = client.get("/pools/p/recompute").json()["result"]
    assert rc["pools"]["p"] == stats
    alice_live = client.get("/pools/p/users/alice/stats").json()["result"]
    assert rc["users"]["p"]["alice"] == alice_live
    log.info("http recompute: pool stats == ledger recompute field-by-field; "
             "alice=%s; rejected swap excluded but traceable", alice_live)


def test_reseed_over_http(client):
    """业务：HTTP 重复初始化——同配置原样返回，不同配置 422。"""
    _seed(client)
    r = client.post("/admin/pools", json={
        "pool_id": "p", "token_x": "X", "token_y": "Y",
        "reserve_x": 1_000_000, "reserve_y": 1_000_000})
    assert r.status_code == 200
    assert r.json()["result"]["already_exists"] is True
    assert r.json()["result"]["reserve_x"] == 1_000_000

    r = client.post("/admin/pools", json={
        "pool_id": "p", "token_x": "X", "token_y": "Z",
        "reserve_x": 1, "reserve_y": 1})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "POOL_ALREADY_EXISTS"

    pool = client.get("/pools/p").json()["result"]
    assert pool["reserve_x"] == 1_000_000 and pool["total_shares"] == 1_000_000
    log.info("http reseed: same config -> already_exists, different -> "
             "POOL_ALREADY_EXISTS, state intact")
