# 声明式资源控制面设计说明

本服务在最小 FastAPI 骨架之上实现了一个自定义资源 **`Widget`** 的声明式控制面，包含两条主链路：

1. **准入控制（Admission）**：所有变更先经过纯校验，拒绝时给出可区分的稳定原因码，且不触碰任何持久状态。
2. **调和循环（Reconciliation）**：后台把期望状态（`spec`）幂等地收敛到外部系统中的实际状态（`gadget`），基于版本做乐观并发控制。

```
HTTP ──► admission（纯校验，可拒绝） ──► store（版本化写入） ──► reconciler ──► ExternalSystem（真实副作用）
```

## 1. 资源模型

| 字段 | 含义 | 约束 |
| --- | --- | --- |
| `namespace` / `name` | 资源标识 | 创建后不可变；命名空间必须在允许集合内 |
| `spec.content` | 期望载荷 | 非空字符串 |
| `spec.ref` | 关联到另一个 Widget | 只能引用**同命名空间**内资源；创建后**不可变** |
| `version` | 乐观锁版本号 | 每次接受变更自增；由服务端管理 |
| `status.observed_version` | 已调和的 `spec` 版本 | 仅由调和器写入 |
| `status.gadget_id` | 外部系统中的实际对象 ID | 由 `(namespace,name)` 确定性派生：`gadget-<ns>-<name>` |
| `deleting` | 删除墓碑标记 | 置位后拒绝一切 `spec` 更新 |

允许的命名空间：`default`、`team-a`、`team-b`（见 `app/models.py: ALLOWED_NAMESPACES`）。

## 2. 准入规则

准入逻辑位于 `app/admission.py`，是**无副作用的纯函数**：HTTP 层只有在收到 `allowed=True` 后才会调用存储层，因此被拒绝的请求不可能产生任何状态变化。

拒绝时返回稳定、可区分的 `reason`（HTTP 层映射到相应状态码）：

| reason | HTTP | 触发条件 |
| --- | --- | --- |
| `invalid_field` | 422 | 名称/命名空间为空、`content` 为空、`ref` 字段不完整、自引用、未知操作 |
| `immutable_field` | 422 | UPDATE 试图更改创建后冻结的 `spec.ref` |
| `identifier_conflict` | 409 | CREATE 的 `(namespace,name)` 已存在 |
| `cross_namespace_ref_forbidden` | 403 | `spec.ref` 指向其他命名空间（越权命名空间引用） |
| `namespace_not_allowed` | 403 | 目标命名空间不在允许集合内 |
| `version_conflict` | 409 | UPDATE 携带的 `expected_version` 与当前版本不一致 |
| `resource_is_deleting` | 409 | 对处于删除墓碑期的资源发起 UPDATE |
| `not_found` | 404 | UPDATE/DELETE 的对象不存在 |

校验顺序固定（身份 → 命名空间 → 操作专属规则 → 字段 → 不可变字段），因此同一非法请求每次得到相同结论。

### Admission Webhook

`POST /admission-widgets` 以 admission-review 风格做纯校验，返回：

```json
{ "allowed": false, "reason": "cross_namespace_ref_forbidden", "message": "..." }
```

该端点只读存储（用于判定存在性/当前版本），从不写入。

## 3. 调和语义（Reconciliation）

调和器位于 `app/reconciler.py`：

- **声明式**：输入只有当前 `spec`，输出是把外部系统收敛到该状态。
- **确定性外部标识**：Widget 拥有的 gadget ID 由身份派生（`gadget-<ns>-<name>`），因此重复调和永远 upsert 同一个对象，**不会产生重复副作用**。
- **幂等 no-op**：当 `status.observed_version == version`、`gadget_id` 匹配且外部内容等于 `spec.content` 时，调和直接返回，不做任何写入。
- **状态仅由调和器写**：`store.update_status` 是一次 compare-and-swap（CAS），只在存储版本仍等于读取版本时成功。
- **冲突重试而非覆盖**：如果调和过程中有新的 `spec` 写入，CAS 以 `version_conflict` 失败，调和器重新读取最新期望状态并重试，避免"丢失更新"。

