"""命名空间级配额测试：服务端强制的写入门槛。

覆盖：默认上限、两类配额超限拒绝且不留状态、并发不超卖、副本增量结算、
删除释放且不重复释放、配额下调后的超配兼容与恢复、按命名空间覆盖、容量视图。
每个用例通过 log_case 打印请求输入与判定依据。
"""
import asyncio

import pytest

from controlplane.admission import AdmissionController
from controlplane.errors import (
    InvalidSpecError,
    QuotaExceededError,
)
from controlplane.quota import Quota, QuotaManager
from controlplane.store import ResourceStore
from tests.conftest import log_case


@pytest.fixture
def tight_store() -> ResourceStore:
    """自定义配额的存储：每命名空间最多 2 个资源、副本合计 10。"""
    quota = QuotaManager(overrides={"team-a": Quota(2, 10)})
    return ResourceStore(
        AdmissionController(allowed_namespaces={"team-a", "team-b"}),
        quota=quota,
    )


async def test_default_quota_limits_are_five_and_twenty(store):
    cap = await store.capacity("team-a")
    log_case("capacity", {"namespace": "team-a"},
             "未单独配置时取默认上限 maxResources=5/maxReplicas=20，用量为 0、未超配")
    assert cap["limits"] == {"maxResources": 5, "maxReplicas": 20}
    assert cap["usage"] == {"resourceCount": 0, "replicaTotal": 0}
    assert cap["overQuota"] is False


async def test_resource_count_quota_exceeded_rejected(tight_store):
    await tight_store.create("team-a", "r1", {"image": "img:1", "replicas": 1})
    await tight_store.create("team-a", "r2", {"image": "img:2", "replicas": 1})
    log_case("create r3", {"namespace": "team-a", "currentCount": 2, "limit": 2},
             "已有 2 个资源（上限 2），第 3 个创建必须 QuotaExceeded(resources)")
    with pytest.raises(QuotaExceededError) as exc_info:
        await tight_store.create("team-a", "r3", {"image": "img:3", "replicas": 1})
    err = exc_info.value
    assert err.reason == "QuotaExceeded"
    assert err.status_code == 429
    assert err.details["quotaType"] == "resources"
    assert err.details["used"] == 2
    assert err.details["limit"] == 2
    assert err.details["delta"] == 1
    assert err.details["requested"] == 3
    # 被拒绝的创建不留半成品状态、不占额度。
    from controlplane.errors import NotFoundError
    with pytest.raises(NotFoundError):
        await tight_store.get("team-a", "r3")
    cap = await tight_store.capacity("team-a")
    assert cap["usage"]["resourceCount"] == 2


async def test_replica_quota_exceeded_on_create_rejected(tight_store):
    await tight_store.create("team-a", "big", {"image": "img:1", "replicas": 8})
    log_case("create small", {"namespace": "team-a", "usedReplicas": 8,
                              "requested": 3, "limit": 10},
             "8+3=11 > 10 -> QuotaExceeded(replicas)，即使条数上限（2）还没到")
    with pytest.raises(QuotaExceededError) as exc_info:
        await tight_store.create("team-a", "small", {"image": "img:2", "replicas": 3})
    err = exc_info.value
    assert err.details["quotaType"] == "replicas"
    assert err.details["used"] == 8
    assert err.details["limit"] == 10
    assert err.details["delta"] == 3
    assert err.details["requested"] == 11
    from controlplane.errors import NotFoundError
    with pytest.raises(NotFoundError):
        await tight_store.get("team-a", "small")


async def test_quota_error_distinct_from_invalid_spec_and_conflict(tight_store):
    await tight_store.create("team-a", "r1", {"image": "img:1", "replicas": 1})
    await tight_store.create("team-a", "r2", {"image": "img:2", "replicas": 1})
    log_case("reject kinds", {},
             "字段非法=InvalidSpec(422)，配额超限=QuotaExceeded(429)，reason 与状态码均可区分")
    with pytest.raises(InvalidSpecError):
        await tight_store.create("team-a", "r3", {"replicas": 1})  # 缺 image
    with pytest.raises(QuotaExceededError):
        await tight_store.create("team-a", "r3", {"image": "img:3", "replicas": 1})
    # 字段非法的请求同样不得占位（同名请求随后仍是配额错误而非 AlreadyExists）。
    with pytest.raises(QuotaExceededError):
        await tight_store.create("team-a", "r3", {"image": "img:3", "replicas": 1})


