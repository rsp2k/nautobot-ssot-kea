"""Source adapter: load an ISC Kea DHCPv4 config into DiffSync.

This is the read side (PULL). It parses a ``kea-dhcp4.conf`` ``Dhcp4`` object and
normalizes every value to its dhcp-models-native form (CIDR prefix, pool range,
MAC, option data) so the diff against the Nautobot side is apples-to-apples.

Config-only: Kea leases live in the lease database (memfile / lease_cmds /
SQL backend), NOT in ``kea-dhcp4.conf``. So this adapter emits NO leases. Lease
sync would need a separate memfile / ``lease4-get-all`` dump and is future work.
Kea config also has no explicit exclusion concept, so no exclusions are emitted.
"""

from __future__ import annotations

from diffsync import Adapter
from nautobot_dhcp_models.ssot.base import (
    DhcpExclusion,
    DhcpLease,
    DhcpOption,
    DhcpPool,
    DhcpReservation,
    DhcpScope,
    DhcpServer,
)

from nautobot_ssot_kea.utils.kea import (
    normalize_mac,
    normalize_option_data,
    parse_kea_pool,
)

# Kea reservation identifier keys, in preference order. hw-address maps cleanly
# to a MAC; the others are opaque client identifiers passed through normalize_mac
# (which leaves non-MAC values lowercased but otherwise intact).
_RESERVATION_ID_KEYS = ("hw-address", "client-id", "duid", "circuit-id", "flex-id")


class KeaAdapter(Adapter):
    """Load a parsed Kea ``Dhcp4`` config dict into the DiffSync store."""

    dhcpserver = DhcpServer
    dhcpscope = DhcpScope
    dhcppool = DhcpPool
    dhcpexclusion = DhcpExclusion
    dhcpreservation = DhcpReservation
    dhcpoption = DhcpOption
    dhcplease = DhcpLease

    top_level = (
        "dhcpserver",
        "dhcpscope",
        "dhcppool",
        "dhcpexclusion",
        "dhcpreservation",
        "dhcpoption",
        "dhcplease",
    )

    def __init__(self, *args, config: dict, server_name: str, job=None, sync=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = config
        self.server_name = server_name
        self.job = job
        self.sync = sync

    def load(self) -> None:
        """Walk the config: server, global options, then each subnet and its children."""
        server_name = self.server_name
        if not server_name:
            raise ValueError("KeaAdapter requires a server_name (Kea config has no server identity)")

        # Kea config carries no AD-authorization concept; leave it unknown.
        self.add(self.dhcpserver(name=server_name, vendor="kea", ad_authorized=None))

        for opt in self.config.get("option-data", []):
            self._add_option(server_name, "", "", opt)

        global_lifetime = self.config.get("valid-lifetime")
        for subnet in self.config.get("subnet4", []):
            self._load_subnet(server_name, subnet, global_lifetime)

    def _load_subnet(self, server_name: str, subnet: dict, global_lifetime) -> None:
        prefix = subnet["subnet"]  # Kea subnets are already CIDR.
        self.add(
            self.dhcpscope(
                server_name=server_name,
                prefix=prefix,
                name="",  # Kea subnets have no name attribute.
                state="enabled",
                default_lease_time=subnet.get("valid-lifetime") or global_lifetime or 86400,
                description=subnet.get("comment") or subnet.get("user-context-description") or "",
            )
        )
        for pool in subnet.get("pools", []):
            start, end = parse_kea_pool(pool.get("pool", ""))
            self.add(
                self.dhcppool(
                    server_name=server_name,
                    prefix=prefix,
                    start_address=start,
                    end_address=end,
                )
            )
        for opt in subnet.get("option-data", []):
            self._add_option(server_name, prefix, "", opt)
        for res in subnet.get("reservations", []):
            self._load_reservation(server_name, prefix, res)

    def _load_reservation(self, server_name: str, prefix: str, res: dict) -> None:
        ip = res["ip-address"]
        identifier = ""
        for key in _RESERVATION_ID_KEYS:
            if res.get(key):
                identifier = res[key]
                break
        self.add(
            self.dhcpreservation(
                server_name=server_name,
                prefix=prefix,
                ip_address=ip,
                mac_address=normalize_mac(identifier),
                hostname=res.get("hostname", ""),
                reservation_type="dhcp",
                description=res.get("comment", ""),
            )
        )
        for opt in res.get("option-data", []):
            self._add_option(server_name, prefix, ip, opt)

    def _add_option(self, server_name: str, scope_prefix: str, reservation_ip: str, opt: dict) -> None:
        self.add(
            self.dhcpoption(
                server_name=server_name,
                scope_prefix=scope_prefix,
                reservation_ip=reservation_ip,
                code=int(opt["code"]),
                value=normalize_option_data(opt.get("data")),
                option_name=opt.get("name", ""),
                # Kea config doesn't carry the option data type; the shared
                # optdef get_or_create resolves it on the target side.
                data_type="string",
            )
        )
