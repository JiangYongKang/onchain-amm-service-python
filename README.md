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
  models.py    # 领域模型：池子状态、持仓、报价、幂等记录、运营策略、流水
  pricing.py   # 纯函数计价口径（兑换/份额/赎回）
  ledger.py    # 流水过滤与分页（纯函数）
  stats.py     # 手续费/做市收益对账口径（纯函数，可从流水重推）
  store.py     # 本地内存状态 + 每池并发锁
  service.py   # 业务编排：校验、幂等、并发、策略、流水、结构化日志
  schemas.py   # HTTP 请求模型
  api.py       # FastAPI 路由
main.py        # 应用入口与统一错误响应
tests/         # 计价 / 流动性 / 兑换 / 并发 / 手续费 / 流水 / 策略 / HTTP 测试
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
| POST | `/admin/pools` | 播种本地池子（初始库存、代币名、手续费）；重复初始化见下文 |
| POST | `/liquidity/add` | 注入流动性（需 `request_id` 幂等） |
| POST | `/liquidity/remove` | 赎回份额（需 `request_id` 幂等） |
| POST | `/quotes` | 询价，返回期望输出与 `pool_version` |
| POST | `/swaps` | 兑换（需 `request_id`，支持成交保护） |
| GET  | `/pools/{pool_id}` | 查询池子状态 |
| GET  | `/pools/{pool_id}/positions/{user}` | 查询用户份额 |
| GET  | `/pools/{pool_id}/stats` | 池子对账：本金/手续费/成交笔数/成交总量 |
| GET  | `/pools/{pool_id}/users/{user}/stats` | 用户对账：份额占比/应得手续费/累计贡献 |
| GET  | `/history` | 交易流水分页查询（pool_id/user/op/时间范围过滤） |
| PUT  | `/admin/pools/{pool_id}/policy` | 设置容量策略（单笔上限、缓冲水位） |
| GET  | `/admin/pools/{pool_id}/policy` | 查询当前策略 |
| POST | `/admin/pools/{pool_id}/pause` | 熔断：暂停 swap/add/remove |
| POST | `/admin/pools/{pool_id}/resume` | 恢复：解除熔断 |
| POST | `/admin/reset` | 清空全部本地状态 |

### 重复初始化（seed）语义

同一 `pool_id` 已存在时，不会再次叠加库存或份额：

- 参数与既有池子**完全一致**（代币、手续费、存量均相同，即池子尚未发生任何业务）：
  原样返回既有状态，响应带 `"created": false`（首次创建为 `true`）。
- 参数不一致（含存量已被成交改变后再次 seed）：以 `POOL_ALREADY_EXISTS` 拒绝，
  既有存量与份额不受影响。

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

## 手续费口径与归属规则

手续费从输入侧内扣，按**输入侧代币**计量：

```
effective_in = floor( amount_in * (1_000_000 - f) / 1_000_000 )
fee          = amount_in - effective_in
```

- `fee + effective_in == amount_in` 恒成立：舍入只影响「手续费/有效输入」的划分，
  不会凭空多出或少掉资产；手续费留存池内，体现为恒定乘积 k 的增长。
- **本金与手续费分列**：池子统计中 `deposited_*`/`withdrawn_*` 来自注入/赎回流水
  （用户本金），`fee_*` 来自成交流水（做市收益）；库存恒等式
  `reserve == seed + deposited - withdrawn + volume_in - volume_out` 可由流水逐项核对。
- **归属**：用户应得手续费分成 = `floor(池子累计手续费 * 用户份额 / 总份额)`，
  两侧分别计算、向下取整；取整余量（dust）留存池内，
  全体用户分成之和不超过池子实收手续费。
- 只有 `status == "success"` 的成交计入手续费与成交量；**被拒绝的请求不产生手续费**；
  同一 `request_id` 的重复提交只计一次。
- 所有统计数字都可从流水（`/history`）重新推导，测试对此做了恒等断言。

## 容量与熔断策略语义

管理员可按池子设置（`PUT /admin/pools/{pool_id}/policy`）：

