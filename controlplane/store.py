"""版本化资源存储：所有写入先过准入，再按 resource_version 乐观并发控制。"""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from typing import Any

from .admission import AdmissionController
from .errors import AlreadyExistsError, ConflictError, DeletingError, NotFoundError
from .models import Resource
from .quota import Quota, QuotaManager, Usage

logger = logging.getLogger("controlplane.quota")


class ResourceStore:
    """进程内资源存储。

    并发语义：所有变更操作都在同一把 asyncio.Lock 内完成
    「读取当前版本 -> 准入/版本校验/配额校验 -> 写入」，因此并发写入被串行化，
    基于过期 resource_version 的写入必然以 Conflict 失败，不会静默覆盖；
    配额的「扫描真实用量 -> 判定 -> 提交」同样在该临界区内完成，不会超卖。
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
            # 配额门槛：基于服务端当下真实用量判定，通过后才提交；
            # 拒绝在插入之前抛出，不占额度、不留半成品。
            current = self.quota.check_create(namespace, spec, self._resources.values())
            resource = Resource(namespace=namespace, name=name, spec=copy.deepcopy(spec))
            resource.resource_version = 1
            self._resources[key] = resource
            logger.info(
                "create admitted ns=%s name=%s replicas=%s used=(count=%d,repl=%d)",
                namespace, name, spec.get("replicas", 0),
                current.resource_count, current.replica_total,
            )
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
            # 配额门槛（提交前最后一道关）：副本数按增量结算；
            # 调小/持平始终放行，调大且超上限才拒绝。拒绝不触碰任何状态。
            current = self.quota.check_update(
                namespace, resource, spec, self._resources.values()
            )
            old_replicas = resource.spec.get("replicas", 0)
            resource.spec = copy.deepcopy(spec)
            resource.resource_version += 1
            resource.updated_at = time.time()
            logger.info(
                "update admitted ns=%s name=%s replica_delta=%d used=(count=%d,repl=%d)",
                namespace, name, spec.get("replicas", 0) - old_replicas,
                current.resource_count, current.replica_total,
            )
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
            # 删除一旦接受，该资源即刻退出用量集合（deleting 不再被扫描计入），
            # 条数与副本额度同步释放；重复删除在此之前幂等返回，不会重复释放。
            logger.info(
                "delete admitted ns=%s name=%s released replicas=%s",
                namespace, name, resource.spec.get("replicas", 0),
            )
            return resource

    async def get(self, namespace: str, name: str) -> Resource:
        async with self._lock:
            return self._get_locked(namespace, name)

    async def list(self) -> list[Resource]:
        async with self._lock:
            return list(self._resources.values())

    async def set_quota(self, namespace: str, quota: Quota) -> None:
        """按命名空间调整配额。下调到低于当前用量也允许：不删除、不改动既有资源。"""
        async with self._lock:
            self.admission.validate_namespace(namespace)
            self.quota.set_quota(namespace, quota)
            logger.info("quota updated ns=%s quota=%s", namespace, quota)

    async def usage(self, namespace: str) -> Usage:
        """查询命名空间当前真实用量（只计未删除中的资源）。"""
        async with self._lock:
            return self.quota.usage(namespace, self._resources.values())

    async def capacity(self, namespace: str) -> dict:
        """查询容量信息：上限、当前用量、是否超配。"""
        async with self._lock:
            return self.quota.capacity(namespace, self._resources.values())

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
