"""HTTP surface.

Endpoints
---------
* ``POST /admission-widgets``            admission webhook (pure validation)
* ``POST /api/v1/namespaces/{ns}/widgets``           create (admitted)
* ``GET  /api/v1/namespaces/{ns}/widgets/{name}``    fetch
* ``GET  /api/v1/namespaces/{ns}/widgets``           list
* ``PUT  /api/v1/namespaces/{ns}/widgets/{name}``    optimistic update
* ``POST .../widgets/{name}/reconcile``              trigger reconciliation
* ``DELETE /api/v1/namespaces/{ns}/widgets/{name}``  delete + cleanup

Every state-changing request passes admission *before* any store call, so a
rejected request produces no persistent change.  Errors carry the stable
machine-readable ``reason`` code from :mod:`app.errors`.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.admission import admit
from app.errors import ControlPlaneError
from app.external import ExternalSystem
from app.models import (
    AdmissionRequest,
    WidgetRef,
    WidgetSpec,
)
from app.reconciler import Reconciler
from app.store import WidgetStore

logger = logging.getLogger("controlplane.api")


# --------------------------------------------------------------------------- #
# Wire schemas
# --------------------------------------------------------------------------- #
from pydantic import BaseModel


class RefIn(BaseModel):
    namespace: str
    name: str


class SpecIn(BaseModel):
    content: str = ""
    ref: Optional[RefIn] = None


class CreateBody(BaseModel):
    name: str
    spec: SpecIn


class UpdateBody(BaseModel):
    # Version the client last observed; mandatory for optimistic concurrency.
    expected_version: int
    spec: SpecIn


# --------------------------------------------------------------------------- #
def _to_spec(spec_in: SpecIn) -> WidgetSpec:
    return WidgetSpec(
        content=spec_in.content,
        ref=None
        if spec_in.ref is None
        else WidgetRef(namespace=spec_in.ref.namespace, name=spec_in.ref.name),
    )


def _widget_out(w) -> dict:
    return {
        "namespace": w.namespace,
        "name": w.name,
        "version": w.version,
        "deleting": w.deleting,
        "spec": {
            "content": w.spec.content,
            "ref": None
            if w.spec.ref is None
            else {"namespace": w.spec.ref.namespace, "name": w.spec.ref.name},
        },
        "status": {
            "phase": w.status.phase.value,
            "observed_version": w.status.observed_version,
            "gadget_id": w.status.gadget_id,
            "message": w.status.message,
        },
    }


def create_app(
    store: WidgetStore | None = None,
    external: ExternalSystem | None = None,
    reconciler: Reconciler | None = None,
) -> FastAPI:
    app = FastAPI(title="widget-control-plane")
    store = store or WidgetStore()
    external = external or ExternalSystem()
    reconciler = reconciler or Reconciler(store, external)
    app.state.store = store
    app.state.external = external
    app.state.reconciler = reconciler

    # ----- error handler: map domain errors to distinguishable JSON ------
    @app.exception_handler(ControlPlaneError)
    async def _on_domain_error(_request: Request, exc: ControlPlaneError) -> JSONResponse:
        logger.info(
            "request rejected: reason=%s message=%s", exc.reason, exc.message
        )
        return JSONResponse(
            status_code=exc.http_status,
            content={"allowed": False, "reason": exc.reason, "message": exc.message},
        )

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    # ------------------------------------------------------------------ #
    # Admission webhook: validates without persisting anything.
    # ------------------------------------------------------------------ #
    @app.post("/admission-widgets")
    async def admission_webhook(body: dict) -> dict:
        op = body.get("operation", "")
        namespace = body.get("namespace", "")
        name = body.get("name", "")
        raw_spec = body.get("spec")
        spec_in = None if raw_spec is None else SpecIn(**raw_spec)
        expected_version = body.get("expected_version")

        old = None
        if op in ("UPDATE", "DELETE"):
            try:
                old = store.get(namespace, name)
            except ControlPlaneError:
                old = None
        exists = store.exists(namespace, name)

        req = AdmissionRequest(
            operation=op,
            namespace=namespace,
            name=name,
            new_spec=None if spec_in is None else _to_spec(spec_in),
            old=old,
            exists=exists,
            expected_version=expected_version,
        )
        decision = admit(req)
        return {
            "allowed": decision.allowed,
            "reason": decision.reason,
            "message": decision.message,
        }

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #
    @app.post("/api/v1/namespaces/{namespace}/widgets", status_code=201)
    async def create_widget(namespace: str, body: CreateBody) -> dict:
        spec = _to_spec(body.spec)
        # Admission first; no state is touched until it allows.
        decision = admit(
            AdmissionRequest(
                operation="CREATE",
                namespace=namespace,
                name=body.name,
                new_spec=spec,
                exists=store.exists(namespace, body.name),
            )
        )
        if not decision.allowed:
            raise _error_for(decision)

        w = store.create(namespace, body.name, spec)
        # Newly created resources converge in the background; trigger once.
        w = reconciler.reconcile_one(namespace, body.name)
        return _widget_out(w)

    @app.get("/api/v1/namespaces/{namespace}/widgets")
    async def list_widgets(namespace: str) -> dict:
        return {"items": [_widget_out(w) for w in store.list(namespace)]}

    @app.get("/api/v1/namespaces/{namespace}/widgets/{name}")
    async def get_widget(namespace: str, name: str) -> dict:
        return _widget_out(store.get(namespace, name))

    @app.put("/api/v1/namespaces/{namespace}/widgets/{name}")
    async def update_widget(namespace: str, name: str, body: UpdateBody) -> dict:
        old = store.get(namespace, name)
        spec = _to_spec(body.spec)
        decision = admit(
            AdmissionRequest(
                operation="UPDATE",
                namespace=namespace,
                name=name,
                new_spec=spec,
                old=old,
                expected_version=body.expected_version,
            )
        )
        if not decision.allowed:
            raise _error_for(decision)

        updated = store.update_spec(namespace, name, spec, body.expected_version)
        updated = reconciler.reconcile_one(namespace, name)
        return _widget_out(updated)

    @app.post("/api/v1/namespaces/{namespace}/widgets/{name}/reconcile")
    async def trigger_reconcile(namespace: str, name: str) -> dict:
        w = reconciler.reconcile_one(namespace, name)
        return _widget_out(w)

    @app.delete("/api/v1/namespaces/{namespace}/widgets/{name}")
    async def delete_widget(namespace: str, name: str) -> dict:
        old = store.get(namespace, name)  # raises not_found if absent
        decision = admit(
            AdmissionRequest(
                operation="DELETE",
                namespace=namespace,
                name=name,
                new_spec=None,
                old=old,
            )
        )
        if not decision.allowed:
            raise _error_for(decision)

        # Mark tombstone (idempotent): concurrent updates are now rejected.
        store.mark_deleting(namespace, name, old.version)
        # Cleanup side effects then remove the record. Idempotent overall.
        reconciler.finalize_delete(namespace, name)
        return {"allowed": True, "reason": "", "message": f"{namespace}/{name} deleted"}

    return app


def _error_for(decision) -> ControlPlaneError:
    """Translate an admission denial into the matching domain exception."""
    from app import errors

    mapping = {
        cls.reason: cls
        for cls in (
            errors.InvalidFieldError,
            errors.ImmutableFieldError,
            errors.IdentifierConflictError,
            errors.CrossNamespaceRefError,
            errors.UnauthorizedNamespaceError,
            errors.VersionConflictError,
            errors.NotFoundError,
            errors.DeletingConflictError,
        )
    }
    exc_cls = mapping.get(decision.reason, errors.InvalidFieldError)
    return exc_cls(decision.message)