async def test_concurrent_creates_do_not_oversell(tight_store):
    log_case("concurrent create", {"namespace": "team-a", "并发数": 8,
                                   "limit": "2 resources"},
             "锁内串行判定：恰好 2 个成功，其余 QuotaExceeded；实际条数绝不超过 2")
    results = await asyncio.gather(
        *(tight_store.create("team-a", f"r{i}", {"image": f"img:{i}", "replicas": 1})
          for i in range(8)),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    quota_errors = [r for r in results if isinstance(r, QuotaExceededError)]
    assert len(successes) == 2
    assert len(quota_errors) == 6
    cap = await tight_store.capacity("team-a")
    assert cap["usage"]["resourceCount"] == 2  # 无超卖
    assert cap["usage"]["replicaTotal"] == 2


async def test_concurrent_replica_scale_ups_do_not_oversell(tight_store):
    await tight_store.create("team-a", "svc", {"image": "img:svc", "replicas": 8})
    log_case("concurrent scale-up", {"namespace": "team-a", "writer 数": 6,
                                     "usedReplicas": 8, "limit": 10,
                                     "每个 writer": "读最新版本后把 replicas +1"},
             "6 个 writer 竞争放大：额度只够 2 次 +1，恰好 2 个成功、其余 QuotaExceeded")

    from controlplane.errors import ConflictError

    async def bump_once() -> str:
        while True:
            current = await tight_store.get("team-a", "svc")
            try:
                await tight_store.update(
                    "team-a", "svc",
                    {"image": "img:svc", "replicas": current.spec["replicas"] + 1},
                    resource_version=current.resource_version,
                )
                return "ok"
            except ConflictError:
                continue  # 版本过期：重读最新值后重试
            except QuotaExceededError:
                return "quota"  # 放大被配额拒绝：退出

    results = await asyncio.gather(*(bump_once() for _ in range(6)))
    successes = [r for r in results if r == "ok"]
    quota_errors = [r for r in results if r == "quota"]
    log_case("concurrent scale-up 结果", {"成功": len(successes),
                                         "QuotaExceeded": len(quota_errors)},
             "8 -> 10 恰好放行了 2 次 +1，剩余 4 次被拒，实际合计=10，未超卖")
    assert len(successes) == 2
    assert len(quota_errors) == 4
    cap = await tight_store.capacity("team-a")
    assert cap["usage"]["replicaTotal"] == 10
    assert cap["usage"]["replicaTotal"] <= cap["limits"]["maxReplicas"]


async def test_scale_down_releases_replica_quota_by_delta(tight_store):
    await tight_store.create("team-a", "a", {"image": "img:a", "replicas": 6})
    await tight_store.create("team-a", "b", {"image": "img:b", "replicas": 4})
    # 当前副本合计 10，已满。此时把 a 从 6 调到 2（增量 -4），应立即释放 4 个额度。
    log_case("scale down", {"resource": "a", "6->2": "delta=-4", "usedBefore": 10},
             "调小副本按负增量结算：用量降到 6，释放出的额度可被新的放大写入使用")
    scaled = await tight_store.update("team-a", "a", {"image": "img:a", "replicas": 2})
    assert scaled.spec["replicas"] == 2
    cap = await tight_store.capacity("team-a")
    assert cap["usage"]["replicaTotal"] == 6
    # 释放后，把 b 从 4 调到 8（+2）应当放行（6+2=8 <= 10）。
    await tight_store.update("team-a", "b", {"image": "img:b", "replicas": 8})
    cap = await tight_store.capacity("team-a")
    assert cap["usage"]["replicaTotal"] == 10


async def test_delete_releases_quota(tight_store):
    await tight_store.create("team-a", "r1", {"image": "img:1", "replicas": 7})
    await tight_store.create("team-a", "r2", {"image": "img:2", "replicas": 3})
    log_case("delete r1", {"before": "count=2,repl=10"},
             "删除一旦接受：条数 -1、副本 -7，额度立即可用")
    await tight_store.delete("team-a", "r1")
    cap = await tight_store.capacity("team-a")
    assert cap["usage"] == {"resourceCount": 1, "replicaTotal": 3}
    # 释放后可以再创建一个占 7 副本的资源。
    await tight_store.create("team-a", "r3", {"image": "img:3", "replicas": 7})
    cap = await tight_store.capacity("team-a")
    assert cap["usage"] == {"resourceCount": 2, "replicaTotal": 10}


async def test_repeated_delete_does_not_double_release(tight_store):
    await tight_store.create("team-a", "r1", {"image": "img:1", "replicas": 7})
    first = await tight_store.delete("team-a", "r1")
    second = await tight_store.delete("team-a", "r1")  # 幂等，不重复释放
    log_case("delete x2", {"resource": "r1", "replicas": 7},
             "重复删除幂等：deleting 资源已脱离用量集合，第二次删除不再释放任何额度")
    assert second.resource_version == first.resource_version
    cap = await tight_store.capacity("team-a")
    assert cap["usage"] == {"resourceCount": 0, "replicaTotal": 0}
    # 删除中的资源不能被后续核算再次计入：新资源可完整使用被释放的额度。
    await tight_store.create("team-a", "r2", {"image": "img:2", "replicas": 10})
    cap = await tight_store.capacity("team-a")
    assert cap["usage"] == {"resourceCount": 1, "replicaTotal": 10}


async def test_deleting_resource_excluded_from_quota_check(tight_store):
    await tight_store.create("team-a", "a", {"image": "img:a", "replicas": 5})
    await tight_store.delete("team-a", "a")
    log_case("create after delete", {"deleted": "a(replicas=5)", "limit": "count=2,repl=10"},
             "删除中的 a 不计入用量，b 按全新额度核算，5<=10 放行")
    await tight_store.create("team-a", "b", {"image": "img:b", "replicas": 5})
    cap = await tight_store.capacity("team-a")
    assert cap["usage"] == {"resourceCount": 1, "replicaTotal": 5}


async def test_lower_quota_below_usage_blocks_growth_allows_shrink(tight_store):
    await tight_store.create("team-a", "a", {"image": "img:a", "replicas": 4})
    await tight_store.create("team-a", "b", {"image": "img:b", "replicas": 4})
    # 当前 count=2 / replicas=8；把配额下调到 1 个资源、6 副本（立即处于超配）。
    await tight_store.set_quota("team-a", Quota(1, 6))
    cap = await tight_store.capacity("team-a")
    log_case("quota lowered", {"usage": cap["usage"], "limits": cap["limits"]},
             "用量 2/8 高于新上限 1/6 -> overQuota=True；既有资源不删除、仍可读")
    assert cap["overQuota"] is True
    # 既有资源依然可读、未被改动。
    assert (await tight_store.get("team-a", "a")).spec["replicas"] == 4
    assert (await tight_store.get("team-a", "b")).spec["replicas"] == 4
    # 任何让用量变大的写入都被拒：新建（条数与副本都增）。
    log_case("create while over-quota", {}, "超配下创建必然增大用量 -> QuotaExceeded(resources)")
    with pytest.raises(QuotaExceededError) as exc_info:
        await tight_store.create("team-a", "c", {"image": "img:c", "replicas": 1})
    assert exc_info.value.details["quotaType"] == "resources"
    # 调大副本也被拒（副本类超限），状态不变。
    log_case("scale-up while over-quota", {"a": "4->5"},
             "超配下 +1 副本属于放大型写入 -> QuotaExceeded(replicas)")
    with pytest.raises(QuotaExceededError) as exc_info:
        await tight_store.update("team-a", "a", {"image": "img:a", "replicas": 5})
    assert exc_info.value.details["quotaType"] == "replicas"
    assert (await tight_store.get("team-a", "a")).spec["replicas"] == 4
    # 让用量变小的写入放行：调小副本。
    log_case("scale-down while over-quota", {"a": "4->2"},
             "调小副本是缩小型写入 -> 放行，用量随之回落")
    await tight_store.update("team-a", "a", {"image": "img:a", "replicas": 2})
    cap = await tight_store.capacity("team-a")
    assert cap["usage"]["replicaTotal"] == 6
    assert cap["overQuota"] is True  # 条数 2 仍 > 1，仍超配
    # 删除一个资源：条数与副本继续回落，直到回到上限以内。
    await tight_store.delete("team-a", "b")
    cap = await tight_store.capacity("team-a")
    log_case("recovery", {"usage": cap["usage"], "limits": cap["limits"]},
             "删除 b 后 count=1/repl=2，均 <= 上限 -> overQuota=False，放大写入恢复")
    assert cap["usage"] == {"resourceCount": 1, "replicaTotal": 2}
    assert cap["overQuota"] is False
    # 恢复后放大写入重新可用：2 -> 6（+4，正好到上限）。
    await tight_store.update("team-a", "a", {"image": "img:a", "replicas": 6})
    assert (await tight_store.get("team-a", "a")).spec["replicas"] == 6
    with pytest.raises(QuotaExceededError):
        await tight_store.update("team-a", "a", {"image": "img:a", "replicas": 7})


async def test_quota_override_is_per_namespace(store):
    await store.set_quota("team-a", Quota(1, 3))
    log_case("override", {"team-a": "1 resources / 3 replicas",
                          "team-b": "default 5/20"},
             "覆盖只作用于 team-a；team-b 仍使用默认配额，互不影响")
    await store.create("team-a", "a1", {"image": "img:a", "replicas": 3})
    with pytest.raises(QuotaExceededError):
        await store.create("team-a", "a2", {"image": "img:a", "replicas": 1})
    # team-b 不受 team-a 低配额影响：默认 5/20。
    for i in range(5):
        await store.create("team-b", f"b{i}", {"image": "img:b", "replicas": 4})
    cap_a = await store.capacity("team-a")
    cap_b = await store.capacity("team-b")
    assert cap_a["limits"] == {"maxResources": 1, "maxReplicas": 3}
    assert cap_b["limits"] == {"maxResources": 5, "maxReplicas": 20}
    assert cap_b["usage"] == {"resourceCount": 5, "replicaTotal": 20}
    with pytest.raises(QuotaExceededError) as exc_info:
        await store.create("team-b", "b5", {"image": "img:b", "replicas": 1})
    assert exc_info.value.details["quotaType"] == "resources"


async def test_usage_recomputation_converges_without_leak(tight_store):
    """重复多次建/改/删后，扫描式用量与逐条额度变化始终一致，不存在越用越少的泄漏。"""
    await tight_store.create("team-a", "a", {"image": "img:a", "replicas": 6})
    await tight_store.create("team-a", "b", {"image": "img:b", "replicas": 3})
    await tight_store.update("team-a", "a", {"image": "img:a", "replicas": 4})  # -2
    await tight_store.update("team-a", "b", {"image": "img:b", "replicas": 6})  # +3 -> 10
    await tight_store.delete("team-a", "a")                                    # -4,count-1
    await tight_store.delete("team-a", "a")                                    # 幂等
    await tight_store.create("team-a", "c", {"image": "img:c", "replicas": 2})  # +2 -> 8
    log_case("mixed ops", {"序列": "create6/create3/down4/up6/delete/delete/create2"},
             "扫描式核算每步都基于真实集合：最终 count=2(b,c) repl=8")
    cap = await tight_store.capacity("team-a")
    assert cap["usage"] == {"resourceCount": 2, "replicaTotal": 8}
    # 反复查询用量（不写入）结论必须完全一致。
    again = await tight_store.capacity("team-a")
    assert again["usage"] == cap["usage"]
