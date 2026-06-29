"""Verify client-class associations round-trip: Kea config -> store -> Kea config.

Exercises the new first-class fields (require-client-classes on scope/pool/pd-pool,
client-classes on reservations) end-to-end through the source adapter, the Nautobot
target CRUD, a re-load (must show NO diff -> symmetric), and the exporter.
Run via `nautobot-server shell < client_class_check.py` in the Kea dev stack.
"""

from nautobot_dhcp_models.models import DHCPServer
from nautobot_dhcp_models.ssot.adapter import NautobotAdapter
from nautobot_ssot_kea.diffsync.adapters.kea import KeaAdapter
from nautobot_ssot_kea.export import build_kea_config

V4 = {
    "subnet4": [{
        "id": 1, "subnet": "10.55.0.0/24",
        "require-client-classes": ["corp", "voip"],
        "pools": [{"pool": "10.55.0.10 - 10.55.0.250", "evaluate-additional-classes": ["guest"]}],
        "reservations": [{
            "hw-address": "00:11:22:33:44:55", "ip-address": "10.55.0.5",
            "client-classes": ["printer"],
        }],
    }],
}
V6 = {
    "subnet6": [{
        "id": 1, "subnet": "2001:db8:55::/64",
        "pd-pools": [{
            "prefix": "2001:db8:beef::", "prefix-len": 48, "delegated-len": 56,
            "require-client-classes": ["wholesale"],
        }],
        "reservations": [{
            "duid": "00:03:00:01:aa:bb:cc:dd:ee:55",
            "ip-addresses": ["2001:db8:55::5"], "prefixes": ["2001:db8:beef:100::/56"],
            "client-classes": ["business"],
        }],
    }],
}


def _sync(server_name, config, family):
    src = KeaAdapter(config=config, server_name=server_name, family=family)
    src.load()
    tgt = NautobotAdapter(server_name=server_name)
    tgt.load()
    tgt.sync_from(src)
    # Re-load the target and diff again: a non-empty diff = perpetual-diff bug.
    again = NautobotAdapter(server_name=server_name)
    again.load()
    diff = again.diff_from(src)
    summary = diff.summary()
    print(f"[{server_name}] re-sync diff summary: {summary}")
    assert summary.get("create", 0) == 0 and summary.get("update", 0) == 0 and summary.get("delete", 0) == 0, (
        f"PERPETUAL DIFF for {server_name}: {summary}"
    )
    return DHCPServer.objects.get(name=server_name)


def run():
    srv4 = _sync("ccheck-v4", V4, 4)
    out4 = build_kea_config(srv4, family=4)["Dhcp4"]
    sub4 = out4["subnet4"][0]
    print("v4 subnet require-client-classes:", sub4.get("require-client-classes"))
    assert sub4["require-client-classes"] == ["corp", "voip"], sub4
    res4 = sub4["reservations"][0]
    print("v4 reservation client-classes:", res4.get("client-classes"))
    assert res4["client-classes"] == ["printer"], res4

    srv6 = _sync("ccheck-v6", V6, 6)
    out6 = build_kea_config(srv6, family=6)["Dhcp6"]
    sub6 = out6["subnet6"][0]
    pdp = sub6["pd-pools"][0]
    print("v6 pd-pool require-client-classes:", pdp.get("require-client-classes"))
    assert pdp["require-client-classes"] == ["wholesale"], pdp
    r6 = sub6["reservations"][0]
    print("v6 reservation client-classes:", r6.get("client-classes"))
    assert r6["client-classes"] == ["business"], r6

    print("CLIENT-CLASS ROUND-TRIP OK")


run()
