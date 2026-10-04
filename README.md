# 声明式资源控制面

在 FastAPI 服务上实现的一套声明式资源控制面：对自定义资源提供**准入校验**与**后台调和**，
使同一份期望状态在重复提交、并发更新与校验失败时都能得到一致且可解释的结论。

## 架构

```
controlplane/
├── models.py      # 资源数据模型与 spec 字段白名单/类型/不可变字段定义
├── errors.py      # 可区分的错误类型（reason + HTTP 状态码）
├── admission.py   # 准入控制器：纯校验，不产生任何状态变化
├── quota.py       # 命名空间级配额：默认/覆盖上限、用量核算、写入门槛
├── store.py       # 版本化存储：锁内「读版本->校验/配额->写入」，乐观并发控制
├── reconciler.py  # 后台调和循环（测试中可手动触发单轮）
└── api.py         # HTTP 层：REST 端点 + 统一错误响应
```

## 资源模型

每个资源包含：

- `namespace` / `name`：资源标识（二者联合唯一）。
- `spec`：期望状态。允许字段 `image`（str，必填）、`replicas`（int，0–100）、
  `paused`（bool）、`config`（dict）；其余字段一律拒绝。
- `resourceVersion`：每次成功写入（创建/更新/删除标记）严格递增。
- `deleting`：删除标记。
- `status.applied`：实际状态（已生效的 spec 快照）；`status.applyCount`：副作用执行次数。

## 准入规则

所有写入先过准入，**准入失败发生在任何状态变化之前**，被拒绝的请求不产生持久状态变化。

| 规则 | 拒绝原因 (`reason`) | HTTP 状态码 |
|---|---|---|
| spec 含未知字段 / 缺 `image` / 类型错误 / `replicas` 越界 | `InvalidSpec` | 422 |
| 更新变更不可变字段（`image`） | `ImmutableFieldChanged` | 422 |
| 同 namespace/name 已存在 | `AlreadyExists` | 409 |
| 命名空间不在授权集合内（默认 `team-a`/`team-b`） | `ForbiddenNamespace` | 403 |
| 资源不存在 | `NotFound` | 404 |
| 基于过期 `resourceVersion` 的写入 | `Conflict` | 409 |
| 更新删除中的资源 | `ResourceDeleting` | 409 |
| 写入会使用量超过命名空间配额（条数/副本合计） | `QuotaExceeded` | 429 |

错误响应统一为 `{"reason": ..., "message": ..., "details": {...}}`，
`details` 携带判定依据（如冲突的期望/当前版本号、被变更的不可变字段）。
配额拒绝的 `details` 形如
`{"namespace", "quotaType": "resources"|"replicas", "used", "limit", "delta", "requested"}`，
说明是哪一类配额超了、当前用量、上限与本次增量。

## 配额规则

每个命名空间有两类**服务端强制**的上限，是创建/更新的写入门槛（不是返回提示）：

| 配额 | 含义 | 默认值 |
|---|---|---|
| `maxResources` | 命名空间下计入用量的资源条数上限 | **5** |
| `maxReplicas` | 命名空间下计入用量的资源 `spec.replicas` 合计上限 | **20** |

- 默认值合理且可覆盖：可通过 `PUT /quotas/{namespace}`（body `{"maxResources", "maxReplicas"}`，
  均为非负整数）为单个命名空间单独调整；未配置的命名空间使用默认值，命名空间之间互不影响。
- 创建：条数 `+1`、副本按请求的 `replicas` 增加，任一类超限即拒绝。
- 更新：条数不变，副本按**增量**（新值 − 旧值）结算；调小按负增量释放额度。
- 配额校验是「提交写入前的最后一道关」，拒绝在任何状态变化之前抛出，
  **被拒绝的请求不占额度、不留半成品、不推进版本号**。

## 用量口径与释放时机

- **计入对象**：同一命名空间下所有「未处于删除中（`deleting == false`）」的资源。
- **条数用量**：上述资源的个数。
- **副本用量**：上述资源 `spec.replicas` 的合计；未显式提供 `replicas` 时按 0 计。
- **计入时机**：创建在存储锁内提交新资源的同一临界区计入；更新按副本增量结算。
- **释放时机**：删除一旦被接受（标记 `deleting`），该资源立即退出用量集合，
  条数与副本额度同步释放。
- **不重复、不泄漏**：删除中的资源一律不再计入；重复删除幂等，不会重复释放。
  用量始终由当前资源集合**实时扫描**得出而非维护独立计数器，重复执行结论收敛，
  不会出现额度越用越少的泄漏。
