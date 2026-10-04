"""控制面错误类型：每种失败都有可区分的 reason 与对应 HTTP 状态码。"""
from __future__ import annotations


class ControlPlaneError(Exception):
    """所有控制面错误的基类。"""

    reason: str = "InternalError"
    status_code: int = 500

    def __init__(self, message: str = "", *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"reason": self.reason, "message": self.message, "details": self.details}


class InvalidSpecError(ControlPlaneError):
    """期望状态字段非法（schema 校验失败）。"""

    reason = "InvalidSpec"
    status_code = 422


class ImmutableFieldError(ControlPlaneError):
    """更新试图变更不可变字段。"""

    reason = "ImmutableFieldChanged"
    status_code = 422


class AlreadyExistsError(ControlPlaneError):
    """资源标识冲突：同 namespace/name 的资源已存在。"""

    reason = "AlreadyExists"
    status_code = 409


class ForbiddenNamespaceError(ControlPlaneError):
    """越权命名空间操作。"""

    reason = "ForbiddenNamespace"
    status_code = 403


class NotFoundError(ControlPlaneError):
    """资源不存在。"""

    reason = "NotFound"
    status_code = 404


class ConflictError(ControlPlaneError):
    """基于过期 resource_version 的写入冲突。"""

    reason = "Conflict"
    status_code = 409


class DeletingError(ControlPlaneError):
    """资源正在删除中，拒绝更新。"""

    reason = "ResourceDeleting"
    status_code = 409


class QuotaExceededError(ControlPlaneError):
    """命名空间配额超限：写入会超出资源条数或副本合计上限。"""

    reason = "QuotaExceeded"
    status_code = 403
