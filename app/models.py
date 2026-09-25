"""Resource model definitions.

The control plane manages a toy custom resource named ``Widget``.  A Widget
declaratively owns one "gadget" in a simulated external system; the reconciler
makes the external system's actual state converge to the desired ``spec``.

Concurrency is controlled by an opaque, monotonically increasing ``version``:
every accepted mutation bumps the version.  Clients must present the version
they last saw; a stale write fails with a conflict instead of overwriting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# Namespaces an ordinary request is allowed to touch.  Anything else is
# rejected at admission as an unauthorized namespace.
ALLOWED_NAMESPACES = frozenset({"default", "team-a", "team-b"})


class WidgetPhase(str, Enum):
    """Lifecycle phase of a Widget, reported in ``status.phase``."""

    PENDING = "Pending"
    ACTIVE = "Active"
    DELETING = "Deleting"
    FAILED = "Failed"


@dataclass(frozen=True)
class WidgetRef:
    """A reference to another Widget.

    References are identity-only: namespace + name.
    """

    namespace: str
    name: str


@dataclass
class WidgetSpec:
    """Desired state."""

    # Free-form desired payload for the external gadget. Must be non-empty.
    content: str = ""
    # A Widget may reference at most one other Widget, and only within its own
    # namespace (cross-namespace refs are rejected at admission).
    ref: Optional[WidgetRef] = None


@dataclass
class WidgetStatus:
    """Observed state, written only by the reconciler."""

    phase: WidgetPhase = WidgetPhase.PENDING
    # The spec version last reconciled successfully.
    observed_version: int = 0
    # Identity of the gadget created in the external system.
    gadget_id: Optional[str] = None
    # Human-readable explanation of the last decision (kept stable for tests).
    message: str = ""


@dataclass
class Widget:
    """A stored custom resource."""

    namespace: str
    name: str
    spec: WidgetSpec = field(default_factory=WidgetSpec)
    status: WidgetStatus = field(default_factory=WidgetStatus)
    version: int = 0
    # Deletion tombstone marker; once set the resource only accepts a final
    # delete completion, never spec updates.
    deleting: bool = False


@dataclass(frozen=True)
class AdmissionRequest:
    """A normalized admission-review style request."""

    operation: str  # "CREATE" | "UPDATE" | "DELETE"
    namespace: str
    name: str
    new_spec: Optional[WidgetSpec]
    # Present for UPDATE/DELETE; the currently stored object.
    old: Optional[Widget] = None
    # True when an object with this identity already exists (for CREATE).
    exists: bool = False
    # Version the client based its request on (UPDATE/DELETE).
    expected_version: Optional[int] = None


@dataclass(frozen=True)
class AdmissionResponse:
    allowed: bool
    reason: str  # stable machine-readable code; "" when allowed
    message: str
