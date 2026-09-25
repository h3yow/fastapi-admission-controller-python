"""Admission control: validating CREATE/UPDATE/DELETE requests.

This module is *pure validation*: it never reads or writes the store and
therefore a rejected request cannot change persistent state (the HTTP layer
applies state changes only after admission returns ``allowed=True``).

Rejection reasons are stable, distinct codes so callers can tell *why* a
request was refused:

============================== ============================================
reason                         meaning
============================== ============================================
namespace_not_allowed          target/reference namespace not permitted
invalid_field                  malformed/illegal field value
immutable_field                change to a field frozen after creation
identifier_conflict            CREATE for an identity that already exists
resource_is_deleting           UPDATE against a deletion tombstone
version_conflict               write based on an outdated version
============================== ============================================
"""

from __future__ import annotations

import logging
from typing import Any

from app.models import (
    ALLOWED_NAMESPACES,
    AdmissionRequest,
    AdmissionResponse,
)

logger = logging.getLogger("controlplane.admission")


def _deny(reason: str, message: str, req: AdmissionRequest) -> AdmissionResponse:
    logger.info(
        "admission decision: ALLOW=False op=%s target=%s/%s reason=%s | input=%s",
        req.operation,
        req.namespace,
        req.name,
        reason,
        _summarize(req),
    )
    return AdmissionResponse(allowed=False, reason=reason, message=message)


def _allow(req: AdmissionRequest, note: str) -> AdmissionResponse:
    logger.info(
        "admission decision: ALLOW=True op=%s target=%s/%s basis=%s | input=%s",
        req.operation,
        req.namespace,
        req.name,
        note,
        _summarize(req),
    )
    return AdmissionResponse(allowed=True, reason="", message=note)


def _summarize(req: AdmissionRequest) -> dict[str, Any]:
    """A small, stable view of the request used in log output."""
    ref = None
    if req.new_spec is not None and req.new_spec.ref is not None:
        ref = f"{req.new_spec.ref.namespace}/{req.new_spec.ref.name}"
    return {
        "op": req.operation,
        "namespace": req.namespace,
        "name": req.name,
        "content_len": 0
        if req.new_spec is None or req.new_spec.content is None
        else len(req.new_spec.content),
        "ref": ref,
        "exists": req.exists,
        "expected_version": req.expected_version,
        "old_version": None if req.old is None else req.old.version,
        "deleting": False if req.old is None else req.old.deleting,
    }


def _check_target_namespace(req: AdmissionRequest) -> AdmissionResponse | None:
    if req.namespace not in ALLOWED_NAMESPACES:
        return _deny(
            "namespace_not_allowed",
            f"namespace {req.namespace!r} is not in allowed namespaces "
            f"{sorted(ALLOWED_NAMESPACES)}",
            req,
        )
    return None


def _check_identity(req: AdmissionRequest) -> AdmissionResponse | None:
    if not req.namespace or not req.name:
        return _deny(
            "invalid_field", "namespace and name must be non-empty", req
        )
    return None


def _check_spec_fields(req: AdmissionRequest) -> AdmissionResponse | None:
    spec = req.new_spec
    if spec is None:
        return _deny("invalid_field", "spec is required", req)
    if not isinstance(spec.content, str) or not spec.content.strip():
        return _deny("invalid_field", "spec.content must be a non-empty string", req)
    if spec.ref is not None:
        if not spec.ref.namespace or not spec.ref.name:
            return _deny(
                "invalid_field", "spec.ref must have namespace and name", req
            )
        if spec.ref.namespace != req.namespace:
            return _deny(
                "cross_namespace_ref_forbidden",
                f"reference to {spec.ref.namespace}/{spec.ref.name} crosses "
                f"namespace boundary from {req.namespace}",
                req,
            )
        # Cannot (meaningfully) reference oneself.
        if spec.ref.name == req.name and spec.ref.namespace == req.namespace:
            return _deny("invalid_field", "a widget may not reference itself", req)
    return None


def admit(req: AdmissionRequest) -> AdmissionResponse:
    """Validate an admission request; never mutates persistent state."""
    # Identity is checked first so even malformed requests get a stable reason.
    denied = _check_identity(req)
    if denied is not None:
        return denied
    denied = _check_target_namespace(req)
    if denied is not None:
        return denied

    if req.operation == "CREATE":
        if req.exists:
            return _deny(
                "identifier_conflict",
                f"widget {req.namespace}/{req.name} already exists",
                req,
            )
        denied = _check_spec_fields(req)
        if denied is not None:
            return denied
        return _allow(req, "create passed all validators")

    if req.operation == "UPDATE":
        if req.old is None:
            return _deny(
                "not_found",
                f"widget {req.namespace}/{req.name} not found",
                req,
            )
        if req.old.deleting:
            return _deny(
                "resource_is_deleting",
                f"widget {req.namespace}/{req.name} is being deleted; updates rejected",
                req,
            )
        if req.expected_version is None or req.expected_version != req.old.version:
            cur = None if req.old is None else req.old.version
            return _deny(
                "version_conflict",
                f"expected_version {req.expected_version} does not match "
                f"current version {cur}",
                req,
            )
        denied = _check_spec_fields(req)
        if denied is not None:
            return denied
        # spec.ref is frozen at creation: binding identity must not move.
        old_ref = req.old.spec.ref
        new_ref = req.new_spec.ref if req.new_spec is not None else None
        old_key = None if old_ref is None else (old_ref.namespace, old_ref.name)
        new_key = None if new_ref is None else (new_ref.namespace, new_ref.name)
        if old_key != new_key:
            return _deny(
                "immutable_field",
                f"spec.ref is immutable after creation: {old_key} -> {new_key}",
                req,
            )
        return _allow(req, "update passed all validators; ref unchanged")

    if req.operation == "DELETE":
        if req.old is None:
            return _deny(
                "not_found",
                f"widget {req.namespace}/{req.name} not found",
                req,
            )
        return _allow(req, "delete accepted; cleanup will run")

    return _deny("invalid_field", f"unknown operation {req.operation!r}", req)
