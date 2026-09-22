# 本地做市资金池兑换与份额记账服务

一个用 **本地状态** 模拟链上 AMM（恒定乘积做市）资金池的 FastAPI 服务：
支持流动性注入/赎回、带成交保护的兑换、并发安全与请求幂等。
所有金额与份额均为 **非负整数（最小货币单位）**，全程整数运算，不使用浮点数。

## 1. 快速开始

```bash
uv sync --group dev          # 安装运行与测试依赖
uv run uvicorn main:app --reload   # 启动服务（默认 http://127.0.0.1:8000）
uv run pytest                # 运行全部单测（含判定日志）
uv run pytest -o log_cli=true -o log_cli_level=INFO   # 实时查看每条请求的判定日志
```

## 2. 对外接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 健康检查 |
| GET | `/pools/{pool_id}` | 查询池子状态（两侧存量、总份额、各提供者持仓） |
| POST | `/pools/add-liquidity` | 注入流动性，铸造份额 |
| POST | `/pools/remove-liquidity` | 赎回份额，取回两侧代币 |
| POST | `/pools/swap` | 带成交保护的兑换 |

### 兑换示例

```jsonc
// POST /pools/swap
{
  "request_id": "uuid-001",          // 幂等键：同键同体只生效一次
  "pool_id": "p1",
  "trader": "alice",
  "amount_in": 100000,
  "min_amount_out": 90000,           // 最小成交量（滑点保护）
  "quote_amount_out": 90661,         // 客户端拿到的报价（行情保护）
  "quote_valid_until_ms": 1780000000000  // 报价有效期（毫秒时间戳）
}
```

成功响应携带 `pool_before` / `pool_after` / `reason`（份额或成交数量的计算依据），
便于复算与审计。失败响应统一为：

```json
{ "error": { "code": "SLIPPAGE", "message": "成交 75569 低于最小成交量 75570" } }
```

## 3. 份额计价口径（确定、可解释、不增发）

### 3.1 创世注入

池子无份额时，首次注入 `(amount_in, amount_out)` 铸造：

```
shares = isqrt(amount_in * amount_out)        # 几何平均，向下取整
```

因此恒有 `shares² ≤ amount_in · amount_out`，份额不会被凭空增发。

### 3.2 后续注入

按两侧代币占现有存量的比例，**取较小值并向下取整**：

```
shares = min(
  total_shares * amount_in  // reserve_in,
  total_shares * amount_out // reserve_out
)
```

单份额对应的两侧净资产都不会被稀释；若按口径铸出份额为 `0`，请求以
`INVALID_PARAMETER` 拒绝，且不写入任何状态。注入的舍入灰尘留存池内，
归剩余份额持有人。

### 3.3 赎回

* **部分赎回**：`pay = reserve * shares // total_shares`，两侧各自向下取整，
  灰尘留存池内；若任一侧应得为 `0`，以 `INVALID_PARAMETER` 拒绝，
  防止份额被零对价销毁。
* **全部赎回**（`shares == total_shares`）：取走池内全部存量，
  避免末位灰尘永久锁死。
* 申请份额超过本人持仓 → `INSUFFICIENT_SHARES`。

## 4. 兑换口径与成交保护

恒定乘积公式，手续费 **0.30%**（`30/10000`），输出向下取整：

```
in_after_fee = amount_in - amount_in * 30 // 10000
amount_out   = reserve_out * in_after_fee // (reserve_in + in_after_fee)
```

手续费使 `K = reserve_in · reserve_out` 严格不减；服务在每笔成功兑换后
断言该不变量。三道保护按固定顺序校验，失败原因互不混淆：

1. 参数合法性（`amount_in > 0`、有效期为正）
2. 请求是否过期（`now_ms > quote_valid_until_ms`）
3. 池子是否已注入流动性 / 投入是否小到应得为 0
4. **报价保护**：实际成交必须等于 `quote_amount_out`，行情在报价后
   发生变化即判报价失效（`STALE_QUOTE`），要求重新取价
5. **滑点保护**：成交不得低于 `min_amount_out`（`SLIPPAGE`）

所有被拒绝的请求都在任何状态写入之前返回，绝不留下部分生效的状态。

## 5. 失败原因分类

| code | HTTP | 触发场景 |
| --- | --- | --- |
| `INVALID_PARAMETER` | 422/409 | 金额/份额非正、铸出 0 份额、请求体字段缺失等 |
| `POOL_NOT_FOUND` | 404 | 池子不存在 |
| `POOL_NOT_INITIALIZED` | 409 | 池子存在但尚无流动性 |
| `REQUEST_EXPIRED` | 409 | 本地时间已超过报价有效期 |
| `STALE_QUOTE` | 409 | 报价与当前存量下的成交不一致（行情已变） |
| `SLIPPAGE` | 409 | 实际成交低于最小成交量 |
| `INSUFFICIENT_LIQUIDITY` | 409 | 投入过小，当前存量下应得为 0 |
| `INSUFFICIENT_SHARES` | 409 | 赎回份额超过本人持仓 |
| `DUPLICATE_IDEMPOTENCY_CONFLICT` | 409 | 同一 `request_id` 被用于不同请求体 |

## 6. 并发与幂等语义

* 每个写操作在 **同一把池级锁** 内完成「校验 + 记账 + 记录结果」，
  并发请求被串行化提交，不会互相覆盖或重复扣减。
* `request_id` 为幂等键：
  * **同键同体**重复到达（无论是否已过期）→ 返回首次的一致结果，状态只生效一次；
  * **同键异体** → 以 `DUPLICATE_IDEMPOTENCY_CONFLICT` 拒绝。

## 7. 日志与可复现性

每次成功 / 拒绝都会通过 `amm` logger 打印：请求输入、池子前后快照、
判定依据（计算公式或拒绝原因）。单测通过 `caplog` 捕获这些日志，
并发与边界场景的判定均可复盘。
