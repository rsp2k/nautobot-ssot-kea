"""End-to-end DHCPv6 + PD sync check: fixture kea-dhcp6.conf -> dhcp-models.

Syncs the v6 config (subnet6 + pd-pools + DUID reservations + dhcp6 options) plus
the v6 lease dump, prints object counts, re-syncs to prove idempotency (no
perpetual bitemporal drift), then exports the stored config back out and asserts
the round-trip: subnet6, pd-pools, and the flat reservations regrouped by DUID.
"""

import json

from nautobot_dhcp_models.ssot.adapter import NautobotAdapter

from nautobot_ssot_kea.diffsync.adapters.kea import KeaAdapter
from nautobot_ssot_kea.export import build_kea_config
from nautobot_ssot_kea.utils.kea import parse_kea_leases6_csv

CONFIG_PATH = "/opt/plugin/tests/fixtures/kea-dhcp6.conf"
LEASES_PATH = "/opt/plugin/tests/fixtures/kea-leases6.csv"
SERVER_NAME = "kea01-dhcp6"


def run():
    raw = json.load(open(CONFIG_PATH))
    config = raw.get("Dhcp6", raw)
    leases = parse_kea_leases6_csv(open(LEASES_PATH).read())

    src = KeaAdapter(config=config, server_name=SERVER_NAME, leases=leases, family=6)
    src.load()
    tgt = NautobotAdapter(server_name=SERVER_NAME)
    tgt.load()

    print("=== first sync ===")
    src.sync_to(tgt)

    from nautobot_dhcp_models.models import (
        DHCPDelegatedPrefixReservation,
        DHCPLease,
        DHCPOption,
        DHCPPool,
        DHCPPrefixDelegationPool,
        DHCPReservation,
        DHCPScope,
    )

    scope = DHCPScope.objects.get(server__name=SERVER_NAME, prefix__network="2001:db8:100::")
    print("scopes        :", DHCPScope.objects.filter(server__name=SERVER_NAME).count())
    print("  family      :", scope.family, "| preferred_lifetime:", scope.preferred_lifetime,
          "| pd_allocator:", scope.pd_allocator, "| relay:", scope.relay_addresses)
    print("pools         :", DHCPPool.objects.filter(scope__server__name=SERVER_NAME).count())
    print("pd_pools      :", DHCPPrefixDelegationPool.objects.filter(scope__server__name=SERVER_NAME).count())
    print("reservations  :", DHCPReservation.objects.filter(scope__server__name=SERVER_NAME).count())
    print("pd_reservs    :", DHCPDelegatedPrefixReservation.objects.filter(scope__server__name=SERVER_NAME).count())
    print("options       :", DHCPOption.objects.filter(option_definition__space="dhcp6").count())
    print("leases        :", DHCPLease.objects.filter(scope__server__name=SERVER_NAME).count())

    pdp = DHCPPrefixDelegationPool.objects.get(scope__server__name=SERVER_NAME)
    print("  pd_pool     :", pdp, "| excluded:", pdp.excluded_prefix, "/", pdp.excluded_prefix_length)
    pd_lease = DHCPLease.objects.get(scope__server__name=SERVER_NAME, lease_type="pd")
    print("  pd_lease    :", pd_lease.ip_address, "/", pd_lease.prefix_length, "duid:", pd_lease.duid)

    # Re-load fresh adapters and diff: must be empty (no perpetual bitemporal drift).
    src2 = KeaAdapter(config=config, server_name=SERVER_NAME, leases=leases, family=6)
    src2.load()
    tgt2 = NautobotAdapter(server_name=SERVER_NAME)
    tgt2.load()
    summary = src2.diff_to(tgt2).summary()
    print("=== second-sync diff summary (expect create/update = 0) ===")
    print(summary)
    assert summary.get("create", 0) == 0, f"idempotency broken: {summary}"
    assert summary.get("update", 0) == 0, f"idempotency broken: {summary}"
    print("IDEMPOTENT: second v6 sync is a no-op.")

    # Export back out and assert the round-trip.
    from nautobot_dhcp_models.models import DHCPServer

    out = build_kea_config(DHCPServer.objects.get(name=SERVER_NAME))
    assert "Dhcp6" in out, out.keys()
    subnets = {s["subnet"]: s for s in out["Dhcp6"]["subnet6"]}
    print("=== export ===")
    print("subnet6       :", sorted(subnets))
    s100 = subnets["2001:db8:100::/64"]
    assert s100["pd-pools"][0]["prefix"] == "2001:db8:cafe::", s100["pd-pools"]
    assert s100["pd-pools"][0]["delegated-len"] == 56
    assert s100["pd-pools"][0]["excluded-prefix"] == "2001:db8:cafe::"
    assert s100["preferred-lifetime"] == 86400
    assert s100["relay"] == {"ip-addresses": ["2001:db8:100::1"]}
    # The flat address + prefix reservations regroup into one DUID-keyed entry.
    res = s100["reservations"][0]
    assert res["duid"] == "00:03:00:01:aa:bb:cc:dd:ee:ff", res
    assert res["ip-addresses"] == ["2001:db8:100::5"], res
    assert res["prefixes"] == ["2001:db8:cafe:100::/56"], res
    print("pd-pool       :", s100["pd-pools"][0]["prefix"], "/", s100["pd-pools"][0]["delegated-len"])
    print("reservation   :", res)
    print("ROUND-TRIP OK")


run()
