"""命名空间级容量治理：配额上限、用量口径与超配判定。

用量口径（核算规则）：
- 计入对象：命名空间下所有 **未处于删除中**（deleting=False）的资源。
- 计入时机：每次写入准入判定时，基于存储内当下的真实资源集合现算；
  判定与写入在同一把存储锁内完成，因此并发写入被串行化，不会超卖。
- 副本口径：spec.replicas 缺省按 1 计入；更新按增量结算
  （delta = 新副本数 - 旧副本数），调小副本 delta 为负，必然放行。
- 释放时机：删除被接受（标记 deleting=True）的瞬间即释放额度；
  重复删除幂等返回，不会再次扣减；删除中的资源不再计入后续核算。
- 无独立台账：用量始终由权威状态现算，重复执行结论收敛，
  不存在"额度越用越少"的泄漏。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .errors import QuotaExceededError
from .models import Resource

#: 默认配额：每个命名空间最多 5 个资源、副本合计最多 20。
DEFAULT_MAX_RESOURCES: int = 5
DEFAULT_MAX_REPLICAS: int = 20

#: spec.replicas 缺省时的计入口径。
DEFAULT_REPLICAS: int = 1


@dataclass(frozen=True)
class NamespaceQuota:
    """一个命名空间的两类上限。"""

    max_resources: int = DEFAULT_MAX_RESOURCES
    max_replicas: int = DEFAULT_MAX_REPLICAS

    def to_dict(self) -> dict:
        return {"maxResources": self.max_resources, "maxReplicas": self.max_replicas}


@dataclass(frozen=True)
class QuotaUsage:
    """一个命名空间的当前用量。"""

    resources: int = 0
    replicas: int = 0

    def to_dict(self) -> dict:
        return {"resources": self.resources, "replicas": self.replicas}


def counted_replicas(spec: dict[str, Any]) -> int:
    """单个资源计入配额的副本数：replicas 缺省按 1 计。"""
    replicas = spec.get("replicas", DEFAULT_REPLICAS)
    return replicas if isinstance(replicas, int) and not isinstance(replicas, bool) else DEFAULT_REPLICAS


class QuotaManager:
    """配额管理器。

    自身只保存「上限配置」（默认值 + 按命名空间覆盖），不保存用量台账；
    用量由调用方（存储层）在锁内传入权威资源集合现算，保证并发安全且
    不会因重复执行而漂移。
    """

    def __init__(self, default_quota: NamespaceQuota | None = None) -> None:
        self._default = default_quota or NamespaceQuota()
        self._overrides: dict[str, NamespaceQuota] = {}

    def quota_for(self, namespace: str) -> NamespaceQuota:
        """返回某命名空间生效的上限（覆盖值优先，否则默认）。"""
        return self._overrides.get(namespace, self._default)

    def set_quota(self, namespace: str, quota: NamespaceQuota) -> None:
        """按命名空间调整上限。允许调到低于当前用量（进入超配状态，
        既有资源不受影响，仅后续放大性写入被拒绝）。"""
        self._overrides[namespace] = quota

    def usage(self, namespace: str, resources: Iterable[Resource]) -> QuotaUsage:
        """按口径现算用量：只计入该命名空间下未删除中的资源。"""
        counted = [r for r in resources if r.namespace == namespace and not r.deleting]
        return QuotaUsage(
            resources=len(counted),
            replicas=sum(counted_replicas(r.spec) for r in counted),
        )

    def check_create(
        self, namespace: str, spec: dict[str, Any], resources: Iterable[Resource]
    ) -> None:
        """创建准入：条数 +1、副本 +新值 后均不得超过上限，否则拒绝。"""
        quota = self.quota_for(namespace)
        current = self.usage(namespace, resources)
        if current.resources + 1 > quota.max_resources:
            raise QuotaExceededError(
                f"namespace {namespace!r} resource count quota exceeded",
                details={
                    "namespace": namespace,
                    "quota": "resources",
                    "used": current.resources,
                    "limit": quota.max_resources,
                    "requested": current.resources + 1,
                },
            )
        new_replicas = counted_replicas(spec)
        if current.replicas + new_replicas > quota.max_replicas:
            raise QuotaExceededError(
                f"namespace {namespace!r} replicas quota exceeded",
                details={
                    "namespace": namespace,
                    "quota": "replicas",
                    "used": current.replicas,
                    "limit": quota.max_replicas,
                    "requested": current.replicas + new_replicas,
                },
            )

    def check_update(
        self,
        namespace: str,
        existing: Resource,
        new_spec: dict[str, Any],
        resources: Iterable[Resource],
    ) -> None:
        """更新准入：按增量结算。delta<=0（调小或不变）必然放行；
        delta>0 时要求 当前用量+delta 不超上限。条数不受更新影响。"""
        quota = self.quota_for(namespace)
        current = self.usage(namespace, resources)
        delta = counted_replicas(new_spec) - counted_replicas(existing.spec)
        if delta > 0 and current.replicas + delta > quota.max_replicas:
            raise QuotaExceededError(
                f"namespace {namespace!r} replicas quota exceeded",
                details={
                    "namespace": namespace,
                    "quota": "replicas",
                    "used": current.replicas,
                    "limit": quota.max_replicas,
                    "requested": current.replicas + delta,
                },
            )

    def status(self, namespace: str, resources: Iterable[Resource]) -> dict:
        """容量视图：上限 + 当前用量 + 是否超配（用量高于上限）。"""
        quota = self.quota_for(namespace)
        current = self.usage(namespace, resources)
        return {
            "namespace": namespace,
            "limits": quota.to_dict(),
            "usage": current.to_dict(),
            "overQuota": (
                current.resources > quota.max_resources
                or current.replicas > quota.max_replicas
            ),
        }
