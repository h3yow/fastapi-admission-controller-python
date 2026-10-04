# 声明式资源控制面

在 FastAPI 服务上实现的一套声明式资源控制面：对自定义资源提供**准入校验**与**后台调和**，
使同一份期望状态在重复提交、并发更新与校验失败时都能得到一致且可解释的结论。

## 架构

```
controlplane/
├── models.py      # 资源数据模型与 spec 字段白名单/类型/不可变字段定义
├── errors.py      # 可区分的错误类型（reason + HTTP 状态码）
├── admission.py   # 准入控制器：纯校验，不产生任何状态变化
├── quota.py       # 配额治理：命名空间上限、用量口径与超配判定
├── store.py       # 版本化存储：锁内「读版本->校验->写入」，乐观并发控制
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
| 命名空间配额超限（条数或副本合计） | `QuotaExceeded` | 403 |

错误响应统一为 `{"reason": ..., "message": ..., "details": {...}}`，
`details` 携带判定依据（如冲突的期望/当前版本号、被变更的不可变字段、
超限的配额类别/当前用量/上限）。

## 配额治理

配额是**服务端强制的写入门槛**：判定发生在存储锁内、任何状态变化之前，
被拒绝的请求不占用额度、不留下半成品状态。

### 上限与默认值

每个命名空间有两类上限，均可按命名空间单独覆盖（`PUT /quotas/{namespace}`）：

| 上限 | 默认值 | 含义 |
|---|---|---|
| `maxResources` | 5 | 该命名空间下资源条数上限 |
| `maxReplicas` | 20 | 该命名空间下所有资源副本数合计上限 |

### 用量口径

- **计入对象**：命名空间下所有未处于删除中（`deleting=false`）的资源；
  `spec.replicas` 缺省按 1 计入。
- **计入时机**：每次写入准入判定时，基于存储内当下的真实资源集合现算；
  判定与写入在同一把锁内完成，并发写入被串行化，**不会超卖**。
- **增量结算**：更新按 `delta = 新副本数 - 旧副本数` 结算；
  `delta <= 0`（调小或不变）必然放行。
- **释放时机**：删除被接受（标记 `deleting`）的瞬间即释放额度；
  重复删除幂等，不重复释放；删除中的资源不再计入后续核算。
- **无独立台账**：用量始终由权威状态现算，重复执行结论收敛，
  不存在额度越用越少的泄漏。

### 超配（配额低于当前用量）

把配额调低到当前用量之下是允许的，此时：

- 既有资源**不会被自动删除或修改**，仍可正常读取；
- 容量查询返回 `overQuota: true`；
- **收缩性写入放行**：调小副本数、删除资源；
- **放大性写入拒绝**：调大副本数、新建资源，直到用量回落到上限以内。

### 容量查询

`GET /quotas/{namespace}` 返回：

```json
{
  "namespace": "team-a",
  "limits": {"maxResources": 5, "maxReplicas": 20},
  "usage": {"resources": 2, "replicas": 6},
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
| POST | `/reconcile` / `/reconcile/{namespace}/{name}` | 触发调和 |
| GET | `/quotas/{namespace}` | 查询容量（上限 + 用量 + 是否超配） |
| PUT | `/quotas/{namespace}` | 调整命名空间配额（body: `{maxResources, maxReplicas}`） |

## 运行与验证

```bash
# 启动服务
uv run python main.py        # 或 uv run uvicorn main:app

# 运行测试（39 个用例，覆盖准入拒绝、并发写入、版本冲突、调和幂等、删除清理、
# 配额超限拒绝/并发不超卖/增量结算/删除释放/超配恢复）
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

# 查询容量 -> limits + usage + overQuota
curl localhost:8000/quotas/team-a

# 调小配额到副本合计 2 -> 之后的放大写入返回 403 QuotaExceeded
curl -X PUT localhost:8000/quotas/team-a \
  -H 'Content-Type: application/json' \
  -d '{"maxResources": 5, "maxReplicas": 2}'
curl -X PUT localhost:8000/resources/team-a/web \
  -H 'Content-Type: application/json' \
  -d '{"spec": {"image": "nginx:1.27", "replicas": 3}}'
# -> {"reason": "QuotaExceeded", "details": {"quota": "replicas", "used": ..., "limit": 2, ...}}
```
