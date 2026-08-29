"""Canonical JSON serialisation for anything that gets hashed.

Constraint 2 of the brief. Dict insertion order must not affect a hash, so
`sort_keys=True` is the whole point. Fixed separators, explicit UTF-8, no NaN.

Constraint 1 is enforced here rather than at the call site: **all money is
integer paise, and a float anywhere in an event body is a TypeError.** Floats
are not canonically serialisable — `0.1 + 0.2` does not render identically
across platforms — so a single float makes the chain non-reproducible between
environments. Rejecting eagerly means that bug surfaces on the first append
instead of at demo time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import Enum
from typing import Any

__all__ = ["canonical", "canonical_str"]



def _reject_floats(node: Any, path: str = "$") -> None:
    """Walk the whole body before serialising and refuse any float.

    Done as a separate pass because `json.dumps` has no hook that fires on a
    float — `default` is only consulted for types json does not already know.
    """
    if isinstance(node, bool):  # bool subclasses int; explicitly fine
        return
    if isinstance(node, float):
        raise TypeError(
            f"float at {path}: all money is integer paise and floats are not "
            f"canonically serialisable (got {node!r})"
        )
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                raise TypeError(f"non-string key at {path}: {key!r}")
            _reject_floats(value, f"{path}.{key}")
        return
    if isinstance(node, (list, tuple)):
        for i, value in enumerate(node):
            _reject_floats(value, f"{path}[{i}]")
        return


def _encode(value: Any) -> Any:
    """Render the few non-JSON types an event body is allowed to carry."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise TypeError(f"naive datetime {value!r}: timestamps must be timezone-aware UTC")
        # ISO-8601 UTC, explicit Z, second precision. Sub-second precision is
        # dropped deliberately: it is a reproducibility hazard and no event in
        # this system is ordered by it.
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"{type(value).__name__} is not serialisable in an event body: {value!r}")


def canonical(event: Any) -> bytes:
    """Serialise `event` to the one byte-string that represents it."""
    _reject_floats(event)
    return json.dumps(
        event,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=_encode,
    ).encode("utf-8")


def canonical_str(event: Any) -> str:
    """`canonical()` as text, for storage in a JSON/TEXT column."""
    return canonical(event).decode("utf-8")
