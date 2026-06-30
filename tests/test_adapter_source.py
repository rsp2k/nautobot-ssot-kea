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
    scope = adapter6.get("dhcpscope", {"server_name": "kea01-dhcp6", "prefix": "2001:db8:100::/64"})
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


def test_noncanonical_v6_input_is_canonicalized():
    """A source config with uppercase/leading-zero v6 literals must yield canonical
    DiffSync identities, else it re-creates every record on the second sync (the
    store round-trips to canonical, so a non-canonical identity never matches)."""
    config = {
        "subnet6": [
            {
                "id": 1,
                "subnet": "2001:DB8:0001::/64",
                "pools": [{"pool": "2001:DB8:0001::1000 - 2001:DB8:0001::2000"}],
                "pd-pools": [{"prefix": "2001:DB8:CAFE::", "prefix-len": 48, "delegated-len": 56}],
                "reservations": [
                    {
                        "duid": "00:03:00:01:aa",
                        "ip-addresses": ["2001:DB8:0001::5"],
                        "prefixes": ["2001:DB8:CAFE:0100::/56"],
                    }
                ],
            }
        ]
    }
    a = KeaAdapter(config=config, server_name="kea6", family=6)
    a.load()
    scope = a.get_all("dhcpscope")[0]
    assert scope.prefix == "2001:db8:1::/64"
    pool = a.get_all("dhcppool")[0]
    assert pool.start_address == "2001:db8:1::1000" and pool.end_address == "2001:db8:1::2000"
    pdp = a.get_all("dhcpprefixdelegationpool")[0]
    assert pdp.pd_prefix == "2001:db8:cafe::"
    res = a.get_all("dhcpreservation")[0]
    assert res.ip_address == "2001:db8:1::5"
    pdr = a.get_all("dhcpdelegatedprefixreservation")[0]
    assert pdr.delegated_prefix == "2001:db8:cafe:100::"


def test_code_only_option_gets_synthesized_name():
    """An option given by code only must carry option_name='option-<code>' to match
    the optdef name the target synthesizes -- otherwise it diffs forever."""
    config = {"option-data": [{"code": 42, "data": "1.2.3.4"}], "subnet4": []}
    a = KeaAdapter(config=config, server_name="kea4", family=4)
    a.load()
    opt = a.get_all("dhcpoption")[0]
    assert opt.code == 42
    assert opt.option_name == "option-42"


def test_v4_clientid_reservation_does_not_overflow_mac():
    """A non-hw v4 reservation identifier must land in client_id, not mac_address."""
    long_id = "x" * 40  # >17 chars; would overflow mac_address(17)
    config = {
        "subnet4": [
            {"id": 1, "subnet": "10.0.0.0/24", "reservations": [{"client-id": long_id, "ip-address": "10.0.0.5"}]}
        ]
    }
    a = KeaAdapter(config=config, server_name="kea4", family=4)
    a.load()
    res = a.get_all("dhcpreservation")[0]
    assert res.identifier_type == "client-id"
    assert res.client_id == long_id
    assert res.mac_address == ""


