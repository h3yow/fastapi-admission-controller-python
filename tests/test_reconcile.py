"""调和幂等性测试：重复/并发触发调和不产生重复副作用，状态收敛。"""
import asyncio

from controlplane.reconciler import Reconciler
from tests.conftest import log_case

SPEC = {"image": "nginx:1.27", "replicas": 2}


async def test_reconcile_applies_desired_state(store):
    await store.create("team-a", "web", SPEC)
    reconciler = Reconciler(store)
    log_case("reconcile", {"name": "web"}, "applied=None != spec -> 执行一次 apply")
    await reconciler.reconcile_once()
    resource = await store.get("team-a", "web")
    assert resource.applied == resource.spec
    assert resource.apply_count == 1


async def test_reconcile_is_idempotent(store):
    await store.create("team-a", "web", SPEC)
    reconciler = Reconciler(store)
    await reconciler.reconcile_once()
    log_case("reconcile x3", {"name": "web"}, "applied == spec 后重复调和应为空操作")
    await reconciler.reconcile_once()
    await reconciler.reconcile_once()
    resource = await store.get("team-a", "web")
    assert resource.apply_count == 1  # 副作用不重复


async def test_concurrent_reconcile_no_duplicate_side_effects(store):
    await store.create("team-a", "web", SPEC)
    log_case("concurrent reconcile", {"name": "web", "并发数": 10},
             "锁内检查 applied != spec，并发调和最多产生一次 apply")
    await asyncio.gather(*(store.reconcile("team-a", "web") for _ in range(10)))
    resource = await store.get("team-a", "web")
    assert resource.apply_count == 1
    assert resource.applied == resource.spec


async def test_reconcile_picks_up_update_exactly_once(store):
    await store.create("team-a", "web", SPEC)
    reconciler = Reconciler(store)
    await reconciler.reconcile_once()
    await store.update("team-a", "web", {"image": "nginx:1.27", "replicas": 5})
    log_case("reconcile after update", {"name": "web", "replicas": 5},
             "spec 变化后调和应恰好再 apply 一次，使实际状态追平期望状态")
    await reconciler.reconcile_once()
    resource = await store.get("team-a", "web")
    assert resource.applied["replicas"] == 5
    assert resource.apply_count == 2
    # 再调和不应有新的副作用。
    await reconciler.reconcile_once()
    assert (await store.get("team-a", "web")).apply_count == 2


async def test_reconcile_all_converges_multiple_resources(store):
    await store.create("team-a", "a", SPEC)
    await store.create("team-b", "b", {"image": "redis:7", "replicas": 1})
    reconciler = Reconciler(store)
    await reconciler.reconcile_once()
    await reconciler.reconcile_once()
    log_case("reconcile_all", {"资源数": 2}, "每个资源各 apply 一次，重复调和无新增副作用")
    for ns, name in [("team-a", "a"), ("team-b", "b")]:
        resource = await store.get(ns, name)
        assert resource.applied == resource.spec
        assert resource.apply_count == 1
