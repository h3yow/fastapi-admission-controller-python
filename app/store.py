"""Versioned, thread-safe in-memory resource store.

Concurrency model
-----------------
* Every object carries an opaque, monotonically increasing ``version``.
* ``create`` rejects duplicate identities (``identifier_conflict``).
* ``update_spec`` / ``mark_deleting`` require the caller's ``expected_version``
  to equal the stored version; a mismatch fails with ``version_conflict`` and
  changes nothing (optimistic concurrency control, no silent overwrite).
* ``update_status`` is a compare-and-swap used by the reconciler so that a
  status write based on a stale read loses to a concurrent spec update and is
  retried instead of clobbering it ("lost update" prevention).
* All returned objects are independent copies; callers never mutate storage
  outside the store lock.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Optional

from app.models import Widget, WidgetSpec
from app.errors import (
    DeletingConflictError,
    IdentifierConflictError,
    NotFoundError,
    VersionConflictError,
)


def _clone(w: Widget) -> Widget:
    """Return a deep-ish independent copy of a widget."""
    spec = replace(w.spec)
    if w.spec.ref is not None:
        spec.ref = replace(w.spec.ref)
    status = replace(w.status)
    return replace(w, spec=spec, status=status)


class WidgetStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[tuple[str, str], Widget] = {}

    # ---------- reads ----------

    def get(self, namespace: str, name: str) -> Widget:
        with self._lock:
            w = self._items.get((namespace, name))
            if w is None:
                raise NotFoundError(f"widget {namespace}/{name} not found")
            return _clone(w)

    def exists(self, namespace: str, name: str) -> bool:
        with self._lock:
            return (namespace, name) in self._items

    def list(self, namespace: Optional[str] = None) -> list[Widget]:
        with self._lock:
            return [
                _clone(w)
                for (ns, _name), w in self._items.items()
                if namespace is None or ns == namespace
            ]

    # ---------- mutations ----------

    def create(self, namespace: str, name: str, spec: WidgetSpec) -> Widget:
        with self._lock:
            key = (namespace, name)
            if key in self._items:
                raise IdentifierConflictError(
                    f"widget {namespace}/{name} already exists"
                )
            w = Widget(namespace=namespace, name=name, spec=replace(spec), version=0)
            self._items[key] = w
            return _clone(w)

    def update_spec(
        self, namespace: str, name: str, spec: WidgetSpec, expected_version: int
    ) -> Widget:
        with self._lock:
            w = self._items.get((namespace, name))
            if w is None:
                raise NotFoundError(f"widget {namespace}/{name} not found")
            if w.deleting:
                raise DeletingConflictError(
                    f"widget {namespace}/{name} is being deleted"
                )
            if w.version != expected_version:
                raise VersionConflictError(
                    f"expected version {expected_version} but current is {w.version}"
                )
            w.spec = replace(spec)
            if spec.ref is not None:
                w.spec.ref = replace(spec.ref)
            w.version += 1
            return _clone(w)

    def update_status(self, namespace: str, name: str, expected: Widget) -> Widget:
        """Compare-and-swap the status block.

        Succeeds only when the stored object's version still matches the one
        the reconciler read; spec/version/deleting are never modified here.
        """
        with self._lock:
            w = self._items.get((namespace, name))
            if w is None:
                raise NotFoundError(f"widget {namespace}/{name} not found")
            if w.version != expected.version or w.deleting != expected.deleting:
                raise VersionConflictError(
                    f"status write lost: expected version {expected.version}, "
                    f"current {w.version}"
                )
            w.status = replace(expected.status)
            return _clone(w)

    def mark_deleting(
        self, namespace: str, name: str, expected_version: int
    ) -> Widget:
        """Place the deletion tombstone.

        Repeated calls with the same (still-current) version are idempotent and
        return the tombstoned widget unchanged.
        """
        with self._lock:
            w = self._items.get((namespace, name))
            if w is None:
                raise NotFoundError(f"widget {namespace}/{name} not found")
            if w.version != expected_version:
                raise VersionConflictError(
                    f"expected version {expected_version} but current is {w.version}"
                )
            if not w.deleting:
                w.deleting = True
            return _clone(w)

    def delete(self, namespace: str, name: str) -> None:
        """Final removal after cleanup. Idempotent when already absent."""
        with self._lock:
            self._items.pop((namespace, name), None)
