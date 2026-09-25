"""Background reconciliation: converge desired ``spec`` onto actual state.

Idempotency
-----------
* Each Widget deterministically owns exactly one external gadget whose id is
  derived from its identity (``gadget-<namespace>-<name>``).  Re-running
  reconcile therefore upserts the *same* gadget; duplicate/triggered
  reconciles never create duplicate gadgets.
* Status writes use compare-and-swap (:meth:`WidgetStore.update_status`).  If
  another writer bumped the spec version while we were working, the CAS fails
  with ``version_conflict`` and we re-read and retry instead of overwriting
  the newer desired state (no lost update).
* A Widget already reporting the desired state is a no-op (status untouched).

Deletion
--------
:meth:`finalize_delete` removes the owned gadget and then the tombstone.
Both steps are idempotent and keyed by identity, so duplicate or racing
finalizers converge to "gone" without touching other widgets' gadgets.
"""

from __future__ import annotations

import logging

from app.errors import VersionConflictError
from app.external import ExternalSystem
from app.models import Widget, WidgetPhase
from app.store import WidgetStore

logger = logging.getLogger("controlplane.reconciler")

# Upper bound on CAS retries before giving up (defensive; contention here is
# tiny because the critical section is short).
_MAX_CAS_RETRIES = 10


def gadget_id_for(namespace: str, name: str) -> str:
    """Deterministic external identity owned by a Widget."""
    return f"gadget-{namespace}-{name}"


class Reconciler:
    def __init__(self, store: WidgetStore, external: ExternalSystem) -> None:
        self.store = store
        self.external = external

    # ------------------------------------------------------------------
    def reconcile_one(self, namespace: str, name: str) -> Widget:
        """Converge one widget. Safe to call repeatedly and concurrently.

        Raises :class:`NotFoundError` if the resource was removed.
        """
        for attempt in range(1, _MAX_CAS_RETRIES + 1):
            snapshot = self.store.get(namespace, name)
            if snapshot.deleting:
                logger.info(
                    "reconcile skip: %s/%s is deleting; awaiting finalize",
                    namespace,
                    name,
                )
                return snapshot

            gadget_id = gadget_id_for(namespace, name)
            already_done = (
                snapshot.status.gadget_id == gadget_id
                and snapshot.status.observed_version == snapshot.version
                and snapshot.status.phase is WidgetPhase.ACTIVE
                and self.external.gadget_content(gadget_id) == snapshot.spec.content
            )
            if already_done:
                logger.info(
                    "reconcile no-op: %s/%s version=%s already converged gadget=%s",
                    namespace,
                    name,
                    snapshot.version,
                    gadget_id,
                )
                return snapshot

            # Side effect is idempotent: same stable id -> single gadget.
            self.external.upsert_gadget(gadget_id, snapshot.spec.content)
            logger.info(
                "reconcile apply: input=%s/%s version=%s content=%r -> upsert gadget=%s (attempt %d)",
                namespace,
                name,
                snapshot.version,
                snapshot.spec.content,
                gadget_id,
                attempt,
            )

            snapshot.status.phase = WidgetPhase.ACTIVE
            snapshot.status.observed_version = snapshot.version
            snapshot.status.gadget_id = gadget_id
            snapshot.status.message = (
                f"reconciled at version {snapshot.version} -> {gadget_id}"
            )

            try:
                result = self.store.update_status(namespace, name, snapshot)
            except VersionConflictError:
                # A concurrent spec update landed; drop our status and retry
                # against the newer desired state rather than clobbering it.
                logger.info(
                    "reconcile cas-lost: %s/%s expected version=%s; re-reading and retrying",
                    namespace,
                    name,
                    snapshot.version,
                )
                continue
            logger.info(
                "reconcile committed: %s/%s observed_version=%s gadget=%s",
                namespace,
                name,
                result.status.observed_version,
                gadget_id,
            )
            return result

        raise VersionConflictError(
            f"reconcile {namespace}/{name} exhausted CAS retries"
        )

    # ------------------------------------------------------------------
    def finalize_delete(self, namespace: str, name: str) -> None:
        """Delete the owned gadget and remove the resource tombstone.

        Idempotent: a second call after successful removal is a no-op and
        never raises, and it never affects any other widget's gadget.
        """
        gadget_id = gadget_id_for(namespace, name)
        # Cleanup runs even if the tombstone record is already gone, so a
        # crash between cleanup and store removal remains recoverable.
        existed = self.external.gadget_exists(gadget_id)
        self.external.delete_gadget(gadget_id)
        self.store.delete(namespace, name)
        logger.info(
            "finalize delete: input=%s/%s gadget=%s removed_external=%s and store record",
            namespace,
            name,
            gadget_id,
            existed,
        )
