"""命名空间级容量配额：默认上限、按命名空间覆盖、用量核算与写入门槛。

用量口径（稳定且可重复核算）：
- 计入对象：同一命名空间下「未处于删除中（deleting=False）」的资源。
- 条数用量：上述资源的个数。
- 副本用量：上述资源 ``spec.replicas`` 的合计；未显式给出 replicas 按 0 计。
- 计入时机：创建在存储内提交新资源的同一临界区内计入；
  更新按副本增量（新值 - 旧值）结算；
  删除一旦被接受（标记 deleting）即释放全部额度。
- 不重复计入：删除中的资源一律不计入，重复删除幂等、不会重复释放；
  用量始终由当前资源集合扫描得出，重复执行结论收敛，不存在计数器泄漏。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .errors import QuotaExceededError
from .models import Resource

#: 默认配额：每个命名空间最多 5 个资源、副本合计最多 20。
DEFAULT_MAX_RESOURCES = 5
DEFAULT_MAX_REPLICAS = 20

#: 配额类别标识（用于错误 details 与日志）。
QUOTA_KIND_RESOURCES = "resources"
QUOTA_KIND_REPLICAS = "replicas"


@dataclass(frozen=True)
class Quota:
    """单个命名空间的配额上限。"""

    max_resources: int
    max_replicas: int

    def __post_init__(self) -> None:
        for field_name, value in (
            ("max_resources", self.max_resources),
            ("max_replicas", self.max_replicas),
        ):
            # bool 是 int 的子类，配额上限不接受布尔值。
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative int, got {value!r}")


@dataclass(frozen=True)
class Usage:
    """某命名空间在某一时刻的用量快照。"""

    namespace: str
    resource_count: int
    replica_total: int


def _replicas_of(spec: dict) -> int:
    """副本口径：缺失或显式为 None 按 0 计（spec 已过准入，类型必为 int）。"""
    replicas = spec.get("replicas")
    return replicas if isinstance(replicas, int) and not isinstance(replicas, bool) else 0


class QuotaManager:
    """配额管理器：负责上限解析、用量核算与写入判定（纯逻辑，不持有锁）。

    判定方法只读取传入的当前资源集合、要么放行要么抛 QuotaExceededError，
    不修改任何状态；并发安全由 ResourceStore 的同一把 asyncio.Lock 保证：
    「扫描用量 -> 判定 -> 提交写入」在同一临界区内完成，不会超卖。
    """

    def __init__(
        self,
        default_quota: Quota | None = None,
        overrides: dict[str, Quota] | None = None,
    ) -> None:
        self._default = default_quota or Quota(DEFAULT_MAX_RESOURCES, DEFAULT_MAX_REPLICAS)
        self._overrides: dict[str, Quota] = dict(overrides or {})

    def quota_for(self, namespace: str) -> Quota:
        """返回命名空间的生效配额（覆盖值优先，否则默认值）。"""
        return self._overrides.get(namespace, self._default)

    def set_quota(self, namespace: str, quota: Quota) -> None:
        """按命名空间单独调整配额（允许下调到低于当前用量，不改动任何既有资源）。"""
        if not namespace:
            raise ValueError("namespace must not be empty")
        self._overrides[namespace] = quota

    def usage(self, namespace: str, resources: Iterable[Resource]) -> Usage:
        """扫描当前资源集合核算用量：只计入未删除中的资源。"""
        counted = [
            r for r in resources
            if r.namespace == namespace and not r.deleting
        ]
        return Usage(
            namespace=namespace,
            resource_count=len(counted),
            replica_total=sum(_replicas_of(r.spec) for r in counted),
        )

    def check_create(self, namespace: str, spec: dict, resources: Iterable[Resource]) -> Usage:
        """创建门槛：新资源会让条数 +1、副本按请求值增加；超限即拒绝。

        创建只会让用量单调增加，因此超配状态下也必须同时满足两类上限。
        返回判定基于的用量快照（便于日志记录判定依据）。
        """
        current = self.usage(namespace, resources)
        quota = self.quota_for(namespace)
        requested_count = current.resource_count + 1
        requested_replicas = current.replica_total + _replicas_of(spec)
        self._enforce(
            namespace, quota,
            kind=QUOTA_KIND_RESOURCES,
            used=current.resource_count,
            requested=requested_count,
            limit=quota.max_resources,
            delta=1,
        )
        self._enforce(
            namespace, quota,
            kind=QUOTA_KIND_REPLICAS,
            used=current.replica_total,
            requested=requested_replicas,
            limit=quota.max_replicas,
            delta=_replicas_of(spec),
        )
        return current

    def check_update(
        self, namespace: str, existing: Resource, new_spec: dict,
        resources: Iterable[Resource],
    ) -> Usage:
        """更新门槛：条数不变，副本按增量（新值 - 旧值）结算。

        超配兼容：让用量变小或不变的写入始终放行（调小副本可帮助回落）；
        仅当本次写入会让该类用量「变大」且超过上限时才拒绝。
        """
        current = self.usage(namespace, resources)
        quota = self.quota_for(namespace)
        delta = _replicas_of(new_spec) - _replicas_of(existing.spec)
        requested_replicas = current.replica_total + delta
        self._enforce(
            namespace, quota,
            kind=QUOTA_KIND_REPLICAS,
            used=current.replica_total,
            requested=requested_replicas,
            limit=quota.max_replicas,
            delta=delta,
        )
        return current

    def capacity(self, namespace: str, resources: Iterable[Resource]) -> dict:
        """容量视图：上限、当前用量与是否超配一并返回。"""
        current = self.usage(namespace, resources)
        quota = self.quota_for(namespace)
        return {
            "namespace": namespace,
            "limits": {
                "maxResources": quota.max_resources,
                "maxReplicas": quota.max_replicas,
            },
            "usage": {
                "resourceCount": current.resource_count,
                "replicaTotal": current.replica_total,
            },
            "overQuota": (
                current.resource_count > quota.max_resources
                or current.replica_total > quota.max_replicas
            ),
        }

    def _enforce(
        self,
        namespace: str, quota: Quota, kind: str,
        *, used: int, requested: int, limit: int, delta: int,
    ) -> None:
        # 用量不增加（调小/持平）时一律放行，保证超配后可以回落、可以删除。
        if requested <= used:
            return
        if requested <= limit:
            return
        raise QuotaExceededError(
            f"namespace {namespace!r} {kind} quota exceeded",
            details={
                "namespace": namespace,
                "quotaType": kind,
                "used": used,
                "limit": limit,
                "delta": delta,
                "requested": requested,
            },
        )
