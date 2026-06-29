"""Verify DDNS settings round-trip: Kea config -> store -> Kea config.

Promotes the per-subnet ddns-* / hostname-char-* keys from the extra passthrough
to first-class diffable columns. Confirms symmetric load (no perpetual diff) and
that the exporter re-emits every key. Run via `nautobot-server shell` in the Kea stack.
"""

from nautobot_dhcp_models.models import DHCPServer
from nautobot_dhcp_models.ssot.adapter import NautobotAdapter
from nautobot_ssot_kea.diffsync.adapters.kea import KeaAdapter
from nautobot_ssot_kea.export import build_kea_config

V4 = {
    "subnet4": [{
        "id": 1, "subnet": "10.88.0.0/24",
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


def run():
    name = "ddnscheck-v4"
    src = KeaAdapter(config=V4, server_name=name, family=4)
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
    sub = build_kea_config(srv, family=4)["Dhcp4"]["subnet4"][0]
    print("exported ddns keys:", {k: v for k, v in sub.items() if k.startswith(("ddns-", "hostname-char"))})
    assert sub["ddns-send-updates"] is True, sub
    assert sub["ddns-override-no-update"] is False, sub
    assert sub["ddns-qualifying-suffix"] == "example.org.", sub
    assert sub["ddns-replace-client-name"] == "when-present", sub
    assert sub["ddns-conflict-resolution-mode"] == "check-with-dhcid", sub
    assert sub["ddns-update-on-renew"] is True, sub
    assert sub["ddns-ttl-percent"] == 0.33, sub
    assert sub["hostname-char-set"] == "[^A-Za-z0-9.-]", sub
    assert sub["hostname-char-replacement"] == "x", sub
    print("DDNS ROUND-TRIP OK")


run()
