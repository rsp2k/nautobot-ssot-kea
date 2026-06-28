"""Kea source adapter — load() against the fixture config.

Pure pytest: no Django, no Nautobot ORM, no live Kea server.
"""

import json
from pathlib import Path

import pytest

from nautobot_ssot_kea.diffsync.adapters.kea import KeaAdapter

FIXTURE = Path(__file__).parent / "fixtures" / "kea-dhcp4.conf"


@pytest.fixture
def adapter() -> KeaAdapter:
    raw = json.loads(FIXTURE.read_text())
    config = raw.get("Dhcp4", raw)
    a = KeaAdapter(config=config, server_name="kea01")
    a.load()
    return a


def test_server_loaded(adapter):
    server = adapter.get("dhcpserver", "kea01")
    assert server.vendor == "kea"
    assert server.ad_authorized is None


def test_scopes_loaded_with_cidr_and_lease_time(adapter):
    scopes = {s.prefix: s for s in adapter.get_all("dhcpscope")}
    assert set(scopes) == {"10.0.10.0/24", "10.0.20.0/24"}
    assert scopes["10.0.10.0/24"].state == "enabled"
    # Per-subnet valid-lifetime wins.
    assert scopes["10.0.10.0/24"].default_lease_time == 691200
    # Subnet without its own valid-lifetime falls back to the global default.
    assert scopes["10.0.20.0/24"].default_lease_time == 86400
    # Kea subnets have no name.
    assert scopes["10.0.10.0/24"].name == ""


def test_pools_parsed(adapter):
    pools = adapter.get_all("dhcppool")
    assert len(pools) == 2  # one per subnet
    p10 = [p for p in pools if p.prefix == "10.0.10.0/24"][0]
    assert p10.start_address == "10.0.10.10"
    assert p10.end_address == "10.0.10.250"


def test_no_exclusions(adapter):
    assert adapter.get_all("dhcpexclusion") == []


def test_reservation_normalizes_mac(adapter):
    res = adapter.get(
        "dhcpreservation",
        {"server_name": "kea01", "prefix": "10.0.10.0/24", "ip_address": "10.0.10.5"},
    )
    assert res.mac_address == "00:11:22:33:44:55"
    assert res.hostname == "printer-f1"
    assert res.reservation_type == "dhcp"


def test_no_leases(adapter):
    # Config-only adapter (no lease dump passed) emits no leases.
    assert adapter.get_all("dhcplease") == []


def test_leases_loaded_from_dump():
    from nautobot_ssot_kea.utils.kea import parse_kea_leases_csv

    raw = json.loads(FIXTURE.read_text())
    config = raw.get("Dhcp4", raw)
    leases = parse_kea_leases_csv((Path(__file__).parent / "fixtures" / "kea-leases4.csv").read_text())
    a = KeaAdapter(config=config, server_name="kea01", leases=leases)
    a.load()

    loaded = a.get_all("dhcplease")
    assert len(loaded) == 3  # .50, .51 active + .70 declined (.60 deleted by marker)
    lease = a.get(
        "dhcplease",
        {"server_name": "kea01", "prefix": "10.0.10.0/24", "ip_address": "10.0.10.50"},
    )
    assert lease.mac_address == "aa:bb:cc:dd:ee:01"
    assert lease.hostname == "laptop-42"
    assert lease.lease_state == "active"
    assert lease.expires == "2026-06-21T00:00:00+00:00"

    declined = a.get(
        "dhcplease",
        {"server_name": "kea01", "prefix": "10.0.10.0/24", "ip_address": "10.0.10.70"},
    )
    assert declined.lease_state == "declined"


def test_options_at_three_levels(adapter):
    options = adapter.get_all("dhcpoption")
    server_opts = [o for o in options if not o.scope_prefix and not o.reservation_ip]
    scope_opts = [o for o in options if o.scope_prefix and not o.reservation_ip]
    res_opts = [o for o in options if o.reservation_ip]
    # Global DNS (1), routers per subnet (2), reservation routers (1).
    assert len(server_opts) == 1
    assert len(scope_opts) == 2
    assert len(res_opts) == 1

    dns = [o for o in server_opts if o.code == 6][0]
    # Comma+space in the Kea source is normalized to comma-no-space.
    assert dns.value == "10.0.0.10,10.0.0.11"
    assert dns.option_name == "domain-name-servers"


