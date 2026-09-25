"""Reconciliation idempotency and convergence semantics."""

from __future__ import annotations

from app.models import WidgetSpec
from tests.conftest import log_request


def test_repeated_reconcile_converges_without_duplicate_side_effects(
    store, external, reconciler
):
    store.create("default", "w1", WidgetSpec(content="payload"))
    first = reconciler.reconcile_one("default", "w1")
    for _ in range(5):
        again = reconciler.reconcile_one("default", "w1")
        assert again.status.observed_version == 0
        assert again.status.gadget_id == first.status.gadget_id
    assert external.gadget_count() == 1
    assert external.gadget_content("gadget-default-w1") == "payload"


def test_spec_change_reconciles_and_keeps_single_gadget(client):
    r = log_request(
        client,
        "post",
        "/api/v1/namespaces/default/widgets",
        json={"name": "w1", "spec": {"content": "v1"}},
    )
    assert r.status_code == 201

    r = log_request(
        client,
        "put",
        "/api/v1/namespaces/default/widgets/w1",
        json={"expected_version": 0, "spec": {"content": "v2"}},
    )
    assert r.status_code == 200
    assert r.json()["version"] == 1
    assert r.json()["status"]["observed_version"] == 1

    # explicit repeated trigger is a no-op
    for _ in range(3):
        again = log_request(
            client, "post", "/api/v1/namespaces/default/widgets/w1/reconcile"
        )
        assert again.json()["status"]["observed_version"] == 1

    ext = client.app.state.external
    assert ext.gadget_count() == 1
    assert ext.gadget_content("gadget-default-w1") == "v2"


def test_reconcile_is_isolated_per_resource(store, external, reconciler):
    store.create("default", "a", WidgetSpec(content="A"))
    store.create("team-a", "b", WidgetSpec(content="B"))
    reconciler.reconcile_one("default", "a")
    reconciler.reconcile_one("team-a", "b")
    reconciler.reconcile_one("default", "a")
    assert external.gadget_count() == 2
    assert external.gadget_content("gadget-default-a") == "A"
    assert external.gadget_content("gadget-team-a-b") == "B"


def test_deleting_resource_is_not_reconciled(store, external, reconciler):
    store.create("default", "w1", WidgetSpec(content="x"))
    store.mark_deleting("default", "w1", expected_version=0)
    snapshot = reconciler.reconcile_one("default", "w1")
    assert snapshot.deleting is True
    # no gadget gets created for a deleting resource
    assert external.gadget_count() == 0
