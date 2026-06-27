"""Unit tests for the pure value helpers."""

import pytest

from nautobot_ssot_kea.utils.kea import (
    canonical_dt,
    kea_expire_to_iso,
    kea_lease_state,
    normalize_mac,
    normalize_option_data,
    parse_kea_leases_csv,
    parse_kea_pool,
)


def test_kea_lease_state():
    assert kea_lease_state(0) == "active"
    assert kea_lease_state("0") == "active"
    assert kea_lease_state(1) == "declined"
    assert kea_lease_state(2) == "expired"
    assert kea_lease_state("") == "active"


def test_kea_expire_to_iso():
    assert kea_expire_to_iso(1782000000) == "2026-06-21T00:00:00+00:00"
    assert kea_expire_to_iso("") == ""


def test_parse_kea_leases_csv_dedupes_and_drops_markers():
    text = (
        "address,hwaddr,client_id,valid_lifetime,expire,subnet_id,fqdn_fwd,fqdn_rev,hostname,state,user_context,pool_id\n"
        "10.0.10.50,aa:bb:cc:dd:ee:01,,691200,1782000000,1,0,0,laptop-42,0,,0\n"
        "10.0.10.60,aa:bb:cc:dd:ee:03,,691200,1782200000,1,0,0,old,0,,0\n"
        "10.0.10.60,aa:bb:cc:dd:ee:03,,0,1782200000,1,0,0,old,0,,0\n"  # delete marker
        "10.0.10.70,aa:bb:cc:dd:ee:04,,691200,1782300000,1,0,0,decl,1,,0\n"
    )
    leases = {row["address"]: row for row in parse_kea_leases_csv(text)}
    assert set(leases) == {"10.0.10.50", "10.0.10.70"}  # .60 deleted by the marker
    assert leases["10.0.10.50"]["subnet_id"] == 1
    assert leases["10.0.10.50"]["hwaddr"] == "aa:bb:cc:dd:ee:01"
    assert leases["10.0.10.70"]["state"] == "1"


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
