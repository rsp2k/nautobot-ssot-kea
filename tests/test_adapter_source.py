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
    # Kea leases live in the lease database, not the config.
    assert adapter.get_all("dhcplease") == []


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
