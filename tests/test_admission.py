"""Admission validation tests.

Each rejected request must (a) carry a distinct, stable ``reason`` and
(b) leave zero persistent state behind.
"""

from __future__ import annotations

from tests.conftest import log_request


def _create(client, ns, name, content="hello", ref=None, expected_status=None):
    payload = {"name": name, "spec": {"content": content}}
    if ref is not None:
        payload["spec"]["ref"] = ref
    return log_request(client, "post", f"/api/v1/namespaces/{ns}/widgets", json=payload)


def test_create_ok_and_converges(client):
    r = _create(client, "default", "w1", "hello")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version"] == 0
    assert body["status"]["phase"] == "Active"
    assert body["status"]["gadget_id"] == "gadget-default-w1"


def test_create_rejects_unauthorized_namespace_and_persists_nothing(client):
    r = _create(client, "kube-system", "w1", "x")
    assert r.status_code == 403
    assert r.json()["reason"] == "namespace_not_allowed"
    listing = client.get("/api/v1/namespaces/kube-system/widgets").json()
    assert listing["items"] == []
    assert client.app.state.external.gadget_count() == 0


def test_create_rejects_empty_content(client):
    r = _create(client, "default", "w1", "   ")
    assert r.status_code == 422
    assert r.json()["reason"] == "invalid_field"
    assert client.get("/api/v1/namespaces/default/widgets").json()["items"] == []


def test_create_rejects_cross_namespace_reference(client):
    r = _create(
        client,
        "default",
        "w1",
        "x",
        ref={"namespace": "team-a", "name": "r1"},
    )
    assert r.status_code == 403
    assert r.json()["reason"] == "cross_namespace_ref_forbidden"
    # no side effect at all
    assert client.app.state.external.gadget_count() == 0


def test_create_identifier_conflict_is_distinguishable(client):
    assert _create(client, "default", "w1", "a").status_code == 201
    r = _create(client, "default", "w1", "b")
    assert r.status_code == 409
    assert r.json()["reason"] == "identifier_conflict"
    # original content unchanged
    got = client.get("/api/v1/namespaces/default/widgets/w1").json()
    assert got["spec"]["content"] == "a"


def test_update_rejects_stale_version_without_overwrite(client):
    _create(client, "default", "w1", "v1")
    # two clients both read version 0; first update wins
    r1 = log_request(
        client,
        "put",
        "/api/v1/namespaces/default/widgets/w1",
        json={"expected_version": 0, "spec": {"content": "v2"}},
    )
    assert r1.status_code == 200
    # second client writes against the now-stale version 0 -> conflict
    r2 = log_request(
        client,
        "put",
        "/api/v1/namespaces/default/widgets/w1",
        json={"expected_version": 0, "spec": {"content": "v3"}},
    )
    assert r2.status_code == 409
    assert r2.json()["reason"] == "version_conflict"
    got = client.get("/api/v1/namespaces/default/widgets/w1").json()
    assert got["spec"]["content"] == "v2"  # not silently overwritten by v3


def test_update_rejects_immutable_ref_change(client):
    _create(
        client, "default", "w1", "x", ref={"namespace": "default", "name": "r1"}
    )
    r = log_request(
        client,
        "put",
        "/api/v1/namespaces/default/widgets/w1",
        json={
            "expected_version": 0,
            "spec": {"content": "x", "ref": {"namespace": "default", "name": "r2"}},
        },
    )
    assert r.status_code == 422
    assert r.json()["reason"] == "immutable_field"


def test_admission_webhook_endpoint_is_pure(client):
    # invalid request through the webhook must not create anything
    r = log_request(
        client,
        "post",
        "/admission-widgets",
        json={
            "operation": "CREATE",
            "namespace": "default",
            "name": "w1",
            "spec": {"content": ""},
        },
    )
    assert r.json()["allowed"] is False
    assert r.json()["reason"] == "invalid_field"
    assert client.get("/api/v1/namespaces/default/widgets").json()["items"] == []


def test_webhook_update_and_delete_reviews(client):
    _create(client, "default", "w1", "v1")

    # stale update via webhook -> denied, state untouched
    r = log_request(
        client,
        "post",
        "/admission-widgets",
        json={
            "operation": "UPDATE",
            "namespace": "default",
            "name": "w1",
            "expected_version": 99,
            "spec": {"content": "v2"},
        },
    )
    assert r.json()["allowed"] is False
    assert r.json()["reason"] == "version_conflict"

    # delete review of existing object -> allowed, but nothing deleted
    r = log_request(
        client,
        "post",
        "/admission-widgets",
        json={"operation": "DELETE", "namespace": "default", "name": "w1"},
    )
    assert r.json()["allowed"] is True
    assert client.app.state.store.exists("default", "w1")

    # delete review of missing object -> distinct not_found
    r = log_request(
        client,
        "post",
        "/admission-widgets",
        json={"operation": "DELETE", "namespace": "default", "name": "ghost"},
    )
    assert r.json()["allowed"] is False
    assert r.json()["reason"] == "not_found"
