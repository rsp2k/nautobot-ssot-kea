"""Verify Kea HA -> redundancy, focusing on the CROSS-server correctness.

Two daemons of one HA relationship sync independently. Each must:
  - converge with no perpetual diff,
  - contribute its OWN member row to the shared group,
  - NOT delete the peer's member row when it re-syncs (per-server scoping).
Run via `nautobot-server shell` in the Kea stack.
"""

from nautobot_dhcp_models.models import DHCPRedundancyGroup
from nautobot_dhcp_models.ssot.adapter import NautobotAdapter
from nautobot_ssot_kea.diffsync.adapters.kea import KeaAdapter

PEERS = [
    {"name": "server1", "url": "http://10.0.0.1:8000/", "role": "primary"},
    {"name": "server2", "url": "http://10.0.0.2:8000/", "role": "secondary"},
]


def _config(this_server):
    return {
        "subnet4": [{"id": 1, "subnet": "10.0.0.0/24"}],
        "hooks-libraries": [{
            "library": "/usr/lib/kea/hooks/libdhcp_ha.so",
            "parameters": {"high-availability": [{
                "this-server-name": this_server,
                "mode": "load-balancing",
                "heartbeat-delay": 10000,
                "max-response-delay": 60000,
                "max-unacked-clients": 5,
                "peers": PEERS,
            }]},
        }],
    }


def _sync(server_name, this_server):
    src = KeaAdapter(config=_config(this_server), server_name=server_name, family=4)
    src.load()
    tgt = NautobotAdapter(server_name=server_name)
    tgt.load()
    tgt.sync_from(src)
    again = NautobotAdapter(server_name=server_name)
    again.load()
    s = again.diff_from(src).summary()
    print(f"[{server_name}] re-sync diff summary: {s}")
    assert s.get("create", 0) == 0 and s.get("update", 0) == 0 and s.get("delete", 0) == 0, s


def run():
    grp_name = "ha:server1,server2"

    _sync("redcheck-1", "server1")
    _sync("redcheck-2", "server2")

    g = DHCPRedundancyGroup.objects.get(name=grp_name)
    members = {m.server.name: m.role for m in g.members.all()}
    print("group members after both syncs:", members)
    assert members == {"redcheck-1": "primary", "redcheck-2": "secondary"}, members
    assert g.mode == "load-balance" and g.heartbeat_delay == 10000

    # Re-sync server1: must NOT delete server2's member (the cross-server invariant).
    _sync("redcheck-1", "server1")
    members_after = {m.server.name for m in DHCPRedundancyGroup.objects.get(name=grp_name).members.all()}
    print("members after re-syncing server1:", members_after)
    assert members_after == {"redcheck-1", "redcheck-2"}, members_after

    print("REDUNDANCY CROSS-SERVER ROUND-TRIP OK")


run()