- **并发安全**：「扫描真实用量 → 判定 → 提交写入」与版本校验在同一把 `asyncio.Lock`
  临界区内完成，并发创建/并发放大被串行化，实际放行后的用量不会超过上限（无超卖）。

## 超配（配额下调到低于当前用量）的行为

允许把配额下调到低于当前用量，此时**不自动删除、不悄悄改动任何既有资源，读接口照常**：

- 容量视图中 `overQuota` 变为 `true`，仍可同时拿到上限与当前真实用量。
- 允许**让用量变小或不变**的写入：调小副本、删除资源始终放行。
- 拒绝**让用量变大**的写入：新建、调大副本继续返回 `QuotaExceeded`，
  直到用量回落到上限以内（`overQuota=false`）后放大写入自动恢复。

容量查询 `GET /quotas/{namespace}` 返回：

```json
{
  "namespace": "team-a",
  "limits": {"maxResources": 5, "maxReplicas": 20},
  "usage": {"resourceCount": 2, "replicaTotal": 8},
  "overQuota": false
}
```

## 调和语义

- 调和把实际状态（`applied`）收敛到期望状态（`spec`）。
- **幂等**：仅当 `applied != spec` 时才执行一次 apply（`applyCount` 递增）；
  重复或并发触发调和不会产生重复副作用。
- 删除中的资源不参与调和，不会被误处理。

## 并发与冲突处理

- 所有变更操作在同一把 `asyncio.Lock` 内完成「读取当前版本 → 准入/版本校验 → 写入」，
  并发写入被串行化。
- 更新可携带 `resourceVersion`：与当前版本不一致时以 `Conflict` 显式失败，
  **不会静默覆盖**；客户端应重新读取最新版本后重试（读-改-写重试循环必然收敛）。
- 并发创建同一资源：恰好一个成功，其余 `AlreadyExists`，失败方不推进版本号。
- 删除与更新并发：串行化后恰有一个生效；更新方得到 `ResourceDeleting`。

## 删除与残留清理

- 删除标记 `deleting` 并回收实际状态（`applied = None`），只作用于目标资源。
- 重复删除幂等：返回同一资源，版本号不再推进。
- 删除不存在的资源：稳定返回 `NotFound`。
- 删除中的资源被调和跳过，不会留下后续调和中被误处理的状态。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/resources/{namespace}` | 创建（body: `{name, spec}`） |
| PUT | `/resources/{namespace}/{name}` | 更新（body: `{spec, resourceVersion?}`） |
| DELETE | `/resources/{namespace}/{name}` | 删除（幂等） |
| GET | `/resources/{namespace}/{name}` | 查询单个 |
| GET | `/resources` | 列出全部 |
| GET | `/quotas/{namespace}` | 查询容量：上限、当前用量、是否超配 |
| PUT | `/quotas/{namespace}` | 按命名空间调整配额（body: `{maxResources, maxReplicas}`，非负整数） |
| POST | `/reconcile` / `/reconcile/{namespace}/{name}` | 触发调和 |

## 运行与验证

```bash
# 启动服务
uv run python main.py        # 或 uv run uvicorn main:app

# 运行测试（44 个用例，覆盖准入拒绝、并发写入/并发不超卖、版本冲突、调和幂等、
# 删除清理与额度释放、配额超限拒绝/超配恢复）
uv run pytest -q

# 查看单测日志中的输入与判定依据
uv run pytest -q -s          # 日志形如：CASE <动作> | input=<输入> | 判定依据: <理由>
```

手动验证示例：

```bash
curl -X POST localhost:8000/resources/team-a \
  -H 'Content-Type: application/json' \
  -d '{"name": "web", "spec": {"image": "nginx:1.27", "replicas": 2}}'

curl -X PUT localhost:8000/resources/team-a/web \
  -H 'Content-Type: application/json' \
  -d '{"spec": {"image": "nginx:1.27", "replicas": 3}, "resourceVersion": 1}'

# 过期版本写入 -> 409 Conflict
curl -X PUT localhost:8000/resources/team-a/web \
  -H 'Content-Type: application/json' \
  -d '{"spec": {"image": "nginx:1.27", "replicas": 9}, "resourceVersion": 1}'

# 查询容量（上限/当前用量/是否超配）
curl localhost:8000/quotas/team-a

# 按命名空间收紧配额（允许低于当前用量：只挡放大，不删资源）
curl -X PUT localhost:8000/quotas/team-a \
  -H 'Content-Type: application/json' \
  -d '{"maxResources": 5, "maxReplicas": 2}'
# 此后调大副本 -> 429 QuotaExceeded；调小副本/删除仍放行，回落之后自动恢复
```
