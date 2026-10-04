"""HTTP API 层：把控制面语义映射为 REST 端点与可区分的错误响应。"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .admission import AdmissionController
from .errors import ControlPlaneError, InvalidSpecError
from .store import ResourceStore


class CreateRequest(BaseModel):
    name: str
    spec: dict[str, Any]


class UpdateRequest(BaseModel):
    spec: dict[str, Any]
    resourceVersion: int | None = None  # 携带则启用乐观并发控制


class QuotaRequest(BaseModel):
    maxResources: int
    maxReplicas: int


def create_app(store: ResourceStore | None = None) -> FastAPI:
    """应用工厂：默认装配一个只允许 team-a/team-b 命名空间的控制面。"""
    if store is None:
        store = ResourceStore(AdmissionController(allowed_namespaces={"team-a", "team-b"}))

    app = FastAPI(title="declarative-control-plane")
    app.state.store = store

    @app.exception_handler(ControlPlaneError)
    async def control_plane_error_handler(request: Request, exc: ControlPlaneError):
        # 统一的错误结构：reason 可区分，message/details 可解释。
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.post("/resources/{namespace}", status_code=201)
    async def create_resource(namespace: str, body: CreateRequest):
        resource = await store.create(namespace, body.name, body.spec)
        return resource.to_dict()

    @app.put("/resources/{namespace}/{name}")
    async def update_resource(namespace: str, name: str, body: UpdateRequest):
        resource = await store.update(namespace, name, body.spec, body.resourceVersion)
        return resource.to_dict()

    @app.delete("/resources/{namespace}/{name}")
    async def delete_resource(namespace: str, name: str):
        resource = await store.delete(namespace, name)
        return resource.to_dict()

    @app.get("/resources/{namespace}/{name}")
    async def get_resource(namespace: str, name: str):
        resource = await store.get(namespace, name)
        return resource.to_dict()

    @app.get("/resources")
    async def list_resources():
        return {"items": [r.to_dict() for r in await store.list()]}

    @app.post("/reconcile")
    async def reconcile_all():
        reconciled = await store.reconcile_all()
        return {"items": [r.to_dict() for r in reconciled]}

    @app.post("/reconcile/{namespace}/{name}")
    async def reconcile_one(namespace: str, name: str):
        resource = await store.reconcile(namespace, name)
        return resource.to_dict()

    @app.get("/quotas/{namespace}")
    async def get_quota(namespace: str):
        return await store.quota_status(namespace)

    @app.put("/quotas/{namespace}")
    async def put_quota(namespace: str, body: QuotaRequest):
        if body.maxResources < 0 or body.maxReplicas < 0:
            raise InvalidSpecError(
                "quota limits must be non-negative",
                details={"maxResources": body.maxResources, "maxReplicas": body.maxReplicas},
            )
        return await store.set_quota(namespace, body.maxResources, body.maxReplicas)

    return app
