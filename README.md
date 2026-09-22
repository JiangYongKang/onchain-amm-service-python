# 本地做市资金池（AMM）兑换与份额记账服务

一个基于 FastAPI 的**本地内存版**恒定乘积做市服务：用本地状态模拟资金池存量与行情变化，
提供流动性注入/赎回、带成交保护的兑换、报价查询，并保证并发一致、重复请求幂等、
失败可区分且不留部分状态。

- 全部金额使用**整数最小单位**记账（不使用浮点），舍入方向显式可控。
- 单进程 asyncio，每个池子一把锁；幂等记录按 `(操作类型, request_id)` 存放。

## 目录结构

```
amm_service/
  errors.py    # 错误码与业务异常（失败原因稳定可区分）
  models.py    # 领域模型：池子状态、持仓、报价、幂等记录
  pricing.py   # 纯函数计价口径（兑换/份额/赎回）
  store.py     # 本地内存状态 + 每池并发锁
  service.py   # 业务编排：校验、幂等、并发、结构化日志
  schemas.py   # HTTP 请求模型
  api.py       # FastAPI 路由
main.py        # 应用入口与统一错误响应
tests/         # 计价 / 流动性 / 兑换 / 并发 / HTTP 测试
```

## 本地运行

```bash
uv sync --group dev          # 安装依赖（含 pytest）
uv run uvicorn main:app --reload
# 打开 http://127.0.0.1:8000/docs 可交互调用
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/admin/pools` | 播种本地池子（初始库存、代币名、手续费） |
| POST | `/liquidity/add` | 注入流动性（需 `request_id` 幂等） |
| POST | `/liquidity/remove` | 赎回份额（需 `request_id` 幂等） |
| POST | `/quotes` | 询价，返回期望输出与 `pool_version` |
| POST | `/swaps` | 兑换（需 `request_id`，支持成交保护） |
| GET  | `/pools/{pool_id}` | 查询池子状态 |
| GET  | `/pools/{pool_id}/positions/{user}` | 查询用户份额 |
| POST | `/admin/reset` | 清空全部本地状态 |

### 典型调用顺序

```bash
# 1) 播种：两侧各 1,000,000 最小单位，手续费 0.30%（3000 ppm）
curl -s -XPOST localhost:8000/admin/pools -H 'content-type: application/json' -d '{
  "pool_id":"p1","token_x":"X","token_y":"Y",
  "reserve_x":1000000,"reserve_y":1000000}'

# 2) 询价
curl -s -XPOST localhost:8000/quotes -H 'content-type: application/json' -d '{
  "pool_id":"p1","side":"x_to_y","amount_in":1000}'
# => amount_out=996, pool_version=0

# 3) 带保护成交：最小成交 996，携带报价与报价时版本，30 秒有效期
curl -s -XPOST localhost:8000/swaps -H 'content-type: application/json' -d '{
  "request_id":"tx-0001","pool_id":"p1","user":"alice",
  "side":"x_to_y","amount_in":1000,"min_out":996,
  "quoted_out":996,"quote_version":0,"deadline_ms":1750000000000}'
```

成功响应：`{"ok": true, "result": {...}}`
业务失败统一为 HTTP 422：`{"ok": false, "error": {"code": "...", "message": "..."}}`

## 份额计价口径（确定性、可解释）

所有运算均为整数运算，符号：库存 `Rx,Ry`，总份额 `S`，手续费 `f`（ppm）。

### 兑换（恒定乘积 `x*y=k`，手续费内扣）

```
eff_in   = amount_in * (1_000_000 - f)
amount_out = floor( Ry * eff_in / (Rx * 1_000_000 + eff_in) )
```

- 输出**向下取整**：保守地少给用户，余额留在池内，因此成交后
  `Rx' * Ry' >= Rx * Ry`（k 永不减少，手续费使其增长）。
- 库存更新原子完成：`Rx' = Rx + amount_in`，`Ry' = Ry - amount_out`。

### 注入流动性

- **首次注入**：要求 `amount_x == amount_y`，铸造 `shares = min(amount_x, amount_y)`。
  不接受非等比首注（避免初始比例口径歧义）。
- **后续注入**：

```
shares = min( floor(amount_x * S / Rx), floor(amount_y * S / Ry) )
```

  两侧比例分别向下取整后**取较小值**：任何一侧的舍入误差都不会让份额被多铸；
  非等比注入时，超出当前比例的部分视为捐赠留存池内，份额绝不凭空增发。
- `min_shares` 是滑点保护：实铸份额低于该值则拒绝（`SLIPPAGE_EXCEEDED`）。

