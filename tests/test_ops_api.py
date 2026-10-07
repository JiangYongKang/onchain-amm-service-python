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


def test_ledger_details_carry_replay_elements_over_http(client):
    """业务：HTTP 成功响应与流水明细带齐对账要素（追加字段，向后兼容）。"""
    _seed(client)
    add = client.post("/liquidity/add", json={
        "request_id": "a1", "pool_id": "p", "user": "bob",
        "amount_x": 10_000, "amount_y": 10_000}).json()["result"]
    # 成功响应：既有 shares 保留，追加两侧实际存入金额
    assert add["shares"] == 10_000
    assert add["amount_x"] == 10_000 and add["amount_y"] == 10_000

    rm = client.post("/liquidity/remove", json={
        "request_id": "r1", "pool_id": "p", "user": "bob",
        "shares": 3_000}).json()["result"]
    # 成功响应：既有 amount_x/y、remaining_shares 保留，追加烧掉份额
    assert rm["amount_x"] == rm["amount_y"] == 3_000
    assert rm["remaining_shares"] == 7_000
    assert rm["shares_burned"] == 3_000

    def detail(op, request_id):
        items = client.get("/history", params={
            "pool_id": "p", "op": op, "status": "success"}).json()["result"]["items"]
        return next(i["detail"] for i in items if i["request_id"] == request_id)

    assert detail("add", "a1") == {"shares": 10_000,
                                   "amount_x": 10_000, "amount_y": 10_000}
    assert detail("remove", "r1") == {
        "amount_x": 3_000, "amount_y": 3_000,
        "shares_burned": 3_000, "remaining_shares": 7_000}
    log.info("http ledger details: add{amount_x,amount_y,shares} and "
             "remove{shares_burned,amount_x,amount_y} present; legacy fields kept")


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