| 策略 | 含义 | 超限错误码 |
| --- | --- | --- |
| `max_swap_in` | 单笔兑换输入上限（输入侧最小单位），0 = 不限 | `SWAP_AMOUNT_EXCEEDS_LIMIT` |
| `reserve_buffer` | 输出侧库存缓冲水位：成交后 `reserve_out` 不得低于该值，0 = 不启用 | `RESERVE_BUFFER_VIOLATION` |

熔断（`POST .../pause` / `.../resume`，`ops` 取 `swap`/`add`/`remove` 子集）：

- 暂停期间对应操作以 `POOL_PAUSED` 明确失败，**不留下部分生效的状态**
  （校验发生在改账之前，版本号不推进），拒绝记录同样进入流水。
- 未暂停的操作不受影响；暂停/恢复只影响之后受理的请求。
- **策略切换不改判已接受的请求**：同一 `request_id` 的幂等回放仍返回首次结果，
  即使新策略下同样参数会被拒绝。
- 恢复后第一笔交易从当前（暂停前）存量继续：版本连续、k 不减。

## 流水历史与统计查询

### `GET /history`

每笔被受理的请求（成功或被拒）恰好一条流水；幂等回放与冲突重试**不**新增记录。

- 过滤参数（均可选）：`pool_id`、`user`、`op`（`add`/`remove`/`swap`）、
  `since_ms`/`until_ms`（闭区间）。
- 分页：`page`（≥1）、`page_size`（1..500，默认 50），按 `seq` 倒序（最新在前）。
- 响应 `{total, page, page_size, items}`：`total` 是过滤后全集大小，与页码无关。
- 非法分页参数（`page < 1`、`page_size` 越界、时间范围倒置、未知 `op`）
  以 `INVALID_PARAMETER` 拒绝。
- 每条记录含 `seq/ts_ms/pool_id/user/op/request_id/status/error_code/detail`，
  失败记录的 `error_code` 即稳定错误码。

### `GET /pools/{pool_id}/stats`

`fee_x/fee_y`（两侧累计手续费）、`swap_count`、`volume_in_*/volume_out_*`、
`deposited_*/withdrawn_*`（本金进出）、当前库存与总份额。

### `GET /pools/{pool_id}/users/{user}/stats`

`shares`、`share_ppm`（份额占比，百万分比）、`fee_entitled_x/y`（应得手续费分成）、
`deposited_x/y` 与 `add_count`（累计贡献）。

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
| `POOL_ALREADY_EXISTS` | 重复初始化池子且参数与既有池子不一致 |
| `SWAP_AMOUNT_EXCEEDS_LIMIT` | 单笔兑换输入超过池子 `max_swap_in` |
| `RESERVE_BUFFER_VIOLATION` | 成交后输出侧库存将跌破 `reserve_buffer` |
| `POOL_PAUSED` | 对应操作已被管理员暂停 |

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
uv run pytest tests/test_concurrency.py -v   # 并发与幂等
uv run pytest tests/test_swap.py -v          # 兑换与成交保护
uv run pytest tests/test_fees_stats.py -v    # 手续费对账与做市收益
uv run pytest tests/test_history.py -v       # 流水历史与分页
uv run pytest tests/test_policy.py -v        # 容量/熔断/重复初始化
uv run pytest tests/test_api_ops.py -v       # 运营接口 HTTP 端到端

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
4. 并发 N 笔成交后 `version` 恰好增加 N；相同 `request_id` 的并发重复只执行一次；
5. 手续费 `fee + effective_in == amount_in`，全体用户分成之和 ≤ 池子实收手续费；
6. 流水条数 == 被受理请求数（回放/冲突不新增），`total` 与过滤结果一致；
7. 暂停期间版本号不推进，恢复后从暂停前存量继续且 k 不减；
8. 重复 seed 不改变存量与份额。

## 说明与限制

- 状态仅存于进程内存，重启即清空；`/admin/reset` 可显式重置。
- 不跟踪用户代币余额（本地模拟）：注入视为代币已转入，赎回视为代币已转出；
  份额持仓是唯一被严格记账的用户资产。
- 播种（seed）代表外部初始库存，对应份额记在系统账户 `_system` 名下。
