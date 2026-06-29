"""Verify shared-network parity round-trips: Kea config -> store -> Kea config.

Exercises the new DhcpSharedNetwork sync (operational fields + member-subnet
linkage) end-to-end through the source adapter, the Nautobot target CRUD, a
re-load (must show NO diff -> symmetric), and the exporter (shared-networks[]
re-emitted with members nested). Run via `nautobot-server shell` in the Kea stack.
"""

from nautobot_dhcp_models.models import DHCPScope, DHCPServer, DHCPSharedNetwork
from nautobot_dhcp_models.ssot.adapter import NautobotAdapter
from nautobot_ssot_kea.diffsync.adapters.kea import KeaAdapter
from nautobot_ssot_kea.export import build_kea_config

V4 = {
    "shared-networks": [{
        "name": "sncheck-campus",
        "interface": "eth0",
        "valid-lifetime": 7200,
        "relay": {"ip-addresses": ["10.77.0.1"]},
        "require-client-classes": ["corp"],
        "option-data": [{"code": 6, "data": "10.77.0.10"}],  # rides extra
        "subnet4": [
            {"id": 1, "subnet": "10.77.10.0/24", "pools": [{"pool": "10.77.10.10 - 10.77.10.250"}]},
            {"id": 2, "subnet": "10.77.20.0/24"},
        ],
    }],
    "subnet4": [{"id": 3, "subnet": "10.77.99.0/24"}],  # standalone
}


def _sync(server_name, config, family):
    src = KeaAdapter(config=config, server_name=server_name, family=family)
    src.load()
    tgt = NautobotAdapter(server_name=server_name)
    tgt.load()
    tgt.sync_from(src)
    again = NautobotAdapter(server_name=server_name)
    again.load()
    s = again.diff_from(src).summary()
    print(f"[{server_name}] re-sync diff summary: {s}")
    assert s.get("create", 0) == 0 and s.get("update", 0) == 0 and s.get("delete", 0) == 0, (
        f"PERPETUAL DIFF for {server_name}: {s}"
    )
    return DHCPServer.objects.get(name=server_name)


def run():
    srv = _sync("sncheck-v4", V4, 4)

    # The shared-network landed with its operational fields.
    sn = DHCPSharedNetwork.objects.get(server=srv, name="sncheck-campus")
    print("shared-network:", sn.name, "interface=", sn.interface, "valid=", sn.default_lease_time)
    assert sn.interface == "eth0"
    assert sn.default_lease_time == 7200
    assert sn.relay_addresses == ["10.77.0.1"]
    assert sn.require_client_classes == ["corp"]

    # Member scopes carry the FK; the standalone one does not.
    m1 = DHCPScope.objects.get(server=srv, prefix__network="10.77.10.0")
    m2 = DHCPScope.objects.get(server=srv, prefix__network="10.77.20.0")
    standalone = DHCPScope.objects.get(server=srv, prefix__network="10.77.99.0")
    print("member FKs:", m1.shared_network_id, m2.shared_network_id, "standalone:", standalone.shared_network_id)
    assert m1.shared_network_id == sn.pk
    assert m2.shared_network_id == sn.pk
    assert standalone.shared_network_id is None

    # Export back: shared-networks[] with members nested, standalone at top level.
    out = build_kea_config(srv, family=4)["Dhcp4"]
    shared = {s["name"]: s for s in out.get("shared-networks", [])}
    print("exported shared-networks:", sorted(shared))
    assert "sncheck-campus" in shared, out
    entry = shared["sncheck-campus"]
    assert entry["interface"] == "eth0", entry
    assert entry["valid-lifetime"] == 7200, entry
    assert entry["relay"] == {"ip-addresses": ["10.77.0.1"]}, entry
    assert entry["require-client-classes"] == ["corp"], entry
    assert entry["option-data"] == [{"code": 6, "data": "10.77.0.10"}], entry  # rode extra
    member_subnets = {s["subnet"] for s in entry["subnet4"]}
    print("member subnets:", sorted(member_subnets))
    assert member_subnets == {"10.77.10.0/24", "10.77.20.0/24"}, entry
    top = {s["subnet"] for s in out["subnet4"]}
    assert top == {"10.77.99.0/24"}, out["subnet4"]

    print("SHARED-NETWORK ROUND-TRIP OK")


run()
