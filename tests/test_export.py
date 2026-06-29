"""Unit tests for the Kea config export's pure logic (pool/exclusion subtraction)."""

from types import SimpleNamespace

from nautobot_ssot_kea.diffsync.adapters.kea import _require_classes
from nautobot_ssot_kea.export import _emit_require_classes, pools_minus_exclusions


def test_no_exclusions_passes_through():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.250")], []) == [("10.0.0.10", "10.0.0.250")]


def test_single_exclusion_splits_pool():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.250")], [("10.0.0.50", "10.0.0.60")]) == [
        ("10.0.0.10", "10.0.0.49"),
        ("10.0.0.61", "10.0.0.250"),
    ]


def test_exclusion_at_start():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.250")], [("10.0.0.10", "10.0.0.19")]) == [
        ("10.0.0.20", "10.0.0.250"),
    ]


def test_exclusion_at_end():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.250")], [("10.0.0.200", "10.0.0.250")]) == [
        ("10.0.0.10", "10.0.0.199"),
    ]


def test_exclusion_covers_whole_pool():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.250")], [("10.0.0.1", "10.0.0.255")]) == []


def test_multiple_exclusions():
    assert pools_minus_exclusions(
        [("10.0.0.10", "10.0.0.250")],
        [("10.0.0.50", "10.0.0.60"), ("10.0.0.100", "10.0.0.110")],
    ) == [
        ("10.0.0.10", "10.0.0.49"),
        ("10.0.0.61", "10.0.0.99"),
        ("10.0.0.111", "10.0.0.250"),
    ]


def test_exclusion_outside_pool_is_ignored():
    assert pools_minus_exclusions([("10.0.0.10", "10.0.0.20")], [("10.0.9.0", "10.0.9.9")]) == [
        ("10.0.0.10", "10.0.0.20"),
    ]


# --- client-class associations: key-aliasing on read, single spelling on write ---


def test_require_classes_reads_legacy_key():
    assert _require_classes({"require-client-classes": ["a", "b"]}) == ["a", "b"]


def test_require_classes_reads_new_key():
    assert _require_classes({"evaluate-additional-classes": ["a"]}) == ["a"]


def test_require_classes_prefers_new_key_when_both_present():
    element = {"require-client-classes": ["old"], "evaluate-additional-classes": ["new"]}
    assert _require_classes(element) == ["new"]


def test_require_classes_absent_is_empty_list():
    assert _require_classes({}) == []


def test_emit_require_classes_writes_legacy_key():
    element = {}
    _emit_require_classes(element, SimpleNamespace(require_client_classes=["corp"]))
    assert element == {"require-client-classes": ["corp"]}


def test_emit_require_classes_omits_when_empty():
    element = {}
    _emit_require_classes(element, SimpleNamespace(require_client_classes=[]))
    assert element == {}  # no empty list emitted -> no diff churn against a bare config


def test_emit_then_read_round_trips():
    element = {}
    _emit_require_classes(element, SimpleNamespace(require_client_classes=["x", "y"]))
    assert _require_classes(element) == ["x", "y"]
