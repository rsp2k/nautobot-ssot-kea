"""Cross-vendor demo: sync Kea + an MS-flavored server for the SAME subnet, compare.

Runs in the Kea container (has nautobot_ssot_kea + the shared nautobot_dhcp_models.ssot).
The MS side is stood up via an inline adapter using the shared base models, so no
msdhcp install is needed -- the point is that BOTH land as diffable DHCPScope rows.
"""

import json

from diffsync import Adapter

from nautobot_dhcp_models.ssot.adapter import NautobotAdapter
from nautobot_dhcp_models.ssot.base import DhcpOption, DhcpPool, DhcpScope, DhcpServer
from nautobot_ssot_kea.diffsync.adapters.kea import KeaAdapter


def run():
    # 1) Sync the Kea config as server "kea01".
    cfg = json.load(open("/opt/plugin/tests/fixtures/kea-dhcp4.conf"))
    cfg = cfg.get("Dhcp4", cfg)
    ksrc = KeaAdapter(config=cfg, server_name="kea01")
    ksrc.load()
    ktgt = NautobotAdapter(server_name="kea01")
    ktgt.load()
    ksrc.sync_to(ktgt)

    # 2) Sync an MS-flavored server for the SAME subnet, different lease/pool/router.
    class _MSish(Adapter):
        dhcpserver = DhcpServer
        dhcpscope = DhcpScope
        dhcppool = DhcpPool
        dhcpoption = DhcpOption
        top_level = ("dhcpserver", "dhcpscope", "dhcppool", "dhcpoption")

        def load(self):
            self.add(self.dhcpserver(name="ms-dhcp01", vendor="microsoft", ad_authorized=True))
            self.add(
                self.dhcpscope(
                    server_name="ms-dhcp01",
                    prefix="10.0.10.0/24",
                    name="VLAN10",
                    state="enabled",
                    default_lease_time=86400,
                    description="",
                )
            )
            self.add(
                self.dhcppool(
                    server_name="ms-dhcp01",
                    prefix="10.0.10.0/24",
                    start_address="10.0.10.10",
                    end_address="10.0.10.200",
                )
            )
            self.add(
                self.dhcpoption(
                    server_name="ms-dhcp01",
                    scope_prefix="10.0.10.0/24",
                    reservation_ip="",
                    code=3,
                    value="10.0.10.254",
                    option_name="Router",
                    data_type="ipv4-address",
                )
            )

    msrc = _MSish()
    msrc.load()
    mtgt = NautobotAdapter(server_name="ms-dhcp01")
    mtgt.load()
    msrc.sync_to(mtgt)

    # 3) Compare the SAME subnet across both vendors -- this is the migration delta.
    from nautobot.ipam.models import Namespace, Prefix

    from nautobot_dhcp_models.models import DHCPOption, DHCPPool, DHCPScope

    ns = Namespace.objects.get(name="Global")
    p = Prefix.objects.get(prefix="10.0.10.0/24", namespace=ns)
    print("\n=== 10.0.10.0/24 across vendors (one prefix, two diffable beliefs) ===")
    print(f"{'vendor':10} {'lease':>8}  {'pool':28}  routers")
    for s in DHCPScope.objects.filter(prefix=p).select_related("server").order_by("server__vendor"):
        pool = DHCPPool.objects.filter(scope=s).first()
        routers = DHCPOption.objects.filter(scope=s, option_definition__code=3).first()
        pool_str = f"{pool.start_address}-{pool.end_address}" if pool else "-"
        print(f"{s.server.vendor:10} {s.default_lease_time:>8}  {pool_str:28}  {routers.value if routers else '-'}")
    print()


run()
