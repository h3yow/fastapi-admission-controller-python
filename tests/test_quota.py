"""命名空间配额测试：超限拒绝不留状态、并发不超卖、增量结算、删除释放、超配行为。"""
import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from controlplane.admission import AdmissionController
from controlplane.api import create_app
from controlplane.errors import QuotaExceededError
from controlplane.quota import NamespaceQuota, QuotaManager
from controlplane.store import ResourceStore
from tests.conftest import log_case

SPEC_R1 = {"image": "nginx:1.27", "replicas": 1}


@pytest.fixture
def quota_store() -> ResourceStore:
    # 小配额便于触发上限：3 个资源、副本合计 6。
    return ResourceStore(
        AdmissionController(allowed_namespaces={"team-a", "team-b"}),
        QuotaManager(NamespaceQuota(max_resources=3, max_replicas=6)),
    )


async def test_create_rejected_when_resource_count_exceeded(quota_store):
    for i in range(3):
        await quota_store.create("team-a", f"r{i}", SPEC_R1)
    log_case("create 第 4 个资源", {"namespace": "team-a", "已用条数": 3, "上限": 3},
             "resources 配额：3+1 > 3 -> QuotaExceeded，且不得留下任何状态")
    with pytest.raises(QuotaExceededError) as exc_info:
        await quota_store.create("team-a", "r3", SPEC_R1)
    err = exc_info.value
    log_case("拒绝明细", err.to_dict(),
             "reason=QuotaExceeded 与 InvalidSpec/AlreadyExists/ForbiddenNamespace 可区分；"
             "details 指明配额类别、当前用量与上限")
    assert err.reason == "QuotaExceeded"
    assert err.status_code == 403
    assert err.details["quota"] == "resources"
    assert err.details["used"] == 3 and err.details["limit"] == 3
    # 不留半成品：取不到、列表不变、版本号体系未推进。
    with pytest.raises(Exception):
        await quota_store.get("team-a", "r3")
    assert len(await quota_store.list()) == 3
    status = await quota_store.quota_status("team-a")
    assert status["usage"] == {"resources": 3, "replicas": 3}


async def test_create_rejected_when_replicas_exceeded(quota_store):
    await quota_store.create("team-a", "big", {"image": "nginx:1.27", "replicas": 5})
    log_case("create 副本超限", {"已用副本": 5, "请求副本": 2, "上限": 6},
             "replicas 配额：5+2 > 6 -> QuotaExceeded，额度不被占用")
    with pytest.raises(QuotaExceededError) as exc_info:
        await quota_store.create("team-a", "more", {"image": "nginx:1.27", "replicas": 2})
    assert exc_info.value.details["quota"] == "replicas"
    assert exc_info.value.details["used"] == 5
    assert exc_info.value.details["limit"] == 6
    assert exc_info.value.details["requested"] == 7
    status = await quota_store.quota_status("team-a")
    assert status["usage"]["replicas"] == 5  # 被拒绝的请求没有占用额度


