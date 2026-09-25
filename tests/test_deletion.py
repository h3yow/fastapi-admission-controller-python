"""删除与残留清理测试：结果明确稳定，清理不影响其他资源、不留误处理状态。"""
import asyncio

import pytest

from controlplane.errors import DeletingError, NotFoundError
from tests.conftest import log_case

SPEC = {"image": "nginx:1.27", "replicas": 2}


async def test_delete_marks_and_cleans_residual_state(store):
    await store.create("team-a", "web", SPEC)
    await store.reconcile("team-a", "web")
    log_case("delete", {"name": "web"}, "删除应标记 deleting 并回收 applied 残留状态")
    deleted = await store.delete("team-a", "web")
    assert deleted.deleting is True
    assert deleted.applied is None  # 残留已清理


async def test_delete_not_found_is_stable(store):
    log_case("delete", {"name": "ghost"}, "资源不存在 -> NotFound，重复调用结论一致")
    for _ in range(2):
        with pytest.raises(NotFoundError) as exc_info:
            await store.delete("team-a", "ghost")
        assert exc_info.value.reason == "NotFound"


async def test_repeated_delete_is_idempotent(store):
    await store.create("team-a", "web", SPEC)
    first = await store.delete("team-a", "web")
    log_case("delete x2", {"name": "web"}, "重复删除幂等：返回同一资源，版本不再推进")
    second = await store.delete("team-a", "web")
    assert second.deleting is True
    assert second.resource_version == first.resource_version


async def test_update_during_deletion_rejected(store):
    await store.create("team-a", "web", SPEC)
    await store.delete("team-a", "web")
    log_case("update while deleting", {"name": "web"},
             "删除中的资源拒绝更新 -> ResourceDeleting，与 NotFound/Conflict 可区分")
    with pytest.raises(DeletingError) as exc_info:
        await store.update("team-a", "web", {"image": "nginx:1.27", "replicas": 9})
    assert exc_info.value.reason == "ResourceDeleting"
    assert exc_info.value.status_code == 409


async def test_concurrent_update_and_delete_single_winner(store):
    await store.create("team-a", "web", SPEC)
    log_case("delete vs update", {"name": "web"},
             "删除与更新并发：串行化后恰有一个生效，另一个得到可区分的失败")
    results = await asyncio.gather(
        store.delete("team-a", "web"),
        store.update("team-a", "web", {"image": "nginx:1.27", "replicas": 9}),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, Exception)]
    assert len(errors) == 1
    # 失败方要么 NotFound 不可能（删除不移除记录），要么是 ResourceDeleting。
    assert isinstance(errors[0], DeletingError)
    resource = await store.get("team-a", "web")
    assert resource.deleting is True
    assert resource.spec["replicas"] == 2  # 更新未生效


async def test_deleting_resource_skipped_by_reconcile(store):
    await store.create("team-a", "web", SPEC)
    await store.delete("team-a", "web")
    log_case("reconcile deleting", {"name": "web"},
             "删除中的资源不参与调和，不会重新 apply 已清理的状态")
    resource = await store.reconcile("team-a", "web")
    assert resource.applied is None
    assert resource.apply_count == 0


async def test_delete_does_not_affect_other_resources(store):
    await store.create("team-a", "web", SPEC)
    await store.create("team-a", "other", {"image": "redis:7", "replicas": 1})
    await store.reconcile_all()
    await store.delete("team-a", "web")
    log_case("delete isolation", {"删除": "web", "保留": "other"},
             "清理只作用于目标资源，其他资源状态与调和行为不受影响")
    other = await store.get("team-a", "other")
    assert other.deleting is False
    assert other.applied == other.spec
    await store.reconcile_all()  # 后续调和不会误处理已删除资源
    other_after = await store.get("team-a", "other")
    assert other_after.apply_count == 1
