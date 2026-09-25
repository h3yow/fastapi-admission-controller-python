"""Application-level error types with stable, distinguishable reasons."""


class ControlPlaneError(Exception):
    """Base class for all control-plane errors.

    ``reason`` is a stable machine-readable code that callers/tests can match.
    ``http_status`` is the corresponding HTTP status for the REST surface.
    """

    reason: str = "internal_error"
    http_status: int = 500

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.reason)
        self.message = message or self.reason


class InvalidFieldError(ControlPlaneError):
    reason = "invalid_field"
    http_status = 422


class ImmutableFieldError(ControlPlaneError):
    reason = "immutable_field"
    http_status = 422


class IdentifierConflictError(ControlPlaneError):
    reason = "identifier_conflict"
    http_status = 409


class CrossNamespaceRefError(ControlPlaneError):
    reason = "cross_namespace_ref_forbidden"
    http_status = 403


class UnauthorizedNamespaceError(ControlPlaneError):
    reason = "namespace_not_allowed"
    http_status = 403


class VersionConflictError(ControlPlaneError):
    reason = "version_conflict"
    http_status = 409


class NotFoundError(ControlPlaneError):
    reason = "not_found"
    http_status = 404


class DeletingConflictError(ControlPlaneError):
    reason = "resource_is_deleting"
    http_status = 409