def test_v4_lease_clientid_does_not_overflow_mac():
    """A v4 lease keyed by a long client-id (no hwaddr) routes to duid, not mac_address.

    Real v4 leases can be identified by an RFC 4361 / DUID-style client-id with no
    hardware address; that id is far longer than 17 chars and would overflow
    mac_address (the crash a real MS export surfaced on the reservation path).
    """
    long_cid = "ff:00:00:00:00:02:00:00:ab:11:38:34:59:10:5f:03:e5:2c"  # 18 octets
    config = {"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}
    leases = [{"address": "10.0.0.50", "subnet_id": 1, "client_id": long_cid, "state": 0, "expire": 0}]
    a = KeaAdapter(config=config, server_name="kea4", family=4, leases=leases)
    a.load()
    lease = a.get_all("dhcplease")[0]
    assert lease.mac_address == ""
    assert lease.duid == long_cid  # the wide slot, matching where MS puts an extended id
    # A normal hwaddr-keyed lease still lands in mac_address.
    a2 = KeaAdapter(
        config=config, server_name="kea4", family=4,
        leases=[{"address": "10.0.0.51", "subnet_id": 1, "hwaddr": "aa:bb:cc:dd:ee:01", "state": 0, "expire": 0}],
    )
    a2.load()
    lease2 = a2.get_all("dhcplease")[0]
    assert lease2.mac_address == "aa:bb:cc:dd:ee:01" and lease2.duid == ""


def test_user_context_and_unmodeled_keys_captured():
    """user-context and any config keys we don't model are preserved (escape hatch)."""
    config = {
        "valid-lifetime": 86400,
        "decline-probation-period": 86400,  # unmodeled global -> server.extra
        "user-context": {"site": "hq"},
        "subnet4": [
            {
                "id": 1,
                "subnet": "10.0.10.0/24",
                "user-context": {"vlan": 100},
                "store-extended-info": True,  # unmodeled subnet key -> scope.extra
                "calculate-tee-times": True,  # unmodeled subnet key -> scope.extra
                "pools": [{"pool": "10.0.10.10 - 10.0.10.250"}],
                "reservations": [
                    {
                        "hw-address": "00:11:22:33:44:55",
                        "ip-address": "10.0.10.5",
                        "client-classes": ["voip"],  # first-class -> reservation.client_classes
                        "qualifying-suffix": "lab.example.",  # unmodeled -> reservation.extra
                    }
                ],
            }
        ],
    }
    a = KeaAdapter(config=config, server_name="kea01", family=4)
    a.load()

    server = a.get("dhcpserver", "kea01")
    assert server.user_context == {"site": "hq"}
    assert server.extra.get("decline-probation-period") == 86400
    assert "valid-lifetime" not in server.extra  # consumed, not leaked into extra

    scope = a.get("dhcpscope", {"server_name": "kea01", "prefix": "10.0.10.0/24"})
    assert scope.user_context == {"vlan": 100}
    assert scope.extra.get("store-extended-info") is True
    assert scope.extra.get("calculate-tee-times") is True
    assert "pools" not in scope.extra and "reservations" not in scope.extra

    res = a.get("dhcpreservation", {"server_name": "kea01", "prefix": "10.0.10.0/24", "ip_address": "10.0.10.5"})
    assert res.client_classes == ["voip"]  # promoted to a first-class field
    assert res.extra.get("qualifying-suffix") == "lab.example."  # genuinely unmodeled -> extra
    assert "client-classes" not in res.extra  # consumed, not leaked


def test_lifetime_triplet_captured():
    """min/max valid + min/max preferred lifetimes are first-class, not extra."""
    config = {
        "subnet6": [
            {
                "id": 1,
                "subnet": "2001:db8:100::/64",
                "min-valid-lifetime": 1800,
                "valid-lifetime": 3600,
                "max-valid-lifetime": 7200,
                "min-preferred-lifetime": 900,
                "preferred-lifetime": 1800,
                "max-preferred-lifetime": 3600,
            }
        ],
    }
    a = KeaAdapter(config=config, server_name="kea6", family=6)
    a.load()
    s = a.get("dhcpscope", {"server_name": "kea6", "prefix": "2001:db8:100::/64"})
    assert (s.min_lease_time, s.default_lease_time, s.max_lease_time) == (1800, 3600, 7200)
    assert (s.min_preferred_lifetime, s.preferred_lifetime, s.max_preferred_lifetime) == (900, 1800, 3600)
    # None of them leaked into the passthrough.
    assert not any(k.endswith("lifetime") for k in s.extra)


def test_client_class_associations_captured():
    """require-client-classes (both spellings) + reservation client-classes are first-class."""
    config = {
        "subnet4": [
            {
                "id": 1,
                "subnet": "10.0.10.0/24",
                # Older spelling on the subnet.
                "require-client-classes": ["corp", "voip"],
                "pools": [
                    {
                        "pool": "10.0.10.10 - 10.0.10.250",
                        # Newer spelling on the pool -- adapter must read it too.
                        "evaluate-additional-classes": ["guest"],
                    }
                ],
                "reservations": [
                    {
                        "hw-address": "00:11:22:33:44:55",
                        "ip-address": "10.0.10.5",
                        "client-classes": ["printer"],
                    }
                ],
            }
        ],
    }
    a = KeaAdapter(config=config, server_name="kea4", family=4)
    a.load()

    scope = a.get("dhcpscope", {"server_name": "kea4", "prefix": "10.0.10.0/24"})
    assert scope.require_client_classes == ["corp", "voip"]
    assert not any("client-classes" in k for k in scope.extra)  # consumed, not leaked

    pool = a.get_all("dhcppool")[0]
    assert pool.require_client_classes == ["guest"]  # read from the newer alias
    assert "evaluate-additional-classes" not in pool.extra

    res = a.get("dhcpreservation", {"server_name": "kea4", "prefix": "10.0.10.0/24", "ip_address": "10.0.10.5"})
    assert res.client_classes == ["printer"]


def test_v6_pd_pool_client_classes_captured():
    """A pd-pool's require list and a v6 reservation's client-classes survive the fan-out."""
    config = {
        "subnet6": [
            {
                "id": 1,
                "subnet": "2001:db8:100::/64",
                "pd-pools": [
                    {
                        "prefix": "2001:db8:cafe::",
                        "prefix-len": 48,
                        "delegated-len": 56,
                        "require-client-classes": ["wholesale"],
                    }
                ],
                "reservations": [
                    {
                        "duid": "00:03:00:01:aa:bb:cc:dd:ee:01",
                        "ip-addresses": ["2001:db8:100::5"],
                        "prefixes": ["2001:db8:cafe:100::/56"],
                        "client-classes": ["business"],
                    }
                ],
            }
        ],
    }
    a = KeaAdapter(config=config, server_name="kea6", family=6)
    a.load()

    pdp = a.get_all("dhcpprefixdelegationpool")[0]
    assert pdp.require_client_classes == ["wholesale"]

    # The v6 reservation fans into an address row + a PD row; both carry the classes.
    addr = a.get(
        "dhcpreservation",
        {"server_name": "kea6", "prefix": "2001:db8:100::/64", "ip_address": "2001:db8:100::5"},
    )
    assert addr.client_classes == ["business"]
    pd_res = a.get_all("dhcpdelegatedprefixreservation")[0]
    assert pd_res.client_classes == ["business"]


def test_ha_hook_projected_to_redundancy():
    """The HA hook projects to a redundancy group + THIS server's own member row.

    The hooks-libraries blob also stays in extra (untouched) for lossless export.
    """
    config = {
        "subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}],
        "hooks-libraries": [
            {"library": "/usr/lib/kea/hooks/libdhcp_lease_cmds.so"},
            {
                "library": "/usr/lib/kea/hooks/libdhcp_ha.so",
                "parameters": {
                    "high-availability": [{
                        "this-server-name": "server1",
                        "mode": "load-balancing",
                        "heartbeat-delay": 10000,
                        "max-response-delay": 60000,
                        "max-unacked-clients": 5,
                        "peers": [
                            {"name": "server1", "url": "http://10.0.0.1:8000/", "role": "primary"},
                            {"name": "server2", "url": "http://10.0.0.2:8000/", "role": "secondary"},
                        ],
                    }],
                },
            },
        ],
    }
    a = KeaAdapter(config=config, server_name="kea-srv1", family=4)
    a.load()

    grp_name = "ha:server1,server2"
    g = a.get("dhcpredundancygroup", grp_name)
    assert g.mode == "load-balance"  # Kea "load-balancing" mapped to the neutral mode
    assert g.heartbeat_delay == 10000
    assert g.max_response_delay == 60000
    assert g.max_unacked_clients == 5

    # Only THIS server's membership is emitted, with the role/url of the matching peer.
    members = a.get_all("dhcpredundancygroupmember")
    assert len(members) == 1
    m = members[0]
    assert m.server_name == "kea-srv1"
    assert m.role == "primary"
    assert m.url == "http://10.0.0.1:8000/"

    # The hooks-libraries blob is still in extra verbatim (read-only projection).
    srv = a.get("dhcpserver", "kea-srv1")
    assert any("libdhcp_ha" in (h.get("library") or "") for h in srv.extra.get("hooks-libraries", []))

    # Kea HA is daemon-wide: every subnet inherits the relationship as its
    # redundancy group, so the group's "Protected Scopes" view is populated.
    scope = a.get("dhcpscope", {"server_name": "kea-srv1", "prefix": "10.0.0.0/24"})
    assert scope.redundancy_group == grp_name


def test_subnet_without_ha_has_no_redundancy_group():
    """A daemon with no HA hook leaves its scopes' redundancy_group empty (no churn)."""
    a = KeaAdapter(config={"subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}]}, server_name="solo", family=4)
    a.load()
    assert a.get("dhcpscope", {"server_name": "solo", "prefix": "10.0.0.0/24"}).redundancy_group == ""


def test_v4_subnet_selection_fields_captured():
    """relay/interface/allocator/reservation-mode load for v4 subnets, not just v6.

    Regression: these keys are in _SUBNET_CONSUMED (stripped from extra), so loading
    them only for family==6 silently dropped a v4 subnet's relay/interface config.
    """
    config = {
        "subnet4": [{
            "id": 1, "subnet": "10.0.0.0/24",
            "interface": "eth1",
            "relay": {"ip-addresses": ["10.0.0.1", "10.0.0.2"]},
            "allocator": "random",
            "reservations-in-subnet": True,
            "reservations-out-of-pool": False,
            "pools": [{"pool": "10.0.0.10 - 10.0.0.250"}],
        }],
    }
    a = KeaAdapter(config=config, server_name="kea4", family=4)
    a.load()
    s = a.get("dhcpscope", {"server_name": "kea4", "prefix": "10.0.0.0/24"})
    assert s.interface == "eth1"
    assert s.relay_addresses == ["10.0.0.1", "10.0.0.2"]
    assert s.allocator == "random"
    assert s.reservations_in_subnet is True
    assert s.reservations_out_of_pool is False
    # Consumed, not leaked into the passthrough.
    assert not any(k in s.extra for k in ("relay", "interface", "allocator", "reservations-in-subnet"))


def test_server_daemon_config_captured_first_class():
    """Daemon-level dhcp-ddns/interfaces/host-reservation-identifiers promote to server fields.

    The un-promoted siblings of the nested blocks stay in extra under their key.
    """
    config = {
        "interfaces-config": {"interfaces": ["eth0", "eth1"], "dhcp-socket-type": "udp"},
        "dhcp-ddns": {"enable-updates": True, "server-ip": "127.0.0.1", "server-port": 53001, "max-queue-size": 1024},
        "host-reservation-identifiers": ["hw-address", "duid", "client-id"],
        "decline-probation-period": 86400,  # genuinely unmodeled -> extra
        "subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}],
    }
    a = KeaAdapter(config=config, server_name="kea4", family=4)
    a.load()
    srv = a.get("dhcpserver", "kea4")
    assert srv.ddns_enabled is True
    assert srv.ddns_server_ip == "127.0.0.1"
    assert srv.ddns_server_port == 53001
    assert srv.listen_interfaces == ["eth0", "eth1"]
    assert srv.host_identifier_priority == ["hw-address", "duid", "client-id"]
    # Promoted scalars are gone from the nested blocks; siblings remain.
    assert srv.extra["dhcp-ddns"] == {"max-queue-size": 1024}
    assert srv.extra["interfaces-config"] == {"dhcp-socket-type": "udp"}
    assert "host-reservation-identifiers" not in srv.extra  # fully consumed
    assert srv.extra.get("decline-probation-period") == 86400


def test_ddns_settings_captured_first_class():
    """ddns-* + hostname-char-* on a subnet are first-class, not extra passthrough."""
    config = {
        "subnet4": [{
            "id": 1, "subnet": "10.0.10.0/24",
            "ddns-send-updates": True,
            "ddns-override-no-update": False,
            "ddns-qualifying-suffix": "example.org.",
            "ddns-replace-client-name": "when-present",
            "ddns-conflict-resolution-mode": "check-with-dhcid",
            "ddns-update-on-renew": True,
            "ddns-ttl-percent": 0.33,
            "hostname-char-set": "[^A-Za-z0-9.-]",
            "hostname-char-replacement": "x",
        }],
    }
    a = KeaAdapter(config=config, server_name="kea4", family=4)
    a.load()
    s = a.get("dhcpscope", {"server_name": "kea4", "prefix": "10.0.10.0/24"})
    assert s.ddns_send_updates is True
    assert s.ddns_override_no_update is False
    assert s.ddns_qualifying_suffix == "example.org."
    assert s.ddns_replace_client_name == "when-present"
    assert s.ddns_conflict_resolution_mode == "check-with-dhcid"
    assert s.ddns_update_on_renew is True
    assert s.ddns_ttl_percent == 0.33
    assert s.hostname_char_set == "[^A-Za-z0-9.-]"
    assert s.hostname_char_replacement == "x"
    # None of them leaked into the passthrough.
    assert not any(k.startswith("ddns-") or k.startswith("hostname-char") for k in s.extra)


def test_shared_network_captured_with_members():
    """A shared-network is captured with its operational fields; members link by name."""
    config = {
        "shared-networks": [
            {
                "name": "campus-a",
                "interface": "eth0",
                "valid-lifetime": 7200,
                "relay": {"ip-addresses": ["10.0.0.1"]},
                "require-client-classes": ["corp"],
                "rapid-commit": True,
                "ddns-send-updates": False,  # unmodeled -> shared-network extra
                "option-data": [{"code": 6, "data": "10.0.0.10"}],  # rides extra (lossless)
                "subnet4": [
                    {"id": 1, "subnet": "10.10.0.0/24", "pools": [{"pool": "10.10.0.10 - 10.10.0.250"}]},
                    {"id": 2, "subnet": "10.20.0.0/24"},
                ],
            }
        ],
        "subnet4": [{"id": 3, "subnet": "10.99.0.0/24"}],  # standalone
    }
    a = KeaAdapter(config=config, server_name="kea4", family=4)
    a.load()

    sn = a.get("dhcpsharednetwork", {"server_name": "kea4", "name": "campus-a"})
    assert sn.interface == "eth0"
    assert sn.default_lease_time == 7200
    assert sn.relay_addresses == ["10.0.0.1"]
    assert sn.require_client_classes == ["corp"]
    assert sn.rapid_commit is True
    assert sn.extra.get("ddns-send-updates") is False  # unmodeled key preserved
    assert sn.extra.get("option-data") == [{"code": 6, "data": "10.0.0.10"}]  # options ride extra

    # Member subnets link to the shared-network by name.
    m1 = a.get("dhcpscope", {"server_name": "kea4", "prefix": "10.10.0.0/24"})
    m2 = a.get("dhcpscope", {"server_name": "kea4", "prefix": "10.20.0.0/24"})
    assert m1.shared_network == "campus-a"
    assert m2.shared_network == "campus-a"
    # A member that omits valid-lifetime inherits the shared-network's.
    assert m2.default_lease_time == 7200

    # The standalone subnet has no shared-network.
    standalone = a.get("dhcpscope", {"server_name": "kea4", "prefix": "10.99.0.0/24"})
    assert standalone.shared_network == ""


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
