"""End-to-end sync check: fixture Kea config -> dhcp-models, run via `nautobot-server shell`.

Loads the source + target adapters, syncs, prints object counts, then re-syncs to
prove idempotency (a second run should produce an empty diff -- i.e. the bitemporal
amend/save logic does NOT cause perpetual drift).
"""

import json

from nautobot_dhcp_models.ssot.adapter import NautobotAdapter

from nautobot_ssot_kea.diffsync.adapters.kea import KeaAdapter

CONFIG_PATH = "/opt/plugin/tests/fixtures/kea-dhcp4.conf"
SERVER_NAME = "kea01"


def run():
    raw = json.load(open(CONFIG_PATH))
    config = raw.get("Dhcp4", raw)

    src = KeaAdapter(config=config, server_name=SERVER_NAME)
    src.load()
    tgt = NautobotAdapter(server_name=SERVER_NAME)
    tgt.load()

    print("=== first sync ===")
    src.sync_to(tgt)

    from nautobot_dhcp_models.models import (
        DHCPExclusion,
        DHCPLease,
        DHCPOption,
        DHCPPool,
        DHCPReservation,
        DHCPScope,
        DHCPServer,
    )

    print("servers     :", DHCPServer.objects.count())
    print("scopes      :", DHCPScope.objects.count())
    print("pools       :", DHCPPool.objects.count())
    print("exclusions  :", DHCPExclusion.objects.count())
    print("reservations:", DHCPReservation.objects.count())
    print("options     :", DHCPOption.objects.count())
    print("leases      :", DHCPLease.objects.count())

    # Re-load fresh adapters from the DB and diff: should be empty.
    src2 = KeaAdapter(config=config, server_name=SERVER_NAME)
    src2.load()
    tgt2 = NautobotAdapter(server_name=SERVER_NAME)
    tgt2.load()
    diff = src2.diff_to(tgt2)
    summary = diff.summary()
    print("=== second-sync diff summary (expect all 0 except no-change) ===")
    print(summary)
    assert summary.get("create", 0) == 0, f"idempotency broken: {summary}"
    assert summary.get("update", 0) == 0, f"idempotency broken: {summary}"
    print("IDEMPOTENT: second sync is a no-op.")


run()
