"""自定义资源的数据模型（纯类型定义）。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

#: 期望状态中允许出现的字段（除此之外一律拒绝）。
ALLOWED_SPEC_FIELDS: frozenset[str] = frozenset({"image", "replicas", "paused", "config"})

#: 创建后不可变更的字段。
IMMUTABLE_SPEC_FIELDS: frozenset[str] = frozenset({"image"})

#: 每个字段允许的值类型（config 允许任意 JSON 对象）。
SPEC_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "image": (str,),
    "replicas": (int,),
    "paused": (bool,),
    "config": (dict,),
}


@dataclass
class Resource:
    """一份自定义资源的完整记录（期望状态 + 实际状态 + 元数据）。"""

    namespace: str
    name: str
    spec: dict[str, Any]
    resource_version: int = 0
    deleting: bool = False
    applied: dict[str, Any] | None = None  # 实际状态：已生效的 spec 快照
    apply_count: int = 0                   # 副作用执行次数（用于验证幂等）
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.name}"

    def to_dict(self) -> dict:
        return {
            "namespace": self.namespace,
            "name": self.name,
            "spec": self.spec,
            "resourceVersion": self.resource_version,
            "deleting": self.deleting,
            "status": {
                "applied": self.applied,
                "applyCount": self.apply_count,
                "reconciled": self.applied == self.spec,
            },
        }
