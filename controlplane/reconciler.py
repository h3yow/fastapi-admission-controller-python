"""调和器：周期性把实际状态收敛到期望状态。"""
from __future__ import annotations

import asyncio
import logging

from .store import ResourceStore

logger = logging.getLogger("controlplane.reconciler")


class Reconciler:
    """后台调和循环。

    调和本身由 ResourceStore.reconcile 保证幂等；本类只负责触发节奏。
    测试中直接调用 reconcile_once()，不依赖后台任务。
    """

    def __init__(self, store: ResourceStore, interval: float = 30.0) -> None:
        self.store = store
        self.interval = interval
        self._task: asyncio.Task | None = None

    async def reconcile_once(self) -> None:
        """执行一轮调和（测试直接调用此方法）。"""
        reconciled = await self.store.reconcile_all()
        for resource in reconciled:
            logger.info(
                "reconciled %s rv=%d reconciled=%s apply_count=%d",
                resource.key,
                resource.resource_version,
                resource.applied == resource.spec,
                resource.apply_count,
            )

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.reconcile_once()
            except Exception:  # 单轮失败不终止调和循环
                logger.exception("reconcile loop iteration failed")