`POST .../widgets/{name}/reconcile` 可手动触发；CREATE/UPDATE 成功后也会同步触发一次。

## 4. 并发与冲突处理

- **存储层**（`app/store.py`）所有读写由一把可重入锁保护，返回对象均为独立拷贝，调用方无法在锁外篡改存储。
- **乐观并发控制（OCC）**：UPDATE 必须携带 `expected_version`；版本不匹配立即返回 `409 version_conflict`，**绝不静默覆盖**。并发更新时恰好一个成功，其余全部冲突失败。
- **状态 CAS**：调和器的状态写入与 `spec.version` 绑定，过期状态写入失败并重试，杜绝调和器与新 spec 写入之间的丢失更新。
- **外部系统幂等**：`ExternalSystem.upsert_gadget/delete_gadget` 以稳定 ID 为键，并按 key 加锁，10 个线程并发调和同一资源也只会有 1 个 gadget。

## 5. 删除与残留清理

`DELETE` 流程：

1. 对象不存在 → `404 not_found`（稳定结果，重复删除同一个已删除对象同样得到 `not_found`）。
2. 放置删除墓碑 `deleting=true`（按版本 CAS；同版本重复调用幂等）。
3. 墓碑存在期间，任何 UPDATE 都被稳定拒绝（`resource_is_deleting`）。
4. `finalize_delete` 删除该 Widget 独占的 gadget（确定性 ID，按 key 幂等），然后删除存储记录。清理即使在记录已消失时也安全，崩溃于"清理后、删记录前"可再次调用收敛。
5. 清理只影响 `gadget-<ns>-<name>` 这一个外部对象，不触及任何其他资源；同名资源随后重建时从干净状态开始（版本归零、无残留 `gadget_id`）。

与删除完全并发的 UPDATE 只有两种合法线性序，两种都收敛到同一最终状态：
- 更新先于墓碑提交：更新短暂成功，随后被删除；
- 更新晚于墓碑：得到 `resource_is_deleting` / `version_conflict` / `not_found`。

## 6. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/admission-widgets` | 纯准入校验 |
| POST | `/api/v1/namespaces/{ns}/widgets` | 创建（过准入 + 调和） |
| GET | `/api/v1/namespaces/{ns}/widgets` | 列出 |
| GET | `/api/v1/namespaces/{ns}/widgets/{name}` | 获取 |
| PUT | `/api/v1/namespaces/{ns}/widgets/{name}` | 乐观更新（需 `expected_version`） |
| POST | `.../widgets/{name}/reconcile` | 触发调和 |
| DELETE | `.../widgets/{name}` | 删除 + 清理 |
| GET | `/healthz` | 健康检查 |

错误体统一为 `{"allowed": false, "reason": "<稳定码>", "message": "..."}`。

## 7. 验证方法

```bash
uv sync
uv run pytest                       # 全部测试
uv run pytest -o log_cli=true -o log_cli_level=INFO   # 同时查看输入/判定日志
uv run uvicorn main:app --reload    # 启动服务
```

测试组织：

- `tests/test_admission.py`：各类拒绝原因可区分，拒绝后无存储/无副作用；过期版本拒绝且不覆盖。
- `tests/test_concurrency.py`：8 线程并发更新恰好 1 胜 7 冲突、6 线程并发创建 1 成 5 冲突、10 线程并发调和只产生 1 个 gadget、调和 CAS 失败后重试不丢更新。
- `tests/test_deletion.py`：删除清理、重复删除、不存在资源、删除墓碑期更新稳定被拒、删除/更新竞态收敛、清理不波及他人、删除后重建无残留。
- `tests/test_reconcile.py`：重复调和幂等、spec 变更收敛、资源间隔离、墓碑资源不调和。

测试通过 `tests/conftest.py: log_request` 对每次 HTTP 调用打印 **输入（方法/路径/payload）** 与 **判定依据（HTTP 状态码/响应体）**；准入与调和模块在 INFO 级别打印决策理由（`reason`、版本、gadget 等）。
