"""准入校验：对创建/更新请求做纯函数式判定，不产生任何状态变化。"""
from __future__ import annotations

from typing import Any

from .errors import ForbiddenNamespaceError, ImmutableFieldError, InvalidSpecError
from .models import ALLOWED_SPEC_FIELDS, IMMUTABLE_SPEC_FIELDS, SPEC_FIELD_TYPES, Resource


class AdmissionController:
    """准入控制器。

    所有方法都是纯校验：要么返回 None（放行），要么抛出
    ControlPlaneError 子类（拒绝，reason 可区分）。
    被拒绝的请求不触碰任何存储状态。
    """

    def __init__(self, allowed_namespaces: set[str] | None = None) -> None:
        # None 表示不限制命名空间；否则只允许集合内的命名空间。
        self.allowed_namespaces = allowed_namespaces

    def validate_namespace(self, namespace: str) -> None:
        """校验命名空间是否被授权。"""
        if self.allowed_namespaces is not None and namespace not in self.allowed_namespaces:
            raise ForbiddenNamespaceError(
                f"namespace {namespace!r} is not allowed",
                details={"namespace": namespace, "allowed": sorted(self.allowed_namespaces)},
            )

    def validate_spec(self, spec: dict[str, Any]) -> None:
        """校验期望状态的字段集合、类型与取值。"""
        if not isinstance(spec, dict):
            raise InvalidSpecError("spec must be an object", details={"got": type(spec).__name__})

        unknown = sorted(set(spec) - ALLOWED_SPEC_FIELDS)
        if unknown:
            raise InvalidSpecError(
                f"unknown spec fields: {unknown}",
                details={"unknownFields": unknown, "allowedFields": sorted(ALLOWED_SPEC_FIELDS)},
            )

        if "image" not in spec:
            raise InvalidSpecError("spec.image is required", details={"missing": ["image"]})

        for field_name, value in spec.items():
            allowed_types = SPEC_FIELD_TYPES[field_name]
            # bool 是 int 的子类，replicas 不允许传入 bool。
            if not isinstance(value, allowed_types) or (
                field_name == "replicas" and isinstance(value, bool)
            ):
                raise InvalidSpecError(
                    f"spec.{field_name} has wrong type",
                    details={
                        "field": field_name,
                        "expected": [t.__name__ for t in allowed_types],
                        "got": type(value).__name__,
                    },
                )

        replicas = spec.get("replicas")
        if replicas is not None and not (0 <= replicas <= 100):
            raise InvalidSpecError(
                "spec.replicas out of range",
                details={"field": "replicas", "min": 0, "max": 100, "got": replicas},
            )

        image = spec.get("image")
        if image is not None and not image.strip():
            raise InvalidSpecError("spec.image must not be empty", details={"field": "image"})

    def validate_create(self, namespace: str, name: str, spec: dict[str, Any]) -> None:
        """创建准入：命名空间 + 标识 + spec 校验。"""
        self.validate_namespace(namespace)
        if not name or "/" in name:
            raise InvalidSpecError(
                "invalid resource name", details={"name": name}
            )
        self.validate_spec(spec)

    def validate_update(self, existing: Resource, new_spec: dict[str, Any]) -> None:
        """更新准入：spec 校验 + 不可变字段检查。"""
        self.validate_spec(new_spec)
        changed = sorted(
            f for f in IMMUTABLE_SPEC_FIELDS
            if existing.spec.get(f) != new_spec.get(f)
        )
        if changed:
            raise ImmutableFieldError(
                f"immutable fields changed: {changed}",
                details={
                    "immutableFields": changed,
                    "current": {f: existing.spec.get(f) for f in changed},
                    "requested": {f: new_spec.get(f) for f in changed},
                },
            )
