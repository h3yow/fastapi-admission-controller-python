# fastapi-admission-controller-python

一个声明式自定义资源（`Widget`）控制面：**准入校验** + **后台调和**，保证同一份期望状态在重复提交、并发更新与校验失败时得到一致且可解释的结论。

## 快速开始

```bash
uv sync
uv run uvicorn main:app --reload
```

创建资源：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/namespaces/default/widgets \
  -H 'content-type: application/json' \
  -d '{"name":"w1","spec":{"content":"hello"}}'
```

基于版本的乐观更新（返回体里的 `version` 作为下一次的 `expected_version`）：

```bash
curl -s -X PUT http://127.0.0.1:8000/api/v1/namespaces/default/widgets/w1 \
  -H 'content-type: application/json' \
  -d '{"expected_version":0,"spec":{"content":"world"}}'
```

删除（同时清理外部副作用）：

```bash
curl -s -X DELETE http://127.0.0.1:8000/api/v1/namespaces/default/widgets/w1
```

## 测试

```bash
uv run pytest                                             # 24 个用例
uv run pytest -o log_cli=true -o log_cli_level=INFO      # 打印输入与判定依据
```

## 文档

详见 [`docs/design.md`](docs/design.md)：准入规则与拒绝原因码、调和幂等语义、
乐观并发/CAS 冲突处理、删除与残留清理语义以及验证方法。

## 代码结构

```
app/
  models.py       资源/请求数据模型与允许命名空间
  errors.py       稳定可区分的错误原因码（reason）
  store.py        版本化、线程安全的内存存储（OCC + 状态 CAS）
  external.py     模拟外部系统（按稳定 ID 幂等，去重副作用）
  admission.py    纯准入校验（拒绝不落任何状态）
  reconciler.py   幂等调和、CAS 冲突重试、删除清理
  api.py          HTTP 面：webhook + CRUD + reconcile
tests/            准入 / 并发冲突 / 删除清理 / 幂等收敛
```
