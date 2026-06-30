"""SSoT Job: ISC Kea DHCPv4 config -> nautobot-dhcp-models (one-way)."""

from __future__ import annotations

import json
import re

from diffsync.enum import DiffSyncFlags
from nautobot.apps.jobs import BooleanVar, ChoiceVar, FileVar, Job, ObjectVar, StringVar, register_jobs
from nautobot_dhcp_models.models import DHCPServer
from nautobot_dhcp_models.ssot.adapter import NautobotAdapter
from nautobot_ssot.jobs.base import DataSource

from nautobot_ssot_kea.diffsync.adapters.kea import KeaAdapter
from nautobot_ssot_kea.export import (
    DEFAULT_HOOKS_DIR,
    add_option_def,
    build_cutover_config,
    build_kea_config,
    find_option_codes_by_data,
    strip_option_code,
)
from nautobot_ssot_kea.utils.ctrl_agent import KeaCommandError, KeaControlAgent
from nautobot_ssot_kea.utils.kea import kea_api_lease_to_row, parse_kea_leases6_csv, parse_kea_leases_csv

name = "ISC Kea DHCP SSoT"  # noqa: F841 -- grouping label in the Jobs UI


class KeaDataSource(DataSource):
    """Sync an ISC Kea DHCPv4 config into nautobot-dhcp-models."""

    config_file = FileVar(
        label="Kea config (kea-dhcp4.conf / kea-dhcp6.conf)",
        description=(
            'The ISC Kea JSON config. Either a full {"Dhcp4": {...}} or {"Dhcp6": {...}} file, '
            "or the inner object. Family is auto-detected (a subnet6 key means DHCPv6)."
        ),
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
        # Accept a full {"Dhcp4"/"Dhcp6": {...}} file or just the inner object,
        # and detect the family (a subnet6 key means DHCPv6).
        if "Dhcp6" in cfg:
            self.config, self.family = cfg["Dhcp6"], 6
        elif "Dhcp4" in cfg:
            self.config, self.family = cfg["Dhcp4"], 4
        else:
            self.config, self.family = cfg, (6 if "subnet6" in cfg else 4)
        lease_parser = parse_kea_leases6_csv if self.family == 6 else parse_kea_leases_csv
        self.leases = lease_parser(self.lease_file.read().decode("utf-8")) if self.lease_file else []
        self.logger.info(
            f"Loaded Kea DHCPv{self.family} config for server {self.server_name!r} "
            f"({len(self.leases)} lease(s) from dump)."
        )
        super().run(*args, **kwargs)

    def load_source_adapter(self) -> None:
        """Build the Kea adapter from the parsed config."""
        self.source_adapter = KeaAdapter(
            config=self.config,
            server_name=self.server_name,
            leases=self.leases,
            family=self.family,
            job=self,
            sync=self.sync,
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


class KeaLiveDataSource(DataSource):
    """Pull a Kea server's running config + leases live via its Control Agent REST API.

    The connected counterpart to KeaDataSource: instead of uploading a config and a
    lease CSV, point at the Control Agent URL and the job runs config-get +
    lease{4,6}-get-all. Same downstream sync (the Kea adapter + Nautobot target).
    """

    ca_url = StringVar(
        label="Control Agent URL",
        description="The kea-ctrl-agent endpoint, e.g. http://kea01:8000/",
    )
    server_name = StringVar(
        label="Kea server name",
        description="Logical name for this Kea instance; becomes the DHCPServer name.",
    )
    family = ChoiceVar(
        choices=(("dhcp4", "DHCPv4"), ("dhcp6", "DHCPv6")),
        default="dhcp4",
        label="Service",
        description="Which Kea daemon to pull from.",
    )
    include_leases = BooleanVar(
        default=True,
        label="Pull leases",
        description="Also pull current leases via lease4/6-get-all (needs the lease_cmds hook).",
    )
    delete_records_missing_from_source = BooleanVar(
        default=False,
        label="Delete records missing from the server",
        description="If True, delete Nautobot records absent from the live config. Default: additive only.",
    )

    class Meta:
        """Job metadata shown in the SSoT dashboard."""

        name = "Kea (live API) -> Nautobot"
        data_source = "ISC Kea Control Agent"
        data_target = "Nautobot"
        description = "Pull a Kea server's running config + leases over the Control Agent REST API."

    @classmethod
    def data_mappings(cls):
        """Describe the source->target mapping shown on the job detail page."""
        from nautobot_ssot.contrib.types import DataMapping  # noqa: PLC0415

        return (
            DataMapping("config-get", None, "DHCP Scope / Pool / Option", None),
            DataMapping("lease4/6-get-all", None, "DHCP Lease", None),
        )

    def run(self, *args, **kwargs):  # type: ignore[override]
        """Connect to the CA, pull config + leases, then run the standard SSoT sync."""
        self.ca_url = kwargs["ca_url"]
        self.server_name = kwargs["server_name"]
        self.family = 6 if kwargs["family"] == "dhcp6" else 4
        self.delete_records_missing_from_source = kwargs["delete_records_missing_from_source"]
        if not self.server_name:
            raise ValueError("A Kea server name is required; the config carries no server identity.")

        service = f"dhcp{self.family}"
        ca = KeaControlAgent(self.ca_url)
        status = ca.status_get(service)
        self.logger.info(f"Connected to Kea Control Agent at {self.ca_url} (pid {status.get('pid')}).")

        full = ca.config_get(service)
        key = "Dhcp6" if self.family == 6 else "Dhcp4"
        self.config = full.get(key, full)

        self.leases = []
        if kwargs["include_leases"]:
            self.leases = [kea_api_lease_to_row(lease) for lease in ca.leases_get_all(self.family)]
        self.logger.info(
            f"Pulled Kea DHCPv{self.family} config for {self.server_name!r} ({len(self.leases)} live lease(s))."
        )
        super().run(*args, **kwargs)

    def load_source_adapter(self) -> None:
        """Build the Kea adapter from the pulled config (same adapter as the file path)."""
        self.source_adapter = KeaAdapter(
            config=self.config,
            server_name=self.server_name,
            leases=self.leases,
            family=self.family,
            job=self,
            sync=self.sync,
        )
        self.source_adapter.load()
        self.logger.info(
            f"Loaded from server: {len(self.source_adapter.get_all('dhcpscope'))} subnet(s), "
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
            self.logger.info("Additive-only: Nautobot records absent from the live config were NOT deleted.")
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
        """Build the Kea config and attach it as a downloadable file (family auto-detected)."""
        config = build_kea_config(server)
        content = json.dumps(config, indent=2)
        is_v6 = "Dhcp6" in config
        filename = "kea-dhcp6.conf" if is_v6 else "kea-dhcp4.conf"
        self.create_file(filename, content)
        root = config["Dhcp6" if is_v6 else "Dhcp4"]
        subnets = root.get("subnet6" if is_v6 else "subnet4", [])
        reservations = sum(len(s.get("reservations", [])) for s in subnets)
        self.logger.info(
            "Exported %s: %d subnet(s), %d reservation(s) to %s.",
            server.name,
            len(subnets),
            reservations,
            filename,
        )
        return {
            "server": server.name,
            "family": 6 if is_v6 else 4,
            "subnets": len(subnets),
            "reservations": reservations,
        }


class KeaMigrateCutover(Job):
    """Push a stored server's config to a live 2-node Kea pair, made redundant via HA.

    The migration cutover: generate the Kea config from any DHCPServer in the store
    (e.g. an MS server synced in), inject HA between two Control-Agent nodes, and
    apply it live (config-set, optionally persisted with config-write). Each node's
    current config is pulled first so its plumbing -- control socket, interfaces,
    lease DB -- is preserved.
    """

    server = ObjectVar(
        model=DHCPServer,
        label="Source server to migrate",
        description="Any DHCPServer in dhcp-models -- e.g. the imported Microsoft server.",
    )
    node1_ca_url = StringVar(label="Node 1 Control Agent URL", description="e.g. http://172.30.0.11:8000/")
    node2_ca_url = StringVar(label="Node 2 Control Agent URL", description="e.g. http://172.30.0.12:8000/")
    node1_name = StringVar(default="node1", label="Node 1 HA name")
    node2_name = StringVar(default="node2", label="Node 2 HA name")
    ha_mode = ChoiceVar(
        choices=(("hot-standby", "Hot standby"), ("load-balancing", "Load balancing")),
        default="hot-standby",
        label="HA mode",
    )
    hooks_dir = StringVar(
        default=DEFAULT_HOOKS_DIR,
        label="Kea hooks directory",
        description="Where libdhcp_ha.so / libdhcp_lease_cmds.so live on the target nodes.",
    )
    persist = BooleanVar(
        default=True,
        label="Persist (config-write)",
        description="After applying in memory, write the config to disk so it survives a restart.",
    )
    dry_run = BooleanVar(
        default=True,
        label="Dry run",
        description="Build and validate the per-node configs but do NOT push. Uncheck to cut over for real.",
    )

    class Meta:
        """Job metadata shown in the Jobs UI."""

        name = "Kea Migration Cutover"
        description = (
            "Generate a server's Kea config, inject HA between two nodes, and push it live (config-set/write)."
        )

    @staticmethod
    def _apply_resilient(ca, config, service, max_repairs=60):
        """config-set, repairing what Kea rejects and retrying. Returns ``(dropped, defined)``.

        Real migrated data trips Kea two ways, and config-set is atomic so one bad
        option fails the whole push:

        * A value that doesn't fit Kea's definition (``...code: N``) -> strip code ``N``
          everywhere and retry. The option is lost (logged).
        * A non-standard code Kea has no built-in definition for ("not a valid string of
          hexadecimal digits: <data>") -> synthesize an ``option-def`` whose type is
          inferred from the data and retry. The option is *preserved*.

        Both repairs are idempotent on the code, so a self-correcting loop converges.
        """
        dropped: list = []
        defined: list = []
        for _ in range(max_repairs + 1):
            try:
                ca.config_set(config, service=service)
                return dropped, defined
            except KeaCommandError as exc:
                text = str(exc)
                match = re.search(r"code:\s*(\d+)", text)
                code = int(match.group(1)) if match else None
                if code is not None and code not in dropped:
                    dropped.append(code)
                    config = strip_option_code(config, code)
                    continue
                hexmatch = re.search(r"not a valid string of hexadecimal digits:\s*(\S+)", text)
                if hexmatch:
                    data = hexmatch.group(1)
                    repaired = False
                    for c in find_option_codes_by_data(config, data) - set(defined):
                        if add_option_def(config, c, data, space=service):
                            defined.append(c)
                            repaired = True
                    if repaired:
                        continue
                raise
        raise KeaCommandError(f"Gave up after {max_repairs} repairs (dropped={dropped}, defined={defined}).")

    def run(self, server, node1_ca_url, node2_ca_url, node1_name, node2_name, ha_mode, hooks_dir, persist, dry_run):  # type: ignore[override]
        """Generate, HA-inject, and push the config to both nodes (or preview on dry run)."""
        generated = build_kea_config(server)
        key = "Dhcp6" if "Dhcp6" in generated else "Dhcp4"
        service = "dhcp6" if key == "Dhcp6" else "dhcp4"
        standby_role = "secondary" if ha_mode == "load-balancing" else "standby"
        peers = [
            {"name": node1_name, "url": node1_ca_url.rstrip("/") + "/", "role": "primary", "auto-failover": True},
            {"name": node2_name, "url": node2_ca_url.rstrip("/") + "/", "role": standby_role, "auto-failover": True},
        ]
        subnet_count = len(generated[key].get("subnet6" if key == "Dhcp6" else "subnet4", []))
        self.logger.info(
            f"Migrating {server.name!r}: {subnet_count} {service} subnet(s) -> {node1_name} + {node2_name} "
            f"(HA {ha_mode}){' [DRY RUN]' if dry_run else ''}."
        )

        for name, url, this_name in (
            (node1_name, node1_ca_url, node1_name),
            (node2_name, node2_ca_url, node2_name),
        ):
            ca = KeaControlAgent(url)
            current = ca.config_get(service)  # preflight + plumbing source
            cfg = build_cutover_config(
                generated, current, this_server_name=this_name, peers=peers, mode=ha_mode, hooks_dir=hooks_dir
            )
            if dry_run:
                self.logger.info(
                    f"[dry run] {name}: would apply {subnet_count} subnet(s) with HA this-server={this_name}."
                )
                continue
            dropped, defined = self._apply_resilient(ca, cfg, service)
            if defined:
                self.logger.info(
                    f"{name}: defined {len(defined)} non-standard option(s) Kea lacked a built-in for "
                    f"(preserved with inferred types): codes {sorted(defined)}."
                )
            if dropped:
                self.logger.warning(
                    f"{name}: dropped {len(dropped)} option(s) Kea rejected (incompatible source data): "
                    f"codes {sorted(dropped)}."
                )
            self.logger.info(f"{name}: config applied (in memory).")
            if persist:
                ca.config_write(service=service)
                self.logger.info(f"{name}: config persisted to disk.")

        return {
            "server": server.name,
            "subnets": subnet_count,
            "nodes": [node1_name, node2_name],
            "ha_mode": ha_mode,
            "dry_run": dry_run,
            "persisted": persist and not dry_run,
        }


jobs = [KeaDataSource, KeaLiveDataSource, KeaConfigExport, KeaMigrateCutover]
register_jobs(*jobs)
