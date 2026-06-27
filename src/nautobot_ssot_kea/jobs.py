"""SSoT Job: ISC Kea DHCPv4 config -> nautobot-dhcp-models (one-way)."""

from __future__ import annotations

import json

from diffsync.enum import DiffSyncFlags
from nautobot.apps.jobs import BooleanVar, FileVar, Job, ObjectVar, StringVar, register_jobs
from nautobot_dhcp_models.models import DHCPServer
from nautobot_dhcp_models.ssot.adapter import NautobotAdapter
from nautobot_ssot.jobs.base import DataSource

from nautobot_ssot_kea.diffsync.adapters.kea import KeaAdapter
from nautobot_ssot_kea.export import build_kea_config
from nautobot_ssot_kea.utils.kea import parse_kea_leases_csv

name = "ISC Kea DHCP SSoT"  # noqa: F841 -- grouping label in the Jobs UI


class KeaDataSource(DataSource):
    """Sync an ISC Kea DHCPv4 config into nautobot-dhcp-models."""

    config_file = FileVar(
        label="Kea config (kea-dhcp4.conf)",
        description='The ISC Kea DHCPv4 JSON config. Either a full {"Dhcp4": {...}} file or the inner object.',
    )
    server_name = StringVar(
        label="Kea server name",
        description="Logical name for this Kea instance; becomes the DHCPServer name (Kea config carries no hostname).",
    )
    lease_file = FileVar(
        required=False,
        label="Kea lease dump (kea-leases4.csv, optional)",
        description="Optional memfile lease CSV (or kea-admin lease-dump output). Leases map to scopes by subnet id.",
    )
    delete_records_missing_from_source = BooleanVar(
        default=False,
        label="Delete records missing from the config",
        description=(
            "If True, delete Nautobot DHCP records absent from this config. "
            "If False (default), additive-only: create/update only, never delete."
        ),
    )

    class Meta:
        """Job metadata shown in the SSoT dashboard."""

        name = "Kea -> Nautobot"
        data_source = "ISC Kea"
        data_target = "Nautobot"
        description = (
            "Pull ISC Kea DHCPv4 subnets, pools, reservations, options, and (optional) leases into dhcp-models."
        )

    @classmethod
    def data_mappings(cls):
        """Describe the source->target mapping shown on the job detail page."""
        from nautobot_ssot.contrib.types import DataMapping  # noqa: PLC0415

        return (
            DataMapping("subnet4", None, "DHCP Scope", None),
            DataMapping("pools", None, "DHCP Pool", None),
            DataMapping("reservations", None, "DHCP Reservation", None),
            DataMapping("option-data", None, "DHCP Option", None),
            DataMapping("lease dump", None, "DHCP Lease", None),
        )

    def run(self, *args, **kwargs):  # type: ignore[override]
        """Parse the upload up-front, then run the standard SSoT sync."""
        self.config_file = kwargs["config_file"]
        self.server_name = kwargs["server_name"]
        self.lease_file = kwargs.get("lease_file")
        self.delete_records_missing_from_source = kwargs["delete_records_missing_from_source"]
        if not self.server_name:
            raise ValueError("A Kea server name is required; the config carries no server identity.")
        cfg = json.loads(self.config_file.read().decode("utf-8"))
        # Accept either a full {"Dhcp4": {...}} file or just the inner object.
        self.config = cfg.get("Dhcp4", cfg)
        self.leases = parse_kea_leases_csv(self.lease_file.read().decode("utf-8")) if self.lease_file else []
        self.logger.info(f"Loaded Kea config for server {self.server_name!r} ({len(self.leases)} lease(s) from dump).")
        super().run(*args, **kwargs)

    def load_source_adapter(self) -> None:
        """Build the Kea adapter from the parsed config."""
        self.source_adapter = KeaAdapter(
            config=self.config, server_name=self.server_name, leases=self.leases, job=self, sync=self.sync
        )
        self.source_adapter.load()
        self.logger.info(
            f"Loaded from config: {len(self.source_adapter.get_all('dhcpscope'))} subnet(s), "
            f"{len(self.source_adapter.get_all('dhcppool'))} pool(s), "
            f"{len(self.source_adapter.get_all('dhcpreservation'))} reservation(s), "
            f"{len(self.source_adapter.get_all('dhcplease'))} lease(s)."
        )

    def load_target_adapter(self) -> None:
        """Build the Nautobot adapter scoped to this server's existing records."""
        self.target_adapter = NautobotAdapter(server_name=self.server_name, job=self, sync=self.sync)
        self.target_adapter.load()

    def execute_sync(self) -> None:
        """Run the sync, honoring the additive-only default."""
        if not self.delete_records_missing_from_source:
            self.diffsync_flags |= DiffSyncFlags.SKIP_UNMATCHED_DST
            self.logger.info("Additive-only: Nautobot records absent from the config were NOT deleted.")
        super().execute_sync()


class KeaConfigExport(Job):
    """Generate a downloadable kea-dhcp4.conf from a DHCPServer's stored config.

    The reverse of the Kea data source: read any DHCPServer out of dhcp-models and
    emit the equivalent Kea config. Run it on a server that was synced *from
    Microsoft* and you get the migrated Kea config (exclusions become pool gaps).
    """

    server = ObjectVar(
        model=DHCPServer,
        label="DHCP server to export",
        description="Any DHCPServer in dhcp-models -- e.g. an MS server synced in becomes a Kea config out.",
    )

    class Meta:
        """Job metadata shown in the Jobs UI."""

        name = "Kea Config Export"
        description = "Generate a downloadable kea-dhcp4.conf from a DHCPServer's config in dhcp-models."

    def run(self, server):  # type: ignore[override]
        """Build the Kea config and attach it as a downloadable file."""
        config = build_kea_config(server)
        content = json.dumps(config, indent=2)
        self.create_file("kea-dhcp4.conf", content)
        subnets = config["Dhcp4"].get("subnet4", [])
        reservations = sum(len(s.get("reservations", [])) for s in subnets)
        self.logger.info(
            "Exported %s: %d subnet(s), %d reservation(s) to kea-dhcp4.conf.",
            server.name,
            len(subnets),
            reservations,
        )
        return {"server": server.name, "subnets": len(subnets), "reservations": reservations}


jobs = [KeaDataSource, KeaConfigExport]
register_jobs(*jobs)