### 赎回流动性

```
out_x = floor( shares * Rx / S )
out_y = floor( shares * Ry / S )
```

- 两侧分别**向下取整**，除不尽的 dust 留存池内；被销毁的份额严格等于 `shares`，
  不存在份额凭空消失。
- 持仓不足直接拒绝（`INSUFFICIENT_SHARE_BALANCE`），不产生任何扣减。
- `min_x / min_y` 为赎回滑点保护。
- 边界保护：若赎回全部份额会留下「总份额 0 但库存非 0」的不可续 dust 池，
  该请求以 `INVALID_PARAMETER` 拒绝（等比池全额赎回不受影响）。

### 份额守恒

任何时刻：`sum(所有用户持仓) == pool.total_shares`（测试强制断言）。

## 成交保护与失败原因分类

兑换按下述顺序校验，每个失败都有独立、稳定的错误码：

| 顺序 | 校验 | 错误码 | 含义 |
| --- | --- | --- | --- |
| 1 | 参数合法性 | `INVALID_PARAMETER` | side 非法、金额非正、首注不等比等 |
| 2 | 有效期 | `REQUEST_EXPIRED` | 当前时间晚于 `deadline_ms`（0 表示不限） |
| 3 | 报价新鲜度 | `STALE_QUOTE` | 携带 `quoted_out` 时，`quote_version` 与当前版本不一致，或实际输出已不等于报价 |
| 4 | 流动性 | `INSUFFICIENT_LIQUIDITY` | 输出侧库存为 0，或本次输出数量为 0 |
| 5 | 最小成交 | `SLIPPAGE_EXCEEDED` | `amount_out < min_out` |

其余错误码：

| 错误码 | 含义 |
| --- | --- |
| `POOL_NOT_FOUND` | 池子不存在（同样被幂等记录，重复请求回放同一错误） |
| `INSUFFICIENT_SHARE_BALANCE` | 赎回份额超过持仓 |
| `DUPLICATE_REQUEST_CONFLICT` | 同一 `request_id` 被用于**不同业务要素**的请求 |

## 并发与幂等语义

- **并发**：同一 `pool_id` 的写操作在同一把 `asyncio.Lock` 内串行提交；
  所有校验通过后才一次性改账并自增 `version`，任一校验抛出都发生在改账之前，
  因此被拒绝请求不留下部分状态。不同池子互不阻塞。
- **幂等**：键为 `(操作类型, request_id)`。
  - 同键同参：重复请求直接回放首次结果（成功返回相同输出并带 `"replayed": true`；
    失败回放相同错误码），不二次扣减、不重复铸/销份额。
  - 同键不同参：以 `DUPLICATE_REQUEST_CONFLICT` 拒绝。
  - 池不存在的请求也记录幂等，避免失败请求绕过记账。
- **行情变化模拟**：兑换与流动性操作都会改变库存比例与 `version`；
  先 `/quotes` 再 `/swaps` 时，若中间有其他成交使行情漂移，旧报价会被
  `STALE_QUOTE` 拒绝，从而得到可复现的结论。

## 本地验证

```bash
# 全量单测（并发、边界、不变量；日志打印请求输入/池子前后状态/判定依据）
uv run pytest -v

# 只看某类场景
uv run pytest tests/test_concurrency.py -v
uv run pytest tests/test_swap.py -v

# 启动服务手工验证
uv run uvicorn main:app --reload
```

测试日志示例（每条成交/拒绝都包含 `input`、`before`、`after`、`decision`、`reason`）：

```
swap {"decision":"accepted", "input":{...}, "before":{...}, "after":{...},
      "reason":"out=floor(...)=996; k_after>=k_before", ...}
swap_rejected {"decision":"rejected", "error":"SLIPPAGE_EXCEEDED", ...}
idempotency_replay {"decision":"replayed", ...}
```

关键不变量（测试覆盖）：

1. 成功兑换后 `reserve_x * reserve_y` 不减少；
2. 库存净变化 == 累计投入 − 累计产出；
3. 持仓总和 == 总份额；
4. 并发 N 笔成交后 `version` 恰好增加 N；相同 `request_id` 的并发重复只执行一次。

## 说明与限制

- 状态仅存于进程内存，重启即清空；`/admin/reset` 可显式重置。
- 不跟踪用户代币余额（本地模拟）：注入视为代币已转入，赎回视为代币已转出；
  份额持仓是唯一被严格记账的用户资产。
- 播种（seed）代表外部初始库存，对应份额记在系统账户 `_system` 名下。
