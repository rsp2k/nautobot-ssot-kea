"""Unit tests for the Kea config export's pure logic (pool/exclusion subtraction)."""

from nautobot_ssot_kea.export import pools_minus_exclusions


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
