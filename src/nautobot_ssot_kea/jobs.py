"""SSoT Job: ISC Kea DHCPv4 config -> nautobot-dhcp-models (one-way)."""

from __future__ import annotations

import json

from diffsync.enum import DiffSyncFlags
from nautobot.apps.jobs import BooleanVar, FileVar, StringVar, register_jobs
from nautobot_dhcp_models.ssot.adapter import NautobotAdapter
from nautobot_ssot.jobs.base import DataSource

from nautobot_ssot_kea.diffsync.adapters.kea import KeaAdapter

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
        description = "Pull ISC Kea DHCPv4 subnets, pools, reservations, and options into dhcp-models."

    @classmethod
    def data_mappings(cls):
        """Describe the source->target mapping shown on the job detail page."""
        from nautobot_ssot.contrib.types import DataMapping  # noqa: PLC0415

        return (
            DataMapping("subnet4", None, "DHCP Scope", None),
            DataMapping("pools", None, "DHCP Pool", None),
            DataMapping("reservations", None, "DHCP Reservation", None),
            DataMapping("option-data", None, "DHCP Option", None),
        )

    def run(self, *args, **kwargs):  # type: ignore[override]
        """Parse the upload up-front, then run the standard SSoT sync."""
        self.config_file = kwargs["config_file"]
        self.server_name = kwargs["server_name"]
        self.delete_records_missing_from_source = kwargs["delete_records_missing_from_source"]
        if not self.server_name:
            raise ValueError("A Kea server name is required; the config carries no server identity.")
        cfg = json.loads(self.config_file.read().decode("utf-8"))
        # Accept either a full {"Dhcp4": {...}} file or just the inner object.
        self.config = cfg.get("Dhcp4", cfg)
        self.logger.info(f"Loaded Kea config for server {self.server_name!r}.")
        super().run(*args, **kwargs)

    def load_source_adapter(self) -> None:
        """Build the Kea adapter from the parsed config."""
        self.source_adapter = KeaAdapter(config=self.config, server_name=self.server_name, job=self, sync=self.sync)
        self.source_adapter.load()
        self.logger.info(
            f"Loaded from config: {len(self.source_adapter.get_all('dhcpscope'))} subnet(s), "
            f"{len(self.source_adapter.get_all('dhcppool'))} pool(s), "
            f"{len(self.source_adapter.get_all('dhcpreservation'))} reservation(s)."
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


jobs = [KeaDataSource]
register_jobs(*jobs)
