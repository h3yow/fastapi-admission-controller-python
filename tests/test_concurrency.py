"""并发写入与版本冲突测试：冲突失败可区分，重复执行后状态收敛。"""
import asyncio

import pytest

from controlplane.errors import AlreadyExistsError, ConflictError
from tests.conftest import log_case

SPEC_V1 = {"image": "nginx:1.27", "replicas": 1}


async def test_concurrent_creates_only_one_wins(store):
    log_case("concurrent create", {"name": "web", "并发数": 8},
             "存储串行化写入，同 key 只允许一个创建成功，其余必须 AlreadyExists")
    results = await asyncio.gather(
        *(store.create("team-a", "web", SPEC_V1) for _ in range(8)),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, AlreadyExistsError)]
    log_case("concurrent create 结果", {"成功": len(successes), "AlreadyExists": len(failures)},
             "恰好 1 个成功、7 个冲突，无其他异常类型")
    assert len(successes) == 1
    assert len(failures) == 7
    resource = await store.get("team-a", "web")
    assert resource.resource_version == 1  # 失败的创建没有推进版本


async def test_concurrent_updates_with_same_version_conflict(store):
    await store.create("team-a", "web", SPEC_V1)
    stale_rv = 1
    log_case("concurrent update", {"name": "web", "resourceVersion": stale_rv, "并发数": 8},
             "8 个写入都基于 rv=1，串行化后只有一个能匹配当前版本，其余必须 Conflict")

    async def attempt(i: int):
        return await store.update(
            "team-a", "web",
            {"image": "nginx:1.27", "replicas": 10 + i},
            resource_version=stale_rv,
        )

    results = await asyncio.gather(*(attempt(i) for i in range(8)), return_exceptions=True)
    successes = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, ConflictError)]
    log_case("concurrent update 结果", {"成功": len(successes), "Conflict": len(conflicts)},
             "恰好 1 个成功、7 个 Conflict；最终版本号 = 2，无静默覆盖")
    assert len(successes) == 1
    assert len(conflicts) == 7
    for c in conflicts:
        assert c.reason == "Conflict"
        assert c.status_code == 409
    resource = await store.get("team-a", "web")
    assert resource.resource_version == 2
    # 最终状态等于唯一成功者的输入，不存在丢失更新或混合状态。
    assert resource.spec == successes[0].spec


async def test_stale_version_update_fails_explicitly(store):
    await store.create("team-a", "web", SPEC_V1)
    await store.update("team-a", "web", {"image": "nginx:1.27", "replicas": 3})
    log_case("update", {"name": "web", "resourceVersion": 1},
             "当前版本已是 2，基于 rv=1 的写入必须以 Conflict 失败而非静默覆盖")
    with pytest.raises(ConflictError) as exc_info:
        await store.update("team-a", "web", {"image": "nginx:1.27", "replicas": 9},
                           resource_version=1)
    assert exc_info.value.details == {"expected": 1, "current": 2}
    resource = await store.get("team-a", "web")
    assert resource.spec["replicas"] == 3  # 未被过期写入覆盖


async def test_read_modify_write_retry_converges(store):
    """典型的冲突重试循环：重新读取版本后写入必然成功，状态收敛。"""
    await store.create("team-a", "web", SPEC_V1)

    async def writer(target_replicas: int):
        while True:
            current = await store.get("team-a", "web")
            try:
                return await store.update(
                    "team-a", "web",
                    {"image": "nginx:1.27", "replicas": target_replicas},
                    resource_version=current.resource_version,
                )
            except ConflictError:
                continue  # 冲突后基于最新版本重试

    log_case("retry writers", {"并发数": 6}, "冲突-重读-重试循环最终全部成功，无丢失更新")
    results = await asyncio.gather(*(writer(i + 1) for i in range(6)))
    assert all(r.resource_version >= 2 for r in results)
    final = await store.get("team-a", "web")
    # 每次成功写入恰好推进一个版本：1(创建) + 6(更新) = 7。
    assert final.resource_version == 7
    assert final.spec["replicas"] in range(1, 7)
