"""Verify Kea lease-dump sync + churn-control, via `nautobot-server shell`."""

import json

from nautobot_dhcp_models.ssot.adapter import NautobotAdapter
from nautobot_ssot_kea.diffsync.adapters.kea import KeaAdapter
from nautobot_ssot_kea.utils.kea import parse_kea_leases_csv


def run():
    raw = json.load(open("/opt/plugin/tests/fixtures/kea-dhcp4.conf"))
    config = raw.get("Dhcp4", raw)
    leases = parse_kea_leases_csv(open("/opt/plugin/tests/fixtures/kea-leases4.csv").read())

    src = KeaAdapter(config=config, server_name="kea01", leases=leases)
    src.load()
    tgt = NautobotAdapter(server_name="kea01")
    tgt.load()
    src.sync_to(tgt)

    from nautobot_dhcp_models.models import DHCPLease

    qs = DHCPLease.objects.filter(scope__server__name="kea01").order_by("ip_address")
    print("kea01 leases:", qs.count())
    for lease in qs:
        print(f"  {lease.ip_address}  {lease.mac_address}  {lease.lease_state:9}  valid_during={lease.valid_during}")

    # Idempotency: re-sync with the same dump is a no-op.
    s2 = KeaAdapter(config=config, server_name="kea01", leases=leases)
    s2.load()
    t2 = NautobotAdapter(server_name="kea01")
    t2.load()
    print("second-sync:", s2.diff_to(t2).summary())

    # Churn-control: a new holder on .50 -> amend (belief rotates).
    leases2 = [dict(x) for x in leases]
    for x in leases2:
        if x["address"] == "10.0.10.50":
            x["hwaddr"] = "aa:bb:cc:dd:ee:99"
    s3 = KeaAdapter(config=config, server_name="kea01", leases=leases2)
    s3.load()
    t3 = NautobotAdapter(server_name="kea01")
    t3.load()
    s3.sync_to(t3)
    av = DHCPLease.all_versions.filter(scope__server__name="kea01", ip_address="10.0.10.50").count()
    cur = DHCPLease.objects.get(scope__server__name="kea01", ip_address="10.0.10.50")
    print(f"new-holder on .50 -> all_versions={av} current_mac={cur.mac_address}")


run()
