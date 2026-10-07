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
  reconciliation.py # 仅凭历史流水重算池子/用户口径（对账纯函数）
  schemas.py   # HTTP 请求模型
  api.py       # FastAPI 路由
main.py        # 应用入口与统一错误响应
tests/         # 计价 / 流动性 / 兑换 / 并发 / 手续费对账 / 流水历史 / 容量熔断 / HTTP 测试
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
| GET  | `/pools/{pool_id}` | 查询池子状态（含容量/暂停策略字段） |
| GET  | `/pools/{pool_id}/positions/{user}` | 查询用户份额 |
| GET  | `/pools/{pool_id}/stats` | 池子对账统计：本金/手续费/成交笔数/成交量 |
| GET  | `/pools/{pool_id}/recompute` | 仅凭历史流水重算池子/用户口径（对账核对，与 stats 逐字段一致） |
| GET  | `/pools/{pool_id}/users/{user}/stats` | 用户统计：份额占比、应得手续费分成、累计贡献 |
| GET  | `/history` | 交易流水分页查询（pool_id/user/op/status/时间范围过滤） |
| POST | `/admin/pools/{pool_id}/policy` | 设置容量策略（`max_swap_in`、`reserve_buffer`） |
| POST | `/admin/pools/{pool_id}/pause` | 暂停某类操作（`{"op": "swap"\|"add"\|"remove"}`） |
| POST | `/admin/pools/{pool_id}/resume` | 恢复某类操作 |
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

## 手续费口径与归属规则

- **计费口径**：手续费从输入侧内扣。
  `effective_in = floor(amount_in * (1_000_000 - fee_ppm) / 1_000_000)`，
  `fee = amount_in - effective_in`。手续费留在输入侧库存中，随池子归全体
  流动性提供者按份额共有；兑换响应与流水 `detail.fee_in` 均携带该值。
- **本金与手续费分离**：池子维护只增计数器——`deposited_*`（注入累计，含
  seed 初始库存）、`withdrawn_*`（赎回累计）、`swap_in_* / swap_out_*`
  （兑换流量）、`fee_*`（手续费累计）、`swap_count_*`（分方向笔数）。
  `GET /pools/{pool_id}/stats` 返回全部计数器与净本金
  `principal_* = deposited_* - withdrawn_*`。
- **库存恒等式**（测试强制断言，可按侧核对）：

  ```
  reserve == deposited - withdrawn + swap_in - swap_out
  ```

- **可从流水重推**：每笔成功操作的流水 `detail` 都带齐还原要素——
  注入记录两侧实际存入 `amount_x/amount_y` 与铸出 `shares`，
  赎回记录销毁份额 `shares_burned` 与两侧实际取出 `amount_x/amount_y`，
  兑换记录 `side / amount_in / amount_out / fee_in`。统计端点的每个数字
  （含池子本金、用户累计贡献与累计赎回）都等于对 `GET /history`
  成功流水逐笔求和的结果，详见下文「仅凭历史流水重算对账」，测试逐字段断言。
- **用户视角**：`GET /pools/{pool_id}/users/{user}/stats` 返回
  `shares`、`share_ppm`（份额占比，百万分比）、`fee_share_x/y`
  （应得手续费分成 = `floor(shares * fee_total / total_shares)`，按当前
  持仓快照）与 `contributed_* / withdrawn_*`（累计注入/赎回）。
  分成向下取整，舍入余量留在池内未分配——全体用户分成之和不超过池子
  累计手续费，不凭空多发资产。

## 交易流水历史

- 每笔**成功与被拒绝**的操作（含 seed、policy、pause/resume 管理操作）
  都写一条流水：全局单调 `seq`、`ts_ms`、池子、用户、操作类型、
  `request_id`、状态与失败原因码、业务明细 `detail`。
