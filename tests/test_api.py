"""HTTP API 测试：错误响应的 reason 与状态码可区分，正常流程端到端可走通。"""
import pytest
from httpx import ASGITransport, AsyncClient

from controlplane.api import create_app
from tests.conftest import log_case


@pytest.fixture
def client():
    app = create_app()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_full_lifecycle(client):
    log_case("HTTP lifecycle", {"namespace": "team-a", "name": "web"},
             "创建->调和->更新->删除 全链路，每步状态码与版本号符合预期")
    resp = await client.post("/resources/team-a",
                             json={"name": "web", "spec": {"image": "nginx:1.27", "replicas": 1}})
    assert resp.status_code == 201
    assert resp.json()["resourceVersion"] == 1

    resp = await client.post("/reconcile/team-a/web")
    assert resp.json()["status"]["reconciled"] is True
    assert resp.json()["status"]["applyCount"] == 1

    resp = await client.put("/resources/team-a/web",
                            json={"spec": {"image": "nginx:1.27", "replicas": 3},
                                  "resourceVersion": 1})
    assert resp.status_code == 200
    assert resp.json()["resourceVersion"] == 2

    resp = await client.delete("/resources/team-a/web")
    assert resp.status_code == 200
    assert resp.json()["deleting"] is True


async def test_error_reasons_are_distinguishable(client):
    await client.post("/resources/team-a",
                      json={"name": "web", "spec": {"image": "nginx:1.27", "replicas": 1}})
    cases = [
        ("post", "/resources/team-a",
         {"name": "web", "spec": {"image": "nginx:1.27"}}, 409, "AlreadyExists"),
        ("post", "/resources/team-x",
         {"name": "x", "spec": {"image": "nginx:1.27"}}, 403, "ForbiddenNamespace"),
        ("post", "/resources/team-a",
         {"name": "bad", "spec": {"image": "nginx", "bogus": 1}}, 422, "InvalidSpec"),
        ("put", "/resources/team-a/web",
         {"spec": {"image": "changed:1"}}, 422, "ImmutableFieldChanged"),
        ("put", "/resources/team-a/web",
         {"spec": {"image": "nginx:1.27"}, "resourceVersion": 99}, 409, "Conflict"),
        ("get", "/resources/team-a/ghost", None, 404, "NotFound"),
    ]
    for method, url, body, expected_status, expected_reason in cases:
        log_case(f"HTTP {method.upper()} {url}", body or {},
                 f"期望 status={expected_status} reason={expected_reason}")
        resp = await getattr(client, method)(url, json=body) if body else await getattr(client, method)(url)
        assert resp.status_code == expected_status
        assert resp.json()["reason"] == expected_reason


async def test_quota_enforced_over_http(client):
    log_case("HTTP quota", {"namespace": "team-a", "default": "5 resources / 20 replicas"},
             "副本合计到 20 后继续放大 -> 429 QuotaExceeded，details 可解释；容量接口返回上限/用量/超配")
    # 用 5 个资源把副本合计打到 20（4*5）。
    for i in range(5):
        resp = await client.post("/resources/team-a",
                                 json={"name": f"w{i}",
                                       "spec": {"image": "nginx:1.27", "replicas": 4}})
        assert resp.status_code == 201

    resp = await client.get("/quotas/team-a")
    assert resp.status_code == 200
    body = resp.json()
    assert body["limits"] == {"maxResources": 5, "maxReplicas": 20}
    assert body["usage"] == {"resourceCount": 5, "replicaTotal": 20}
    assert body["overQuota"] is False

    # 第 6 个资源：条数超限 -> 429。
    resp = await client.post("/resources/team-a",
                             json={"name": "w5", "spec": {"image": "nginx:1.27"}})
    assert resp.status_code == 429
    err = resp.json()
    assert err["reason"] == "QuotaExceeded"
    assert err["details"]["quotaType"] == "resources"
    assert err["details"]["used"] == 5 and err["details"]["limit"] == 5

    # 调小配额到 1/10 -> 立即超配，但只读与缩容仍可用。
    resp = await client.put("/quotas/team-a", json={"maxResources": 1, "maxReplicas": 10})
    assert resp.status_code == 200
    assert resp.json()["overQuota"] is True

    # 放大被拒（429），调小放行（200）。
    resp = await client.put("/resources/team-a/w0",
                            json={"spec": {"image": "nginx:1.27", "replicas": 5}})
    assert resp.status_code == 429
    assert resp.json()["details"]["quotaType"] == "replicas"
    resp = await client.put("/resources/team-a/w0",
                            json={"spec": {"image": "nginx:1.27", "replicas": 1}})
    assert resp.status_code == 200
