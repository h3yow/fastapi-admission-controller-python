"""Simulated external system that owns real side effects (gadgets).

The point of this module is to make reconciliation idempotency observable:
all operations are keyed by the stable ``gadget_id``, so submitting the same
desired state twice results in exactly one gadget, and deleting an absent
gadget is a no-op.  Tests assert on :meth:`gadget_count` to prove that
repeated/concurrent reconciliation never duplicates side effects.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class Gadget:
    gadget_id: str
    content: str


class ExternalSystem:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._gadgets: dict[str, Gadget] = {}
        # Per-key locks serialize operations on the same gadget so concurrent
        # reconciles for the same widget cannot create duplicates.
        self._key_locks: dict[str, threading.RLock] = {}
        self._key_lock = threading.Lock()

    def _lock_for(self, gadget_id: str) -> threading.RLock:
        with self._key_lock:
            lk = self._key_locks.get(gadget_id)
            if lk is None:
                lk = threading.RLock()
                self._key_locks[gadget_id] = lk
            return lk

    def upsert_gadget(self, gadget_id: str, content: str) -> str:
        """Create or update the gadget with the given stable id.

        Returns the gadget id. Calling this repeatedly with the same id only
        ever results in a single gadget.
        """
        with self._lock_for(gadget_id):
            with self._lock:
                self._gadgets[gadget_id] = Gadget(gadget_id, content)
            return gadget_id

    def delete_gadget(self, gadget_id: str) -> None:
        """Delete the gadget if present; no-op otherwise (idempotent)."""
        with self._lock_for(gadget_id):
            with self._lock:
                self._gadgets.pop(gadget_id, None)

    def gadget_exists(self, gadget_id: str) -> bool:
        with self._lock:
            return gadget_id in self._gadgets

    def gadget_content(self, gadget_id: str) -> str | None:
        with self._lock:
            g = self._gadgets.get(gadget_id)
            return None if g is None else g.content

    def gadget_count(self) -> int:
        with self._lock:
            return len(self._gadgets)

    def reset(self) -> None:
        with self._lock:
            self._gadgets.clear()