async def test_concurrent_creates_never_oversell(quota_store):
    log_case("并发 create", {"并发数": 10, "条数上限": 3},
             "配额判定与写入在同一把锁内串行：恰好 3 个成功，其余 QuotaExceeded，不超卖")
    results = await asyncio.gather(
        *(quota_store.create("team-a", f"c{i}", SPEC_R1) for i in range(10)),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    rejected = [r for r in results if isinstance(r, QuotaExceededError)]
    log_case("并发 create 结果", {"成功": len(successes), "QuotaExceeded": len(rejected)},
             "成功数 == 上限 3，其余全部因配额被拒，无其他异常")
    assert len(successes) == 3
    assert len(rejected) == 7
    status = await quota_store.quota_status("team-a")
    assert status["usage"] == {"resources": 3, "replicas": 3}  # 实际用量未越上限


async def test_concurrent_scale_up_never_oversell(quota_store):
    await quota_store.create("team-a", "a", {"image": "nginx:1.27", "replicas": 2})
    await quota_store.create("team-b", "b", {"image": "redis:7", "replicas": 1})
    log_case("并发调大副本", {"当前副本": 2, "目标副本": [5, 6, 7], "上限": 6},
             "按增量串行结算：5/6 必然放行，7 必然被拒，放行后的总副本不得超过 6")
    results = await asyncio.gather(
        *(quota_store.update("team-a", "a", {"image": "nginx:1.27", "replicas": t})
          for t in (5, 6, 7)),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    rejected = [r for r in results if isinstance(r, QuotaExceededError)]
    final = await quota_store.get("team-a", "a")
    status = await quota_store.quota_status("team-a")
    log_case("并发调大结果", {"成功": len(successes), "拒绝": len(rejected),
                            "最终副本": final.spec["replicas"], "用量": status["usage"]},
             "恰好 2 个成功、1 个 QuotaExceeded；最终用量 <= 上限 6，不超卖")
    assert len(successes) == 2
    assert len(rejected) == 1
    assert rejected[0].details["quota"] == "replicas"
    assert status["usage"]["replicas"] <= 6
    assert status["usage"]["replicas"] == final.spec["replicas"]


async def test_scale_down_settles_correctly(quota_store):
    await quota_store.create("team-a", "web", {"image": "nginx:1.27", "replicas": 5})
    log_case("调小副本", {"旧副本": 5, "新副本": 2},
             "delta = 2-5 = -3 <= 0 必然放行；用量按增量结算为 2")
    await quota_store.update("team-a", "web", {"image": "nginx:1.27", "replicas": 2})
    status = await quota_store.quota_status("team-a")
    assert status["usage"]["replicas"] == 2
    # 释放出的额度立即可用：再创建 4 副本资源（2+4=6 恰好顶到上限）。
    await quota_store.create("team-a", "new", {"image": "redis:7", "replicas": 4})
    status = await quota_store.quota_status("team-a")
    log_case("调小后再创建", {"用量": status["usage"]},
             "调小释放的额度被正确回收复用，2+4=6 不超上限")
    assert status["usage"] == {"resources": 2, "replicas": 6}


async def test_delete_releases_and_is_idempotent(quota_store):
    await quota_store.create("team-a", "web", {"image": "nginx:1.27", "replicas": 4})
    await quota_store.delete("team-a", "web")
    status = await quota_store.quota_status("team-a")
    log_case("删除后用量", status["usage"],
             "删除被接受即释放：deleting 资源不再计入，用量归零")
    assert status["usage"] == {"resources": 0, "replicas": 0}
    # 重复删除：幂等返回，不重复释放（用量保持 0，不会变成负数/泄漏）。
    again = await quota_store.delete("team-a", "web")
    assert again.deleting is True
    status = await quota_store.quota_status("team-a")
    log_case("重复删除后用量", status["usage"], "重复删除幂等，额度不重复释放")
    assert status["usage"] == {"resources": 0, "replicas": 0}
    # 删除中资源不占用额度：释放的额度可被新资源使用。
    await quota_store.create("team-a", "other", {"image": "redis:7", "replicas": 6})
    status = await quota_store.quota_status("team-a")
    assert status["usage"] == {"resources": 1, "replicas": 6}


async def test_overquota_policy_and_recovery(quota_store):
    await quota_store.create("team-a", "a", {"image": "nginx:1.27", "replicas": 3})
    await quota_store.create("team-a", "b", {"image": "redis:7", "replicas": 3})
    # 把副本上限调到 4，低于当前用量 6 -> 进入超配状态。
    status = await quota_store.set_quota("team-a", max_resources=3, max_replicas=4)
    log_case("调低配额", {"新上限": 4, "当前用量": 6},
             "允许调低：既有资源不被删改、可读，状态标记 overQuota")
    assert status["overQuota"] is True
    assert status["usage"]["replicas"] == 6
    # 既有资源仍可读、未被改动。
    resource = await quota_store.get("team-a", "a")
    assert resource.spec["replicas"] == 3
    # 放大性写入被拒。
    with pytest.raises(QuotaExceededError):
        await quota_store.update("team-a", "a", {"image": "nginx:1.27", "replicas": 4})
    with pytest.raises(QuotaExceededError):
        await quota_store.create("team-a", "c", SPEC_R1)
    log_case("超配下放大写入", {}, "调大副本/新建均被 QuotaExceeded 拒绝")
    # 收缩性写入放行：调小副本。
    await quota_store.update("team-a", "a", {"image": "nginx:1.27", "replicas": 1})
    status = await quota_store.quota_status("team-a")
    assert status["usage"]["replicas"] == 4
    assert status["overQuota"] is False
    log_case("超配下收缩写入", {"用量": status["usage"]},
             "调小副本放行，用量回落到上限以内，超配解除")
    # 删除也放行，且释放额度。
    await quota_store.delete("team-a", "b")
    status = await quota_store.quota_status("team-a")
    assert status["usage"] == {"resources": 1, "replicas": 1}
    # 用量回落后恢复正常写入。
    await quota_store.create("team-a", "c", {"image": "redis:7", "replicas": 2})
    status = await quota_store.quota_status("team-a")
    log_case("回落后恢复", {"用量": status["usage"]}, "用量回到上限内后，新写入正常放行")
    assert status["usage"] == {"resources": 2, "replicas": 3}


async def test_default_quota_applies_without_override(store):
    log_case("默认配额", {}, "未覆盖的命名空间使用默认值：5 个资源 / 副本合计 20")
    status = await store.quota_status("team-a")
    assert status["limits"] == {"maxResources": 5, "maxReplicas": 20}
    assert status["usage"] == {"resources": 0, "replicas": 0}
    assert status["overQuota"] is False
    # 第 6 个资源被默认条数上限拒绝。
    for i in range(5):
        await store.create("team-a", f"d{i}", SPEC_R1)
    with pytest.raises(QuotaExceededError):
        await store.create("team-a", "d5", SPEC_R1)


async def test_quota_api_endpoints():
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        log_case("HTTP 配额端点", {"GET/PUT": "/quotas/team-a"},
                 "查询返回 limits+usage+overQuota；调整立即生效；超限写入返回 403 QuotaExceeded")
        resp = await client.get("/quotas/team-a")
        assert resp.status_code == 200
        assert resp.json() == {
            "namespace": "team-a",
            "limits": {"maxResources": 5, "maxReplicas": 20},
            "usage": {"resources": 0, "replicas": 0},
            "overQuota": False,
        }
        resp = await client.put("/quotas/team-a",
                                json={"maxResources": 1, "maxReplicas": 2})
        assert resp.status_code == 200
        assert resp.json()["limits"] == {"maxResources": 1, "maxReplicas": 2}

        resp = await client.post("/resources/team-a",
                                 json={"name": "web", "spec": SPEC_R1})
        assert resp.status_code == 201
        resp = await client.post("/resources/team-a",
                                 json={"name": "web2", "spec": SPEC_R1})
        assert resp.status_code == 403
        body = resp.json()
        log_case("HTTP 超限拒绝", body, "403 + reason=QuotaExceeded，details 可解释")
        assert body["reason"] == "QuotaExceeded"
        assert body["details"]["quota"] == "resources"

        resp = await client.get("/quotas/team-a")
        assert resp.json()["usage"] == {"resources": 1, "replicas": 1}
        # 非法配额值被拒绝。
        resp = await client.put("/quotas/team-a",
                                json={"maxResources": -1, "maxReplicas": 2})
        assert resp.status_code == 422
