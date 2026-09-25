"""Deletion and residual-cleanup semantics."""

from __future__ import annotations

import threading

from tests.conftest import log_request, thread_client


def _mk(client, ns, name, content="x"):
    return log_request(
        client,
        "post",
        f"/api/v1/namespaces/{ns}/widgets",
        json={"name": name, "spec": {"content": content}},
    )


def test_delete_removes_record_and_side_effect(client):
    _mk(client, "default", "w1")
    assert client.app.state.external.gadget_count() == 1
    r = log_request(client, "delete", "/api/v1/namespaces/default/widgets/w1")
    assert r.status_code == 200
    assert r.json()["allowed"] is True
    assert not client.app.state.store.exists("default", "w1")
    assert client.app.state.external.gadget_count() == 0
    assert not client.app.state.external.gadget_exists("gadget-default-w1")


def test_delete_nonexistent_is_stable_not_found(client):
    r = log_request(client, "delete", "/api/v1/namespaces/default/widgets/ghost")
    assert r.status_code == 404
    assert r.json()["reason"] == "not_found"


def test_duplicate_delete_after_completion_is_not_found(client):
    _mk(client, "default", "w1")
    first = log_request(client, "delete", "/api/v1/namespaces/default/widgets/w1")
    assert first.status_code == 200
    second = log_request(client, "delete", "/api/v1/namespaces/default/widgets/w1")
    # first delete fully completes; the identity no longer exists
    assert second.status_code == 404
    assert second.json()["reason"] == "not_found"


def test_update_during_active_deletion_is_stably_rejected(client, store):
    # Place the deletion tombstone (the resource is now in its deleting
    # window); every subsequent update must be rejected with a stable reason.
    _mk(client, "default", "w1")
    store.mark_deleting("default", "w1", expected_version=0)

    r = log_request(
        client,
        "put",
        "/api/v1/namespaces/default/widgets/w1",
        json={"expected_version": 0, "spec": {"content": "late"}},
    )
    assert r.status_code == 409
    assert r.json()["reason"] == "resource_is_deleting"

    # the late update changed nothing; cleanup can then proceed via DELETE
    got = client.get("/api/v1/namespaces/default/widgets/w1").json()
    assert got["spec"]["content"] == "x"
    assert got["deleting"] is True

    done = log_request(client, "delete", "/api/v1/namespaces/default/widgets/w1")
    assert done.status_code == 200
    assert not store.exists("default", "w1")
    assert client.app.state.external.gadget_count() == 0


def test_racing_update_and_delete_converge_to_stable_state(client, app):
    # When the two operations race without an ordering guarantee, exactly one
    # linearization wins; the final state must be identical either way:
    # delete succeeds, the resource and its gadget are gone, no corruption.
    _mk(client, "default", "w1")
    outcome: dict[str, int] = {}
    barrier = threading.Barrier(2)

    def do_delete() -> None:
        barrier.wait()
        r = log_request(
            thread_client(app), "delete", "/api/v1/namespaces/default/widgets/w1"
        )
        outcome["delete"] = r.status_code

    def do_update() -> None:
        barrier.wait()
        r = log_request(
            thread_client(app),
            "put",
            "/api/v1/namespaces/default/widgets/w1",
            json={"expected_version": 0, "spec": {"content": "late"}},
        )
        outcome["update"] = r.status_code
        outcome["update_reason"] = r.json().get("reason", "")

    t_del = threading.Thread(target=do_delete)
    t_upd = threading.Thread(target=do_update)
    t_del.start()
    t_upd.start()
    t_del.join()
    t_upd.join()

    assert outcome["delete"] == 200
    assert outcome["update"] in (200, 409, 404)
    if outcome["update"] != 200:
        assert outcome["update_reason"] in (
            "version_conflict",
            "resource_is_deleting",
            "not_found",
        )
    # whichever linearization won, the end state is the same
    assert not client.app.state.store.exists("default", "w1")
    assert client.app.state.external.gadget_count() == 0


def test_update_against_deleting_tombstone_rejected_at_store(store, reconciler):
    from app.errors import DeletingConflictError, VersionConflictError
    from app.models import WidgetSpec

    store.create("default", "w1", WidgetSpec(content="x"))
    store.mark_deleting("default", "w1", expected_version=0)
    try:
        store.update_spec("default", "w1", WidgetSpec(content="y"), 0)
        raise AssertionError("update should have failed")
    except (DeletingConflictError, VersionConflictError) as exc:
        assert exc.reason in ("resource_is_deleting", "version_conflict")
    # finalize twice converges cleanly
    reconciler.finalize_delete("default", "w1")
    reconciler.finalize_delete("default", "w1")
    assert not store.exists("default", "w1")


def test_cleanup_does_not_touch_other_resources(client):
    _mk(client, "default", "w1")
    _mk(client, "default", "w2")
    assert client.app.state.external.gadget_count() == 2
    log_request(client, "delete", "/api/v1/namespaces/default/widgets/w1")
    assert client.app.state.external.gadget_count() == 1
    assert client.app.state.external.gadget_exists("gadget-default-w2")
    remaining = client.get("/api/v1/namespaces/default/widgets").json()["items"]
    assert [w["name"] for w in remaining] == ["w2"]


def test_no_residual_state_misreconciled_after_delete(store, external, reconciler):
    from app.models import WidgetSpec

    store.create("default", "w1", WidgetSpec(content="x"))
    reconciler.reconcile_one("default", "w1")
    reconciler.finalize_delete("default", "w1")
    # recreating the same identity later starts from a clean slate
    w = store.create("default", "w1", WidgetSpec(content="fresh"))
    assert w.version == 0
    assert w.status.observed_version == 0
    assert w.status.gadget_id is None
    reconciler.reconcile_one("default", "w1")
    assert external.gadget_count() == 1
    assert external.gadget_content("gadget-default-w1") == "fresh"
