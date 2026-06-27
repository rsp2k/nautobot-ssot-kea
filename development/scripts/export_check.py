"""Verify build_kea_config end-to-end, via `nautobot-server shell`.

1. Round-trips kea01 (synced from the Kea fixture) back out to Kea config.
2. Stands up an MS-style scope with an exclusion and shows the exclusion become a
   Kea pool gap -- the migration payoff.
"""

import json

from django.contrib.contenttypes.models import ContentType
from nautobot.extras.models import Status
from nautobot.ipam.models import Namespace, Prefix

from nautobot_dhcp_models.models import (
    DHCPExclusion,
    DHCPPool,
    DHCPScope,
    DHCPServer,
)
from nautobot_ssot_kea.export import build_kea_config


def run():
    # 1) Round-trip the real kea01 data.
    kea = build_kea_config(DHCPServer.objects.get(name="kea01"))
    subs = {s["subnet"]: s for s in kea["Dhcp4"]["subnet4"]}
    print("=== kea01 export ===")
    print("subnets:", sorted(subs))
    assert subs["10.0.10.0/24"]["pools"] == [{"pool": "10.0.10.10 - 10.0.10.250"}], subs["10.0.10.0/24"]["pools"]
    print("10.0.10.0/24 pools:", subs["10.0.10.0/24"]["pools"])
    print("10.0.10.0/24 reservations:", subs["10.0.10.0/24"].get("reservations"))
    print("server option-data:", kea["Dhcp4"].get("option-data"))
    print("ROUND-TRIP OK")

    # 2) MS-style scope (range + exclusion) -> exported as Kea pool gaps.
    status = Status.objects.get(name="Active")
    status.content_types.add(ContentType.objects.get_for_model(DHCPServer))
    ns = Namespace.objects.get(name="Global")
    srv, _ = DHCPServer.objects.get_or_create(name="ms-export-demo", defaults={"vendor": "microsoft", "status": status})
    pfx, _ = Prefix.objects.get_or_create(
        prefix="10.9.0.0/24", namespace=ns, defaults={"status": status, "type": "network"}
    )
    scope, _ = DHCPScope.objects.get_or_create(server=srv, prefix=pfx)
    DHCPPool.objects.get_or_create(scope=scope, start_address="10.9.0.10", end_address="10.9.0.250")
    DHCPExclusion.objects.get_or_create(scope=scope, start_address="10.9.0.50", end_address="10.9.0.60")

    cfg = build_kea_config(srv)
    pools = cfg["Dhcp4"]["subnet4"][0]["pools"]
    print("\n=== ms-export-demo export (exclusion .50-.60) ===")
    print("10.9.0.0/24 pools:", pools)
    assert pools == [{"pool": "10.9.0.10 - 10.9.0.49"}, {"pool": "10.9.0.61 - 10.9.0.250"}], pools
    print("EXCLUSION -> POOL GAP OK")


run()
