"""Concurrent writes, version conflicts and reconciliation idempotency.

These tests use real OS threads against the shared store/external system.
"""

from __future__ import annotations

import logging
import threading

import pytest

from app.models import WidgetSpec
from tests.conftest import log_request, thread_client

logger = logging.getLogger("controlplane.test")


def test_concurrent_updates_exactly_one_wins_no_lost_update(client, app):
    # baseline at version 0
    r = log_request(
        client,
        "post",
        "/api/v1/namespaces/default/widgets",
        json={"name": "w1", "spec": {"content": "v0"}},
    )
    assert r.status_code == 201

    results: list[int] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()  # maximize contention
        resp = log_request(
            thread_client(app),
            "put",
            "/api/v1/namespaces/default/widgets/w1",
            json={"expected_version": 0, "spec": {"content": f"by-{i}"}},
        )
        results.append(resp.status_code)
        logger.info(
            "worker %d input(content=by-%d, expected_version=0) -> http=%d",
            i,
            i,
            resp.status_code,
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [code for code in results if code == 200]
    losers = [code for code in results if code == 409]
    logger.info(
        "decision basis: winners=%d losers=%d (optimistic version check on version=0)",
        len(winners),
        len(losers),
    )
    assert len(winners) == 1
    assert len(losers) == 7

    final = client.get("/api/v1/namespaces/default/widgets/w1").json()
    assert final["version"] == 1
    # status reflects the single winning desired state, not a mix
    assert final["status"]["observed_version"] == 1
    assert final["spec"]["content"].startswith("by-")
    # exactly one side effect despite 8 reconciles
    assert client.app.state.external.gadget_count() == 1


def test_concurrent_creates_one_wins_rest_conflict(client, app):
    barrier = threading.Barrier(6)
    codes: list[int] = []

    def worker() -> None:
        barrier.wait()
        resp = log_request(
            thread_client(app),
            "post",
            "/api/v1/namespaces/default/widgets",
            json={"name": "dup", "spec": {"content": "x"}},
        )
        codes.append(resp.status_code)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(codes).count(201) == 1
    assert sorted(codes).count(409) == 5
    assert client.app.state.external.gadget_count() == 1


def test_concurrent_reconciles_no_duplicate_side_effects(store, external, reconciler):
    store.create("default", "w1", WidgetSpec(content="payload"))
    barrier = threading.Barrier(10)
    errors: list[Exception] = []

    def worker() -> None:
        barrier.wait()
        try:
            reconciler.reconcile_one("default", "w1")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert external.gadget_count() == 1
    w = store.get("default", "w1")
    assert w.status.gadget_id == "gadget-default-w1"
    assert w.status.observed_version == 0


def test_reconcile_after_concurrent_spec_change_does_not_lose_update(
    store, external, reconciler
):
    w = store.create("default", "w1", WidgetSpec(content="old"))
    reconciler.reconcile_one("default", "w1")  # observed_version 0

    # Simulate a reconciler that read v0, while a spec update lands at v1
    # before its status CAS. It must retry and converge on v1.
    snapshot = store.get("default", "w1")  # v0 read
    external.upsert_gadget("gadget-default-w1", snapshot.spec.content)
    store.update_spec("default", "w1", WidgetSpec(content="new"), expected_version=0)

    # The stale CAS must fail ...
    with pytest.raises(Exception) as exc_info:
        store.update_status("default", "w1", snapshot)
    assert exc_info.value.reason == "version_conflict"

    # ... and a fresh reconcile converges to v1 with no duplicate gadget.
    out = reconciler.reconcile_one("default", "w1")
    assert out.version == 1
    assert out.status.observed_version == 1
    assert external.gadget_content("gadget-default-w1") == "new"
    assert external.gadget_count() == 1