FIXTURE6 = Path(__file__).parent / "fixtures" / "kea-dhcp6.conf"


@pytest.fixture
def adapter6() -> KeaAdapter:
    raw = json.loads(FIXTURE6.read_text())
    config = raw.get("Dhcp6", raw)
    a = KeaAdapter(config=config, server_name="kea01-dhcp6")
    a.load()
    return a


def test_v6_family_detected(adapter6):
    assert adapter6.family == 6


def test_v6_scope_carries_pd_fields(adapter6):
    scope = adapter6.get(
        "dhcpscope", {"server_name": "kea01-dhcp6", "prefix": "2001:db8:100::/64"}
    )
    assert scope.preferred_lifetime == 86400
    assert scope.rapid_commit is True
    assert scope.pd_allocator == "iterative"
    assert scope.relay_addresses == ["2001:db8:100::1"]
    assert scope.reservations_out_of_pool is True


def test_v6_pd_pool_loaded(adapter6):
    pdp = adapter6.get(
        "dhcpprefixdelegationpool",
        {
            "server_name": "kea01-dhcp6",
            "prefix": "2001:db8:100::/64",
            "pd_prefix": "2001:db8:cafe::",
            "prefix_length": 48,
            "delegated_length": 56,
        },
    )
    assert pdp.excluded_prefix == "2001:db8:cafe::"
    assert pdp.excluded_prefix_length == 72


def test_v6_reservation_fans_address_and_prefix(adapter6):
    # One DUID with one ip-address and one prefix -> one address row + one PD row.
    res = adapter6.get(
        "dhcpreservation",
        {"server_name": "kea01-dhcp6", "prefix": "2001:db8:100::/64", "ip_address": "2001:db8:100::5"},
    )
    assert res.identifier_type == "duid"
    assert res.duid == "00:03:00:01:aa:bb:cc:dd:ee:ff"
    assert res.hostname == "cpe-1"

    pd_res = adapter6.get(
        "dhcpdelegatedprefixreservation",
        {
            "server_name": "kea01-dhcp6",
            "prefix": "2001:db8:100::/64",
            "delegated_prefix": "2001:db8:cafe:100::",
            "delegated_prefix_length": 56,
        },
    )
    assert pd_res.duid == "00:03:00:01:aa:bb:cc:dd:ee:ff"


def test_v6_options_in_dhcp6_space(adapter6):
    options = adapter6.get_all("dhcpoption")
    assert options, "expected v6 options"
    assert all(o.space == "dhcp6" for o in options)
    # The pd-pool option carries the pd_pool_key identifier.
    pd_opts = [o for o in options if o.pd_pool_key]
    assert len(pd_opts) == 1
    assert pd_opts[0].code == 64
    assert pd_opts[0].pd_pool_key == "2001:db8:cafe::/48-56"


def test_v6_leases_loaded_from_dump():
    from nautobot_ssot_kea.utils.kea import parse_kea_leases6_csv

    raw = json.loads(FIXTURE6.read_text())
    config = raw.get("Dhcp6", raw)
    leases = parse_kea_leases6_csv((Path(__file__).parent / "fixtures" / "kea-leases6.csv").read_text())
    a = KeaAdapter(config=config, server_name="kea01-dhcp6", leases=leases)
    a.load()

    loaded = a.get_all("dhcplease")
    # .::5 (na) + .cafe:100:: (pd) + .::70 (declined); .::9 deleted by marker.
    assert len(loaded) == 3
    pd_lease = a.get(
        "dhcplease",
        {"server_name": "kea01-dhcp6", "prefix": "2001:db8:100::/64", "ip_address": "2001:db8:cafe:100::"},
    )
    assert pd_lease.lease_type == "pd"
    assert pd_lease.prefix_length == 56
    assert pd_lease.duid == "00:03:00:01:aa:bb:cc:dd:ee:01"
