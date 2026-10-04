"""版本化资源存储：所有写入先过准入，再按 resource_version 乐观并发控制。"""
from __future__ import annotations

import asyncio
import copy
import time
from typing import Any

from .admission import AdmissionController
from .errors import AlreadyExistsError, ConflictError, DeletingError, NotFoundError
from .models import Resource
from .quota import NamespaceQuota, QuotaManager


class ResourceStore:
    """进程内资源存储。

    并发语义：所有变更操作都在同一把 asyncio.Lock 内完成
    「读取当前版本 -> 准入/版本校验 -> 写入」，因此并发写入被串行化，
    基于过期 resource_version 的写入必然以 Conflict 失败，不会静默覆盖。

    配额语义：配额判定也在同一把锁内、基于当下真实用量完成，
    判定通过才写入，因此并发创建/调大副本不会超卖；被拒绝的请求
    不产生任何状态变化，也不占用额度。
    """

    def __init__(
        self,
        admission: AdmissionController,
        quota: QuotaManager | None = None,
    ) -> None:
        self.admission = admission
        self.quota = quota or QuotaManager()
        self._resources: dict[str, Resource] = {}
        self._lock = asyncio.Lock()

    async def create(self, namespace: str, name: str, spec: dict[str, Any]) -> Resource:
        async with self._lock:
            # 准入失败发生在任何状态变化之前。
            self.admission.validate_create(namespace, name, spec)
            key = f"{namespace}/{name}"
            if key in self._resources:
                raise AlreadyExistsError(
                    f"resource {key} already exists",
                    details={"namespace": namespace, "name": name},
                )
            # 配额判定：基于锁内当下真实用量，超限则拒绝且不留状态。
            self.quota.check_create(namespace, spec, self._resources.values())
            resource = Resource(namespace=namespace, name=name, spec=copy.deepcopy(spec))
            resource.resource_version = 1
            self._resources[key] = resource
            return resource

    async def update(
        self,
        namespace: str,
        name: str,
        spec: dict[str, Any],
        resource_version: int | None = None,
    ) -> Resource:
        async with self._lock:
            resource = self._get_locked(namespace, name)
            if resource.deleting:
                raise DeletingError(
                    f"resource {resource.key} is being deleted",
                    details={"namespace": namespace, "name": name},
                )
            # 准入校验（含不可变字段），失败则不产生任何变化。
            self.admission.validate_update(resource, spec)
            # 乐观并发控制：调用方携带的版本必须等于当前版本。
            if resource_version is not None and resource_version != resource.resource_version:
                raise ConflictError(
                    f"resource {resource.key} version conflict",
                    details={
                        "expected": resource_version,
                        "current": resource.resource_version,
                    },
                )
            # 配额判定：按副本增量结算，调小必然放行，放大超限则拒绝。
            self.quota.check_update(namespace, resource, spec, self._resources.values())
            resource.spec = copy.deepcopy(spec)
            resource.resource_version += 1
            resource.updated_at = time.time()
            return resource

    async def delete(self, namespace: str, name: str) -> Resource:
        """删除：标记 deleting 并清理残留实际状态。

        - 不存在：NotFound（结果稳定，重复调用结论一致）。
        - 已在删除中：幂等返回同一资源，不重复清理。
        - 清理只触碰本资源的 applied 状态，不影响其他资源；
          deleting 资源会被调和跳过，不会留下被误处理的状态。
        """
        async with self._lock:
            resource = self._get_locked(namespace, name)
            if resource.deleting:
                return resource  # 重复删除：幂等
            resource.deleting = True
            resource.applied = None  # 残留清理：回收实际状态
            resource.resource_version += 1
            resource.updated_at = time.time()
            return resource

    async def get(self, namespace: str, name: str) -> Resource:
        async with self._lock:
            return self._get_locked(namespace, name)

    async def list(self) -> list[Resource]:
        async with self._lock:
            return list(self._resources.values())

    async def quota_status(self, namespace: str) -> dict:
        """容量视图：上限 + 当前用量 + 是否超配（锁内一致快照）。"""
        async with self._lock:
            return self.quota.status(namespace, self._resources.values())

    async def set_quota(
        self, namespace: str, max_resources: int, max_replicas: int
    ) -> dict:
        """按命名空间调整上限，返回调整后的容量视图。

        允许调到低于当前用量：既有资源不受影响，仅后续放大性写入被拒。
        """
        async with self._lock:
            self.quota.set_quota(
                namespace,
                NamespaceQuota(max_resources=max_resources, max_replicas=max_replicas),
            )
            return self.quota.status(namespace, self._resources.values())

    async def reconcile(self, namespace: str, name: str) -> Resource:
        """调和单个资源：让实际状态收敛到期望状态（幂等）。

        applied == spec 时不产生任何副作用；只有真正发生变化才
        执行一次 apply（apply_count 递增），因此重复或并发触发
        调和不会产生重复副作用。
        """
        async with self._lock:
            resource = self._get_locked(namespace, name)
            if resource.deleting:
                return resource  # 删除中的资源不参与调和
            if resource.applied != resource.spec:
                resource.applied = copy.deepcopy(resource.spec)
                resource.apply_count += 1
            return resource

    async def reconcile_all(self) -> list[Resource]:
        async with self._lock:
            keys = [r.key for r in self._resources.values()]
        reconciled = []
        for key in keys:
            namespace, name = key.split("/", 1)
            reconciled.append(await self.reconcile(namespace, name))
        return reconciled

    def _get_locked(self, namespace: str, name: str) -> Resource:
        key = f"{namespace}/{name}"
        resource = self._resources.get(key)
        if resource is None:
            raise NotFoundError(
                f"resource {key} not found",
                details={"namespace": namespace, "name": name},
            )
        return resource
