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