- **成功明细自带全部还原要素**（追加字段，不改动既有字段含义）：

  | op | success 明细 `detail` 字段 | 可还原的口径 |
  | --- | --- | --- |
  | `seed` | `reserve_x` / `reserve_y` / `fee_ppm` / `total_shares` | 初始本金（归属系统账户 `_system`）与初始份额 |
  | `add` | `amount_x` / `amount_y`（两侧**实际存入**）、`shares`（**实际铸出**份额） | 池子/用户累计本金增加、份额增加 |
  | `remove` | `shares_burned`（**实际销毁**份额）、`amount_x` / `amount_y`（两侧**实际取出**，已按 floor 舍入）、`remaining_shares` | 池子累计赎回、份额减少；用户累计赎回与持仓 |
  | `swap` | `side` / `amount_in` / `amount_out`（已 floor）/ `fee_in` | 兑换流量、分方向笔数与手续费 |

  被拒绝记录的 `detail` 只带 `message`（冲突拒绝带 `reason`），失败原因以
  稳定的 `error_code` 区分；拒绝记录不携带金额、不参与任何统计口径。
- **幂等去重**：同一 `(操作类型, request_id)` 只记一条；成功/失败的重放
  以及池不存在的重复请求都不会产生第二条记录。
- **查询**：`GET /history`，过滤参数 `pool_id`、`user`、`op`
  （seed/add/remove/swap/policy/pause/resume）、`status`
  （success/rejected）、`since_ms`/`until_ms`（闭区间），分页参数
  `page`（>=1）、`page_size`（1..500）。每页返回的 `total` 恒等于
  过滤后的记录总数；参数非法以 `INVALID_PARAMETER` 明确拒绝。

## 仅凭历史流水重算对账（口径说明书）

统计接口报出的**每一个数字**都等于对该池子**完整历史流水**（不过滤
`op/status`：成功的 seed 记录是初始本金与初始份额的唯一出处）逐笔算出来的结果。
实现见 `amm_service/reconciliation.py`，服务端一键核对入口
`GET /pools/{pool_id}/recompute`；测试 `tests/test_reconciliation.py`
对混合/拒绝/重复/并发场景逐字段断言该不变量。

**参与重算的记录**：仅 `status == "success"` 且 `op ∈
{seed, add, remove, swap}`。被拒绝请求与 policy/pause/resume 管理记录
不参与金额（拒绝请求仍在流水中可查、原因可区分）。幂等重放不产生第二条
流水，所以同一笔请求无论提交多少次都只被计入一次。

**池子维度**（与 `GET /pools/{id}/stats` 同字段）：

```
deposited_*  = Σ seed.reserve_*                # seed 初始本金（_system 名下）
             + Σ add.amount_*                  # 每笔实际存入
withdrawn_*  = Σ remove.amount_*               # 每笔实际取出（floor 后）
principal_*  = deposited_* - withdrawn_*
swap_in_x    = Σ swap(amount_in, side=x_to_y)
swap_in_y    = Σ swap(amount_in, side=y_to_x)
swap_out_x   = Σ swap(amount_out, side=y_to_x)
swap_out_y   = Σ swap(amount_out, side=x_to_y)
fee_x        = Σ swap(fee_in, side=x_to_y)     # fee_in = amount_in - floor(amount_in*(1e6-f)/1e6)
fee_y        = Σ swap(fee_in, side=y_to_x)
swap_count_* = 对应方向成功 swap 的条数
```

**库存与份额还原**（任何时刻重算都不凭空多/少资产）：

```
reserve_x == principal_x + swap_in_x - swap_out_x
reserve_y == principal_y + swap_in_y - swap_out_y
total_shares == Σ seed.total_shares + Σ add.shares - Σ remove.shares_burned
             == Σ 各用户当前持仓
```

**用户维度**（与 `GET /pools/{id}/users/{user}/stats` 同字段）：

```
shares(u)        = Σ add.shares (u) - Σ remove.shares_burned (u)
                   （seed 的份额记在 _system 名下）
contributed_*(u) = Σ add.amount_* (u)
withdrawn_*(u)   = Σ remove.amount_* (u)
share_ppm        = floor(shares * 1_000_000 / total_shares)
fee_share_*(u)   = floor(shares * fee_total_* / total_shares)
```

**舍入一致性**：兑换输出、赎回两侧数量、份额占比、手续费分成全部向下取整
（floor），重算函数与记账/统计端点共用同一公式，因此重算结果逐字段相等，
不会比接口多一分或少一分；floor 的舍入余量（dust/未分配分成）留存在池内。

