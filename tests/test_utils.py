"""Unit tests for the pure value helpers."""

import pytest

from nautobot_ssot_kea.utils.kea import (
    canonical_dt,
    normalize_mac,
    normalize_option_data,
    parse_kea_pool,
)


def test_parse_kea_pool_range():
    assert parse_kea_pool("10.0.10.10 - 10.0.10.250") == ("10.0.10.10", "10.0.10.250")
    # Tolerate a hyphen with no surrounding spaces.
    assert parse_kea_pool("10.0.10.10-10.0.10.250") == ("10.0.10.10", "10.0.10.250")


def test_parse_kea_pool_cidr():
    assert parse_kea_pool("10.0.30.0/24") == ("10.0.30.0", "10.0.30.255")
    assert parse_kea_pool("192.0.2.0/28") == ("192.0.2.0", "192.0.2.15")


def test_parse_kea_pool_invalid():
    with pytest.raises(ValueError):
        parse_kea_pool("garbage")


def test_normalize_option_data():
    assert normalize_option_data("10.0.0.10, 10.0.0.11") == "10.0.0.10,10.0.0.11"
    assert normalize_option_data("10.0.10.1") == "10.0.10.1"
    assert normalize_option_data(["10.0.0.1", "10.0.0.2"]) == "10.0.0.1,10.0.0.2"
    assert normalize_option_data(None) == ""


def test_normalize_mac():
    assert normalize_mac("00:11:22:33:44:55") == "00:11:22:33:44:55"
    assert normalize_mac("00-11-22-33-44-55") == "00:11:22:33:44:55"
    # Non-MAC identifier (e.g. a DUID/client-id) passes through, lowercased.
    assert normalize_mac("some-client-id-7") == "some-client-id-7"


def test_canonical_dt_normalizes_z_and_offset():
    a = canonical_dt("2026-06-29T08:00:00Z")
    b = canonical_dt("2026-06-29T08:00:00+00:00")
    assert a == b == "2026-06-29T08:00:00+00:00"
    assert canonical_dt("") == ""
