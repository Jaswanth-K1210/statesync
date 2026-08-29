"""Canonical serialisation — the foundation the hash chain rests on.

Constraint 2: json.dumps(sort_keys=True, separators=(",",":"),
ensure_ascii=False, allow_nan=False).
Constraint 1: all money is integer paise; floats are rejected outright.
"""

from datetime import UTC, datetime

import pytest

from statesync.ledger.canonical import canonical


def test_canonical_ignores_key_order():
    a = {"amount": 100, "id": "pay_1", "type": "CAPTURE"}
    b = {"type": "CAPTURE", "id": "pay_1", "amount": 100}
    assert canonical(a) == canonical(b)


def test_canonical_uses_compact_separators():
    assert canonical({"a": 1, "b": 2}) == b'{"a":1,"b":2}'


def test_canonical_rejects_top_level_float():
    with pytest.raises(TypeError):
        canonical({"amount": 100.50})


def test_canonical_rejects_float_nested_in_list():
    with pytest.raises(TypeError):
        canonical({"components": [{"mdr": 9440}, {"gst": 1699.2}]})


def test_canonical_rejects_nan_and_infinity():
    with pytest.raises(TypeError):
        canonical({"x": float("nan")})
    with pytest.raises(TypeError):
        canonical({"x": float("inf")})


def test_canonical_accepts_bool_which_is_not_a_float():
    assert canonical({"ok": True}) == b'{"ok":true}'


def test_canonical_renders_datetime_as_iso8601_utc_seconds():
    event = {"at": datetime(2026, 9, 5, 14, 30, 9, 123456, tzinfo=UTC)}
    assert canonical(event) == b'{"at":"2026-09-05T14:30:09Z"}'


def test_canonical_requires_timezone_aware_datetimes():
    with pytest.raises(TypeError):
        canonical({"at": datetime(2026, 9, 5, 14, 30, 9)})  # noqa: DTZ001


def test_canonical_preserves_non_ascii_without_escaping():
    assert canonical({"note": "₹4,000"}) == '{"note":"₹4,000"}'.encode()


def test_canonical_rejects_unknown_object_types():
    class Opaque:
        pass

    with pytest.raises(TypeError):
        canonical({"x": Opaque()})
