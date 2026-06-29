"""Verify daemon-level server config round-trips, incl. the nested-block remainder.

Promotes dhcp-ddns (connection), interfaces-config interfaces, and
host-reservation-identifiers to first-class DHCPServer fields while keeping the
un-promoted siblings (max-queue-size, dhcp-socket-type) in extra. Confirms
symmetric load (no perpetual diff) and that export rebuilds the nested blocks by
merging fields over the remainder. Run via `nautobot-server shell` in the Kea stack.
"""

from nautobot_dhcp_models.models import DHCPServer
from nautobot_dhcp_models.ssot.adapter import NautobotAdapter
from nautobot_ssot_kea.diffsync.adapters.kea import KeaAdapter
from nautobot_ssot_kea.export import build_kea_config

CONFIG = {
    "interfaces-config": {"interfaces": ["eth0", "eth1"], "dhcp-socket-type": "udp"},
    "dhcp-ddns": {"enable-updates": True, "server-ip": "127.0.0.1", "server-port": 53001, "max-queue-size": 1024},
    "host-reservation-identifiers": ["hw-address", "duid", "client-id"],
    "subnet4": [{"id": 1, "subnet": "10.66.0.0/24"}],
}


def run():
    name = "srvcheck-v4"
    src = KeaAdapter(config=CONFIG, server_name=name, family=4)
    src.load()
    tgt = NautobotAdapter(server_name=name)
    tgt.load()
    tgt.sync_from(src)
    again = NautobotAdapter(server_name=name)
    again.load()
    s = again.diff_from(src).summary()
    print(f"[{name}] re-sync diff summary: {s}")
    assert s.get("create", 0) == 0 and s.get("update", 0) == 0 and s.get("delete", 0) == 0, s

    srv = DHCPServer.objects.get(name=name)
    print("fields:", srv.ddns_enabled, srv.ddns_server_ip, srv.ddns_server_port,
          srv.listen_interfaces, srv.host_identifier_priority)
    assert srv.ddns_enabled is True
    assert srv.listen_interfaces == ["eth0", "eth1"]
    assert srv.host_identifier_priority == ["hw-address", "duid", "client-id"]

    out = build_kea_config(srv, family=4)["Dhcp4"]
    print("exported dhcp-ddns:", out.get("dhcp-ddns"))
    print("exported interfaces-config:", out.get("interfaces-config"))
    print("exported host-reservation-identifiers:", out.get("host-reservation-identifiers"))
    # Nested blocks rebuilt: promoted fields + preserved siblings.
    assert out["dhcp-ddns"] == {
        "enable-updates": True, "server-ip": "127.0.0.1", "server-port": 53001, "max-queue-size": 1024,
    }, out["dhcp-ddns"]
    assert out["interfaces-config"] == {"interfaces": ["eth0", "eth1"], "dhcp-socket-type": "udp"}, out["interfaces-config"]
    assert out["host-reservation-identifiers"] == ["hw-address", "duid", "client-id"], out
    print("SERVER DAEMON ROUND-TRIP OK")


run()
