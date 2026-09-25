"""Shared test fixtures and a logging HTTP helper.

Every API call made through :func:`log_request` records the request input and
the server's decision (status/reason) at INFO level, satisfying the
"print inputs and decision basis in unit-test logs" requirement.
"""

from __future__ import annotations

import logging

import pytest

from app.api import create_app
from app.external import ExternalSystem
from app.reconciler import Reconciler
from app.store import WidgetStore

logger = logging.getLogger("controlplane.test")


@pytest.fixture
def store() -> WidgetStore:
    return WidgetStore()


@pytest.fixture
def external() -> ExternalSystem:
    return ExternalSystem()


@pytest.fixture
def reconciler(store: WidgetStore, external: ExternalSystem) -> Reconciler:
    return Reconciler(store, external)


@pytest.fixture
def app(store: WidgetStore, external: ExternalSystem, reconciler: Reconciler):
    return create_app(store=store, external=external, reconciler=reconciler)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


def thread_client(app):
    """A fresh TestClient for use inside a worker thread.

    TestClient wraps a per-instance anyio portal and must not be shared
    across threads; all such clients still talk to the same shared
    store/external components held on ``app.state``.
    """
    from fastapi.testclient import TestClient

    return TestClient(app)


def log_request(client, method: str, path: str, *, json=None, **kwargs):
    """Perform an HTTP call and log input + decision basis."""
    logger.info("TEST INPUT: %s %s payload=%s kwargs=%s", method, path, json, kwargs)
    resp = client.request(method.upper(), path, json=json, **kwargs)
    try:
        body = resp.json()
    except Exception:  # pragma: no cover
        body = resp.text
    logger.info(
        "TEST DECISION: %s %s -> http=%s body=%s", method, path, resp.status_code, body
    )
    return resp