本地手工核对：

```bash
# 端点：直接返回 {pools: {id: 池子统计}, users: {id: {user: 用户统计}}}
curl -s localhost:8000/pools/p1/recompute | jq .result
curl -s localhost:8000/pools/p1/stats     | jq .result   # 两者 pools.p1 字段逐项相等
```

## 容量与熔断

- **容量策略**（`POST /admin/pools/{pool_id}/policy`，字段缺省表示不变）：
  - `max_swap_in`：单笔兑换输入上限，0 = 不限；超限以
    `AMOUNT_LIMIT_EXCEEDED` 拒绝（恰好等于上限可成交）。
  - `reserve_buffer`：输出侧缓冲水位，0 = 不限；成交后输出侧库存低于
    水位以 `RESERVE_BUFFER_BREACH` 拒绝（恰好等于水位可成交）。
- **熔断**：`POST /admin/pools/{pool_id}/pause|resume`，按操作类型
  （swap/add/remove）独立暂停/恢复。暂停期间对应请求以 `POOL_PAUSED`
  明确拒绝，校验发生在任何改账之前，不留部分状态；暂停只作用于目标池子。
- **策略语义**：所有校验与落账在同一把池子锁内原子完成——策略切换只影响
  切换后新到达的请求；已接受的请求不会被改判，其幂等重放仍返回首次结果；
  暂停/恢复不触碰存量与份额，恢复后的第一笔交易从当前真实存量继续定价。

## 重复初始化语义

同一 `pool_id` 重复调用 `POST /admin/pools`：

- 代币对与手续费率相同：视为幂等重试，**原样返回当前状态**并带
  `"already_exists": true`，不改动存量、份额与版本（修复了历史上重复
  初始化会悄悄叠加存量与份额的问题）；
- 配置不同：以 `POOL_ALREADY_EXISTS` 明确拒绝并记入流水。

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
| `POOL_ALREADY_EXISTS` | 以不同配置重复初始化同一池子 |
| `POOL_PAUSED` | 该类操作（swap/add/remove）已被管理员暂停 |
| `AMOUNT_LIMIT_EXCEEDED` | 单笔兑换输入超过池子 `max_swap_in` 上限 |
| `RESERVE_BUFFER_BREACH` | 成交后输出侧库存将低于 `reserve_buffer` 水位 |

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
uv run pytest tests/test_swap.py -v          # 兑换保护
uv run pytest tests/test_fees_stats.py -v    # 手续费对账与用户统计
uv run pytest tests/test_history.py -v       # 流水历史与分页
uv run pytest tests/test_reconciliation.py -v # 仅凭流水重算 == 统计接口（核心对账不变量）
uv run pytest tests/test_policy.py -v        # 容量、熔断、重复初始化
uv run pytest tests/test_ops_api.py -v       # 新能力的 HTTP 端到端

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
5. 按侧恒等式 `reserve == deposited − withdrawn + swap_in − swap_out` 恒成立，
   池子统计与逐笔流水重推结果完全一致；注入两侧金额、赎回销毁份额也逐笔可溯；
6. 仅凭成功流水重算的池子统计、用户累计贡献/赎回/当前份额与统计接口**逐字段
   相等**（含 floor 取整方向），被拒绝记录不参与但留痕可查、原因可区分；
7. 同一请求重复提交（含失败重放）只留一条参与重算的记录；并发注入/赎回/兑换
   后最终重算结果与接口一致，任何时刻重算都不凭空多/少资产；
8. 被拒绝的请求不产生手续费、不改变成交统计，但留痕可查；
9. 暂停期间请求全部明确失败且资金状态不变，恢复后从正确存量继续；
10. 重复初始化不叠加存量与份额（同配置幂等返回，不同配置明确拒绝）。

## 说明与限制

- 状态仅存于进程内存，重启即清空；`/admin/reset` 可显式重置。
- 不跟踪用户代币余额（本地模拟）：注入视为代币已转入，赎回视为代币已转出；
  份额持仓是唯一被严格记账的用户资产。
- 播种（seed）代表外部初始库存，对应份额记在系统账户 `_system` 名下。
