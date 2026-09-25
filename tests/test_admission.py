"""准入校验测试：每种拒绝原因可区分，且被拒绝的请求不产生任何状态变化。"""
import pytest

from controlplane.errors import (
    AlreadyExistsError,
    ForbiddenNamespaceError,
    ImmutableFieldError,
    InvalidSpecError,
    NotFoundError,
)
from tests.conftest import log_case

VALID_SPEC = {"image": "nginx:1.27", "replicas": 2}


async def test_create_valid(store):
    log_case("create", {"namespace": "team-a", "name": "web", "spec": VALID_SPEC},
             "spec 字段均在白名单内、类型合法、命名空间已授权 -> 放行")
    resource = await store.create("team-a", "web", VALID_SPEC)
    assert resource.resource_version == 1
    assert resource.spec["image"] == "nginx:1.27"


@pytest.mark.parametrize("spec,fragment", [
    ({"image": "nginx", "unknown": 1}, "unknown spec fields"),
    ({"replicas": 1}, "image is required"),
    ({"image": "nginx", "replicas": "two"}, "wrong type"),
    ({"image": "nginx", "replicas": True}, "wrong type"),
    ({"image": "nginx", "replicas": 101}, "out of range"),
    ({"image": "  "}, "must not be empty"),
])
async def test_create_invalid_spec_rejected(store, spec, fragment):
    log_case("create", {"namespace": "team-a", "name": "bad", "spec": spec},
             f"期望 InvalidSpec，消息含 {fragment!r}")
    with pytest.raises(InvalidSpecError) as exc_info:
        await store.create("team-a", "bad", spec)
    assert exc_info.value.reason == "InvalidSpec"
    assert fragment in exc_info.value.message
    # 被拒绝的请求不得产生持久状态。
    with pytest.raises(NotFoundError):
        await store.get("team-a", "bad")


async def test_create_forbidden_namespace(store):
    log_case("create", {"namespace": "team-x", "name": "web", "spec": VALID_SPEC},
             "team-x 不在授权命名空间集合 {team-a, team-b} 内 -> ForbiddenNamespace")
    with pytest.raises(ForbiddenNamespaceError) as exc_info:
        await store.create("team-x", "web", VALID_SPEC)
    assert exc_info.value.reason == "ForbiddenNamespace"
    assert exc_info.value.status_code == 403
    with pytest.raises(NotFoundError):
        await store.get("team-x", "web")


async def test_create_conflict_already_exists(store):
    await store.create("team-a", "web", VALID_SPEC)
    log_case("create", {"namespace": "team-a", "name": "web", "spec": VALID_SPEC},
             "同 namespace/name 已存在 -> AlreadyExists，且原资源不被覆盖")
    with pytest.raises(AlreadyExistsError) as exc_info:
        await store.create("team-a", "web", {"image": "other:1", "replicas": 5})
    assert exc_info.value.reason == "AlreadyExists"
    assert exc_info.value.status_code == 409
    existing = await store.get("team-a", "web")
    assert existing.spec == VALID_SPEC  # 状态未被覆盖
    assert existing.resource_version == 1


async def test_update_immutable_field_rejected(store):
    await store.create("team-a", "web", VALID_SPEC)
    new_spec = {"image": "nginx:1.28", "replicas": 3}
    log_case("update", {"name": "web", "spec": new_spec},
             "image 属于不可变字段，变更 -> ImmutableFieldChanged")
    with pytest.raises(ImmutableFieldError) as exc_info:
        await store.update("team-a", "web", new_spec)
    assert exc_info.value.reason == "ImmutableFieldChanged"
    assert exc_info.value.details["immutableFields"] == ["image"]
    existing = await store.get("team-a", "web")
    assert existing.spec == VALID_SPEC  # 拒绝后状态不变
    assert existing.resource_version == 1


async def test_update_mutable_field_allowed(store):
    await store.create("team-a", "web", VALID_SPEC)
    new_spec = {"image": "nginx:1.27", "replicas": 5, "paused": True}
    log_case("update", {"name": "web", "spec": new_spec},
             "replicas/paused 为可变字段且 image 未变 -> 放行，rv 递增")
    updated = await store.update("team-a", "web", new_spec)
    assert updated.spec == new_spec
    assert updated.resource_version == 2


async def test_update_not_found(store):
    log_case("update", {"name": "ghost", "spec": VALID_SPEC}, "资源不存在 -> NotFound")
    with pytest.raises(NotFoundError) as exc_info:
        await store.update("team-a", "ghost", VALID_SPEC)
    assert exc_info.value.reason == "NotFound"
