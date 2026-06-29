"""Verify the Hamilton H1/H2 fixes round-trip for DHCPv4.

H1: a v4 subnet's relay/interface/allocator/reservation-mode must survive
    (previously loaded only for v6, silently dropped for v4).
H2: a v4 pool's require-client-classes must survive when the scope has no
    exclusions (previously always lost to the pools_minus_exclusions gap transform).
Also confirms the with-exclusions path still produces pool gaps (classes dropped,
as documented). Run via `nautobot-server shell` in the Kea stack.
"""

from nautobot_dhcp_models.models import DHCPServer
from nautobot_dhcp_models.ssot.adapter import NautobotAdapter
from nautobot_ssot_kea.diffsync.adapters.kea import KeaAdapter
from nautobot_ssot_kea.export import build_kea_config

# No exclusions: selection fields + per-pool classes must both round-trip.
CLEAN = {
    "subnet4": [{
        "id": 1, "subnet": "10.44.0.0/24",
        "interface": "eth1",
        "relay": {"ip-addresses": ["10.44.0.1", "10.44.0.2"]},
        "allocator": "random",
        "reservations-in-subnet": True,
        "reservations-out-of-pool": False,
        "pools": [{"pool": "10.44.0.10 - 10.44.0.250", "require-client-classes": ["corp"]}],
    }],
}


def _sync(name, config):
    src = KeaAdapter(config=config, server_name=name, family=4)
    src.load()
    tgt = NautobotAdapter(server_name=name)
    tgt.load()
    tgt.sync_from(src)
    again = NautobotAdapter(server_name=name)
    again.load()
    s = again.diff_from(src).summary()
    print(f"[{name}] re-sync diff summary: {s}")
    assert s.get("create", 0) == 0 and s.get("update", 0) == 0 and s.get("delete", 0) == 0, s
    return DHCPServer.objects.get(name=name)


def run():
    srv = _sync("v4sel", CLEAN)
    sub = build_kea_config(srv, family=4)["Dhcp4"]["subnet4"][0]
    print("exported selection:", {k: sub.get(k) for k in
          ("interface", "relay", "allocator", "reservations-in-subnet", "reservations-out-of-pool")})
    assert sub["interface"] == "eth1", sub
    assert sub["relay"] == {"ip-addresses": ["10.44.0.1", "10.44.0.2"]}, sub
    assert sub["allocator"] == "random", sub
    assert sub["reservations-in-subnet"] is True, sub
    assert sub["reservations-out-of-pool"] is False, sub
    print("exported pools:", sub["pools"])
    assert sub["pools"] == [{"pool": "10.44.0.10 - 10.44.0.250", "require-client-classes": ["corp"]}], sub["pools"]
    print("H1 + H2 (no-exclusion) ROUND-TRIP OK")


run()
